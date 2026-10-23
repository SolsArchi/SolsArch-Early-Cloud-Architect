#!/usr/bin/env python3
"""Terraform for a site that is only static files on Alibaba Cloud, Huawei Cloud, Tencent Cloud, Oracle Cloud, IBM Cloud and Scaleway.

iac_static.py does this for AWS, Azure and Google Cloud. These six clouds used to get their container design for a plain single-page
app, which meant a private network, a container service and a NAT gateway that bill every hour for nothing. This writes only what a
static site needs: one bucket with website hosting turned on (every unknown path answers index.html so client-side routes work),
a content delivery network in front where the cloud has one Terraform can create, logs, the Terraform state backend, outputs and a
README. The backend, versions and provider files are taken from each cloud's normal generator so the two always agree.
"""
from iac_common import Files, T, fmt_hcl, hcl_str, tf

KEEP = ("terraform/versions.tf", "terraform/backend.tf", "terraform/backend.hcl.example", "terraform/bootstrap/main.tf")


def base(model, cls):
    """A file set holding the versions, backend and bootstrap files of the cloud's normal generator."""
    full = cls(model).generate()
    files = Files(model.fingerprint)
    for path in KEEP:
        if path in full.files:
            files.files[path] = full.files[path]
    return files, full


def common_variables(region_description, default_region, extra=""):
    return T("""
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
          description = "@@desc@@"
          type        = string
          default     = "@@region@@"
        }

        variable "domain_name" {
          description = "The address visitors will use, for example www.example.com. Leave empty to use the bucket address and create no CDN."
          type        = string
          default     = ""
        }
    """, desc=region_description, region=default_region) + extra


def finish(model, files, provider_name, steps, notes, region):
    files.add("terraform/terraform.tfvars.example", fmt_hcl(
        'project_name = "%s"\nenvironment  = "production"\nregion       = "%s"\ndomain_name  = ""\n' % (model.project, region)))
    files.add("terraform/README.md", T("""
        # @@provider@@ static site for @@project@@

        Generated from plan @@fp@@. The repository has no running service, so this creates only what a static site needs: a bucket with
        website hosting@@cdn@@. No network, container service or NAT gateway is created, so there is no hourly charge for idle infrastructure.

        ## Steps

        @@steps@@

        ## Notes

        @@notes@@
    """, provider=provider_name, project=model.project, fp=model.fingerprint, cdn=" and, once you set `domain_name`, a content delivery network in front of it",
                              steps="\n".join("%d. %s" % (i, s) for i, s in enumerate(steps, 1)),
                              notes="\n".join("- " + n for n in notes)))
    return files


# --------------------------------------------------------------------------- Alibaba
def alibaba(model):
    import iac_alibaba
    f, full = base(model, iac_alibaba.Alibaba)
    region = full.files["terraform/terraform.tfvars.example"].split('region       = "')[1].split('"')[0]
    f.add("terraform/variables.tf", tf(common_variables("Alibaba Cloud region, for example cn-hangzhou, ap-southeast-1, eu-central-1, us-west-1.", region)))
    f.add("terraform/site.tf", tf(T("""
        locals {
          name = "${var.project_name}-${var.environment}"
        }

        resource "random_id" "suffix" {
          byte_length = 3
        }

        # Website hosting needs the bucket to be readable by everyone; it holds only the public files of the site.
        resource "alicloud_oss_bucket" "site" {
          bucket        = "${local.name}-site-${random_id.suffix.hex}"
          acl           = "public-read"
          force_destroy = false

          server_side_encryption_rule {
            sse_algorithm = "AES256"
          }

          versioning {
            status = "Enabled"
          }
        }

        resource "alicloud_oss_bucket_website" "site" {
          bucket = alicloud_oss_bucket.site.bucket

          index_document {
            suffix      = "index.html"
            support_sub_dir = true
          }

          error_document {
            key         = "index.html"
            http_status = "200"
          }
        }

        resource "alicloud_cdn_domain_new" "site" {
          count       = var.domain_name == "" ? 0 : 1
          domain_name = var.domain_name
          cdn_type    = "web"

          sources {
            content  = alicloud_oss_bucket.site.extranet_endpoint
            type     = "oss"
            port     = 80
            priority = 20
            weight   = 15
          }
        }

        resource "alicloud_log_project" "site" {
          project_name = "${local.name}-${random_id.suffix.hex}"
          description  = "Logs of ${local.name}"
        }

        resource "alicloud_log_store" "access" {
          project_name     = alicloud_log_project.site.project_name
          logstore_name    = "access"
          retention_period = 7
        }
    """)))
    f.add("terraform/outputs.tf", tf(T("""
        output "bucket_name" {
          description = "Upload the built site here, for example with: ossutil cp -r dist/ oss://BUCKET/"
          value       = alicloud_oss_bucket.site.bucket
        }

        output "website_endpoint" {
          description = "Address of the bucket's website."
          value       = "${alicloud_oss_bucket.site.bucket}.${alicloud_oss_bucket.site.extranet_endpoint}"
        }

        output "cdn_cname" {
          description = "Point your domain's CNAME record at this address once domain_name is set."
          value       = var.domain_name == "" ? null : alicloud_cdn_domain_new.site[0].cname
        }
    """)))
    return finish(model, f, "Alibaba Cloud", [
        "Create an access key for a RAM user and export `ALICLOUD_ACCESS_KEY` and `ALICLOUD_SECRET_KEY`.",
        "Create an OSS bucket for Terraform state, copy `backend.hcl.example` to `backend.hcl` and fill it in.",
        "`terraform init -backend-config=backend.hcl`, `terraform plan`, `terraform apply`.",
        "Build the site and upload the files to the bucket named in the outputs (`ossutil cp -r dist/ oss://BUCKET/`).",
        "To use your own domain set `domain_name`, apply again and add the CNAME record from the outputs."],
        ["A CDN domain on Alibaba Cloud needs a domain you own; for mainland China regions it also needs an ICP filing.",
         "The bucket is public-read on purpose: website hosting serves the files without a sign-in."], region)


