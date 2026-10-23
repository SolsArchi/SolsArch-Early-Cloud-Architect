#!/usr/bin/env python3
"""Terraform for UpCloud, generated from the plan.

Containers run on UpCloud Managed Kubernetes inside a private network, the managed database sits in the same zone and is reachable
only from that network, and Managed Object Storage holds files. Redis, MongoDB and RabbitMQ run inside the cluster from
../kubernetes when the plan marks them self-run. UpCloud plan names change, so they are variables with the common defaults and the
README says how to list the current ones before you apply.
"""
from iac_common import Files, T, fmt_hcl, selfrun, tf

DEFAULT_ZONE = "de-fra1"
OBJECT_REGION = "europe-1"
NODE_PLANS = [(1.5, "2xCPU-4GB"), (3.0, "4xCPU-8GB"), (6.0, "6xCPU-16GB"), (1e9, "8xCPU-32GB")]


class UpCloud:
    def __init__(self, model):
        self.m = model
        self.files = Files(model.fingerprint)
        self.zone = model.region if model.region and "-" in model.region and model.region[-1:].isdigit() else DEFAULT_ZONE
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
            # UpCloud Managed Object Storage speaks the S3 protocol. Create a bucket for state in the control panel, then fill this in.
            bucket                      = "REPLACE-with-your-state-bucket"
            key                         = "@@project@@/production.tfstate"
            region                      = "us-east-1"
            endpoints                   = { s3 = "https://REPLACE-with-your-object-storage-endpoint" }
            skip_credentials_validation = true
            skip_region_validation      = true
            skip_requesting_account_id  = true
            skip_metadata_api_check     = true
            skip_s3_checksum            = true
            use_path_style              = true
        """, project=m.project))
        self.files.add("terraform/terraform.tfvars.example", fmt_hcl(
            'project_name = "%s"\nenvironment  = "production"\nzone         = "%s"\n' % (m.project, self.zone)))
        self.files.add("terraform/README.md", self.readme())
        return self.files

    def versions(self):
        self.files.add("terraform/versions.tf", tf(T("""
            terraform {
              required_version = ">= 1.9.0"

              required_providers {
                upcloud = {
                  source  = "UpCloudLtd/upcloud"
                  version = "~> 5.0"
                }
              }
            }

            provider "upcloud" {}
        """)))

    def variables(self):
        db = ""
        if self.managed_db:
            db = T("""
                variable "database_plan" {
                  description = "Managed database compute plan. List current ones with: upctl database plans pg"
                  type        = string
                  default     = "rdb.standard.2CPU-8GB"
                }

                variable "database_storage_gib" {
                  description = "Managed database storage in GiB."
                  type        = number
                  default     = 120
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

            variable "zone" {
              description = "UpCloud zone, for example de-fra1, fi-hel1 or us-nyc1. List them with: upctl zone list"
              type        = string
              default     = "@@zone@@"
            }

            variable "node_plan" {
              description = "Plan of each worker node. List the current ones with: upctl kubernetes plans"
              type        = string
              default     = "@@plan@@"
            }

            variable "node_count" {
              description = "How many worker nodes the cluster runs."
              type        = number
              default     = @@nodes@@
            }

            variable "admin_cidr" {
              description = "Address range allowed to reach the Kubernetes API, for example your office address with /32."
              type        = string
              default     = "REPLACE-with-your-office-address/32"
            }
        """, zone=self.zone, plan=self.node_plan, nodes=3 if self.ha else 2), db))

    def cluster(self):
        self.files.add("terraform/cluster.tf", tf(T("""
            locals {
              name = "${var.project_name}-${var.environment}"
            }

            resource "upcloud_network" "main" {
              name = local.name
              zone = var.zone

              ip_network {
                address = "10.20.0.0/24"
                dhcp    = true
                family  = "IPv4"
              }

              # The managed Kubernetes service attaches a router to this network; do not detach it on the next apply.
              lifecycle {
                ignore_changes = [router]
              }
            }

            resource "upcloud_kubernetes_cluster" "main" {
              name                    = local.name
              network                 = upcloud_network.main.id
              zone                    = var.zone
              control_plane_ip_filter = [var.admin_cidr]
            }

            resource "upcloud_kubernetes_node_group" "main" {
              cluster    = upcloud_kubernetes_cluster.main.id
              name       = "main"
              plan       = var.node_plan
              node_count = var.node_count

              labels = {
                managed-by = "terraform"
              }
            }
        """)))

    def database(self):
        m = self.m
        kind = "postgresql" if m.db_key == "database" else "mysql"
        self.files.add("terraform/database.tf", tf(T("""
            resource "upcloud_managed_database_@@kind@@" "main" {
              name                    = "${local.name}-db"
              title                   = "${local.name} database"
              zone                    = var.zone
              plan_compute            = var.database_plan
              plan_node_count         = @@nodes@@
              plan_storage_gib        = var.database_storage_gib
              termination_protection  = true

              network {
                family = "IPv4"
                name   = "private"
                type   = "private"
                uuid   = upcloud_network.main.id
              }
            }
        """, kind=kind, nodes=2 if self.ha else 1)))

    def object_storage(self):
        self.files.add("terraform/storage.tf", tf(T("""
            variable "object_storage_region" {
              description = "Region of Managed Object Storage. List them with: upctl object-storage regions"
              type        = string
              default     = "@@region@@"
            }

            resource "upcloud_managed_object_storage" "assets" {
              name              = "${local.name}-assets"
              region            = var.object_storage_region
              configured_status = "started"

              network {
                family = "IPv4"
                name   = "public"
                type   = "public"
              }
            }

            resource "upcloud_managed_object_storage_bucket" "assets" {
              service_uuid = upcloud_managed_object_storage.assets.id
              name         = "${local.name}-assets"
            }
        """, region=OBJECT_REGION)))

    def outputs(self):
        parts = [T("""
            output "kubeconfig" {
              description = "Save it with: terraform output -raw kubeconfig > kubeconfig.yaml"
              value       = upcloud_kubernetes_cluster.main.kubeconfig
              sensitive   = true
            }

            output "cluster_id" {
              description = "The id of the Kubernetes cluster."
              value       = upcloud_kubernetes_cluster.main.id
            }
        """)]
        if self.managed_db:
            kind = "postgresql" if self.m.db_key == "database" else "mysql"
            parts.append(T("""
                output "database_service_uri" {
                  description = "Connection address of the database. Put the host and port in kubernetes/configmap.yaml and the password in the Kubernetes secret."
                  value       = upcloud_managed_database_@@kind@@.main.service_uri
                  sensitive   = true
                }
            """, kind=kind))
        if self.storage:
            parts.append(T("""
                output "object_storage_endpoints" {
                  description = "Addresses of the object storage service."
                  value       = upcloud_managed_object_storage.assets.endpoint
                }
            """))
        self.files.add("terraform/outputs.tf", tf(*parts))

    def readme(self):
        m = self.m
        return T("""
            # UpCloud infrastructure for @@project@@

            Generated from plan @@fp@@. This creates a private network and an UpCloud Managed Kubernetes cluster with its worker nodes@@db@@@@obj@@.
            The application runs from the manifests in `../kubernetes` (or the Helm chart in `../helm`).

            ## Steps

            1. Create an API user in the control panel (People, Account, create a subaccount with API access) and export `UPCLOUD_USERNAME` and `UPCLOUD_PASSWORD`.
            2. Create a bucket for Terraform state in Managed Object Storage, copy `backend.hcl.example` to `backend.hcl`, fill it in, and export its keys as `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`.
            3. Check the plan names are current: `upctl kubernetes plans`, `upctl database plans pg`, `upctl object-storage regions`. Change `node_plan`, `database_plan` and `object_storage_region` if needed. Put your own address in `admin_cidr`.
            4. `terraform init -backend-config=backend.hcl`, `terraform plan`, `terraform apply`.
            5. `terraform output -raw kubeconfig > kubeconfig.yaml` and `export KUBECONFIG=$PWD/kubeconfig.yaml`.
            6. Install an ingress controller (`ingress-nginx`). Then follow `../kubernetes/README.md`.

            ## Settings and secrets

            @@settings@@
        """, project=m.project, fp=m.fingerprint, db=(", a managed database reachable only from that network" if self.managed_db else ""),
                 obj=(" and Managed Object Storage" if self.storage else ""),
                 settings=("The application reads %d setting(s). Their names are in `kubernetes/secret.example.yaml` and `kubernetes/configmap.yaml`; "
                           "the values go into the Kubernetes secret with `kubectl create secret`." % len(m.settings)) if m.settings else "The scan found no environment variables.")


def generate(model):
    return UpCloud(model).generate()
