#!/usr/bin/env python3
"""Terraform for OVHcloud Public Cloud, generated from the plan.

Containers run on Managed Kubernetes inside a private network (vRack), with the managed databases (PostgreSQL, MySQL,
MongoDB, Valkey, Kafka) attached to the same private network, Object Storage with S3 credentials for the app, and a Managed
Private Registry. The application itself runs from the manifests in ../kubernetes, which are the same on every
Kubernetes-based provider.
"""
from iac_common import Files, T, fmt_hcl, selfrun, tf

DEFAULT_REGION = "GRA11"
FLAVORS = [(1.5, "b3-8"), (3.0, "b3-16"), (6.0, "b3-32"), (1e9, "b3-64")]
ENGINES = [("database", "postgresql", "16", "database"), ("database_mysql", "mysql", "8", "mysql"), ("database_mongo", "mongodb", "7.0", "mongo"),
           ("cache", "valkey", "8.0", "cache"), ("queue_kafka", "kafka", "3.8", "kafka")]
USERS = {"postgresql": "ovh_cloud_project_database_postgresql_user", "mongodb": "ovh_cloud_project_database_mongodb_user",
         "valkey": "ovh_cloud_project_database_valkey_user"}


class OvhCloud:
    def __init__(self, model):
        self.m = model
        self.files = Files(model.fingerprint)
        self.region = model.region.upper() if model.region and model.region.upper()[:2].isalpha() and any(c.isdigit() for c in model.region) else DEFAULT_REGION
        self.ha = not model.single_az
        self.dbs = [e for e in ENGINES if model.has(e[0]) and not selfrun(model, e[0])]
        self.storage = model.has("storage")
        self.registry = model.has("registry")
        need = sum(float(model.sizing["unit_vcpu"]) for u in model.units if u["kind"] != "static") * model.replicas / 2
        self.flavor = next(t for limit, t in FLAVORS if need <= limit)

    def generate(self):
        m = self.m
        self.versions()
        self.variables()
        self.network()
        self.cluster()
        if self.dbs:
            self.databases()
        if self.storage:
            self.object_storage()
        if self.registry:
            self.container_registry()
        self.outputs()
        self.files.add("terraform/backend.tf", T("""
            terraform {
              backend "s3" {}
            }
        """))
        self.files.add("terraform/backend.hcl.example", T("""
            # OVHcloud Object Storage (S3 API). Create a container and S3 credentials in the control panel, then fill this in.
            bucket                      = "REPLACE-with-your-state-bucket"
            key                         = "@@project@@/production.tfstate"
            region                      = "gra"
            endpoints                   = { s3 = "https://s3.gra.io.cloud.ovh.net" }
            skip_credentials_validation = true
            skip_region_validation      = true
            skip_requesting_account_id  = true
            skip_metadata_api_check     = true
            skip_s3_checksum            = true
        """, project=m.project))
        self.files.add("terraform/terraform.tfvars.example", fmt_hcl(
            'project_name = "%s"\nenvironment  = "production"\nservice_name = "REPLACE-with-your-public-cloud-project-id"\nregion       = "%s"\n' % (m.project, self.region)))
        self.files.add("terraform/README.md", self.readme())
        return self.files

    def versions(self):
        self.files.add("terraform/versions.tf", tf(T("""
            terraform {
              required_version = ">= 1.9.0"

              required_providers {
                ovh = {
                  source  = "ovh/ovh"
                  version = "~> 2.20"
                }
              }
            }

            provider "ovh" {
              endpoint = var.ovh_endpoint
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

            variable "service_name" {
              description = "The id of your OVHcloud Public Cloud project."
              type        = string
            }

            variable "ovh_endpoint" {
              description = "API endpoint: ovh-eu, ovh-ca or ovh-us."
              type        = string
              default     = "ovh-eu"
            }

            variable "region" {
              description = "Public Cloud region, for example GRA11, SBG5, DE1, UK1, WAW1."
              type        = string
              default     = "@@region@@"
            }

            variable "node_flavor" {
              description = "Instance flavor of each cluster node."
              type        = string
              default     = "@@flavor@@"
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

            variable "database_flavor" {
              description = "Flavor of each managed database node."
              type        = string
              default     = "db1-4"
            }
        """, region=self.region, flavor=self.flavor, min=2 if self.ha else 1, max=6 if self.ha else 3)))

    def network(self):
        self.files.add("terraform/network.tf", tf(T("""
            locals {
              name   = "${var.project_name}-${var.environment}"
              region = var.region
              site   = regex("^[A-Za-z]+", var.region)
            }

            resource "ovh_cloud_project_network_private" "main" {
              service_name = var.service_name
              name         = local.name
              regions      = [var.region]
            }

            resource "ovh_cloud_project_network_private_subnet" "main" {
              service_name = var.service_name
              network_id   = ovh_cloud_project_network_private.main.id
              region       = var.region
              start        = "10.50.0.10"
              end          = "10.50.0.250"
              network      = "10.50.0.0/24"
              dhcp         = true
              no_gateway   = false
            }

            resource "ovh_cloud_project_gateway" "main" {
              service_name = var.service_name
              name         = local.name
              model        = "s"
              region       = var.region
              network_id   = tolist(ovh_cloud_project_network_private.main.regions_attributes)[0].openstackid
              subnet_id    = ovh_cloud_project_network_private_subnet.main.id
            }
        """)))

    def cluster(self):
        self.files.add("terraform/cluster.tf", tf(T("""
            resource "ovh_cloud_project_kube" "main" {
              service_name       = var.service_name
              name               = local.name
              region             = var.region
              private_network_id = tolist(ovh_cloud_project_network_private.main.regions_attributes)[0].openstackid
              nodes_subnet_id    = ovh_cloud_project_network_private_subnet.main.id
              plan               = "@@plan@@"
              update_policy      = "ALWAYS_UPDATE"

              private_network_configuration {
                default_vrack_gateway              = ""
                private_network_routing_as_default = false
              }

              depends_on = [ovh_cloud_project_gateway.main]
            }

            resource "ovh_cloud_project_kube_nodepool" "main" {
              service_name  = var.service_name
              kube_id       = ovh_cloud_project_kube.main.id
              name          = "main"
              flavor_name   = var.node_flavor
              desired_nodes = var.node_min
              min_nodes     = var.node_min
              max_nodes     = var.node_max
              autoscale     = true
            }
        """, plan="standard" if self.ha else "free")))

    def databases(self):
        parts = []
        for key, engine, version, name in self.dbs:
            nodes = 3 if (self.ha and engine in ("kafka", "mongodb")) else (2 if self.ha else 1)
            plan = "business" if self.ha else "essential"
            if engine in ("mongodb", "kafka") and not self.ha:
                plan = "essential"
            parts.append(T("""
                resource "ovh_cloud_project_database" "@@name@@" {
                  service_name = var.service_name
                  description  = "${local.name}-@@name@@"
                  engine       = "@@engine@@"
                  version      = "@@version@@"
                  plan         = "@@plan@@"
                  flavor       = var.database_flavor

                  nodes {
                    region     = local.site
                    network_id = tolist(ovh_cloud_project_network_private.main.regions_attributes)[0].openstackid
                    subnet_id  = ovh_cloud_project_network_private_subnet.main.id
                  }

                  dynamic "nodes" {
                    for_each = range(@@extra@@)
                    content {
                      region     = local.site
                      network_id = tolist(ovh_cloud_project_network_private.main.regions_attributes)[0].openstackid
                      subnet_id  = ovh_cloud_project_network_private_subnet.main.id
                    }
                  }

                  ip_restrictions {
                    description = "Private network"
                    ip          = "10.50.0.0/24"
                  }

                  deletion_protection = true
                }
            """, name=name, engine=engine, version=version, plan=plan, extra=nodes - 1))
            if engine in USERS:
                parts.append(T("""
                    resource "@@type@@" "@@name@@_app" {
                      service_name = var.service_name
                      cluster_id   = ovh_cloud_project_database.@@name@@.id
                      name         = "app"
                    }
                """, type=USERS[engine], name=name))
            elif engine == "mysql":
                parts.append(T("""
                    resource "ovh_cloud_project_database_user" "@@name@@_app" {
                      service_name = var.service_name
                      cluster_id   = ovh_cloud_project_database.@@name@@.id
                      engine       = "mysql"
                      name         = "app"
                    }
                """, name=name))
        self.files.add("terraform/databases.tf", tf(*parts))

    def object_storage(self):
        self.files.add("terraform/storage.tf", tf(T("""
            resource "ovh_cloud_project_user" "storage" {
              service_name = var.service_name
              description  = "${local.name} object storage"
              role_names   = ["objectstore_operator"]
            }

            resource "ovh_cloud_project_user_s3_credential" "app" {
              service_name = var.service_name
              user_id      = ovh_cloud_project_user.storage.id
            }

            resource "ovh_cloud_project_storage" "assets" {
              service_name = var.service_name
              name         = "${local.name}-assets"
              region_name  = local.site
              versioning = {
                status = "enabled"
              }
            }
        """)))

    def container_registry(self):
        self.files.add("terraform/registry.tf", tf(T("""
            resource "ovh_cloud_project_containerregistry" "main" {
              service_name = var.service_name
              name         = replace(local.name, "-", "")
              region       = local.site
            }
        """)))

    def outputs(self):
        parts = [T("""
            output "kubeconfig" {
              description = "Save it with: terraform output -raw kubeconfig > kubeconfig.yaml"
              value       = ovh_cloud_project_kube.main.kubeconfig
              sensitive   = true
            }

            output "cluster_url" {
              description = "The Kubernetes API address."
              value       = ovh_cloud_project_kube.main.url
            }
        """)]
        for key, engine, version, name in self.dbs:
            parts.append(T("""
                output "@@name@@_endpoints" {
                  description = "Connection endpoints of the @@engine@@ cluster."
                  value       = ovh_cloud_project_database.@@name@@.endpoints
                }
            """, name=name, engine=engine))
            if engine in USERS or engine == "mysql":
                parts.append(T("""
                    output "@@name@@_app_password" {
                      description = "Generated password of the app user. Put it in the Kubernetes secret, not in a file."
                      value       = @@type@@.@@name@@_app.password
                      sensitive   = true
                    }
                """, name=name, type=USERS.get(engine, "ovh_cloud_project_database_user")))
        if self.storage:
            parts.append(T("""
                output "bucket" {
                  description = "The bucket for uploaded files."
                  value       = ovh_cloud_project_storage.assets.name
                }

                output "bucket_access_key" {
                  description = "S3 access key for the application."
                  value       = ovh_cloud_project_user_s3_credential.app.access_key_id
                  sensitive   = true
                }

                output "bucket_secret_key" {
                  description = "S3 secret key for the application."
                  value       = ovh_cloud_project_user_s3_credential.app.secret_access_key
                  sensitive   = true
                }
            """))
        if self.registry:
            parts.append(T("""
                output "registry_url" {
                  description = "Push your images here."
                  value       = ovh_cloud_project_containerregistry.main.url
                }
            """))
        self.files.add("terraform/outputs.tf", tf(*parts))

    def readme(self):
        m = self.m
        return T("""
            # OVHcloud infrastructure for @@project@@

            Generated from plan @@fp@@. This creates a private network with a gateway, a Managed Kubernetes cluster@@dbs@@@@obj@@@@reg@@.
            The application runs from the manifests in `../kubernetes` (or the Helm chart in `../helm`).

            ## Steps

            1. Create API credentials at https://api.ovh.com/createToken/ and export `OVH_APPLICATION_KEY`, `OVH_APPLICATION_SECRET` and `OVH_CONSUMER_KEY`.
            2. Find your Public Cloud project id in the control panel and put it in `terraform.tfvars` as `service_name`.
            3. Create an Object Storage container for state, copy `backend.hcl.example` to `backend.hcl`, fill it in, and export its S3 credentials as `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`.
            4. `terraform init -backend-config=backend.hcl`, `terraform plan`, `terraform apply`.
            5. `terraform output -raw kubeconfig > kubeconfig.yaml` and `export KUBECONFIG=$PWD/kubeconfig.yaml`.
            6. Install an ingress controller (`ingress-nginx`); OVHcloud creates a load balancer for it. Then follow `../kubernetes/README.md`.

            ## Notes

            - Database flavors, versions and regions vary by site. If an apply is refused, check the current ones in the control panel or the OVHcloud API.
            - Secrets (RabbitMQ and other self-run data services) run inside the cluster. The plan lists a secret store as a gap, so use Kubernetes secrets with an external secrets operator.
        """, project=m.project, fp=m.fingerprint, dbs=(", managed databases (%s)" % ", ".join(e[1] for e in self.dbs) if self.dbs else ""),
                 obj=(", Object Storage" if self.storage else ""), reg=(" and a private registry" if self.registry else ""))


def generate(model):
    return OvhCloud(model).generate()