# --------------------------------------------------------------------------- Huawei
def huawei(model):
    import iac_huawei
    f, full = base(model, iac_huawei.Huawei)
    region = full.files["terraform/terraform.tfvars.example"].split('region       = "')[1].split('"')[0]
    f.add("terraform/variables.tf", tf(common_variables("Huawei Cloud region, for example ap-southeast-3, ap-southeast-1, cn-north-4.", region)))
    f.add("terraform/site.tf", tf(T("""
        locals {
          name = "${var.project_name}-${var.environment}"
        }

        resource "random_id" "suffix" {
          byte_length = 3
        }

        # Website hosting needs the bucket to be readable by everyone; it holds only the public files of the site.
        resource "huaweicloud_obs_bucket" "site" {
          bucket        = "${local.name}-site-${random_id.suffix.hex}"
          acl           = "public-read"
          force_destroy = false
          versioning    = true
          encryption    = true

          website {
            index_document = "index.html"
            error_document = "index.html"
          }
        }

        resource "huaweicloud_cdn_domain" "site" {
          count        = var.domain_name == "" ? 0 : 1
          name         = var.domain_name
          type         = "web"
          service_area = "outside_mainland_china"

          sources {
            origin                  = huaweicloud_obs_bucket.site.bucket_domain_name
            origin_type             = "obs_bucket"
            active                  = 1
            obs_web_hosting_enabled = true
          }
        }

        resource "huaweicloud_lts_group" "site" {
          group_name  = local.name
          ttl_in_days = 7
        }

        resource "huaweicloud_lts_stream" "access" {
          group_id    = huaweicloud_lts_group.site.id
          stream_name = "access"
        }
    """)))
    f.add("terraform/outputs.tf", tf(T("""
        output "bucket_name" {
          description = "Upload the built site here, for example with obsutil: obsutil cp dist/ obs://BUCKET/ -r -f"
          value       = huaweicloud_obs_bucket.site.bucket
        }

        output "website_endpoint" {
          description = "Address of the bucket."
          value       = huaweicloud_obs_bucket.site.bucket_domain_name
        }

        output "cdn_cname" {
          description = "Point your domain's CNAME record at this address once domain_name is set."
          value       = var.domain_name == "" ? null : huaweicloud_cdn_domain.site[0].cname
        }
    """)))
    return finish(model, f, "Huawei Cloud", [
        "Create an access key (My Credentials) and export `HW_ACCESS_KEY` and `HW_SECRET_KEY`.",
        "Create an OBS bucket for Terraform state, copy `backend.hcl.example` to `backend.hcl` and fill it in.",
        "`terraform init -backend-config=backend.hcl`, `terraform plan`, `terraform apply`.",
        "Build the site and upload the files to the bucket named in the outputs.",
        "To use your own domain set `domain_name`, apply again and add the CNAME record from the outputs."],
        ["A CDN domain on Huawei Cloud needs a domain you own; mainland China service areas need an ICP filing.",
         "The bucket is public-read on purpose: website hosting serves the files without a sign-in."], region)


