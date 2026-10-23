#!/usr/bin/env python3
"""Deployment configuration for application platforms, edge platforms, GPU clouds and data platforms.

These providers are deployed with their own configuration file or command line, not with a general infrastructure tool, so
this writes the real file each platform reads (render.yaml, fly.toml, app.json, vercel.json, netlify.toml, wrangler.toml,
firebase.json, config.toml, ...) plus a short README and, where the platform has no declarative file for a piece, a script
that runs the vendor's own CLI. Values come from the plan; where the repository does not say (a build command, a domain),
the file carries a REPLACE marker instead of a guess.
"""
import json

from iac_common import IMAGE_TAG, Files, T, selfrun, shq
from iac_k8s import dump

NL = chr(10)


def toml_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, list):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    return json.dumps(str(v))


def toml(sections):
    """sections: list of (header or None, dict). A header like '[[a.b]]' makes an array table."""
    out = []
    for header, body in sections:
        if header:
            out.append(header)
        for k, v in body.items():
            out.append("%s = %s" % (k if k.replace("_", "").replace("-", "").isalnum() else json.dumps(k), toml_value(v)))
        out.append("")
    return "\n".join(out)


class Paas:
    def __init__(self, model, provider):
        self.m = model
        self.p = provider
        self.files = Files(model.fingerprint)
        self.units = [u for u in model.units if u["kind"] != "static"]
        self.public = [u for u in model.public_units() if u["kind"] != "static"]
        self.statics = [u for u in model.units if u["kind"] == "static"]
        self.serverless = model.mode == "serverless"
        self.ha = not model.single_az
        self.pg = model.db_key == "database"
        self.mysql = model.db_key == "database_mysql"
        self.mongo = model.has("database_mongo")
        self.redis = model.has("cache")
        self.storage = model.has("storage")
        self.kafka = model.has("queue_kafka")
        self.name = model.project

    # ------------------------------------------------------------ helpers
    def port(self, u):
        return u["port"] or 8080

    def docker(self, u):
        """The Dockerfile path from the repository root. A unit with none gets the path where iac_docker writes one."""
        if u.get("dockerfile"):
            return u["dockerfile"]
        folder = u["folder"].strip("./")
        return (folder + "/Dockerfile") if folder else "Dockerfile"

    def context(self, u):
        folder = u["folder"].strip("./")
        return folder or "."

    def readme(self, path, title, steps, notes=()):
        self.files.add(path, T("""
            # @@title@@

            Generated from plan @@fp@@.

            ## Steps

            @@steps@@

            @@notes@@
        """, title=title, fp=self.m.fingerprint, steps="\n".join("%d. %s" % (i, s) for i, s in enumerate(steps, 1)),
                              notes=("## Notes\n\n" + "\n".join("- " + n for n in notes)) if notes else ""))

    # ------------------------------------------------------------ Render
    def render(self):
        services, env_for_db = [], []
        db_env = []
        if self.pg:
            db_env.append({"key": "DATABASE_URL", "fromDatabase": {"name": "%s-db" % self.name, "property": "connectionString"}})
        if self.redis:
            db_env.append({"key": "REDIS_URL", "fromService": {"type": "keyvalue", "name": "%s-cache" % self.name, "property": "connectionString"}})
        for u in self.units:
            worker = u["kind"] == "worker"
            kind = "worker" if worker else ("web" if u in self.public else "pserv")
            svc = {"type": kind, "name": u["name"], "runtime": "docker", "dockerfilePath": "./" + self.docker(u), "dockerContext": "./" + self.context(u) if self.context(u) != "." else ".",
                   "plan": "standard" if self.ha else "starter", "region": "oregon", "autoDeploy": False}
            if kind != "worker":
                svc["healthCheckPath"] = u["health_path"] or "/"
                svc["numInstances"] = self.m.replicas if self.ha else 1
            svc["envVars"] = [{"key": "PORT", "value": str(self.port(u))}, {"key": "APP_ENV", "value": "production"}] + db_env + [
                {"key": "APP_SECRETS_JSON", "sync": False}] + [
                {"key": i["name"], "sync": False} for i in self.m.settings]  # sync: false = you type the value once in the dashboard
            services.append(svc)
        for u in self.statics:
            services.append({"type": "web", "name": u["name"], "runtime": "static", "buildCommand": "REPLACE-with-your-build-command",
                             "staticPublishPath": "REPLACE-with-your-output-folder", "routes": [{"type": "rewrite", "source": "/*", "destination": "/index.html"}]})
        spec = {"services": services}
        if self.redis:
            spec["services"].append({"type": "keyvalue", "name": "%s-cache" % self.name, "plan": "starter", "region": "oregon", "maxmemoryPolicy": "allkeys-lru", "ipAllowList": []})
        if self.pg:
            spec["databases"] = [{"name": "%s-db" % self.name, "plan": "basic-1gb" if self.ha else "basic-256mb", "postgresMajorVersion": "16", "region": "oregon", "ipAllowList": []}]
        self.files.add("render/render.yaml", "# yaml-language-server: $schema=https://render.com/schema/render.yaml.json\n" + dump(spec))
        self.readme("render/README.md", "Render deployment for %s" % self.name, [
            "Copy `render.yaml` to the root of your repository and push it.",
            "In the Render dashboard choose New, Blueprint, and pick the repository.",
            "Fill in the secret values marked `sync: false` (for example `APP_SECRETS_JSON`).",
            "Check each plan and region in the file against the Render pricing page before you apply."],
            ["Render has no object storage, queue, or CDN rules in this catalogue; the plan lists those as gaps.",
             "Private services (`pserv`) are reachable only inside the Render private network."])

    # ------------------------------------------------------------ Fly.io
    def flyio(self):
        script = ["#!/usr/bin/env bash", "set -euo pipefail", "# Run once per environment. Requires flyctl and `fly auth login`.", ""]
        for u in self.units:
            n, worker = u["name"], u["kind"] == "worker"
            body = [(None, {"app": "%s-%s" % (self.name, n), "primary_region": "iad"}),
                    ("[build]", {"dockerfile": "../" + self.docker(u)}),
                    ("[env]", {"PORT": str(self.port(u)), "APP_ENV": "production"})]
            if worker:
                body.append(("[processes]", {"worker": "REPLACE-with-the-worker-start-command"}))
            else:
                min_run = 0 if self.serverless else (2 if self.ha else 1)
                body.append(("[http_service]", {"internal_port": self.port(u), "force_https": True,
                                                "auto_stop_machines": "stop" if self.serverless else "off", "auto_start_machines": True,
                                                "min_machines_running": min_run}))
                body.append(("[[http_service.checks]]", {"grace_period": "15s", "interval": "15s", "method": "GET", "path": u["health_path"] or "/", "timeout": "5s"}))
            body.append(("[[vm]]", {"size": "shared-cpu-1x" if float(self.m.sizing["unit_vcpu"]) <= 1 else "shared-cpu-2x",
                                   "memory": "%dmb" % max(256, int(float(self.m.sizing["unit_memory_gb"]) * 1024))}))
            self.files.add("fly/%s.fly.toml" % n, toml(body))
            script.append("fly apps create %s-%s || true" % (self.name, n))
        if self.pg:
            script.append("fly mpg create --name %s-db --region iad   # Fly Managed Postgres" % self.name)
        if self.redis:
            script.append("fly redis create --name %s-cache --region iad   # Upstash Redis" % self.name)
        if self.storage:
            script.append("fly storage create --name %s-assets   # Tigris object storage" % self.name)
        script.append("")
        script.append("# Secrets are set per app, never in the file:")
        for u in self.units:
            script.append("# fly secrets set -a %s-%s APP_SECRETS_JSON=... DATABASE_URL=..." % (self.name, u["name"]))
        script.append("")
        for u in self.units:
            script.append("fly deploy -c fly/%s.fly.toml --remote-only" % u["name"])
        self.files.add("fly/deploy.sh", "\n".join(script))
        self.readme("fly/README.md", "Fly.io deployment for %s" % self.name, [
            "Install flyctl and run `fly auth login`.",
            "Review `deploy.sh`, then run it from the repository root. Each unit becomes its own Fly app with its own `fly.toml`.",
            "Set the secrets with `fly secrets set` as shown in the script's comments.",
            "Attach the database: `fly mpg attach` (Managed Postgres) so `DATABASE_URL` is set for each app."],
            ["Apps reach each other over the private network at `<app>.internal`.", "The worker start command is a REPLACE marker because the repository does not name it."])

    # ------------------------------------------------------------ Railway
    def railway(self):
        for u in self.units:
            cfg = {"$schema": "https://railway.com/railway.schema.json",
                   "build": {"builder": "DOCKERFILE", "dockerfilePath": self.docker(u)},
                   "deploy": {"numReplicas": self.m.replicas if (self.ha and u["kind"] != "worker") else 1, "restartPolicyType": "ON_FAILURE", "restartPolicyMaxRetries": 5}}
            if u["kind"] != "worker":
                cfg["deploy"]["healthcheckPath"] = u["health_path"] or "/"
                cfg["deploy"]["healthcheckTimeout"] = 60
            if self.serverless:
                cfg["deploy"]["sleepApplication"] = True
            self.files.add("railway/%s.railway.json" % u["name"], json.dumps(cfg, indent=2))
        refs = []
        if self.pg:
            refs.append("DATABASE_URL=${{Postgres.DATABASE_URL}}")
        if self.mongo:
            refs.append("MONGO_URL=${{MongoDB.MONGO_URL}}")
        if self.redis:
            refs.append("REDIS_URL=${{Redis.REDIS_URL}}")
        self.files.add("railway/variables.example.env", "# Reference variables: set these in each service so it reads the database, not a copy of its address.\n" + "\n".join(refs + ["APP_ENV=production", "# APP_SECRETS_JSON=..."]))
        self.readme("railway/README.md", "Railway deployment for %s" % self.name, [
            "Create a project in Railway and add one service per unit from your repository.",
            "In each service set the config file path to `railway/<unit>.railway.json` (Settings, Config as code).",
            "Add the database plugins the plan uses (%s)." % (", ".join(x for x, f in (("PostgreSQL", self.pg), ("MongoDB", self.mongo), ("Redis", self.redis), ("Buckets", self.storage)) if f) or "none"),
            "Copy the reference variables from `variables.example.env` into each service."],
            ["Railway provisions databases and buckets from the dashboard or its CLI, so they have no config file here."])

    # ------------------------------------------------------------ Heroku
    def heroku(self):
        script = ["#!/usr/bin/env bash", "set -euo pipefail", "# Requires the Heroku CLI and `heroku login`. One Heroku app per unit.", ""]
        for u in self.units:
            n, worker = u["name"], u["kind"] == "worker"
            app = "%s-%s" % (self.name, n)
            addons = []
            if self.pg and u is self.units[0]:
                addons.append({"plan": "heroku-postgresql:essential-0" if not self.ha else "heroku-postgresql:standard-0"})
            if self.redis and u is self.units[0]:
                addons.append({"plan": "heroku-redis:mini"})
            if self.kafka and u is self.units[0]:
                addons.append({"plan": "heroku-kafka:basic-0"})
            formation = {("worker" if worker else "web"): {"quantity": 1 if worker else (self.m.replicas if self.ha else 1), "size": "standard-1x" if self.ha else "basic"}}
            app_json = {"name": app, "stack": "container", "formation": formation, "addons": addons,
                        "env": dict({"APP_ENV": {"value": "production"}, "PORT": {"value": str(self.port(u))}, "APP_SECRETS_JSON": {"required": False}},
                                    **{i["name"]: {"required": i["required"]} for i in self.m.settings})}
            self.files.add("heroku/%s/app.json" % n, json.dumps(app_json, indent=2))
            self.files.add("heroku/%s/heroku.yml" % n, dump({"build": {"docker": {("worker" if worker else "web"): self.docker(u)}}}))
            script.append("heroku apps:create %s --stack container || true" % app)
            for a in addons:
                script.append("heroku addons:create %s -a %s" % (a["plan"], app))
            script.append("heroku config:set APP_ENV=production -a %s" % app)
            script.append("git subtree push --prefix %s heroku-%s main 2>/dev/null || echo %s" % (
                shq(self.context(u)), n, shq("Push the unit folder to the Heroku remote for %s" % app)))
        self.files.add("heroku/deploy.sh", "\n".join(script))
        self.readme("heroku/README.md", "Heroku deployment for %s" % self.name, [
            "Install the Heroku CLI and run `heroku login`.",
            "Copy each `heroku/<unit>/heroku.yml` and `app.json` into that unit's folder (or the repository root for a single unit).",
            "Run `deploy.sh`, or create each app and add-on by hand as it shows.",
            "Share one database between apps with `heroku addons:attach`."],
            ["Heroku has no object storage; the plan lists it as a gap.", "Heroku Private Spaces are an Enterprise feature and must be created in the dashboard."])

    # ------------------------------------------------------------ Vercel
    def vercel(self):
        for u in self.units + self.statics:
            cfg = {"$schema": "https://openapi.vercel.sh/vercel.json", "regions": ["iad1"],
                   "headers": [{"source": "/(.*)", "headers": [{"key": "X-Content-Type-Options", "value": "nosniff"},
                                                               {"key": "Referrer-Policy", "value": "strict-origin-when-cross-origin"},
                                                               {"key": "Strict-Transport-Security", "value": "max-age=31536000; includeSubDomains"}]}]}
            if u["kind"] in ("api", "service") and u["kind"] != "static":
                cfg["functions"] = {"api/**/*": {"maxDuration": 300 if self.m.streaming else 60, "memory": max(1024, int(float(self.m.sizing["unit_memory_gb"]) * 1024))}}
            self.files.add("vercel/%s.vercel.json" % u["name"], json.dumps(cfg, indent=2))
        self.readme("vercel/README.md", "Vercel deployment for %s" % self.name, [
            "Import the repository in Vercel, one project per unit, with the unit's folder as the Root Directory.",
            "Copy `vercel/<unit>.vercel.json` to `vercel.json` in that folder.",
            "Add the database, cache and blob store from the Vercel Marketplace (Storage tab); the environment variables are added for you.",
            "Set the remaining secrets in Settings, Environment Variables."],
            ["Functions run for at most the `maxDuration` set in the file; long jobs belong on a queue and a worker elsewhere.",
             "Vercel Functions cannot run a background worker; the plan lists workers as a caveat."])

    # ------------------------------------------------------------ Netlify
    def netlify(self):
        body = [("[build]", {"command": "REPLACE-with-your-build-command", "publish": "REPLACE-with-your-output-folder", "functions": "netlify/functions"}),
                ("[functions]", {"node_bundler": "esbuild"}),
                ("[[headers]]", {"for": "/*"}),
                ("[headers.values]", {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "strict-origin-when-cross-origin",
                                      "Strict-Transport-Security": "max-age=31536000; includeSubDomains"})]
        for u in self.public:
            if u["kind"] in ("api", "service"):
                body += [("[[redirects]]", {"from": "/%s/*" % u["name"], "to": "/.netlify/functions/:splat", "status": 200})]
        self.files.add("netlify/netlify.toml", toml(body))
        self.readme("netlify/README.md", "Netlify deployment for %s" % self.name, [
            "Replace the two REPLACE markers in `netlify.toml` with your build command and output folder, then copy it to the repository root.",
            "Connect the repository in Netlify. Add Netlify DB (Neon) from the Extensions page for the database.",
            "Set the remaining secrets under Site configuration, Environment variables."],
            ["Netlify Functions have a time limit; the plan lists background workers and streaming as caveats."])

    # ------------------------------------------------------------ Cloudflare
    def cloudflare(self):
        for u in self.units:
            n = u["name"]
            sections = [(None, {"name": "%s-%s" % (self.name, n), "main": "src/index.ts", "compatibility_date": "2025-09-01",
                                "compatibility_flags": ["nodejs_compat"]}),
                        ("[observability]", {"enabled": True})]
            if not self.serverless:
                sections += [("[[containers]]", {"class_name": "App", "image": "./" + self.docker(u), "max_instances": 5 if self.ha else 2, "instance_type": "standard-1"}),
                             ("[[durable_objects.bindings]]", {"name": "APP", "class_name": "App"}),
                             ("[[migrations]]", {"tag": "v1", "new_sqlite_classes": ["App"]})]
            if self.pg:
                sections.append(("[[hyperdrive]]", {"binding": "DB", "id": "REPLACE-with-the-hyperdrive-config-id"}))
            if self.redis:
                sections.append(("[[kv_namespaces]]", {"binding": "CACHE", "id": "REPLACE-with-the-kv-namespace-id"}))
            if self.storage:
                sections.append(("[[r2_buckets]]", {"binding": "ASSETS", "bucket_name": "%s-assets" % self.name}))
            self.files.add("cloudflare/%s.wrangler.toml" % n, toml(sections))
        self.readme("cloudflare/README.md", "Cloudflare deployment for %s" % self.name, [
            "Install Wrangler and run `wrangler login`.",
            "Create the resources: `wrangler r2 bucket create %s-assets`, `wrangler kv namespace create CACHE`, `wrangler hyperdrive create db --connection-string=...`." % self.name,
            "Put the returned ids in the REPLACE markers of `<unit>.wrangler.toml`.",
            "Set secrets with `wrangler secret put NAME`, then deploy with `wrangler deploy -c cloudflare/<unit>.wrangler.toml`."],
            ["The Worker entry point `src/index.ts` is yours to write: it routes requests to the container or runs the code directly.",
             "Hyperdrive pools connections to an external PostgreSQL; Cloudflare does not host the database."])

    # ------------------------------------------------------------ Deno Deploy
    def denodeploy(self):
        for u in self.units:
            cfg = {"name": "%s-%s" % (self.name, u["name"]), "deploy": {"entrypoint": "REPLACE-with-main.ts", "exclude": ["node_modules", ".git"]},
                   "tasks": {"start": "deno run -A REPLACE-with-main.ts"}}
            self.files.add("deno/%s.deno.json" % u["name"], json.dumps(cfg, indent=2))
        self.readme("deno/README.md", "Deno Deploy for %s" % self.name, [
            "Create an app per unit in the Deno Deploy dashboard and link the repository.",
            "Copy `deno/<unit>.deno.json` to `deno.json` in the unit's folder and set the entry point.",
            "Add a Deno KV database from the dashboard if the plan uses it, and set secrets as environment variables."])

    # ------------------------------------------------------------ Fastly
    def fastly(self):
        body = [(None, {"manifest_version": 3, "name": self.name, "language": "javascript", "description": "Generated by SolsArch"}),
                ("[scripts]", {"build": "npm run build"})]
        if self.redis:
            body.append(("[setup.kv_stores.cache]", {}))
        self.files.add("fastly/fastly.toml", toml(body))
        self.readme("fastly/README.md", "Fastly Compute for %s" % self.name, [
            "Install the Fastly CLI and run `fastly profile create`.",
            "Copy `fastly.toml` next to your Compute project and adjust the language and build script.",
            "`fastly compute publish` builds and deploys it."])

    # ------------------------------------------------------------ Firebase
    def firebase(self):
        cfg = {"hosting": {"public": "REPLACE-with-your-output-folder", "ignore": ["firebase.json", "**/.*", "**/node_modules/**"],
                           "headers": [{"source": "**", "headers": [{"key": "X-Content-Type-Options", "value": "nosniff"}]}]}}
        if self.serverless:
            cfg["functions"] = [{"source": u["folder"], "codebase": u["name"]} for u in self.units]
            cfg["hosting"]["rewrites"] = [{"source": "/api/**", "function": {"functionId": "api"}}]
        else:
            cfg["apphosting"] = [{"backendId": "%s-%s" % (self.name, u["name"]), "rootDir": u["folder"]} for u in self.public]
        self.files.add("firebase/firebase.json", json.dumps(cfg, indent=2))
        if not self.serverless:
            for u in self.public:
                self.files.add("firebase/%s.apphosting.yaml" % u["name"], dump({
                    "runConfig": {"cpu": 1 if float(self.m.sizing["unit_vcpu"]) <= 1 else 2, "memoryMiB": max(512, int(float(self.m.sizing["unit_memory_gb"]) * 1024)),
                                  "minInstances": 1 if self.ha else 0, "maxInstances": 10, "concurrency": 80},
                    "env": [{"variable": "APP_ENV", "value": "production"}, {"variable": "APP_SECRETS_JSON", "secret": "app-secrets"}]}))
        self.files.add("firebase/.firebaserc", json.dumps({"projects": {"default": "REPLACE-with-your-firebase-project-id"}}, indent=2))
        self.readme("firebase/README.md", "Firebase deployment for %s" % self.name, [
            "Install the Firebase CLI, run `firebase login`, and put your project id in `.firebaserc`.",
            "Copy `firebase.json` (and the `<unit>.apphosting.yaml` files as `apphosting.yaml` in each unit's folder) into the repository.",
            "Create the secret: `firebase apphosting:secrets:set app-secrets`.",
            "Deploy with `firebase deploy`."],
            ["Authentication, Firestore and Storage are enabled in the console; their rules live in `firestore.rules` and `storage.rules`, which you own."])

    # ------------------------------------------------------------ Supabase
    def supabase(self):
        self.files.add("supabase/config.toml", toml([
            (None, {"project_id": self.name}),
            ("[api]", {"enabled": True, "port": 54321, "schemas": ["public", "graphql_public"], "max_rows": 1000}),
            ("[db]", {"port": 54322, "major_version": 17}),
            ("[auth]", {"enabled": True, "site_url": "http://127.0.0.1:3000", "enable_signup": True, "minimum_password_length": 10}),
            ("[storage]", {"enabled": self.storage, "file_size_limit": "50MiB"}),
            ("[edge_runtime]", {"enabled": True, "policy": "per_worker"})]))
        for u in self.units:
            self.files.add("supabase/functions/%s/README.md" % u["name"], "Put the Edge Function for %s in `index.ts` here; deploy with `supabase functions deploy %s`.\n" % (u["name"], u["name"]))
        self.readme("supabase/README.md", "Supabase deployment for %s" % self.name, [
            "Install the Supabase CLI and run `supabase login`.",
            "Copy the `supabase` folder to the repository root and run `supabase link --project-ref REPLACE`.",
            "Put your schema in `supabase/migrations`, then `supabase db push`.",
            "Deploy functions with `supabase functions deploy` and set secrets with `supabase secrets set`."],
            ["Edge Functions run Deno, not containers; a service written for another runtime must be ported or hosted elsewhere."])

    # ------------------------------------------------------------ Databricks / Snowflake
    def databricks(self):
        for u in self.public or self.units:
            self.files.add("databricks/%s.app.yaml" % u["name"], dump({"command": ["REPLACE-with-the-start-command"], "env": [{"name": "APP_ENV", "value": "production"}, {"name": "PORT", "value": str(self.port(u))}]}))
        self.files.add("databricks/databricks.yml", dump({"bundle": {"name": self.name},
                                                       "resources": {"apps": {u["name"]: {"name": "%s-%s" % (self.name, u["name"]), "source_code_path": "./" + self.context(u)} for u in (self.public or self.units)}},
                                                       "targets": {"production": {"mode": "production", "workspace": {"host": "REPLACE-with-your-workspace-url"}}}}))
        self.readme("databricks/README.md", "Databricks deployment for %s" % self.name, [
            "Install the Databricks CLI and run `databricks auth login`.",
            "Copy `databricks.yml` to the repository root and each `<unit>.app.yaml` to that unit's folder as `app.yaml`.",
            "`databricks bundle deploy -t production`."])

    def snowflake(self):
        for u in self.units:
            self.files.add("snowflake/%s.spec.yaml" % u["name"], dump({"spec": {"containers": [{"name": u["name"], "image": "/%s/%s/repo/%s:%s" % (self.name.upper(), "PUBLIC", u["name"], IMAGE_TAG),
                                                                                         "env": {"PORT": str(self.port(u)), "APP_ENV": "production"},
                                                                                         "readinessProbe": {"port": self.port(u), "path": u["health_path"] or "/"}}],
                                                                 "endpoints": [{"name": "app", "port": self.port(u), "public": u in self.public}]}}))
        sql = ["-- Run as a role that can create compute pools and services.", "CREATE COMPUTE POOL IF NOT EXISTS %s_POOL MIN_NODES = 1 MAX_NODES = %d INSTANCE_FAMILY = CPU_X64_S;" % (self.name.upper().replace("-", "_"), 3 if self.ha else 1)]
        for u in self.units:
            sql.append("CREATE SERVICE IF NOT EXISTS %s IN COMPUTE POOL %s_POOL FROM @%s_STAGE SPECIFICATION_FILE = '%s.spec.yaml' MIN_INSTANCES = 1;" % (
                u["name"].upper().replace("-", "_"), self.name.upper().replace("-", "_"), self.name.upper().replace("-", "_"), u["name"]))
        self.files.add("snowflake/setup.sql", "\n".join(sql))
        self.readme("snowflake/README.md", "Snowflake deployment for %s" % self.name, [
            "Create an image repository and a stage, push each image with Docker, and upload the spec files to the stage.",
            "Run `setup.sql` in Snowsight or with SnowSQL."])

    # ------------------------------------------------------------ GPU clouds
    def coreweave(self):
        self.readme("coreweave/README.md", "CoreWeave deployment for %s" % self.name, [
            "Create a CKS cluster in the CoreWeave console and download its kubeconfig.",
            "Apply `../kubernetes` (the same manifests every Kubernetes provider uses).",
            "For GPU workloads add `nodeSelector` / `resources.limits.nvidia.com/gpu` to the deployment, using the GPU class you chose in the console."])

    def lambdalabs(self):
        self.files.add("lambdalabs/launch.sh", "\n".join([
            "#!/usr/bin/env bash", "set -euo pipefail", "# Requires LAMBDA_API_KEY. Lists instance types, then launches one with your SSH key.",
            'curl -fsS -u "$LAMBDA_API_KEY:" https://cloud.lambda.ai/api/v1/instance-types | head -c 2000', "",
            'curl -fsS -u "$LAMBDA_API_KEY:" -X POST https://cloud.lambda.ai/api/v1/instance-operations/launch \\',
            '  -H "Content-Type: application/json" \\',
            '  -d \'{"region_name":"REPLACE-region","instance_type_name":"REPLACE-gpu-type","ssh_key_names":["REPLACE-key"],"name":"%s"}\'' % self.name]))
        self.readme("lambdalabs/README.md", "Lambda Cloud deployment for %s" % self.name, [
            "Create an API key and an SSH key in the Lambda console and export `LAMBDA_API_KEY`.",
            "Run `launch.sh` after filling in the region, GPU type and key name.",
            "Add the instance address to `../ansible/inventory/production.ini` and run the playbook."],
            ["Lambda Cloud is bare GPU servers; the Ansible playbook installs Docker and runs the units."])

    def runpod(self):
        pod = {"name": self.name, "imageName": "REPLACE-with-your-image", "gpuTypeIds": ["REPLACE-gpu-type"], "gpuCount": 1, "containerDiskInGb": 50,
               "volumeInGb": 50, "ports": ["%d/http" % (self.port(self.units[0]) if self.units else 8080)], "env": {"APP_ENV": "production"}}
        self.files.add("runpod/pod.json", json.dumps(pod, indent=2))
        self.files.add("runpod/create.sh", "\n".join([
            "#!/usr/bin/env bash", "set -euo pipefail", "# Requires RUNPOD_API_KEY.",
            'curl -fsS -X POST https://rest.runpod.io/v1/pods -H "Authorization: Bearer $RUNPOD_API_KEY" -H "Content-Type: application/json" -d @runpod/pod.json']))
        self.readme("runpod/README.md", "RunPod deployment for %s" % self.name, [
            "Create an API key and export `RUNPOD_API_KEY`.", "Fill in `pod.json` (image, GPU type) and run `create.sh`."],
            ["For the serverless design create a Serverless endpoint from the console or the endpoints API using the same image."])

    def togetherai(self):
        body = {"model": "REPLACE-with-the-model-id", "hardware": "REPLACE-with-the-hardware-id", "min_replicas": 1, "max_replicas": 2 if self.ha else 1}
        self.files.add("together/endpoint.json", json.dumps(body, indent=2))
        self.readme("together/README.md", "Together AI endpoint for %s" % self.name, [
            "Create an API key and export `TOGETHER_API_KEY`.",
            "Choose a model and hardware (`GET /v1/hardware`), fill in `endpoint.json`.",
            "Create it: `curl -X POST https://api.together.xyz/v1/endpoints -H \"Authorization: Bearer $TOGETHER_API_KEY\" -H \"Content-Type: application/json\" -d @together/endpoint.json`."],
            ["Together AI hosts models, not your containers; your application runs elsewhere and calls the endpoint."])

    # ------------------------------------------------------------ Hugging Face Spaces
    def huggingface(self):
        """One Docker Space per unit. A Space is a git repository whose README.md starts with a YAML block (sdk: docker, app_port)."""
        for u in self.units:
            n = u["name"]
            front = ["---", "title: %s" % n, "sdk: docker", "app_port: %d" % self.port(u), "---", "",
                     "Generated by SolsArch Early Cloud Architect from plan %s." % self.m.fingerprint, ""]
            self.files.add("huggingface/%s/README.md" % n, NL.join(front), header=False)
        names = ", ".join(repr(u["name"]) for u in self.units)
        self.files.add("huggingface/deploy_spaces.py", NL.join([
            "#!/usr/bin/env python3",
            '"""Creates one Docker Space per unit and uploads each unit folder. Needs: pip install huggingface_hub, and HF_TOKEN set in your shell."""',
            "import os, shutil, sys, tempfile",
            "from pathlib import Path",
            "from huggingface_hub import HfApi",
            "",
            "OWNER = sys.argv[1] if len(sys.argv) > 1 else None  # your Hugging Face user or organisation name",
            "UNITS = {%s}" % ", ".join("%s: %s" % (repr(u["name"]), repr(self.context(u))) for u in self.units),
            "IGNORE = shutil.ignore_patterns('.git', 'node_modules', '__pycache__', '.venv', '.solsarch', '.env', '.env.*', 'docs')",
            "if not OWNER:",
            "    sys.exit('usage: python huggingface/deploy_spaces.py OWNER')",
            "api = HfApi(token=os.environ['HF_TOKEN'])",
            "secrets = {}",
            "env_file = Path('huggingface/app-settings.env')",
            "if env_file.exists():",
            "    for line in env_file.read_text().splitlines():",
            "        if line.strip() and not line.startswith('#') and '=' in line:",
            "            key, value = line.split('=', 1)",
            "            secrets[key] = value",
            "for name, context in UNITS.items():",
            "    repo = f'{OWNER}/{name}'",
            "    api.create_repo(repo_id=repo, repo_type='space', space_sdk='docker', exist_ok=True)",
            "    with tempfile.TemporaryDirectory() as tmp:",
            "        stage = Path(tmp) / name",
            "        shutil.copytree(context, stage, ignore=IGNORE)",
            "        front = Path('huggingface') / name / 'README.md'",
            "        existing = (stage / 'README.md').read_text() if (stage / 'README.md').exists() else ''",
            "        (stage / 'README.md').write_text(front.read_text() + chr(10) + existing)",
            "        api.upload_folder(folder_path=str(stage), repo_id=repo, repo_type='space')",
            "    for key, value in secrets.items():",
            "        api.add_space_secret(repo_id=repo, key=key, value=value)  # stored encrypted; never printed",
            "    print('uploaded', repo)",
            ""]), header=False)
        self.readme("huggingface/README.md", "Hugging Face Spaces deployment for %s" % self.name, [
            "Create a Hugging Face account and an access token with write access (Settings, Access Tokens). Keep it in your shell only: `export HF_TOKEN=...`.",
            "Install the client: `pip install huggingface_hub`.",
            "Copy `app-settings.env.example` to `app-settings.env`, fill in the values, and do not commit that copy.",
            "Run `python huggingface/deploy_spaces.py YOUR-USER-OR-ORG`. It creates one Docker Space per unit (%s), uploads the unit's folder with the front matter in `<unit>/README.md`, and stores the settings as Space secrets." % names,
            "Open each Space; the first build starts by itself. Free hardware sleeps when idle and wakes on the next visit."],
            ["Each Docker Space needs a Dockerfile at the root of the folder it uploads. Units that already have one use it; others get the one written to `docker/<unit>/Dockerfile` (copy it to the unit's folder).",
             "A Space listens on the port in `app_port` (this plan uses the port found in your code). Secrets are available to the running container as environment variables.",
             "Spaces has no managed database or cache. Add them with `--also neon` or `--also mongodbatlas`, or point the settings at a hosted service."])

    # ------------------------------------------------------------ Cloud Foundry
    def cloudfoundry(self):
        apps = []
        for u in self.units:
            worker = u["kind"] == "worker"
            app = {"name": u["name"], "memory": "%dM" % int(max(256, float(self.m.sizing["unit_memory_gb"]) * 1024)),
                   "instances": 1 if worker else (self.m.replicas if self.ha else 1),
                   "docker": {"image": "REPLACE-with-your-registry/%s:REPLACE-with-the-tag" % u["name"]},
                   "env": {"APP_ENV": "production"}}
            if worker:
                app["no-route"] = True
                app["health-check-type"] = "process"
            else:
                app["health-check-type"] = "http"
                app["health-check-http-endpoint"] = u["health_path"] or "/"
                if u in self.public:
                    app["routes"] = [{"route": "%s.REPLACE-with-your-apps-domain" % u["name"]}]
            uses = set(u.get("uses") or [])
            bind = lambda key: len(self.units) == 1 or key in uses  # the scan ties each unit to the stores it uses; a lone unit gets them all
            services = [s for s, on in (("%s-db" % self.name, (self.pg and bind("database")) or (self.mysql and bind("database_mysql"))),
                                        ("%s-cache" % self.name, self.redis and bind("cache")), ("%s-mongo" % self.name, self.mongo and bind("database_mongo"))) if on]
            if services:
                app["services"] = services
            apps.append(app)
        self.files.add("cloudfoundry/manifest.yml", "---" + NL + dump({"applications": apps}))
        lines = ["#!/usr/bin/env bash", "set -euo pipefail",
                 "# Requires the cf CLI and `cf login`. Offering and plan names come from YOUR operator: run `cf marketplace` and fill in the REPLACE values.", ""]
        for flag, on, label in ((self.pg, "PostgreSQL", "db"), (self.mysql, "MySQL", "db"), (self.redis, "Redis", "cache"), (self.mongo, "MongoDB", "mongo")):
            if flag:
                lines.append("cf create-service REPLACE-with-the-%s-offering REPLACE-with-the-plan %s-%s" % (on.lower(), self.name, label))
        lines += ["", "# For a private image set CF_DOCKER_PASSWORD in your shell and add --docker-username to the push below.",
                  "cf push -f cloudfoundry/manifest.yml", ""]
        self.files.add("cloudfoundry/deploy.sh", NL.join(lines))
        settings = ["#!/usr/bin/env bash", "set -euo pipefail", "# Stores the values from app-settings.env as environment variables of each app. The file is read here and never printed.",
                    'ENV_FILE="${1:-app-settings.env}"', "for app in %s; do" % " ".join(u["name"] for u in self.units),
                    '  while IFS="=" read -r key value; do', '    case "$key" in ""|"#"*) continue;; esac', '    cf set-env "$app" "$key" "$value" > /dev/null', '  done < "$ENV_FILE"',
                    "done", 'echo "Restage each app so it picks the values up: cf restage APP"', ""]
        self.files.add("cloudfoundry/set-app-settings.sh", NL.join(settings))
        self.readme("cloudfoundry/README.md", "Cloud Foundry deployment for %s" % self.name, [
            "Get an account on a Cloud Foundry platform from your operator (a company platform team, VMware Tanzu, SAP BTP and others) and run `cf login`.",
            "Push your container images to a registry the platform can reach and put the image names in `manifest.yml`.",
            "Run `cf marketplace` to see which services your operator offers, then fill in the REPLACE values in `deploy.sh`.",
            "Run `bash cloudfoundry/deploy.sh`. It creates the services and pushes the apps.",
            "Copy `app-settings.env.example` to `app-settings.env`, fill it in, run `bash cloudfoundry/set-app-settings.sh`, then `cf restage` each app."],
            ["Routes, quotas and plans belong to your operator; the manifest uses the standard attributes only.",
             "Open-source Cloud Foundry keeps apps running, so there is no scale to zero."])

    # ------------------------------------------------------------ Koyeb
    def koyeb(self):
        lines = ["#!/usr/bin/env bash", "set -euo pipefail",
                 "# Requires the Koyeb CLI and `koyeb login`. One app and service per unit, built from your repository's Dockerfile.",
                 '# Pass your repository as the first argument, for example: bash koyeb/deploy.sh github.com/YOU/REPO main', 'REPO="${1:?usage: deploy.sh github.com/OWNER/REPO [branch]}"',
                 'BRANCH="${2:-main}"', ""]
        for u in self.units:
            port = self.port(u)
            worker = u["kind"] == "worker"
            cmd = ['koyeb app init %s' % u["name"], '--git "$REPO" --git-branch "$BRANCH"', '--git-builder docker --git-docker-dockerfile %s' % shq(self.docker(u)),
                   "--env APP_ENV=production", "--min-scale %d" % (0 if self.serverless and not worker else 1), "--max-scale %d" % (self.m.replicas if self.ha else 1)]
            if worker:
                cmd.append("--type worker")
            else:
                cmd.append("--ports %d:http --routes /:%d" % (port, port))
                cmd.append("--checks %d:http:%s" % (port, u["health_path"] or "/"))
            lines.append(" ".join(cmd))
        self.files.add("koyeb/deploy.sh", NL.join(lines) + NL)
        settings = ["#!/usr/bin/env bash", "set -euo pipefail", "# Stores the values from app-settings.env on each service. The file is read here and never printed.",
                    'ENV_FILE="${1:-app-settings.env}"', "for unit in %s; do" % " ".join(u["name"] for u in self.units),
                    '  args=()', '  while IFS="=" read -r key value; do', '    case "$key" in ""|"#"*) continue;; esac', '    args+=(--env "$key=$value")',
                    '  done < "$ENV_FILE"', '  koyeb service update "$unit/$unit" "${args[@]}"', "done", ""]
        self.files.add("koyeb/set-app-settings.sh", NL.join(settings))
        self.readme("koyeb/README.md", "Koyeb deployment for %s" % self.name, [
            "Create a Koyeb account, install the CLI and run `koyeb login` (it opens the browser or asks for an API token you create in the dashboard).",
            "Connect your GitHub account to Koyeb in the dashboard so it can read the repository.",
            "Run `bash koyeb/deploy.sh github.com/YOU/REPO main`. It creates one app and service per unit (%s) and builds each from its Dockerfile." % ", ".join(u["name"] for u in self.units),
            "Copy `app-settings.env.example` to `app-settings.env`, fill it in and run `bash koyeb/set-app-settings.sh`.",
            "Add a database from the Koyeb dashboard if the plan lists one (managed Postgres), or use `--also neon` / `--also mongodbatlas`."],
            ["Flags were checked against the Koyeb CLI reference (`koyeb app init --help` shows the current list).",
             "With `--min-scale 0` a service sleeps when idle and the first request wakes it."])

    # folder each platform's files go in, and the command that stores one setting from a KEY=value file (None = the dashboard only)
    SETTINGS = {
        "huggingface": ("huggingface", None), "cloudfoundry": ("cloudfoundry", None), "koyeb": ("koyeb", None),
        "render": ("render", None), "flyio": ("fly", 'fly secrets import < "$ENV_FILE"'),
        "railway": ("railway", 'railway variables $(sed "s/^/--set /" "$ENV_FILE" | tr "\\n" " ")'),
        "heroku": ("heroku", 'heroku config:set $(grep -v "^#" "$ENV_FILE" | tr "\\n" " ")'),
        "vercel": ("vercel", 'while IFS="=" read -r k v; do printf "%s" "$v" | vercel env add "$k" production; done < "$ENV_FILE"'),
        "netlify": ("netlify", 'netlify env:import "$ENV_FILE"'),
        "cloudflare": ("cloudflare", 'while IFS="=" read -r k v; do printf "%s" "$v" | wrangler secret put "$k"; done < "$ENV_FILE"'),
        "denodeploy": ("deno", None), "fastly": ("fastly", None), "firebase": ("firebase", None), "supabase": ("supabase", 'supabase secrets set --env-file "$ENV_FILE"'),
        "databricks": ("databricks", None), "coreweave": ("coreweave", None), "lambdalabs": ("lambdalabs", None), "runpod": ("runpod", None),
    }

    def app_settings(self):
        """Every setting the code reads: an example file of KEY names, and where this platform stores the values."""
        spec = self.SETTINGS.get(self.p)
        if not spec or not self.m.settings:
            return
        folder, command = spec
        lines = ["# Settings and secrets the application reads. Copy to app-settings.env, fill in the values, never commit that copy."]
        for i in self.m.settings:
            lines.append("# %s%s" % (i["category"], ", required" if i["required"] else ""))
            lines.append("%s=CHANGE_ME" % i["name"])
        self.files.add("%s/app-settings.env.example" % folder, chr(10).join(lines) + chr(10), header=False)
        if command:
            self.files.add("%s/set-app-settings.sh" % folder, chr(10).join([
                "#!/usr/bin/env bash", "set -euo pipefail", "# Stores the values from app-settings.env on the platform. The file is read here and never printed.",
                'ENV_FILE="${1:-app-settings.env}"', command]) + chr(10))

    def generate(self):
        getattr(self, self.p)()
        self.app_settings()
        return self.files


GENERATORS = ("render", "flyio", "railway", "heroku", "vercel", "netlify", "cloudflare", "denodeploy", "fastly", "firebase", "supabase",
              "databricks", "snowflake", "coreweave", "lambdalabs", "runpod", "togetherai", "huggingface", "cloudfoundry", "koyeb")


def generate(model, provider):
    return Paas(model, provider).generate()
