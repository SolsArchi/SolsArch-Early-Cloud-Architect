#!/usr/bin/env python3
"""Terraform for a site that is only static files (a single-page app or a built website) on AWS, Azure or Google Cloud.

When the plan has no running service, building a network, a container cluster and a NAT gateway would only cost money. This
writes just what a static site needs: private storage, a content delivery network in front, security headers, a rule that
sends unknown paths to index.html (so client-side routes work), logs, an alert, and a place for build-time secrets. The three
clouds get the same capabilities.
"""
from iac_common import Files, T, fmt_hcl, tf


def has_static_only(model):
    return not any(u["kind"] != "static" for u in model.units)


# --------------------------------------------------------------------------- AWS
def aws(model, bootstrap_text):
    f = Files(model.fingerprint)
    region = model.region or "us-east-1"
    f.add("terraform/versions.tf", tf(T("""
        terraform {
          required_version = ">= 1.9.0"

          required_providers {
            aws = {
              source  = "hashicorp/aws"
              version = "~> 6.0"
            }
          }
        }
    """)))
    f.add("terraform/providers.tf", tf(T("""
        provider "aws" {
          region = var.region

          default_tags {
            tags = local.tags
          }
        }

        # CloudFront publishes its metrics in us-east-1, so the alarm and its topic live there.
        provider "aws" {
          alias  = "us_east_1"
          region = "us-east-1"

          default_tags {
            tags = local.tags
          }
        }
    """)))
    f.add("terraform/variables.tf", tf(T("""
        variable "project_name" {
          description = "Short name used in resource names."
          type        = string
          default     = "@@project@@"
        }

        variable "environment" {
          description = "Environment name, for example production."
          type        = string
          default     = "production"
        }

        variable "region" {
          description = "Region of the storage bucket."
          type        = string
          default     = "@@region@@"
        }

        variable "acm_certificate_arn" {
          description = "Certificate in us-east-1 for your domain. Leave empty to use the default CloudFront address."
          type        = string
          default     = ""
        }

        variable "domain" {
          description = "Your domain name, if you have one."
          type        = string
          default     = ""
        }

        variable "alarm_email" {
          description = "Email address that receives alarms. Leave empty to create the topic without a subscriber."
          type        = string
          default     = ""
        }
    """, project=model.project, region=region)))
    f.add("terraform/main.tf", tf(T("""
        locals {
          name = "${var.project_name}-${var.environment}"
          tags = { project = var.project_name, environment = var.environment, managed-by = "solsarch" }
        }

        resource "aws_s3_bucket" "site" {
          bucket_prefix = "${local.name}-site-"
          force_destroy = false
        }

        resource "aws_s3_bucket_public_access_block" "site" {
          bucket                  = aws_s3_bucket.site.id
          block_public_acls       = true
          block_public_policy     = true
          ignore_public_acls      = true
          restrict_public_buckets = true
        }

        resource "aws_s3_bucket_versioning" "site" {
          bucket = aws_s3_bucket.site.id

          versioning_configuration {
            status = "Enabled"
          }
        }

        resource "aws_s3_bucket_server_side_encryption_configuration" "site" {
          bucket = aws_s3_bucket.site.id

          rule {
            apply_server_side_encryption_by_default {
              sse_algorithm = "AES256"
            }
          }
        }

        resource "aws_s3_bucket_lifecycle_configuration" "site" {
          bucket = aws_s3_bucket.site.id

          rule {
            id     = "expire-old-versions"
            status = "Enabled"

            filter {}

            noncurrent_version_expiration {
              noncurrent_days = 90
            }

            abort_incomplete_multipart_upload {
              days_after_initiation = 7
            }
          }
        }

        resource "aws_s3_bucket" "logs" {
          bucket_prefix = "${local.name}-logs-"
          force_destroy = false
        }

        resource "aws_s3_bucket_public_access_block" "logs" {
          bucket                  = aws_s3_bucket.logs.id
          block_public_acls       = true
          block_public_policy     = true
          ignore_public_acls      = true
          restrict_public_buckets = true
        }

        resource "aws_s3_bucket_ownership_controls" "logs" {
          bucket = aws_s3_bucket.logs.id

          rule {
            object_ownership = "BucketOwnerPreferred"
          }
        }

        resource "aws_s3_bucket_server_side_encryption_configuration" "logs" {
          bucket = aws_s3_bucket.logs.id

          rule {
            apply_server_side_encryption_by_default {
              sse_algorithm = "AES256"
            }
          }
        }

        resource "aws_s3_bucket_lifecycle_configuration" "logs" {
          bucket = aws_s3_bucket.logs.id

          rule {
            id     = "expire-logs"
            status = "Enabled"

            filter {}

            expiration {
              days = 90
            }
          }
        }

        resource "aws_cloudfront_origin_access_control" "site" {
          name                              = local.name
          origin_access_control_origin_type = "s3"
          signing_behavior                  = "always"
          signing_protocol                  = "sigv4"
        }

        data "aws_cloudfront_cache_policy" "optimized" {
          name = "Managed-CachingOptimized"
        }

        resource "aws_cloudfront_response_headers_policy" "security" {
          name = "${local.name}-security"

          security_headers_config {
            content_type_options {
              override = true
            }

            frame_options {
              frame_option = "DENY"
              override     = true
            }

            referrer_policy {
              referrer_policy = "strict-origin-when-cross-origin"
              override        = true
            }

            strict_transport_security {
              access_control_max_age_sec = 31536000
              include_subdomains         = true
              override                   = true
            }
          }
        }

        resource "aws_cloudfront_distribution" "site" {
          enabled             = true
          is_ipv6_enabled     = true
          comment             = local.name
          default_root_object = "index.html"
          price_class         = "PriceClass_100"
          aliases             = var.domain == "" ? [] : [var.domain]

          origin {
            origin_id                = "site"
            domain_name              = aws_s3_bucket.site.bucket_regional_domain_name
            origin_access_control_id = aws_cloudfront_origin_access_control.site.id
          }

          default_cache_behavior {
            target_origin_id           = "site"
            viewer_protocol_policy     = "redirect-to-https"
            allowed_methods            = ["GET", "HEAD", "OPTIONS"]
            cached_methods             = ["GET", "HEAD"]
            compress                   = true
            cache_policy_id            = data.aws_cloudfront_cache_policy.optimized.id
            response_headers_policy_id = aws_cloudfront_response_headers_policy.security.id
          }

          custom_error_response {
            error_code            = 403
            response_code         = 200
            response_page_path    = "/index.html"
            error_caching_min_ttl = 0
          }

          custom_error_response {
            error_code            = 404
            response_code         = 200
            response_page_path    = "/index.html"
            error_caching_min_ttl = 0
          }

          logging_config {
            bucket          = aws_s3_bucket.logs.bucket_domain_name
            include_cookies = false
            prefix          = "cdn/"
          }

          restrictions {
            geo_restriction {
              restriction_type = "none"
            }
          }

          viewer_certificate {
            cloudfront_default_certificate = var.acm_certificate_arn == ""
            acm_certificate_arn            = var.acm_certificate_arn == "" ? null : var.acm_certificate_arn
            ssl_support_method             = var.acm_certificate_arn == "" ? null : "sni-only"
            minimum_protocol_version       = var.acm_certificate_arn == "" ? null : "TLSv1.2_2021"
          }

          depends_on = [aws_s3_bucket_ownership_controls.logs]
        }

        data "aws_iam_policy_document" "site" {
          statement {
            actions   = ["s3:GetObject"]
            resources = ["${aws_s3_bucket.site.arn}/*"]

            principals {
              type        = "Service"
              identifiers = ["cloudfront.amazonaws.com"]
            }

            condition {
              test     = "StringEquals"
              variable = "AWS:SourceArn"
              values   = [aws_cloudfront_distribution.site.arn]
            }
          }
        }

        resource "aws_s3_bucket_policy" "site" {
          bucket = aws_s3_bucket.site.id
          policy = data.aws_iam_policy_document.site.json
        }

        resource "aws_secretsmanager_secret" "app" {
          name_prefix = "${local.name}-build-"
          description = "Build-time settings for ${local.name}. Add the real values as a JSON object."
        }

        resource "aws_sns_topic" "alarms" {
          provider = aws.us_east_1
          name     = "${local.name}-alarms"
        }

        resource "aws_sns_topic_subscription" "email" {
          count     = var.alarm_email == "" ? 0 : 1
          provider  = aws.us_east_1
          topic_arn = aws_sns_topic.alarms.arn
          protocol  = "email"
          endpoint  = var.alarm_email
        }

        resource "aws_cloudwatch_metric_alarm" "errors" {
          provider            = aws.us_east_1
          alarm_name          = "${local.name}-5xx-errors"
          alarm_description   = "More than 5 percent of requests failed."
          namespace           = "AWS/CloudFront"
          metric_name         = "5xxErrorRate"
          statistic           = "Average"
          period              = 300
          evaluation_periods  = 2
          threshold           = 5
          comparison_operator = "GreaterThanThreshold"
          treat_missing_data  = "notBreaching"
          alarm_actions       = [aws_sns_topic.alarms.arn]

          dimensions = {
            DistributionId = aws_cloudfront_distribution.site.id
            Region         = "Global"
          }
        }
    """)))
    f.add("terraform/outputs.tf", tf(T("""
        output "site_address" {
          description = "Open this address to see the site."
          value       = "https://${aws_cloudfront_distribution.site.domain_name}"
        }

        output "bucket_name" {
          description = "Upload the built files here."
          value       = aws_s3_bucket.site.bucket
        }

        output "distribution_id" {
          description = "Needed to clear the cache after an upload."
          value       = aws_cloudfront_distribution.site.id
        }
    """)))
    f.add("terraform/backend.tf", T("""
        terraform {
          backend "s3" {}
        }
    """))
    f.add("terraform/backend.hcl.example", T("""
        bucket       = "REPLACE-with-the-state-bucket-from-bootstrap"
        key          = "@@project@@/production/terraform.tfstate"
        region       = "@@region@@"
        encrypt      = true
        use_lockfile = true
    """, project=model.project, region=region))
    f.add("terraform/bootstrap/main.tf", bootstrap_text)
    f.add("terraform/terraform.tfvars.example", fmt_hcl('project_name = "%s"\nenvironment  = "production"\nregion       = "%s"\n' % (model.project, region)))
    f.add("terraform/README.md", readme(model, "Amazon S3 and CloudFront", [
        "`cd bootstrap && terraform init && terraform apply` once. It creates the encrypted bucket that keeps Terraform state.",
        "Copy `backend.hcl.example` to `backend.hcl` and put the bucket name in it.",
        "`terraform init -backend-config=backend.hcl`, then `terraform apply`.",
        "Build your site, then upload it: `aws s3 sync dist/ s3://$(terraform output -raw bucket_name) --delete`",
        "Clear the cache: `aws cloudfront create-invalidation --distribution-id $(terraform output -raw distribution_id) --paths \"/*\"`"]))
    return f