# --------------------------------------------------------------------------- Tencent
def tencent(model):
    import iac_tencent
    f, full = base(model, iac_tencent.Tencent)
    region = full.files["terraform/terraform.tfvars.example"].split('region       = "')[1].split('"')[0]
    f.add("terraform/variables.tf", tf(common_variables("Tencent Cloud region, for example ap-singapore, ap-hongkong, ap-guangzhou.", region, T("""

        variable "app_id" {
          description = "Your Tencent Cloud account APPID (digits). COS bucket names must end with it."
          type        = string
        }
    """))))
    f.add("terraform/site.tf", tf(T("""
        locals {
          name = "${var.project_name}-${var.environment}"
        }

        # Website hosting needs the bucket to be readable by everyone; it holds only the public files of the site.
        resource "tencentcloud_cos_bucket" "site" {
          bucket            = "${local.name}-site-${var.app_id}"
          acl               = "public-read"
          versioning_enable = true
          force_clean       = false

          website {
            index_document = "index.html"
            error_document = "index.html"
          }
        }

        resource "tencentcloud_cdn_domain" "site" {
          count        = var.domain_name == "" ? 0 : 1
          domain       = var.domain_name
          service_type = "web"

          origin {
            origin_type = "cos"
            origin_list = [tencentcloud_cos_bucket.site.cos_bucket_url]
          }
        }

        resource "tencentcloud_cls_logset" "site" {
          logset_name = local.name
        }

        resource "tencentcloud_cls_topic" "access" {
          logset_id  = tencentcloud_cls_logset.site.id
          topic_name = "access"
          period     = 7
        }
    """)))
    f.add("terraform/outputs.tf", tf(T("""
        output "bucket_name" {
          description = "Upload the built site here, for example with coscli: coscli sync dist/ cos://BUCKET/ -r"
          value       = tencentcloud_cos_bucket.site.bucket
        }

        output "website_endpoint" {
          description = "Address of the bucket's website."
          value       = tencentcloud_cos_bucket.site.website[0].endpoint
        }

        output "cdn_cname" {
          description = "Point your domain's CNAME record at this address once domain_name is set."
          value       = var.domain_name == "" ? null : tencentcloud_cdn_domain.site[0].cname
        }
    """)))
    return finish(model, f, "Tencent Cloud", [
        "Create an API key and export `TENCENTCLOUD_SECRET_ID` and `TENCENTCLOUD_SECRET_KEY`. Find your APPID on the Account Information page and put it in `app_id`.",
        "Create a COS bucket for Terraform state, copy `backend.hcl.example` to `backend.hcl` and fill it in.",
        "`terraform init -backend-config=backend.hcl`, `terraform plan`, `terraform apply`.",
        "Build the site and upload the files to the bucket named in the outputs.",
        "To use your own domain set `domain_name`, apply again and add the CNAME record from the outputs."],
        ["A CDN domain on Tencent Cloud needs a domain you own; mainland China acceleration needs an ICP filing.",
         "The bucket is public-read on purpose: website hosting serves the files without a sign-in."], region)


