#!/usr/bin/env python3
"""Terraform for Exoscale, generated from the plan.

Containers run on an Exoscale Scalable Kubernetes Service (SKS) cluster with the security group SKS creates for its network plugin,
the managed database (DBaaS) accepts connections only from the addresses you list, and kubeconfig access is time-limited. Exoscale
Object Storage (SOS) buckets are created through its S3 interface, not by this provider, so the README gives the command. Redis,
MongoDB and RabbitMQ run inside the cluster from ../kubernetes when the plan marks them self-run. Instance types and database plans
change, so they are variables with common defaults and the README says how to list the current ones.
"""
from iac_common import Files, T, fmt_hcl, selfrun, tf

DEFAULT_ZONE = "ch-gva-2"
NODE_TYPES = [(1.5, "standard.medium"), (3.0, "standard.large"), (6.0, "standard.extra-large"), (1e9, "standard.huge")]


class Exoscale:
    def __init__(self, model):
        self.m = model
        self.files = Files(model.fingerprint)
        self.zone = model.region if model.region and model.region.count("-") == 2 else DEFAULT_ZONE
        self.ha = not model.single_az
        self.managed_db = model.db_key is not None and not selfrun(model, model.db_key)
        self.storage = model.has("storage")
        need = sum(float(model.sizing["unit_vcpu"]) for u in model.units if u["kind"] != "static") * model.replicas / 2
        self.node_type = next(t for limit, t in NODE_TYPES if need <= limit)

    def generate(self):
        m = self.m
        self.versions()
        self.variables()
        self.cluster()
        if self.managed_db:
            self.database()
        self.outputs()
        self.files.add("terraform/backend.tf", T("""
            terraform {
              backend "s3" {}
            }
        """))
        self.files.add("terraform/backend.hcl.example", T("""
            # Exoscale Object Storage (SOS) speaks the S3 protocol. Create a bucket for state (see the README), then fill this in.
            bucket                      = "REPLACE-with-your-state-bucket"
            key                         = "@@project@@/production.tfstate"
            region                      = "@@zone@@"
            endpoints                   = { s3 = "https://sos-@@zone@@.exo.io" }
            skip_credentials_validation = true
            skip_region_validation      = true
            skip_requesting_account_id  = true
            skip_metadata_api_check     = true
            skip_s3_checksum            = true
            use_path_style              = true
        """, project=m.project, zone=self.zone))
        self.files.add("terraform/terraform.tfvars.example", fmt_hcl(
            'project_name = "%s"\nenvironment  = "production"\nzone         = "%s"\n' % (m.project, self.zone)))
        self.files.add("terraform/README.md", self.readme())
        return self.files

    def versions(self):
        self.files.add("terraform/versions.tf", tf(T("""
            terraform {
              required_version = ">= 1.9.0"

              required_providers {
                exoscale = {
                  source  = "exoscale/exoscale"
                  version = "~> 0.75"
                }
              }
            }

            provider "exoscale" {}
        """)))

    def variables(self):
        db = ""
        if self.managed_db:
            db = T("""
                variable "database_plan" {
                  description = "Managed database plan. List current ones with: exo dbaas type show pg"
                  type        = string
                  default     = "startup-4"
                }

                variable "database_allowed_cidrs" {
                  description = "Addresses allowed to connect to the database. After the cluster exists add the public addresses of its nodes (kubectl get nodes -o wide) and apply again."
                  type        = list(string)
                  default     = []
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
              description = "Exoscale zone, for example ch-gva-2, de-fra-1 or at-vie-1. List them with: exo zone list"
              type        = string
              default     = "@@zone@@"
            }

            variable "node_type" {
              description = "Instance type of each worker node. List the current ones with: exo compute instance-type list"
              type        = string
              default     = "@@type@@"
            }

            variable "node_count" {
              description = "How many worker nodes the cluster runs."
              type        = number
              default     = @@nodes@@
            }

            variable "kubeconfig_ttl_seconds" {
              description = "How long the generated kubeconfig stays valid."
              type        = number
              default     = 86400
            }
        """, zone=self.zone, type=self.node_type, nodes=3 if self.ha else 2), db))

    def cluster(self):
        self.files.add("terraform/cluster.tf", tf(T("""
            locals {
              name = "${var.project_name}-${var.environment}"
            }

            resource "exoscale_sks_cluster" "main" {
              zone                          = var.zone
              name                          = local.name
              service_level                 = "@@level@@"
              cni                           = "calico"
              auto_upgrade                  = true
              exoscale_ccm                  = true
              metrics_server                = true
              create_default_security_group = true
            }

            resource "exoscale_sks_nodepool" "main" {
              zone               = var.zone
              cluster_id         = exoscale_sks_cluster.main.id
              name               = "${local.name}-main"
              instance_type      = var.node_type
              size               = var.node_count
              security_group_ids = [exoscale_sks_cluster.main.default_security_group_id]
            }

            resource "exoscale_sks_kubeconfig" "admin" {
              zone        = var.zone
              cluster_id  = exoscale_sks_cluster.main.id
              user        = "kubernetes-admin"
              groups      = ["system:masters"]
              ttl_seconds = var.kubeconfig_ttl_seconds
            }
        """, level="pro" if self.ha else "starter")))

    def database(self):
        m = self.m
        kind = "pg" if m.db_key == "database" else "mysql"
        version = "16" if kind == "pg" else "8"
        self.files.add("terraform/database.tf", tf(T("""
            resource "exoscale_dbaas" "main" {
              zone                   = var.zone
              name                   = "${local.name}-db"
              type                   = "@@kind@@"
              plan                   = var.database_plan
              termination_protection = true

              @@kind@@ = {
                version   = "@@version@@"
                ip_filter = var.database_allowed_cidrs
              }
            }
        """, kind=kind, version=version)))

    def outputs(self):
        parts = [T("""
            output "kubeconfig" {
              description = "Save it with: terraform output -raw kubeconfig > kubeconfig.yaml"
              value       = exoscale_sks_kubeconfig.admin.kubeconfig
              sensitive   = true
            }

            output "cluster_endpoint" {
              description = "The Kubernetes API endpoint."
              value       = exoscale_sks_cluster.main.endpoint
            }
        """)]
        if self.managed_db:
            parts.append(T("""
                output "database_uri" {
                  description = "Connection address of the database. Put the host and port in kubernetes/configmap.yaml and the password in the Kubernetes secret."
                  value       = exoscale_dbaas.main.uri
                  sensitive   = true
                }
            """))
        self.files.add("terraform/outputs.tf", tf(*parts))

    def readme(self):
        m = self.m
        return T("""
            # Exoscale infrastructure for @@project@@

            Generated from plan @@fp@@. This creates an SKS Kubernetes cluster with its worker nodes and the security group SKS makes for its network plugin@@db@@.
            The application runs from the manifests in `../kubernetes` (or the Helm chart in `../helm`).

            ## Steps

            1. Create an API key and secret in the portal (IAM) and export `EXOSCALE_API_KEY` and `EXOSCALE_API_SECRET`.
            2. Create a bucket for Terraform state: `exo storage mb sos://YOUR-STATE-BUCKET --zone @@zone@@`. Copy `backend.hcl.example` to `backend.hcl`, fill it in, and export a key pair for it as `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`.
            3. Check the instance type and database plan are current: `exo compute instance-type list`, `exo dbaas type show pg`. Change `node_type` and `database_plan` if needed.
            4. `terraform init -backend-config=backend.hcl`, `terraform plan`, `terraform apply`.
            5. `terraform output -raw kubeconfig > kubeconfig.yaml` and `export KUBECONFIG=$PWD/kubeconfig.yaml`.
            6. @@dbstep@@Install an ingress controller (`ingress-nginx`). Then follow `../kubernetes/README.md`.@@obj@@

            ## Settings and secrets

            @@settings@@
        """, project=m.project, fp=m.fingerprint, zone=self.zone,
                 db=(", plus a managed database that only accepts the addresses in `database_allowed_cidrs`" if self.managed_db else ""),
                 dbstep=("Add your nodes' public addresses (`kubectl get nodes -o wide`) to `database_allowed_cidrs` in `terraform.tfvars` and run `terraform apply` again. " if self.managed_db else ""),
                 obj=("\n            7. Create the application bucket: `exo storage mb sos://%s-assets --zone %s`. Terraform cannot create SOS buckets, so this stays a command." % (m.project, self.zone) if self.storage else ""),
                 settings=("The application reads %d setting(s). Their names are in `kubernetes/secret.example.yaml` and `kubernetes/configmap.yaml`; "
                           "the values go into the Kubernetes secret with `kubectl create secret`." % len(m.settings)) if m.settings else "The scan found no environment variables.")


def generate(model):
    return Exoscale(model).generate()
