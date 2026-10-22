#!/usr/bin/env python3
"""Terraform for DigitalOcean, generated from the plan.

Containers run on App Platform from a private Container Registry, inside a VPC, with managed databases (PostgreSQL, MySQL,
MongoDB, Valkey, Kafka) behind a database firewall, and Spaces for files. The app is created in a second step, after the
images exist in the registry (var.deploy_app), so the first apply never fails on a missing image.
"""
from iac_common import Files, T, fmt_hcl, tf

DEFAULT_REGION = "nyc3"
SIZES = [(0.5, "apps-s-1vcpu-0.5gb"), (1.0, "apps-s-1vcpu-1gb"), (2.0, "apps-s-1vcpu-2gb"), (1e9, "apps-d-2vcpu-4gb")]
DB_SIZE = {"small": "db-s-1vcpu-1gb", "medium": "db-s-2vcpu-4gb", "large": "db-s-4vcpu-8gb"}
ENGINES = [("database", "pg", "16", "database", "5432"), ("database_mysql", "mysql", "8", "mysql", "3306"),
           ("database_mongo", "mongodb", "7.0", "mongo", "27017"), ("cache", "valkey", "8", "cache", "25061"),
           ("queue_kafka", "kafka", "3.8", "kafka", "25062")]


class DigitalOcean:
    def __init__(self, model):
        self.m = model
        self.files = Files(model.fingerprint)
        self.region = model.region if model.region and model.region[:3].isalpha() and model.region[-1:].isdigit() else DEFAULT_REGION
        self.public = [u for u in model.public_units() if u["kind"] != "static"]
        self.units = [u for u in model.units if u["kind"] != "static"]
        self.ha = not model.single_az
        self.dbs = [e for e in ENGINES if model.has(e[0]) and not self.selfrun(e[0])]
        self.storage = model.has("storage")
        cpu = float(model.sizing["unit_vcpu"])
        self.size = next(s for limit, s in SIZES if cpu <= limit)
        self.db_size = DB_SIZE.get(model.sizing.get("tier", "small"), DB_SIZE["small"])

    def selfrun(self, key):
        from iac_common import selfrun
        return selfrun(self.m, key)

    def generate(self):
        m = self.m
        self.versions()
        self.variables()
        self.core()
        self.databases()
        if self.storage:
            self.spaces()
        self.app()
        self.outputs()
        self.files.add("terraform/backend.tf", T("""
            terraform {
              backend "s3" {}
            }
        """))
        self.files.add("terraform/backend.hcl.example", T("""
            # DigitalOcean Spaces speaks the S3 protocol. Create a Space in the control panel, then fill this in.
            bucket                      = "REPLACE-with-your-state-space"
            key                         = "@@project@@/production.tfstate"
            region                      = "us-east-1"
            endpoints                   = { s3 = "https://@@region@@.digitaloceanspaces.com" }
            skip_credentials_validation = true
            skip_region_validation      = true
            skip_requesting_account_id  = true
            skip_metadata_api_check     = true
            skip_s3_checksum            = true
        """, project=m.project, region=self.region))
        self.files.add("terraform/terraform.tfvars.example", fmt_hcl(
            'project_name = "%s"\nenvironment  = "production"\nregion       = "%s"\nadmin_cidr   = "REPLACE-with-your-office-address/32"\ndeploy_app   = false\n' % (m.project, self.region)))
        self.files.add("terraform/README.md", self.readme())
        return self.files

    def versions(self):
        self.files.add("terraform/versions.tf", tf(T("""
            terraform {
              required_version = ">= 1.9.0"

              required_providers {
                digitalocean = {
                  source  = "digitalocean/digitalocean"
                  version = "~> 2.100"
                }
              }
            }

            provider "digitalocean" {}
        """)))

    def variables(self):
        self.files.add("terraform/variables.tf", tf(T("""
            variable "project_name" {
              description = "Short name used in every resource name."
              type        = string
            }

            variable "environment" {
              description = "Environment name, for example production."
              type        = string
              default     = "production"
            }

            variable "region" {
              description = "DigitalOcean region slug, for example nyc3, fra1, sgp1."
              type        = string
              default     = "@@region@@"
            }

            variable "admin_cidr" {
              description = "Address range allowed to reach the databases directly, for example your office /32."
              type        = string
            }

            variable "image_tag" {
              description = "Tag of the container images in the registry."
              type        = string
              default     = "latest"
            }

            variable "deploy_app" {
              description = "Create the App Platform app. Turn on only after the images are pushed to the registry."
              type        = bool
              default     = false
            }

            variable "instance_size" {
              description = "App Platform instance size for every unit."
              type        = string
              default     = "@@size@@"
            }

            variable "instance_count" {
              description = "How many instances of each web unit."
              type        = number
              default     = @@count@@
            }
        """, region=self.region, size=self.size, count=self.m.replicas if self.ha else 1)))

    def core(self):
        self.files.add("terraform/core.tf", tf(T("""
            locals {
              name = "${var.project_name}-${var.environment}"
              tags = [var.project_name, var.environment, "solsarch"]
            }

            resource "digitalocean_vpc" "main" {
              name        = local.name
              region      = var.region
              ip_range    = "10.30.0.0/24"
              description = "Private network for ${local.name}"
            }

            resource "digitalocean_container_registry" "main" {
              name                   = replace(local.name, "-", "")
              subscription_tier_slug = "@@tier@@"
              region                 = var.region
            }

            resource "digitalocean_project" "main" {
              name        = local.name
              description = "Managed by SolsArch Terraform"
              purpose     = "Web Application"
              environment = "Production"
            }
        """, tier="basic" if self.ha else "starter")))

    def databases(self):
        if not self.dbs:
            return
        parts = []
        for key, engine, version, name, port in self.dbs:
            nodes = 1 if (not self.ha or engine in ("mongodb",)) else (3 if engine == "kafka" else 2)
            size = "db-s-2vcpu-4gb" if engine == "kafka" else self.db_size
            parts.append(T("""
                resource "digitalocean_database_cluster" "@@name@@" {
                  name                 = "${local.name}-@@name@@"
                  engine               = "@@engine@@"
                  version              = "@@version@@"
                  size                 = "@@size@@"
                  region               = var.region
                  node_count           = @@nodes@@
                  private_network_uuid = digitalocean_vpc.main.id
                  tags                 = local.tags
                }
            """, name=name, engine=engine, version=version, size=size, nodes=nodes))
        for _, _, _, name, _ in self.dbs:
            parts.append(T("""
                resource "digitalocean_database_firewall" "@@name@@" {
                  cluster_id = digitalocean_database_cluster.@@name@@.id

                  rule {
                    type  = "ip_addr"
                    value = var.admin_cidr
                  }

                  dynamic "rule" {
                    for_each = toset(digitalocean_app.main[*].id)
                    content {
                      type  = "app"
                      value = rule.value
                    }
                  }
                }
            """, name=name))
        self.files.add("terraform/databases.tf", tf(*parts))

    def spaces(self):
        self.files.add("terraform/storage.tf", tf(T("""
            resource "digitalocean_spaces_bucket" "assets" {
              name   = "${local.name}-assets"
              region = var.region
              acl    = "private"

              versioning {
                enabled = true
              }
            }
        """)))

    def env_lines(self, indent):
        """Environment variables for each App Platform component, from the managed databases and Spaces."""
        out = []
        for key, engine, version, name, port in self.dbs:
            ref = "digitalocean_database_cluster.%s" % name
            if engine in ("pg", "mysql"):
                out += [("DB_HOST", "%s.private_host" % ref, False), ("DB_PORT", "%s.port" % ref, False),
                        ("DB_NAME", "%s.database" % ref, False), ("DB_USER", "%s.user" % ref, False), ("DB_PASSWORD", "%s.password" % ref, True)]
            elif engine == "mongodb":
                out.append(("MONGODB_URI", "%s.private_uri" % ref, True))
            elif engine == "valkey":
                out += [("REDIS_HOST", "%s.private_host" % ref, False), ("REDIS_PORT", "%s.port" % ref, False), ("REDIS_PASSWORD", "%s.password" % ref, True)]
            elif engine == "kafka":
                out.append(("KAFKA_BOOTSTRAP_SERVERS", '"${%s.private_host}:${%s.port}"' % (ref, ref), False))
        if self.storage:
            out += [("S3_BUCKET", "digitalocean_spaces_bucket.assets.name", False),
                    ("S3_ENDPOINT", '"https://${var.region}.digitaloceanspaces.com"', False)]
        return out

    def app(self):
        env = self.env_lines(0)

        def env_blocks(pad):
            text = ""
            for key, value, secret in env:
                expr = value if value.startswith('"') else "tostring(%s)" % value
                text += pad + "env {\n" + pad + '  key   = "%s"\n' % key + pad + "  value = %s\n" % expr
                text += pad + '  type  = "%s"\n' % ("SECRET" if secret else "GENERAL") + pad + "}\n\n"
            return text.rstrip("\n")

        comps = []
        first = self.public[0] if self.public else None
        for u in self.units:
            n, worker = u["name"], u["kind"] == "worker"
            body = [
                '    name               = "%s"' % n,
                "    instance_size_slug = var.instance_size",
                "    instance_count     = %s" % ("1" if worker else "var.instance_count"),
            ]
            if not worker:
                body.append("    http_port          = %d" % (u["port"] or 8080))
            block = "  %s {\n%s\n\n" % ("worker" if worker else "service", "\n".join(body))
            block += "    image {\n      registry_type = \"DOCR\"\n      repository    = \"%s\"\n      tag           = var.image_tag\n    }\n" % n
            if not worker:
                block += "\n    health_check {\n      http_path = \"%s\"\n    }\n" % (u["health_path"] or "/")
                if u in self.public:
                    path = "/" if u is first else "/%s" % n
                    block += "\n    routes {\n      path = \"%s\"\n    }\n" % path
            envs = env_blocks("    ")
            if envs:
                block += "\n" + envs + "\n"
            block += "  }\n"
            comps.append(block)
        spec = "\n".join(comps)
        self.files.add("terraform/app.tf", tf(T("""
            resource "digitalocean_app" "main" {
              count = var.deploy_app ? 1 : 0

              spec {
                name   = local.name
                region = var.region

            @@components@@
              }

              depends_on = [digitalocean_container_registry.main]
            }

            resource "digitalocean_project_resources" "main" {
              project   = digitalocean_project.main.id
              resources = concat(
                [for a in digitalocean_app.main : a.urn],
                @@dburns@@
              )
            }
        """, components=spec.rstrip("\n"), dburns="[%s]" % ", ".join("digitalocean_database_cluster.%s.urn" % d[3] for d in self.dbs))))

    def outputs(self):
        parts = [T("""
            output "registry_endpoint" {
              description = "Push your images here before turning deploy_app on."
              value       = digitalocean_container_registry.main.endpoint
            }

            output "app_url" {
              description = "The address App Platform gave the app."
              value       = one(digitalocean_app.main[*].live_url)
            }
        """)]
        for _, engine, _, name, _ in self.dbs:
            parts.append(T("""
                output "@@name@@_private_host" {
                  description = "Private address of the @@engine@@ cluster."
                  value       = digitalocean_database_cluster.@@name@@.private_host
                }
            """, name=name, engine=engine))
        self.files.add("terraform/outputs.tf", tf(*parts))

    def readme(self):
        m = self.m
        return T("""
            # DigitalOcean infrastructure for @@project@@

            Generated from plan @@fp@@. This creates a VPC, a Container Registry, @@dbs@@a project@@spaces@@, and (in the second step) an App Platform app.

            ## Steps

            1. Create a token (API, Generate New Token) and export it: `export DIGITALOCEAN_TOKEN=...`. For Spaces also export `SPACES_ACCESS_KEY_ID` and `SPACES_SECRET_ACCESS_KEY`.
            2. Create a Space for state, copy `backend.hcl.example` to `backend.hcl`, fill it in, and export its keys as `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`.
            3. `cp terraform.tfvars.example terraform.tfvars`, set `admin_cidr`.
            4. `terraform init -backend-config=backend.hcl`, then `terraform apply` (first step: `deploy_app = false`).
            5. Build and push each image to `terraform output registry_endpoint` (`doctl registry login`, then `docker push`).
            6. Set `deploy_app = true` and `terraform apply` again.

            ## Notes

            - Databases accept connections only from your `admin_cidr` and from the app; they use private addresses inside the VPC.
            - Secrets reach the app as encrypted environment variables. They also sit in the Terraform state, so keep the state bucket private.
            - Serverless design: DigitalOcean Functions deploy with `doctl serverless`, not Terraform. The app here is the container equivalent.
        """, project=m.project, fp=m.fingerprint, dbs=("managed databases (%s), " % ", ".join(e[1] for e in self.dbs) if self.dbs else ""),
                 spaces=(", a Spaces bucket" if self.storage else ""))


def generate(model):
    return DigitalOcean(model).generate()