# --------------------------------------------------------------------------- Oracle
def oci(model):
    import iac_oci
    f, full = base(model, iac_oci.Oci)
    f.add("terraform/variables.tf", tf(T("""
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
          description = "Oracle Cloud region identifier, for example us-ashburn-1 or eu-frankfurt-1."
          type        = string
          default     = "us-ashburn-1"
        }

        variable "compartment_ocid" {
          description = "The compartment that will hold the bucket."
          type        = string
        }
    """)))
    f.add("terraform/site.tf", tf(T("""
        locals {
          name = "${var.project_name}-${var.environment}"
        }

        data "oci_objectstorage_namespace" "ns" {
          compartment_id = var.compartment_ocid
        }

        # Reading objects without a sign-in is what makes the bucket a website; it holds only the public files of the site.
        resource "oci_objectstorage_bucket" "site" {
          compartment_id = var.compartment_ocid
          namespace      = data.oci_objectstorage_namespace.ns.namespace
          name           = "${local.name}-site"
          access_type    = "ObjectRead"
          storage_tier   = "Standard"
          versioning     = "Enabled"
        }

        resource "oci_logging_log_group" "site" {
          compartment_id = var.compartment_ocid
          display_name   = local.name
          description    = "Logs of ${local.name}"
        }
    """)))
    f.add("terraform/outputs.tf", tf(T("""
        output "bucket_name" {
          description = "Upload the built site here, for example with: oci os object bulk-upload -bn BUCKET --src-dir dist"
          value       = oci_objectstorage_bucket.site.name
        }

        output "site_url" {
          description = "Base address of the objects, for example <this>/index.html."
          value       = "https://objectstorage.${var.region}.oraclecloud.com/n/${data.oci_objectstorage_namespace.ns.namespace}/b/${oci_objectstorage_bucket.site.name}/o/"
        }
    """)))
    f.add("terraform/terraform.tfvars.example", fmt_hcl(
        'project_name     = "%s"\nenvironment      = "production"\ncompartment_ocid = "REPLACE-with-your-compartment-ocid"\nregion           = "us-ashburn-1"\n' % model.project))
    f.add("terraform/README.md", T("""
        # Oracle Cloud static site for @@project@@

        Generated from plan @@fp@@. The repository has no running service, so this creates only a public-read Object Storage bucket and a
        log group. No network, container service or NAT gateway is created.

        ## Steps

        1. Set up the OCI CLI configuration (`oci setup config`) or the `TF_VAR_` / `OCI_` environment variables for the Terraform provider.
        2. Create a bucket for Terraform state, copy `backend.hcl.example` to `backend.hcl` and fill it in.
        3. Put your compartment id in `terraform.tfvars`, then `terraform init -backend-config=backend.hcl`, `terraform plan`, `terraform apply`.
        4. Build the site and upload it: `oci os object bulk-upload -bn BUCKET --src-dir dist`.

        ## Notes

        - Oracle Object Storage serves the files but has no content delivery network or index-document setting that Terraform can create. Visitors
          must use the full object address, and client-side routes need a front end such as Cloudflare in front (use `--also cloudflare`).
        - The bucket is public-read on purpose; it holds only the public files of the site.
    """, project=model.project, fp=model.fingerprint))
    return f


# --------------------------------------------------------------------------- IBM
def ibm(model):
    import iac_ibm
    f, full = base(model, iac_ibm.Ibm)
    region = full.files["terraform/terraform.tfvars.example"].split('region       = "')[1].split('"')[0]
    f.add("terraform/variables.tf", tf(common_variables("IBM Cloud region, for example us-south, eu-de, eu-gb, jp-tok.", region)))
    f.add("terraform/site.tf", tf(T("""
        locals {
          name = "${var.project_name}-${var.environment}"
        }

        resource "random_id" "suffix" {
          byte_length = 3
        }

        resource "ibm_resource_group" "site" {
          name = local.name
        }

        resource "ibm_resource_instance" "cos" {
          name              = "${local.name}-cos"
          service           = "cloud-object-storage"
          plan              = "standard"
          location          = "global"
          resource_group_id = ibm_resource_group.site.id
        }

        resource "ibm_cos_bucket" "site" {
          bucket_name          = "${local.name}-site-${random_id.suffix.hex}"
          resource_instance_id = ibm_resource_instance.cos.id
          region_location      = var.region
          storage_class        = "smart"

          object_versioning {
            enable = true
          }
        }

        resource "ibm_cos_bucket_website_configuration" "site" {
          bucket_crn      = ibm_cos_bucket.site.crn
          bucket_location = var.region

          website_configuration {
            index_document {
              suffix = "index.html"
            }

            error_document {
              key = "index.html"
            }
          }
        }

        resource "ibm_resource_instance" "logs" {
          name              = "${local.name}-logs"
          service           = "logs"
          plan              = "standard"
          location          = var.region
          resource_group_id = ibm_resource_group.site.id
        }
    """)))
    f.add("terraform/outputs.tf", tf(T("""
        output "bucket_name" {
          description = "Upload the built site here with the IBM Cloud CLI: ibmcloud cos object-put or the console."
          value       = ibm_cos_bucket.site.bucket_name
        }

        output "website_endpoint" {
          description = "Address of the bucket's website."
          value       = ibm_cos_bucket_website_configuration.site.website_endpoint
        }
    """)))
    return finish(model, f, "IBM Cloud", [
        "Create an API key (Manage, Access, API keys) and export `IC_API_KEY`.",
        "Create a Cloud Object Storage bucket for Terraform state with HMAC credentials, copy `backend.hcl.example` to `backend.hcl` and fill it in.",
        "`terraform init -backend-config=backend.hcl`, `terraform plan`, `terraform apply`.",
        "Build the site and upload the files to the bucket named in the outputs.",
        "Make the bucket's objects publicly readable in the console (Permissions, Public access) so the website endpoint can serve them."],
        ["IBM Cloud Object Storage serves the files from the website endpoint; a content delivery network needs IBM Cloud Internet Services and a domain you own, which Terraform cannot decide for you.",
         "Public access to objects is a console or CLI setting, not part of the Terraform bucket resource."], region)


