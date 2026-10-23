#!/usr/bin/env python3
"""Terraform for Tencent Cloud, generated from the plan.

Server design: containers run on Tencent Kubernetes Engine (TKE) in a VPC with two subnets, with the managed databases
(TencentDB for PostgreSQL, Redis, MongoDB), CKafka, COS, Container Registry (TCR), Secrets Manager (SSM) and Cloud Log
Service (CLS) beside it, and the application runs from ../kubernetes. Serverless design: Serverless Cloud Function (SCF)
container-image functions behind API Gateway. Everything is created in a VPC; the image-dependent parts wait for
var.deploy_app.
"""
from iac_common import Files, T, add_app_env_files, fmt_hcl, merged_env, selfrun, settings_pairs, tf

DEFAULT_REGION = "ap-singapore"
NODE_TYPES = [(1.5, "S5.LARGE8"), (3.0, "S5.2XLARGE16"), (6.0, "S5.4XLARGE32"), (1e9, "S5.8XLARGE64")]


class Tencent:
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
        need = sum(float(model.sizing["unit_vcpu"]) for u in model.units if u["kind"] != "static") * model.replicas / 2
        self.node_type = next(t for limit, t in NODE_TYPES if need <= limit)
        self.region = model.region if model.region and model.region.startswith(("ap-", "na-", "eu-", "sa-")) else DEFAULT_REGION

    def generate(self):
        m = self.m
        self.versions()
        add_app_env_files(self.files, self.m)
        self.variables()
        self.network()
        self.registry()
        if self.serverless:
            self.functions()
        else:
            self.cluster()
        self.data_services()
        if self.storage:
            self.object_storage()
        self.security_and_logs()
        self.outputs()
        self.files.add("terraform/backend.tf", T("""
            terraform {
              backend "cos" {}
            }
        """))
        self.files.add("terraform/backend.hcl.example", T("""
            # Create a COS bucket for state, then fill this in.
            bucket = "REPLACE-with-your-state-bucket-appid"
            prefix = "@@project@@/production"
            region = "@@region@@"
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
                tencentcloud = {
                  source  = "tencentcloudstack/tencentcloud"
                  version = "~> 1.83"
                }
                random = {
                  source  = "hashicorp/random"
                  version = "~> 3.6"
                }
              }
            }

            provider "tencentcloud" {
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
              description = "Tencent Cloud region, for example ap-singapore, ap-guangzhou, eu-frankfurt, na-siliconvalley."
              type        = string
              default     = "@@region@@"
            }

            variable "kubernetes_version" {
              description = "TKE Kubernetes version. List the supported ones in the console."
              type        = string
              default     = "1.32.2"
            }

            variable "node_type" {
              description = "Instance type of each cluster node."
              type        = string
              default     = "@@node@@"
            }

            variable "node_min" {
              description = "Fewest nodes the autoscaler keeps."
              type        = number
              default     = @@min@@
            }

            variable "node_max" {
              description = "Most nodes the autoscaler adds."
              type        = number
              default     = @@max@@
            }

            variable "image_tag" {
              description = "Tag of the container images in the registry."
              type        = string
              default     = "0.1.0"
            }

            variable "deploy_app" {
              description = "Create the functions. Turn on only after the images are pushed to the registry."
              type        = bool
              default     = false
            }
        """, region=self.region, node=self.node_type, min=2 if self.ha else 1, max=6 if self.ha else 3)))

    def network(self):
        self.files.add("terraform/network.tf", tf(T("""
            locals {
              name = "${var.project_name}-${var.environment}"
            }

            data "tencentcloud_availability_zones_by_product" "cvm" {
              product = "cvm"
            }

            resource "tencentcloud_vpc" "main" {
              name       = local.name
              cidr_block = "10.80.0.0/16"
            }

            resource "tencentcloud_subnet" "a" {
              name              = "${local.name}-a"
              vpc_id            = tencentcloud_vpc.main.id
              cidr_block        = "10.80.1.0/24"
              availability_zone = data.tencentcloud_availability_zones_by_product.cvm.zones[0].name
            }

            resource "tencentcloud_subnet" "b" {
              name              = "${local.name}-b"
              vpc_id            = tencentcloud_vpc.main.id
              cidr_block        = "10.80.2.0/24"
              availability_zone = data.tencentcloud_availability_zones_by_product.cvm.zones[min(1, length(data.tencentcloud_availability_zones_by_product.cvm.zones) - 1)].name
            }

            resource "tencentcloud_security_group" "app" {
              name = "${local.name}-app"
            }

            resource "tencentcloud_eip" "nat" {
              name = "${local.name}-nat"
            }

            resource "tencentcloud_nat_gateway" "main" {
              name             = local.name
              vpc_id           = tencentcloud_vpc.main.id
              bandwidth        = 100
              max_concurrent   = 1000000
              assigned_eip_set = [tencentcloud_eip.nat.public_ip]
            }
        """)))

    def registry(self):
        self.files.add("terraform/registry.tf", tf(T("""
            resource "tencentcloud_tcr_instance" "main" {
              name          = replace(local.name, "-", "")
              instance_type = "basic"
            }

            resource "tencentcloud_tcr_namespace" "main" {
              instance_id = tencentcloud_tcr_instance.main.id
              name        = var.project_name
              is_public   = false
            }
        """)))

    def cluster(self):
        self.files.add("terraform/cluster.tf", tf(T("""
            resource "tencentcloud_kubernetes_cluster" "main" {
              cluster_name           = local.name
              cluster_version        = var.kubernetes_version
              cluster_deploy_type    = "MANAGED_CLUSTER"
              vpc_id                 = tencentcloud_vpc.main.id
              cluster_cidr           = "172.16.0.0/16"
              cluster_max_service_num = 1024
              cluster_internet       = false
              cluster_intranet       = true
              cluster_intranet_subnet_id = tencentcloud_subnet.a.id
              network_type           = "GR"
              container_runtime      = "containerd"
              deletion_protection    = true
              cluster_level          = "L5"
            }

            resource "tencentcloud_kubernetes_node_pool" "main" {
              name                 = "main"
              cluster_id           = tencentcloud_kubernetes_cluster.main.id
              vpc_id               = tencentcloud_vpc.main.id
              subnet_ids           = [tencentcloud_subnet.a.id, tencentcloud_subnet.b.id]
              min_size             = var.node_min
              max_size             = var.node_max
              desired_capacity     = var.node_min
              enable_auto_scale    = true
              multi_zone_subnet_policy = "EQUALITY"
              delete_keep_instance = false

              auto_scaling_config {
                instance_type              = var.node_type
                system_disk_type           = "CLOUD_PREMIUM"
                system_disk_size           = 50
                security_group_ids         = [tencentcloud_security_group.app.id]
                public_ip_assigned         = false
                enhanced_security_service  = true
                enhanced_monitor_service   = true

                data_disk {
                  disk_type = "CLOUD_PREMIUM"
                  disk_size = 100
                  encrypt   = true
                }
              }

              node_config {
                is_schedule = true
              }
            }
        """)))

    def functions(self):
        parts = []
        for u in self.units:
            n = u["name"].replace("-", "_")
            parts.append(T("""
                resource "tencentcloud_scf_function" "@@n@@" {
                  count     = var.deploy_app ? 1 : 0
                  name      = "${local.name}-@@name@@"
                  func_type = "HTTP"
                  mem_size  = @@mem@@
                  timeout   = 60
                  vpc_id    = tencentcloud_vpc.main.id
                  subnet_id = tencentcloud_subnet.a.id

                  image_config {
                    image_type = "personal"
                    image_uri  = "${tencentcloud_tcr_instance.main.public_domain}/${tencentcloud_tcr_namespace.main.name}/@@name@@:${var.image_tag}"
                    image_port = @@port@@
                  }

                  environment = @@envs@@
                }
            """, n=n, name=u["name"], mem=int(max(128, min(self.m.sizing["unit_memory_gb"] * 1024, 3072)) // 64 * 64), port=u["port"] or 8080,
                         envs=merged_env(self.m, '{ APP_ENV = "production", PORT = "%d" }' % (u["port"] or 8080))))
        if self.public:
            parts.append(T("""
                resource "tencentcloud_api_gateway_service" "main" {
                  service_name = local.name
                  protocol     = "http&https"
                  net_type     = ["OUTER"]
                  ip_version   = "IPv4"
                }
            """))
            for u in self.public:
                n = u["name"].replace("-", "_")
                path = "/" if u is self.public[0] else "/%s" % u["name"]
                parts.append(T("""
                    resource "tencentcloud_api_gateway_api" "@@n@@" {
                      count                         = var.deploy_app ? 1 : 0
                      service_id                    = tencentcloud_api_gateway_service.main.id
                      api_name                      = "@@name@@"
                      api_type                      = "NORMAL"
                      auth_type                     = "NONE"
                      protocol                      = "HTTP"
                      request_config_method         = "ANY"
                      request_config_path           = "@@path@@"
                      service_config_type           = "SCF"
                      service_config_scf_function_name = tencentcloud_scf_function.@@n@@[0].name
                      service_config_scf_function_namespace = "default"
                      service_config_scf_function_qualifier = "$LATEST"
                      service_config_scf_is_integrated_response = true
                    }
                """, n=n, name=u["name"], path=path))
        self.files.add("terraform/functions.tf", tf(*parts))

    def data_services(self):
        parts = []
        zone = "data.tencentcloud_availability_zones_by_product.cvm.zones[0].name"
        if self.pg:
            parts.append(T("""
                resource "random_password" "database" {
                  length  = 32
                  special = false
                }

                resource "tencentcloud_postgresql_instance" "main" {
                  name              = "${local.name}-db"
                  availability_zone = @@zone@@
                  vpc_id            = tencentcloud_vpc.main.id
                  subnet_id         = tencentcloud_subnet.a.id
                  security_groups   = [tencentcloud_security_group.app.id]
                  charge_type       = "POSTPAID_BY_HOUR"
                  engine_version    = "16.4"
                  memory            = 4
                  storage           = 50
                  root_user         = "app"
                  root_password     = random_password.database.result
                  delete_protection = true

                  dynamic "db_node_set" {
                    for_each = @@nodes@@
                    content {
                      role = db_node_set.value
                      zone = @@zone@@
                    }
                  }
                }
            """, zone=zone, nodes='["Primary", "Standby"]' if self.ha else '["Primary"]'))
        if self.redis:
            parts.append(T("""
                resource "random_password" "redis" {
                  length  = 32
                  special = false
                }

                resource "tencentcloud_redis_instance" "main" {
                  name              = "${local.name}-redis"
                  availability_zone = @@zone@@
                  type_id           = 8
                  mem_size          = 1024
                  redis_replicas_num = @@replicas@@
                  vpc_id            = tencentcloud_vpc.main.id
                  subnet_id         = tencentcloud_subnet.a.id
                  security_groups   = [tencentcloud_security_group.app.id]
                  password          = random_password.redis.result
                  charge_type       = "POSTPAID"
                }
            """, zone=zone, replicas=2 if self.ha else 1))
        if self.mongo:
            parts.append(T("""
                resource "random_password" "mongodb" {
                  length  = 32
                  special = false
                }

                resource "tencentcloud_mongodb_instance" "main" {
                  instance_name   = "${local.name}-mongo"
                  available_zone  = @@zone@@
                  engine_version  = "MONGO_70_WT"
                  machine_type    = "HIO10G"
                  memory          = 4
                  volume          = 100
                  vpc_id          = tencentcloud_vpc.main.id
                  subnet_id       = tencentcloud_subnet.a.id
                  security_groups = [tencentcloud_security_group.app.id]
                  password        = random_password.mongodb.result
                  charge_type     = "POSTPAID_BY_HOUR"
                }
            """, zone=zone))
        if self.kafka:
            parts.append(T("""
                resource "tencentcloud_ckafka_instance" "main" {
                  instance_name = "${local.name}-kafka"
                  zone_id       = data.tencentcloud_availability_zones_by_product.cvm.zones[0].id
                  vpc_id        = tencentcloud_vpc.main.id
                  subnet_id     = tencentcloud_subnet.a.id
                  kafka_version = "2.8.1"
                  band_width    = 40
                  disk_size     = 500
                  disk_type     = "CLOUD_BASIC"
                  partition     = 800
                  specifications_type = "standard"
                  instance_type = 2
                  charge_type   = "POSTPAID_BY_HOUR"

                  config {
                    auto_create_topic_enable   = false
                    default_num_partitions     = 3
                    default_replication_factor = 2
                  }
                }
            """))
        if parts:
            self.files.add("terraform/data.tf", tf(*parts))

    def object_storage(self):
        self.files.add("terraform/storage.tf", tf(T("""
            data "tencentcloud_user_info" "me" {}

            resource "tencentcloud_cos_bucket" "assets" {
              bucket                = "${local.name}-assets-${data.tencentcloud_user_info.me.app_id}"
              acl                   = "private"
              versioning_enable     = true
              encryption_algorithm  = "AES256"
              force_clean           = false
            }
        """)))

    def security_and_logs(self):
        self.files.add("terraform/security.tf", tf(T("""
            resource "tencentcloud_ssm_secret" "app" {
              secret_name = "${local.name}-app"
              description = "Application settings. Add the real JSON as a new version."
            }

            resource "tencentcloud_ssm_secret_version" "initial" {
              secret_name   = tencentcloud_ssm_secret.app.secret_name
              version_id    = "v1"
              secret_string = "{}"

              lifecycle {
                ignore_changes = [secret_string]
              }
            }

            resource "tencentcloud_cls_logset" "main" {
              logset_name = local.name
            }

            resource "tencentcloud_cls_topic" "app" {
              logset_id    = tencentcloud_cls_logset.main.id
              topic_name   = "app"
              period       = 30
              storage_type = "hot"
            }
        """)))

    def outputs(self):
        parts = [T("""
            output "registry_address" {
              description = "Push your images here before turning deploy_app on."
              value       = "${tencentcloud_tcr_instance.main.public_domain}/${tencentcloud_tcr_namespace.main.name}"
            }

            output "vpc_id" {
              description = "The network the services share."
              value       = tencentcloud_vpc.main.id
            }
        """)]
        if not self.serverless:
            parts.append(T("""
                output "kubeconfig" {
                  description = "Save it with: terraform output -raw kubeconfig > kubeconfig.yaml"
                  value       = tencentcloud_kubernetes_cluster.main.kube_config_intranet
                  sensitive   = true
                }
            """))
        if self.pg:
            parts.append(T("""
                output "database_host" {
                  description = "Private address of the PostgreSQL instance."
                  value       = tencentcloud_postgresql_instance.main.private_access_ip
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
            # Tencent Cloud infrastructure for @@project@@

            Generated from plan @@fp@@. This creates a VPC with two subnets and a NAT gateway, Container Registry, Secrets Manager,
            Cloud Log Service, @@compute@@, and the data services the plan uses.

            ## Steps

            1. Create an API key (CAM, Access Keys) and export `TENCENTCLOUD_SECRET_ID` and `TENCENTCLOUD_SECRET_KEY`.
            2. Create a COS bucket for state, copy `backend.hcl.example` to `backend.hcl` and fill it in.
            3. `terraform init -backend-config=backend.hcl`, then `terraform apply` with `deploy_app = false`.
            4. Build and push each image to `terraform output registry_address` (`docker login` to the registry domain, then `docker push`).
            @@steps@@

            ## Notes

            - The TKE cluster API is on the private network only. Reach it from a machine in the VPC or add a public endpoint on purpose.
            - Kubernetes versions, instance types, engine versions and zones vary by region. If an apply is refused, list the current ones in the console and change the value.
            - The plan lists an identity service as a gap; it is not created here.
        """, project=m.project, fp=m.fingerprint,
                 compute=("SCF container functions behind API Gateway" if self.serverless else "a TKE cluster"),
                 steps=("5. Set `deploy_app = true` and apply again." if self.serverless else
                        "5. `terraform output -raw kubeconfig > kubeconfig.yaml`, then follow `../kubernetes/README.md`."))


def generate(model):
    import iac_static
    if iac_static.has_static_only(model):  # a site of only static files needs a bucket and a CDN, not a network and a container service
        import iac_static_more
        return iac_static_more.tencent(model)
    return Tencent(model).generate()
