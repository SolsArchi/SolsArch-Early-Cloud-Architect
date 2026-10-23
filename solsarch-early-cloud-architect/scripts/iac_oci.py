#!/usr/bin/env python3
"""Terraform for Oracle Cloud Infrastructure (OCI), generated from the plan.

Server design: containers run on Container Instances in a private subnet behind a public Load Balancer with a Web
Application Firewall, pulling images from Container Registry (OCIR). Serverless design: OCI Functions behind API Gateway.
Both use a VCN with public and private subnets, a NAT gateway for outbound calls and a service gateway for Oracle services,
OCI Database with PostgreSQL, OCI Cache with Redis, Object Storage, a Vault with a key and a secret for application settings,
Streaming (Kafka compatible), and a log group. Instances are created in a second step (var.deploy_app), after the images
exist in the registry.
"""
from iac_common import Files, T, add_app_env_files, fmt_hcl, merged_env, selfrun, settings_pairs, tf


class Oci:
    def __init__(self, model):
        self.m = model
        self.files = Files(model.fingerprint)
        self.public = [u for u in model.public_units() if u["kind"] != "static"]
        self.units = [u for u in model.units if u["kind"] != "static"]
        self.serverless = model.mode == "serverless"
        self.ha = not model.single_az
        self.pg = model.db_key == "database" and not selfrun(model, "database")
        self.redis = model.has("cache") and not selfrun(model, "cache")
        self.kafka = model.has("queue_kafka") and not selfrun(model, "queue_kafka")
        self.storage = model.has("storage")
        self.vault = model.has("secrets")
        self.waf = model.has("edge") and not self.serverless
        self.cpu = max(1, int(round(float(model.sizing["unit_vcpu"]))))
        self.memory = max(float(model.sizing["unit_memory_gb"]), self.cpu * 2.0)

    # ------------------------------------------------------------------ main
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
            self.containers()
            self.load_balancer()
        self.data_services()
        if self.storage:
            self.object_storage()
        self.security()
        self.observability()
        self.outputs()
        self.files.add("terraform/backend.tf", T("""
            terraform {
              backend "s3" {}
            }
        """))
        self.files.add("terraform/backend.hcl.example", T("""
            # OCI Object Storage has an S3-compatible API. Create a bucket and a customer secret key, then fill this in.
            bucket                      = "REPLACE-with-your-state-bucket"
            key                         = "@@project@@/production.tfstate"
            region                      = "us-ashburn-1"
            endpoints                   = { s3 = "https://REPLACE-with-your-namespace.compat.objectstorage.us-ashburn-1.oraclecloud.com" }
            skip_credentials_validation = true
            skip_region_validation      = true
            skip_requesting_account_id  = true
            skip_metadata_api_check     = true
            skip_s3_checksum            = true
            use_path_style              = true
        """, project=m.project))
        self.files.add("terraform/terraform.tfvars.example", fmt_hcl(
            'project_name     = "%s"\nenvironment      = "production"\ntenancy_ocid     = "REPLACE-with-your-tenancy-ocid"\ncompartment_ocid = "REPLACE-with-your-compartment-ocid"\nregion           = "us-ashburn-1"\nregion_key       = "iad"\ndeploy_app       = false\n' % m.project))
        self.files.add("terraform/README.md", self.readme())
        return self.files

    def versions(self):
        self.files.add("terraform/versions.tf", tf(T("""
            terraform {
              required_version = ">= 1.9.0"

              required_providers {
                oci = {
                  source  = "oracle/oci"
                  version = "~> 9.0"
                }
                random = {
                  source  = "hashicorp/random"
                  version = "~> 3.6"
                }
              }
            }

            provider "oci" {
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

            variable "tenancy_ocid" {
              description = "The OCID of your tenancy."
              type        = string
            }

            variable "compartment_ocid" {
              description = "The OCID of the compartment that holds everything this creates."
              type        = string
            }

            variable "region" {
              description = "OCI region, for example us-ashburn-1 or eu-frankfurt-1."
              type        = string
              default     = "us-ashburn-1"
            }

            variable "region_key" {
              description = "Short region key used in the registry address (iad, fra, lhr, phx)."
              type        = string
              default     = "iad"
            }

            variable "image_tag" {
              description = "Tag of the container images in the registry."
              type        = string
              default     = "latest"
            }

            variable "deploy_app" {
              description = "Create the running containers or functions. Turn on only after the images are pushed."
              type        = bool
              default     = false
            }

            variable "replicas" {
              description = "How many copies of each web container."
              type        = number
              default     = @@replicas@@
            }

            variable "ocpus" {
              description = "OCPUs for each container."
              type        = number
              default     = @@cpu@@
            }

            variable "memory_gbs" {
              description = "Memory in GB for each container."
              type        = number
              default     = @@memory@@
            }
        """, replicas=self.m.replicas if self.ha else 1, cpu=self.cpu, memory=int(self.memory))))

    def network(self):
        self.files.add("terraform/network.tf", tf(T("""
            locals {
              name = "${var.project_name}-${var.environment}"
              tags = { project = var.project_name, environment = var.environment, managed-by = "solsarch" }
            }

            data "oci_identity_availability_domains" "all" {
              compartment_id = var.tenancy_ocid
            }

            data "oci_objectstorage_namespace" "ns" {
              compartment_id = var.tenancy_ocid
            }

            data "oci_core_services" "all" {
              filter {
                name   = "name"
                values = ["All .* Services In Oracle Services Network"]
                regex  = true
              }
            }

            resource "oci_core_vcn" "main" {
              compartment_id = var.compartment_ocid
              display_name   = local.name
              cidr_blocks    = ["10.60.0.0/16"]
              dns_label      = "app"
              freeform_tags  = local.tags
            }

            resource "oci_core_internet_gateway" "main" {
              compartment_id = var.compartment_ocid
              vcn_id         = oci_core_vcn.main.id
              display_name   = "${local.name}-igw"
              freeform_tags  = local.tags
            }

            resource "oci_core_nat_gateway" "main" {
              compartment_id = var.compartment_ocid
              vcn_id         = oci_core_vcn.main.id
              display_name   = "${local.name}-nat"
              freeform_tags  = local.tags
            }

            resource "oci_core_service_gateway" "main" {
              compartment_id = var.compartment_ocid
              vcn_id         = oci_core_vcn.main.id
              display_name   = "${local.name}-sgw"

              services {
                service_id = data.oci_core_services.all.services[0].id
              }
            }

            resource "oci_core_route_table" "public" {
              compartment_id = var.compartment_ocid
              vcn_id         = oci_core_vcn.main.id
              display_name   = "${local.name}-public"
              freeform_tags  = local.tags

              route_rules {
                destination       = "0.0.0.0/0"
                destination_type  = "CIDR_BLOCK"
                network_entity_id = oci_core_internet_gateway.main.id
              }
            }

            resource "oci_core_route_table" "private" {
              compartment_id = var.compartment_ocid
              vcn_id         = oci_core_vcn.main.id
              display_name   = "${local.name}-private"
              freeform_tags  = local.tags

              route_rules {
                destination       = "0.0.0.0/0"
                destination_type  = "CIDR_BLOCK"
                network_entity_id = oci_core_nat_gateway.main.id
              }

              route_rules {
                destination       = data.oci_core_services.all.services[0].cidr_block
                destination_type  = "SERVICE_CIDR_BLOCK"
                network_entity_id = oci_core_service_gateway.main.id
              }
            }

            resource "oci_core_subnet" "public" {
              compartment_id             = var.compartment_ocid
              vcn_id                     = oci_core_vcn.main.id
              display_name               = "${local.name}-public"
              cidr_block                 = "10.60.0.0/24"
              route_table_id             = oci_core_route_table.public.id
              prohibit_public_ip_on_vnic = false
              freeform_tags              = local.tags
            }

            resource "oci_core_subnet" "private" {
              compartment_id             = var.compartment_ocid
              vcn_id                     = oci_core_vcn.main.id
              display_name               = "${local.name}-private"
              cidr_block                 = "10.60.1.0/24"
              route_table_id             = oci_core_route_table.private.id
              prohibit_public_ip_on_vnic = true
              freeform_tags              = local.tags
            }

            resource "oci_core_network_security_group" "lb" {
              compartment_id = var.compartment_ocid
              vcn_id         = oci_core_vcn.main.id
              display_name   = "${local.name}-lb"
              freeform_tags  = local.tags
            }

            resource "oci_core_network_security_group" "app" {
              compartment_id = var.compartment_ocid
              vcn_id         = oci_core_vcn.main.id
              display_name   = "${local.name}-app"
              freeform_tags  = local.tags
            }

            resource "oci_core_network_security_group" "data" {
              compartment_id = var.compartment_ocid
              vcn_id         = oci_core_vcn.main.id
              display_name   = "${local.name}-data"
              freeform_tags  = local.tags
            }

            resource "oci_core_network_security_group_security_rule" "lb_https" {
              network_security_group_id = oci_core_network_security_group.lb.id
              direction                 = "INGRESS"
              protocol                  = "6"
              source                    = "0.0.0.0/0"
              source_type               = "CIDR_BLOCK"
              description               = "Web traffic from the internet"

              tcp_options {
                destination_port_range {
                  min = 443
                  max = 443
                }
              }
            }

            resource "oci_core_network_security_group_security_rule" "lb_http" {
              network_security_group_id = oci_core_network_security_group.lb.id
              direction                 = "INGRESS"
              protocol                  = "6"
              source                    = "0.0.0.0/0"
              source_type               = "CIDR_BLOCK"
              description               = "Redirected to HTTPS"

              tcp_options {
                destination_port_range {
                  min = 80
                  max = 80
                }
              }
            }

            resource "oci_core_network_security_group_security_rule" "lb_egress" {
              network_security_group_id = oci_core_network_security_group.lb.id
              direction                 = "EGRESS"
              protocol                  = "6"
              destination               = oci_core_network_security_group.app.id
              destination_type          = "NETWORK_SECURITY_GROUP"
              description               = "To the containers"
            }

            resource "oci_core_network_security_group_security_rule" "app_from_lb" {
              network_security_group_id = oci_core_network_security_group.app.id
              direction                 = "INGRESS"
              protocol                  = "6"
              source                    = oci_core_network_security_group.lb.id
              source_type               = "NETWORK_SECURITY_GROUP"
              description               = "Only the load balancer reaches the containers"
            }

            resource "oci_core_network_security_group_security_rule" "app_egress" {
              network_security_group_id = oci_core_network_security_group.app.id
              direction                 = "EGRESS"
              protocol                  = "all"
              destination               = "0.0.0.0/0"
              destination_type          = "CIDR_BLOCK"
              description               = "Outbound calls through the NAT gateway"
            }

            resource "oci_core_network_security_group_security_rule" "data_from_app" {
              network_security_group_id = oci_core_network_security_group.data.id
              direction                 = "INGRESS"
              protocol                  = "6"
              source                    = oci_core_network_security_group.app.id
              source_type               = "NETWORK_SECURITY_GROUP"
              description               = "Only the containers reach the data services"
            }
        """)))

    def registry(self):
        parts = [T("""
            resource "oci_artifacts_container_repository" "@@n@@" {
              compartment_id = var.compartment_ocid
              display_name   = "${local.name}/@@name@@"
              is_public      = false
              is_immutable   = false
            }
        """, n=u["name"].replace("-", "_"), name=u["name"]) for u in self.units]
        self.files.add("terraform/registry.tf", tf(*parts))

    def image(self, unit):
        return '"${var.region_key}.ocir.io/${data.oci_objectstorage_namespace.ns.namespace}/${local.name}/%s:${var.image_tag}"' % unit["name"]

    def env_pairs(self):
        out = [('APP_ENV', '"production"')]
        if self.pg:
            out += [("DB_HOST", "oci_psql_db_system.main.network_details[0].primary_db_endpoint_private_ip"), ("DB_PORT", '"5432"'),
                    ("DB_NAME", '"postgres"'), ("DB_USER", '"app"')]
        if self.redis:
            out += [("REDIS_HOST", "oci_redis_redis_cluster.main.primary_fqdn"), ("REDIS_PORT", '"6379"')]
        if self.kafka:
            out.append(("KAFKA_BOOTSTRAP_SERVERS", "oci_streaming_stream_pool.main.kafka_settings[0].bootstrap_servers"))
        if self.storage:
            out += [("S3_BUCKET", "oci_objectstorage_bucket.assets.name"), ("S3_NAMESPACE", "data.oci_objectstorage_namespace.ns.namespace")]
        out += settings_pairs(self.m)
        return out

    def containers(self):
        env = "\n".join("      %s = %s" % (k, v) for k, v in self.env_pairs())
        parts = []
        for u in self.units:
            n = u["name"].replace("-", "_")
            worker = u["kind"] == "worker"
            count = "var.deploy_app ? %s : 0" % ("1" if worker else "var.replicas")
            health = ""
            if not worker:
                health = T("""

                    health_checks {
                      health_check_type = "HTTP"
                      port              = @@port@@
                      path              = "@@path@@"
                    }
                """, port=u["port"] or 8080, path=u["health_path"] or "/").rstrip("\n")
            parts.append(T("""
                resource "oci_container_instances_container_instance" "@@n@@" {
                  count                    = @@count@@
                  compartment_id           = var.compartment_ocid
                  availability_domain      = data.oci_identity_availability_domains.all.availability_domains[count.index % length(data.oci_identity_availability_domains.all.availability_domains)].name
                  display_name             = "${local.name}-@@name@@-${count.index + 1}"
                  shape                    = "CI.Standard.E4.Flex"
                  container_restart_policy = "ALWAYS"
                  freeform_tags            = local.tags

                  shape_config {
                    ocpus         = var.ocpus
                    memory_in_gbs = var.memory_gbs
                  }

                  containers {
                    display_name = "@@name@@"
                    image_url    = @@image@@

                    environment_variables = {
                      PORT = "@@port@@"
            @@env@@
                    }

                    resource_config {
                      vcpus_limit         = var.ocpus
                      memory_limit_in_gbs = var.memory_gbs
                    }

                    security_context {
                      is_non_root_user_check_enabled = true
                      is_root_file_system_readonly   = true
                      run_as_user                    = 10001
                    }
            @@health@@
                  }

                  vnics {
                    subnet_id             = oci_core_subnet.private.id
                    is_public_ip_assigned = false
                    nsg_ids               = [oci_core_network_security_group.app.id]
                  }

                  depends_on = [oci_artifacts_container_repository.@@n@@]
                }
            """, n=n, name=u["name"], count=count, image=self.image(u), port=u["port"] or 8080, env=env, health=health))
        self.files.add("terraform/containers.tf", tf(*parts))

    def load_balancer(self):
        if not self.public:
            return
        first = self.public[0]
        parts = [T("""
            resource "oci_load_balancer_load_balancer" "main" {
              compartment_id             = var.compartment_ocid
              display_name               = local.name
              shape                      = "flexible"
              subnet_ids                 = [oci_core_subnet.public.id]
              network_security_group_ids = [oci_core_network_security_group.lb.id]
              is_private                 = false
              freeform_tags              = local.tags

              shape_details {
                minimum_bandwidth_in_mbps = 10
                maximum_bandwidth_in_mbps = 100
              }
            }
        """)]
        for u in self.public:
            n = u["name"].replace("-", "_")
            parts.append(T("""
                resource "oci_load_balancer_backend_set" "@@n@@" {
                  load_balancer_id = oci_load_balancer_load_balancer.main.id
                  name             = "@@name@@"
                  policy           = "ROUND_ROBIN"

                  health_checker {
                    protocol          = "HTTP"
                    port              = @@port@@
                    url_path          = "@@path@@"
                    return_code       = 200
                    interval_ms       = 10000
                    timeout_in_millis = 3000
                    retries           = 3
                  }
                }

                resource "oci_load_balancer_backend" "@@n@@" {
                  count            = var.deploy_app ? var.replicas : 0
                  load_balancer_id = oci_load_balancer_load_balancer.main.id
                  backendset_name  = oci_load_balancer_backend_set.@@n@@.name
                  ip_address       = oci_container_instances_container_instance.@@n@@[count.index].vnics[0].private_ip
                  port             = @@port@@
                }
            """, n=n, name=u["name"], port=u["port"] or 8080, path=u["health_path"] or "/"))
        others = [u for u in self.public if u is not first]
        route = ""
        if others:
            routes = "\n".join(T("""
                path_routes {
                  backend_set_name = oci_load_balancer_backend_set.@@n@@.name
                  path             = "/@@name@@"

                  path_match_type {
                    match_type = "PREFIX_MATCH"
                  }
                }
            """, n=u["name"].replace("-", "_"), name=u["name"]) for u in others)
            parts.append("resource \"oci_load_balancer_path_route_set\" \"main\" {\n  load_balancer_id = oci_load_balancer_load_balancer.main.id\n  name             = \"routes\"\n\n%s}\n" % routes)
            route = '  path_route_set_name      = oci_load_balancer_path_route_set.main.name\n'
        parts.append(T("""
            resource "oci_load_balancer_listener" "http" {
              load_balancer_id         = oci_load_balancer_load_balancer.main.id
              name                     = "http"
              default_backend_set_name = oci_load_balancer_backend_set.@@first@@.name
              port                     = 80
              protocol                 = "HTTP"
            @@route@@
              connection_configuration {
                idle_timeout_in_seconds = @@idle@@
              }
            }
        """, first=first["name"].replace("-", "_"), route=route.rstrip("\n"), idle=300 if self.m.streaming else 60))
        self.files.add("terraform/load_balancer.tf", tf(*parts))

    def functions(self):
        parts = [T("""
            resource "oci_functions_application" "main" {
              compartment_id             = var.compartment_ocid
              display_name               = local.name
              subnet_ids                 = [oci_core_subnet.private.id]
              network_security_group_ids = [oci_core_network_security_group.app.id]
              shape                      = "GENERIC_X86"
              freeform_tags              = local.tags
            }
        """)]
        for u in self.units:
            n = u["name"].replace("-", "_")
            parts.append(T("""
                resource "oci_functions_function" "@@n@@" {
                  count          = var.deploy_app ? 1 : 0
                  application_id = oci_functions_application.main.id
                  display_name   = "@@name@@"
                  memory_in_mbs  = @@mem@@
                  timeout_in_seconds = 120

                  source_details {
                    source_type = "PRE_BUILT_IMAGE"
                    image       = @@image@@
                  }

                  depends_on = [oci_artifacts_container_repository.@@n@@]
                }
            """, n=n, name=u["name"], image=self.image(u), mem=int(min(max(self.memory * 1024, 128), 3072) // 128 * 128)))
        if self.public:
            routes = ""
            for u in self.public:
                n = u["name"].replace("-", "_")
                path = "/" if u is self.public[0] else "/%s" % u["name"]
                routes += T("""
                    routes {
                      path    = "@@path@@/{path*}"
                      methods = ["ANY"]

                      backend {
                        type        = "ORACLE_FUNCTIONS_BACKEND"
                        function_id = oci_functions_function.@@n@@[0].id
                      }
                    }
                """, n=n, path=path.rstrip("/")) + "\n"
            parts.append(T("""
                resource "oci_apigateway_gateway" "main" {
                  compartment_id = var.compartment_ocid
                  display_name   = local.name
                  endpoint_type  = "PUBLIC"
                  subnet_id      = oci_core_subnet.public.id
                  freeform_tags  = local.tags
                }

                resource "oci_apigateway_deployment" "main" {
                  count          = var.deploy_app ? 1 : 0
                  compartment_id = var.compartment_ocid
                  gateway_id     = oci_apigateway_gateway.main.id
                  display_name   = local.name
                  path_prefix    = "/"

                  specification {
            @@routes@@
                  }
                }
            """, routes=routes.rstrip("\n")))
        self.files.add("terraform/functions.tf", tf(*parts))

    def data_services(self):
        parts = []
        if self.pg:
            parts.append(T("""
                resource "random_password" "database" {
                  length  = 32
                  special = false
                }

                resource "oci_psql_db_system" "main" {
                  compartment_id              = var.compartment_ocid
                  display_name                = "${local.name}-db"
                  db_version                  = "16"
                  shape                       = "PostgreSQL.VM.Standard.E4.Flex.2.32GB"
                  instance_count              = @@count@@
                  instance_ocpu_count         = 2
                  instance_memory_size_in_gbs = 32
                  freeform_tags               = local.tags

                  credentials {
                    username = "app"

                    password_details {
                      password_type = "PLAIN_TEXT"
                      password      = random_password.database.result
                    }
                  }

                  network_details {
                    subnet_id = oci_core_subnet.private.id
                    nsg_ids   = [oci_core_network_security_group.data.id]
                  }

                  storage_details {
                    is_regionally_durable = @@ha@@
                    system_type           = "OCI_OPTIMIZED_STORAGE"
                  }

                  management_policy {
                    backup_policy {
                      kind           = "DAILY"
                      backup_start   = "02:00"
                      retention_days = 14
                    }
                  }

                  lifecycle {
                    prevent_destroy = true
                  }
                }
            """, count=2 if self.ha else 1, ha="true" if self.ha else "false"))
        if self.redis:
            parts.append(T("""
                resource "oci_redis_redis_cluster" "main" {
                  compartment_id     = var.compartment_ocid
                  display_name       = "${local.name}-redis"
                  node_count         = @@nodes@@
                  node_memory_in_gbs = 2
                  software_version   = "REDIS_7_0"
                  subnet_id          = oci_core_subnet.private.id
                  nsg_ids            = [oci_core_network_security_group.data.id]
                  freeform_tags      = local.tags
                }
            """, nodes=3 if self.ha else 1))
        if self.kafka:
            parts.append(T("""
                resource "oci_streaming_stream_pool" "main" {
                  compartment_id = var.compartment_ocid
                  name           = "${local.name}-streams"

                  kafka_settings {
                    auto_create_topics_enable = false
                    num_partitions            = 3
                  }

                  private_endpoint_settings {
                    subnet_id = oci_core_subnet.private.id
                    nsg_ids   = [oci_core_network_security_group.data.id]
                  }
                }

                resource "oci_streaming_stream" "events" {
                  name               = "events"
                  partitions         = 3
                  retention_in_hours = 72
                  stream_pool_id     = oci_streaming_stream_pool.main.id
                }
            """))
        if parts:
            self.files.add("terraform/data.tf", tf(*parts))

    def object_storage(self):
        self.files.add("terraform/storage.tf", tf(T("""
            resource "oci_objectstorage_bucket" "assets" {
              compartment_id = var.compartment_ocid
              namespace      = data.oci_objectstorage_namespace.ns.namespace
              name           = "${local.name}-assets"
              access_type    = "NoPublicAccess"
              versioning     = "Enabled"
              kms_key_id     = oci_kms_key.main.id
              freeform_tags  = local.tags
            }
        """)))

    def security(self):
        parts = [T("""
            resource "oci_kms_vault" "main" {
              compartment_id = var.compartment_ocid
              display_name   = local.name
              vault_type     = "DEFAULT"
              freeform_tags  = local.tags
            }

            resource "oci_kms_key" "main" {
              compartment_id      = var.compartment_ocid
              display_name        = "${local.name}-key"
              management_endpoint = oci_kms_vault.main.management_endpoint

              key_shape {
                algorithm = "AES"
                length    = 32
              }
            }

            resource "oci_vault_secret" "app" {
              compartment_id = var.compartment_ocid
              vault_id       = oci_kms_vault.main.id
              key_id         = oci_kms_key.main.id
              secret_name    = "${local.name}-app"
              description    = "Application settings. Replace the placeholder with the real JSON."

              secret_content {
                content_type = "BASE64"
                content      = base64encode("{}")
              }

              lifecycle {
                ignore_changes = [secret_content]
              }
            }
        """)]
        if self.waf and self.public:
            parts.append(T("""
                resource "oci_waf_web_app_firewall_policy" "main" {
                  compartment_id = var.compartment_ocid
                  display_name   = "${local.name}-waf"

                  actions {
                    name = "allow"
                    type = "ALLOW"
                  }

                  actions {
                    name = "block"
                    type = "RETURN_HTTP_RESPONSE"
                    code = 403

                    body {
                      type = "STATIC_TEXT"
                      text = "Blocked"
                    }
                  }

                  request_rate_limiting {
                    rules {
                      name        = "limit-per-client"
                      type        = "REQUEST_RATE_LIMITING"
                      action_name = "block"

                      configurations {
                        period_in_seconds         = 10
                        requests_limit            = 500
                        action_duration_in_seconds = 60
                      }
                    }
                  }
                }

                resource "oci_waf_web_app_firewall" "main" {
                  compartment_id              = var.compartment_ocid
                  backend_type                = "LOAD_BALANCER"
                  load_balancer_id            = oci_load_balancer_load_balancer.main.id
                  web_app_firewall_policy_id  = oci_waf_web_app_firewall_policy.main.id
                  display_name                = "${local.name}-waf"
                }
            """))
        self.files.add("terraform/security.tf", tf(*parts))

    def observability(self):
        self.files.add("terraform/observability.tf", tf(T("""
            resource "oci_logging_log_group" "main" {
              compartment_id = var.compartment_ocid
              display_name   = local.name
              description    = "Logs for ${local.name}"
            }
        """)))

    def outputs(self):
        parts = [T("""
            output "registry_address" {
              description = "Push your images to this address before turning deploy_app on."
              value       = "${var.region_key}.ocir.io/${data.oci_objectstorage_namespace.ns.namespace}/${local.name}"
            }

            output "vcn_id" {
              description = "The network the services share."
              value       = oci_core_vcn.main.id
            }
        """)]
        if self.public and not self.serverless:
            parts.append(T("""
                output "load_balancer_ip" {
                  description = "Point your domain's A record here."
                  value       = oci_load_balancer_load_balancer.main.ip_address_details[0].ip_address
                }
            """))
        if self.pg:
            parts.append(T("""
                output "database_host" {
                  description = "Private address of the PostgreSQL primary."
                  value       = oci_psql_db_system.main.network_details[0].primary_db_endpoint_private_ip
                }

                output "database_password" {
                  description = "The generated database password. Put it in the vault, not in a file."
                  value       = random_password.database.result
                  sensitive   = true
                }
            """))
        self.files.add("terraform/outputs.tf", tf(*parts))

    def readme(self):
        m = self.m
        return T("""
            # Oracle Cloud infrastructure for @@project@@

            Generated from plan @@fp@@. This creates a VCN with public and private subnets, a NAT gateway and a service gateway, a
            Vault with a key, @@compute@@, and the data services the plan uses.

            ## Steps

            1. Configure the OCI CLI profile or export the `TF_VAR_` and `OCI_` variables (tenancy, user, fingerprint, key). Put your tenancy and compartment OCIDs in `terraform.tfvars`.
            2. Create an Object Storage bucket for state and a customer secret key, copy `backend.hcl.example` to `backend.hcl`, fill it in, and export the key as `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`.
            3. `terraform init -backend-config=backend.hcl`, then `terraform apply` with `deploy_app = false`.
            4. Build and push each image to `terraform output registry_address` (`docker login <region-key>.ocir.io`, then `docker push`).
            5. Set `deploy_app = true` and apply again.

            ## Notes

            - The load balancer listens on HTTP so you can test. Add a certificate and an HTTPS listener (and redirect HTTP) before real traffic.
            - The WAF policy here limits request rates. Turn on the managed protection rules in the console or extend the policy; they depend on your tenancy's rule set.
            - Identity domains and RabbitMQ are listed as gaps by the plan; they are not created here.
        """, project=m.project, fp=m.fingerprint,
                 compute=("OCI Functions behind an API Gateway" if self.serverless else "Container Instances behind a load balancer%s" % (" with a WAF" if self.waf else "")))


def generate(model):
    return Oci(model).generate()