# --------------------------------------------------------------------------- Scaleway
def scaleway(model):
    import iac_scaleway
    f, full = base(model, iac_scaleway.Scaleway)
    region = full.files["terraform/terraform.tfvars.example"].split('region       = "')[1].split('"')[0]
    f.add("terraform/variables.tf", tf(common_variables("Scaleway region, for example fr-par, nl-ams or pl-waw.", region)))
    f.add("terraform/site.tf", tf(T("""
        locals {
          name = "${var.project_name}-${var.environment}"
        }

        resource "random_id" "suffix" {
          byte_length = 3
        }

        resource "scaleway_object_bucket" "site" {
          name = "${local.name}-site-${random_id.suffix.hex}"

          versioning {
            enabled = true
          }
        }

        # Website hosting needs the objects to be readable by everyone; the bucket holds only the public files of the site.
        resource "scaleway_object_bucket_acl" "site" {
          bucket = scaleway_object_bucket.site.name
          acl    = "public-read"
        }

        resource "scaleway_object_bucket_website_configuration" "site" {
          bucket = scaleway_object_bucket.site.name

          index_document {
            suffix = "index.html"
          }

          error_document {
            key = "index.html"
          }
        }

        # Edge Services puts a cache, TLS and a web application firewall in front of the bucket.
        resource "scaleway_edge_services_pipeline" "site" {
          name        = local.name
          description = "Cache and TLS in front of the static site"
        }

        resource "scaleway_edge_services_backend_stage" "site" {
          pipeline_id = scaleway_edge_services_pipeline.site.id

          s3_backend_config {
            bucket_name   = scaleway_object_bucket.site.name
            bucket_region = var.region
          }
        }

        resource "scaleway_edge_services_waf_stage" "site" {
          pipeline_id      = scaleway_edge_services_pipeline.site.id
          backend_stage_id = scaleway_edge_services_backend_stage.site.id
          mode             = "enable"
          paranoia_level   = 2
        }

        resource "scaleway_edge_services_route_stage" "site" {
          pipeline_id  = scaleway_edge_services_pipeline.site.id
          waf_stage_id = scaleway_edge_services_waf_stage.site.id

          rule {
            backend_stage_id = scaleway_edge_services_backend_stage.site.id

            rule_http_match {
              method_filters = ["get", "head"]

              path_filter {
                path_filter_type = "regex"
                value            = ".*"
              }
            }
          }
        }

        resource "scaleway_edge_services_cache_stage" "site" {
          pipeline_id    = scaleway_edge_services_pipeline.site.id
          route_stage_id = scaleway_edge_services_route_stage.site.id
        }

        resource "scaleway_edge_services_tls_stage" "site" {
          pipeline_id         = scaleway_edge_services_pipeline.site.id
          cache_stage_id      = scaleway_edge_services_cache_stage.site.id
          managed_certificate = var.domain_name != ""
        }

        resource "scaleway_edge_services_dns_stage" "site" {
          count        = var.domain_name == "" ? 0 : 1
          pipeline_id  = scaleway_edge_services_pipeline.site.id
          tls_stage_id = scaleway_edge_services_tls_stage.site.id
          fqdns        = [var.domain_name]
        }
    """)))
    f.add("terraform/outputs.tf", tf(T("""
        output "bucket_name" {
          description = "Upload the built site here, for example with the AWS CLI pointed at the Scaleway endpoint."
          value       = scaleway_object_bucket.site.name
        }

        output "website_endpoint" {
          description = "Address of the bucket's website."
          value       = scaleway_object_bucket_website_configuration.site.website_endpoint
        }
    """)))
    return finish(model, f, "Scaleway", [
        "Create an API key (Credentials, API keys) and export `SCW_ACCESS_KEY`, `SCW_SECRET_KEY` and `SCW_DEFAULT_PROJECT_ID`.",
        "Create an Object Storage bucket for Terraform state, copy `backend.hcl.example` to `backend.hcl` and fill it in.",
        "`terraform init -backend-config=backend.hcl`, `terraform plan`, `terraform apply`.",
        "Build the site and upload the files to the bucket named in the outputs.",
        "To use your own domain set `domain_name`, apply again and add the DNS record Edge Services shows."],
        ["The bucket's objects are public-read on purpose: website hosting serves the files without a sign-in.",
         "Edge Services settings and prices change; check the pipeline in the console after the first apply."], region)


