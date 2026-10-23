#!/usr/bin/env python3
"""Terraform for Civo, generated from the plan.

Containers run on a Civo Kubernetes cluster inside a private network with its own firewall, the managed database sits on the same
network and firewall, and Object Store holds files. Redis, MongoDB and RabbitMQ run inside the cluster from ../kubernetes when the
plan marks them self-run. Civo size and version names change, so database sizes and versions are looked up from Civo itself while
the node size is a variable with a common default and a note on how to list the current ones.
"""
from iac_common import Files, T, fmt_hcl, selfrun, tf

DEFAULT_REGION = "NYC1"
NODE_SIZES = [(1.5, "g4s.kube.medium"), (3.0, "g4s.kube.large"), (6.0, "g4s.kube.xlarge"), (1e9, "g4s.kube.xlarge")]


class Civo:
    def __init__(self, model):
        self.m = model
        self.files = Files(model.fingerprint)
        self.region = model.region.upper() if model.region and model.region[:3].isalpha() and model.region[-1:].isdigit() else DEFAULT_REGION
        self.ha = not model.single_az
        self.managed_db = model.db_key is not None and not selfrun(model, model.db_key)
        self.storage = model.has("storage")
        need = sum(float(model.sizing["unit_vcpu"]) for u in model.units if u["kind"] != "static") * model.replicas / 2
        self.node_size = next(t for limit, t in NODE_SIZES if need <= limit)

    def generate(self):
        m = self.m
        self.versions()
        self.variables()
        self.cluster()
        if self.managed_db:
            self.database()
        if self.storage:
            self.object_store()
        self.outputs()
        self.files.add("terraform/backend.tf", T("""
            terraform {
              backend "s3" {}
            }
        """))
        self.files.add("terraform/backend.hcl.example", T("""
            # Civo Object Store speaks the S3 protocol. Create a store in the dashboard (Object Stores), then fill this in.
            bucket                      = "REPLACE-with-your-state-store"
            key                         = "@@project@@/production.tfstate"
            region                      = "us-east-1"
            endpoints                   = { s3 = "https://objectstore.REPLACE-with-your-region-code.civo.com" }
            skip_credentials_validation = true
            skip_region_validation      = true
            skip_requesting_account_id  = true
            skip_metadata_api_check     = true
            skip_s3_checksum            = true
            use_path_style              = true
        """, project=m.project))
        self.files.add("terraform/terraform.tfvars.example", fmt_hcl(
            'project_name = "%s"\nenvironment  = "production"\nregion       = "%s"\n' % (m.project, self.region)))
        self.files.add("terraform/README.md", self.readme())
        return self.files

    def versions(self):
        self.files.add("terraform/versions.tf", tf(T("""
            terraform {
              required_version = ">= 1.9.0"

              required_providers {
                civo = {
                  source  = "civo/civo"
                  version = "~> 1.3"
                }
              }
            }

            provider "civo" {
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
              description = "Civo region code, for example NYC1, LON1 or FRA1."
              type        = string
              default     = "@@region@@"
            }

            variable "node_size" {
              description = "Size of each cluster node. List the current ones with: civo kubernetes size"
              type        = string
              default     = "@@size@@"
            }

            variable "node_count" {
              description = "How many nodes the cluster runs."
              type        = number
              default     = @@nodes@@
            }

            variable "admin_cidr" {
              description = "Address range allowed to reach the Kubernetes API, for example your office address with /32."
              type        = string
              default     = "REPLACE-with-your-office-address/32"
            }
        """, region=self.region, size=self.node_size, nodes=3 if self.ha else 2)))

    def cluster(self):
        self.files.add("terraform/cluster.tf", tf(T("""
            locals {
              name = "${var.project_name}-${var.environment}"
            }

            resource "civo_network" "main" {
              label = local.name
            }

            resource "civo_firewall" "main" {
              name                 = local.name
              network_id           = civo_network.main.id
              create_default_rules = false

              ingress_rule {
                label      = "web"
                protocol   = "tcp"
                port_range = "80"
                cidr       = ["0.0.0.0/0"]
                action     = "allow"
              }

              ingress_rule {
                label      = "web-tls"
                protocol   = "tcp"
                port_range = "443"
                cidr       = ["0.0.0.0/0"]
                action     = "allow"
              }

              ingress_rule {
                label      = "kubernetes-api"
                protocol   = "tcp"
                port_range = "6443"
                cidr       = [var.admin_cidr]
                action     = "allow"
              }

              egress_rule {
                label      = "all-out"
                protocol   = "tcp"
                port_range = "1-65535"
                cidr       = ["0.0.0.0/0"]
                action     = "allow"
              }
            }

            data "civo_kubernetes_version" "stable" {
              filter {
                key    = "type"
                values = ["stable"]
              }
            }

            resource "civo_kubernetes_cluster" "main" {
              name               = local.name
              network_id         = civo_network.main.id
              firewall_id        = civo_firewall.main.id
              kubernetes_version = element(data.civo_kubernetes_version.stable.versions, 0).version
              cluster_type       = "k3s"

              pools {
                label      = "main"
                size       = var.node_size
                node_count = var.node_count
              }
            }
        """)))

    def database(self):
        m = self.m
        engine = "postgres" if m.db_key == "database" else "mysql"
        self.files.add("terraform/database.tf", tf(T("""
            data "civo_size" "database" {
              filter {
                key      = "name"
                values   = ["db.small"]
                match_by = "re"
              }

              filter {
                key    = "type"
                values = ["database"]
              }
            }

            data "civo_database_version" "main" {
              filter {
                key    = "engine"
                values = ["@@engine@@"]
              }
            }

            resource "civo_database" "main" {
              name        = "${local.name}-db"
              size        = element(data.civo_size.database.sizes, 0).name
              nodes       = @@nodes@@
              engine      = element(data.civo_database_version.main.versions, 0).engine
              version     = element(data.civo_database_version.main.versions, 0).version
              network_id  = civo_network.main.id
              firewall_id = civo_firewall.main.id
            }
        """, engine=engine, nodes=2 if self.ha else 1)))

    def object_store(self):
        self.files.add("terraform/storage.tf", tf(T("""
            resource "civo_object_store" "assets" {
              name        = "${local.name}-assets"
              max_size_gb = 500
            }
        """)))

    def outputs(self):
        parts = [T("""
            output "kubeconfig" {
              description = "Save it with: terraform output -raw kubeconfig > kubeconfig.yaml"
              value       = civo_kubernetes_cluster.main.kubeconfig
              sensitive   = true
            }

            output "cluster_endpoint" {
              description = "The Kubernetes API endpoint."
              value       = civo_kubernetes_cluster.main.api_endpoint
            }
        """)]
        if self.managed_db:
            parts.append(T("""
                output "database_host" {
                  description = "Put this in the DB_HOST value of kubernetes/configmap.yaml."
                  value       = civo_database.main.private_ipv4
                }

                output "database_port" {
                  description = "The database port."
                  value       = civo_database.main.port
                }

                output "database_user" {
                  description = "The database user."
                  value       = civo_database.main.username
                }

                output "database_password" {
                  description = "The generated database password. Put it in the Kubernetes secret, not in a file."
                  value       = civo_database.main.password
                  sensitive   = true
                }
            """))
        if self.storage:
            parts.append(T("""
                output "bucket_url" {
                  description = "The address of the object store."
                  value       = civo_object_store.assets.bucket_url
                }

                output "bucket_access_key_id" {
                  description = "Access key id of the object store."
                  value       = civo_object_store.assets.access_key_id
                }
            """))
        self.files.add("terraform/outputs.tf", tf(*parts))

    def readme(self):
        m = self.m
        return T("""
            # Civo infrastructure for @@project@@

            Generated from plan @@fp@@. This creates a private network with its own firewall and a Civo Kubernetes cluster@@db@@@@obj@@.
            The application runs from the manifests in `../kubernetes` (or the Helm chart in `../helm`).

            ## Steps

            1. Create an API key in the dashboard (Security) and export it: `export CIVO_TOKEN=...`
            2. Create an Object Store for Terraform state, copy `backend.hcl.example` to `backend.hcl`, fill it in, and export its keys as `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`.
            3. Check the node size is current: `civo kubernetes size`. Change `node_size` if needed. Put your own address in `admin_cidr`.
            4. `terraform init -backend-config=backend.hcl`, `terraform plan`, `terraform apply`.
            5. `terraform output -raw kubeconfig > kubeconfig.yaml` and `export KUBECONFIG=$PWD/kubeconfig.yaml`.
            6. Install an ingress controller (`ingress-nginx`); Civo creates a load balancer for it. Then follow `../kubernetes/README.md`.

            ## Settings and secrets

            @@settings@@
        """, project=m.project, fp=m.fingerprint, db=(", a managed database on the same network and firewall" if self.managed_db else ""),
                 obj=(" and an Object Store" if self.storage else ""),
                 settings=("The application reads %d setting(s). Their names are in `kubernetes/secret.example.yaml` and `kubernetes/configmap.yaml`; the values go into the Kubernetes secret "
                           "(`kubernetes/secret.example.yaml` shows the keys)." % len(m.settings)) if m.settings else "The scan found no environment variables.")


def generate(model):
    return Civo(model).generate()
