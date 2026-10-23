#!/usr/bin/env python3
"""Ansible for servers you run yourself, generated from the plan.

The same playbook configures a Hetzner, Vultr, OVHcloud, Scaleway or any other Linux server: it hardens the host,
installs Docker, runs each deployable unit as a container behind Caddy (automatic HTTPS), runs the data services the
plan marked as self-run (PostgreSQL, MongoDB, Redis, RabbitMQ) on a data host with backups, and adds monitoring. The
Terraform for each provider only creates the servers, network and firewall and prints an inventory, so the server
configuration is identical on every cloud.
"""
from iac_common import IMAGE_TAG, Files, T, selfrun
from iac_k8s import dump, scalar

COLLECTIONS = ["community.docker", "community.general"]


class Ansible:
    def __init__(self, model, registry=None):
        self.m = model
        self.files = Files(model.fingerprint)
        self.registry = registry or "REGISTRY.example.com/%s" % model.project
        self.units = [u for u in model.units if u["kind"] != "static"]
        self.public = [u for u in model.public_units() if u["kind"] != "static"]
        self.data = self.data_services()
        self.split = bool(self.data)

    def data_services(self):
        m = self.m
        out = []
        if selfrun(m, "database"):
            out.append("postgres")
        if selfrun(m, "database_mysql"):
            out.append("mysql")
        if selfrun(m, "database_mongo"):
            out.append("mongo")
        if selfrun(m, "cache"):
            out.append("redis")
        if selfrun(m, "queue_rabbitmq"):
            out.append("rabbitmq")
        return out

    # ------------------------------------------------------------------ files
    def generate(self):
        r = "ansible"
        self.files.add(r + "/ansible.cfg", T("""
            [defaults]
            inventory = inventory/production.ini
            roles_path = roles
            host_key_checking = True
            retry_files_enabled = False
            interpreter_python = auto_silent
            stdout_callback = default
            result_format = yaml

            [ssh_connection]
            pipelining = True
        """))
        self.files.add(r + "/requirements.yml", dump({"collections": [{"name": c} for c in COLLECTIONS]}))
        self.files.add(r + "/inventory/production.ini.example", T("""
            # Terraform prints the same lines as the output "ansible_inventory". Paste them here, save as production.ini.
            [app]
            app-1 ansible_host=REPLACE-with-the-server-address ansible_user=deploy

            [data]
            data-1 ansible_host=REPLACE-with-the-data-server-private-address ansible_user=deploy
        """))
        self.files.add(r + "/inventory/production.ini", T("""
            [app]

            [data]
        """), header=True)
        self.files.add(r + "/group_vars/all/main.yml", self.group_vars())
        self.files.add(r + "/group_vars/all/vault.example.yml", self.vault_example())
        self.files.add(r + "/site.yml", self.site())
        self.common_role()
        self.app_role()
        if self.data:
            self.data_role()
        self.monitoring_role()
        self.files.add(r + "/README.md", self.readme())
        return self.files

    def group_vars(self):
        m = self.m
        units = []
        for u in self.units:
            item = {"name": u["name"], "kind": u["kind"], "port": u["port"] or 8080, "health_path": u["health_path"] or "/",
                    "image": "%s/%s" % (self.registry, u["name"]), "tag": IMAGE_TAG, "replicas": 1 if u["kind"] == "worker" else m.replicas,
                    "public": u in self.public}
            units.append(item)
        first = self.public[0]["name"] if self.public else ""
        body = dump({"project": m.project, "app_dir": "/opt/%s" % m.project, "public_domain": "",
                     "tls_mode": "caddy", "default_unit": first, "registry_user": "", "units": units,
                     "unit_cpu_limit": str(m.sizing["unit_vcpu"]), "unit_memory_limit": "%dM" % int(m.sizing["unit_memory_gb"] * 1024),
                     "data_services": self.data, "data_host_private_address": "", "postgres_version": "16",
                     "backup_bucket": "", "backup_endpoint": "", "backup_retention_days": 14,
                     "ssh_allowed_cidrs": ["0.0.0.0/0"], "monitoring_enabled": True})
        return body

    def vault_example(self):
        m = self.m
        keys = {"vault_registry_password": "CHANGE_ME"}
        if "postgres" in self.data:
            keys["vault_postgres_password"] = "CHANGE_ME"
        if "mysql" in self.data:
            keys["vault_mysql_password"] = "CHANGE_ME"
        if "mongo" in self.data:
            keys["vault_mongo_password"] = "CHANGE_ME"
        if "redis" in self.data:
            keys["vault_redis_password"] = "CHANGE_ME"
        if "rabbitmq" in self.data:
            keys["vault_rabbitmq_password"] = "CHANGE_ME"
        for item in self.m.settings:  # one entry per setting the code reads
            keys["vault_app_%s" % item["name"].lower()] = "CHANGE_ME"
        keys["vault_backup_access_key"] = "CHANGE_ME"
        keys["vault_backup_secret_key"] = "CHANGE_ME"
        keys["vault_grafana_admin_password"] = "CHANGE_ME"
        return ("# Copy to vault.yml, fill it in, then encrypt it: ansible-vault encrypt group_vars/all/vault.yml\n"
                "# Never commit the unencrypted file.\n" + dump(keys))

    def site(self):
        plays = [{"name": "Prepare every server", "hosts": "all", "become": True, "roles": ["common"]}]
        if self.data:
            plays.append({"name": "Run the data services", "hosts": "data", "become": True, "roles": ["data"]})
        plays.append({"name": "Run the application", "hosts": "app", "become": True, "roles": ["app"]})
        plays.append({"name": "Monitor the servers", "hosts": "all", "become": True,
                      "roles": [{"role": "monitoring", "when": "monitoring_enabled | bool"}]})
        return "---\n" + dump(plays)

    # ------------------------------------------------------------------ roles
    def role_file(self, role, kind, name, text, header=True):
        self.files.add("ansible/roles/%s/%s/%s" % (role, kind, name), text, header=header)

    def common_role(self):
        tasks = [
            {"name": "Update the package index", "ansible.builtin.apt": {"update_cache": True, "cache_valid_time": 3600}},
            {"name": "Install base packages", "ansible.builtin.apt": {
                "name": ["ca-certificates", "curl", "gnupg", "ufw", "fail2ban", "unattended-upgrades", "chrony", "jq", "python3-docker"],
                "state": "present"}},
            {"name": "Create the deploy user", "ansible.builtin.user": {"name": "deploy", "shell": "/bin/bash", "groups": "sudo", "append": True}},
            {"name": "Allow the deploy user to use sudo without a password", "ansible.builtin.copy": {
                "dest": "/etc/sudoers.d/90-deploy", "content": "deploy ALL=(ALL) NOPASSWD:ALL\n", "mode": "0440", "validate": "visudo -cf %s"}},
            {"name": "Disable password logins and root login over SSH", "ansible.builtin.copy": {
                "dest": "/etc/ssh/sshd_config.d/10-solsarch.conf",
                "content": "PasswordAuthentication no\nPermitRootLogin no\nKbdInteractiveAuthentication no\nMaxAuthTries 3\n",
                "mode": "0644", "validate": "sshd -t -f %s"},
             "notify": "Restart ssh"},
            {"name": "Allow SSH from the allowed ranges", "community.general.ufw": {
                "rule": "allow", "port": "22", "proto": "tcp", "src": "{{ item }}"}, "loop": "{{ ssh_allowed_cidrs }}"},
            {"name": "Allow web traffic on app servers", "community.general.ufw": {"rule": "allow", "port": "{{ item }}", "proto": "tcp"},
             "loop": ["80", "443"], "when": "'app' in group_names"},
            {"name": "Allow the data ports from the private network only", "community.general.ufw": {
                "rule": "allow", "port": "{{ item }}", "proto": "tcp", "src": "10.0.0.0/8"},
             "loop": ["5432", "3306", "27017", "6379", "5672"], "when": "'data' in group_names"},
            {"name": "Turn the firewall on with deny by default", "community.general.ufw": {"state": "enabled", "policy": "deny", "direction": "incoming"}},
            {"name": "Turn on automatic security updates", "ansible.builtin.copy": {
                "dest": "/etc/apt/apt.conf.d/20auto-upgrades", "content": 'APT::Periodic::Update-Package-Lists "1";\nAPT::Periodic::Unattended-Upgrade "1";\n', "mode": "0644"}},
            {"name": "Make sure fail2ban is running", "ansible.builtin.service": {"name": "fail2ban", "state": "started", "enabled": True}},
            {"name": "Add the Docker repository", "ansible.builtin.deb822_repository": {
                "name": "docker", "types": ["deb"], "uris": "https://download.docker.com/linux/{{ ansible_distribution | lower }}",
                "suites": ["{{ ansible_distribution_release }}"], "components": ["stable"],
                "architectures": ["{{ 'arm64' if ansible_architecture == 'aarch64' else 'amd64' }}"],
                "signed_by": "https://download.docker.com/linux/{{ ansible_distribution | lower }}/gpg", "state": "present"}},
            {"name": "Install Docker", "ansible.builtin.apt": {
                "name": ["docker-ce", "docker-ce-cli", "containerd.io", "docker-compose-plugin"], "state": "present", "update_cache": True}},
            {"name": "Limit container log size", "ansible.builtin.copy": {
                "dest": "/etc/docker/daemon.json",
                "content": '{"log-driver": "json-file", "log-opts": {"max-size": "20m", "max-file": "5"}, "live-restore": true}\n', "mode": "0644"},
             "notify": "Restart docker"},
            {"name": "Make sure Docker is running", "ansible.builtin.service": {"name": "docker", "state": "started", "enabled": True}},
            {"name": "Let the deploy user run Docker", "ansible.builtin.user": {"name": "deploy", "groups": "docker", "append": True}},
        ]
        handlers = [{"name": "Restart ssh", "ansible.builtin.service": {"name": "ssh", "state": "restarted"}},
                    {"name": "Restart docker", "ansible.builtin.service": {"name": "docker", "state": "restarted"}}]
        self.role_file("common", "tasks", "main.yml", "---\n" + dump(tasks))
        self.role_file("common", "handlers", "main.yml", "---\n" + dump(handlers))

    def app_role(self):
        tasks = [
            {"name": "Create the application folder", "ansible.builtin.file": {"path": "{{ app_dir }}", "state": "directory", "owner": "deploy", "mode": "0750"}},
            {"name": "Sign in to the container registry", "community.docker.docker_login": {
                "registry_url": "{{ registry_url | default(omit) }}", "username": "{{ registry_user }}", "password": "{{ vault_registry_password }}"},
             "when": "registry_user | length > 0", "no_log": True},
            {"name": "Write the environment file", "ansible.builtin.template": {
                "src": "app.env.j2", "dest": "{{ app_dir }}/app.env", "owner": "deploy", "mode": "0600"}, "no_log": True},
            {"name": "Write the Caddy configuration", "ansible.builtin.template": {
                "src": "Caddyfile.j2", "dest": "{{ app_dir }}/Caddyfile", "owner": "deploy", "mode": "0644"}},
            {"name": "Write the compose file", "ansible.builtin.template": {
                "src": "compose.yml.j2", "dest": "{{ app_dir }}/compose.yml", "owner": "deploy", "mode": "0644", "validate": "docker compose -f %s config -q"},
             "notify": "Start the application"},
            {"name": "Start the application", "community.docker.docker_compose_v2": {
                "project_src": "{{ app_dir }}", "files": ["compose.yml"], "pull": "always", "state": "present", "remove_orphans": True}},
            {"name": "Wait for each public unit to answer its health check", "ansible.builtin.uri": {
                "url": "http://127.0.0.1:{{ item.port }}{{ item.health_path }}", "status_code": [200, 204, 301, 302]},
             "loop": "{{ units | selectattr('public') | list }}", "register": "app_health", "retries": 20, "delay": 6, "until": "app_health.status in [200, 204, 301, 302]"},
        ]
        handlers = [{"name": "Start the application", "community.docker.docker_compose_v2": {
            "project_src": "{{ app_dir }}", "files": ["compose.yml"], "pull": "always", "state": "present", "remove_orphans": True}}]
        self.role_file("app", "tasks", "main.yml", "---\n" + dump(tasks))
        self.role_file("app", "handlers", "main.yml", "---\n" + dump(handlers))
        self.role_file("app", "templates", "app.env.j2", T("""
            APP_ENV=production
            {% if 'postgres' in data_services %}
            DB_HOST={{ data_host_private_address }}
            DB_PORT=5432
            DB_NAME=app
            DB_USER=app
            DB_PASSWORD={{ vault_postgres_password }}
            {% endif %}
            {% if 'mysql' in data_services %}
            DB_HOST={{ data_host_private_address }}
            DB_PORT=3306
            DB_NAME=app
            DB_USER=app
            DB_PASSWORD={{ vault_mysql_password }}
            {% endif %}
            {% if 'mongo' in data_services %}
            MONGODB_URI=mongodb://app:{{ vault_mongo_password }}@{{ data_host_private_address }}:27017/app?authSource=admin
            {% endif %}
            {% if 'redis' in data_services %}
            REDIS_HOST={{ data_host_private_address }}
            REDIS_PORT=6379
            REDIS_PASSWORD={{ vault_redis_password }}
            {% endif %}
            {% if 'rabbitmq' in data_services %}
            AMQP_URL=amqp://app:{{ vault_rabbitmq_password }}@{{ data_host_private_address }}:5672/
            {% endif %}
        """) + "".join("%s={{ vault_app_%s }}" % (i["name"], i["name"].lower()) + chr(10) for i in self.m.settings))
        caddy = T("""
            {
            {% if tls_mode != 'caddy' or not public_domain %}
            	auto_https off
            {% endif %}
            	servers {
            		timeouts {
            			read_body 30s
            		}
            	}
            }

            {% if public_domain and tls_mode == 'caddy' %}{{ public_domain }}{% else %}:80{% endif %} {
            	encode zstd gzip
            	header {
            		Strict-Transport-Security "max-age=31536000; includeSubDomains"
            		X-Content-Type-Options nosniff
            		Referrer-Policy strict-origin-when-cross-origin
            		-Server
            	}
            {% for unit in units | selectattr('public') | rejectattr('name', 'equalto', default_unit) %}
            	handle_path /{{ unit.name }}/* {
            		reverse_proxy {{ unit.name }}:{{ unit.port }} {
            			health_uri {{ unit.health_path }}
            			flush_interval -1
            		}
            	}
            {% endfor %}
            {% for unit in units | selectattr('name', 'equalto', default_unit) %}
            	handle {
            		reverse_proxy {{ unit.name }}:{{ unit.port }} {
            			health_uri {{ unit.health_path }}
            			flush_interval -1
            		}
            	}
            {% endfor %}
            }
        """)
        self.role_file("app", "templates", "Caddyfile.j2", caddy)
        compose = T("""
            services:
              caddy:
                image: caddy:2
                restart: unless-stopped
                ports:
                  - "80:80"
                  - "443:443"
                volumes:
                  - ./Caddyfile:/etc/caddy/Caddyfile:ro
                  - caddy_data:/data
                  - caddy_config:/config
                networks: [web]
            {% for unit in units %}
              {{ unit.name }}:
                image: "{{ unit.image }}:{{ unit.tag }}"
                restart: unless-stopped
                env_file: [app.env]
                environment:
                  PORT: "{{ unit.port }}"
                read_only: true
                tmpfs: [/tmp]
                user: "10001:10001"
                cap_drop: [ALL]
                security_opt: ["no-new-privileges:true"]
                mem_limit: {{ unit_memory_limit }}
                cpus: {{ unit_cpu_limit }}
            {% if unit.public %}
                ports:
                  - "127.0.0.1:{{ unit.port }}:{{ unit.port }}"
            {% endif %}
            {% if unit.kind != 'worker' %}
                healthcheck:
                  test: ["CMD-SHELL", "wget -qO- http://127.0.0.1:{{ unit.port }}{{ unit.health_path }} >/dev/null 2>&1 || exit 1"]
                  interval: 20s
                  timeout: 5s
                  retries: 5
                  start_period: 30s
            {% endif %}
                networks: [web]
            {% endfor %}

            volumes:
              caddy_data: {}
              caddy_config: {}

            networks:
              web: {}
        """)
        self.role_file("app", "templates", "compose.yml.j2", compose)

    def data_role(self):
        tasks = [
            {"name": "Create the data folders", "ansible.builtin.file": {"path": "/opt/data/{{ item }}", "state": "directory", "mode": "0750"},
             "loop": ["backups", "init"]},
            {"name": "Write the compose file for the data services", "ansible.builtin.template": {
                "src": "data-compose.yml.j2", "dest": "/opt/data/compose.yml", "mode": "0600", "validate": "docker compose -f %s config -q"},
             "no_log": True},
            {"name": "Start the data services", "community.docker.docker_compose_v2": {
                "project_src": "/opt/data", "files": ["compose.yml"], "pull": "always", "state": "present"}},
            {"name": "Write the backup script", "ansible.builtin.template": {"src": "backup.sh.j2", "dest": "/usr/local/sbin/solsarch-backup", "mode": "0750"}, "no_log": True},
            {"name": "Run the backup every night", "ansible.builtin.cron": {
                "name": "solsarch nightly backup", "minute": "17", "hour": "2", "job": "/usr/local/sbin/solsarch-backup >> /var/log/solsarch-backup.log 2>&1"}},
        ]
        self.role_file("data", "tasks", "main.yml", "---\n" + dump(tasks))
        blocks = ["services:"]
        if "postgres" in self.data:
            image = "pgvector/pgvector:pg{{ postgres_version }}" if self.m.has("vector") else "postgres:{{ postgres_version }}"
            blocks.append(T("""
                  postgres:
                    image: @@image@@
                    restart: unless-stopped
                    environment:
                      POSTGRES_USER: app
                      POSTGRES_DB: app
                      POSTGRES_PASSWORD: "{{ vault_postgres_password }}"
                    ports: ["{{ data_host_private_address }}:5432:5432"]
                    volumes: [postgres_data:/var/lib/postgresql/data]
                    healthcheck:
                      test: ["CMD-SHELL", "pg_isready -U app -d app"]
                      interval: 10s
                      timeout: 5s
                      retries: 10
            """, image=image).rstrip("\n"))
        if "mysql" in self.data:
            blocks.append(T("""
                  mysql:
                    image: mysql:8.4
                    restart: unless-stopped
                    environment:
                      MYSQL_DATABASE: app
                      MYSQL_USER: app
                      MYSQL_PASSWORD: "{{ vault_mysql_password }}"
                      MYSQL_ROOT_PASSWORD: "{{ vault_mysql_password }}"
                    ports: ["{{ data_host_private_address }}:3306:3306"]
                    volumes: [mysql_data:/var/lib/mysql]
            """).rstrip("\n"))
        if "mongo" in self.data:
            blocks.append(T("""
                  mongo:
                    image: mongo:7
                    restart: unless-stopped
                    environment:
                      MONGO_INITDB_ROOT_USERNAME: app
                      MONGO_INITDB_ROOT_PASSWORD: "{{ vault_mongo_password }}"
                    ports: ["{{ data_host_private_address }}:27017:27017"]
                    volumes: [mongo_data:/data/db]
            """).rstrip("\n"))
        if "redis" in self.data:
            blocks.append(T("""
                  redis:
                    image: redis:7
                    restart: unless-stopped
                    command: ["redis-server", "--requirepass", "{{ vault_redis_password }}", "--appendonly", "yes"]
                    ports: ["{{ data_host_private_address }}:6379:6379"]
                    volumes: [redis_data:/data]
            """).rstrip("\n"))
        if "rabbitmq" in self.data:
            blocks.append(T("""
                  rabbitmq:
                    image: rabbitmq:3-management
                    restart: unless-stopped
                    environment:
                      RABBITMQ_DEFAULT_USER: app
                      RABBITMQ_DEFAULT_PASS: "{{ vault_rabbitmq_password }}"
                    ports: ["{{ data_host_private_address }}:5672:5672"]
                    volumes: [rabbitmq_data:/var/lib/rabbitmq]
            """).rstrip("\n"))
        vols = {"postgres": "postgres_data", "mysql": "mysql_data", "mongo": "mongo_data", "redis": "redis_data", "rabbitmq": "rabbitmq_data"}
        blocks.append("")
        blocks.append("volumes:")
        blocks += ["  %s: {}" % vols[s] for s in self.data]
        self.role_file("data", "templates", "data-compose.yml.j2", "\n".join(blocks))
        lines = ["#!/bin/sh", "set -eu", 'stamp=$(date +%Y%m%d-%H%M%S)', 'dir=/opt/data/backups']
        if "postgres" in self.data:
            lines.append('docker compose -f /opt/data/compose.yml exec -T postgres pg_dump -U app -Fc app > "$dir/postgres-$stamp.dump"')
        if "mysql" in self.data:
            lines.append('docker compose -f /opt/data/compose.yml exec -T mysql mysqldump -uapp -p"{{ vault_mysql_password }}" app > "$dir/mysql-$stamp.sql"')
        if "mongo" in self.data:
            lines.append('docker compose -f /opt/data/compose.yml exec -T mongo mongodump -u app -p "{{ vault_mongo_password }}" --authenticationDatabase admin --archive > "$dir/mongo-$stamp.archive"')
        lines.append('find "$dir" -type f -mtime +{{ backup_retention_days }} -delete')
        lines.append('{% if backup_bucket %}')
        lines.append('AWS_ACCESS_KEY_ID="{{ vault_backup_access_key }}" AWS_SECRET_ACCESS_KEY="{{ vault_backup_secret_key }}" \\')
        lines.append('  docker run --rm -v "$dir":/backup amazon/aws-cli --endpoint-url "{{ backup_endpoint }}" s3 sync /backup "s3://{{ backup_bucket }}/backups/"')
        lines.append('{% endif %}')
        self.role_file("data", "templates", "backup.sh.j2", "\n".join(lines))

    def monitoring_role(self):
        tasks = [
            {"name": "Create the monitoring folder", "ansible.builtin.file": {"path": "/opt/monitoring", "state": "directory", "mode": "0750"}},
            {"name": "Write the monitoring compose file", "ansible.builtin.template": {
                "src": "monitoring-compose.yml.j2", "dest": "/opt/monitoring/compose.yml", "mode": "0600", "validate": "docker compose -f %s config -q"}, "no_log": True},
            {"name": "Write the Prometheus configuration", "ansible.builtin.template": {"src": "prometheus.yml.j2", "dest": "/opt/monitoring/prometheus.yml", "mode": "0644"}},
            {"name": "Start monitoring", "community.docker.docker_compose_v2": {
                "project_src": "/opt/monitoring", "files": ["compose.yml"], "pull": "always", "state": "present"}},
        ]
        self.role_file("monitoring", "tasks", "main.yml", "---\n" + dump(tasks))
        self.role_file("monitoring", "templates", "monitoring-compose.yml.j2", T("""
            services:
              node-exporter:
                image: prom/node-exporter:v1.8.2
                restart: unless-stopped
                network_mode: host
                pid: host
                volumes: ["/:/host:ro,rslave"]
                command: ["--path.rootfs=/host"]
              cadvisor:
                image: gcr.io/cadvisor/cadvisor:v0.49.1
                restart: unless-stopped
                ports: ["127.0.0.1:8081:8080"]
                volumes:
                  - /:/rootfs:ro
                  - /var/run:/var/run:ro
                  - /sys:/sys:ro
                  - /var/lib/docker/:/var/lib/docker:ro
            {% if 'app' in group_names %}
              prometheus:
                image: prom/prometheus:v2.55.1
                restart: unless-stopped
                ports: ["127.0.0.1:9090:9090"]
                volumes:
                  - ./prometheus.yml:/etc/prometheus/prometheus.yml:ro
                  - prometheus_data:/prometheus
              grafana:
                image: grafana/grafana:11.3.0
                restart: unless-stopped
                ports: ["127.0.0.1:3000:3000"]
                environment:
                  GF_SECURITY_ADMIN_PASSWORD: "{{ vault_grafana_admin_password }}"
                  GF_USERS_ALLOW_SIGN_UP: "false"
                volumes: [grafana_data:/var/lib/grafana]
            volumes:
              prometheus_data: {}
              grafana_data: {}
            {% endif %}
        """))
        self.role_file("monitoring", "templates", "prometheus.yml.j2", T("""
            global:
              scrape_interval: 30s
            scrape_configs:
              - job_name: node
                static_configs:
                  - targets: ["127.0.0.1:9100"]
              - job_name: containers
                static_configs:
                  - targets: ["127.0.0.1:8081"]
        """))

    def readme(self):
        return T("""
            # Server configuration for @@project@@

            Generated from plan @@fp@@. This playbook turns plain Linux servers into a hardened Docker host, runs your units
            behind Caddy with automatic HTTPS, runs the self-run data services on a data host with nightly backups, and
            adds monitoring. It is the same on every cloud; only the Terraform that creates the servers differs.

            ## Steps

            1. Create the servers with the Terraform in `../terraform`. Copy its `ansible_inventory` output into `inventory/production.ini`.
            2. `ansible-galaxy collection install -r requirements.yml`
            3. `cp group_vars/all/vault.example.yml group_vars/all/vault.yml`, fill it in, then `ansible-vault encrypt group_vars/all/vault.yml`.
            4. Edit `group_vars/all/main.yml`: set `public_domain`, `registry_user`, `data_host_private_address` and the image tags.
            5. `ansible-playbook site.yml --check --diff` to preview, then `ansible-playbook site.yml --ask-vault-pass`.

            ## What it does

            - Hardens SSH (keys only), turns the firewall on with deny by default, adds fail2ban and automatic security updates.
            - Installs Docker, limits log size and runs each unit as a read-only, non-root container with CPU and memory limits.
            - Waits for each public unit to pass its health check before it finishes.
            - Runs the data services marked self-run in the plan (@@data@@), with nightly backups you can copy to object storage.
        """, project=self.m.project, fp=self.m.fingerprint, data=", ".join(self.data) or "none; the plan uses managed data services")