GENERATORS = {"alibaba": alibaba, "huawei": huawei, "tencent": tencent, "oci": oci, "ibm": ibm, "scaleway": scaleway}


# --------------------------------------------------------------------------- DigitalOcean
def digitalocean(model):
    import iac_digitalocean
    f, full = base(model, iac_digitalocean.DigitalOcean)
    region = full.files["terraform/terraform.tfvars.example"].split('region       = "')[1].split('"')[0]
    statics = [u for u in model.units if u["kind"] == "static"]
    f.add("terraform/variables.tf", tf(T("""
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
          description = "DigitalOcean region, for example nyc3, ams3, fra1. App Platform uses the first three letters (nyc, ams, fra)."
          type        = string
          default     = "@@region@@"
        }

        variable "github_repo" {
          description = "The GitHub repository App Platform builds the site from, as owner/name. Connect your GitHub account to DigitalOcean first."
          type        = string
          default     = "REPLACE-with-owner/repository"
        }

        variable "github_branch" {
          description = "The branch to deploy."
          type        = string
          default     = "main"
        }

        variable "build_command" {
          description = "Command that builds the site. Check your package.json scripts."
          type        = string
          default     = "npm run build"
        }

        variable "output_dir" {
          description = "Folder the build writes the finished site to, for example dist (Vite), build (Create React App) or out."
          type        = string
          default     = "dist"
        }
    """, region=region)))
    sites = []
    for u in statics:
        folder = u["folder"].strip("./")
        sites.append(T("""
            static_site {
              name              = "@@name@@"
              build_command     = var.build_command
              output_dir        = var.output_dir
              source_dir        = "@@dir@@"
              catchall_document = "index.html"

              github {
                repo           = var.github_repo
                branch         = var.github_branch
                deploy_on_push = true
              }

              routes {
                path = "/"
              }
            }
        """, name=u["name"], dir=hcl_str(folder or "/")).rstrip(chr(10)))
    body = chr(10).join("    " + line if line else line for s in sites for line in (s + chr(10)).split(chr(10)))
    f.add("terraform/site.tf", tf(T("""
        locals {
          name = "${var.project_name}-${var.environment}"
        }

        resource "digitalocean_project" "main" {
          name        = local.name
          description = "Static site for ${var.project_name}"
          purpose     = "Web Application"
          environment = "Production"
        }

        resource "digitalocean_app" "site" {
          spec {
            name   = local.name
            region = substr(var.region, 0, 3)

            alert {
              rule = "DEPLOYMENT_FAILED"
            }

            alert {
              rule = "DOMAIN_FAILED"
            }

        @@sites@@
          }
        }

        resource "digitalocean_project_resources" "main" {
          project   = digitalocean_project.main.id
          resources = [digitalocean_app.site.urn]
        }
    """, sites=body.rstrip(chr(10)))))
    f.add("terraform/outputs.tf", tf(T("""
        output "site_url" {
          description = "The address App Platform gives the site. Add your own domain in the app's settings."
          value       = digitalocean_app.site.live_url
        }
    """)))
    return finish(model, f, "DigitalOcean", [
        "Create a personal access token with read and write scope and export `DIGITALOCEAN_TOKEN`. Create a Spaces bucket and keys for Terraform state, copy `backend.hcl.example` to `backend.hcl` and fill it in.",
        "Connect your GitHub account to DigitalOcean (Apps, Create App, GitHub) so it can read the repository. Put the repository in `github_repo` and check `build_command` and `output_dir`.",
        "`terraform init -backend-config=backend.hcl`, `terraform plan`, `terraform apply`. App Platform builds the site and deploys it on every push.",
        "Open `site_url` from the outputs. Add your own domain under the app's Settings, Domains."],
        ["App Platform static sites are built from a Git repository, so there is no bucket to upload to.",
         "Unknown paths answer index.html so client-side routes work."], region)


GENERATORS["digitalocean"] = digitalocean
