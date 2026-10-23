#!/usr/bin/env python3
"""Terraform for AWS functions that need no private network.

This is the design for a serverless plan whose data lives outside the account (MongoDB Atlas, Neon, a hosted Redis) or that has
no data store at all. Putting such functions in a VPC would force a NAT gateway (a fixed hourly charge) only so they can reach the
internet, so they are not placed in one. What it writes:

  - one container-image function per unit, with the AWS Lambda Web Adapter so an ordinary web server runs unchanged
  - a Function URL per public unit (response streaming for units that stream), or an HTTP API when the plan chose API Gateway
  - settings and secrets: one sensitive map `app_env`, stored in Parameter Store (free) or Secrets Manager, and passed to the
    function so unchanged code keeps reading its environment variables
  - an EventBridge schedule for work that used to run in an in-process scheduler
  - log groups with short retention, an image lifecycle policy that keeps few images, alarms to an SNS topic

Everything here is free tier eligible; the README says what is not (outbound data beyond the allowance, X-Ray beyond its allowance).
"""
import re

from iac_common import Files, T, fmt_hcl, ident, tf

API_URL_NAME = re.compile(r"(API|BACKEND|SERVER).*(URL|URI|HOST|ENDPOINT)|(URL|URI|HOST|ENDPOINT).*(API|BACKEND)", re.I)


def function_url_resources(public_units):
    """A Function URL per public unit (response streaming for the units that stream), with the two permissions it needs."""
    parts = []
    for u in public_units:
        n = ident(u["name"])
        mode = "RESPONSE_STREAM" if u.get("streaming") else "BUFFERED"
        parts.append(T("""
            resource "aws_lambda_function_url" "@@n@@" {
              function_name      = aws_lambda_function.@@n@@.function_name
              authorization_type = "NONE"
              invoke_mode        = "@@mode@@"

              # Browsers may call this address only from the origins listed in cors_allowed_origins. Empty (the default) allows none.
              dynamic "cors" {
                for_each = length(var.cors_allowed_origins) == 0 ? [] : [1]

                content {
                  allow_origins = var.cors_allowed_origins
                  allow_methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
                  allow_headers = ["content-type", "authorization"]
                  max_age       = 86400
                }
              }
            }

            resource "aws_lambda_permission" "url_@@n@@" {
              statement_id           = "AllowPublicFunctionUrl"
              action                 = "lambda:InvokeFunctionUrl"
              function_name          = aws_lambda_function.@@n@@.function_name
              principal              = "*"
              function_url_auth_type = "NONE"
            }

            resource "aws_lambda_permission" "url_invoke_@@n@@" {
              statement_id             = "AllowInvokeViaFunctionUrl"
              action                   = "lambda:InvokeFunction"
              function_name            = aws_lambda_function.@@n@@.function_name
              principal                = "*"
              invoked_via_function_url = true
            }
        """, n=n, mode=mode))
    return parts


def scheduler_resources(units):
    """An EventBridge Scheduler role and one schedule per unit whose code starts a scheduler inside the app."""
    parts = [T("""
        data "aws_iam_policy_document" "scheduler_assume" {
          statement {
            actions = ["sts:AssumeRole"]

            principals {
              type        = "Service"
              identifiers = ["scheduler.amazonaws.com"]
            }
          }
        }

        resource "aws_iam_role" "scheduler" {
          name               = "${local.name}-scheduler"
          assume_role_policy = data.aws_iam_policy_document.scheduler_assume.json
        }
    """)]
    for u in units:
        if not u.get("scheduler"):
            continue
        n = ident(u["name"])
        parts.append(T("""
            data "aws_iam_policy_document" "scheduler_invoke_@@n@@" {
              statement {
                actions   = ["lambda:InvokeFunction"]
                resources = [aws_lambda_function.@@n@@.arn]
              }
            }

            resource "aws_iam_role_policy" "scheduler_@@n@@" {
              name   = "invoke-@@name@@"
              role   = aws_iam_role.scheduler.id
              policy = data.aws_iam_policy_document.scheduler_invoke_@@n@@.json
            }

            resource "aws_scheduler_schedule" "@@n@@" {
              name                = "${local.name}-@@name@@"
              description         = "Calls POST /internal/scheduled on @@name@@. Move the work your in-process scheduler did into that route."
              schedule_expression = var.schedule_expression

              flexible_time_window {
                mode = "OFF"
              }

              target {
                arn      = aws_lambda_function.@@n@@.arn
                role_arn = aws_iam_role.scheduler.arn
                input    = jsonencode({ source = "solsarch.scheduler", job = "scheduled" })
              }
            }
        """, n=n, name=u["name"]))
    return parts


