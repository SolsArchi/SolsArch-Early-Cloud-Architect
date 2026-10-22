#!/usr/bin/env python3
"""Terraform for Linode (Akamai Cloud), generated from the plan.

Containers run on the Linode Kubernetes Engine (LKE). PostgreSQL and MySQL are the managed databases, files go to Object
Storage, and everything else the plan marks self-run (Redis, MongoDB, RabbitMQ) runs inside the cluster from the manifests
in ../kubernetes, which also create the ingress that Linode turns into a NodeBalancer. The cluster firewall follows the
Linode guidance: nodes accept traffic from the NodeBalancer range and the private network, and nothing else.
"""
from iac_common import Files, T, fmt_hcl, selfrun, tf

DEFAULT_REGION = "us-east"
OBJECT_REGION = {"us-east": "us-iad", "us-ord": "us-ord", "us-west": "us-sea", "us-central": "us-ord", "us-southeast": "us-mia",
                 "eu-west": "gb-lon", "eu-central": "de-fra-2", "fr-par": "fr-par", "ap-south": "sg-sin-2", "ap-northeast": "jp-osa",
                 "ap-west": "in-maa", "ap-southeast": "au-mel", "it-mil": "it-mil", "nl-ams": "nl-ams", "se-sto": "se-sto"}
NODE_TYPES = [(1.5, "g6-standard-2"), (3.0, "g6-standard-4"), (6.0, "g6-standard-6"), (1e9, "g6-standard-8")]


