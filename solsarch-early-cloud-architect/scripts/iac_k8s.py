#!/usr/bin/env python3
"""Kubernetes manifests, a Helm chart and a Kustomize base, generated from the plan.

The same hardened workload definitions serve every provider that runs Kubernetes (Linode, Vultr, OVHcloud, Tencent,
CoreWeave and any cluster you already have), so providers differ only in how the cluster is created. Defaults follow
the production rules that real failures taught: resource requests and limits, a non-root read-only container,
liveness and readiness probes on the path the code serves, a pod disruption budget, spread across zones, a default-deny
network policy, and an ingress whose proxy settings keep streaming responses alive.
"""
import json
import re

from iac_common import IMAGE_TAG, Files, T, selfrun

DEFAULT_REGISTRY = "REGISTRY.example.com/PROJECT"
_PLAIN = re.compile(r"^[A-Za-z0-9_./@-][A-Za-z0-9 _./@:=+-]*$")
_RESERVED = ("true", "false", "null", "yes", "no", "on", "off", "~", "y", "n")


def yq(text):
    """A YAML double-quoted string."""
    return json.dumps(str(text))


def scalar(value):
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value)
    if (_PLAIN.match(text) and text.lower() not in _RESERVED and not re.match(r"^[-+]?[0-9.]+(e[-+]?[0-9]+)?$", text, re.I)
            and not text.endswith(":") and ": " not in text and " #" not in text and text == text.strip()):
        return text
    return json.dumps(text)


def _inline(value):
    if isinstance(value, dict):
        return "{}"
    if isinstance(value, list):
        return "[]"
    return scalar(value)


def _emit(value, indent, lines):
    pad = "  " * indent
    if isinstance(value, dict):
        for k, v in value.items():
            if isinstance(v, dict) and v:
                lines.append("%s%s:" % (pad, scalar(k)))
                _emit(v, indent + 1, lines)
            elif isinstance(v, list) and v:
                lines.append("%s%s:" % (pad, scalar(k)))
                _emit(v, indent + 1, lines)
            else:
                lines.append("%s%s: %s" % (pad, scalar(k), _inline(v)))
        return
    for item in value:
        if isinstance(item, dict) and item:
            sub = []
            _emit(item, indent + 1, sub)
            sub[0] = pad + "- " + sub[0].lstrip()
            lines.extend(sub)
        elif isinstance(item, list) and item:
            lines.append(pad + "-")
            _emit(item, indent + 1, lines)
        else:
            lines.append("%s- %s" % (pad, _inline(item)))


def dump(value):
    """A small deterministic YAML writer: keys keep the order they were written in."""
    lines = []
    _emit(value, 0, lines)
    return chr(10).join(lines) + chr(10)


