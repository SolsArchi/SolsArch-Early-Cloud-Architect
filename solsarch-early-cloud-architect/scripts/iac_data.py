#!/usr/bin/env python3
"""Terraform for managed data platforms that sit beside any hosting provider: Neon (serverless PostgreSQL) and MongoDB Atlas.

Each is a standalone module in its own folder, so it works as the data tier of any plan (a provider chosen as an overlay
with --also, or as the database of an application platform), and the application finds it through its outputs.
"""
from iac_common import Files, T, fmt_hcl, tf


class Data:
    def __init__(self, model):
        self.m = model
        self.files = Files(model.fingerprint)
        self.ha = not model.single_az

    def neon(self, folder="terraform-neon"):
        m = self.m
        self.files.add(folder + "/versions.tf", tf(T("""
            terraform {
              required_version = ">= 1.9.0"

              required_providers {
                neon = {
                  source  = "kislerdm/neon"
                  version = "~> 0.9"
                }
              }
            }

            provider "neon" {}
        """)))
        self.files.add(folder + "/main.tf", tf(T("""
            variable "project_name" {
              description = "Short name used for the Neon project."
              type        = string
              default     = "@@project@@"
            }

            variable "region_id" {
              description = "Neon region, for example aws-us-east-2, aws-eu-central-1, azure-eastus2."
              type        = string
              default     = "aws-us-east-2"
            }

            variable "allowed_ips" {
              description = "Addresses allowed to connect. Leave empty to allow any address with the password."
              type        = list(string)
              default     = []
            }

            resource "neon_project" "main" {
              name                      = var.project_name
              region_id                 = var.region_id
              pg_version                = 17
              history_retention_seconds = @@retention@@
              allowed_ips               = var.allowed_ips
              store_password            = "yes"

              branch {
                name          = "main"
                database_name = "app"
                role_name     = "app"
              }

              autoscaling_limit_min_cu = @@min@@
              autoscaling_limit_max_cu = @@max@@
              suspend_timeout_seconds  = @@suspend@@
            }
        """, project=m.project, retention=604800 if self.ha else 86400, min=0.5 if self.ha else 0.25, max=4 if self.ha else 2,
                       suspend=0 if self.ha else 300)))
        self.files.add(folder + "/outputs.tf", tf(T("""
            output "database_host" {
              description = "Host to put in DB_HOST."
              value       = neon_project.main.database_host
            }

            output "database_name" {
              description = "The database name."
              value       = neon_project.main.database_name
            }

            output "database_user" {
              description = "The database role."
              value       = neon_project.main.database_user
            }

            output "database_password" {
              description = "The generated password. Put it in the host platform's secret store, not in a file."
              value       = neon_project.main.database_password
              sensitive   = true
            }

            output "connection_uri" {
              description = "Full connection string (contains the password)."
              value       = neon_project.main.connection_uri
              sensitive   = true
            }
        """)))
        self.files.add(folder + "/README.md", T("""
            # Neon database for @@project@@

            Generated from plan @@fp@@. Create an API key at https://console.neon.tech/app/settings/api-keys and export it as `NEON_API_KEY`,
            then `terraform init` and `terraform apply`. The connection string is in the `connection_uri` output (marked sensitive).

            Neon scales compute to zero when idle. The history window and minimum compute follow the high availability setting in your profile.
        """, project=m.project, fp=m.fingerprint))

    def mongodbatlas(self, folder="terraform-mongodbatlas"):
        m = self.m
        self.files.add(folder + "/versions.tf", tf(T("""
            terraform {
              required_version = ">= 1.9.0"

              required_providers {
                mongodbatlas = {
                  source  = "mongodb/mongodbatlas"
                  version = "~> 2.0"
                }
                random = {
                  source  = "hashicorp/random"
                  version = "~> 3.6"
                }
              }
            }

            provider "mongodbatlas" {}
        """)))
        self.files.add(folder + "/main.tf", tf(T("""
            variable "project_name" {
              description = "Short name used for the Atlas project and cluster."
              type        = string
              default     = "@@project@@"
            }

            variable "org_id" {
              description = "Your MongoDB Atlas organization id."
              type        = string
            }

            variable "provider_name" {
              description = "Cloud that hosts the cluster: AWS, AZURE or GCP."
              type        = string
              default     = "AWS"
            }

            variable "region_name" {
              description = "Atlas region name, for example US_EAST_1 (AWS), EASTUS2 (Azure) or CENTRAL_US (GCP)."
              type        = string
              default     = "US_EAST_1"
            }

            variable "allowed_cidrs" {
              description = "Addresses allowed to connect. Use your application's egress addresses; avoid 0.0.0.0/0."
              type        = list(string)
              default     = []
            }

            resource "mongodbatlas_project" "main" {
              name   = var.project_name
              org_id = var.org_id
            }

            resource "mongodbatlas_advanced_cluster" "main" {
              project_id             = mongodbatlas_project.main.id
              name                   = var.project_name
              cluster_type           = "REPLICASET"
              backup_enabled         = true
              pit_enabled            = @@pit@@
              termination_protection_enabled = true
              mongo_db_major_version = "8.0"

              replication_specs = [
                {
                  region_configs = [
                    {
                      provider_name = var.provider_name
                      region_name   = var.region_name
                      priority      = 7

                      electable_specs = {
                        instance_size = "@@size@@"
                        node_count    = 3
                      }

                      auto_scaling = {
                        disk_gb_enabled = true
                      }
                    }
                  ]
                }
              ]

              advanced_configuration = {
                minimum_enabled_tls_protocol = "TLS1_2"
                javascript_enabled           = false
              }
            }

            resource "mongodbatlas_project_ip_access_list" "app" {
              for_each   = toset(var.allowed_cidrs)
              project_id = mongodbatlas_project.main.id
              cidr_block = each.value
              comment    = "Application"
            }

            resource "random_password" "app" {
              length  = 32
              special = false
            }

            resource "mongodbatlas_database_user" "app" {
              project_id         = mongodbatlas_project.main.id
              username           = "app"
              password           = random_password.app.result
              auth_database_name = "admin"

              roles {
                role_name     = "readWrite"
                database_name = "app"
              }
            }
        """, project=m.project, pit="true" if self.ha else "false", size="M30" if self.ha else "M10")))
        self.files.add(folder + "/outputs.tf", tf(T("""
            output "connection_string" {
              description = "Connection string without credentials; add the user and password."
              value       = mongodbatlas_advanced_cluster.main.connection_strings.standard_srv
            }

            output "database_user" {
              description = "The application user."
              value       = mongodbatlas_database_user.app.username
            }

            output "database_password" {
              description = "The generated password. Put it in the host platform's secret store, not in a file."
              value       = random_password.app.result
              sensitive   = true
            }
        """)))
        self.files.add(folder + "/README.md", T("""
            # MongoDB Atlas for @@project@@

            Generated from plan @@fp@@. Create an organization API key and export `MONGODB_ATLAS_PUBLIC_KEY` and `MONGODB_ATLAS_PRIVATE_KEY`, put
            your organization id in `terraform.tfvars` as `org_id`, set `allowed_cidrs` to your application's egress addresses, then `terraform init` and `terraform apply`.

            The cluster has termination protection on. Turn it off on purpose before you destroy it.
        """, project=m.project, fp=m.fingerprint))


def generate(model, which):
    d = Data(model)
    getattr(d, which)()
    return d.files