# --------------------------------------------------------------------------- Azure
def azure(model, bootstrap_text):
    f = Files(model.fingerprint)
    lean = model.lean
    f.add("terraform/versions.tf", tf(T("""
        terraform {
          required_version = ">= 1.9.0"

          required_providers {
            azurerm = {
              source  = "hashicorp/azurerm"
              version = "~> 5.0"
            }
          }
        }

        provider "azurerm" {
          features {
            key_vault {
              purge_soft_delete_on_destroy = false
            }
          }
        }
    """)))
    f.add("terraform/variables.tf", tf(T("""
        variable "project_name" {
          description = "Short name used in resource names."
          type        = string
          default     = "@@project@@"
        }

        variable "environment" {
          description = "Environment name, for example production."
          type        = string
          default     = "production"
        }

        variable "location" {
          description = "Azure region for the resource group and vault. Static Web Apps are limited to a few regions."
          type        = string
          default     = "@@region@@"
        }

        variable "sku" {
          description = "Static Web Apps plan: Free or Standard (Standard adds custom domain limits, SLA and private endpoints)."
          type        = string
          default     = "@@sku@@"
        }

        variable "alarm_email" {
          description = "Email address that receives alerts. Leave empty to create the action group without a receiver."
          type        = string
          default     = ""
        }
    """, project=model.project, region=model.region or "westeurope", sku="Free" if lean else "Standard")))
    f.add("terraform/main.tf", tf(T("""
        data "azurerm_client_config" "current" {}

        locals {
          name = "${var.project_name}-${var.environment}"
          tags = { project = var.project_name, environment = var.environment, managed-by = "solsarch" }
        }

        resource "azurerm_resource_group" "main" {
          name     = "rg-${local.name}"
          location = var.location
          tags     = local.tags
        }

        resource "azurerm_static_web_app" "site" {
          name                = "swa-${local.name}"
          resource_group_name = azurerm_resource_group.main.name
          location            = azurerm_resource_group.main.location
          sku_tier            = var.sku
          sku_size            = var.sku
          tags                = local.tags
        }

        resource "azurerm_log_analytics_workspace" "main" {
          name                = "log-${local.name}"
          resource_group_name = azurerm_resource_group.main.name
          location            = azurerm_resource_group.main.location
          sku                 = "PerGB2018"
          retention_in_days   = 30
          tags                = local.tags
        }

        resource "azurerm_key_vault" "main" {
          name                       = substr(replace("kv-${local.name}", "-", ""), 0, 24)
          resource_group_name        = azurerm_resource_group.main.name
          location                   = azurerm_resource_group.main.location
          tenant_id                  = data.azurerm_client_config.current.tenant_id
          sku_name                   = "standard"
          rbac_authorization_enabled = true
          purge_protection_enabled   = true
          soft_delete_retention_days = 30
          tags                       = local.tags
        }

        resource "azurerm_monitor_action_group" "main" {
          name                = "ag-${local.name}"
          resource_group_name = azurerm_resource_group.main.name
          short_name          = "alerts"

          dynamic "email_receiver" {
            for_each = var.alarm_email == "" ? [] : [var.alarm_email]
            content {
              name          = "owner"
              email_address = email_receiver.value
            }
          }
        }

        resource "azurerm_monitor_activity_log_alert" "service_health" {
          name                = "alert-${local.name}-service-health"
          resource_group_name = azurerm_resource_group.main.name
          location            = "global"
          scopes              = [azurerm_resource_group.main.id]
          description         = "Azure reported a problem that affects the site."

          criteria {
            category = "ServiceHealth"
          }

          action {
            action_group_id = azurerm_monitor_action_group.main.id
          }
        }
    """)))
    f.add("terraform/outputs.tf", tf(T("""
        output "site_address" {
          description = "Open this address to see the site."
          value       = "https://${azurerm_static_web_app.site.default_host_name}"
        }

        output "deployment_token" {
          description = "Used by the deploy command or pipeline to upload the built files. Keep it secret."
          value       = azurerm_static_web_app.site.api_key
          sensitive   = true
        }
    """)))
    f.add("terraform/backend.tf", T("""
        terraform {
          backend "azurerm" {}
        }
    """))
    f.add("terraform/backend.hcl.example", T("""
        resource_group_name  = "REPLACE-with-the-state-resource-group"
        storage_account_name = "REPLACE-with-the-state-storage-account"
        container_name       = "tfstate"
        key                  = "@@project@@/production.tfstate"
    """, project=model.project))
    f.add("terraform/bootstrap/main.tf", bootstrap_text)
    f.add("terraform/terraform.tfvars.example", fmt_hcl('project_name = "%s"\nenvironment  = "production"\nlocation     = "%s"\n' % (model.project, model.region or "westeurope")))
    f.add("terraform/README.md", readme(model, "Azure Static Web Apps", [
        "`az login`, then `cd bootstrap && terraform init && terraform apply` once. It creates the storage account that keeps Terraform state.",
        "Copy `backend.hcl.example` to `backend.hcl` and fill it in.",
        "`terraform init -backend-config=backend.hcl`, then `terraform apply`.",
        "Build your site, then upload it: `npx @azure/static-web-apps-cli deploy ./dist --deployment-token $(terraform output -raw deployment_token)`"]))
    return f


