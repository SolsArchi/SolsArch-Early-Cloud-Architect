#!/usr/bin/env python3
"""Terraform for Google Cloud, generated from the plan.

Containers run on Cloud Run behind a global external Application Load Balancer with Cloud Armor (server model), or on
Cloud Run scaled to zero behind API Gateway (serverless model). It follows the same baseline as every other provider: a
private data tier, a managed front door, encryption and backups on every data store, regional high availability when it
is on, secrets in Secret Manager, service accounts with narrow roles, logs and alerts, and health checks that use the
path the code really serves.
"""
from iac_common import Files, T, fmt_hcl, ident, tf

GCP_REGION_DEFAULT = "us-central1"
SQL_TIER = {"small": "db-custom-1-3840", "medium": "db-custom-2-7680", "large": "db-custom-8-30720"}
SQL_MYSQL_TIER = SQL_TIER


class Gcp:
    def __init__(self, model):
        self.m = model
        self.files = Files(model.fingerprint)
        self.public = model.public_units()
        self.default_unit = self.public[0] if self.public else None
        self.use_edge = model.has("edge")
        self.serverless = model.mode == "serverless"
        self.region = model.region or GCP_REGION_DEFAULT
        self.ha = not model.single_az
        self.api_gateway = self.serverless and bool(self.public)
        self.lb = (not self.serverless) and bool(self.public)

    def generate(self):
        m = self.m
        self.versions()
        self.providers()
        self.variables()
        self.core()
        self.network()
        self.registry_and_identity()
        self.secrets()
        self.data_stores()
        self.run_services()
        if self.lb:
            self.load_balancer()
        if self.api_gateway:
            self.gateway()
        self.monitoring()
        self.outputs()
        self.files.add("terraform/backend.tf", T("""
            terraform {
              backend "gcs" {}
            }
        """))
        self.files.add("terraform/backend.hcl.example", T("""
            bucket = "REPLACE-with-the-state-bucket-from-bootstrap"
            prefix = "@@project@@/production"
        """, project=m.project))
        self.files.add("terraform/bootstrap/main.tf", self.bootstrap())
        self.files.add("terraform/terraform.tfvars.example", fmt_hcl('project_id   = "REPLACE-with-your-project-id"\nproject_name = "%s"\nenvironment  = "production"\nregion       = "%s"\n' % (m.project, self.region)))
        self.files.add("terraform/README.md", self.readme())
        return self.files

    def versions(self):
        beta = ""
        if self.api_gateway:
            beta = T("""
                google-beta = {
                  source  = "hashicorp/google-beta"
                  version = "~> 8.0"
                }
            """).rstrip("\n")
        self.files.add("terraform/versions.tf", tf(T("""
            terraform {
              required_version = ">= 1.9.0"

              required_providers {
                google = {
                  source  = "hashicorp/google"
                  version = "~> 8.0"
                }
@@beta@@
                random = {
                  source  = "hashicorp/random"
                  version = "~> 3.6"
                }
              }
            }
        """, beta=beta)))

    def providers(self):
        body = T("""
            provider "google" {
              project = var.project_id
              region  = var.region
            }
        """)
        if self.api_gateway:
            body += "\n" + T("""
                provider "google-beta" {
                  project = var.project_id
                  region  = var.region
                }
            """)
        self.files.add("terraform/providers.tf", tf(body))

    def variables(self):
        m = self.m
        out = [T("""
            variable "project_id" {
              type        = string
              description = "Google Cloud project that holds everything."
            }

            variable "project_name" {
              type        = string
              description = "Short name used in resource names and labels."
              default     = "@@project@@"
            }

            variable "environment" {
              type        = string
              description = "Deployment environment, for example production or staging."
              default     = "production"
            }

            variable "region" {
              type        = string
              description = "Google Cloud region."
              default     = "@@region@@"
            }

            variable "subnet_cidr" {
              type        = string
              description = "Address range of the application subnet."
              default     = "10.60.0.0/20"
            }

            variable "image_tag" {
              type        = string
              description = "Tag of the container images in the repository created here."
              default     = "latest"
            }

            variable "alarm_email" {
              type        = string
              description = "Email address that receives alerts. Leave null to create no notification channel."
              default     = null
            }

            variable "enable_deletion_protection" {
              type        = bool
              description = "Protect databases from deletion."
              default     = @@protect@@
            }
        """, project=m.project, region=self.region, protect="false" if m.lean else "true")]
        if self.lb:
            out.append(T("""
                variable "domain" {
                  type        = string
                  description = "Domain name for a Google-managed certificate. Leave null to serve HTTP until you have one."
                  default     = null
                }
            """))
        if m.db_key:
            out.append(T("""
                variable "db_name" {
                  type        = string
                  description = "Name of the first database."
                  default     = "app"
                }

                variable "db_username" {
                  type        = string
                  description = "Database user name. The password is generated and kept in Secret Manager."
                  default     = "app"
                }
            """))
        for unit in m.units:
            if unit["kind"] == "static":
                continue
            n = ident(unit["name"])
            out.append(T("""
                variable "@@n@@_image" {
                  type        = string
                  description = "Container image for @@name@@. Leave null to use the repository created for it with the image_tag."
                  default     = null
                }

                variable "@@n@@_min_instances" {
                  type        = number
                  description = "Smallest number of copies of @@name@@ kept running."
                  default     = @@count@@
                }
            """, n=n, name=unit["name"], count=0 if self.serverless else (1 if unit["kind"] == "worker" else m.replicas)))
        self.files.add("terraform/variables.tf", tf(*out))

    def core(self):
        m = self.m
        apis = ["run.googleapis.com", "artifactregistry.googleapis.com", "secretmanager.googleapis.com", "compute.googleapis.com",
                "monitoring.googleapis.com", "logging.googleapis.com", "iam.googleapis.com"]
        if m.db_key:
            apis += ["sqladmin.googleapis.com", "servicenetworking.googleapis.com"]
        if m.has("cache"):
            apis.append("redis.googleapis.com")
        if m.has("queue_kafka"):
            apis.append("managedkafka.googleapis.com")
        if self.api_gateway:
            apis += ["apigateway.googleapis.com", "servicemanagement.googleapis.com", "servicecontrol.googleapis.com"]
        if m.has("identity"):
            apis.append("identitytoolkit.googleapis.com")
        quoted = ", ".join('"%s"' % a for a in sorted(set(apis)))
        self.files.add("terraform/main.tf", tf(T("""
            locals {
              name = "${var.project_name}-${var.environment}"
              labels = {
                project     = var.project_name
                environment = var.environment
                managed_by  = "terraform"
                generated   = "solsarch"
              }
            }

            resource "google_project_service" "apis" {
              for_each = toset([@@apis@@])

              service            = each.value
              disable_on_destroy = false
            }
        """, apis=quoted)))

    def network(self):
        m = self.m
        out = [T("""
            resource "google_compute_network" "main" {
              name                    = local.name
              auto_create_subnetworks = false

              depends_on = [google_project_service.apis]
            }

            resource "google_compute_subnetwork" "apps" {
              name                     = "${local.name}-apps"
              region                   = var.region
              network                  = google_compute_network.main.id
              ip_cidr_range            = var.subnet_cidr
              private_ip_google_access = true@@flow@@
            }

            resource "google_compute_router" "main" {
              name    = local.name
              region  = var.region
              network = google_compute_network.main.id
            }

            resource "google_compute_router_nat" "main" {
              name                               = local.name
              router                             = google_compute_router.main.name
              region                             = var.region
              nat_ip_allocate_option             = "AUTO_ONLY"
              source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"

              log_config {
                enable = true
                filter = "ERRORS_ONLY"
              }
            }
        """, flow=T("""

              log_config {
                aggregation_interval = "INTERVAL_5_MIN"
                flow_sampling        = 0.5
                metadata             = "INCLUDE_ALL_METADATA"
              }
        """).rstrip("\n") if m.regulated else "")]
        if m.db_key:
            out.append(T("""
                resource "google_compute_global_address" "private_services" {
                  name          = "${local.name}-private-services"
                  purpose       = "VPC_PEERING"
                  address_type  = "INTERNAL"
                  prefix_length = 16
                  network       = google_compute_network.main.id
                }

                resource "google_service_networking_connection" "private_services" {
                  network                 = google_compute_network.main.id
                  service                 = "servicenetworking.googleapis.com"
                  reserved_peering_ranges = [google_compute_global_address.private_services.name]
                }
            """))
        out.append(T("""
            resource "google_compute_firewall" "deny_ingress" {
              name      = "${local.name}-deny-ingress"
              network   = google_compute_network.main.name
              direction = "INGRESS"
              priority  = 65000

              deny {
                protocol = "all"
              }

              source_ranges = ["0.0.0.0/0"]
            }
        """))
        self.files.add("terraform/network.tf", tf(*out))

    def registry_and_identity(self):
        m = self.m
        out = [T("""
            resource "google_artifact_registry_repository" "main" {
              location      = var.region
              repository_id = local.name
              format        = "DOCKER"
              description   = "Container images for ${local.name}"
              labels        = local.labels

              cleanup_policies {
                id     = "keep-recent"
                action = "KEEP"

                most_recent_versions {
                  keep_count = 20
                }
              }

              depends_on = [google_project_service.apis]
            }

            resource "google_service_account" "apps" {
              account_id   = "${substr(var.project_name, 0, 20)}-apps"
              display_name = "Application runtime for ${local.name}"
            }

            resource "google_project_iam_member" "apps_logs" {
              project = var.project_id
              role    = "roles/logging.logWriter"
              member  = "serviceAccount:${google_service_account.apps.email}"
            }

            resource "google_project_iam_member" "apps_metrics" {
              project = var.project_id
              role    = "roles/monitoring.metricWriter"
              member  = "serviceAccount:${google_service_account.apps.email}"
            }
        """)]
        if m.db_key:
            out.append(T("""
                resource "google_project_iam_member" "apps_sql" {
                  project = var.project_id
                  role    = "roles/cloudsql.client"
                  member  = "serviceAccount:${google_service_account.apps.email}"
                }
            """))
        self.files.add("terraform/identity.tf", tf(*out))

    def secrets(self):
        m = self.m
        out = [T("""
            resource "google_secret_manager_secret" "app" {
              secret_id = "${local.name}-app"
              labels    = local.labels

              replication {
                auto {}
              }

              depends_on = [google_project_service.apis]
            }

            resource "google_secret_manager_secret_iam_member" "app" {
              secret_id = google_secret_manager_secret.app.id
              role      = "roles/secretmanager.secretAccessor"
              member    = "serviceAccount:${google_service_account.apps.email}"
            }
        """)]
        if m.db_key:
            out.append(T("""
                resource "random_password" "db" {
                  length  = 32
                  special = false
                }

                resource "google_secret_manager_secret" "db_password" {
                  secret_id = "${local.name}-db-password"
                  labels    = local.labels

                  replication {
                    auto {}
                  }

                  depends_on = [google_project_service.apis]
                }

                resource "google_secret_manager_secret_version" "db_password" {
                  secret      = google_secret_manager_secret.db_password.id
                  secret_data = random_password.db.result
                }

                resource "google_secret_manager_secret_iam_member" "db_password" {
                  secret_id = google_secret_manager_secret.db_password.id
                  role      = "roles/secretmanager.secretAccessor"
                  member    = "serviceAccount:${google_service_account.apps.email}"
                }
            """))
        self.files.add("terraform/secrets.tf", tf(*out))

    def data_stores(self):
        m = self.m
        out = []
        if m.db_key:
            engine = "POSTGRES_16" if m.db_key == "database" else "MYSQL_8_0"
            tier = SQL_TIER[m.sizing["db_class"]]
            backup = T("""
                backup_configuration {
                  enabled                        = true
                  point_in_time_recovery_enabled = true
                  start_time                     = "03:00"
                  transaction_log_retention_days = 7

                  backup_retention_settings {
                    retained_backups = 14
                  }
                }
            """).rstrip("\n") if m.db_key == "database" else T("""
                backup_configuration {
                  enabled            = true
                  binary_log_enabled = true
                  start_time         = "03:00"

                  backup_retention_settings {
                    retained_backups = 14
                  }
                }
            """).rstrip("\n")
            out.append(T("""
                resource "google_sql_database_instance" "main" {
                  name                = "${local.name}-db"
                  region              = var.region
                  database_version    = "@@engine@@"
                  deletion_protection = var.enable_deletion_protection

                  settings {
                    tier              = "@@tier@@"
                    availability_type = "@@avail@@"
                    disk_autoresize   = true
                    disk_size         = @@disk@@
                    disk_type         = "PD_SSD"
                    user_labels       = local.labels

                    ip_configuration {
                      ipv4_enabled    = false
                      private_network = google_compute_network.main.id
                      ssl_mode        = "ENCRYPTED_ONLY"
                    }

                    @@backup@@

                    insights_config {
                      query_insights_enabled = true
                    }

                    maintenance_window {
                      day  = 7
                      hour = 3
                    }
                  }

                  depends_on = [google_service_networking_connection.private_services]
                }

                resource "google_sql_database" "app" {
                  name     = var.db_name
                  instance = google_sql_database_instance.main.name
                }

                resource "google_sql_user" "app" {
                  name     = var.db_username
                  instance = google_sql_database_instance.main.name
                  password = random_password.db.result
                }
            """, engine=engine, tier=tier, avail="REGIONAL" if self.ha else "ZONAL", disk=max(20, m.sizing["storage_gb"]), backup=backup))
        if m.has("cache"):
            out.append(T("""
                resource "google_redis_instance" "main" {
                  name                    = local.name
                  region                  = var.region
                  tier                    = "@@tier@@"
                  memory_size_gb          = @@gb@@
                  redis_version           = "REDIS_7_2"
                  authorized_network      = google_compute_network.main.id
                  connect_mode            = "PRIVATE_SERVICE_ACCESS"
                  auth_enabled            = true
                  transit_encryption_mode = "SERVER_AUTHENTICATION"
                  labels                  = local.labels

                  depends_on = [google_service_networking_connection.private_services]
                }
            """, tier="STANDARD_HA" if self.ha else "BASIC", gb=1 if m.sizing["cache_gb"] <= 1 else 4))
        if m.has("storage") or m.has("static_site"):
            out.append(T("""
                resource "google_storage_bucket" "app" {
                  name                        = "${local.name}-${var.project_id}-app"
                  location                    = var.region
                  uniform_bucket_level_access = true
                  public_access_prevention    = "enforced"
                  force_destroy               = false
                  labels                      = local.labels

                  versioning {
                    enabled = true
                  }

                  lifecycle_rule {
                    condition {
                      num_newer_versions = 5
                    }

                    action {
                      type = "Delete"
                    }
                  }
                }

                resource "google_storage_bucket_iam_member" "apps" {
                  bucket = google_storage_bucket.app.name
                  role   = "roles/storage.objectUser"
                  member = "serviceAccount:${google_service_account.apps.email}"
                }
            """))
        if m.has("queue_kafka"):
            out.append(T("""
                resource "google_managed_kafka_cluster" "main" {
                  cluster_id = local.name
                  location   = var.region

                  capacity_config {
                    vcpu_count   = 3
                    memory_bytes = 3221225472
                  }

                  gcp_config {
                    access_config {
                      network_configs {
                        subnet = google_compute_subnetwork.apps.id
                      }
                    }
                  }

                  depends_on = [google_project_service.apis]
                }
            """))
        if m.has("database_mongo"):
            self.files.add("terraform/atlas.tf", tf(T("""
                # MongoDB Atlas runs outside Google Cloud and is managed with its own provider.
                # Create an Atlas project and an API key, then set the variables below.
                variable "atlas_project_id" {
                  type        = string
                  description = "MongoDB Atlas project id."
                  default     = null
                }

                locals {
                  atlas_enabled = var.atlas_project_id != null
                }
            """)))
        if out:
            self.files.add("terraform/data.tf", tf(*out))

    def env_blocks(self, unit):
        m = self.m
        items = [("PORT", '"%d"' % (unit["port"] or 8080)), ("APP_ENV", "var.environment"), ("GCP_PROJECT", "var.project_id")]
        if m.db_key:
            items += [("DB_HOST", "google_sql_database_instance.main.private_ip_address"), ("DB_PORT", '"%d"' % (5432 if m.db_key == "database" else 3306)),
                      ("DB_NAME", "var.db_name"), ("DB_USER", "var.db_username")]
        if m.has("cache"):
            items += [("REDIS_HOST", "google_redis_instance.main.host"), ("REDIS_PORT", "tostring(google_redis_instance.main.port)")]
        if m.has("storage"):
            items.append(("STORAGE_BUCKET", "google_storage_bucket.app.name"))
        out = ""
        for k, v in items:
            out += '\n\n        env {\n          name  = "%s"\n          value = %s\n        }' % (k, v)
        if m.db_key:
            out += ('\n\n        env {\n          name = "DB_PASSWORD"\n\n          value_source {\n            secret_key_ref {\n'
                    '              secret  = google_secret_manager_secret.db_password.secret_id\n              version = "latest"\n            }\n          }\n        }')
        return out

    def run_services(self):
        m = self.m
        cpu = "%g" % float(m.sizing["unit_vcpu"])
        mem = "%dMi" % int(float(m.sizing["unit_memory_gb"]) * 1024)
        out = []
        for unit in m.units:
            if unit["kind"] == "static":
                continue
            n, name = ident(unit["name"]), unit["name"]
            port = unit["port"] or 8080
            is_public = unit in self.public
            health = unit["health_path"] or "/"
            ingress = "INGRESS_TRAFFIC_INTERNAL_LOAD_BALANCER" if (is_public and self.lb) else (
                "INGRESS_TRAFFIC_ALL" if is_public else "INGRESS_TRAFFIC_INTERNAL_ONLY")
            probes = ""
            ports = ""
            if unit["kind"] != "worker":
                ports = T("""

                    ports {
                      container_port = @@port@@
                    }
                """, port=port).rstrip("\n")
                probes = "\n\n" + T("""
                    startup_probe {
                      http_get {
                        path = "@@health@@"
                        port = @@port@@
                      }
                    }

                    liveness_probe {
                      http_get {
                        path = "@@health@@"
                        port = @@port@@
                      }
                    }
                """, health=health, port=port).rstrip("\n")
            out.append(T("""
                resource "google_cloud_run_v2_service" "@@n@@" {
                  name                = "@@name@@"
                  location            = var.region
                  ingress             = "@@ingress@@"
                  deletion_protection = var.enable_deletion_protection
                  labels              = local.labels

                  template {
                    service_account                  = google_service_account.apps.email
                    max_instance_request_concurrency = 80

                    scaling {
                      min_instance_count = var.@@n@@_min_instances
                      max_instance_count = max(var.@@n@@_min_instances * 4, 10)
                    }

                    vpc_access {
                      egress = "ALL_TRAFFIC"

                      network_interfaces {
                        network    = google_compute_network.main.id
                        subnetwork = google_compute_subnetwork.apps.id
                      }
                    }

                    containers {
                      name  = "@@name@@"
                      image = coalesce(var.@@n@@_image, "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.main.repository_id}/@@name@@:${var.image_tag}")@@ports@@

                      resources {
                        cpu_idle = @@idle@@

                        limits = {
                          cpu    = "@@cpu@@"
                          memory = "@@mem@@"
                        }
                      }@@env@@@@probes@@
                    }
                  }

                  depends_on = [
                    google_project_service.apis,
                    google_secret_manager_secret_iam_member.app,
                  ]
                }
            """, n=n, name=name, ingress=ingress, cpu=cpu, mem=mem, ports=("\n\n" + ports) if ports else "", env=self.env_blocks(unit),
                         probes=probes, idle="true" if (self.serverless or unit["kind"] != "worker") else "false"))
            if is_public:
                out.append(T("""
                    resource "google_cloud_run_v2_service_iam_member" "@@n@@_invoker" {
                      name     = google_cloud_run_v2_service.@@n@@.name
                      location = google_cloud_run_v2_service.@@n@@.location
                      role     = "roles/run.invoker"
                      member   = "allUsers"
                    }
                """, n=n))
        self.files.add("terraform/compute.tf", tf(*out))

    def path_for(self, unit):
        return "/api/*" if unit["kind"] == "api" or unit["name"] == "api" else "/%s/*" % unit["name"]

    def load_balancer(self):
        m = self.m
        d = self.default_unit
        negs, backends, matchers = [], [], []
        for unit in self.public:
            n = ident(unit["name"])
            negs.append(T("""
                resource "google_compute_region_network_endpoint_group" "@@n@@" {
                  name                  = "${local.name}-@@name@@"
                  region                = var.region
                  network_endpoint_type = "SERVERLESS"

                  cloud_run {
                    service = google_cloud_run_v2_service.@@n@@.name
                  }
                }

                resource "google_compute_backend_service" "@@n@@" {
                  name                  = "${local.name}-@@name@@"
                  protocol              = "HTTP"
                  load_balancing_scheme = "EXTERNAL_MANAGED"
                  timeout_sec           = @@timeout@@@@armor@@

                  backend {
                    group = google_compute_region_network_endpoint_group.@@n@@.id
                  }

                  log_config {
                    enable      = true
                    sample_rate = 1
                  }
                }
            """, n=n, name=unit["name"], timeout=300 if m.streaming else 30,
                         armor="\n  security_policy       = google_compute_security_policy.edge.id" if self.use_edge else ""))
        dn = ident(d["name"])
        path_rules = ""
        for unit in self.public:
            if unit is d:
                continue
            path_rules += '\n\n  path_matcher {\n    name            = "%s"\n    default_service = google_compute_backend_service.%s.id\n  }' % (ident(unit["name"]), ident(unit["name"]))
        host_rules = ""
        route_rules = ""
        for unit in self.public:
            if unit is d:
                continue
            host_rules += ""
        # one path matcher with path rules keeps the map small and valid
        rules = ""
        for unit in self.public:
            if unit is d:
                continue
            rules += '\n    path_rule {\n      paths   = ["%s"]\n      service = google_compute_backend_service.%s.id\n    }' % (self.path_for(unit), ident(unit["name"]))
        matcher = ""
        if rules:
            matcher = T("""

                host_rule {
                  hosts        = ["*"]
                  path_matcher = "routes"
                }

                path_matcher {
                  name            = "routes"
                  default_service = google_compute_backend_service.@@dn@@.id
                  @@rules@@
                }
            """, dn=dn, rules=rules.strip("\n")).rstrip("\n")
        body = [*negs, T("""
            resource "google_compute_global_address" "lb" {
              name = "${local.name}-lb"
            }

            resource "google_compute_url_map" "main" {
              name            = local.name
              default_service = google_compute_backend_service.@@dn@@.id@@matcher@@
            }

            resource "google_compute_ssl_policy" "modern" {
              name            = "${local.name}-modern"
              profile         = "MODERN"
              min_tls_version = "TLS_1_2"
            }

            resource "google_compute_managed_ssl_certificate" "main" {
              count = var.domain == null ? 0 : 1

              name = "${local.name}-cert"

              managed {
                domains = [var.domain]
              }
            }

            resource "google_compute_target_http_proxy" "main" {
              name    = local.name
              url_map = var.domain == null ? google_compute_url_map.main.id : google_compute_url_map.redirect[0].id
            }

            resource "google_compute_target_https_proxy" "main" {
              count = var.domain == null ? 0 : 1

              name             = local.name
              url_map          = google_compute_url_map.main.id
              ssl_certificates = [google_compute_managed_ssl_certificate.main[0].id]
              ssl_policy       = google_compute_ssl_policy.modern.id
            }

            resource "google_compute_url_map" "redirect" {
              count = var.domain == null ? 0 : 1

              name = "${local.name}-redirect"

              default_url_redirect {
                https_redirect         = true
                redirect_response_code = "MOVED_PERMANENTLY_DEFAULT"
                strip_query            = false
              }
            }

            resource "google_compute_global_forwarding_rule" "http" {
              name                  = "${local.name}-http"
              load_balancing_scheme = "EXTERNAL_MANAGED"
              port_range            = "80"
              target                = google_compute_target_http_proxy.main.id
              ip_address            = google_compute_global_address.lb.id
            }

            resource "google_compute_global_forwarding_rule" "https" {
              count = var.domain == null ? 0 : 1

              name                  = "${local.name}-https"
              load_balancing_scheme = "EXTERNAL_MANAGED"
              port_range            = "443"
              target                = google_compute_target_https_proxy.main[0].id
              ip_address            = google_compute_global_address.lb.id
            }
        """, dn=dn, matcher=("\n" + matcher) if matcher else "")]
        if self.use_edge:
            body.append(T("""
                resource "google_compute_security_policy" "edge" {
                  name = "${local.name}-edge"

                  rule {
                    action   = "deny(403)"
                    priority = 1000

                    match {
                      expr {
                        expression = "evaluatePreconfiguredWaf('sqli-v33-stable')"
                      }
                    }
                  }

                  rule {
                    action   = "deny(403)"
                    priority = 1001

                    match {
                      expr {
                        expression = "evaluatePreconfiguredWaf('xss-v33-stable')"
                      }
                    }
                  }

                  rule {
                    action   = "throttle"
                    priority = 2000

                    match {
                      versioned_expr = "SRC_IPS_V1"

                      config {
                        src_ip_ranges = ["*"]
                      }
                    }

                    rate_limit_options {
                      conform_action = "allow"
                      exceed_action  = "deny(429)"

                      rate_limit_threshold {
                        count        = 1000
                        interval_sec = 60
                      }
                    }
                  }

                  rule {
                    action   = "allow"
                    priority = 2147483647

                    match {
                      versioned_expr = "SRC_IPS_V1"

                      config {
                        src_ip_ranges = ["*"]
                      }
                    }
                  }
                }
            """))
        self.files.add("terraform/loadbalancer.tf", tf(*body))

    def gateway(self):
        d = self.default_unit
        dn = ident(d["name"])
        paths = []
        for unit in self.public:
            prefix = "/{proxy+}" if unit is d else self.path_for(unit).replace("/*", "/{proxy+}")
            paths.append((ident(unit["name"]), prefix))
        self.files.add("terraform/openapi.yaml.tftpl", self.openapi(paths), header=True)
        self.files.add("terraform/gateway.tf", tf(T("""
            resource "google_service_account" "gateway" {
              account_id   = "${substr(var.project_name, 0, 20)}-gw"
              display_name = "API Gateway for ${local.name}"
            }

            resource "google_cloud_run_v2_service_iam_member" "gateway_invoker" {
              for_each = {
@@services@@
              }

              name     = each.value.name
              location = each.value.location
              role     = "roles/run.invoker"
              member   = "serviceAccount:${google_service_account.gateway.email}"
            }

            resource "google_api_gateway_api" "main" {
              provider = google-beta

              api_id = local.name

              depends_on = [google_project_service.apis]
            }

            resource "google_api_gateway_api_config" "main" {
              provider = google-beta

              api                  = google_api_gateway_api.main.api_id
              api_config_id_prefix = "${local.name}-"

              openapi_documents {
                document {
                  path = "openapi.yaml"
                  contents = base64encode(templatefile("${path.module}/openapi.yaml.tftpl", {
                    title = local.name
@@urls@@
                  }))
                }
              }

              gateway_config {
                backend_config {
                  google_service_account = google_service_account.gateway.id
                }
              }

              lifecycle {
                create_before_destroy = true
              }
            }

            resource "google_api_gateway_gateway" "main" {
              provider = google-beta

              api_config = google_api_gateway_api_config.main.id
              gateway_id = local.name
              region     = var.region
            }
        """, services="\n".join("    %s = google_cloud_run_v2_service.%s" % (n, n) for n, _ in paths),
                                                       urls="\n".join("    %s_url = google_cloud_run_v2_service.%s.uri" % (n, n) for n, _ in paths))))

    def openapi(self, paths):
        lines = ["swagger: '2.0'", "info:", "  title: ${title}", "  version: '1.0'", "schemes:", "  - https", "produces:", "  - application/json", "paths:"]
        for n, path in paths:
            lines += ["  %s:" % path, "    x-google-backend:", "      address: ${%s_url}" % n, "      path_translation: APPEND_PATH_TO_ADDRESS",
                      "    get:", "      operationId: get_%s" % n, "      responses:", "        '200':", "          description: OK",
                      "    post:", "      operationId: post_%s" % n, "      responses:", "        '200':", "          description: OK"]
        return "\n".join(lines) + "\n"

    def monitoring(self):
        m = self.m
        out = [T("""
            resource "google_monitoring_notification_channel" "email" {
              count = var.alarm_email == null ? 0 : 1

              display_name = "${local.name} owner"
              type         = "email"

              labels = {
                email_address = var.alarm_email
              }
            }
        """)]
        for unit in m.units:
            if unit["kind"] == "static":
                continue
            n, name = ident(unit["name"]), unit["name"]
            out.append(T("""
                resource "google_monitoring_alert_policy" "@@n@@_errors" {
                  display_name = "${local.name} @@name@@ server errors"
                  combiner     = "OR"
                  user_labels  = local.labels

                  conditions {
                    display_name = "5xx responses"

                    condition_threshold {
                      filter          = "resource.type = \\"cloud_run_revision\\" AND resource.labels.service_name = \\"@@name@@\\" AND metric.type = \\"run.googleapis.com/request_count\\" AND metric.labels.response_code_class = \\"5xx\\""
                      comparison      = "COMPARISON_GT"
                      threshold_value = 10
                      duration        = "300s"

                      aggregations {
                        alignment_period   = "300s"
                        per_series_aligner = "ALIGN_SUM"
                      }
                    }
                  }

                  notification_channels = google_monitoring_notification_channel.email[*].id
                  depends_on            = [google_cloud_run_v2_service.@@n@@]
                }
            """, n=n, name=name))
        if m.db_key:
            out.append(T("""
                resource "google_monitoring_alert_policy" "db_cpu" {
                  display_name = "${local.name} database CPU"
                  combiner     = "OR"
                  user_labels  = local.labels

                  conditions {
                    display_name = "CPU above 80 percent"

                    condition_threshold {
                      filter          = "resource.type = \\"cloudsql_database\\" AND metric.type = \\"cloudsql.googleapis.com/database/cpu/utilization\\""
                      comparison      = "COMPARISON_GT"
                      threshold_value = 0.8
                      duration        = "300s"

                      aggregations {
                        alignment_period   = "300s"
                        per_series_aligner = "ALIGN_MEAN"
                      }
                    }
                  }

                  notification_channels = google_monitoring_notification_channel.email[*].id
                }
            """))
        self.files.add("terraform/monitoring.tf", tf(*out))

    def outputs(self):
        m = self.m
        out = [T("""
            output "artifact_registry" {
              description = "Push container images here."
              value       = "${var.region}-docker.pkg.dev/${var.project_id}/${google_artifact_registry_repository.main.repository_id}"
            }
        """)]
        if self.lb:
            out.append(T("""
                output "load_balancer_ip" {
                  description = "Public address of the load balancer."
                  value       = google_compute_global_address.lb.address
                }
            """))
        if self.api_gateway:
            out.append(T("""
                output "gateway_hostname" {
                  description = "Public address of the API Gateway."
                  value       = google_api_gateway_gateway.main.default_hostname
                }
            """))
        if m.db_key:
            out.append(T("""
                output "database_connection_name" {
                  description = "Connection name of the Cloud SQL instance."
                  value       = google_sql_database_instance.main.connection_name
                }
            """))
        self.files.add("terraform/outputs.tf", tf(*out))

    def bootstrap(self):
        return tf(T("""
            terraform {
              required_version = ">= 1.9.0"

              required_providers {
                google = {
                  source  = "hashicorp/google"
                  version = "~> 8.0"
                }
              }
            }

            variable "project_id" {
              type        = string
              description = "Google Cloud project that holds the state bucket."
            }

            variable "region" {
              type        = string
              description = "Location of the state bucket."
              default     = "@@region@@"
            }

            provider "google" {
              project = var.project_id
              region  = var.region
            }

            resource "google_storage_bucket" "state" {
              name                        = "${var.project_id}-@@project@@-tfstate"
              location                    = var.region
              uniform_bucket_level_access = true
              public_access_prevention    = "enforced"
              force_destroy               = false

              versioning {
                enabled = true
              }
            }

            output "state_bucket" {
              description = "Put this in backend.hcl as the bucket."
              value       = google_storage_bucket.state.name
            }
        """, region=self.region, project=self.m.project))

    def readme(self):
        m = self.m
        units = ", ".join(u["name"] for u in m.units if u["kind"] != "static")
        return T("""
            # Terraform for @@project@@ on Google Cloud

            Compute model: @@mode@@. Units: @@units@@. Plan fingerprint @@fp@@.

            ## Order of work

            1. `gcloud auth application-default login`, then `cd bootstrap && terraform init && terraform apply -var project_id=YOUR_PROJECT` once. It creates the bucket that keeps Terraform state.
            2. Copy `backend.hcl.example` to `backend.hcl` and put the bucket name in it.
            3. `terraform init -backend-config=backend.hcl`
            4. `terraform apply -var project_id=YOUR_PROJECT`. The first apply creates everything, including the empty image repository.
            5. Build and push each image to the repository in the outputs, then apply again.

            ## What is configured for you

            - A private network with Cloud NAT; the database and cache have private addresses only.
            - A global load balancer with Cloud Armor in front of Cloud Run services that accept traffic only from it.
            - Encrypted data stores with backups; regional high availability when it is on.
            - Secrets in Secret Manager; one service account for the apps with narrow roles.
            - Alert policies; set `alarm_email` to receive them.

            ## What you must still decide

            - A domain name (set `domain` for a managed certificate; until then the load balancer serves HTTP).
            - Container images for each unit (nothing is built here).
        """, project=m.project, mode=m.mode, units=units, fp=m.fingerprint)


def generate(model):
    import iac_static
    if iac_static.has_static_only(model):
        return iac_static.gcp(model, Gcp(model).bootstrap())
    return Gcp(model).generate()
