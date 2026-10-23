#!/usr/bin/env python3
"""Terraform for Alibaba Cloud, generated from the plan.

Server design: containers run on Serverless App Engine (SAE) in a VPC with two vSwitches, behind an Application Load
Balancer, with outbound traffic through a NAT gateway. Serverless design: Function Compute 3.0 custom-container
functions with an HTTP trigger. Data services are ApsaraDB RDS for PostgreSQL, Tair (Redis), ApsaraDB for MongoDB,
Message Queue for Kafka and RabbitMQ, files go to OSS with server-side encryption, images live in Container Registry,
secrets in KMS Secrets Manager, and logs in Simple Log Service. The app is created in a second step (var.deploy_app),
after the images exist in the registry.
"""
from iac_common import Files, T, add_app_env_files, fmt_hcl, merged_env, selfrun, settings_pairs, tf

DEFAULT_REGION = "cn-hangzhou"
CPU = [(0.5, 500), (1.0, 1000), (2.0, 2000), (4.0, 4000), (8.0, 8000)]
MEMORY = [1024, 2048, 4096, 8192, 16384, 32768]


class Alibaba:
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
        self.amqp = model.has("queue_rabbitmq") and not selfrun(model, "queue_rabbitmq")
        self.storage = model.has("storage")
        self.cdn = model.has("edge") and not self.serverless
        self.cpu = next((m for limit, m in CPU if float(model.sizing["unit_vcpu"]) <= limit), CPU[-1][1])
        self.memory = next((m for m in MEMORY if m >= float(model.sizing["unit_memory_gb"]) * 1024 and m >= self.cpu * 2), MEMORY[-1])
        self.region = model.region if model.region and "-" in model.region and model.region.split("-")[0] in ("cn", "ap", "eu", "us", "me") else DEFAULT_REGION

    def generate(self):
        m = self.m
        self.versions()
        self.variables()
        add_app_env_files(self.files, self.m)
        self.network()
        self.registry()
        if self.serverless:
            self.functions()
        else:
            self.sae()
        self.data_services()
        if self.storage:
            self.object_storage()
        self.security()
        self.observability()
        self.outputs()
        self.files.add("terraform/backend.tf", T("""
            terraform {
              backend "oss" {}
            }
        """))
        self.files.add("terraform/backend.hcl.example", T("""
            # Create an OSS bucket (and optionally a Tablestore instance for state locking), then fill this in.
            bucket   = "REPLACE-with-your-state-bucket"
            prefix   = "@@project@@"
            key      = "production.tfstate"
            region   = "@@region@@"
            encrypt  = true
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
                alicloud = {
                  source  = "aliyun/alicloud"
                  version = "~> 2.0"
                }
                random = {
                  source  = "hashicorp/random"
                  version = "~> 3.6"
                }
              }
            }

            provider "alicloud" {
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
              description = "Alibaba Cloud region, for example cn-hangzhou, ap-southeast-1, eu-central-1, us-west-1."
              type        = string
              default     = "@@region@@"
            }

            variable "image_tag" {
              description = "Tag of the container images in the registry."
              type        = string
              default     = "latest"
            }

            variable "deploy_app" {
              description = "Create the running applications or functions. Turn on only after the images are pushed."
              type        = bool
              default     = false
            }

            variable "replicas" {
              description = "How many copies of each web unit."
              type        = number
              default     = @@replicas@@
            }

            variable "cpu_millicores" {
              description = "CPU of each instance in millicores (500, 1000, 2000, 4000, 8000)."
              type        = number
              default     = @@cpu@@
            }

            variable "memory_mb" {
              description = "Memory of each instance in MB."
              type        = number
              default     = @@memory@@
            }

            variable "domain" {
              description = "Public domain name for the CDN. Leave empty to skip the CDN."
              type        = string
              default     = ""
            }
        """, region=self.region, replicas=self.m.replicas if self.ha else 1, cpu=self.cpu, memory=self.memory)))

    def network(self):
        self.files.add("terraform/network.tf", tf(T("""
            locals {
              name = "${var.project_name}-${var.environment}"
              tags = { project = var.project_name, environment = var.environment, managed-by = "solsarch" }
            }

            data "alicloud_zones" "available" {
              available_resource_creation = "VSwitch"
            }

            resource "alicloud_vpc" "main" {
              vpc_name   = local.name
              cidr_block = "10.70.0.0/16"
              tags       = local.tags
            }

            resource "alicloud_vswitch" "a" {
              vpc_id       = alicloud_vpc.main.id
              vswitch_name = "${local.name}-a"
              cidr_block   = "10.70.1.0/24"
              zone_id      = data.alicloud_zones.available.zones[0].id
              tags         = local.tags
            }

            resource "alicloud_vswitch" "b" {
              vpc_id       = alicloud_vpc.main.id
              vswitch_name = "${local.name}-b"
              cidr_block   = "10.70.2.0/24"
              zone_id      = data.alicloud_zones.available.zones[1].id
              tags         = local.tags
            }

            resource "alicloud_security_group" "app" {
              vpc_id              = alicloud_vpc.main.id
              security_group_name = "${local.name}-app"
              tags                = local.tags
            }

            resource "alicloud_nat_gateway" "main" {
              vpc_id           = alicloud_vpc.main.id
              vswitch_id       = alicloud_vswitch.a.id
              nat_gateway_name = local.name
              nat_type         = "Enhanced"
              network_type     = "internet"
              payment_type     = "PayAsYouGo"
              tags             = local.tags
            }

            resource "alicloud_eip_address" "nat" {
              address_name = "${local.name}-nat"
              payment_type = "PayAsYouGo"
              bandwidth    = "100"
              tags         = local.tags
            }

            resource "alicloud_eip_association" "nat" {
              allocation_id = alicloud_eip_address.nat.id
              instance_id   = alicloud_nat_gateway.main.id
              instance_type = "Nat"
            }

            resource "alicloud_snat_entry" "private" {
              snat_table_id     = alicloud_nat_gateway.main.snat_table_ids
              source_vswitch_id = alicloud_vswitch.a.id
              snat_ip           = alicloud_eip_address.nat.ip_address

              depends_on = [alicloud_eip_association.nat]
            }

            resource "alicloud_snat_entry" "private_b" {
              snat_table_id     = alicloud_nat_gateway.main.snat_table_ids
              source_vswitch_id = alicloud_vswitch.b.id
              snat_ip           = alicloud_eip_address.nat.ip_address

              depends_on = [alicloud_eip_association.nat]
            }
        """)))

    def registry(self):
        parts = [T("""
            resource "alicloud_cr_ee_instance" "main" {
              instance_name = replace(local.name, "-", "")
              instance_type = "Basic"
              payment_type  = "Subscription"
              period        = 1
              renew_period  = 1
              renewal_status = "AutoRenewal"
            }

            resource "alicloud_cr_ee_namespace" "main" {
              instance_id        = alicloud_cr_ee_instance.main.id
              name               = replace(local.name, "-", "")
              auto_create        = false
              default_visibility = "PRIVATE"
            }
        """)]
        for u in self.units:
            parts.append(T("""
                resource "alicloud_cr_ee_repo" "@@n@@" {
                  instance_id = alicloud_cr_ee_instance.main.id
                  namespace   = alicloud_cr_ee_namespace.main.name
                  name        = "@@name@@"
                  summary     = "Image of the @@name@@ unit"
                  repo_type   = "PRIVATE"
                }
            """, n=u["name"].replace("-", "_"), name=u["name"]))
        self.files.add("terraform/registry.tf", tf(*parts))

    def image(self, unit):
        return '"${alicloud_cr_ee_instance.main.instance_name}-registry-vpc.${var.region}.cr.aliyuncs.com/${alicloud_cr_ee_namespace.main.name}/%s:${var.image_tag}"' % unit["name"]

    def env_pairs(self):
        out = [("APP_ENV", '"production"')]
        if self.pg:
            out += [("DB_HOST", "alicloud_db_instance.main.connection_string"), ("DB_PORT", '"5432"'), ("DB_NAME", '"app"'), ("DB_USER", '"app"')]
        if self.redis:
            out += [("REDIS_HOST", "alicloud_kvstore_instance.main.connection_domain"), ("REDIS_PORT", '"6379"')]
        if self.storage:
            out += [("OSS_BUCKET", "alicloud_oss_bucket.assets.bucket"), ("OSS_REGION", "var.region")]
        out += settings_pairs(self.m)
        return out

    def sae(self):
        env_list = ",\n".join('      { name = "%s", value = %s }' % (k, v if v.startswith('"') else "tostring(%s)" % v) for k, v in self.env_pairs())
        secrets = []
        if self.pg:
            secrets.append('      { name = "DB_PASSWORD", value = random_password.database.result }')
        if self.redis:
            secrets.append('      { name = "REDIS_PASSWORD", value = random_password.redis.result }')
        parts = [T("""
            resource "alicloud_sae_namespace" "main" {
              namespace_id          = "${var.region}:${replace(local.name, "-", "")}"
              namespace_name        = local.name
              namespace_description = "Namespace of ${local.name}"
            }
        """)]
        for u in self.units:
            n, worker = u["name"].replace("-", "_"), u["kind"] == "worker"
            port = u["port"] or 8080
            probe = ""
            if not worker:
                probe = T("""
                    liveness_v2 {
                      initial_delay_seconds = 30
                      period_seconds        = 15
                      timeout_seconds       = 5
                      failure_threshold     = 3

                      http_get {
                        path   = "@@path@@"
                        port   = @@port@@
                        scheme = "HTTP"
                      }
                    }

                    readiness_v2 {
                      initial_delay_seconds = 10
                      period_seconds        = 10
                      timeout_seconds       = 5
                      failure_threshold     = 3
                      success_threshold     = 1

                      http_get {
                        path   = "@@path@@"
                        port   = @@port@@
                        scheme = "HTTP"
                      }
                    }
                """, path=u["health_path"] or "/", port=port)
            envs = env_list + (",\n" + ",\n".join(secrets) if secrets else "")
            parts.append(T("""
                resource "alicloud_sae_application" "@@n@@" {
                  count             = var.deploy_app ? 1 : 0
                  app_name          = "${local.name}-@@name@@"
                  namespace_id      = alicloud_sae_namespace.main.id
                  package_type      = "Image"
                  image_url         = @@image@@
                  replicas          = @@replicas@@
                  cpu               = var.cpu_millicores
                  memory            = var.memory_mb
                  vpc_id            = alicloud_vpc.main.id
                  vswitch_id        = alicloud_vswitch.a.id
                  security_group_id = alicloud_security_group.app.id
                  timezone          = "UTC"
                  tags              = local.tags
                  envs = jsonencode([
                @@envs@@
                  ])
                @@probe@@
                  depends_on = [alicloud_cr_ee_repo.@@n@@, alicloud_snat_entry.private]
                }
            """, n=n, name=u["name"], image=self.image(u), replicas="1" if worker else "var.replicas", envs=envs, probe=probe.rstrip("\n")))
        if self.public:
            first = self.public[0]
            rules = ""
            for u in self.public:
                n = u["name"].replace("-", "_")
                rules += T("""
                    rules {
                      app_id         = alicloud_sae_application.@@n@@[0].id
                      app_name       = alicloud_sae_application.@@n@@[0].app_name
                      container_port = @@port@@
                      domain         = "*"
                      path           = "@@path@@"
                    }
                """, n=n, port=u["port"] or 8080, path="/" if u is first else "/%s" % u["name"]) + "\n"
            parts.append(T("""
                resource "alicloud_alb_load_balancer" "main" {
                  vpc_id                 = alicloud_vpc.main.id
                  address_type           = "Internet"
                  address_allocated_mode = "Dynamic"
                  load_balancer_edition  = "Standard"
                  load_balancer_name     = local.name
                  tags                   = local.tags

                  load_balancer_billing_config {
                    pay_type = "PayAsYouGo"
                  }

                  zone_mappings {
                    vswitch_id = alicloud_vswitch.a.id
                    zone_id    = alicloud_vswitch.a.zone_id
                  }

                  zone_mappings {
                    vswitch_id = alicloud_vswitch.b.id
                    zone_id    = alicloud_vswitch.b.zone_id
                  }
                }

                resource "alicloud_sae_ingress" "main" {
                  count             = var.deploy_app ? 1 : 0
                  namespace_id      = alicloud_sae_namespace.main.id
                  slb_id            = alicloud_alb_load_balancer.main.id
                  load_balance_type = "alb"
                  listener_port     = 80
                  listener_protocol = "HTTP"

                @@rules@@
                }
            """, rules=rules.rstrip("\n")))
        self.files.add("terraform/sae.tf", tf(*parts))

    def functions(self):
        parts = []
        for u in self.units:
            n = u["name"].replace("-", "_")
            parts.append(T("""
                resource "alicloud_fcv3_function" "@@n@@" {
                  count         = var.deploy_app ? 1 : 0
                  function_name = "${local.name}-@@name@@"
                  handler       = "index.handler"
                  runtime       = "custom-container"
                  memory_size   = var.memory_mb
                  cpu           = var.cpu_millicores / 1000
                  timeout       = 120

                  custom_container_config {
                    image = @@image@@
                    port  = @@port@@
                  }

                  vpc_config {
                    vpc_id            = alicloud_vpc.main.id
                    vswitch_ids       = [alicloud_vswitch.a.id, alicloud_vswitch.b.id]
                    security_group_id = alicloud_security_group.app.id
                  }

                  environment_variables = @@envs@@

                  depends_on = [alicloud_cr_ee_repo.@@n@@]
                }

                resource "alicloud_fcv3_trigger" "@@n@@" {
                  count         = var.deploy_app ? 1 : 0
                  function_name = alicloud_fcv3_function.@@n@@[0].function_name
                  trigger_name  = "http"
                  trigger_type  = "http"
                  qualifier     = "LATEST"
                  trigger_config = jsonencode({
                    authType = "anonymous"
                    methods  = ["GET", "POST", "PUT", "DELETE"]
                  })
                }
            """, n=n, name=u["name"], image=self.image(u), port=u["port"] or 8080, envs=merged_env(self.m, '{ APP_ENV = "production" }')))
        self.files.add("terraform/functions.tf", tf(*parts))

    def data_services(self):
        parts = []
        if self.pg:
            parts.append(T("""
                resource "random_password" "database" {
                  length  = 32
                  special = false
                }

                resource "alicloud_db_instance" "main" {
                  engine                   = "PostgreSQL"
                  engine_version           = "16.0"
                  instance_type            = "pg.n2.2c.2m"
                  instance_storage         = 50
                  instance_name            = "${local.name}-db"
                  instance_charge_type     = "Postpaid"
                  vswitch_id               = alicloud_vswitch.a.id
                  zone_id                  = alicloud_vswitch.a.zone_id
                  zone_id_slave_a          = @@slave@@
                  security_ips             = ["10.70.0.0/16"]
                  security_group_ids       = [alicloud_security_group.app.id]
                  deletion_protection      = true
                  tags                     = local.tags
                }

                resource "alicloud_db_account" "app" {
                  db_instance_id   = alicloud_db_instance.main.id
                  account_name     = "app"
                  account_password_wo         = random_password.database.result
                  account_password_wo_version = 1
                  account_type     = "Super"
                }

                resource "alicloud_db_database" "app" {
                  instance_id    = alicloud_db_instance.main.id
                  data_base_name = "app"
                }
            """, slave="alicloud_vswitch.b.zone_id" if self.ha else "null"))
        if self.redis:
            parts.append(T("""
                resource "random_password" "redis" {
                  length  = 32
                  special = false
                }

                resource "alicloud_kvstore_instance" "main" {
                  db_instance_name = "${local.name}-redis"
                  instance_class   = "redis.master.small.default"
                  engine_version   = "7.0"
                  instance_type    = "Redis"
                  payment_type     = "PostPaid"
                  vswitch_id       = alicloud_vswitch.a.id
                  zone_id          = alicloud_vswitch.a.zone_id
                  security_ips     = ["10.70.0.0/16"]
                  password         = random_password.redis.result
                  ssl_enable       = "Enable"
                  tags             = local.tags
                }
            """))
        if self.mongo:
            parts.append(T("""
                resource "random_password" "mongodb" {
                  length  = 32
                  special = false
                }

                resource "alicloud_mongodb_instance" "main" {
                  name                = "${local.name}-mongo"
                  engine_version      = "7.0"
                  db_instance_class   = "mdb.shard.2x.xlarge.d"
                  db_instance_storage = 20
                  vswitch_id          = alicloud_vswitch.a.id
                  vpc_id              = alicloud_vpc.main.id
                  zone_id             = alicloud_vswitch.a.zone_id
                  security_ip_list    = ["10.70.0.0/16"]
                  account_password    = random_password.mongodb.result
                  replication_factor  = @@factor@@
                  tags                = local.tags
                }
            """, factor=3 if self.ha else 3))
        if self.kafka:
            parts.append(T("""
                resource "alicloud_alikafka_instance" "main" {
                  name           = "${local.name}-kafka"
                  partition_num  = 50
                  disk_type      = 1
                  disk_size      = 500
                  deploy_type    = 5
                  io_max_spec    = "alikafka.hw.2xlarge"
                  spec_type      = "normal"
                  service_version = "3.3.1"
                  vpc_id         = alicloud_vpc.main.id
                  vswitch_id     = alicloud_vswitch.a.id
                  zone_id        = alicloud_vswitch.a.zone_id
                  security_group = alicloud_security_group.app.id
                  tags           = local.tags
                }
            """))
        if self.amqp:
            parts.append(T("""
                resource "alicloud_amqp_instance" "main" {
                  instance_name    = "${local.name}-rabbitmq"
                  instance_type    = "professional"
                  payment_type     = "Subscription"
                  period           = 1
                  max_tps          = 1000
                  max_connections  = 1000
                  queue_capacity   = 50
                  storage_size     = 0
                  support_eip      = false
                  auto_renew       = true
                }
            """))
        if parts:
            self.files.add("terraform/data.tf", tf(*parts))

    def object_storage(self):
        self.files.add("terraform/storage.tf", tf(T("""
            resource "alicloud_oss_bucket" "assets" {
              bucket = "${local.name}-assets"
              tags   = local.tags

              versioning {
                status = "Enabled"
              }

              server_side_encryption_rule {
                sse_algorithm = "AES256"
              }
            }
        """)))

    def security(self):
        parts = [T("""
            resource "alicloud_kms_key" "main" {
              description            = "Key for ${local.name}"
              key_spec               = "Aliyun_AES_256"
              protection_level       = "SOFTWARE"
              pending_window_in_days = 30
              automatic_rotation     = "Enabled"
              rotation_interval      = "31536000s"
            }

            resource "alicloud_kms_secret" "app" {
              secret_name       = "${local.name}-app"
              secret_data       = "{}"
              version_id        = "v1"
              encryption_key_id = alicloud_kms_key.main.id
              description       = "Application settings. Replace the placeholder with the real JSON."

              lifecycle {
                ignore_changes = [secret_data, version_id]
              }
            }
        """)]
        if self.cdn:
            parts.append(T("""
                resource "alicloud_cdn_domain_new" "main" {
                  count       = var.domain == "" ? 0 : 1
                  domain_name = var.domain
                  cdn_type    = "web"
                  scope       = "overseas"

                  sources {
                    type     = "domain"
                    content  = alicloud_alb_load_balancer.main.dns_name
                    port     = 80
                    priority = 20
                    weight   = 15
                  }
                }
            """))
        self.files.add("terraform/security.tf", tf(*parts))

    def observability(self):
        self.files.add("terraform/observability.tf", tf(T("""
            resource "alicloud_log_project" "main" {
              project_name = "${local.name}-logs"
              description  = "Logs of ${local.name}"
            }

            resource "alicloud_log_store" "app" {
              project_name      = alicloud_log_project.main.project_name
              logstore_name     = "app"
              retention_period  = 30
              shard_count       = 2
              auto_split        = true
              max_split_shard_count = 8
            }
        """)))

    def outputs(self):
        parts = [T("""
            output "registry_address" {
              description = "Push your images to this address before turning deploy_app on."
              value       = "${alicloud_cr_ee_instance.main.instance_name}-registry.${var.region}.cr.aliyuncs.com/${alicloud_cr_ee_namespace.main.name}"
            }

            output "vpc_id" {
              description = "The network the services share."
              value       = alicloud_vpc.main.id
            }
        """)]
        if self.public and not self.serverless:
            parts.append(T("""
                output "load_balancer_dns" {
                  description = "Point your domain's CNAME record here."
                  value       = alicloud_alb_load_balancer.main.dns_name
                }
            """))
        if self.pg:
            parts.append(T("""
                output "database_host" {
                  description = "Private address of the PostgreSQL instance."
                  value       = alicloud_db_instance.main.connection_string
                }

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
            # Alibaba Cloud infrastructure for @@project@@

            Generated from plan @@fp@@. This creates a VPC with two vSwitches and a NAT gateway, Container Registry, KMS Secrets
            Manager, Simple Log Service, @@compute@@, and the data services the plan uses.

            ## Steps

            1. Create a RAM user with programmatic access and export `ALICLOUD_ACCESS_KEY` and `ALICLOUD_SECRET_KEY` (and `ALICLOUD_REGION`).
            2. Create an OSS bucket for state, copy `backend.hcl.example` to `backend.hcl` and fill it in.
            3. `terraform init -backend-config=backend.hcl`, then `terraform apply` with `deploy_app = false`.
            4. Build and push each image to your Container Registry namespace (`docker login` to the address in the `registry_address` output, then `docker push`).
            5. Set `deploy_app = true` and apply again.

            ## Notes

            - Instance classes, engine versions and zones differ by region. If an apply is refused, list the current ones in the console and change the value in the `.tf` file.
            - The load balancer listens on HTTP so you can test. Add a certificate and an HTTPS listener before real traffic.
            - Identity as a Service and a web application firewall have no Terraform resource in this design; add them in the console.
        """, project=m.project, fp=m.fingerprint,
                 compute=("Function Compute functions" if self.serverless else "Serverless App Engine applications behind an Application Load Balancer"))


def generate(model):
    return Alibaba(model).generate()
