#!/usr/bin/env python3
"""Terraform for Vultr, generated from the plan.

Containers run on Vultr Kubernetes Engine inside a private VPC, with the managed database on the same VPC, Object Storage
for files, and the cluster firewall turned on. Redis, MongoDB and RabbitMQ run inside the cluster from ../kubernetes when
the plan marks them self-run. Vultr plan names change, so the defaults are the common ones and the README says how to list
the current ones before you apply.
"""
from iac_common import Files, T, fmt_hcl, selfrun, tf

DEFAULT_REGION = "ewr"
NODE_PLANS = [(1.5, "vc2-2c-4gb"), (3.0, "vc2-4c-8gb"), (6.0, "vc2-6c-16gb"), (1e9, "vc2-8c-32gb")]


class Vultr:
    def __init__(self, model):
        self.m = model
        self.files = Files(model.fingerprint)
        self.region = model.region if model.region and len(model.region) <= 4 and model.region.isalpha() else DEFAULT_REGION
        self.ha = not model.single_az
        self.managed_db = model.db_key is not None and not selfrun(model, model.db_key)
        self.storage = model.has("storage")
        need = sum(float(model.sizing["unit_vcpu"]) for u in model.units if u["kind"] != "static") * model.replicas / 2
        self.node_plan = next(t for limit, t in NODE_PLANS if need <= limit)

    def generate(self):
        m = self.m
        self.versions()
        self.variables()
        self.cluster()
        if self.managed_db:
            self.database()
        if self.storage:
            self.object_storage()
        self.outputs()
        self.files.add("terraform/backend.tf", T("""
            terraform {
              backend "s3" {}
            }
        """))
        self.files.add("terraform/backend.hcl.example", T("""
            # Vultr Object Storage speaks the S3 protocol. Create a bucket in the customer portal, then fill this in.
            bucket                      = "REPLACE-with-your-state-bucket"
            key                         = "@@project@@/production.tfstate"
            region                      = "us-east-1"
            endpoints                   = { s3 = "https://@@region@@1.vultrobjects.com" }
            skip_credentials_validation = true
            skip_region_validation      = true
            skip_requesting_account_id  = true
            skip_metadata_api_check     = true
            skip_s3_checksum            = true
            use_path_style              = true
        """, project=m.project, region=self.region))
        self.files.add("terraform/terraform.tfvars.example", fmt_hcl(
            'project_name = "%s"\nenvironment  = "production"\nregion       = "%s"\n' % (m.project, self.region)))
        self.files.add("terraform/README.md", self.readme())
        return self.files

    def versions(self):
        self.files.add("terraform/versions.tf", tf(T("""
            terraform {
              required_version = ">= 1.9.0"

              required_providers {
                vultr = {
                  source  = "vultr/vultr"
                  version = "~> 2.30"
                }
              }
            }

            provider "vultr" {}
        """)))

    def variables(self):
        db = ""
        if self.managed_db:
            db = T("""
                variable "database_plan" {
                  description = "Managed database plan. List current ones with: vultr-cli database plan list"
                  type        = string
                  default     = "vultr-dbaas-startup-cc-1-55-2"
                }
            """)
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
              description = "Vultr region id, for example ewr, ams, sgp."
              type        = string
              default     = "@@region@@"
            }

            variable "kubernetes_version" {
              description = "Kubernetes version. Leave null to use the newest one Vultr offers."
              type        = string
              default     = null
            }

            variable "node_plan" {
              description = "Vultr plan for each cluster node."
              type        = string
              default     = "@@plan@@"
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
        """, region=self.region, plan=self.node_plan, min=2 if self.ha else 1, max=6 if self.ha else 3), db))

    def cluster(self):
        self.files.add("terraform/cluster.tf", tf(T("""
            locals {
              name = "${var.project_name}-${var.environment}"
              tags = [var.project_name, var.environment, "solsarch"]
            }

            data "vultr_kubernetes_version" "current" {}

            resource "vultr_vpc" "main" {
              region         = var.region
              description    = "Private network for ${local.name}"
              v4_subnet      = "10.20.0.0"
              v4_subnet_mask = 24
            }

            resource "vultr_kubernetes" "main" {
              region          = var.region
              label           = local.name
              version         = coalesce(var.kubernetes_version, data.vultr_kubernetes_version.current.latest)
              vpc_id          = vultr_vpc.main.id
              enable_firewall = true
              ha_controlplanes = @@ha@@

              node_pools {
                label         = "${local.name}-main"
                plan          = var.node_plan
                node_quantity = var.node_min
                auto_scaler   = true
                min_nodes     = var.node_min
                max_nodes     = var.node_max
              }
            }
        """, ha="true" if self.ha else "false")))

    def database(self):
        m = self.m
        engine, version = ("pg", "16") if m.db_key == "database" else ("mysql", "8")
        self.files.add("terraform/database.tf", tf(T("""
            resource "vultr_database" "main" {
              database_engine         = "@@engine@@"
              database_engine_version = "@@version@@"
              region                  = var.region
              plan                    = var.database_plan
              plan_replicas           = @@replicas@@
              label                   = "${local.name}-db"
              vpc_id                  = vultr_vpc.main.id
              trusted_ips             = ["10.20.0.0/24"]
              tag                     = var.project_name

              lifecycle {
                prevent_destroy = true
              }
            }
        """, engine=engine, version=version, replicas=1 if self.ha else 0)))

    def object_storage(self):
        self.files.add("terraform/storage.tf", tf(T("""
            data "vultr_object_storage_cluster" "main" {
              filter {
                name   = "region"
                values = [var.region]
              }
            }

            data "vultr_object_storage_tier" "main" {
              filter {
                name   = "slug"
                values = [var.object_storage_tier]
              }
            }

            variable "object_storage_tier" {
              description = "Object Storage tier slug. List them with: vultr-cli object-storage tiers"
              type        = string
              default     = "standard"
            }

            resource "vultr_object_storage" "assets" {
              cluster_id = data.vultr_object_storage_cluster.main.id
              tier_id    = data.vultr_object_storage_tier.main.id
              label      = "${local.name}-assets"
            }
        """)))

    def outputs(self):
        parts = [T("""
            data "vultr_kubernetes_kubeconfig" "main" {
              cluster_id = vultr_kubernetes.main.id
            }

            output "kubeconfig" {
              description = "Base64 kubeconfig. Save it with: terraform output -raw kubeconfig | base64 -d > kubeconfig.yaml"
              value       = data.vultr_kubernetes_kubeconfig.main.kube_config
              sensitive   = true
            }

            output "cluster_endpoint" {
              description = "The Kubernetes API endpoint."
              value       = vultr_kubernetes.main.endpoint
            }
        """)]
        if self.managed_db:
            parts.append(T("""
                output "database_host" {
                  description = "Put this in the DB_HOST value of kubernetes/configmap.yaml."
                  value       = vultr_database.main.host
                }

                output "database_port" {
                  description = "The database port."
                  value       = vultr_database.main.port
                }

                output "database_user" {
                  description = "The database user."
                  value       = vultr_database.main.user
                }

                output "database_password" {
                  description = "The generated database password. Put it in the Kubernetes secret, not in a file."
                  value       = vultr_database.main.password
                  sensitive   = true
                }
            """))
        if self.storage:
            parts.append(T("""
                output "bucket_host" {
                  description = "The S3 host for object storage."
                  value       = vultr_object_storage.assets.s3_hostname
                }

                output "bucket_access_key" {
                  description = "Access key for the application."
                  value       = vultr_object_storage.assets.s3_access_key
                  sensitive   = true
                }

                output "bucket_secret_key" {
                  description = "Secret key for the application."
                  value       = vultr_object_storage.assets.s3_secret_key
                  sensitive   = true
                }
            """))
        self.files.add("terraform/outputs.tf", tf(*parts))

    def readme(self):
        m = self.m
        return T("""
            # Vultr infrastructure for @@project@@

            Generated from plan @@fp@@. This creates a private VPC, a Vultr Kubernetes Engine cluster with the cluster firewall on@@db@@@@obj@@.
            The application runs from the manifests in `../kubernetes` (or the Helm chart in `../helm`).

            ## Steps

            1. Create an API key in the customer portal (Account, API) and export it: `export VULTR_API_KEY=...`
            2. Create an Object Storage bucket for state, copy `backend.hcl.example` to `backend.hcl`, fill it in, and export its keys as `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`.
            3. Check the plan names are current: `vultr-cli kubernetes versions`, `vultr-cli plans list`, `vultr-cli database plan list`, `vultr-cli object-storage tiers`. Change `node_plan`, `database_plan` and `object_storage_tier` if needed.
            4. `terraform init -backend-config=backend.hcl`, `terraform plan`, `terraform apply`.
            5. `terraform output -raw kubeconfig | base64 -d > kubeconfig.yaml` and `export KUBECONFIG=$PWD/kubeconfig.yaml`.
            6. Install an ingress controller (`ingress-nginx`); Vultr creates a load balancer for it. Then follow `../kubernetes/README.md`.
        """, project=m.project, fp=m.fingerprint, db=(", a managed database on the same VPC" if self.managed_db else ""),
                 obj=(" and Object Storage" if self.storage else ""))


def generate(model):
    return Vultr(model).generate()
