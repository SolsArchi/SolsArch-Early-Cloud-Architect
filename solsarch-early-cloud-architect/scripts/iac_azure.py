#!/usr/bin/env python3
"""Terraform for Azure, generated from the plan.

Containers run on Azure Container Apps behind an Application Gateway with a web application firewall (server model),
or as Azure Functions (serverless model). It follows the same baseline as every other provider: a private data tier,
a managed front door, encryption and backups on every data store, zone redundancy when high availability is on,
secrets in Key Vault, managed identities with narrow roles, logs and alarms, and health checks that use the path the
code really serves.
"""
import textwrap

from iac_common import Files, T, fmt_hcl, ident, tf

AZ_REGION_DEFAULT = "eastus"
PG_SKU = {"small": "B_Standard_B2s", "medium": "GP_Standard_D4ds_v5", "large": "GP_Standard_D8ds_v5"}
REDIS = {1: ("1", "C"), 4: ("2", "C")}
RUNTIME = {"python": ("python", "3.11"), "javascript": ("node", "20"), "typescript": ("node", "20"), "java": ("java", "17"),
           "go": ("custom", "1.0"), "rust": ("custom", "1.0"), "ruby": ("custom", "1.0"), "php": ("custom", "1.0")}


class Azure:
    def __init__(self, model):
        self.m = model
        self.files = Files(model.fingerprint)
        self.public = model.public_units()
        self.default_unit = self.public[0] if self.public else None
        self.use_edge = model.has("edge")
        self.functions = model.mode == "serverless"
        self.region = model.region or AZ_REGION_DEFAULT
        self.zones = not model.single_az

    # ------------------------------------------------------------- top level
    def generate(self):
        m = self.m
        self.versions()
        self.providers()
        self.variables()
        self.core()
        self.network()
        self.registry_and_identity()
        self.vault()
        self.data_stores()
        if self.functions:
            self.function_apps()
        else:
            self.container_apps()
            if self.public:
                self.gateway()
        if self.use_edge and self.public:
            self.front_door()
        self.monitoring()
        self.outputs()
        self.files.add("terraform/backend.tf", T("""
            terraform {
              backend "azurerm" {}
            }
        """))
        self.files.add("terraform/backend.hcl.example", T("""
            resource_group_name  = "REPLACE-with-the-state-resource-group"
            storage_account_name = "REPLACE-with-the-state-storage-account"
            container_name       = "tfstate"
            key                  = "@@project@@/production.tfstate"
            use_azuread_auth     = true
        """, project=m.project))
        self.files.add("terraform/bootstrap/main.tf", self.bootstrap())
        self.files.add("terraform/terraform.tfvars.example", fmt_hcl('project_name = "%s"\nenvironment  = "production"\nlocation     = "%s"\n' % (m.project, self.region)))
        self.files.add("terraform/README.md", self.readme())
        return self.files

    def versions(self):
        self.files.add("terraform/versions.tf", tf(T("""
            terraform {
              required_version = ">= 1.9.0"

              required_providers {
                azurerm = {
                  source  = "hashicorp/azurerm"
                  version = "~> 5.0"
                }
                random = {
                  source  = "hashicorp/random"
                  version = "~> 3.6"
                }
              }
            }
        """)))

    def providers(self):
        self.files.add("terraform/providers.tf", tf(T("""
            provider "azurerm" {
              features {
                key_vault {
                  purge_soft_delete_on_destroy = false
                }
              }
            }
        """)))

    def variables(self):
        m = self.m
        out = [T("""
            variable "project_name" {
              type        = string
              description = "Short name used in resource names and tags."
              default     = "@@project@@"
            }

            variable "environment" {
              type        = string
              description = "Deployment environment, for example production or staging."
              default     = "production"
            }

            variable "location" {
              type        = string
              description = "Azure region."
              default     = "@@region@@"
            }

            variable "vnet_cidr" {
              type        = string
              description = "Address range of the virtual network."
              default     = "10.50.0.0/16"
            }

            variable "image_tag" {
              type        = string
              description = "Tag of the container images in the registry created here."
              default     = "latest"
            }

            variable "log_retention_days" {
              type        = number
              description = "Days to keep application logs."
              default     = @@retention@@
            }

            variable "alarm_email" {
              type        = string
              description = "Email address that receives alarms. Leave null to create the action group without a receiver."
              default     = null
            }

            variable "enable_deletion_protection" {
              type        = bool
              description = "Protect data stores and the vault from deletion."
              default     = @@protect@@
            }
        """, project=m.project, region=self.region, retention=365 if m.regulated else 30, protect="false" if m.lean else "true")]
        if m.db_key:
            out.append(T("""
                variable "db_name" {
                  type        = string
                  description = "Name of the first database."
                  default     = "app"
                }

                variable "db_username" {
                  type        = string
                  description = "Administrator user name. The password is generated and kept in Key Vault."
                  default     = "appadmin"
                }
            """))
        for unit in m.units:
            if unit["kind"] == "static":
                continue
            n = ident(unit["name"])
            out.append(T("""
                variable "@@n@@_image" {
                  type        = string
                  description = "Container image for @@name@@. Leave null to use the registry created for it with the image_tag."
                  default     = null
                }

                variable "@@n@@_min_replicas" {
                  type        = number
                  description = "Smallest number of copies of @@name@@."
                  default     = @@count@@
                }
            """, n=n, name=unit["name"], count=1 if unit["kind"] == "worker" else m.replicas))
        self.files.add("terraform/variables.tf", tf(*out))

    def core(self):
        self.files.add("terraform/main.tf", tf(T("""
            data "azurerm_client_config" "current" {}

            resource "random_string" "suffix" {
              length  = 5
              upper   = false
              special = false
            }

            locals {
              name   = "${var.project_name}-${var.environment}"
              suffix = random_string.suffix.result
              tags = {
                Project     = var.project_name
                Environment = var.environment
                ManagedBy   = "terraform"
                GeneratedBy = "solsarch"
              }
            }

            resource "azurerm_resource_group" "main" {
              name     = "rg-${local.name}"
              location = var.location
              tags     = local.tags
            }

            resource "azurerm_log_analytics_workspace" "main" {
              name                = "log-${local.name}"
              location            = azurerm_resource_group.main.location
              resource_group_name = azurerm_resource_group.main.name
              sku                 = "PerGB2018"
              retention_in_days   = var.log_retention_days
              tags                = local.tags
            }
        """)))

    # --------------------------------------------------------------- network
    def network(self):
        m = self.m
        out = [T("""
            resource "azurerm_virtual_network" "main" {
              name                = "vnet-${local.name}"
              location            = azurerm_resource_group.main.location
              resource_group_name = azurerm_resource_group.main.name
              address_space       = [var.vnet_cidr]
              tags                = local.tags
            }

            resource "azurerm_subnet" "apps" {
              name                 = "snet-apps"
              resource_group_name  = azurerm_resource_group.main.name
              virtual_network_name = azurerm_virtual_network.main.name
              address_prefixes     = [cidrsubnet(var.vnet_cidr, 7, 0)]

              delegation {
                name = "container-apps"

                service_delegation {
                  name    = "Microsoft.App/environments"
                  actions = ["Microsoft.Network/virtualNetworks/subnets/join/action"]
                }
              }
            }

            resource "azurerm_subnet" "gateway" {
              name                 = "snet-gateway"
              resource_group_name  = azurerm_resource_group.main.name
              virtual_network_name = azurerm_virtual_network.main.name
              address_prefixes     = [cidrsubnet(var.vnet_cidr, 8, 4)]
            }

            resource "azurerm_subnet" "data" {
              name                 = "snet-data"
              resource_group_name  = azurerm_resource_group.main.name
              virtual_network_name = azurerm_virtual_network.main.name
              address_prefixes     = [cidrsubnet(var.vnet_cidr, 8, 5)]

              delegation {
                name = "postgres"

                service_delegation {
                  name    = "Microsoft.DBforPostgreSQL/flexibleServers"
                  actions = ["Microsoft.Network/virtualNetworks/subnets/join/action"]
                }
              }
            }

            resource "azurerm_subnet" "endpoints" {
              name                 = "snet-endpoints"
              resource_group_name  = azurerm_resource_group.main.name
              virtual_network_name = azurerm_virtual_network.main.name
              address_prefixes     = [cidrsubnet(var.vnet_cidr, 8, 6)]
            }

            resource "azurerm_network_security_group" "data" {
              name                = "nsg-data-${local.name}"
              location            = azurerm_resource_group.main.location
              resource_group_name = azurerm_resource_group.main.name
              tags                = local.tags
            }

            resource "azurerm_network_security_rule" "data_from_apps" {
              name                        = "allow-from-apps"
              priority                    = 100
              direction                   = "Inbound"
              access                      = "Allow"
              protocol                    = "Tcp"
              source_port_range           = "*"
              destination_port_ranges     = ["5432", "3306", "6379", "27017", "5671"]
              source_address_prefix       = azurerm_subnet.apps.address_prefixes[0]
              destination_address_prefix  = "*"
              resource_group_name         = azurerm_resource_group.main.name
              network_security_group_name = azurerm_network_security_group.data.name
            }

            resource "azurerm_network_security_rule" "data_deny_rest" {
              name                        = "deny-everything-else"
              priority                    = 4000
              direction                   = "Inbound"
              access                      = "Deny"
              protocol                    = "*"
              source_port_range           = "*"
              destination_port_range      = "*"
              source_address_prefix       = "*"
              destination_address_prefix  = "*"
              resource_group_name         = azurerm_resource_group.main.name
              network_security_group_name = azurerm_network_security_group.data.name
            }

            resource "azurerm_subnet_network_security_group_association" "data" {
              subnet_id                 = azurerm_subnet.data.id
              network_security_group_id = azurerm_network_security_group.data.id
            }

            resource "azurerm_private_dns_zone" "postgres" {
              name                = "privatelink.postgres.database.azure.com"
              resource_group_name = azurerm_resource_group.main.name
              tags                = local.tags
            }

            resource "azurerm_private_dns_zone_virtual_network_link" "postgres" {
              name                = "postgres-link"
              private_dns_zone_id = azurerm_private_dns_zone.postgres.id
              virtual_network_id  = azurerm_virtual_network.main.id
            }
        """)]
        out.append(T("""
            resource "azurerm_public_ip" "nat" {
              name                = "pip-nat-${local.name}"
              location            = azurerm_resource_group.main.location
              resource_group_name = azurerm_resource_group.main.name
              allocation_method   = "Static"
              sku                 = "Standard"
              tags                = local.tags
            }

            resource "azurerm_nat_gateway" "main" {
              name                = "nat-${local.name}"
              location            = azurerm_resource_group.main.location
              resource_group_name = azurerm_resource_group.main.name
              sku_name            = "Standard"
              tags                = local.tags
            }

            resource "azurerm_nat_gateway_public_ip_association" "main" {
              nat_gateway_id       = azurerm_nat_gateway.main.id
              public_ip_address_id = azurerm_public_ip.nat.id
            }

            resource "azurerm_subnet_nat_gateway_association" "apps" {
              subnet_id      = azurerm_subnet.apps.id
              nat_gateway_id = azurerm_nat_gateway.main.id
            }
        """))
        self.files.add("terraform/network.tf", tf(*out))

    # ------------------------------------------------- registry and identity
    def registry_and_identity(self):
        m = self.m
        self.files.add("terraform/registry.tf", tf(T("""
            resource "azurerm_container_registry" "main" {
              name                          = "cr${replace(var.project_name, "-", "")}${local.suffix}"
              resource_group_name           = azurerm_resource_group.main.name
              location                      = azurerm_resource_group.main.location
              sku                           = "Premium"
              admin_enabled                 = false
              public_network_access_enabled = true
              zone_redundancy_enabled       = @@zr@@
              tags                          = local.tags
            }

            resource "azurerm_user_assigned_identity" "apps" {
              name                = "id-apps-${local.name}"
              location            = azurerm_resource_group.main.location
              resource_group_name = azurerm_resource_group.main.name
              tags                = local.tags
            }

            resource "azurerm_role_assignment" "acr_pull" {
              scope                = azurerm_container_registry.main.id
              role_definition_name = "AcrPull"
              principal_id         = azurerm_user_assigned_identity.apps.principal_id
            }
        """, zr="true" if self.zones else "false")))

    def vault(self):
        self.files.add("terraform/vault.tf", tf(T("""
            resource "azurerm_key_vault" "main" {
              name                          = "kv-${substr(replace(var.project_name, "-", ""), 0, 12)}${local.suffix}"
              location                      = azurerm_resource_group.main.location
              resource_group_name           = azurerm_resource_group.main.name
              tenant_id                     = data.azurerm_client_config.current.tenant_id
              sku_name                      = "standard"
              rbac_authorization_enabled    = true
              purge_protection_enabled      = var.enable_deletion_protection
              soft_delete_retention_days    = 90
              public_network_access_enabled = true
              tags                          = local.tags

              network_acls {
                default_action = "Allow"
                bypass         = "AzureServices"
              }
            }

            resource "azurerm_role_assignment" "vault_admin" {
              scope                = azurerm_key_vault.main.id
              role_definition_name = "Key Vault Administrator"
              principal_id         = data.azurerm_client_config.current.object_id
            }

            resource "azurerm_role_assignment" "vault_read" {
              scope                = azurerm_key_vault.main.id
              role_definition_name = "Key Vault Secrets User"
              principal_id         = azurerm_user_assigned_identity.apps.principal_id
            }
        """)))

    # ------------------------------------------------------------ data stores
    def data_stores(self):
        m = self.m
        out = []
        if m.db_key == "database":
            out.append(T("""
                resource "random_password" "db" {
                  length  = 32
                  special = false
                }

                resource "azurerm_key_vault_secret" "db_password" {
                  name         = "db-password"
                  value        = random_password.db.result
                  key_vault_id = azurerm_key_vault.main.id

                  depends_on = [azurerm_role_assignment.vault_admin]
                }

                resource "azurerm_postgresql_flexible_server" "main" {
                  name                          = "psql-${local.name}-${local.suffix}"
                  resource_group_name           = azurerm_resource_group.main.name
                  location                      = azurerm_resource_group.main.location
                  version                       = "16"
                  delegated_subnet_id           = azurerm_subnet.data.id
                  private_dns_zone_id           = azurerm_private_dns_zone.postgres.id
                  public_network_access_enabled = false
                  administrator_login           = var.db_username
                  administrator_password        = random_password.db.result
                  sku_name                      = "@@sku@@"
                  storage_mb                    = @@storage@@
                  backup_retention_days         = 14
                  geo_redundant_backup_enabled  = @@geo@@
                  zone                          = "1"
                  tags                          = local.tags@@ha@@

                  depends_on = [azurerm_private_dns_zone_virtual_network_link.postgres]

                  lifecycle {
                    prevent_destroy = false
                  }
                }

                resource "azurerm_postgresql_flexible_server_database" "app" {
                  name      = var.db_name
                  server_id = azurerm_postgresql_flexible_server.main.id
                  charset   = "UTF8"
                  collation = "en_US.utf8"
                }
            """, sku=PG_SKU[m.sizing["db_class"]], storage=max(32768, m.sizing["storage_gb"] * 1024),
                         geo="true" if m.regulated else "false",
                         ha=("\n\n  high_availability {\n    mode                      = \"ZoneRedundant\"\n    standby_availability_zone = \"2\"\n  }" if self.zones else "")))
        if m.db_key == "database_mysql":
            out.append(T("""
                resource "random_password" "db" {
                  length  = 32
                  special = false
                }

                resource "azurerm_key_vault_secret" "db_password" {
                  name         = "db-password"
                  value        = random_password.db.result
                  key_vault_id = azurerm_key_vault.main.id

                  depends_on = [azurerm_role_assignment.vault_admin]
                }

                resource "azurerm_mysql_flexible_server" "main" {
                  name                         = "mysql-${local.name}-${local.suffix}"
                  resource_group_name          = azurerm_resource_group.main.name
                  location                     = azurerm_resource_group.main.location
                  version                      = "8.0.21"
                  delegated_subnet_id          = azurerm_subnet.data.id
                  administrator_login          = var.db_username
                  administrator_password       = random_password.db.result
                  sku_name                     = "@@sku@@"
                  backup_retention_days        = 14
                  geo_redundant_backup_enabled = @@geo@@
                  zone                         = "1"
                  tags                         = local.tags@@ha@@

                  storage {
                    size_gb = @@gb@@
                  }
                }

                resource "azurerm_mysql_flexible_database" "app" {
                  name                = var.db_name
                  resource_group_name = azurerm_resource_group.main.name
                  server_name         = azurerm_mysql_flexible_server.main.name
                  charset             = "utf8mb4"
                  collation           = "utf8mb4_unicode_ci"
                }
            """, sku=PG_SKU[m.sizing["db_class"]].replace("B_Standard_B2s", "B_Standard_B2s").replace("GP_Standard_D4ds_v5", "GP_Standard_D4ds_v4").replace("GP_Standard_D8ds_v5", "GP_Standard_D8ds_v4"),
                         gb=max(20, m.sizing["storage_gb"]), geo="true" if m.regulated else "false",
                         ha=("\n\n  high_availability {\n    mode                      = \"ZoneRedundant\"\n    standby_availability_zone = \"2\"\n  }" if self.zones else "")))
        if m.has("cache"):
            capacity, family = REDIS[1 if m.sizing["cache_gb"] <= 1 else 4]
            out.append(T("""
                resource "azurerm_redis_cache" "main" {
                  name                          = "redis-${local.name}-${local.suffix}"
                  location                      = azurerm_resource_group.main.location
                  resource_group_name           = azurerm_resource_group.main.name
                  capacity                      = @@cap@@
                  family                        = "@@family@@"
                  sku_name                      = "Standard"
                  non_ssl_port_enabled          = false
                  minimum_tls_version           = "1.2"
                  public_network_access_enabled = false
                  tags                          = local.tags

                  redis_configuration {
                    authentication_enabled = true
                  }
                }

                resource "azurerm_private_dns_zone" "redis" {
                  name                = "privatelink.redis.cache.windows.net"
                  resource_group_name = azurerm_resource_group.main.name
                  tags                = local.tags
                }

                resource "azurerm_private_dns_zone_virtual_network_link" "redis" {
                  name                = "redis-link"
                  private_dns_zone_id = azurerm_private_dns_zone.redis.id
                  virtual_network_id  = azurerm_virtual_network.main.id
                }

                resource "azurerm_private_endpoint" "redis" {
                  name                = "pe-redis-${local.name}"
                  location            = azurerm_resource_group.main.location
                  resource_group_name = azurerm_resource_group.main.name
                  subnet_id           = azurerm_subnet.endpoints.id
                  tags                = local.tags

                  private_service_connection {
                    name                           = "redis"
                    private_connection_resource_id = azurerm_redis_cache.main.id
                    subresource_names              = ["redisCache"]
                    is_manual_connection           = false
                  }

                  private_dns_zone_group {
                    name                 = "redis"
                    private_dns_zone_ids = [azurerm_private_dns_zone.redis.id]
                  }
                }
            """, cap=capacity, family=family))
        if m.has("storage") or m.has("static_site"):
            out.append(T("""
                resource "azurerm_storage_account" "app" {
                  name                            = "st${substr(replace(var.project_name, "-", ""), 0, 14)}${local.suffix}"
                  resource_group_name             = azurerm_resource_group.main.name
                  location                        = azurerm_resource_group.main.location
                  account_tier                    = "Standard"
                  account_replication_type        = "@@repl@@"
                  min_tls_version                 = "TLS1_2"
                  infrastructure_encryption_enabled = true
                  allow_nested_items_to_be_public = false
                  https_traffic_only_enabled      = true
                  tags                            = local.tags

                  blob_properties {
                    versioning_enabled = true

                    delete_retention_policy {
                      days = 14
                    }
                  }
                }

                resource "azurerm_storage_container" "app" {
                  name                  = "app"
                  storage_account_id    = azurerm_storage_account.app.id
                  container_access_type = "private"
                }

                resource "azurerm_role_assignment" "storage_blob" {
                  scope                = azurerm_storage_account.app.id
                  role_definition_name = "Storage Blob Data Contributor"
                  principal_id         = azurerm_user_assigned_identity.apps.principal_id
                }
            """, repl="ZRS" if self.zones else "LRS"))
        if m.has("queue_kafka"):
            out.append(T("""
                resource "azurerm_eventhub_namespace" "main" {
                  name                          = "evh-${local.name}-${local.suffix}"
                  location                      = azurerm_resource_group.main.location
                  resource_group_name           = azurerm_resource_group.main.name
                  sku                           = "Standard"
                  capacity                      = 1
                  minimum_tls_version           = "1.2"
                  public_network_access_enabled = true
                  tags                          = local.tags
                }

                resource "azurerm_eventhub" "main" {
                  name              = "events"
                  namespace_id      = azurerm_eventhub_namespace.main.id
                  partition_count   = 4
                  message_retention = 1
                }
            """))
        if m.has("database_mongo"):
            out.append(T("""
                resource "azurerm_cosmosdb_account" "mongo" {
                  name                          = "cosmos-${local.name}-${local.suffix}"
                  location                      = azurerm_resource_group.main.location
                  resource_group_name           = azurerm_resource_group.main.name
                  offer_type                    = "Standard"
                  kind                          = "MongoDB"
                  mongo_server_version          = "7.0"
                  public_network_access_enabled = false
                  tags                          = local.tags

                  consistency_policy {
                    consistency_level = "Session"
                  }

                  geo_location {
                    location          = azurerm_resource_group.main.location
                    failover_priority = 0
                    zone_redundant    = @@zr@@
                  }

                  backup {
                    type = "Continuous"
                    tier = "Continuous7Days"
                  }

                  capabilities {
                    name = "EnableMongo"
                  }
                }
            """, zr="true" if self.zones else "false"))
        if out:
            self.files.add("terraform/data.tf", tf(*out))

    # ---------------------------------------------------- Container Apps
    def container_apps(self):
        m = self.m
        cpu = float(m.sizing["unit_vcpu"])
        mem = "%.1fGi" % float(m.sizing["unit_memory_gb"])
        out = [T("""
            resource "azurerm_container_app_environment" "main" {
              name                           = "cae-${local.name}"
              location                       = azurerm_resource_group.main.location
              resource_group_name            = azurerm_resource_group.main.name
              log_analytics_workspace_id     = azurerm_log_analytics_workspace.main.id
              infrastructure_subnet_id       = azurerm_subnet.apps.id
              zone_redundancy_enabled        = @@zr@@
              internal_load_balancer_enabled = false
              tags                           = local.tags

              workload_profile {
                name                  = "Consumption"
                workload_profile_type = "Consumption"
              }
            }
        """, zr="true" if self.zones else "false")]
        for unit in m.units:
            if unit["kind"] == "static":
                continue
            n, name = ident(unit["name"]), unit["name"]
            port = unit["port"] or 8080
            health = unit["health_path"] or "/"
            is_public = unit in self.public
            env = self.env_block()
            ingress = ""
            if is_public:
                ingress = T("""

                    ingress {
                      external_enabled = true
                      target_port      = @@port@@
                      transport        = "auto"

                      traffic_weight {
                        latest_revision = true
                        percentage      = 100
                      }

                      ip_security_restriction {
                        name             = "application-gateway"
                        action           = "Allow"
                        ip_address_range = "${azurerm_public_ip.gateway.ip_address}/32"
                      }
                    }
                """, port=port).rstrip("\n")
            probes = ""
            if unit["kind"] != "worker":
                probes = "\n\n" + T("""

                      liveness_probe {
                        transport = "HTTP"
                        path      = "@@health@@"
                        port      = @@port@@
                      }

                      readiness_probe {
                        transport = "HTTP"
                        path      = "@@health@@"
                        port      = @@port@@
                      }
                """, health=health, port=port).rstrip("\n")
            scale = "\n\n" + T("""

                  http_scale_rule {
                    name                = "http"
                    concurrent_requests = "50"
                  }
            """).rstrip("\n") if unit["kind"] != "worker" else ""
            out.append(T("""
                resource "azurerm_container_app" "@@n@@" {
                  name                         = "@@cname@@"
                  container_app_environment_id = azurerm_container_app_environment.main.id
                  resource_group_name          = azurerm_resource_group.main.name
                  revision_mode                = "Single"
                  workload_profile_name        = "Consumption"
                  tags                         = local.tags

                  identity {
                    type         = "UserAssigned"
                    identity_ids = [azurerm_user_assigned_identity.apps.id]
                  }

                  registry {
                    server   = azurerm_container_registry.main.login_server
                    identity = azurerm_user_assigned_identity.apps.id
                  }@@secrets@@@@ingress@@

                  template {
                    min_replicas = var.@@n@@_min_replicas
                    max_replicas = var.@@n@@_min_replicas * 4

                    container {
                      name   = "@@name@@"
                      image  = coalesce(var.@@n@@_image, "${azurerm_container_registry.main.login_server}/@@name@@:${var.image_tag}")
                      cpu    = @@cpu@@
                      memory = "@@mem@@"@@env@@@@probes@@
                    }@@scale@@
                  }

                  depends_on = [azurerm_role_assignment.acr_pull]
                }
            """, n=n, name=name, cname=name[:30], cpu=cpu, mem=mem, env=env, ingress=("\n\n" + ingress) if ingress else "",
                         probes=probes, scale=scale, secrets=self.secret_block()))
        self.files.add("terraform/compute.tf", tf(*out))

    def secret_block(self):
        m = self.m
        if not (m.db_key == "database" or m.db_key == "database_mysql"):
            return ""
        return "\n\n" + T("""
            secret {
              name                = "db-password"
              key_vault_secret_id = azurerm_key_vault_secret.db_password.versionless_id
              identity            = azurerm_user_assigned_identity.apps.id
            }
        """).rstrip("\n")

    def env_block(self):
        m = self.m
        items = [("PORT", "value", '"8080"'), ("APP_ENV", "value", "var.environment")]
        if m.db_key == "database":
            items += [("DB_HOST", "value", "azurerm_postgresql_flexible_server.main.fqdn"), ("DB_PORT", "value", '"5432"'),
                      ("DB_NAME", "value", "var.db_name"), ("DB_USER", "value", "var.db_username"), ("DB_PASSWORD", "secret_name", '"db-password"')]
        if m.db_key == "database_mysql":
            items += [("DB_HOST", "value", "azurerm_mysql_flexible_server.main.fqdn"), ("DB_PORT", "value", '"3306"'),
                      ("DB_NAME", "value", "var.db_name"), ("DB_USER", "value", "var.db_username"), ("DB_PASSWORD", "secret_name", '"db-password"')]
        if m.has("cache"):
            items += [("REDIS_HOST", "value", "azurerm_redis_cache.main.hostname"), ("REDIS_PORT", "value", '"6380"')]
        if m.has("storage"):
            items.append(("STORAGE_ACCOUNT", "value", "azurerm_storage_account.app.name"))
        if m.has("queue_kafka"):
            items.append(("KAFKA_BOOTSTRAP_SERVERS", "value", '"${azurerm_eventhub_namespace.main.name}.servicebus.windows.net:9093"'))
        blocks = []
        for name, kind, value in items:
            blocks.append("\n\n      env {\n        name  = \"%s\"\n        %s = %s\n      }" % (name, kind, value) if kind == "value" else
                          "\n\n      env {\n        name        = \"%s\"\n        secret_name = %s\n      }" % (name, value))
        return "".join(blocks)

    # ----------------------------------------------------- Application Gateway
    def gateway(self):
        m = self.m
        d = self.default_unit
        backends, probes, settings, rules, listeners = [], [], [], [], []
        for unit in self.public:
            n = ident(unit["name"])
            backends.append(T("""
                backend_address_pool {
                  name  = "@@n@@"
                  fqdns = [azurerm_container_app.@@n@@.ingress[0].fqdn]
                }
            """, n=n))
            probes.append(T("""
                probe {
                  name                                      = "@@n@@"
                  protocol                                  = "Https"
                  path                                      = "@@health@@"
                  interval                                  = 30
                  timeout                                   = 30
                  unhealthy_threshold                       = 3
                  pick_host_name_from_backend_http_settings = true
                  match {
                    status_code = ["200-399"]
                  }
                }
            """, n=n, health=unit["health_path"] or "/"))
            settings.append(T("""
                backend_http_settings {
                  name                                = "@@n@@"
                  cookie_based_affinity               = "Disabled"
                  port                                = 443
                  protocol                            = "Https"
                  request_timeout                     = @@timeout@@
                  probe_name                          = "@@n@@"
                  pick_host_name_from_backend_address = true
                }
            """, n=n, timeout=300 if m.streaming else 60))
        path_rules = []
        for unit in self.public:
            if unit is d:
                continue
            n = ident(unit["name"])
            prefix = "/api/*" if unit["kind"] == "api" or unit["name"] == "api" else "/%s/*" % unit["name"]
            path_rules.append(T("""
                path_rule {
                  name                       = "@@n@@"
                  paths                      = ["@@p@@"]
                  backend_address_pool_name  = "@@n@@"
                  backend_http_settings_name = "@@n@@"
                }
            """, n=n, p=prefix))
        dn = ident(d["name"])
        if path_rules:
            routing = T("""
                url_path_map {
                  name                               = "routes"
                  default_backend_address_pool_name  = "@@dn@@"
                  default_backend_http_settings_name = "@@dn@@"
                  @@pathrules@@
                }

                request_routing_rule {
                  name               = "routes"
                  rule_type          = "PathBasedRouting"
                  http_listener_name = "http"
                  url_path_map_name  = "routes"
                  priority           = 100
                }
            """, dn=dn, pathrules="\n".join(path_rules))
        else:
            routing = T("""
                request_routing_rule {
                  name                       = "routes"
                  rule_type                  = "Basic"
                  http_listener_name         = "http"
                  backend_address_pool_name  = "@@dn@@"
                  backend_http_settings_name = "@@dn@@"
                  priority                   = 100
                }
            """, dn=dn)
        body = T("""
            resource "azurerm_public_ip" "gateway" {
              name                = "pip-gw-${local.name}"
              location            = azurerm_resource_group.main.location
              resource_group_name = azurerm_resource_group.main.name
              allocation_method   = "Static"
              sku                 = "Standard"
              zones               = @@zones@@
              tags                = local.tags
            }

            resource "azurerm_web_application_firewall_policy" "gateway" {
              name                = "waf-${local.name}"
              resource_group_name = azurerm_resource_group.main.name
              location            = azurerm_resource_group.main.location
              tags                = local.tags

              policy_settings {
                enabled = true
                mode    = "Prevention"
              }

              managed_rules {
                managed_rule_set {
                  type    = "OWASP"
                  version = "3.2"
                }
              }
            }

            resource "azurerm_application_gateway" "main" {
              name                = "agw-${local.name}"
              location            = azurerm_resource_group.main.location
              resource_group_name = azurerm_resource_group.main.name
              firewall_policy_id  = azurerm_web_application_firewall_policy.gateway.id
              zones               = @@zones@@
              tags                = local.tags

              sku {
                name = "WAF_v2"
                tier = "WAF_v2"
              }

              autoscale_configuration {
                min_capacity = 1
                max_capacity = 5
              }

              gateway_ip_configuration {
                name      = "gateway"
                subnet_id = azurerm_subnet.gateway.id
              }

              frontend_port {
                name = "http"
                port = 80
              }

              frontend_ip_configuration {
                name                 = "public"
                public_ip_address_id = azurerm_public_ip.gateway.id
              }

              ssl_policy {
                policy_type = "Predefined"
                policy_name = "AppGwSslPolicy20220101"
              }

@@backends@@

@@probes@@

@@settings@@

              http_listener {
                name                           = "http"
                frontend_ip_configuration_name = "public"
                frontend_port_name             = "http"
                protocol                       = "Http"
              }

@@routing@@
            }
        """, zones='["1", "2", "3"]' if self.zones else '["1"]', backends="\n".join(backends), probes="\n".join(probes),
                 settings="\n".join(settings), routing=routing)
        self.files.add("terraform/gateway.tf", tf(body))

    # ------------------------------------------------------------ Front Door
    def front_door(self):
        self.files.add("terraform/edge.tf", tf(T("""
            resource "azurerm_cdn_frontdoor_profile" "main" {
              name                = "afd-${local.name}"
              resource_group_name = azurerm_resource_group.main.name
              sku_name            = "Standard_AzureFrontDoor"
              tags                = local.tags
            }

            resource "azurerm_cdn_frontdoor_endpoint" "main" {
              name                     = "ep-${local.name}"
              cdn_frontdoor_profile_id = azurerm_cdn_frontdoor_profile.main.id
              tags                     = local.tags
            }

            resource "azurerm_cdn_frontdoor_origin_group" "gateway" {
              name                     = "gateway"
              cdn_frontdoor_profile_id = azurerm_cdn_frontdoor_profile.main.id

              load_balancing {
                sample_size                 = 4
                successful_samples_required = 3
              }

              health_probe {
                path                = "@@health@@"
                protocol            = "Http"
                interval_in_seconds = 60
                request_type        = "GET"
              }
            }

            resource "azurerm_cdn_frontdoor_origin" "gateway" {
              name                           = "gateway"
              cdn_frontdoor_origin_group_id  = azurerm_cdn_frontdoor_origin_group.gateway.id
              enabled                        = true
              host_name                      = @@origin@@
              origin_host_header             = @@origin@@
              http_port                      = 80
              https_port                     = 443
              certificate_name_check_enabled = false
              priority                       = 1
              weight                         = 1000
            }

            resource "azurerm_cdn_frontdoor_route" "main" {
              name                          = "default"
              cdn_frontdoor_endpoint_id     = azurerm_cdn_frontdoor_endpoint.main.id
              cdn_frontdoor_origin_group_id = azurerm_cdn_frontdoor_origin_group.gateway.id
              cdn_frontdoor_origin_ids      = [azurerm_cdn_frontdoor_origin.gateway.id]
              supported_protocols           = ["Http", "Https"]
              patterns_to_match             = ["/*"]
              forwarding_protocol           = "HttpOnly"
              https_redirect_enabled        = true
              link_to_default_domain        = true
            }

            resource "azurerm_cdn_frontdoor_firewall_policy" "main" {
              name                = "afdwaf${replace(var.project_name, "-", "")}${local.suffix}"
              resource_group_name = azurerm_resource_group.main.name
              sku_name            = azurerm_cdn_frontdoor_profile.main.sku_name
              enabled             = true
              mode                = "Prevention"
              tags                = local.tags

              custom_rule {
                name                           = "RateLimit"
                enabled                        = true
                priority                       = 1
                type                           = "RateLimitRule"
                action                         = "Block"
                rate_limit_duration_in_minutes = 1
                rate_limit_threshold           = 1000

                match_condition {
                  match_variable = "RequestUri"
                  operator       = "Contains"
                  match_values   = ["/"]
                }
              }
            }

            resource "azurerm_cdn_frontdoor_security_policy" "main" {
              name                     = "waf"
              cdn_frontdoor_profile_id = azurerm_cdn_frontdoor_profile.main.id

              security_policies {
                firewall {
                  cdn_frontdoor_firewall_policy_id = azurerm_cdn_frontdoor_firewall_policy.main.id

                  association {
                    patterns_to_match = ["/*"]

                    domain {
                      cdn_frontdoor_domain_id = azurerm_cdn_frontdoor_endpoint.main.id
                    }
                  }
                }
              }
            }
        """, health=(self.default_unit or {}).get("health_path") or "/",
                 origin=('replace(azurerm_api_management.main.gateway_url, "https://", "")' if self.functions
                         else "azurerm_public_ip.gateway.ip_address"))))

    # ------------------------------------------------------------- Functions
    def function_apps(self):
        m = self.m
        out = [T("""
            resource "azurerm_storage_account" "functions" {
              name                            = "stfn${substr(replace(var.project_name, "-", ""), 0, 12)}${local.suffix}"
              resource_group_name             = azurerm_resource_group.main.name
              location                        = azurerm_resource_group.main.location
              account_tier                    = "Standard"
              account_replication_type        = "LRS"
              min_tls_version                 = "TLS1_2"
              allow_nested_items_to_be_public = false
              shared_access_key_enabled       = false
              tags                            = local.tags
            }

            resource "azurerm_storage_container" "functions" {
              name                  = "deployments"
              storage_account_id    = azurerm_storage_account.functions.id
              container_access_type = "private"
            }

            resource "azurerm_service_plan" "functions" {
              name                = "plan-${local.name}"
              resource_group_name = azurerm_resource_group.main.name
              location            = azurerm_resource_group.main.location
              os_type             = "Linux"
              sku_name            = "FC1"
              tags                = local.tags
            }

            resource "azurerm_application_insights" "main" {
              name                = "appi-${local.name}"
              location            = azurerm_resource_group.main.location
              resource_group_name = azurerm_resource_group.main.name
              workspace_id        = azurerm_log_analytics_workspace.main.id
              application_type    = "web"
              tags                = local.tags
            }
        """)]
        for unit in m.units:
            if unit["kind"] == "static":
                continue
            n, name = ident(unit["name"]), unit["name"]
            langs = unit.get("languages") or ["python"]
            runtime, version = RUNTIME.get(langs[0], ("python", "3.11"))
            out.append(T("""
                resource "azurerm_function_app_flex_consumption" "@@n@@" {
                  name                        = "func-${local.name}-@@name@@-${local.suffix}"
                  resource_group_name         = azurerm_resource_group.main.name
                  location                    = azurerm_resource_group.main.location
                  service_plan_id             = azurerm_service_plan.functions.id
                  storage_container_type      = "blobContainer"
                  storage_container_endpoint  = "${azurerm_storage_account.functions.primary_blob_endpoint}${azurerm_storage_container.functions.name}"
                  storage_authentication_type = "SystemAssignedIdentity"
                  runtime_name                = "@@runtime@@"
                  runtime_version             = "@@version@@"
                  maximum_instance_count      = 100
                  instance_memory_in_mb       = @@mem@@
                  https_only                  = true
                  tags                        = local.tags

                  identity {
                    type = "SystemAssigned"
                  }

                  site_config {
                    application_insights_connection_string = azurerm_application_insights.main.connection_string
                  }

                  app_settings = {
                    APP_ENV = var.environment
                  }
                }

                resource "azurerm_role_assignment" "@@n@@_deploy" {
                  scope                = azurerm_storage_account.functions.id
                  role_definition_name = "Storage Blob Data Contributor"
                  principal_id         = azurerm_function_app_flex_consumption.@@n@@.identity[0].principal_id
                }
            """, n=n, name=name, runtime=runtime, version=version, mem=2048 if m.sizing["unit_memory_gb"] > 1 else 512))
        if self.public:
            out.append(T("""
                resource "azurerm_api_management" "main" {
                  name                = "apim-${local.name}-${local.suffix}"
                  location            = azurerm_resource_group.main.location
                  resource_group_name = azurerm_resource_group.main.name
                  publisher_name      = var.project_name
                  publisher_email     = coalesce(var.alarm_email, "admin@example.com")
                  sku_name            = "Consumption_0"
                  tags                = local.tags
                }
            """))
        self.files.add("terraform/compute.tf", tf(*out))

    # ------------------------------------------------------------ monitoring
    def monitoring(self):
        m = self.m
        out = [T("""
            resource "azurerm_monitor_action_group" "main" {
              name                = "ag-${local.name}"
              resource_group_name = azurerm_resource_group.main.name
              short_name          = "alerts"
              tags                = local.tags

              dynamic "email_receiver" {
                for_each = var.alarm_email == null ? [] : [var.alarm_email]

                content {
                  name          = "owner"
                  email_address = email_receiver.value
                }
              }
            }
        """)]
        if not self.functions:
            for unit in m.units:
                if unit["kind"] == "static":
                    continue
                n, name = ident(unit["name"]), unit["name"]
                out.append(T("""
                    resource "azurerm_monitor_metric_alert" "@@n@@_cpu" {
                      name                = "@@name@@-cpu-high"
                      resource_group_name = azurerm_resource_group.main.name
                      scopes              = [azurerm_container_app.@@n@@.id]
                      description         = "CPU use of @@name@@ is high."
                      severity            = 2
                      frequency           = "PT5M"
                      window_size         = "PT15M"
                      tags                = local.tags

                      criteria {
                        metric_namespace = "Microsoft.App/containerApps"
                        metric_name      = "UsageNanoCores"
                        aggregation      = "Average"
                        operator         = "GreaterThan"
                        threshold        = 850000000
                      }

                      action {
                        action_group_id = azurerm_monitor_action_group.main.id
                      }
                    }
                """, n=n, name=name))
        if m.db_key == "database":
            out.append(T("""
                resource "azurerm_monitor_metric_alert" "db_cpu" {
                  name                = "database-cpu-high"
                  resource_group_name = azurerm_resource_group.main.name
                  scopes              = [azurerm_postgresql_flexible_server.main.id]
                  description         = "Database CPU is high."
                  severity            = 2
                  frequency           = "PT5M"
                  window_size         = "PT15M"
                  tags                = local.tags

                  criteria {
                    metric_namespace = "Microsoft.DBforPostgreSQL/flexibleServers"
                    metric_name      = "cpu_percent"
                    aggregation      = "Average"
                    operator         = "GreaterThan"
                    threshold        = 80
                  }

                  action {
                    action_group_id = azurerm_monitor_action_group.main.id
                  }
                }
            """))
        if self.public and not self.functions:
            out.append(T("""
                resource "azurerm_monitor_metric_alert" "gateway_5xx" {
                  name                = "gateway-5xx"
                  resource_group_name = azurerm_resource_group.main.name
                  scopes              = [azurerm_application_gateway.main.id]
                  description         = "The gateway returns server errors."
                  severity            = 2
                  frequency           = "PT5M"
                  window_size         = "PT15M"
                  tags                = local.tags

                  criteria {
                    metric_namespace = "Microsoft.Network/applicationGateways"
                    metric_name      = "ResponseStatus"
                    aggregation      = "Total"
                    operator         = "GreaterThan"
                    threshold        = 10

                    dimension {
                      name     = "HttpStatusGroup"
                      operator = "Include"
                      values   = ["5xx"]
                    }
                  }

                  action {
                    action_group_id = azurerm_monitor_action_group.main.id
                  }
                }
            """))
        self.files.add("terraform/monitoring.tf", tf(*out))

    def outputs(self):
        m = self.m
        out = [T("""
            output "resource_group" {
              description = "Resource group that holds everything."
              value       = azurerm_resource_group.main.name
            }

            output "registry_login_server" {
              description = "Push container images here."
              value       = azurerm_container_registry.main.login_server
            }

            output "key_vault_uri" {
              description = "Vault that holds the generated secrets."
              value       = azurerm_key_vault.main.vault_uri
            }
        """)]
        if self.use_edge and self.public:
            out.append(T("""
                output "front_door_hostname" {
                  description = "Public address of the application through Front Door."
                  value       = azurerm_cdn_frontdoor_endpoint.main.host_name
                }
            """))
        if self.public and not self.functions:
            out.append(T("""
                output "gateway_ip" {
                  description = "Public address of the Application Gateway."
                  value       = azurerm_public_ip.gateway.ip_address
                }
            """))
        self.files.add("terraform/outputs.tf", tf(*out))

    def bootstrap(self):
        return tf(T("""
            terraform {
              required_version = ">= 1.9.0"

              required_providers {
                azurerm = {
                  source  = "hashicorp/azurerm"
                  version = "~> 5.0"
                }
              }
            }

            variable "location" {
              type        = string
              description = "Region of the state storage."
              default     = "@@region@@"
            }

            provider "azurerm" {
              features {}
            }

            resource "azurerm_resource_group" "state" {
              name     = "rg-@@project@@-tfstate"
              location = var.location
            }

            resource "azurerm_storage_account" "state" {
              name                            = "st@@short@@tfstate"
              resource_group_name             = azurerm_resource_group.state.name
              location                        = azurerm_resource_group.state.location
              account_tier                    = "Standard"
              account_replication_type        = "GRS"
              min_tls_version                 = "TLS1_2"
              allow_nested_items_to_be_public = false

              blob_properties {
                versioning_enabled = true
              }
            }

            resource "azurerm_storage_container" "state" {
              name                  = "tfstate"
              storage_account_id    = azurerm_storage_account.state.id
              container_access_type = "private"
            }

            output "storage_account" {
              description = "Put this in backend.hcl as storage_account_name."
              value       = azurerm_storage_account.state.name
            }
        """, region=self.region, project=self.m.project, short=self.m.project.replace("-", "")[:14]))

    def readme(self):
        m = self.m
        units = ", ".join(u["name"] for u in m.units if u["kind"] != "static")
        return T("""
            # Terraform for @@project@@ on Azure

            Compute model: @@mode@@. Units: @@units@@. Plan fingerprint @@fp@@.

            ## Order of work

            1. `az login`, then `cd bootstrap && terraform init && terraform apply` once. It creates the storage account that keeps Terraform state.
            2. Copy `backend.hcl.example` to `backend.hcl` and fill it in.
            3. `terraform init -backend-config=backend.hcl`
            4. `terraform apply`. The first apply creates everything, including the empty container registry.
            5. Build and push each image to the registry in the outputs, then run `terraform apply` again.

            ## What is configured for you

            - A virtual network with separate subnets for apps, the gateway and the data tier; the data tier accepts traffic only from the apps subnet.
            - Application Gateway with a web application firewall in front of the container apps, which accept traffic only from the gateway.
            - Encrypted data stores with backups; zone redundancy when high availability is on.
            - Secrets in Key Vault with role-based access; managed identities for the apps.
            - Alerts to an action group; set `alarm_email` to subscribe.

            ## What you must still decide

            - A domain name and a certificate (the gateway listens on HTTP until you add one).
            - Container images for each unit (nothing is built here).
        """, project=m.project, mode=m.mode, units=units, fp=m.fingerprint)


def generate(model):
    import iac_static
    if iac_static.has_static_only(model):
        return iac_static.azure(model, Azure(model).bootstrap())
    return Azure(model).generate()