class K8s:
    def __init__(self, model, registry=None, ingress_class="nginx", base="kubernetes"):
        self.m = model
        self.files = Files(model.fingerprint)
        self.registry = registry or DEFAULT_REGISTRY
        self.ingress_class = ingress_class
        self.base = base
        self.ns = model.project
        self.units = [u for u in model.units if u["kind"] != "static"]
        self.public = [u for u in model.public_units() if u["kind"] != "static"]
        self.cpu = float(model.sizing["unit_vcpu"])
        self.mem = float(model.sizing["unit_memory_gb"])
        self.reps = model.replicas

    # ------------------------------------------------------------- helpers
    def path_for(self, unit):
        if unit is self.public[0]:
            return "/"
        return "/api" if unit["kind"] == "api" or unit["name"] == "api" else "/%s" % unit["name"]

    def meta(self, name, labels=True, **extra):
        out = {"name": name, "namespace": self.ns}
        if labels:
            out["labels"] = {"app.kubernetes.io/name": name, "app.kubernetes.io/part-of": self.m.project,
                             "app.kubernetes.io/managed-by": "solsarch"}
        out.update(extra)
        return out

    # ------------------------------------------------------------- manifests
    def generate(self):
        base = self.base
        self.files.add("%s/namespace.yaml" % base, dump({
            "apiVersion": "v1", "kind": "Namespace",
            "metadata": {"name": self.ns, "labels": {"app.kubernetes.io/part-of": self.ns,
                                                     "pod-security.kubernetes.io/enforce": "restricted",
                                                     "pod-security.kubernetes.io/audit": "restricted"}}}))
        self.files.add("%s/serviceaccount.yaml" % base, dump({
            "apiVersion": "v1", "kind": "ServiceAccount", "metadata": self.meta(self.ns + "-app", labels=False),
            "automountServiceAccountToken": False}))
        self.files.add("%s/configmap.yaml" % base, self.configmap())
        self.files.add("%s/secret.example.yaml" % base, self.secret_example())
        resources = ["namespace.yaml", "serviceaccount.yaml", "configmap.yaml"]
        for unit in self.units:
            n = unit["name"]
            self.files.add("%s/deployment-%s.yaml" % (base, n), self.deployment(unit))
            resources.append("deployment-%s.yaml" % n)
            if unit["kind"] != "worker":
                self.files.add("%s/service-%s.yaml" % (base, n), self.service(unit))
                resources.append("service-%s.yaml" % n)
            self.files.add("%s/hpa-%s.yaml" % (base, n), self.hpa(unit))
            self.files.add("%s/pdb-%s.yaml" % (base, n), self.pdb(unit))
            resources += ["hpa-%s.yaml" % n, "pdb-%s.yaml" % n]
        for key, builder in (("database", self.postgres), ("database_mysql", self.mysql), ("database_mongo", self.mongo),
                             ("cache", self.redis), ("queue_rabbitmq", self.rabbitmq)):
            if selfrun(self.m, key):
                name, text = builder()
                self.files.add("%s/data-%s.yaml" % (base, name), text)
                resources.append("data-%s.yaml" % name)
        self.files.add("%s/networkpolicy.yaml" % base, self.networkpolicy())
        resources.append("networkpolicy.yaml")
        if self.public:
            self.files.add("%s/ingress.yaml" % base, self.ingress())
            resources.append("ingress.yaml")
        self.files.add("%s/kustomization.yaml" % base, self.kustomization(resources))
        self.files.add("%s/README.md" % base, self.readme())
        return self.files

    def configmap(self):
        m = self.m
        data = {"APP_ENV": "production"}
        host = lambda key, name, what: name if selfrun(m, key) else "REPLACE-with-the-%s" % what
        if m.db_key:
            name = "postgres" if m.db_key == "database" else "mysql"
            data.update({"DB_HOST": host(m.db_key, name, "database-host"), "DB_PORT": "5432" if m.db_key == "database" else "3306",
                         "DB_NAME": "app", "DB_USER": "app"})
        if m.has("database_mongo"):
            data["MONGODB_HOST"] = host("database_mongo", "mongo", "mongodb-host")
        if m.has("cache"):
            data.update({"REDIS_HOST": host("cache", "redis", "cache-host"), "REDIS_PORT": "6379"})
        if m.has("queue_rabbitmq"):
            data["AMQP_ENDPOINT"] = host("queue_rabbitmq", "rabbitmq", "broker-endpoint")
        if m.has("queue_kafka"):
            data["KAFKA_BOOTSTRAP_SERVERS"] = "REPLACE-with-the-broker-list"
        if m.has("storage"):
            data.update({"S3_ENDPOINT": "REPLACE-with-the-object-storage-endpoint", "S3_BUCKET": "REPLACE-with-the-bucket"})
        for item in m.settings:  # every setting the code reads; secrets go in the Secret, not here
            if item["category"] == "config" and item["name"] not in data:
                data[item["name"]] = "CHANGE_ME"
        return dump({"apiVersion": "v1", "kind": "ConfigMap", "metadata": self.meta(self.ns + "-config", labels=False), "data": data})

    def secret_example(self):
        m = self.m
        keys = ["APP_SECRETS_JSON"]
        if m.db_key:
            keys.insert(0, "DB_PASSWORD")
        if m.has("cache"):
            keys.append("REDIS_PASSWORD")
        if m.has("database_mongo"):
            keys.append("MONGO_PASSWORD")
        if m.has("queue_rabbitmq"):
            keys.append("AMQP_PASSWORD")
        if m.has("storage"):
            keys += ["S3_ACCESS_KEY", "S3_SECRET_KEY"]
        keys += [i["name"] for i in m.settings if i["category"] != "config" and i["name"] not in keys]
        head = ("# This file shows the secret keys the workloads read. Do not put real values here or commit them.\n"
                "# Create the real secret with: kubectl -n %s create secret generic %s-secrets --from-literal=KEY=value ...\n"
                "# or with an external secrets operator that reads your provider's secret store.\n") % (self.ns, self.ns)
        return head + dump({"apiVersion": "v1", "kind": "Secret", "metadata": self.meta(self.ns + "-secrets", labels=False),
                            "type": "Opaque", "stringData": {k: "CHANGE_ME" for k in keys}})

    def deployment(self, unit):
        n, port = unit["name"], unit["port"] or 8080
        health = unit["health_path"] or "/"
        worker = unit["kind"] == "worker"
        container = {"name": n, "image": "%s/%s:%s" % (self.registry, n, IMAGE_TAG), "imagePullPolicy": "Always"}
        if not worker:
            container["ports"] = [{"name": "http", "containerPort": port}]
            probe = {"httpGet": {"path": health, "port": "http"}}
            container["startupProbe"] = dict(probe, failureThreshold=30, periodSeconds=5)
            container["readinessProbe"] = dict(probe, periodSeconds=10)
            container["livenessProbe"] = dict(probe, periodSeconds=20)
        container["env"] = [{"name": "PORT", "value": str(port)}]
        container["envFrom"] = [{"configMapRef": {"name": self.ns + "-config"}},
                                {"secretRef": {"name": self.ns + "-secrets", "optional": True}}]
        container["resources"] = {"requests": {"cpu": "%dm" % int(self.cpu * 250), "memory": "%dMi" % int(self.mem * 512)},
                                  "limits": {"cpu": "%dm" % int(self.cpu * 1000), "memory": "%dMi" % int(self.mem * 1024)}}
        container["securityContext"] = {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                        "capabilities": {"drop": ["ALL"]}}
        container["volumeMounts"] = [{"name": "tmp", "mountPath": "/tmp"}]
        selector = {"app.kubernetes.io/name": n}
        return dump({
            "apiVersion": "apps/v1", "kind": "Deployment", "metadata": self.meta(n, annotations={
                "checkov.io/skip1": "CKV_K8S_43=A digest is known only after the image is built; the pipeline pins the version tag",
                "checkov.io/skip2": "CKV_K8S_35=Settings arrive as environment variables because that is how the application reads them"}),
            "spec": {"replicas": 1 if worker else self.reps, "revisionHistoryLimit": 5,
                     "strategy": {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 0, "maxSurge": 1}},
                     "selector": {"matchLabels": selector},
                     "template": {"metadata": {"labels": dict(self.meta(n)["labels"])},
                                  "spec": {"serviceAccountName": self.ns + "-app", "automountServiceAccountToken": False,
                                           "securityContext": {"runAsNonRoot": True, "runAsUser": 10001, "runAsGroup": 10001,
                                                               "fsGroup": 10001, "seccompProfile": {"type": "RuntimeDefault"}},
                                           "topologySpreadConstraints": [{"maxSkew": 1, "topologyKey": "topology.kubernetes.io/zone",
                                                                          "whenUnsatisfiable": "ScheduleAnyway",
                                                                          "labelSelector": {"matchLabels": selector}}],
                                           "containers": [container],
                                           "volumes": [{"name": "tmp", "emptyDir": {"sizeLimit": "128Mi"}}]}}}})

    def stateful(self, name, image, port, env=None, args=None, mount="/data", size="20Gi", writable=("/tmp",)):
        """A single-replica StatefulSet with a volume, and the headless service in front of it."""
        container = {"name": name, "image": image, "imagePullPolicy": "Always", "ports": [{"name": name, "containerPort": port}]}
        probe = {"tcpSocket": {"port": name}, "periodSeconds": 10, "failureThreshold": 6}
        container["readinessProbe"] = dict(probe, initialDelaySeconds=5)
        container["livenessProbe"] = dict(probe, initialDelaySeconds=30)
        if args:
            container["args"] = args
        container["env"] = env or []
        container["resources"] = {"requests": {"cpu": "250m", "memory": "512Mi"}, "limits": {"cpu": "1", "memory": "2Gi"}}
        container["securityContext"] = {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                        "capabilities": {"drop": ["ALL"]}}
        container["volumeMounts"] = [{"name": "data", "mountPath": mount}] + [
            {"name": "scratch-%d" % i, "mountPath": p} for i, p in enumerate(writable)]
        selector = {"app.kubernetes.io/name": name}
        sts = {"apiVersion": "apps/v1", "kind": "StatefulSet", "metadata": self.meta(name, annotations={
                   "checkov.io/skip1": "CKV_K8S_43=A digest is known only after the image is pulled; the version tag is pinned",
                   "checkov.io/skip2": "CKV_K8S_35=The database image reads its password from an environment variable",
                   "checkov.io/skip3": "CKV_K8S_40=Database images run as the non-root user their image defines"}),
               "spec": {"serviceName": name, "replicas": 1, "selector": {"matchLabels": selector},
                        "template": {"metadata": {"labels": dict(self.meta(name)["labels"])},
                                     "spec": {"automountServiceAccountToken": False,
                                              "securityContext": {"runAsNonRoot": True, "runAsUser": 999, "runAsGroup": 999, "fsGroup": 999,
                                                                  "seccompProfile": {"type": "RuntimeDefault"}},
                                              "containers": [container],
                                              "volumes": [{"name": "scratch-%d" % i, "emptyDir": {}} for i, _ in enumerate(writable)]}},
                        "volumeClaimTemplates": [{"metadata": {"name": "data"},
                                                  "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": size}}}}]}}
        svc = {"apiVersion": "v1", "kind": "Service", "metadata": self.meta(name),
               "spec": {"clusterIP": "None", "selector": selector, "ports": [{"name": name, "port": port, "targetPort": name}]}}
        return name, dump(sts) + "---" + chr(10) + dump(svc)

    @staticmethod
    def secret_env(var, key):
        return {"name": var, "valueFrom": {"secretKeyRef": {"name": "SECRET", "key": key}}}

    def postgres(self):
        image = "pgvector/pgvector:pg16" if self.m.has("vector") else "postgres:16"
        env = [{"name": "POSTGRES_USER", "value": "app"}, {"name": "POSTGRES_DB", "value": "app"},
               {"name": "PGDATA", "value": "/var/lib/postgresql/data/pgdata"}, self.secret_env("POSTGRES_PASSWORD", "DB_PASSWORD")]
        return self.fix(self.stateful("postgres", image, 5432, env, mount="/var/lib/postgresql/data", writable=("/tmp", "/var/run/postgresql")))

    def mysql(self):
        env = [{"name": "MYSQL_DATABASE", "value": "app"}, {"name": "MYSQL_USER", "value": "app"},
               self.secret_env("MYSQL_PASSWORD", "DB_PASSWORD"), self.secret_env("MYSQL_ROOT_PASSWORD", "DB_PASSWORD")]
        return self.fix(self.stateful("mysql", "mysql:8.4", 3306, env, mount="/var/lib/mysql", writable=("/tmp", "/var/run/mysqld")))

    def mongo(self):
        env = [{"name": "MONGO_INITDB_ROOT_USERNAME", "value": "app"}, self.secret_env("MONGO_INITDB_ROOT_PASSWORD", "MONGO_PASSWORD")]
        return self.fix(self.stateful("mongo", "mongo:7", 27017, env, mount="/data/db"))

    def redis(self):
        env = [self.secret_env("REDIS_PASSWORD", "REDIS_PASSWORD")]
        return self.fix(self.stateful("redis", "redis:7", 6379, env, ["redis-server", "--requirepass", "$(REDIS_PASSWORD)", "--appendonly", "yes"], size="5Gi"))

    def rabbitmq(self):
        env = [{"name": "RABBITMQ_DEFAULT_USER", "value": "app"}, self.secret_env("RABBITMQ_DEFAULT_PASS", "AMQP_PASSWORD")]
        return self.fix(self.stateful("rabbitmq", "rabbitmq:3", 5672, env, mount="/var/lib/rabbitmq", size="10Gi", writable=("/tmp", "/var/log/rabbitmq")))

    def fix(self, pair):
        name, text = pair
        return name, text.replace("SECRET", self.ns + "-secrets")

    def service(self, unit):
        n = unit["name"]
        return dump({"apiVersion": "v1", "kind": "Service", "metadata": self.meta(n),
                     "spec": {"type": "ClusterIP", "selector": {"app.kubernetes.io/name": n},
                              "ports": [{"name": "http", "port": 80, "targetPort": "http"}]}})

    def hpa(self, unit):
        n = unit["name"]
        floor = 1 if unit["kind"] == "worker" else self.reps
        return dump({"apiVersion": "autoscaling/v2", "kind": "HorizontalPodAutoscaler", "metadata": self.meta(n, labels=False),
                     "spec": {"scaleTargetRef": {"apiVersion": "apps/v1", "kind": "Deployment", "name": n},
                              "minReplicas": floor, "maxReplicas": floor * 4,
                              "metrics": [{"type": "Resource", "resource": {"name": "cpu", "target": {"type": "Utilization", "averageUtilization": 60}}}]}})

    def pdb(self, unit):
        n = unit["name"]
        return dump({"apiVersion": "policy/v1", "kind": "PodDisruptionBudget", "metadata": self.meta(n, labels=False),
                     "spec": {"maxUnavailable": 1, "selector": {"matchLabels": {"app.kubernetes.io/name": n}}}})

    def networkpolicy(self):
        def policy(name, spec):
            return dump({"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy", "metadata": self.meta(name, labels=False), "spec": spec})

        def tcp(port):
            return {"protocol": "TCP", "port": port}

        docs = [policy("default-deny", {"podSelector": {}, "policyTypes": ["Ingress", "Egress"]}),
                policy("allow-dns-and-egress", {
                    "podSelector": {}, "policyTypes": ["Egress"],
                    "egress": [{"to": [{"namespaceSelector": {}, "podSelector": {"matchLabels": {"k8s-app": "kube-dns"}}}],
                                "ports": [{"protocol": "UDP", "port": 53}, tcp(53)]},
                               {"ports": [tcp(p) for p in (443, 5432, 3306, 6379, 27017)]}]})]
        if self.public:
            docs.append(policy("allow-from-ingress", {
                "podSelector": {}, "policyTypes": ["Ingress"],
                "ingress": [{"from": [{"namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "ingress-nginx"}}}],
                             "ports": [tcp(u["port"] or 8080) for u in self.public]}]}))
        return "---\n".join(docs)

    def ingress(self):
        annotations = {"nginx.ingress.kubernetes.io/proxy-body-size": "20m", "nginx.ingress.kubernetes.io/ssl-redirect": "true"}
        if self.m.streaming:
            annotations.update({"nginx.ingress.kubernetes.io/proxy-buffering": "off", "nginx.ingress.kubernetes.io/proxy-read-timeout": "300",
                                "nginx.ingress.kubernetes.io/proxy-send-timeout": "300"})
        first = self.public[0]
        ordered = [u for u in self.public if u is not first] + [first]
        paths = [{"path": self.path_for(u), "pathType": "Prefix",
                  "backend": {"service": {"name": u["name"], "port": {"name": "http"}}}} for u in ordered]
        return ("# Add a tls section with your certificate secret (or cert-manager) before sending real traffic.\n" + dump({
            "apiVersion": "networking.k8s.io/v1", "kind": "Ingress",
            "metadata": self.meta(self.ns, labels=False, annotations=annotations),
            "spec": {"ingressClassName": self.ingress_class, "rules": [{"http": {"paths": paths}}]}}))

    def kustomization(self, resources):
        return dump({"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization", "namespace": self.ns,
                     "resources": resources,
                     "images": [{"name": "%s/%s" % (self.registry, u["name"]), "newTag": IMAGE_TAG} for u in self.units]})

    def readme(self):
        return T("""
            # Kubernetes deployment for @@project@@

            Generated from plan @@fp@@. The manifests are plain YAML with a Kustomize base. A Helm chart with the same defaults is in `../helm/@@project@@`.

            ## Steps

            1. Replace `@@registry@@` with your container registry in `kustomization.yaml` (the `images` section) and push an image per unit.
            2. Fill in `configmap.yaml` with the data-service addresses from your infrastructure code outputs.
            3. Create the secret named `@@project@@-secrets` (see `secret.example.yaml`; never commit real values).
            4. `kubectl apply -k .`

            ## What is configured for you

            - A restricted namespace, one service account with no token mounted, and a non-root, read-only container.
            - Resource requests and limits, probes on the path the code serves, a pod disruption budget and zone spread.
            - A default-deny network policy with named exceptions for DNS, outbound HTTPS, data stores and the ingress controller.
            - An ingress with proxy settings that keep streaming responses alive when the code streams.
        """, project=self.m.project, fp=self.m.fingerprint, registry=self.registry)


def helm_chart(model, registry=None):
    """A Helm chart that renders the same workloads from values.yaml. Returns a Files set under helm/<project>/."""
    files = Files(model.fingerprint)
    k = K8s(model, registry)
    root = "helm/%s" % model.project
    files.add(root + "/Chart.yaml", "apiVersion: v2\nname: %s\ndescription: Generated by SolsArch for %s\ntype: application\nversion: 0.1.0\nappVersion: \"1.0.0\"\n" % (model.project, model.project))
    units = []
    for unit in k.units:
        units.append("  - name: %s\n    kind: %s\n    port: %d\n    healthPath: %s\n    replicas: %d\n    public: %s\n    path: %s\n    image:\n      repository: %s/%s\n      tag: %s" % (
            unit["name"], unit["kind"], unit["port"] or 8080, yq(unit["health_path"] or "/"), 1 if unit["kind"] == "worker" else k.reps,
            "true" if unit in k.public else "false", yq(k.path_for(unit)) if unit in k.public else '""', k.registry, unit["name"], IMAGE_TAG))
    files.add(root + "/values.yaml", ("# Values for %s. Replace the registry and tags, then helm install.\nnamespace: %s\nresources:\n  requests:\n    cpu: %s\n    memory: %s\n"
                                      "  limits:\n    cpu: %s\n    memory: %s\ningress:\n  className: %s\n  streaming: %s\nunits:\n%s\n") % (
        model.project, k.ns, yq("%dm" % int(k.cpu * 250)), yq("%dMi" % int(k.mem * 512)), yq("%dm" % int(k.cpu * 1000)), yq("%dMi" % int(k.mem * 1024)),
        k.ingress_class, "true" if model.streaming else "false", "\n".join(units)))
    files.add(root + "/templates/_helpers.tpl", T("""
        {{- define "chart.labels" -}}
        app.kubernetes.io/part-of: {{ .Chart.Name }}
        app.kubernetes.io/managed-by: solsarch
        {{- end -}}
    """), header=False)
    files.add(root + "/templates/workloads.yaml", T("""
        {{- range .Values.units }}
        ---
        apiVersion: apps/v1
        kind: Deployment
        metadata:
          name: {{ .name }}
          namespace: {{ $.Values.namespace }}
          labels:
            app.kubernetes.io/name: {{ .name }}
            {{- include "chart.labels" $ | nindent 4 }}
        spec:
          replicas: {{ .replicas }}
          strategy:
            type: RollingUpdate
            rollingUpdate:
              maxUnavailable: 0
              maxSurge: 1
          selector:
            matchLabels:
              app.kubernetes.io/name: {{ .name }}
          template:
            metadata:
              labels:
                app.kubernetes.io/name: {{ .name }}
            spec:
              automountServiceAccountToken: false
              securityContext:
                runAsNonRoot: true
                runAsUser: 10001
                seccompProfile:
                  type: RuntimeDefault
              topologySpreadConstraints:
                - maxSkew: 1
                  topologyKey: topology.kubernetes.io/zone
                  whenUnsatisfiable: ScheduleAnyway
                  labelSelector:
                    matchLabels:
                      app.kubernetes.io/name: {{ .name }}
              containers:
                - name: {{ .name }}
                  image: "{{ .image.repository }}:{{ .image.tag }}"
                  {{- if ne .kind "worker" }}
                  ports:
                    - name: http
                      containerPort: {{ .port }}
                  startupProbe:
                    httpGet:
                      path: {{ .healthPath }}
                      port: http
                    failureThreshold: 30
                    periodSeconds: 5
                  readinessProbe:
                    httpGet:
                      path: {{ .healthPath }}
                      port: http
                  livenessProbe:
                    httpGet:
                      path: {{ .healthPath }}
                      port: http
                  {{- end }}
                  env:
                    - name: PORT
                      value: {{ .port | quote }}
                  envFrom:
                    - configMapRef:
                        name: {{ $.Chart.Name }}-config
                    - secretRef:
                        name: {{ $.Chart.Name }}-secrets
                        optional: true
                  resources:
                    {{- toYaml $.Values.resources | nindent 12 }}
                  securityContext:
                    allowPrivilegeEscalation: false
                    readOnlyRootFilesystem: true
                    capabilities:
                      drop: ["ALL"]
                  volumeMounts:
                    - name: tmp
                      mountPath: /tmp
              volumes:
                - name: tmp
                  emptyDir: {}
        {{- if ne .kind "worker" }}
        ---
        apiVersion: v1
        kind: Service
        metadata:
          name: {{ .name }}
          namespace: {{ $.Values.namespace }}
        spec:
          selector:
            app.kubernetes.io/name: {{ .name }}
          ports:
            - name: http
              port: 80
              targetPort: http
        {{- end }}
        ---
        apiVersion: policy/v1
        kind: PodDisruptionBudget
        metadata:
          name: {{ .name }}
          namespace: {{ $.Values.namespace }}
        spec:
          maxUnavailable: 1
          selector:
            matchLabels:
              app.kubernetes.io/name: {{ .name }}
        ---
        apiVersion: autoscaling/v2
        kind: HorizontalPodAutoscaler
        metadata:
          name: {{ .name }}
          namespace: {{ $.Values.namespace }}
        spec:
          scaleTargetRef:
            apiVersion: apps/v1
            kind: Deployment
            name: {{ .name }}
          minReplicas: {{ .replicas }}
          maxReplicas: {{ mul .replicas 4 }}
          metrics:
            - type: Resource
              resource:
                name: cpu
                target:
                  type: Utilization
                  averageUtilization: 60
        {{- end }}
    """), header=False)
    files.add(root + "/templates/ingress.yaml", T("""
        {{- $public := list }}
        {{- range .Values.units }}{{- if .public }}{{- $public = append $public . }}{{- end }}{{- end }}
        {{- if $public }}
        apiVersion: networking.k8s.io/v1
        kind: Ingress
        metadata:
          name: {{ .Chart.Name }}
          namespace: {{ .Values.namespace }}
          annotations:
            nginx.ingress.kubernetes.io/ssl-redirect: "true"
            {{- if .Values.ingress.streaming }}
            nginx.ingress.kubernetes.io/proxy-buffering: "off"
            nginx.ingress.kubernetes.io/proxy-read-timeout: "300"
            {{- end }}
        spec:
          ingressClassName: {{ .Values.ingress.className }}
          rules:
            - http:
                paths:
                  {{- range $public }}
                  - path: {{ .path }}
                    pathType: Prefix
                    backend:
                      service:
                        name: {{ .name }}
                        port:
                          name: http
                  {{- end }}
        {{- end }}
    """), header=False)
    files.add(root + "/templates/config.yaml", T("""
        apiVersion: v1
        kind: ConfigMap
        metadata:
          name: {{ .Chart.Name }}-config
          namespace: {{ .Values.namespace }}
        data:
          APP_ENV: production
    """), header=False)
    return files


def generate(model, registry=None):
    out = K8s(model, registry).generate()
    out.update(helm_chart(model, registry))
    return out
