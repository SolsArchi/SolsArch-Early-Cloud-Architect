#!/usr/bin/env python3
"""Terraform for Scaleway, generated from the plan.

Containers run on Serverless Containers (kept warm with a minimum scale in the server design, scaling to zero in the
serverless design) from a private Container Registry, connected to a private network. PostgreSQL, Redis and MongoDB are the
managed services on the same private network, files go to Object Storage, and application secrets live in Secret Manager.
Containers are created with deploy turned off, so the first apply works before any image exists.
"""
from iac_common import Files, T, fmt_hcl, selfrun, tf

DEFAULT_REGION = "fr-par"
CPU = [70, 140, 280, 560, 1120, 2240, 4000]
MEMORY = [128, 256, 512, 1024, 2048, 4096]
REGIONS = ("fr-par", "nl-ams", "pl-waw")


class Scaleway:
    def __init__(self, model):
        self.m = model
        self.files = Files(model.fingerprint)
        self.region = model.region if model.region in REGIONS else DEFAULT_REGION
        self.units = [u for u in model.units if u["kind"] != "static"]
        self.public = [u for u in model.public_units() if u["kind"] != "static"]
        self.serverless = model.mode == "serverless"
        self.ha = not model.single_az
        self.pg = model.db_key == "database" and not selfrun(model, "database")
        self.redis = model.has("cache") and not selfrun(model, "cache")
        self.mongo = model.has("database_mongo") and not selfrun(model, "database_mongo")
        self.storage = model.has("storage")
        self.cpu = next((v for v in CPU if v >= float(model.sizing["unit_vcpu"]) * 1000), CPU[-1])
        self.memory = next((v for v in MEMORY if v >= float(model.sizing["unit_memory_gb"]) * 1024), MEMORY[-1])

    def generate(self):
        m = self.m
        self.versions()
        self.variables()
        self.core()
        self.data_services()
        if self.storage:
            self.object_storage()
        self.containers()
        self.outputs()
        self.files.add("terraform/backend.tf", T("""
            terraform {
              backend "s3" {}
            }
        """))
        self.files.add("terraform/backend.hcl.example", T("""
            # Scaleway Object Storage speaks the S3 protocol. Create a bucket in the console, then fill this in.
            bucket                      = "REPLACE-with-your-state-bucket"
            key                         = "@@project@@/production.tfstate"
            region                      = "@@region@@"
            endpoints                   = { s3 = "https://s3.@@region@@.scw.cloud" }
            skip_credentials_validation = true
            skip_region_validation      = true
            skip_requesting_account_id  = true
            skip_metadata_api_check     = true
            skip_s3_checksum            = true
        """, project=m.project, region=self.region))
        self.files.add("terraform/terraform.tfvars.example", fmt_hcl(
            'project_name = "%s"\nenvironment  = "production"\nregion       = "%s"\ndeploy_app   = false\n' % (m.project, self.region)))
        self.files.add("terraform/README.md", self.readme())
        return self.files

    def versions(self):
        self.files.add("terraform/versions.tf", tf(T("""
            terraform {
              required_version = ">= 1.9.0"

              required_providers {
                scaleway = {
                  source  = "scaleway/scaleway"
                  version = "~> 2.80"
                }
                random = {
                  source  = "hashicorp/random"
                  version = "~> 3.6"
                }
              }
            }

            provider "scaleway" {
              region = var.region
              zone   = "${var.region}-1"
            }
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
              description = "Scaleway region: fr-par, nl-ams or pl-waw."
              type        = string
              default     = "@@region@@"
            }

            variable "image_tag" {
              description = "Tag of the container images in the registry."
              type        = string
              default     = "latest"
            }

            variable "deploy_app" {
              description = "Start the containers. Turn on only after the images are pushed to the registry."
              type        = bool
              default     = false
            }

            variable "cpu_limit" {
              description = "CPU limit of each container in mCPU (70, 140, 280, 560, 1120, 2240, 4000)."
              type        = number
              default     = @@cpu@@
            }

            variable "memory_limit" {
              description = "Memory limit of each container in MB (128, 256, 512, 1024, 2048, 4096)."
              type        = number
              default     = @@memory@@
            }

            variable "min_scale" {
              description = "Fewest running copies of each web container. Zero lets it sleep when idle."
              type        = number
              default     = @@min@@
            }

            variable "max_scale" {
              description = "Most running copies of each container."
              type        = number
              default     = 10
            }
        """, region=self.region, cpu=self.cpu, memory=self.memory, min=0 if self.serverless else 1)))

    def core(self):
        self.files.add("terraform/core.tf", tf(T("""
            locals {
              name = "${var.project_name}-${var.environment}"
              tags = [var.project_name, var.environment, "solsarch"]
            }

            resource "scaleway_vpc" "main" {
              name = local.name
              tags = local.tags
            }

            resource "scaleway_vpc_private_network" "main" {
              name   = local.name
              vpc_id = scaleway_vpc.main.id
              tags   = local.tags

              ipv4_subnet {
                subnet = "10.40.0.0/24"
              }
            }

            resource "scaleway_registry_namespace" "main" {
              name      = local.name
              is_public = false
            }

            resource "scaleway_container_namespace" "main" {
              name = local.name
              tags = local.tags
            }

            resource "scaleway_secret" "app" {
              name        = "${local.name}-app"
              description = "Application secrets. Put the values in with the console or scw secret."
              tags        = local.tags
            }
        """)))

    def data_services(self):
        parts = []
        if self.pg:
            parts.append(T("""
                resource "random_password" "database" {
                  length  = 32
                  special = false
                }

                resource "scaleway_rdb_instance" "main" {
                  name                = "${local.name}-db"
                  engine              = "PostgreSQL-16"
                  node_type           = "@@type@@"
                  is_ha_cluster       = @@ha@@
                  user_name           = "app"
                  password            = random_password.database.result
                  encryption_at_rest  = true
                  volume_type         = "sbs_5k"
                  volume_size_in_gb   = 20
                  tags                = local.tags

                  private_network {
                    pn_id       = scaleway_vpc_private_network.main.id
                    enable_ipam = true
                  }
                }
            """, type="DB-GP-XS" if self.ha else "DB-DEV-S", ha="true" if self.ha else "false"))
        if self.redis:
            parts.append(T("""
                resource "random_password" "redis" {
                  length  = 32
                  special = false
                }

                resource "scaleway_redis_cluster" "main" {
                  name         = "${local.name}-redis"
                  version      = var.redis_version
                  node_type    = "RED1-MICRO"
                  user_name    = "app"
                  password     = random_password.redis.result
                  cluster_size = @@size@@
                  tls_enabled  = true
                  tags         = local.tags

                  private_network {
                    id = scaleway_vpc_private_network.main.id
                  }
                }

                variable "redis_version" {
                  description = "Redis version. List them with: scw redis version list"
                  type        = string
                  default     = "7.2.11"
                }
            """, size=3 if self.ha else 1))
        if self.mongo:
            parts.append(T("""
                resource "random_password" "mongodb" {
                  length  = 32
                  special = false
                }

                resource "scaleway_mongodb_instance" "main" {
                  name              = "${local.name}-mongo"
                  version           = "7.0"
                  node_type         = "MGDB-PLAY2-NANO"
                  node_number       = @@nodes@@
                  user_name         = "app"
                  password          = random_password.mongodb.result
                  volume_type       = "sbs_5k"
                  volume_size_in_gb = 20
                  tags              = local.tags

                  private_network {
                    pn_id = scaleway_vpc_private_network.main.id
                  }
                }
            """, nodes=3 if self.ha else 1))
        if parts:
            self.files.add("terraform/data.tf", tf(*parts))

    def object_storage(self):
        self.files.add("terraform/storage.tf", tf(T("""
            resource "scaleway_object_bucket" "assets" {
              name = "${local.name}-assets"
              tags = { project = var.project_name, environment = var.environment }

              versioning {
                enabled = true
              }
            }
        """)))

    def env(self):
        out = []
        if self.pg:
            out += [("DB_HOST", "scaleway_rdb_instance.main.private_ip[0].address", False),
                    ("DB_PORT", "try(scaleway_rdb_instance.main.private_network[0].port, 5432)", False), ("DB_NAME", '"rdb"', False), ("DB_USER", '"app"', False)]
        if self.redis:
            out += [("REDIS_URL", "scaleway_redis_cluster.main.connection_string", True)]
        if self.mongo:
            out += [("MONGODB_HOST", "scaleway_mongodb_instance.main.public_network[0].dns_record", False)]
        if self.storage:
            out += [("S3_BUCKET", "scaleway_object_bucket.assets.name", False), ("S3_ENDPOINT", "scaleway_object_bucket.assets.api_endpoint", False)]
        return out

    def secret_env(self):
        out = []
        if self.pg:
            out.append(("DB_PASSWORD", "random_password.database.result"))
        if self.mongo:
            out.append(("MONGODB_PASSWORD", "random_password.mongodb.result"))
        return out

    def containers(self):
        first = self.public[0] if self.public else None
        env = self.env()
        secrets = self.secret_env()
        parts = []
        for u in self.units:
            n, worker = u["name"], u["kind"] == "worker"
            envs = ['    APP_ENV = "production"', '    PORT    = "%d"' % (u["port"] or 8080)]
            for k, v, _ in env:
                if v.startswith('"'):
                    envs.append("    %s = %s" % (k, v))
                else:
                    envs.append("    %s = tostring(%s)" % (k, v))
            sec = "\n".join("    %s = %s" % (k, v) for k, v in secrets)
            block = ['resource "scaleway_container" "%s" {' % n.replace("-", "_"),
                     '  name           = "${local.name}-%s"' % n,
                     "  namespace_id   = scaleway_container_namespace.main.id",
                     '  registry_image = "${scaleway_registry_namespace.main.endpoint}/%s:${var.image_tag}"' % n,
                     "  port           = %d" % (u["port"] or 8080),
                     "  cpu_limit      = var.cpu_limit",
                     "  memory_limit   = var.memory_limit",
                     "  min_scale      = %s" % ("1" if worker else "var.min_scale"),
                     "  max_scale      = var.max_scale",
                     '  privacy        = "%s"' % ("public" if u in self.public else "private"),
                     "  deploy         = var.deploy_app",
                     "  private_network_id = scaleway_vpc_private_network.main.id",
                     "  https_connections_only = true",
                     "  tags           = local.tags",
                     "",
                     "  environment_variables = {", "\n".join(envs), "  }"]
            if sec:
                block += ["", "  secret_environment_variables = {", sec, "  }"]
            if not worker:
                block += ["", "  liveness_probe {", "    http {", '      path = "%s"' % (u["health_path"] or "/"), "    }", "    failure_threshold = 3",
                          '    interval          = "30s"', '    timeout           = "5s"', "  }"]
            block.append("}")
            parts.append("\n".join(block))
        self.files.add("terraform/containers.tf", tf(*parts))

    def outputs(self):
        parts = [T("""
            output "registry_endpoint" {
              description = "Push your images here before turning deploy_app on."
              value       = scaleway_registry_namespace.main.endpoint
            }
        """)]
        for u in self.public:
            n = u["name"].replace("-", "_")
            parts.append(T("""
                output "@@n@@_url" {
                  description = "Public address of the @@name@@ container."
                  value       = scaleway_container.@@n@@.public_endpoint
                }
            """, n=n, name=u["name"]))
        if self.pg:
            parts.append(T("""
                output "database_password" {
                  description = "The generated database password. Put it in the secret store, not in a file."
                  value       = random_password.database.result
                  sensitive   = true
                }
            """))
        self.files.add("terraform/outputs.tf", tf(*parts))

    def readme(self):
        m = self.m
        return T("""
            # Scaleway infrastructure for @@project@@

            Generated from plan @@fp@@. This creates a private network, a private Container Registry, Serverless Containers@@data@@@@obj@@
            and a Secret Manager secret for application settings.

            ## Steps

            1. Create API keys (IAM, API keys) and export `SCW_ACCESS_KEY`, `SCW_SECRET_KEY`, `SCW_DEFAULT_PROJECT_ID` and `SCW_DEFAULT_ORGANIZATION_ID`.
            2. Create an Object Storage bucket for state, copy `backend.hcl.example` to `backend.hcl`, fill it in, and export the keys as `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`.
            3. `terraform init -backend-config=backend.hcl`, then `terraform apply` with `deploy_app = false`.
            4. Build and push each image to `terraform output registry_endpoint` (`docker login rg.<region>.scw.cloud`, then `docker push`).
            5. Set `deploy_app = true` and apply again.

            ## Notes

            - Scaleway Serverless Containers have a public address of their own, so there is no load balancer in this design. Put the Scaleway Load Balancer or Edge Services in front if you need custom routing or a firewall.
            - Database engine versions and node types change; list current ones with `scw rdb node-type list` and `scw rdb engine list` if an apply is refused.
            - Queues (Kafka, RabbitMQ) and an identity service have no native Scaleway product in the catalogue; the plan lists them as gaps.
        """, project=m.project, fp=m.fingerprint, data=(", managed data services on the private network" if (self.pg or self.redis or self.mongo) else ""),
                 obj=(", an Object Storage bucket" if self.storage else ""))


def generate(model):
    return Scaleway(model).generate()
