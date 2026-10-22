#!/usr/bin/env python3
"""Terraform for IBM Cloud, generated from the plan.

Containers (and the serverless design too, since Code Engine scales to zero) run on IBM Cloud Code Engine apps pulling from
Container Registry. The data services are IBM Cloud Databases (PostgreSQL, Redis, MongoDB) on private endpoints, Event
Streams for Kafka, Cloud Object Storage for files, Secrets Manager, Cloud Logs, App ID and Internet Services (CIS) when the
plan uses them, all inside one resource group with a VPC and public gateway beside them. Apps are created in a second step
(var.deploy_app), after the images exist in the registry.
"""
from iac_common import Files, T, fmt_hcl, selfrun, tf

DEFAULT_REGION = "us-south"
SCALE_CPU = [0.125, 0.25, 0.5, 1, 2, 4, 6, 8]
SCALE_MEM = {0.125: "0.25G", 0.25: "0.5G", 0.5: "1G", 1: "2G", 2: "4G", 4: "8G", 6: "12G", 8: "16G"}


class Ibm:
    def __init__(self, model):
        self.m = model
        self.files = Files(model.fingerprint)
        self.public = [u for u in model.public_units() if u["kind"] != "static"]
        self.units = [u for u in model.units if u["kind"] != "static"]
        self.serverless = model.mode == "serverless"
        self.ha = not model.single_az
        self.pg = model.db_key == "database" and not selfrun(model, "database")
        self.redis = model.has("cache") and not selfrun(model, "cache")
        self.mongo = model.has("database_mongo") and not selfrun(model, "database_mongo")
        self.kafka = model.has("queue_kafka") and not selfrun(model, "queue_kafka")
        self.storage = model.has("storage")
        self.secrets = model.has("secrets")
        self.identity = model.has("identity")
        self.edge = model.has("edge")
        self.cpu = next((c for c in SCALE_CPU if c >= float(model.sizing["unit_vcpu"])), SCALE_CPU[-1])
        self.memory = SCALE_MEM[self.cpu]
        self.region = model.region if model.region in ("us-south", "us-east", "eu-gb", "eu-de", "eu-es", "jp-tok", "jp-osa", "au-syd", "ca-tor", "br-sao") else DEFAULT_REGION

    def generate(self):
        m = self.m
        self.versions()
        self.variables()
        self.core()
        self.apps()
        self.data_services()
        if self.storage:
            self.object_storage()
        self.platform_services()
        self.outputs()
        self.files.add("terraform/backend.tf", T("""
            terraform {
              backend "s3" {}
            }
        """))
        self.files.add("terraform/backend.hcl.example", T("""
            # IBM Cloud Object Storage speaks the S3 protocol. Create a bucket and HMAC credentials, then fill this in.
            bucket                      = "REPLACE-with-your-state-bucket"
            key                         = "@@project@@/production.tfstate"
            region                      = "@@region@@"
            endpoints                   = { s3 = "https://s3.@@region@@.cloud-object-storage.appdomain.cloud" }
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
                ibm = {
                  source  = "IBM-Cloud/ibm"
                  version = "~> 2.0"
                }
                random = {
                  source  = "hashicorp/random"
                  version = "~> 3.6"
                }
              }
            }

            provider "ibm" {
              region = var.region
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
              description = "IBM Cloud region, for example us-south, eu-de, eu-gb, jp-tok."
              type        = string
              default     = "@@region@@"
            }

            variable "image_tag" {
              description = "Tag of the container images in the registry."
              type        = string
              default     = "latest"
            }

            variable "deploy_app" {
              description = "Create the Code Engine apps. Turn on only after the images are pushed to the registry."
              type        = bool
              default     = false
            }

            variable "min_scale" {
              description = "Fewest running copies of each web app. Zero lets it sleep when idle."
              type        = number
              default     = @@min@@
            }

            variable "max_scale" {
              description = "Most running copies of each app."
              type        = number
              default     = 10
            }

            variable "cpu" {
              description = "vCPU of each instance (0.125 to 8)."
              type        = number
              default     = @@cpu@@
            }

            variable "memory" {
              description = "Memory of each instance, matching the CPU (for example 2G for 1 vCPU)."
              type        = string
              default     = "@@memory@@"
            }
        """, region=self.region, min=0 if self.serverless else (2 if self.ha else 1), cpu=self.cpu, memory=self.memory)))

    def core(self):
        self.files.add("terraform/core.tf", tf(T("""
            locals {
              name = "${var.project_name}-${var.environment}"
              tags = ["project:${var.project_name}", "environment:${var.environment}", "managed-by:solsarch"]
            }

            resource "ibm_resource_group" "main" {
              name = local.name
            }

            resource "ibm_is_vpc" "main" {
              name           = local.name
              resource_group = ibm_resource_group.main.id
              tags           = local.tags
            }

            resource "ibm_is_public_gateway" "main" {
              name           = "${local.name}-pgw"
              vpc            = ibm_is_vpc.main.id
              zone           = "${var.region}-1"
              resource_group = ibm_resource_group.main.id
            }

            resource "ibm_is_subnet" "main" {
              name                     = "${local.name}-main"
              vpc                      = ibm_is_vpc.main.id
              zone                     = "${var.region}-1"
              total_ipv4_address_count = 256
              public_gateway           = ibm_is_public_gateway.main.id
              resource_group           = ibm_resource_group.main.id
            }

            resource "ibm_is_security_group" "app" {
              name           = "${local.name}-app"
              vpc            = ibm_is_vpc.main.id
              resource_group = ibm_resource_group.main.id
              tags           = local.tags
            }

            resource "ibm_is_security_group_rule" "inbound_from_group" {
              group     = ibm_is_security_group.app.id
              direction = "inbound"
              remote    = ibm_is_security_group.app.id
            }

            resource "ibm_is_security_group_rule" "outbound_https" {
              group     = ibm_is_security_group.app.id
              direction = "outbound"
              remote    = "0.0.0.0/0"

              tcp {
                port_min = 443
                port_max = 443
              }
            }

            resource "ibm_cr_namespace" "main" {
              name              = replace(local.name, "-", "")
              resource_group_id = ibm_resource_group.main.id
            }

            resource "ibm_code_engine_project" "main" {
              name              = local.name
              resource_group_id = ibm_resource_group.main.id
            }
        """)))

    def env(self):
        out = [("APP_ENV", '"production"')]
        if self.pg:
            out += [("DB_HOST", "data.ibm_database_connection.postgres.postgres[0].hosts[0].hostname"),
                    ("DB_PORT", "data.ibm_database_connection.postgres.postgres[0].hosts[0].port"), ("DB_NAME", '"ibmclouddb"'), ("DB_USER", '"admin"')]
        if self.redis:
            out += [("REDIS_HOST", "data.ibm_database_connection.redis.rediss[0].hosts[0].hostname"),
                    ("REDIS_PORT", "data.ibm_database_connection.redis.rediss[0].hosts[0].port")]
        if self.mongo:
            out += [("MONGODB_HOST", "data.ibm_database_connection.mongodb.mongodb[0].hosts[0].hostname"),
                    ("MONGODB_PORT", "data.ibm_database_connection.mongodb.mongodb[0].hosts[0].port")]
        if self.storage:
            out += [("COS_BUCKET", "ibm_cos_bucket.assets.bucket_name"), ("COS_ENDPOINT", '"s3.private.${var.region}.cloud-object-storage.appdomain.cloud"')]
        return out

    def apps(self):
        parts = []
        env = []
        for k, v in self.env():
            env.append(T("""
                run_env_variables {
                  type  = "literal"
                  name  = "@@k@@"
                  value = @@v@@
                }
            """, k=k, v=v if v.startswith('"') else "tostring(%s)" % v))
        secret_env = []
        if self.pg:
            secret_env.append(("DB_PASSWORD", "random_password.postgres.result"))
        if self.redis:
            secret_env.append(("REDIS_PASSWORD", "random_password.redis.result"))
        if self.mongo:
            secret_env.append(("MONGODB_PASSWORD", "random_password.mongodb.result"))
        for k, v in secret_env:
            env.append(T("""
                run_env_variables {
                  type  = "literal"
                  name  = "@@k@@"
                  value = @@v@@
                }
            """, k=k, v=v))
        env_text = "\n".join(env)
        for u in self.units:
            n, worker, port = u["name"].replace("-", "_"), u["kind"] == "worker", u["port"] or 8080
            probes = ""
            if not worker:
                probes = T("""
                    probe_liveness {
                      type                = "http"
                      path                = "@@path@@"
                      port                = @@port@@
                      initial_delay       = 30
                      interval            = 15
                      timeout             = 5
                      failure_threshold   = 3
                    }

                    probe_readiness {
                      type                = "http"
                      path                = "@@path@@"
                      port                = @@port@@
                      initial_delay       = 10
                      interval            = 10
                      timeout             = 5
                      failure_threshold   = 3
                    }
                """, path=u["health_path"] or "/", port=port)
            parts.append(T("""
                resource "ibm_code_engine_app" "@@n@@" {
                  count               = var.deploy_app ? 1 : 0
                  project_id          = ibm_code_engine_project.main.project_id
                  name                = "@@name@@"
                  image_reference     = "private.${var.region}.icr.io/${ibm_cr_namespace.main.name}/@@name@@:${var.image_tag}"
                  image_port          = @@port@@
                  scale_cpu_limit     = tostring(var.cpu)
                  scale_memory_limit  = var.memory
                  scale_min_instances = @@min@@
                  scale_max_instances = var.max_scale
                  run_as_user         = 10001
                @@env@@
                @@probes@@
                }
            """, n=n, name=u["name"], port=port, min="1" if worker else "var.min_scale", env=env_text, probes=probes.rstrip("\n")))
        self.files.add("terraform/apps.tf", tf(*parts))

    def data_services(self):
        parts = []

        def database(name, service, version, adminpass):
            return T("""
                resource "ibm_database" "@@name@@" {
                  name              = "${local.name}-@@name@@"
                  service           = "@@service@@"
                  plan              = "standard"
                  location          = var.region
                  resource_group_id = ibm_resource_group.main.id
                  adminpassword     = random_password.@@pw@@.result
                  service_endpoints = "private"
                  version           = "@@version@@"
                  deletion_protection = true
                  tags              = local.tags
                }
            """, name=name, service=service, version=version, pw=adminpass)

        def connection(name):
            return T("""
                data "ibm_database_connection" "@@name@@" {
                  deployment_id = ibm_database.@@name@@.id
                  user_type     = "database"
                  user_id       = "admin"
                  endpoint_type = "private"
                }
            """, name=name)

        if self.pg:
            parts.append("resource \"random_password\" \"postgres\" {\n  length  = 32\n  special = false\n}\n")
            parts.append(database("postgres", "databases-for-postgresql", "16", "postgres"))
            parts.append(connection("postgres"))
        if self.redis:
            parts.append("resource \"random_password\" \"redis\" {\n  length  = 32\n  special = false\n}\n")
            parts.append(database("redis", "databases-for-redis", "7.2", "redis"))
            parts.append(connection("redis"))
        if self.mongo:
            parts.append("resource \"random_password\" \"mongodb\" {\n  length  = 32\n  special = false\n}\n")
            parts.append(database("mongodb", "databases-for-mongodb", "7.0", "mongodb"))
            parts.append(connection("mongodb"))
        if self.kafka:
            parts.append(T("""
                resource "ibm_resource_instance" "event_streams" {
                  name              = "${local.name}-events"
                  service           = "messagehub"
                  plan              = "standard"
                  location          = var.region
                  resource_group_id = ibm_resource_group.main.id
                  service_endpoints = "private"
                  tags              = local.tags
                }
            """))
        if parts:
            self.files.add("terraform/data.tf", tf(*parts))

    def object_storage(self):
        self.files.add("terraform/storage.tf", tf(T("""
            resource "ibm_resource_instance" "cos" {
              name              = "${local.name}-cos"
              service           = "cloud-object-storage"
              plan              = "standard"
              location          = "global"
              resource_group_id = ibm_resource_group.main.id
              tags              = local.tags
            }

            resource "ibm_cos_bucket" "assets" {
              bucket_name          = "${local.name}-assets"
              resource_instance_id = ibm_resource_instance.cos.id
              region_location      = var.region
              storage_class        = "smart"
              endpoint_type        = "private"

              object_versioning {
                enable = true
              }
            }
        """)))

    def platform_services(self):
        parts = [T("""
            resource "ibm_resource_instance" "logs" {
              name              = "${local.name}-logs"
              service           = "logs"
              plan              = "standard"
              location          = var.region
              resource_group_id = ibm_resource_group.main.id
              tags              = local.tags
            }
        """)]
        if self.secrets:
            parts.append(T("""
                resource "ibm_resource_instance" "secrets_manager" {
                  name              = "${local.name}-secrets"
                  service           = "secrets-manager"
                  plan              = "standard"
                  location          = var.region
                  resource_group_id = ibm_resource_group.main.id
                  service_endpoints = "public-and-private"
                  tags              = local.tags
                }
            """))
        if self.identity:
            parts.append(T("""
                resource "ibm_resource_instance" "app_id" {
                  name              = "${local.name}-appid"
                  service           = "appid"
                  plan              = "graduated-tier"
                  location          = var.region
                  resource_group_id = ibm_resource_group.main.id
                  tags              = local.tags
                }
            """))
        if self.edge:
            parts.append(T("""
                resource "ibm_cis" "edge" {
                  name              = "${local.name}-cis"
                  plan              = "standard-next"
                  resource_group_id = ibm_resource_group.main.id
                  location          = "global"
                  tags              = local.tags
                }
            """))
        self.files.add("terraform/platform.tf", tf(*parts))

    def outputs(self):
        parts = [T("""
            output "registry_address" {
              description = "Push your images here before turning deploy_app on."
              value       = "${var.region}.icr.io/${ibm_cr_namespace.main.name}"
            }

            output "project_id" {
              description = "The Code Engine project."
              value       = ibm_code_engine_project.main.project_id
            }
        """)]
        for u in self.public:
            n = u["name"].replace("-", "_")
            parts.append(T("""
                output "@@n@@_url" {
                  description = "Public address of the @@name@@ app."
                  value       = one(ibm_code_engine_app.@@n@@[*].endpoint)
                }
            """, n=n, name=u["name"]))
        if self.pg:
            parts.append(T("""
                output "database_password" {
                  description = "The generated database admin password. Put it in the secret store, not in a file."
                  value       = random_password.postgres.result
                  sensitive   = true
                }
            """))
        self.files.add("terraform/outputs.tf", tf(*parts))

    def readme(self):
        m = self.m
        return T("""
            # IBM Cloud infrastructure for @@project@@

            Generated from plan @@fp@@. This creates a resource group, a VPC with a public gateway, Container Registry, a Code Engine
            project with one app per unit, Cloud Logs@@more@@, and the data services the plan uses.

            ## Steps

            1. Create an API key (Manage, Access (IAM), API keys) and export `IC_API_KEY`.
            2. Create a Cloud Object Storage bucket for state and HMAC credentials, copy `backend.hcl.example` to `backend.hcl`, fill it in, and export the keys as `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`.
            3. `terraform init -backend-config=backend.hcl`, then `terraform apply` with `deploy_app = false`.
            4. Build and push each image to `terraform output registry_address` (`ibmcloud cr login`, then `docker push`).
            5. Set `deploy_app = true` and apply again.

            ## Notes

            - Code Engine scales to zero, so the server and serverless designs share these apps; `min_scale` is the difference.
            - Databases use private endpoints; Code Engine reaches them over IBM's private network.
            - RabbitMQ has no IBM product in the catalogue; the plan lists it as a gap.
        """, project=m.project, fp=m.fingerprint,
                 more=(", Secrets Manager" if self.secrets else "") + (", App ID" if self.identity else "") + (", Internet Services" if self.edge else ""))


def generate(model):
    return Ibm(model).generate()