# --------------------------------------------------------------------------- Google Cloud
def gcp(model, bootstrap_text):
    f = Files(model.fingerprint)
    region = model.region or "us-central1"
    f.add("terraform/versions.tf", tf(T("""
        terraform {
          required_version = ">= 1.9.0"

          required_providers {
            google = {
              source  = "hashicorp/google"
              version = "~> 8.0"
            }
            random = {
              source  = "hashicorp/random"
              version = "~> 3.6"
            }
          }
        }

        provider "google" {
          project = var.project_id
          region  = var.region
        }
    """)))
    f.add("terraform/variables.tf", tf(T("""
        variable "project_id" {
          description = "Your Google Cloud project id."
          type        = string
        }

        variable "project_name" {
          description = "Short name used in resource names."
          type        = string
          default     = "@@project@@"
        }

        variable "environment" {
          description = "Environment name, for example production."
          type        = string
          default     = "production"
        }

        variable "region" {
          description = "Region of the storage bucket."
          type        = string
          default     = "@@region@@"
        }

        variable "domain" {
          description = "Your domain name for a managed certificate. Leave empty to serve plain HTTP on the load balancer address."
          type        = string
          default     = ""
        }

        variable "alarm_email" {
          description = "Email address that receives alerts. Leave empty to skip the notification channel."
          type        = string
          default     = ""
        }
    """, project=model.project, region=region)))
    f.add("terraform/main.tf", tf(T("""
        locals {
          name = "${var.project_name}-${var.environment}"
        }

        resource "google_project_service" "apis" {
          for_each = toset(["compute.googleapis.com", "secretmanager.googleapis.com", "monitoring.googleapis.com", "logging.googleapis.com"])

          service            = each.value
          disable_on_destroy = false
        }

        resource "random_id" "suffix" {
          byte_length = 3
        }

        resource "google_storage_bucket" "site" {
          name                        = "${local.name}-site-${random_id.suffix.hex}"
          location                    = var.region
          uniform_bucket_level_access = true
          public_access_prevention    = "inherited"
          force_destroy               = false

          versioning {
            enabled = true
          }

          website {
            main_page_suffix = "index.html"
            not_found_page   = "index.html"
          }

          lifecycle_rule {
            condition {
              num_newer_versions = 5
              with_state         = "ARCHIVED"
            }

            action {
              type = "Delete"
            }
          }

          depends_on = [google_project_service.apis]
        }

        resource "google_storage_bucket_iam_member" "public_read" {
          bucket = google_storage_bucket.site.name
          role   = "roles/storage.objectViewer"
          member = "allUsers"
        }

        resource "google_compute_backend_bucket" "site" {
          name        = "${local.name}-site"
          bucket_name = google_storage_bucket.site.name
          enable_cdn  = true

          cdn_policy {
            cache_mode        = "CACHE_ALL_STATIC"
            default_ttl       = 3600
            client_ttl        = 3600
            max_ttl           = 86400
            negative_caching  = true
            serve_while_stale = 86400
          }

          custom_response_headers = [
            "Strict-Transport-Security: max-age=31536000; includeSubDomains",
            "X-Content-Type-Options: nosniff",
            "X-Frame-Options: DENY",
            "Referrer-Policy: strict-origin-when-cross-origin",
          ]
        }

        resource "google_compute_global_address" "site" {
          name       = "${local.name}-ip"
          depends_on = [google_project_service.apis]
        }

        resource "google_compute_url_map" "site" {
          name            = "${local.name}-site"
          default_service = google_compute_backend_bucket.site.id
        }

        resource "google_compute_url_map" "redirect" {
          name = "${local.name}-redirect"

          default_url_redirect {
            https_redirect         = true
            redirect_response_code = "MOVED_PERMANENTLY_DEFAULT"
            strip_query            = false
          }
        }

        resource "google_compute_managed_ssl_certificate" "site" {
          count = var.domain == "" ? 0 : 1
          name  = "${local.name}-cert"

          managed {
            domains = [var.domain]
          }
        }

        resource "google_compute_target_https_proxy" "site" {
          count            = var.domain == "" ? 0 : 1
          name             = "${local.name}-https"
          url_map          = google_compute_url_map.site.id
          ssl_certificates = [google_compute_managed_ssl_certificate.site[0].id]
        }

        resource "google_compute_target_http_proxy" "site" {
          name    = "${local.name}-http"
          url_map = var.domain == "" ? google_compute_url_map.site.id : google_compute_url_map.redirect.id
        }

        resource "google_compute_global_forwarding_rule" "https" {
          count                 = var.domain == "" ? 0 : 1
          name                  = "${local.name}-https"
          target                = google_compute_target_https_proxy.site[0].id
          ip_address            = google_compute_global_address.site.id
          port_range            = "443"
          load_balancing_scheme = "EXTERNAL_MANAGED"
        }

        resource "google_compute_global_forwarding_rule" "http" {
          name                  = "${local.name}-http"
          target                = google_compute_target_http_proxy.site.id
          ip_address            = google_compute_global_address.site.id
          port_range            = "80"
          load_balancing_scheme = "EXTERNAL_MANAGED"
        }

        resource "google_secret_manager_secret" "app" {
          secret_id = "${local.name}-build"

          replication {
            auto {}
          }

          depends_on = [google_project_service.apis]
        }

        resource "google_monitoring_notification_channel" "email" {
          count        = var.alarm_email == "" ? 0 : 1
          display_name = "${local.name} owner"
          type         = "email"

          labels = {
            email_address = var.alarm_email
          }
        }

        resource "google_monitoring_alert_policy" "errors" {
          display_name = "${local.name} errors"
          combiner     = "OR"

          conditions {
            display_name = "More than 5 percent of requests failed"

            condition_threshold {
              filter          = "resource.type = \\"https_lb_rule\\" AND metric.type = \\"loadbalancing.googleapis.com/https/request_count\\" AND metric.labels.response_code_class = 500"
              comparison      = "COMPARISON_GT"
              threshold_value = 5
              duration        = "300s"

              aggregations {
                alignment_period   = "300s"
                per_series_aligner = "ALIGN_RATE"
              }
            }
          }

          notification_channels = google_monitoring_notification_channel.email[*].id
          depends_on            = [google_project_service.apis]
        }
    """)))
    f.add("terraform/outputs.tf", tf(T("""
        output "site_address" {
          description = "Open this address to see the site (HTTP until you set a domain)."
          value       = var.domain == "" ? "http://${google_compute_global_address.site.address}" : "https://${var.domain}"
        }

        output "load_balancer_ip" {
          description = "Point your domain's A record here."
          value       = google_compute_global_address.site.address
        }

        output "bucket_name" {
          description = "Upload the built files here."
          value       = google_storage_bucket.site.name
        }
    """)))
    f.add("terraform/backend.tf", T("""
        terraform {
          backend "gcs" {}
        }
    """))
    f.add("terraform/backend.hcl.example", T("""
        bucket = "REPLACE-with-the-state-bucket-from-bootstrap"
        prefix = "@@project@@/production"
    """, project=model.project))
    f.add("terraform/bootstrap/main.tf", bootstrap_text)
    f.add("terraform/terraform.tfvars.example", fmt_hcl('project_id   = "REPLACE-with-your-project-id"\nproject_name = "%s"\nenvironment  = "production"\nregion       = "%s"\n' % (model.project, region)))
    f.add("terraform/README.md", readme(model, "Cloud Storage and Cloud CDN", [
        "`gcloud auth application-default login`, then `cd bootstrap && terraform init && terraform apply -var project_id=YOUR_PROJECT` once. It creates the bucket that keeps Terraform state.",
        "Copy `backend.hcl.example` to `backend.hcl` and put the bucket name in it.",
        "`terraform init -backend-config=backend.hcl`, then `terraform apply -var project_id=YOUR_PROJECT`.",
        "Build your site, then upload it: `gcloud storage rsync dist gs://$(terraform output -raw bucket_name) --recursive --delete-unmatched-destination-objects`"]))
    return f


def readme(model, service, steps):
    return T("""
        # Terraform for @@project@@: a static site on @@service@@

        Plan fingerprint @@fp@@. The plan has no running service, so this builds only what a static site needs: private storage,
        a content delivery network in front with security headers, a rule that sends unknown paths to `index.html` (so client-side
        routes work), logs, an alert, and a place for build-time secrets. No network, cluster or NAT gateway is created.

        ## Order of work

        @@steps@@

        ## What you must still decide

        - A domain name and a certificate (the site works on the provider's own address until then).
        - Your build: nothing is built here.
    """, project=model.project, service=service, fp=model.fingerprint, steps="\n".join("%d. %s" % (i, s) for i, s in enumerate(steps, 1)))