def addons(model):
    """An Ansible playbook for Kubernetes providers: installs the ingress controller and certificate manager, then applies ../kubernetes."""
    files = Files(model.fingerprint)
    files.add("ansible/requirements.yml", dump({"collections": [{"name": "kubernetes.core"}]}))
    files.add("ansible/ansible.cfg", T("""
        [defaults]
        roles_path = roles
        retry_files_enabled = False
        interpreter_python = auto_silent
        result_format = yaml
    """))
    tasks = [
        {"name": "Add the ingress-nginx chart repository", "kubernetes.core.helm_repository": {"name": "ingress-nginx", "repo_url": "https://kubernetes.github.io/ingress-nginx"}},
        {"name": "Add the cert-manager chart repository", "kubernetes.core.helm_repository": {"name": "jetstack", "repo_url": "https://charts.jetstack.io"}},
        {"name": "Install the ingress controller", "kubernetes.core.helm": {
            "name": "ingress-nginx", "chart_ref": "ingress-nginx/ingress-nginx", "release_namespace": "ingress-nginx", "create_namespace": True,
            "wait": True, "values": {"controller": {"replicaCount": 2, "allowSnippetAnnotations": False}}}},
        {"name": "Add the Prometheus chart repository", "kubernetes.core.helm_repository": {"name": "prometheus-community", "repo_url": "https://prometheus-community.github.io/helm-charts"}},
        {"name": "Install metrics and alerting (kube-prometheus-stack)", "kubernetes.core.helm": {
            "name": "kube-prometheus-stack", "chart_ref": "prometheus-community/kube-prometheus-stack", "release_namespace": "monitoring", "create_namespace": True,
            "wait": True, "values": {"grafana": {"enabled": True}, "alertmanager": {"enabled": True}}}},
        {"name": "Install cert-manager", "kubernetes.core.helm": {
            "name": "cert-manager", "chart_ref": "jetstack/cert-manager", "release_namespace": "cert-manager", "create_namespace": True,
            "wait": True, "values": {"crds": {"enabled": True}}}},
        {"name": "Apply the application manifests", "kubernetes.core.k8s": {
            "state": "present", "definition": "{{ lookup('kubernetes.core.kustomize', dir=playbook_dir ~ '/../kubernetes') }}"}},
    ]
    play = [{"name": "Install cluster add-ons and the application", "hosts": "localhost", "connection": "local", "gather_facts": False, "tasks": tasks}]
    files.add("ansible/k8s-addons.yml", "---" + chr(10) + dump(play))
    files.add("ansible/README.md", T("""
        # Cluster add-ons for @@project@@

        Generated from plan @@fp@@. After the cluster exists (see ../terraform), point `KUBECONFIG` at it and run:

        1. `ansible-galaxy collection install -r requirements.yml`
        2. Edit `../kubernetes/configmap.yaml` and create the `@@project@@-secrets` secret (see `../kubernetes/secret.example.yaml`).
        3. `ansible-playbook k8s-addons.yml`

        It installs ingress-nginx, cert-manager and kube-prometheus-stack (metrics and alerts) with Helm and applies the Kustomize base in `../kubernetes`.
    """, project=model.project, fp=model.fingerprint))
    return files


def generate(model, registry=None):
    return Ansible(model, registry).generate()