class Linode:
    def __init__(self, model):
        self.m = model
        self.files = Files(model.fingerprint)
        self.region = model.region if model.region in OBJECT_REGION else DEFAULT_REGION
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
        if self.storage:
            self.object_storage()
        self.outputs()
        self.files.add("terraform/backend.tf", T("""
            terraform {
              backend "s3" {}
            }
        """))
        self.files.add("terraform/backend.hcl.example", T("""
            # Linode Object Storage speaks the S3 protocol. Create a bucket in Cloud Manager, then fill this in.
            bucket                      = "REPLACE-with-your-state-bucket"
            key                         = "@@project@@/production.tfstate"
            region                      = "@@object@@"
            endpoints                   = { s3 = "https://@@object@@-1.linodeobjects.com" }
            skip_credentials_validation = true
            skip_region_validation      = true
            skip_requesting_account_id  = true
            skip_metadata_api_check     = true
            skip_s3_checksum            = true
        """, project=m.project, object=OBJECT_REGION[self.region]))
        self.files.add("terraform/terraform.tfvars.example", fmt_hcl(
            'project_name = "%s"\nenvironment  = "production"\nregion       = "%s"\n' % (m.project, self.region)))
        self.files.add("terraform/README.md", self.readme())
        return self.files

    def versions(self):
        self.files.add("terraform/versions.tf", tf(T("""
            terraform {
              required_version = ">= 1.9.0"

              required_providers {
                linode = {
                  source  = "linode/linode"
                  version = "~> 4.0"
                }
              }
            }

            provider "linode" {}
        """)))

    def variables(self):
        extra = ""
        if self.storage:
            extra = T("""
                variable "object_storage_region" {
                  description = "Object Storage region (for example us-iad, de-fra-2, gb-lon)."
                  type        = string
                  default     = "@@object@@"
                }
            """, object=OBJECT_REGION[self.region])
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
              description = "Linode region for the cluster and the database."
              type        = string
              default     = "@@region@@"
            }

            variable "kubernetes_version" {
              description = "Kubernetes version. Leave null to use the newest one Linode offers."
              type        = string
              default     = null
            }

            variable "node_type" {
              description = "Linode plan for each cluster node."
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

            variable "control_plane_acl" {
              description = "Addresses allowed to reach the Kubernetes API. Narrow this to your office or CI range."
              type        = list(string)
              default     = ["0.0.0.0/0"]
            }
        """, region=self.region, node=self.node_type, min=2 if self.ha else 1, max=6 if self.ha else 3), extra))

    def cluster(self):
        self.files.add("terraform/cluster.tf", tf(T("""
            locals {
              name = "${var.project_name}-${var.environment}"
              tags = [var.project_name, var.environment, "solsarch"]
            }

            data "linode_lke_versions" "current" {}

            resource "linode_vpc" "main" {
              label       = local.name
              region      = var.region
              description = "Private network for ${local.name}"
            }

            resource "linode_vpc_subnet" "main" {
              vpc_id = linode_vpc.main.id
              label  = "${local.name}-main"
              ipv4   = "10.0.1.0/24"
            }

            resource "linode_firewall" "nodes" {
              label           = "${local.name}-nodes"
              tags            = local.tags
              inbound_policy  = "DROP"
              outbound_policy = "ACCEPT"

              inbound {
                label    = "nodebalancer-tcp"
                action   = "ACCEPT"
                protocol = "TCP"
                ports    = "30000-32767"
                ipv4     = ["192.168.255.0/24"]
              }

              inbound {
                label    = "nodebalancer-udp"
                action   = "ACCEPT"
                protocol = "UDP"
                ports    = "30000-32767"
                ipv4     = ["192.168.255.0/24"]
              }

              inbound {
                label    = "private-network"
                action   = "ACCEPT"
                protocol = "TCP"
                ports    = "1-65535"
                ipv4     = ["10.0.0.0/8"]
              }

              inbound {
                label    = "private-network-udp"
                action   = "ACCEPT"
                protocol = "UDP"
                ports    = "1-65535"
                ipv4     = ["10.0.0.0/8"]
              }

              inbound {
                label    = "ping"
                action   = "ACCEPT"
                protocol = "ICMP"
                ipv4     = ["0.0.0.0/0"]
              }
            }

            resource "linode_lke_cluster" "main" {
              label       = local.name
              region      = var.region
              k8s_version = coalesce(var.kubernetes_version, data.linode_lke_versions.current.versions[0].id)
              tags        = local.tags

              control_plane {
                high_availability = @@ha@@

                acl {
                  enabled = true

                  addresses {
                    ipv4 = var.control_plane_acl
                  }
                }
              }

              pool {
                type            = var.node_type
                count           = var.node_min
                firewall_id     = linode_firewall.nodes.id
                disk_encryption = "enabled"

                autoscaler {
                  min = var.node_min
                  max = var.node_max
                }
              }

              lifecycle {
                ignore_changes = [pool[0].count]
              }
            }
        """, ha="true" if self.ha else "false")))

    def database(self):
        m = self.m
        engine = "postgresql" if m.db_key == "database" else "mysql"
        resource = "linode_database_postgresql_v2" if engine == "postgresql" else "linode_database_mysql_v2"
        self.files.add("terraform/database.tf", tf(T("""
            data "linode_database_engines" "db" {
              latest = true

              filter {
                name   = "engine"
                values = ["@@engine@@"]
              }
            }

            data "linode_instances" "nodes" {
              filter {
                name   = "id"
                values = [for n in linode_lke_cluster.main.pool[0].nodes : tostring(n.instance_id)]
              }
            }

            resource "@@resource@@" "main" {
              label        = "${local.name}-db"
              engine_id    = data.linode_database_engines.db.engines[0].id
              region       = var.region
              type         = var.database_type
              cluster_size = @@size@@
              allow_list   = [for i in data.linode_instances.nodes.instances : "${i.ip_address}/32"]

              lifecycle {
                prevent_destroy = true
              }
            }

            variable "database_type" {
              description = "Linode plan for each database node."
              type        = string
              default     = "@@dbtype@@"
            }
        """, engine=engine, resource=resource, size=3 if self.ha else 1,
                       dbtype="g6-standard-2" if self.m.sizing.get("tier", "") in ("medium", "large") else "g6-nanode-1")))

    def object_storage(self):
        self.files.add("terraform/storage.tf", tf(T("""
            resource "linode_object_storage_bucket" "assets" {
              label      = "${local.name}-assets"
              region     = var.object_storage_region
              acl        = "private"
              versioning = true
            }

            resource "linode_object_storage_key" "app" {
              label = "${local.name}-app"

              bucket_access {
                bucket_name = linode_object_storage_bucket.assets.label
                region      = var.object_storage_region
                permissions = "read_write"
              }
            }
        """)))

    def outputs(self):
        parts = [T("""
            output "kubeconfig" {
              description = "Base64 kubeconfig. Save it with: terraform output -raw kubeconfig | base64 -d > kubeconfig.yaml"
              value       = linode_lke_cluster.main.kubeconfig
              sensitive   = true
            }

            output "cluster_id" {
              description = "The LKE cluster id."
              value       = linode_lke_cluster.main.id
            }
        """)]
        if self.managed_db:
            resource = "linode_database_postgresql_v2" if self.m.db_key == "database" else "linode_database_mysql_v2"
            parts.append(T("""
                output "database_host" {
                  description = "Put this in the DB_HOST value of kubernetes/configmap.yaml."
                  value       = @@r@@.main.host_primary
                }

                output "database_port" {
                  description = "The database port."
                  value       = @@r@@.main.port
                }

                output "database_password" {
                  description = "The generated database password. Put it in the Kubernetes secret, not in a file."
                  value       = @@r@@.main.root_password
                  sensitive   = true
                }
            """, r=resource))
        if self.storage:
            parts.append(T("""
                output "bucket" {
                  description = "The bucket for uploaded files."
                  value       = linode_object_storage_bucket.assets.label
                }

                output "bucket_endpoint" {
                  description = "The S3 endpoint for the bucket."
                  value       = linode_object_storage_bucket.assets.s3_endpoint
                }

                output "bucket_access_key" {
                  description = "Access key for the application."
                  value       = linode_object_storage_key.app.access_key
                  sensitive   = true
                }

                output "bucket_secret_key" {
                  description = "Secret key for the application."
                  value       = linode_object_storage_key.app.secret_key
                  sensitive   = true
                }
            """))
        self.files.add("terraform/outputs.tf", tf(*parts))

    def readme(self):
        m = self.m
        return T("""
            # Linode infrastructure for @@project@@

            Generated from plan @@fp@@. This creates an LKE cluster with a cluster firewall, a private VPC@@db@@@@obj@@. The
            application runs from the manifests in `../kubernetes` (or the Helm chart in `../helm`).

            ## Steps

            1. Create a personal access token in Cloud Manager and export it: `export LINODE_TOKEN=...`
            2. Create an Object Storage bucket for state, copy `backend.hcl.example` to `backend.hcl`, fill it in, and export the bucket's access keys as `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`.
            3. `cp terraform.tfvars.example terraform.tfvars`, then narrow `control_plane_acl`.
            4. `terraform init -backend-config=backend.hcl`, `terraform plan`, `terraform apply`.
            5. `terraform output -raw kubeconfig | base64 -d > kubeconfig.yaml` and `export KUBECONFIG=$PWD/kubeconfig.yaml`.
            6. Install an ingress controller (`ingress-nginx`); Linode creates a NodeBalancer for it. Then follow `../kubernetes/README.md`.

            ## Notes

            - The database allow list is built from the node addresses, so run `terraform apply` again after nodes are replaced.
            - Control plane high availability is @@ha@@. Turning it on later cannot be undone.
        """, project=m.project, fp=m.fingerprint, db=(", a managed database" if self.managed_db else ""),
                 obj=(" and an Object Storage bucket" if self.storage else ""), ha=("on" if self.ha else "off in this lean design"))


def generate(model):
    return Linode(model).generate()
