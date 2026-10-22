#!/usr/bin/env python3
"""Terraform for Huawei Cloud, generated from the plan.

Server design: containers run on Cloud Container Instance (CCI 2.0) deployments in a VPC with a NAT gateway for outbound
calls, each web unit behind a CCI service. Serverless design: FunctionGraph custom-image functions. Data services are RDS
for PostgreSQL, DCS for Redis, DDS for MongoDB, DMS for Kafka and RabbitMQ; files go to OBS, images to SWR, secrets to CSMS,
logs to LTS. Image-dependent parts wait for var.deploy_app. Flavor and storage codes change by region, so they are
variables with common defaults and the README says how to list the current ones.
"""
from iac_common import Files, T, fmt_hcl, selfrun, tf

DEFAULT_REGION = "ap-southeast-3"


class Huawei:
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
        self.region = model.region if model.region and model.region.startswith(("cn-", "ap-", "af-", "sa-", "na-", "la-", "tr-", "me-")) else DEFAULT_REGION

    def generate(self):
        m = self.m
        self.versions()
        self.variables()
        self.network()
        self.registry()
        if self.serverless:
            self.functions()
        else:
            self.cci()
        self.data_services()
        if self.storage:
            self.object_storage()
        self.security_and_logs()
        self.outputs()
        self.files.add("terraform/backend.tf", T("""
            terraform {
              backend "s3" {}
            }
        """))
        self.files.add("terraform/backend.hcl.example", T("""
            # Huawei Cloud OBS speaks the S3 protocol. Create a bucket and access keys, then fill this in.
            bucket                      = "REPLACE-with-your-state-bucket"
            key                         = "@@project@@/production.tfstate"
            region                      = "@@region@@"
            endpoints                   = { s3 = "https://obs.@@region@@.myhuaweicloud.com" }
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
                huaweicloud = {
                  source  = "huaweicloud/huaweicloud"
                  version = "~> 1.99"
                }
                random = {
                  source  = "hashicorp/random"
                  version = "~> 3.6"
                }
              }
            }

            provider "huaweicloud" {
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
              description = "Huawei Cloud region, for example ap-southeast-3, eu-west-0, cn-north-4."
              type        = string
              default     = "@@region@@"
            }

            variable "image_tag" {
              description = "Tag of the container images in the registry."
              type        = string
              default     = "latest"
            }

            variable "deploy_app" {
              description = "Create the running deployments or functions. Turn on only after the images are pushed."
              type        = bool
              default     = false
            }

            variable "replicas" {
              description = "How many copies of each web unit."
              type        = number
              default     = @@replicas@@
            }

            variable "cpu" {
              description = "CPU limit of each container, for example 0.5, 1 or 2."
              type        = string
              default     = "@@cpu@@"
            }

            variable "memory" {
              description = "Memory limit of each container, for example 1Gi."
              type        = string
              default     = "@@memory@@Gi"
            }
        """, region=self.region, replicas=self.m.replicas if self.ha else 1, cpu=self.m.sizing["unit_vcpu"],
                 memory=max(1, int(round(float(self.m.sizing["unit_memory_gb"])))))))

    def network(self):
        self.files.add("terraform/network.tf", tf(T("""
            locals {
              name = "${var.project_name}-${var.environment}"
            }

            data "huaweicloud_availability_zones" "all" {}

            resource "huaweicloud_vpc" "main" {
              name = local.name
              cidr = "10.90.0.0/16"
            }

            resource "huaweicloud_vpc_subnet" "main" {
              name       = "${local.name}-main"
              vpc_id     = huaweicloud_vpc.main.id
              cidr       = "10.90.1.0/24"
              gateway_ip = "10.90.1.1"
            }

            resource "huaweicloud_networking_secgroup" "app" {
              name = "${local.name}-app"
            }

            resource "huaweicloud_vpc_eip" "nat" {
              name = "${local.name}-nat"

              publicip {
                type = "5_bgp"
              }

              bandwidth {
                name        = "${local.name}-nat"
                size        = 100
                share_type  = "PER"
                charge_mode = "traffic"
              }
            }

            resource "huaweicloud_nat_gateway" "main" {
              name      = local.name
              spec      = "1"
              vpc_id    = huaweicloud_vpc.main.id
              subnet_id = huaweicloud_vpc_subnet.main.id
            }

            resource "huaweicloud_nat_snat_rule" "main" {
              nat_gateway_id = huaweicloud_nat_gateway.main.id
              floating_ip_id = huaweicloud_vpc_eip.nat.id
              subnet_id      = huaweicloud_vpc_subnet.main.id
            }
        """)))

    def registry(self):
        parts = [T("""
            resource "huaweicloud_swr_organization" "main" {
              name = replace(local.name, "-", "")
            }
        """)]
        for u in self.units:
            parts.append(T("""
                resource "huaweicloud_swr_repository" "@@n@@" {
                  organization = huaweicloud_swr_organization.main.name
                  name         = "@@name@@"
                  category     = "app_server"
                  is_public    = false
                }
            """, n=u["name"].replace("-", "_"), name=u["name"]))
        self.files.add("terraform/registry.tf", tf(*parts))

    def image(self, unit):
        return '"swr.${var.region}.myhuaweicloud.com/${huaweicloud_swr_organization.main.name}/%s:${var.image_tag}"' % unit["name"]

    def env_pairs(self):
        out = [("APP_ENV", '"production"')]
        if self.pg:
            out += [("DB_HOST", "huaweicloud_rds_instance.main.private_ips[0]"), ("DB_PORT", '"5432"'), ("DB_NAME", '"app"'), ("DB_USER", '"root"')]
        if self.redis:
            out += [("REDIS_HOST", "huaweicloud_dcs_instance.main.private_ip"), ("REDIS_PORT", '"6379"')]
        if self.storage:
            out += [("OBS_BUCKET", "huaweicloud_obs_bucket.assets.bucket"), ("OBS_ENDPOINT", '"obs.${var.region}.myhuaweicloud.com"')]
        return out

    def cci(self):
        parts = [T("""
            resource "huaweicloud_cciv2_namespace" "main" {
              name = local.name
            }

            resource "huaweicloud_cciv2_network" "main" {
              namespace          = huaweicloud_cciv2_namespace.main.name
              name               = "network"
              security_group_ids = [huaweicloud_networking_secgroup.app.id]

              subnets {
                subnet_id = huaweicloud_vpc_subnet.main.ipv4_subnet_id
              }
            }
        """)]
        env = "\n".join(T("""
            env {
              name  = "@@k@@"
              value = @@v@@
            }
        """, k=k, v=v if v.startswith('"') else "tostring(%s)" % v) for k, v in self.env_pairs())
        for u in self.units:
            n, worker, port = u["name"].replace("-", "_"), u["kind"] == "worker", u["port"] or 8080
            probe = ""
            ports = ""
            if not worker:
                ports = "ports {\n  container_port = %d\n  name           = \"http\"\n}\n" % port
                probe = T("""
                    liveness_probe {
                      initial_delay_seconds = 30
                      period_seconds        = 15
                      failure_threshold     = 3

                      http_get {
                        path   = "@@path@@"
                        port   = @@port@@
                        scheme = "HTTP"
                      }
                    }
                """, path=u["health_path"] or "/", port=port)
            parts.append(T("""
                resource "huaweicloud_cciv2_deployment" "@@n@@" {
                  count     = var.deploy_app ? 1 : 0
                  namespace = huaweicloud_cciv2_namespace.main.name
                  name      = "@@name@@"
                  replicas  = @@replicas@@

                  selector {
                    match_labels = {
                      app = "@@name@@"
                    }
                  }

                  template {
                    metadata {
                      labels = {
                        app = "@@name@@"
                      }
                    }

                    spec {
                      containers {
                        name  = "@@name@@"
                        image = @@image@@
                @@ports@@
                @@env@@
                        resources {
                          requests = {
                            cpu    = var.cpu
                            memory = var.memory
                          }
                          limits = {
                            cpu    = var.cpu
                            memory = var.memory
                          }
                        }

                        security_context {
                          run_as_non_root           = true
                          read_only_root_file_system = true
                        }
                @@probe@@
                      }
                    }
                  }

                  depends_on = [huaweicloud_cciv2_network.main, huaweicloud_swr_repository.@@n@@]
                }
            """, n=n, name=u["name"], replicas="1" if worker else "var.replicas", image=self.image(u), ports=ports.rstrip("\n"),
                          env=env, probe=probe.rstrip("\n")))
            if not worker and u in self.public:
                parts.append(T("""
                    resource "huaweicloud_cciv2_service" "@@n@@" {
                      count     = var.deploy_app ? 1 : 0
                      namespace = huaweicloud_cciv2_namespace.main.name
                      name      = "@@name@@"
                      type      = "ClusterIP"

                      selector = {
                        app = "@@name@@"
                      }

                      ports {
                        name        = "http"
                        port        = 80
                        target_port = "@@port@@"
                        protocol    = "TCP"
                      }
                    }
                """, n=n, name=u["name"], port=port))
        self.files.add("terraform/cci.tf", tf(*parts))

    def functions(self):
        parts = []
        for u in self.units:
            n = u["name"].replace("-", "_")
            parts.append(T("""
                resource "huaweicloud_fgs_function" "@@n@@" {
                  count       = var.deploy_app ? 1 : 0
                  name        = "${local.name}-@@name@@"
                  app         = "default"
                  memory_size = 512
                  timeout     = 60
                  runtime     = "Custom Image"
                  vpc_id      = huaweicloud_vpc.main.id
                  network_id  = huaweicloud_vpc_subnet.main.id

                  custom_image {
                    url = @@image@@
                  }

                  user_data = jsonencode({
                    APP_ENV = "production"
                    PORT    = "@@port@@"
                  })

                  depends_on = [huaweicloud_swr_repository.@@n@@]
                }
            """, n=n, name=u["name"], image=self.image(u), port=u["port"] or 8080))
        self.files.add("terraform/functions.tf", tf(*parts))

    def data_services(self):
        parts = []
        az = "data.huaweicloud_availability_zones.all.names[0]"
        az2 = "data.huaweicloud_availability_zones.all.names[min(1, length(data.huaweicloud_availability_zones.all.names) - 1)]"
        common = """  vpc_id            = huaweicloud_vpc.main.id
  subnet_id         = huaweicloud_vpc_subnet.main.id
  security_group_id = huaweicloud_networking_secgroup.app.id
"""
        if self.pg:
            parts.append(T("""
                resource "random_password" "database" {
                  length  = 32
                  special = false
                }

                resource "huaweicloud_rds_instance" "main" {
                  name                = "${local.name}-db"
                  flavor              = var.database_flavor
                  availability_zone   = @@az@@
                  vpc_id              = huaweicloud_vpc.main.id
                  subnet_id           = huaweicloud_vpc_subnet.main.id
                  security_group_id   = huaweicloud_networking_secgroup.app.id
                  ssl_enable          = true
                  @@ha@@

                  db {
                    type     = "PostgreSQL"
                    version  = "16"
                    password = random_password.database.result
                    port     = 5432
                  }

                  volume {
                    type = "CLOUDSSD"
                    size = 50
                  }

                  backup_strategy {
                    start_time = "02:00-03:00"
                    keep_days  = 14
                  }

                  lifecycle {
                    prevent_destroy = true
                  }
                }

                variable "database_flavor" {
                  description = "RDS flavor code. List them with the huaweicloud_rds_flavors data source or the console."
                  type        = string
                  default     = "@@flavor@@"
                }
            """, az="[%s, %s]" % (az, az2) if self.ha else "[%s]" % az, ha='ha_replication_mode = "async"' if self.ha else "",
                         flavor="rds.pg.n1.large.2.ha" if self.ha else "rds.pg.n1.large.2"))
        if self.redis:
            parts.append(T("""
                resource "random_password" "redis" {
                  length  = 32
                  special = false
                }

                resource "huaweicloud_dcs_instance" "main" {
                  name               = "${local.name}-redis"
                  engine             = "Redis"
                  engine_version     = "6.0"
                  capacity           = 2
                  flavor             = "redis.ha.xu1.large.r2.2"
                  availability_zones = [@@az@@]
                  vpc_id             = huaweicloud_vpc.main.id
                  subnet_id          = huaweicloud_vpc_subnet.main.id
                  password           = random_password.redis.result
                }
            """, az=az))
        if self.mongo:
            parts.append(T("""
                resource "random_password" "mongodb" {
                  length  = 32
                  special = false
                }

                resource "huaweicloud_dds_instance" "main" {
                  name              = "${local.name}-mongo"
                  mode              = "ReplicaSet"
                  availability_zone = @@az@@
                  vpc_id            = huaweicloud_vpc.main.id
                  subnet_id         = huaweicloud_vpc_subnet.main.id
                  security_group_id = huaweicloud_networking_secgroup.app.id
                  password          = random_password.mongodb.result
                  ssl               = true

                  datastore {
                    type    = "DDS-Community"
                    version = "4.0"
                  }

                  flavor {
                    type      = "replica"
                    num       = 3
                    storage   = "ULTRAHIGH"
                    size      = 30
                    spec_code = "dds.mongodb.s6.large.4.repset"
                  }
                }
            """, az=az))
        if self.kafka:
            parts.append(T("""
                resource "huaweicloud_dms_kafka_instance" "main" {
                  name               = "${local.name}-kafka"
                  vpc_id             = huaweicloud_vpc.main.id
                  network_id         = huaweicloud_vpc_subnet.main.id
                  security_group_id  = huaweicloud_networking_secgroup.app.id
                  availability_zones = [@@az@@]
                  engine_version     = "3.x"
                  flavor_id          = "c6.2u4g.cluster"
                  storage_spec_code  = "dms.physical.storage.high.v2"
                  storage_space      = 300
                  broker_num         = 3
                  access_user        = "app"
                  password           = random_password.kafka.result
                  ssl_enable         = true
                }

                resource "random_password" "kafka" {
                  length  = 32
                  special = false
                }
            """, az=az))
        if self.amqp:
            parts.append(T("""
                resource "huaweicloud_dms_rabbitmq_instance" "main" {
                  name               = "${local.name}-rabbitmq"
                  vpc_id             = huaweicloud_vpc.main.id
                  network_id         = huaweicloud_vpc_subnet.main.id
                  security_group_id  = huaweicloud_networking_secgroup.app.id
                  availability_zones = [@@az@@]
                  engine_version     = "3.8.35"
                  flavor_id          = "c6.2u4g.cluster"
                  storage_spec_code  = "dms.physical.storage.ultra.v2"
                  storage_space      = 300
                  broker_num         = 3
                  access_user        = "app"
                  password           = random_password.rabbitmq.result
                  ssl_enable         = true
                }

                resource "random_password" "rabbitmq" {
                  length  = 32
                  special = false
                }
            """, az=az))
        if parts:
            self.files.add("terraform/data.tf", tf(*parts))

    def object_storage(self):
        self.files.add("terraform/storage.tf", tf(T("""
            resource "huaweicloud_obs_bucket" "assets" {
              bucket        = "${local.name}-assets"
              storage_class = "STANDARD"
              acl           = "private"
              versioning    = true
              encryption    = true
              sse_algorithm = "kms"
            }
        """)))

    def security_and_logs(self):
        self.files.add("terraform/security.tf", tf(T("""
            resource "huaweicloud_csms_secret" "app" {
              name        = "${local.name}-app"
              secret_text = "{}"

              lifecycle {
                ignore_changes = [secret_text]
              }
            }

            resource "huaweicloud_lts_group" "main" {
              group_name  = local.name
              ttl_in_days = 30
            }

            resource "huaweicloud_lts_stream" "app" {
              group_id    = huaweicloud_lts_group.main.id
              stream_name = "app"
            }
        """)))

    def outputs(self):
        parts = [T("""
            output "registry_address" {
              description = "Push your images here before turning deploy_app on."
              value       = "swr.${var.region}.myhuaweicloud.com/${huaweicloud_swr_organization.main.name}"
            }

            output "vpc_id" {
              description = "The network the services share."
              value       = huaweicloud_vpc.main.id
            }
        """)]
        if self.pg:
            parts.append(T("""
                output "database_host" {
                  description = "Private address of the PostgreSQL instance."
                  value       = huaweicloud_rds_instance.main.private_ips[0]
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
            # Huawei Cloud infrastructure for @@project@@

            Generated from plan @@fp@@. This creates a VPC with a NAT gateway, SWR image registry, CSMS secret store, LTS logs,
            @@compute@@, and the data services the plan uses.

            ## Steps

            1. Create an access key (My Credentials) and export `HW_ACCESS_KEY`, `HW_SECRET_KEY` (and `HW_REGION_NAME`).
            2. Create an OBS bucket for state, copy `backend.hcl.example` to `backend.hcl`, fill it in, and export its keys as `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`.
            3. `terraform init -backend-config=backend.hcl`, then `terraform apply` with `deploy_app = false`.
            4. Build and push each image to `terraform output registry_address` (`docker login swr.<region>.myhuaweicloud.com`, then `docker push`).
            5. Set `deploy_app = true` and apply again.

            ## Notes

            - Flavor and storage codes for RDS, DCS, DDS and DMS vary by region and engine version. If an apply is refused, list the current codes in the console or with the matching `huaweicloud_*_flavors` data source and change the value.
            - CCI services are cluster-internal. Put an Elastic Load Balance listener (public) in front of the service, or use API Gateway, before real traffic.
            - The plan lists an identity service as a gap; it is not created here.
        """, project=m.project, fp=m.fingerprint,
                 compute=("FunctionGraph custom-image functions" if self.serverless else "Cloud Container Instance (CCI 2.0) deployments"))


def generate(model):
    return Huawei(model).generate()
