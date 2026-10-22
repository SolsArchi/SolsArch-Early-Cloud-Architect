#!/usr/bin/env python3
"""Terraform for Hetzner Cloud, generated from the plan.

Hetzner sells servers, networks, firewalls, load balancers and volumes, so this creates exactly those: a private network,
a firewall that denies by default, app servers spread across hosts, a data server with an encrypted-at-rest volume when
the plan runs PostgreSQL, MongoDB or Redis itself, and a load balancer with a managed certificate when you have a domain.
The Ansible in ../ansible then configures the servers, so the software setup is the same as on every other cloud.
"""
from iac_common import Files, T, fmt_hcl, selfrun, tf

DEFAULT_LOCATION = "fsn1"
ZONES = {"fsn1": "eu-central", "nbg1": "eu-central", "hel1": "eu-central", "ash": "us-east", "hil": "us-west", "sin": "ap-southeast"}
SERVER_TYPES = [(1.5, "cx22"), (3.0, "cx32"), (6.0, "cx42"), (1e9, "cx52")]


class Hetzner:
    def __init__(self, model):
        self.m = model
        self.files = Files(model.fingerprint)
        self.public = [u for u in model.public_units() if u["kind"] != "static"]
        self.location = model.region if model.region in ZONES else DEFAULT_LOCATION
        self.zone = ZONES[self.location]
        self.data = [k for k in ("database", "database_mysql", "database_mongo", "cache", "queue_rabbitmq") if selfrun(model, k)]
        self.has_lb = bool(self.public) and model.has("ingress")
        self.count = 1 if model.single_az else max(2, model.replicas)
        need = sum(float(model.sizing["unit_vcpu"]) for u in model.units if u["kind"] != "static") * model.replicas / max(self.count, 1)
        self.app_type = next(t for limit, t in SERVER_TYPES if need <= limit)
        self.data_type = "cx32"

    def generate(self):
        m = self.m
        self.versions()
        self.variables()
        self.network()
        self.servers()
        if self.has_lb:
            self.load_balancer()
        self.outputs()
        self.files.add("terraform/backend.tf", T("""
            terraform {
              backend "s3" {}
            }
        """))
        self.files.add("terraform/backend.hcl.example", T("""
            # Hetzner Object Storage speaks the S3 protocol. Create a bucket in the console, then fill this in.
            bucket                      = "REPLACE-with-your-state-bucket"
            key                         = "@@project@@/production.tfstate"
            region                      = "@@location@@"
            endpoints                   = { s3 = "https://@@location@@.your-objectstorage.com" }
            skip_credentials_validation = true
            skip_region_validation      = true
            skip_requesting_account_id  = true
            skip_metadata_api_check     = true
            skip_s3_checksum            = true
            use_path_style              = true
        """, project=m.project, location=self.location))
        self.files.add("terraform/terraform.tfvars.example", fmt_hcl(
            'project_name   = "%s"\nenvironment    = "production"\nlocation       = "%s"\nssh_public_key = "REPLACE-with-your-public-key"\n' % (m.project, self.location)))
        self.files.add("terraform/README.md", self.readme())
        return self.files

    def versions(self):
        self.files.add("terraform/versions.tf", tf(T("""
            terraform {
              required_version = ">= 1.9.0"

              required_providers {
                hcloud = {
                  source  = "hetznercloud/hcloud"
                  version = "~> 1.50"
                }
              }
            }

            provider "hcloud" {}
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

            variable "location" {
              description = "Hetzner location: fsn1, nbg1, hel1, ash, hil or sin."
              type        = string
              default     = "@@location@@"
            }

            variable "ssh_public_key" {
              description = "The public key your team uses to sign in to the servers."
              type        = string
            }

            variable "ssh_allowed_cidrs" {
              description = "Addresses allowed to reach SSH. Narrow this to your office or VPN range."
              type        = list(string)
              default     = ["0.0.0.0/0", "::/0"]
            }

            variable "app_server_type" {
              description = "Server type for the application servers."
              type        = string
              default     = "@@app_type@@"
            }

            variable "app_server_count" {
              description = "How many application servers. Two or more gives you a spread across hosts."
              type        = number
              default     = @@count@@
            }

            variable "domain" {
              description = "Public domain name. Leave empty to serve plain HTTP until you have one."
              type        = string
              default     = ""
            }

            variable "enable_backups" {
              description = "Hetzner server backups (a daily image, about 20 percent of the server price)."
              type        = bool
              default     = true
            }
        """, location=self.location, app_type=self.app_type, count=self.count)))

    def network(self):
        m = self.m
        self.files.add("terraform/network.tf", tf(T("""
            locals {
              name   = "${var.project_name}-${var.environment}"
              labels = { project = var.project_name, environment = var.environment, managed-by = "solsarch" }
            }

            resource "hcloud_ssh_key" "deploy" {
              name       = "${local.name}-deploy"
              public_key = var.ssh_public_key
              labels     = local.labels
            }

            resource "hcloud_network" "main" {
              name     = local.name
              ip_range = "10.0.0.0/16"
              labels   = local.labels
            }

            resource "hcloud_network_subnet" "main" {
              network_id   = hcloud_network.main.id
              type         = "cloud"
              network_zone = "@@zone@@"
              ip_range     = "10.0.1.0/24"
            }

            resource "hcloud_firewall" "app" {
              name   = "${local.name}-app"
              labels = local.labels

              rule {
                direction   = "in"
                protocol    = "tcp"
                port        = "22"
                source_ips  = var.ssh_allowed_cidrs
                description = "SSH from the allowed ranges"
              }

              rule {
                direction   = "in"
                protocol    = "tcp"
                port        = "80"
                source_ips  = ["0.0.0.0/0", "::/0"]
                description = "HTTP"
              }

              rule {
                direction   = "in"
                protocol    = "tcp"
                port        = "443"
                source_ips  = ["0.0.0.0/0", "::/0"]
                description = "HTTPS"
              }

              rule {
                direction   = "in"
                protocol    = "icmp"
                source_ips  = ["0.0.0.0/0", "::/0"]
                description = "Ping"
              }
            }

            resource "hcloud_firewall" "data" {
              name   = "${local.name}-data"
              labels = local.labels

              rule {
                direction   = "in"
                protocol    = "tcp"
                port        = "22"
                source_ips  = var.ssh_allowed_cidrs
                description = "SSH from the allowed ranges. Data ports are reachable only on the private network."
              }
            }

            resource "hcloud_placement_group" "app" {
              name   = "${local.name}-app"
              type   = "spread"
              labels = local.labels
            }
        """, zone=self.zone)))

    def servers(self):
        m = self.m
        parts = [T("""
            resource "hcloud_server" "app" {
              count              = var.app_server_count
              name               = "${local.name}-app-${count.index + 1}"
              server_type        = var.app_server_type
              image              = "ubuntu-24.04"
              location           = var.location
              ssh_keys           = [hcloud_ssh_key.deploy.id]
              firewall_ids       = [hcloud_firewall.app.id]
              placement_group_id = hcloud_placement_group.app.id
              backups            = var.enable_backups
              user_data          = local.cloud_init
              labels             = merge(local.labels, { role = "app" })

              network {
                network_id = hcloud_network.main.id
                ip         = "10.0.1.${count.index + 10}"
              }

              public_net {
                ipv4_enabled = true
                ipv6_enabled = true
              }

              depends_on = [hcloud_network_subnet.main]

              lifecycle {
                ignore_changes = [user_data, ssh_keys]
              }
            }
        """), T("""
            locals {
              cloud_init = <<-EOT
                #cloud-config
                package_update: true
                users:
                  - name: deploy
                    groups: [sudo]
                    shell: /bin/bash
                    sudo: ALL=(ALL) NOPASSWD:ALL
                    ssh_authorized_keys:
                      - ${var.ssh_public_key}
                ssh_pwauth: false
              EOT
            }
        """)]
        if self.data:
            parts.append(T("""
                resource "hcloud_server" "data" {
                  name         = "${local.name}-data-1"
                  server_type  = "@@type@@"
                  image        = "ubuntu-24.04"
                  location     = var.location
                  ssh_keys     = [hcloud_ssh_key.deploy.id]
                  firewall_ids = [hcloud_firewall.data.id]
                  backups      = var.enable_backups
                  user_data    = local.cloud_init
                  labels       = merge(local.labels, { role = "data" })

                  network {
                    network_id = hcloud_network.main.id
                    ip         = "10.0.1.50"
                  }

                  public_net {
                    ipv4_enabled = true
                    ipv6_enabled = true
                  }

                  depends_on = [hcloud_network_subnet.main]

                  lifecycle {
                    prevent_destroy = true
                    ignore_changes  = [user_data, ssh_keys]
                  }
                }

                resource "hcloud_volume" "data" {
                  name              = "${local.name}-data"
                  size              = var.data_volume_size_gb
                  location          = var.location
                  format            = "ext4"
                  delete_protection = true
                  labels            = local.labels
                }

                resource "hcloud_volume_attachment" "data" {
                  volume_id = hcloud_volume.data.id
                  server_id = hcloud_server.data.id
                  automount = true
                }

                variable "data_volume_size_gb" {
                  description = "Size of the volume that holds the database files."
                  type        = number
                  default     = 50
                }
            """, type=self.data_type))
        self.files.add("terraform/servers.tf", tf(*parts))

    def load_balancer(self):
        self.files.add("terraform/load_balancer.tf", tf(T("""
            resource "hcloud_load_balancer" "main" {
              name               = local.name
              load_balancer_type = "lb11"
              location           = var.location
              labels             = local.labels
            }

            resource "hcloud_load_balancer_network" "main" {
              load_balancer_id = hcloud_load_balancer.main.id
              subnet_id        = hcloud_network_subnet.main.id
              ip               = "10.0.1.5"
            }

            resource "hcloud_load_balancer_target" "app" {
              type             = "label_selector"
              load_balancer_id = hcloud_load_balancer.main.id
              label_selector   = "project=${var.project_name},environment=${var.environment},role=app"
              use_private_ip   = true

              depends_on = [hcloud_load_balancer_network.main]
            }

            resource "hcloud_managed_certificate" "main" {
              count        = var.domain == "" ? 0 : 1
              name         = "${local.name}-cert"
              domain_names = [var.domain]
              labels       = local.labels
            }

            resource "hcloud_load_balancer_service" "http" {
              load_balancer_id = hcloud_load_balancer.main.id
              protocol         = "http"
              listen_port      = 80
              destination_port = 80

              http {
                redirect_http = false
              }

              health_check {
                protocol = "http"
                port     = 80
                interval = 15
                timeout  = 10
                retries  = 3

                http {
                  path         = "@@health@@"
                  status_codes = ["2??", "3??"]
                }
              }
            }

            resource "hcloud_load_balancer_service" "https" {
              count            = var.domain == "" ? 0 : 1
              load_balancer_id = hcloud_load_balancer.main.id
              protocol         = "https"
              listen_port      = 443
              destination_port = 80

              http {
                certificates  = [hcloud_managed_certificate.main[0].id]
                redirect_http = true
              }

              health_check {
                protocol = "http"
                port     = 80
                interval = 15
                timeout  = 10
                retries  = 3

                http {
                  path         = "@@health@@"
                  status_codes = ["2??", "3??"]
                }
              }
            }
        """, health=self.public[0]["health_path"] or "/")))

    def outputs(self):
        parts = [T("""
            output "ansible_inventory" {
              description = "Paste into ansible/inventory/production.ini."
              value = join("\\n", concat(
                ["[app]"],
                [for s in hcloud_server.app : "${s.name} ansible_host=${s.ipv4_address} ansible_user=deploy"],
                @@data@@
              ))
            }

            output "app_public_ips" {
              description = "Public addresses of the application servers."
              value       = hcloud_server.app[*].ipv4_address
            }

            output "private_network" {
              description = "The private network the servers share."
              value       = hcloud_network.main.ip_range
            }
        """, data=('["", "[data]", "${hcloud_server.data.name} ansible_host=${hcloud_server.data.ipv4_address} ansible_user=deploy"]' if self.data else "[]"))]
        if self.data:
            parts.append(T("""
                output "data_private_address" {
                  description = "Set data_host_private_address in ansible/group_vars/all/main.yml to this."
                  value       = "10.0.1.50"
                }
            """))
        if self.has_lb:
            parts.append(T("""
                output "load_balancer_ip" {
                  description = "Point your domain's A record here."
                  value       = hcloud_load_balancer.main.ipv4
                }
            """))
        self.files.add("terraform/outputs.tf", tf(*parts))

    def readme(self):
        m = self.m
        return T("""
            # Hetzner Cloud infrastructure for @@project@@

            Generated from plan @@fp@@. This creates the private network, a firewall that denies by default, @@count@@ application
            server(s) spread across hosts@@data@@@@lb@@.

            ## Steps

            1. Create an API token in the Hetzner Cloud console (Security, API tokens, read and write) and export it: `export HCLOUD_TOKEN=...`
            2. Create an Object Storage bucket for state, copy `backend.hcl.example` to `backend.hcl` and fill it in.
            3. `cp terraform.tfvars.example terraform.tfvars` and set `ssh_public_key`; narrow `ssh_allowed_cidrs`.
            4. `terraform init -backend-config=backend.hcl`, then `terraform plan` and `terraform apply`.
            5. Copy the `ansible_inventory` output into `../ansible/inventory/production.ini` and follow `../ansible/README.md`.

            ## Notes

            - Hetzner has no managed database, queue or secret store, so the plan runs the data services you use in containers on the data server. Backups are the Hetzner server backup plus the nightly dumps the Ansible role takes.
            - The data server has `prevent_destroy` and its volume has delete protection. Remove them on purpose if you ever need to tear it down.
        """, project=m.project, fp=m.fingerprint, count=self.count,
                 data=(", a data server with a separate volume" if self.data else ""),
                 lb=(" and a load balancer" if self.has_lb else ""))


def generate(model):
    return Hetzner(model).generate()