SCHEDULE_VARIABLES = """
variable "schedule_expression" {
  type        = string
  description = "How often the scheduled job runs, for example rate(15 minutes) or cron(0 6 * * ? *)."
  default     = "rate(15 minutes)"
}
"""
CORS_VARIABLE = """
variable "cors_allowed_origins" {
  type        = list(string)
  description = "Web addresses allowed to call the function URLs from a browser, for example https://app.example.com. Empty means no browser on another site may call them."
  default     = []
}
"""


class AwsLambda:
    def __init__(self, model, region, bootstrap_text):
        self.m = model
        self.files = Files(model.fingerprint)
        self.region = region
        self.bootstrap_text = bootstrap_text
        self.units = [u for u in model.units if u["kind"] != "static"]
        self.public = [u for u in model.public_units() if u["kind"] != "static"]
        ingress = (model.by_key.get("ingress") or {}).get("service", "")
        self.function_url = "Function URL" in ingress
        self.parameter_store = "Parameter Store" in (model.by_key.get("secrets") or {}).get("service", "Parameter Store")
        self.retention = 7 if model.lean else (365 if model.regulated else 30)
        self.keep_images = 2 if model.lean else 10
        inputs = {i["name"]: i for i in model.config_inputs}
        external_mongo = any(d["key"] == "database_mongo" for d in model.external_decisions)
        external_pg = any(d["key"] == "database" for d in model.external_decisions)
        self.required = sorted(n for n, i in inputs.items() if i["required"] or (external_mongo and "MONGO" in n and i["category"] == "connection")
                               or (external_pg and ("DATABASE_URL" in n or "POSTGRES" in n)))
        self.plain = sorted(n for n, i in inputs.items() if i["category"] == "config")
        self.api_unit = next((u for u in self.public if u["kind"] in ("api", "service")), self.public[0] if self.public else None)

    # ------------------------------------------------------------------ top level
    def generate(self):
        m = self.m
        self.versions()
        self.variables()
        self.core()
        self.compute()
        if self.public:
            self.ingress()
        if m.scheduled:
            self.scheduler()
        self.settings()
        self.monitoring()
        self.outputs()
        self.files.add("terraform/backend.tf", T("""
            terraform {
              backend "s3" {}
            }
        """))
        self.files.add("terraform/backend.hcl.example", T("""
            bucket       = "REPLACE-with-the-state-bucket-from-bootstrap"
            key          = "@@project@@/production/terraform.tfstate"
            region       = "@@region@@"
            encrypt      = true
            use_lockfile = true
        """, project=m.project, region=self.region))
        self.files.add("terraform/bootstrap/main.tf", self.bootstrap_text)
        self.files.add("terraform/terraform.tfvars.example", self.tfvars())
        self.files.add("terraform/README.md", self.readme())
        return self.files

    def versions(self):
        self.files.add("terraform/versions.tf", tf(T("""
            terraform {
              required_version = ">= 1.9.0"

              required_providers {
                aws = {
                  source  = "hashicorp/aws"
                  version = "~> 6.0"
                }
              }
            }

            provider "aws" {
              region = var.region

              default_tags {
                tags = {
                  Project     = var.project_name
                  Environment = var.environment
                  ManagedBy   = "terraform"
                  GeneratedBy = "solsarch"
                }
              }
            }
        """)))

    def variables(self):
        m = self.m
        required = ", ".join('"%s"' % n for n in self.required)
        validation = ""
        if self.required:
            validation = chr(10) * 2 + T("""
                  validation {
                    condition     = alltrue([for k in [@@required@@] : contains(keys(var.app_env), k)])
                    error_message = "app_env must contain: @@names@@."
                  }
            """, required=required, names=", ".join(self.required)).rstrip("\n")
        out = [T("""
            variable "project_name" {
              type        = string
              description = "Short name used in resource names and tags."
              default     = "@@project@@"
            }

            variable "environment" {
              type        = string
              description = "Deployment environment, for example production."
              default     = "production"
            }

            variable "region" {
              type        = string
              description = "AWS region."
              default     = "@@region@@"
            }

            variable "image_tag" {
              type        = string
              description = "Tag of the container images in the repositories created here."
              default     = "0.1.0"
            }

            variable "log_retention_days" {
              type        = number
              description = "Days to keep function logs. Short on purpose: the free allowance is 5 GB of logs a month."
              default     = @@retention@@
            }

            variable "alarm_email" {
              type        = string
              description = "Email address that receives alarms. Leave null to create the topic without a subscriber."
              default     = null
            }

            variable "cors_allowed_origins" {
              type        = list(string)
              description = "Web addresses allowed to call the function URLs from a browser, for example https://app.example.com. Empty means no browser on another site may call them."
              default     = []
            }

            variable "app_env" {
              type        = map(string)
              sensitive   = true
              description = "Settings and secrets the application reads from its environment (API keys, connection strings)."
              default     = {}@@validation@@
            }
        """, project=m.project, region=self.region, retention=self.retention, validation=validation)]
        if m.scheduled:
            out.append(T("""
                variable "schedule_expression" {
                  type        = string
                  description = "How often the scheduled job runs, for example rate(15 minutes) or cron(0 6 * * ? *)."
                  default     = "rate(15 minutes)"
                }
            """))
        for u in self.units:
            n = ident(u["name"])
            out.append(T("""
                variable "@@n@@_image" {
                  type        = string
                  description = "Container image for @@name@@. Leave null to use the repository created for it with the image_tag."
                  default     = null
                }

                variable "@@n@@_memory_mb" {
                  type        = number
                  description = "Memory of @@name@@ in MB. More memory also means more CPU, and costs more per second."
                  default     = @@mem@@
                }

                variable "@@n@@_timeout_seconds" {
                  type        = number
                  description = "Longest one request or job of @@name@@ may run (up to 900)."
                  default     = @@timeout@@
                }
            """, n=n, name=u["name"], mem=max(512, int(m.sizing["unit_memory_gb"] * 1024)), timeout=300 if u.get("streaming") or u.get("scheduler") else 30))
        self.files.add("terraform/variables.tf", tf(*out))

    def core(self):
        parts = [T("""
            data "aws_caller_identity" "current" {}

            locals {
              name = "${var.project_name}-${var.environment}"
            }
        """)]
        for u in self.units:
            n = ident(u["name"])
            parts.append(T("""
                resource "aws_ecr_repository" "@@n@@" {
                  name                 = "${local.name}/@@name@@"
                  image_tag_mutability = "IMMUTABLE"
                  force_delete         = false

                  image_scanning_configuration {
                    scan_on_push = true
                  }

                  encryption_configuration {
                    encryption_type = "AES256"
                  }
                }

                resource "aws_ecr_lifecycle_policy" "@@n@@" {
                  repository = aws_ecr_repository.@@n@@.name

                  policy = jsonencode({
                    rules = [{
                      rulePriority = 1
                      description  = "Keep the newest @@keep@@ images so storage stays inside the free allowance"
                      selection = {
                        tagStatus   = "any"
                        countType   = "imageCountMoreThan"
                        countNumber = @@keep@@
                      }
                      action = {
                        type = "expire"
                      }
                    }]
                  })
                }
            """, n=n, name=u["name"], keep=self.keep_images))
        parts.append(T("""
            data "aws_iam_policy_document" "lambda_assume" {
              statement {
                actions = ["sts:AssumeRole"]

                principals {
                  type        = "Service"
                  identifiers = ["lambda.amazonaws.com"]
                }
              }
            }

            resource "aws_iam_role" "lambda" {
              name               = "${local.name}-lambda"
              assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
            }

            resource "aws_iam_role_policy_attachment" "lambda_logs" {
              role       = aws_iam_role.lambda.name
              policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
            }
        """))
        if self.parameter_store:
            parts.append(T("""
                data "aws_iam_policy_document" "lambda_parameters" {
                  statement {
                    sid       = "ReadApplicationParameters"
                    actions   = ["ssm:GetParameter", "ssm:GetParameters", "ssm:GetParametersByPath"]
                    resources = ["arn:aws:ssm:${var.region}:${data.aws_caller_identity.current.account_id}:parameter/${local.name}/*"]
                  }
                }

                resource "aws_iam_role_policy" "lambda_parameters" {
                  name   = "read-application-parameters"
                  role   = aws_iam_role.lambda.id
                  policy = data.aws_iam_policy_document.lambda_parameters.json
                }
            """))
        else:
            parts.append(T("""
                data "aws_iam_policy_document" "lambda_secrets" {
                  statement {
                    sid       = "ReadApplicationSecret"
                    actions   = ["secretsmanager:GetSecretValue"]
                    resources = [aws_secretsmanager_secret.app.arn]
                  }
                }

                resource "aws_iam_role_policy" "lambda_secrets" {
                  name   = "read-application-secret"
                  role   = aws_iam_role.lambda.id
                  policy = data.aws_iam_policy_document.lambda_secrets.json
                }
            """))
        if self.m.has("storage"):
            parts.append(T("""
                resource "aws_s3_bucket" "app" {
                  bucket_prefix = "${local.name}-app-"
                  force_destroy = false
                }

                resource "aws_s3_bucket_public_access_block" "app" {
                  bucket                  = aws_s3_bucket.app.id
                  block_public_acls       = true
                  block_public_policy     = true
                  ignore_public_acls      = true
                  restrict_public_buckets = true
                }

                resource "aws_s3_bucket_versioning" "app" {
                  bucket = aws_s3_bucket.app.id

                  versioning_configuration {
                    status = "Enabled"
                  }
                }

                resource "aws_s3_bucket_server_side_encryption_configuration" "app" {
                  bucket = aws_s3_bucket.app.id

                  rule {
                    apply_server_side_encryption_by_default {
                      sse_algorithm = "AES256"
                    }
                  }
                }

                data "aws_iam_policy_document" "lambda_bucket" {
                  statement {
                    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"]
                    resources = [aws_s3_bucket.app.arn, "${aws_s3_bucket.app.arn}/*"]
                  }
                }

                resource "aws_iam_role_policy" "lambda_bucket" {
                  name   = "use-the-application-bucket"
                  role   = aws_iam_role.lambda.id
                  policy = data.aws_iam_policy_document.lambda_bucket.json
                }
            """))
        self.files.add("terraform/main.tf", tf(*parts))

    # ------------------------------------------------------------------ functions
    def environment(self, unit):
        pairs = [("APP_ENV", "var.environment"), ("AWS_LWA_PORT", '"%d"' % (unit["port"] or 8080)),
                 ("AWS_LWA_READINESS_CHECK_PATH", '"%s"' % (unit.get("health_path") or "/"))]
        if unit.get("streaming"):
            pairs.append(("AWS_LWA_INVOKE_MODE", '"response_stream"'))
        if unit.get("scheduler"):
            pairs.append(("AWS_LWA_PASS_THROUGH_PATH", '"/internal/scheduled"'))
        if self.m.has("storage"):
            pairs.append(("S3_BUCKET", "aws_s3_bucket.app.id"))
        if self.function_url and unit["kind"] == "web" and self.api_unit is not None and self.api_unit is not unit:
            for name in unit.get("env_vars", []):
                if API_URL_NAME.search(name):
                    pairs.append((name, "aws_lambda_function_url.%s.function_url" % ident(self.api_unit["name"])))
        return pairs

    def compute(self):
        parts = []
        for u in self.units:
            n, name = ident(u["name"]), u["name"]
            fixed = "\n".join("      %s = %s" % (k, v) for k, v in self.environment(u))
            parts.append(T("""
                resource "aws_cloudwatch_log_group" "@@n@@" {
                  name              = "/aws/lambda/${local.name}-@@name@@"
                  retention_in_days = var.log_retention_days
                }

                resource "aws_lambda_function" "@@n@@" {
                  function_name = "${local.name}-@@name@@"
                  role          = aws_iam_role.lambda.arn
                  package_type  = "Image"
                  image_uri     = coalesce(var.@@n@@_image, "${aws_ecr_repository.@@n@@.repository_url}:${var.image_tag}")
                  timeout       = var.@@n@@_timeout_seconds
                  memory_size   = var.@@n@@_memory_mb

                  tracing_config {
                    mode = "@@trace@@"
                  }

                  environment {
                    variables = merge(
                      {
@@fixed@@
                      },
                      var.app_env
                    )
                  }

                  depends_on = [aws_cloudwatch_log_group.@@n@@, aws_iam_role_policy_attachment.lambda_logs]
                }
            """, n=n, name=name, fixed=fixed, trace="PassThrough" if self.m.lean else "Active"))
        self.files.add("terraform/compute.tf", tf(*parts))

    def ingress(self):
        parts = []
        if self.function_url:
            parts = function_url_resources(self.public)
        else:
            parts.append(T("""
                resource "aws_apigatewayv2_api" "main" {
                  name          = local.name
                  protocol_type = "HTTP"
                }

                resource "aws_cloudwatch_log_group" "apigw" {
                  name              = "/apigateway/${local.name}"
                  retention_in_days = var.log_retention_days
                }

                resource "aws_apigatewayv2_stage" "default" {
                  api_id      = aws_apigatewayv2_api.main.id
                  name        = "$default"
                  auto_deploy = true

                  access_log_settings {
                    destination_arn = aws_cloudwatch_log_group.apigw.arn
                    format = jsonencode({
                      requestId = "$context.requestId"
                      method    = "$context.httpMethod"
                      path      = "$context.path"
                      status    = "$context.status"
                      latency   = "$context.responseLatency"
                    })
                  }
                }
            """))
            first = self.public[0]
            for u in self.public:
                n = ident(u["name"])
                route = "$default" if u is first else "ANY /%s/{proxy+}" % u["name"]
                parts.append(T("""
                    resource "aws_apigatewayv2_integration" "@@n@@" {
                      api_id                 = aws_apigatewayv2_api.main.id
                      integration_type       = "AWS_PROXY"
                      integration_uri        = aws_lambda_function.@@n@@.invoke_arn
                      payload_format_version = "2.0"
                    }

                    resource "aws_apigatewayv2_route" "@@n@@" {
                      api_id    = aws_apigatewayv2_api.main.id
                      route_key = "@@route@@"
                      target    = "integrations/${aws_apigatewayv2_integration.@@n@@.id}"
                    }

                    resource "aws_lambda_permission" "apigw_@@n@@" {
                      statement_id  = "AllowApiGateway"
                      action        = "lambda:InvokeFunction"
                      function_name = aws_lambda_function.@@n@@.function_name
                      principal     = "apigateway.amazonaws.com"
                      source_arn    = "${aws_apigatewayv2_api.main.execution_arn}/*/*"
                    }
                """, n=n, route=route))
        self.files.add("terraform/ingress.tf", tf(*parts))

    def scheduler(self):
        parts = scheduler_resources(self.units)
        self.files.add("terraform/schedule.tf", tf(*parts))

    def settings(self):
        if self.parameter_store:
            plain = ", ".join('"%s"' % n for n in self.plain)
            self.files.add("terraform/settings.tf", tf(T("""
                locals {
                  plain_setting_names = [@@plain@@]
                }

                # Every entry of app_env is also kept in Parameter Store (Standard tier, no charge), so other tools and the
                # pipeline can read them, and code that prefers to fetch its settings can do so.
                resource "aws_ssm_parameter" "setting" {
                  for_each = nonsensitive(toset(keys(var.app_env)))

                  name  = "/${local.name}/env/${each.key}"
                  type  = "SecureString"
                  tier  = "Standard"
                  value = var.app_env[each.key]
                }
            """, plain=plain)))
        else:
            self.files.add("terraform/settings.tf", tf(T("""
                resource "aws_secretsmanager_secret" "app" {
                  name_prefix = "${local.name}-app-"
                  description = "Settings and secrets for ${local.name}."
                }

                resource "aws_secretsmanager_secret_version" "app" {
                  secret_id     = aws_secretsmanager_secret.app.id
                  secret_string = jsonencode(var.app_env)
                }
            """)))

    def monitoring(self):
        parts = [T("""
            resource "aws_sns_topic" "alarms" {
              name = "${local.name}-alarms"
            }

            resource "aws_sns_topic_subscription" "email" {
              count = var.alarm_email == null ? 0 : 1

              topic_arn = aws_sns_topic.alarms.arn
              protocol  = "email"
              endpoint  = var.alarm_email
            }
        """)]
        for u in self.units:
            n, name = ident(u["name"]), u["name"]
            parts.append(T("""
                resource "aws_cloudwatch_metric_alarm" "@@n@@_errors" {
                  alarm_name          = "${local.name}-@@name@@-errors"
                  namespace           = "AWS/Lambda"
                  metric_name         = "Errors"
                  statistic           = "Sum"
                  comparison_operator = "GreaterThanThreshold"
                  threshold           = 5
                  period              = 300
                  evaluation_periods  = 2
                  treat_missing_data  = "notBreaching"
                  alarm_actions       = [aws_sns_topic.alarms.arn]

                  dimensions = {
                    FunctionName = aws_lambda_function.@@n@@.function_name
                  }
                }

                resource "aws_cloudwatch_metric_alarm" "@@n@@_throttles" {
                  alarm_name          = "${local.name}-@@name@@-throttles"
                  namespace           = "AWS/Lambda"
                  metric_name         = "Throttles"
                  statistic           = "Sum"
                  comparison_operator = "GreaterThanThreshold"
                  threshold           = 0
                  period              = 300
                  evaluation_periods  = 1
                  treat_missing_data  = "notBreaching"
                  alarm_actions       = [aws_sns_topic.alarms.arn]

                  dimensions = {
                    FunctionName = aws_lambda_function.@@n@@.function_name
                  }
                }
            """, n=n, name=name))
        self.files.add("terraform/monitoring.tf", tf(*parts))

    def outputs(self):
        parts = []
        for u in self.public:
            n = ident(u["name"])
            if self.function_url:
                parts.append(T("""
                    output "@@n@@_url" {
                      description = "Public address of @@name@@."
                      value       = aws_lambda_function_url.@@n@@.function_url
                    }
                """, n=n, name=u["name"]))
        if not self.function_url and self.public:
            parts.append(T("""
                output "api_endpoint" {
                  description = "Address of the HTTP API."
                  value       = aws_apigatewayv2_api.main.api_endpoint
                }
            """))
        for u in self.units:
            parts.append(T("""
                output "@@n@@_repository_url" {
                  description = "Push the @@name@@ image here."
                  value       = aws_ecr_repository.@@n@@.repository_url
                }
            """, n=ident(u["name"]), name=u["name"]))
        parts.append(T("""
            output "settings_prefix" {
              description = "Where the application settings are stored."
              value       = "@@where@@"
            }
        """, where="/${local.name}/env/" if self.parameter_store else "Secrets Manager secret named in settings.tf"))
        self.files.add("terraform/outputs.tf", tf(*parts))

    def tfvars(self):
        return fmt_hcl('project_name = "%s"\nenvironment  = "production"\nregion       = "%s"\n\n# Settings and secrets the application reads. Fill these in; do not commit real values.\n'
                       '# app_env = {\n%s# }\n' % (self.m.project, self.region, "".join('#   %s = "CHANGE_ME"\n' % n for n in sorted(set(self.required) | {i["name"] for i in self.m.config_inputs if i["category"] != "config"}))))

    def readme(self):
        m = self.m
        ext = ", ".join(d["short"] for d in m.external_decisions) or "none"
        return T("""
            # Terraform for @@project@@ on AWS (functions, no private network)

            Plan fingerprint @@fp@@. Units: @@units@@. Data kept outside this account: @@ext@@.

            This design puts **no function in a VPC**, so no NAT gateway, Elastic IP or load balancer is created. Those cost money every hour
            whether or not anyone uses the app. Functions reach the internet and the outside data services directly.

            ## Order of work

            1. `cd bootstrap && terraform init && terraform apply` once. It creates the encrypted, versioned bucket that keeps Terraform state.
            2. Copy `backend.hcl.example` to `backend.hcl` and put the bucket name in it.
            3. Create the outside data services first (for example `../terraform-mongodbatlas`) and note their connection strings.
            4. Copy `terraform.tfvars.example` to `terraform.tfvars` and fill in `app_env` (API keys, connection strings). Never commit it.
            5. `terraform init -backend-config=backend.hcl`, then create only the image repositories first (a function cannot be created before its image exists):
               `terraform apply @@targets@@`
            6. Build and push each image to its repository (see the outputs; the Dockerfiles in `../docker` already include the AWS Lambda Web Adapter).
            7. `terraform apply` once more. It creates the functions, addresses, schedule and alarms.

            ## What is configured for you

            - One container-image function per unit. The **AWS Lambda Web Adapter** lets an ordinary web server (uvicorn, Node) run unchanged.
            - @@ingress@@
            - Settings and secrets: the `app_env` map is kept in @@store@@ and also passed to the functions as environment variables, so unchanged code keeps working. The values are in the Terraform state; keep the state bucket private.
            - @@sched@@
            - Logs kept @@ret@@ days, images limited to the newest @@keep@@, alarms for errors and throttling to an SNS topic (set `alarm_email`).

            ## Free tier notes

            Lambda (1 million requests and 400,000 GB-seconds a month), Function URLs, EventBridge Scheduler (14 million invocations), Parameter Store
            standard parameters, 5 GB of logs and 500 MB of image storage have monthly allowances that do not expire. Outbound data transfer beyond the allowance,
            and calls to outside services (database, AI model API), are billed by whoever provides them.

            ## What you must still decide

            - Whether the in-process pieces fit a function: a scheduler (@@schedule_note@@), streaming time limits (a function can stream for up to 15 minutes), and anything that starts child processes.
            - A domain name (put a CloudFront distribution in front when you want one).
            - `cors_allowed_origins`: the web addresses of your front end, if a browser on another site calls the function URLs. It starts empty, which allows none.
        """, project=m.project, fp=m.fingerprint, units=", ".join(u["name"] for u in self.units), ext=ext,
                 ingress=("A **Function URL** per public unit; units that stream use response streaming, which has no 30-second cut-off."
                          if self.function_url else "An HTTP API (API Gateway) in front of the functions."),
                 store="Parameter Store (Standard tier, free)" if self.parameter_store else "Secrets Manager",
                 sched=("An **EventBridge schedule** calls `POST /internal/scheduled` on the unit that had the scheduler (`schedule_expression`). Move the work the in-process scheduler did into that route and stop starting the scheduler."
                        if m.scheduled else "No scheduler was found in the code."),
                 targets=" ".join("-target=aws_ecr_repository.%s" % ident(u["name"]) for u in self.units), ret=self.retention, keep=self.keep_images, schedule_note="detected" if m.scheduled else "none found")


def generate(model, region, bootstrap_text):
    return AwsLambda(model, region, bootstrap_text).generate()
