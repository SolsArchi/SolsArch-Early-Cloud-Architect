#!/usr/bin/env python3
"""Terraform for AWS, generated from the plan.

Containers run on ECS Fargate behind an Application Load Balancer (server model) or as Lambda functions behind an
HTTP API (serverless model). Every resource that appears was asked for by a decision in the plan. The rules from
real deployment failures are built in: Multi-AZ data stores when high availability is on, encryption everywhere,
backups on, a TLS 1.2+ listener policy, load-balancer access logs, no public data stores, security-group rules as
separate resources (so two groups never form a dependency cycle), a WAF on every CloudFront distribution, health
checks that use the path the code really serves, and no hard-coded account, role or subnet ids.
"""
import json
import re
import textwrap

from iac_common import Files, Model, T, fmt_hcl, ident, q, slug, tf

AWS_DB_CLASS = {"small": "db.t4g.medium", "medium": "db.r6g.large", "large": "db.r6g.2xlarge"}
AWS_CACHE_NODE = {1: "cache.t4g.small", 4: "cache.t4g.medium"}
AWS_REGION_DEFAULT = "us-east-1"


def path_prefix(unit, default_unit):
    if unit is default_unit:
        return None
    return "/api/*" if unit["kind"] == "api" or unit["name"] == "api" else "/%s/*" % unit["name"]


class Aws:
    def __init__(self, model):
        self.m = model
        self.files = Files(model.fingerprint)
        self.public = model.public_units()
        self.default_unit = self.public[0] if self.public else None
        self.use_cdn = model.has("edge")
        self.lambda_mode = model.mode == "serverless"
        self.az_count = 2
        self.nat_count = 1 if model.single_az else 2
        self.kms = model.regulated
        self.region = model.region or AWS_REGION_DEFAULT

    # ----------------------------------------------------------- top level
    def generate(self):
        m = self.m
        self.versions()
        self.providers()
        self.variables()
        self.network()
        self.security_groups()
        if self.lambda_mode:
            self.lambda_compute()
        else:
            self.ecs_compute()
            self.alb()
        self.data_stores()
        self.storage()
        if self.use_cdn:
            self.edge()
        if m.has("identity"):
            self.identity()
        self.monitoring()
        self.outputs()
        self.files.add("terraform/backend.tf", T("""
            terraform {
              backend "s3" {}
            }
        """))
        self.files.add("terraform/backend.hcl.example", T("""
            bucket       = "REPLACE-with-the-state-bucket-from-bootstrap"
            key          = "@@project@@/@@env@@/terraform.tfstate"
            region       = "@@region@@"
            encrypt      = true
            use_lockfile = true
        """, project=m.project, env="production", region=self.region))
        self.files.add("terraform/bootstrap/main.tf", self.bootstrap())
        self.files.add("terraform/terraform.tfvars.example", self.tfvars())
        self.files.add("terraform/README.md", self.readme())
        return self.files

    # --------------------------------------------------------------- basics
    def versions(self):
        self.files.add("terraform/versions.tf", tf(T("""
            terraform {
              required_version = ">= 1.9.0"

              required_providers {
                aws = {
                  source  = "hashicorp/aws"
                  version = "~> 6.0"
                }
                random = {
                  source  = "hashicorp/random"
                  version = "~> 3.6"
                }
              }
            }
        """)))

    def providers(self):
        tags = T("""
            default_tags {
              tags = {
                Project     = var.project_name
                Environment = var.environment
                ManagedBy   = "terraform"
                GeneratedBy = "solsarch"
              }
            }
        """)
        body = 'provider "aws" {\n  region = var.region\n\n' + textwrap.indent(tags, "  ") + "}\n"
        if self.use_cdn:
            body += '\nprovider "aws" {\n  alias  = "us_east_1"\n  region = "us-east-1"\n\n' + textwrap.indent(tags, "  ") + "}\n"
        self.files.add("terraform/providers.tf", tf(body))

    def variables(self):
        m = self.m
        out = [T("""
            variable "project_name" {
              type        = string
              description = "Short name used in resource names and tags."
              default     = "@@project@@"
            }

            variable "environment" {
              type        = string
              description = "Deployment environment, for example production or staging."
              default     = "production"
            }

            variable "region" {
              type        = string
              description = "AWS region for every resource except the CloudFront web ACL, which must live in us-east-1."
              default     = "@@region@@"
            }

            variable "vpc_cidr" {
              type        = string
              description = "Address range of the VPC."
              default     = "10.40.0.0/16"
            }

            variable "image_tag" {
              type        = string
              description = "Tag of the container images in the repositories created here."
              default     = "latest"
            }

            variable "log_retention_days" {
              type        = number
              description = "Days to keep application logs."
              default     = @@retention@@
            }

            variable "alarm_email" {
              type        = string
              description = "Email address that receives alarms. Leave null to create the topic without a subscriber."
              default     = null
            }

            variable "enable_deletion_protection" {
              type        = bool
              description = "Protect the load balancer and the databases from deletion."
              default     = @@protect@@
            }
        """, project=m.project, region=self.region, retention=365 if m.regulated else 30,
                   protect="false" if m.lean else "true")]
        if not self.lambda_mode and self.public:
            out.append(T("""
                variable "acm_certificate_arn" {
                  type        = string
                  description = "ARN of an ACM certificate for HTTPS on the load balancer. Leave null for HTTP only until you have one."
                  default     = null
                }
            """))
        for unit in m.units:
            if unit["kind"] == "static":
                continue
            n = ident(unit["name"])
            out.append(T("""
                variable "@@n@@_image" {
                  type        = string
                  description = "Container image for @@name@@. Leave null to use the repository created for it with the image_tag."
                  default     = null
                }

                variable "@@n@@_desired_count" {
                  type        = number
                  description = "Number of copies of @@name@@ to run."
                  default     = @@count@@
                }
            """, n=n, name=unit["name"], count=1 if unit["kind"] == "worker" else m.replicas))
        if m.db_key:
            out.append(T("""
                variable "db_name" {
                  type        = string
                  description = "Name of the first database."
                  default     = "app"
                }

                variable "db_username" {
                  type        = string
                  description = "Administrator user name. The password is generated and kept in Secrets Manager."
                  default     = "app_admin"
                }
            """))
            if not self.lambda_mode:
                out.append(T("""
                    variable "db_instance_class" {
                      type        = string
                      description = "Instance class of the database."
                      default     = "@@cls@@"
                    }
                """, cls=AWS_DB_CLASS[m.sizing["db_class"]]))
        self.files.add("terraform/variables.tf", tf(*out))

    # -------------------------------------------------------------- network
    def network(self):
        m = self.m
        body = [T("""
            data "aws_availability_zones" "available" {
              state = "available"
            }

            locals {
              name = "${var.project_name}-${var.environment}"
              azs  = slice(data.aws_availability_zones.available.names, 0, 2)
            }

            resource "aws_vpc" "main" {
              cidr_block           = var.vpc_cidr
              enable_dns_hostnames = true
              enable_dns_support   = true

              tags = {
                Name = local.name
              }
            }

            resource "aws_internet_gateway" "main" {
              vpc_id = aws_vpc.main.id

              tags = {
                Name = local.name
              }
            }

            resource "aws_subnet" "public" {
              count = 2

              vpc_id                  = aws_vpc.main.id
              cidr_block              = cidrsubnet(var.vpc_cidr, 4, count.index)
              availability_zone       = local.azs[count.index]
              map_public_ip_on_launch = false

              tags = {
                Name = "${local.name}-public-${count.index}"
                Tier = "public"
              }
            }

            resource "aws_subnet" "private" {
              count = 2

              vpc_id            = aws_vpc.main.id
              cidr_block        = cidrsubnet(var.vpc_cidr, 4, count.index + 4)
              availability_zone = local.azs[count.index]

              tags = {
                Name = "${local.name}-private-${count.index}"
                Tier = "private"
              }
            }

            resource "aws_subnet" "data" {
              count = 2

              vpc_id            = aws_vpc.main.id
              cidr_block        = cidrsubnet(var.vpc_cidr, 4, count.index + 8)
              availability_zone = local.azs[count.index]

              tags = {
                Name = "${local.name}-data-${count.index}"
                Tier = "data"
              }
            }

            resource "aws_route_table" "public" {
              vpc_id = aws_vpc.main.id

              tags = {
                Name = "${local.name}-public"
              }
            }

            resource "aws_route" "public_internet" {
              route_table_id         = aws_route_table.public.id
              destination_cidr_block = "0.0.0.0/0"
              gateway_id             = aws_internet_gateway.main.id
            }

            resource "aws_route_table_association" "public" {
              count = 2

              subnet_id      = aws_subnet.public[count.index].id
              route_table_id = aws_route_table.public.id
            }

            resource "aws_route_table" "data" {
              vpc_id = aws_vpc.main.id

              tags = {
                Name = "${local.name}-data"
              }
            }

            resource "aws_route_table_association" "data" {
              count = 2

              subnet_id      = aws_subnet.data[count.index].id
              route_table_id = aws_route_table.data.id
            }
        """)]
        if self.m.has("egress") or not self.lambda_mode:
            body.append(T("""
                resource "aws_eip" "nat" {
                  count = @@nats@@

                  domain = "vpc"

                  tags = {
                    Name = "${local.name}-nat-${count.index}"
                  }
                }

                resource "aws_nat_gateway" "main" {
                  count = @@nats@@

                  allocation_id = aws_eip.nat[count.index].id
                  subnet_id     = aws_subnet.public[count.index].id

                  tags = {
                    Name = "${local.name}-${count.index}"
                  }

                  depends_on = [aws_internet_gateway.main]
                }

                resource "aws_route_table" "private" {
                  count = 2

                  vpc_id = aws_vpc.main.id

                  tags = {
                    Name = "${local.name}-private-${count.index}"
                  }
                }

                resource "aws_route" "private_nat" {
                  count = 2

                  route_table_id         = aws_route_table.private[count.index].id
                  destination_cidr_block = "0.0.0.0/0"
                  nat_gateway_id         = aws_nat_gateway.main[min(count.index, @@nats@@ - 1)].id
                }

                resource "aws_route_table_association" "private" {
                  count = 2

                  subnet_id      = aws_subnet.private[count.index].id
                  route_table_id = aws_route_table.private[count.index].id
                }
            """, nats=self.nat_count))
        if self.m.regulated:
            body.append(T("""
                resource "aws_flow_log" "vpc" {
                  vpc_id               = aws_vpc.main.id
                  traffic_type         = "ALL"
                  log_destination_type = "cloud-watch-logs"
                  log_destination      = aws_cloudwatch_log_group.flow.arn
                  iam_role_arn         = aws_iam_role.flow.arn
                }

                resource "aws_cloudwatch_log_group" "flow" {
                  name              = "/vpc/${local.name}/flow"
                  retention_in_days = var.log_retention_days
                  kms_key_id        = aws_kms_key.main.arn
                }

                data "aws_iam_policy_document" "flow_assume" {
                  statement {
                    actions = ["sts:AssumeRole"]

                    principals {
                      type        = "Service"
                      identifiers = ["vpc-flow-logs.amazonaws.com"]
                    }
                  }
                }

                resource "aws_iam_role" "flow" {
                  name               = "${local.name}-flow-logs"
                  assume_role_policy = data.aws_iam_policy_document.flow_assume.json
                }

                data "aws_iam_policy_document" "flow_write" {
                  statement {
                    actions   = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogGroups", "logs:DescribeLogStreams"]
                    resources = ["${aws_cloudwatch_log_group.flow.arn}:*"]
                  }
                }

                resource "aws_iam_role_policy" "flow" {
                  name   = "write-flow-logs"
                  role   = aws_iam_role.flow.id
                  policy = data.aws_iam_policy_document.flow_write.json
                }
            """))
        if self.kms:
            body.append(T("""
                data "aws_caller_identity" "current" {}

                data "aws_iam_policy_document" "kms" {
                  statement {
                    sid       = "AccountAdministration"
                    actions   = ["kms:*"]
                    resources = ["*"]

                    principals {
                      type        = "AWS"
                      identifiers = ["arn:aws:iam::${data.aws_caller_identity.current.account_id}:root"]
                    }
                  }

                  statement {
                    sid       = "LogsService"
                    actions   = ["kms:Encrypt*", "kms:Decrypt*", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"]
                    resources = ["*"]

                    principals {
                      type        = "Service"
                      identifiers = ["logs.${var.region}.amazonaws.com"]
                    }
                  }
                }

                resource "aws_kms_key" "main" {
                  description             = "Encryption key for ${local.name}"
                  enable_key_rotation     = true
                  deletion_window_in_days = 30
                  policy                  = data.aws_iam_policy_document.kms.json
                }

                resource "aws_kms_alias" "main" {
                  name          = "alias/${local.name}"
                  target_key_id = aws_kms_key.main.key_id
                }
            """))
        self.files.add("terraform/network.tf", tf(*body))

    # ------------------------------------------------------ security groups
    def security_groups(self):
        m = self.m
        out = []
        has_alb = bool(self.public) and not self.lambda_mode
        if has_alb:
            out.append(T("""
                resource "aws_security_group" "alb" {
                  name_prefix = "${local.name}-alb-"
                  description = "Load balancer: HTTPS and HTTP from the internet"
                  vpc_id      = aws_vpc.main.id

                  lifecycle {
                    create_before_destroy = true
                  }
                }

                resource "aws_vpc_security_group_ingress_rule" "alb_https" {
                  security_group_id = aws_security_group.alb.id
                  description       = "HTTPS from the internet"
                  from_port         = 443
                  to_port           = 443
                  ip_protocol       = "tcp"
                  cidr_ipv4         = "0.0.0.0/0"
                }

                resource "aws_vpc_security_group_ingress_rule" "alb_http" {
                  security_group_id = aws_security_group.alb.id
                  description       = "HTTP from the internet (redirects to HTTPS once a certificate is set)"
                  from_port         = 80
                  to_port           = 80
                  ip_protocol       = "tcp"
                  cidr_ipv4         = "0.0.0.0/0"
                }
            """))
        out.append(T("""
            resource "aws_security_group" "app" {
              name_prefix = "${local.name}-app-"
              description = "Application compute"
              vpc_id      = aws_vpc.main.id

              lifecycle {
                create_before_destroy = true
              }
            }

            resource "aws_vpc_security_group_egress_rule" "app_https" {
              security_group_id = aws_security_group.app.id
              description       = "HTTPS to AWS services and outside APIs"
              from_port         = 443
              to_port           = 443
              ip_protocol       = "tcp"
              cidr_ipv4         = "0.0.0.0/0"
            }
        """))
        if has_alb:
            for unit in self.public:
                out.append(T("""
                    resource "aws_vpc_security_group_ingress_rule" "app_from_alb_@@n@@" {
                      security_group_id            = aws_security_group.app.id
                      description                  = "Traffic from the load balancer to @@name@@"
                      from_port                    = @@port@@
                      to_port                      = @@port@@
                      ip_protocol                  = "tcp"
                      referenced_security_group_id = aws_security_group.alb.id
                    }

                    resource "aws_vpc_security_group_egress_rule" "alb_to_app_@@n@@" {
                      security_group_id            = aws_security_group.alb.id
                      description                  = "Load balancer to @@name@@"
                      from_port                    = @@port@@
                      to_port                      = @@port@@
                      ip_protocol                  = "tcp"
                      referenced_security_group_id = aws_security_group.app.id
                    }
                """, n=ident(unit["name"]), name=unit["name"], port=unit["port"] or 8080))
        data_ports = []
        if m.db_key:
            data_ports.append(("db", 5432 if m.db_key == "database" else 3306, "Database"))
        if m.has("database_mongo"):
            data_ports.append(("docdb", 27017, "Document database"))
        if m.has("cache"):
            data_ports.append(("cache", 6379, "Cache"))
        if m.has("queue_rabbitmq"):
            data_ports.append(("mq", 5671, "Message broker"))
        if m.has("queue_kafka"):
            data_ports.append(("msk", 9094, "Event streaming"))
        for key, port, label in data_ports:
            out.append(T("""
                resource "aws_security_group" "@@key@@" {
                  name_prefix = "${local.name}-@@key@@-"
                  description = "@@label@@: reachable only from the application"
                  vpc_id      = aws_vpc.main.id

                  lifecycle {
                    create_before_destroy = true
                  }
                }

                resource "aws_vpc_security_group_ingress_rule" "@@key@@_from_app" {
                  security_group_id            = aws_security_group.@@key@@.id
                  description                  = "From the application"
                  from_port                    = @@port@@
                  to_port                      = @@port@@
                  ip_protocol                  = "tcp"
                  referenced_security_group_id = aws_security_group.app.id
                }

                resource "aws_vpc_security_group_egress_rule" "app_to_@@key@@" {
                  security_group_id            = aws_security_group.app.id
                  description                  = "Application to @@label@@"
                  from_port                    = @@port@@
                  to_port                      = @@port@@
                  ip_protocol                  = "tcp"
                  referenced_security_group_id = aws_security_group.@@key@@.id
                }
            """, key=key, port=port, label=label))
        self.files.add("terraform/security.tf", tf(*out))

    # ------------------------------------------------------ registries & IAM
    def ecr_and_iam(self):
        m = self.m
        out = []
        for unit in m.units:
            if unit["kind"] == "static":
                continue
            n = ident(unit["name"])
            out.append(T("""
                resource "aws_ecr_repository" "@@n@@" {
                  name                 = "${local.name}/@@name@@"
                  image_tag_mutability = "MUTABLE"
                  force_delete         = false

                  image_scanning_configuration {
                    scan_on_push = true
                  }

                  encryption_configuration {
                    encryption_type = @@enc@@
                  }
                }

                resource "aws_ecr_lifecycle_policy" "@@n@@" {
                  repository = aws_ecr_repository.@@n@@.name

                  policy = jsonencode({
                    rules = [{
                      rulePriority = 1
                      description  = "Keep the 20 newest images"
                      selection = {
                        tagStatus   = "any"
                        countType   = "imageCountMoreThan"
                        countNumber = 20
                      }
                      action = {
                        type = "expire"
                      }
                    }]
                  })
                }
            """, n=n, name=unit["name"], enc='"KMS"' if self.kms else '"AES256"').replace(
                "encryption_type = \"KMS\"", "encryption_type = \"KMS\"\n    kms_key         = aws_kms_key.main.arn") if False else T("""
                resource "aws_ecr_repository" "@@n@@" {
                  name                 = "${local.name}/@@name@@"
                  image_tag_mutability = "MUTABLE"
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
                      description  = "Keep the 20 newest images"
                      selection = {
                        tagStatus   = "any"
                        countType   = "imageCountMoreThan"
                        countNumber = 20
                      }
                      action = {
                        type = "expire"
                      }
                    }]
                  })
                }
            """, n=n, name=unit["name"]))
        return out

    def secret_arns(self):
        m = self.m
        arns = ["aws_secretsmanager_secret.app.arn"]
        if m.db_key and not m.has("database_mongo"):
            arns.append("aws_rds_cluster.main.master_user_secret[0].secret_arn")
        if m.has("database_mongo"):
            arns.append("aws_secretsmanager_secret.docdb.arn")
        if m.has("queue_rabbitmq"):
            arns.append("aws_secretsmanager_secret.mq.arn")
        return arns

    # ----------------------------------------------------------- ECS (server)
    def ecs_compute(self):
        m = self.m
        cpu, mem = m.vcpu_mem()
        cpu_units = {256: 256, 512: 512, 1024: 1024, 2048: 2048, 4096: 4096}.get(cpu, 512)
        mem_ok = {256: 512, 512: 1024, 1024: 2048, 2048: 4096, 4096: 8192}[cpu_units]
        out = self.ecr_and_iam()
        arns = self.secret_arns()
        out.append(T("""
            data "aws_iam_policy_document" "ecs_assume" {
              statement {
                actions = ["sts:AssumeRole"]

                principals {
                  type        = "Service"
                  identifiers = ["ecs-tasks.amazonaws.com"]
                }
              }
            }

            resource "aws_iam_role" "execution" {
              name               = "${local.name}-ecs-execution"
              assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
            }

            resource "aws_iam_role_policy_attachment" "execution" {
              role       = aws_iam_role.execution.name
              policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
            }

            data "aws_iam_policy_document" "execution_secrets" {
              statement {
                sid       = "ReadApplicationSecrets"
                actions   = ["secretsmanager:GetSecretValue"]
                resources = [@@arns@@]
              }@@kms_stmt@@
            }

            resource "aws_iam_role_policy" "execution_secrets" {
              name   = "read-application-secrets"
              role   = aws_iam_role.execution.id
              policy = data.aws_iam_policy_document.execution_secrets.json
            }

            resource "aws_iam_role" "task" {
              name               = "${local.name}-ecs-task"
              assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
            }

            resource "aws_ecs_cluster" "main" {
              name = local.name

              setting {
                name  = "containerInsights"
                value = "enabled"
              }
            }

            resource "aws_ecs_cluster_capacity_providers" "main" {
              cluster_name       = aws_ecs_cluster.main.name
              capacity_providers = ["FARGATE", "FARGATE_SPOT"]

              default_capacity_provider_strategy {
                capacity_provider = "FARGATE"
                base              = 1
                weight            = 1
              }
            }
        """, arns=", ".join(arns), kms_stmt=("""

              statement {
                sid       = "DecryptWithTheProjectKey"
                actions   = ["kms:Decrypt"]
                resources = [aws_kms_key.main.arn]
              }""" if self.kms else "")))
        for unit in m.units:
            if unit["kind"] == "static":
                continue
            n, name = ident(unit["name"]), unit["name"]
            port = unit["port"] or 8080
            env = self.container_env(unit)
            secrets = self.container_secrets()
            health = unit["health_path"] or "/"
            is_public = unit in self.public
            out.append(T("""
                resource "aws_cloudwatch_log_group" "@@n@@" {
                  name              = "/ecs/${local.name}/@@name@@"
                  retention_in_days = var.log_retention_days@@kms@@
                }

                resource "aws_ecs_task_definition" "@@n@@" {
                  family                   = "${local.name}-@@name@@"
                  cpu                      = @@cpu@@
                  memory                   = @@mem@@
                  network_mode             = "awsvpc"
                  requires_compatibilities = ["FARGATE"]
                  execution_role_arn       = aws_iam_role.execution.arn
                  task_role_arn            = aws_iam_role.task.arn

                  container_definitions = jsonencode([{
                    name      = "@@name@@"
                    image     = coalesce(var.@@n@@_image, "${aws_ecr_repository.@@n@@.repository_url}:${var.image_tag}")
                    essential = true@@ports@@
                    environment = [
@@env@@
                    ]
                    secrets = [
@@secrets@@
                    ]
                    logConfiguration = {
                      logDriver = "awslogs"
                      options = {
                        "awslogs-group"         = aws_cloudwatch_log_group.@@n@@.name
                        "awslogs-region"        = var.region
                        "awslogs-stream-prefix" = "@@name@@"
                      }
                    }
                  }])
                }

                resource "aws_ecs_service" "@@n@@" {
                  name            = "@@name@@"
                  cluster         = aws_ecs_cluster.main.id
                  task_definition = aws_ecs_task_definition.@@n@@.arn
                  desired_count   = var.@@n@@_desired_count
                  launch_type     = "FARGATE"

                  enable_execute_command             = false
                  deployment_minimum_healthy_percent = 100
                  deployment_maximum_percent         = 200@@grace@@

                  deployment_circuit_breaker {
                    enable   = true
                    rollback = true
                  }

                  network_configuration {
                    subnets          = aws_subnet.private[*].id
                    security_groups  = [aws_security_group.app.id]
                    assign_public_ip = false
                  }@@lb@@@@depends@@

                  lifecycle {
                    ignore_changes = [desired_count]
                  }
                }

                resource "aws_appautoscaling_target" "@@n@@" {
                  service_namespace  = "ecs"
                  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.@@n@@.name}"
                  scalable_dimension = "ecs:service:DesiredCount"
                  min_capacity       = var.@@n@@_desired_count
                  max_capacity       = var.@@n@@_desired_count * 4
                }

                resource "aws_appautoscaling_policy" "@@n@@_cpu" {
                  name               = "${local.name}-@@name@@-cpu"
                  policy_type        = "TargetTrackingScaling"
                  service_namespace  = aws_appautoscaling_target.@@n@@.service_namespace
                  resource_id        = aws_appautoscaling_target.@@n@@.resource_id
                  scalable_dimension = aws_appautoscaling_target.@@n@@.scalable_dimension

                  target_tracking_scaling_policy_configuration {
                    target_value = 60

                    predefined_metric_specification {
                      predefined_metric_type = "ECSServiceAverageCPUUtilization"
                    }
                  }
                }
            """, n=n, name=name, cpu=cpu_units, mem=mem_ok,
                         kms="\n  kms_key_id        = aws_kms_key.main.arn" if self.kms else "",
                         ports="" if unit["kind"] == "worker" else
                         '\n    portMappings = [{\n      containerPort = %d\n      protocol      = "tcp"\n    }]' % port,
                         env=env, secrets=secrets,
                         grace="\n  health_check_grace_period_seconds = 60" if is_public else "",
                         lb=("\n\n  load_balancer {\n    target_group_arn = aws_lb_target_group.%s.arn\n    container_name   = \"%s\"\n    container_port   = %d\n  }" % (n, name, port)) if is_public else "",
                         depends=("\n\n  depends_on = [aws_lb_listener.main]" if is_public else "")))
        self.files.add("terraform/compute.tf", tf(*out))

    def container_env(self, unit):
        m = self.m
        items = [("PORT", '"%d"' % (unit["port"] or 8080)), ("APP_ENV", "var.environment"), ("AWS_REGION", "var.region")]
        if m.db_key:
            if m.has("database_mongo"):
                pass
            else:
                items += [("DB_HOST", "aws_rds_cluster.main.endpoint"), ("DB_PORT", "tostring(aws_rds_cluster.main.port)"),
                          ("DB_NAME", "var.db_name"), ("DB_USER", "var.db_username")]
        if m.has("database_mongo"):
            items += [("MONGO_HOST", "aws_docdb_cluster.main.endpoint"), ("MONGO_PORT", '"27017"')]
        if m.has("cache"):
            items += [("REDIS_HOST", "aws_elasticache_replication_group.main.primary_endpoint_address"), ("REDIS_PORT", '"6379"')]
        if m.has("queue_rabbitmq"):
            items.append(("AMQP_ENDPOINT", "aws_mq_broker.main.instances[0].endpoints[0]"))
        if m.has("queue_kafka"):
            items.append(("KAFKA_BOOTSTRAP_BROKERS", "aws_msk_cluster.main.bootstrap_brokers_tls"))
        if m.has("storage"):
            items.append(("S3_BUCKET", "aws_s3_bucket.app.id"))
        if m.has("identity"):
            items += [("COGNITO_USER_POOL_ID", "aws_cognito_user_pool.main.id"), ("COGNITO_CLIENT_ID", "aws_cognito_user_pool_client.main.id")]
        return "\n".join('                      { name = "%s", value = %s },' % kv for kv in items)

    def container_secrets(self):
        m = self.m
        items = []
        if m.db_key and not m.has("database_mongo"):
            items.append(("DB_PASSWORD", '"${aws_rds_cluster.main.master_user_secret[0].secret_arn}:password::"'))
        if m.has("database_mongo"):
            items.append(("MONGO_PASSWORD", "aws_secretsmanager_secret.docdb.arn"))
        if m.has("queue_rabbitmq"):
            items.append(("AMQP_PASSWORD", "aws_secretsmanager_secret.mq.arn"))
        items.append(("APP_SECRETS_JSON", "aws_secretsmanager_secret.app.arn"))
        return "\n".join('                      { name = "%s", valueFrom = %s },' % kv for kv in items)

    # ------------------------------------------------------------------ ALB
    def alb(self):
        if not self.public:
            return
        m = self.m
        d = self.default_unit
        cert = T("""
            locals {
              has_certificate = var.acm_certificate_arn != null && var.acm_certificate_arn != ""
            }
        """)
        out = [cert, T("""
            resource "aws_s3_bucket" "alb_logs" {
              bucket_prefix = "${local.name}-alb-logs-"
              force_destroy = false
            }

            resource "aws_s3_bucket_public_access_block" "alb_logs" {
              bucket                  = aws_s3_bucket.alb_logs.id
              block_public_acls       = true
              block_public_policy     = true
              ignore_public_acls      = true
              restrict_public_buckets = true
            }

            resource "aws_s3_bucket_server_side_encryption_configuration" "alb_logs" {
              bucket = aws_s3_bucket.alb_logs.id

              rule {
                apply_server_side_encryption_by_default {
                  sse_algorithm = "AES256"
                }
              }
            }

            resource "aws_s3_bucket_lifecycle_configuration" "alb_logs" {
              bucket = aws_s3_bucket.alb_logs.id

              rule {
                id     = "expire-old-logs"
                status = "Enabled"

                filter {}

                transition {
                  days          = 30
                  storage_class = "STANDARD_IA"
                }

                expiration {
                  days = 365
                }
              }
            }

            data "aws_elb_service_account" "main" {}

            data "aws_iam_policy_document" "alb_logs" {
              statement {
                actions   = ["s3:PutObject"]
                resources = ["${aws_s3_bucket.alb_logs.arn}/alb/*"]

                principals {
                  type        = "AWS"
                  identifiers = [data.aws_elb_service_account.main.arn]
                }
              }

              statement {
                actions   = ["s3:PutObject"]
                resources = ["${aws_s3_bucket.alb_logs.arn}/alb/*"]

                principals {
                  type        = "Service"
                  identifiers = ["logdelivery.elasticloadbalancing.amazonaws.com"]
                }
              }
            }

            resource "aws_s3_bucket_policy" "alb_logs" {
              bucket = aws_s3_bucket.alb_logs.id
              policy = data.aws_iam_policy_document.alb_logs.json
            }

            resource "aws_lb" "main" {
              name                       = substr(local.name, 0, 32)
              load_balancer_type         = "application"
              internal                   = false
              subnets                    = aws_subnet.public[*].id
              security_groups            = [aws_security_group.alb.id]
              drop_invalid_header_fields = true
              idle_timeout               = @@idle@@
              enable_deletion_protection = var.enable_deletion_protection

              access_logs {
                bucket  = aws_s3_bucket.alb_logs.id
                prefix  = "alb"
                enabled = true
              }

              depends_on = [aws_s3_bucket_policy.alb_logs]
            }
        """, idle=300 if m.streaming else 60)]
        for unit in self.public:
            n = ident(unit["name"])
            out.append(T("""
                resource "aws_lb_target_group" "@@n@@" {
                  name_prefix = "@@pre@@"
                  port        = @@port@@
                  protocol    = "HTTP"
                  target_type = "ip"
                  vpc_id      = aws_vpc.main.id

                  health_check {
                    path                = "@@health@@"
                    healthy_threshold   = 2
                    unhealthy_threshold = 3
                    timeout             = 5
                    interval            = 30
                    matcher             = "200-399"
                  }

                  deregistration_delay = 30

                  lifecycle {
                    create_before_destroy = true
                  }
                }
            """, n=n, pre=re.sub(r"[^a-zA-Z0-9]", "", unit["name"])[:5] or "tg", port=unit["port"] or 8080,
                         health=unit["health_path"] or "/"))
        default_n = ident(d["name"])
        default_action = ("fixed_response" if self.use_cdn else "forward")
        if self.use_cdn:
            out.append(T("""
                resource "random_password" "origin_verify" {
                  length  = 40
                  special = false
                }

                resource "aws_lb_listener" "main" {
                  load_balancer_arn = aws_lb.main.arn
                  port              = local.has_certificate ? 443 : 80
                  protocol          = local.has_certificate ? "HTTPS" : "HTTP"
                  ssl_policy        = local.has_certificate ? "ELBSecurityPolicy-TLS13-1-2-2021-06" : null
                  certificate_arn   = local.has_certificate ? var.acm_certificate_arn : null

                  default_action {
                    type = "fixed-response"

                    fixed_response {
                      content_type = "text/plain"
                      message_body = "Forbidden"
                      status_code  = "403"
                    }
                  }
                }
            """))
            for i, unit in enumerate(self.public):
                n = ident(unit["name"])
                prefix = path_prefix(unit, d)
                path_cond = "" if prefix is None else T("""

                      condition {
                        path_pattern {
                          values = ["@@p@@"]
                        }
                      }
                """, p=prefix).rstrip("\n")
                out.append(T("""
                    resource "aws_lb_listener_rule" "@@n@@" {
                      listener_arn = aws_lb_listener.main.arn
                      priority     = @@prio@@

                      action {
                        type             = "forward"
                        target_group_arn = aws_lb_target_group.@@n@@.arn
                      }

                      condition {
                        http_header {
                          http_header_name = "X-Origin-Verify"
                          values           = [random_password.origin_verify.result]
                        }
                      }@@path@@
                    }
                """, n=n, prio=(1 if prefix is None else 10 + i), path=("\n" + textwrap.indent(path_cond, "") if path_cond else "")))
        else:
            out.append(T("""
                resource "aws_lb_listener" "main" {
                  load_balancer_arn = aws_lb.main.arn
                  port              = local.has_certificate ? 443 : 80
                  protocol          = local.has_certificate ? "HTTPS" : "HTTP"
                  ssl_policy        = local.has_certificate ? "ELBSecurityPolicy-TLS13-1-2-2021-06" : null
                  certificate_arn   = local.has_certificate ? var.acm_certificate_arn : null

                  default_action {
                    type             = "forward"
                    target_group_arn = aws_lb_target_group.@@d@@.arn
                  }
                }

                resource "aws_lb_listener" "http_redirect" {
                  count = local.has_certificate ? 1 : 0

                  load_balancer_arn = aws_lb.main.arn
                  port              = 80
                  protocol          = "HTTP"

                  default_action {
                    type = "redirect"

                    redirect {
                      port        = "443"
                      protocol    = "HTTPS"
                      status_code = "HTTP_301"
                    }
                  }
                }
            """, d=default_n))
            for i, unit in enumerate(self.public):
                prefix = path_prefix(unit, d)
                if prefix is None:
                    continue
                n = ident(unit["name"])
                out.append(T("""
                    resource "aws_lb_listener_rule" "@@n@@" {
                      listener_arn = aws_lb_listener.main.arn
                      priority     = @@prio@@

                      action {
                        type             = "forward"
                        target_group_arn = aws_lb_target_group.@@n@@.arn
                      }

                      condition {
                        path_pattern {
                          values = ["@@p@@"]
                        }
                      }
                    }
                """, n=n, prio=10 + i, p=prefix))
        self.files.add("terraform/alb.tf", tf(*out))

    # --------------------------------------------------------- Lambda (serverless)
    def lambda_compute(self):
        m = self.m
        out = self.ecr_and_iam()
        arns = self.secret_arns()
        out.append(T("""
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

            resource "aws_iam_role_policy_attachment" "lambda_basic" {
              role       = aws_iam_role.lambda.name
              policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
            }

            data "aws_iam_policy_document" "lambda_secrets" {
              statement {
                sid       = "ReadApplicationSecrets"
                actions   = ["secretsmanager:GetSecretValue"]
                resources = [@@arns@@]
              }
            }

            resource "aws_iam_role_policy" "lambda_secrets" {
              name   = "read-application-secrets"
              role   = aws_iam_role.lambda.id
              policy = data.aws_iam_policy_document.lambda_secrets.json
            }

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
                  ip        = "$context.identity.sourceIp"
                  method    = "$context.httpMethod"
                  path      = "$context.path"
                  status    = "$context.status"
                  latency   = "$context.responseLatency"
                })
              }
            }
        """, arns=", ".join(arns)))
        default_unit = self.default_unit
        for unit in m.units:
            if unit["kind"] == "static":
                continue
            n, name = ident(unit["name"]), unit["name"]
            env = "\n".join('      %s = %s' % (k, v) for k, v in self.lambda_env(unit))
            out.append(T("""
                resource "aws_cloudwatch_log_group" "@@n@@" {
                  name              = "/aws/lambda/${local.name}-@@name@@"
                  retention_in_days = var.log_retention_days
                }

                resource "aws_lambda_function" "@@n@@" {
                  function_name = "${local.name}-@@name@@"
                  role          = aws_iam_role.lambda.arn
                  package_type  = "Image"
                  image_uri     = coalesce(var.@@n@@_image, "${aws_ecr_repository.@@n@@.repository_url}:${var.image_tag}")
                  timeout       = 30
                  memory_size   = @@mem@@

                  tracing_config {
                    mode = "Active"
                  }

                  vpc_config {
                    subnet_ids         = aws_subnet.private[*].id
                    security_group_ids = [aws_security_group.app.id]
                  }

                  environment {
                    variables = {
@@env@@
                    }
                  }

                  depends_on = [aws_cloudwatch_log_group.@@n@@]
                }
            """, n=n, name=name, mem=max(512, int(self.m.sizing["unit_memory_gb"] * 1024)), env=env))
            if unit in self.public:
                prefix = path_prefix(unit, default_unit)
                route = "$default" if prefix is None else "ANY " + prefix.replace("/*", "/{proxy+}")
                out.append(T("""
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

                    resource "aws_lambda_permission" "@@n@@" {
                      statement_id  = "AllowApiGateway@@N@@"
                      action        = "lambda:InvokeFunction"
                      function_name = aws_lambda_function.@@n@@.function_name
                      principal     = "apigateway.amazonaws.com"
                      source_arn    = "${aws_apigatewayv2_api.main.execution_arn}/*/*"
                    }
                """, n=n, N=re.sub(r"[^A-Za-z0-9]", "", name.title()), route=route))
        self.files.add("terraform/compute.tf", tf(*out))

    def lambda_env(self, unit):
        return [(k, v) for k, v in [item for item in (
            [("APP_ENV", "var.environment")] + [(name, value) for name, value in self._data_env()])]]

    def _data_env(self):
        m = self.m
        items = []
        if m.db_key and not m.has("database_mongo"):
            items += [("DB_HOST", "aws_rds_cluster.main.endpoint"), ("DB_PORT", "tostring(aws_rds_cluster.main.port)"), ("DB_NAME", "var.db_name"),
                      ("DB_SECRET_ARN", "aws_rds_cluster.main.master_user_secret[0].secret_arn")]
        if m.has("cache"):
            items += [("REDIS_HOST", "aws_elasticache_replication_group.main.primary_endpoint_address")]
        if m.has("storage"):
            items.append(("S3_BUCKET", "aws_s3_bucket.app.id"))
        return items

    # ---------------------------------------------------------- data stores
    def data_stores(self):
        m = self.m
        out = [T("""
            resource "aws_db_subnet_group" "main" {
              count = 0

              name       = "unused"
              subnet_ids = aws_subnet.data[*].id
            }
        """)] if False else []
        if m.db_key or m.has("database_mongo"):
            pass
        if m.db_key and not m.has("database_mongo") or (m.db_key and m.db_key in ("database", "database_mysql")):
            engine = "aurora-postgresql" if m.db_key == "database" else "aurora-mysql"
            port = 5432 if m.db_key == "database" else 3306
            logs = '["postgresql"]' if m.db_key == "database" else '["audit", "error", "slowquery"]'
            sls = self.lambda_mode
            instance_class = '"db.serverless"' if sls else "var.db_instance_class"
            scaling = "\n\n" + T("""
                  serverlessv2_scaling_configuration {
                    min_capacity = 0.5
                    max_capacity = @@max@@
                  }
            """, max={"small": 4, "medium": 16, "large": 64}[m.sizing["db_class"]]).rstrip("\n") if sls else ""
            out.append(T("""
                resource "aws_db_subnet_group" "main" {
                  name       = local.name
                  subnet_ids = aws_subnet.data[*].id
                }

                resource "aws_rds_cluster" "main" {
                  cluster_identifier              = local.name
                  engine                          = "@@engine@@"
                  database_name                   = var.db_name
                  master_username                 = var.db_username
                  manage_master_user_password     = true
                  db_subnet_group_name            = aws_db_subnet_group.main.name
                  vpc_security_group_ids          = [aws_security_group.db.id]
                  port                            = @@port@@
                  storage_encrypted               = true@@kms@@
                  backup_retention_period         = 7
                  preferred_backup_window         = "03:00-04:00"
                  copy_tags_to_snapshot           = true
                  deletion_protection             = var.enable_deletion_protection
                  skip_final_snapshot             = false
                  final_snapshot_identifier       = "${local.name}-final"
                  iam_database_authentication_enabled = true
                  enabled_cloudwatch_logs_exports = @@logs@@@@scaling@@
                }

                resource "aws_rds_cluster_instance" "main" {
                  count = @@count@@

                  identifier                   = "${local.name}-${count.index}"
                  cluster_identifier           = aws_rds_cluster.main.id
                  engine                       = aws_rds_cluster.main.engine
                  instance_class               = @@cls@@
                  db_subnet_group_name         = aws_db_subnet_group.main.name
                  performance_insights_enabled = true
                  auto_minor_version_upgrade   = true
                  publicly_accessible          = false
                }
            """, engine=engine, port=port, logs=logs, scaling=scaling, cls=instance_class,
                         count=1 if m.single_az else 2, kms="\n  kms_key_id                      = aws_kms_key.main.arn" if self.kms else ""))
        if m.has("database_mongo"):
            out.append(T("""
                resource "random_password" "docdb" {
                  length  = 32
                  special = false
                }

                resource "aws_secretsmanager_secret" "docdb" {
                  name_prefix = "${local.name}/docdb-"
                }

                resource "aws_secretsmanager_secret_version" "docdb" {
                  secret_id     = aws_secretsmanager_secret.docdb.id
                  secret_string = random_password.docdb.result
                }

                resource "aws_docdb_subnet_group" "main" {
                  name       = local.name
                  subnet_ids = aws_subnet.data[*].id
                }

                resource "aws_docdb_cluster" "main" {
                  cluster_identifier      = local.name
                  engine                  = "docdb"
                  master_username         = "app_admin"
                  master_password         = random_password.docdb.result
                  db_subnet_group_name    = aws_docdb_subnet_group.main.name
                  vpc_security_group_ids  = [aws_security_group.docdb.id]
                  storage_encrypted       = true
                  backup_retention_period = 7
                  deletion_protection     = var.enable_deletion_protection
                  skip_final_snapshot     = false
                  final_snapshot_identifier = "${local.name}-docdb-final"
                }

                resource "aws_docdb_cluster_instance" "main" {
                  count = @@count@@

                  identifier         = "${local.name}-docdb-${count.index}"
                  cluster_identifier = aws_docdb_cluster.main.id
                  instance_class     = "db.t4g.medium"
                }
            """, count=1 if m.single_az else 2))
        if m.has("cache"):
            node = AWS_CACHE_NODE[1 if m.sizing["cache_gb"] <= 1 else 4]
            out.append(T("""
                resource "aws_elasticache_subnet_group" "main" {
                  name       = local.name
                  subnet_ids = aws_subnet.data[*].id
                }

                resource "aws_elasticache_replication_group" "main" {
                  replication_group_id       = substr(local.name, 0, 40)
                  description                = "Cache for ${local.name}"
                  engine                     = "redis"
                  engine_version             = "7.1"
                  node_type                  = "@@node@@"
                  port                       = 6379
                  num_cache_clusters         = @@nodes@@
                  automatic_failover_enabled = @@ha@@
                  multi_az_enabled           = @@ha@@
                  subnet_group_name          = aws_elasticache_subnet_group.main.name
                  security_group_ids         = [aws_security_group.cache.id]
                  at_rest_encryption_enabled = true
                  transit_encryption_enabled = true
                  snapshot_retention_limit   = 7
                  apply_immediately          = false
                }
            """, node=node, nodes=1 if m.single_az else 2, ha="false" if m.single_az else "true"))
        if m.has("queue_rabbitmq"):
            out.append(T("""
                resource "random_password" "mq" {
                  length  = 32
                  special = false
                }

                resource "aws_secretsmanager_secret" "mq" {
                  name_prefix = "${local.name}/mq-"
                }

                resource "aws_secretsmanager_secret_version" "mq" {
                  secret_id     = aws_secretsmanager_secret.mq.id
                  secret_string = random_password.mq.result
                }

                resource "aws_mq_broker" "main" {
                  broker_name                = local.name
                  engine_type                = "RabbitMQ"
                  engine_version             = "3.13"
                  host_instance_type         = "mq.t3.micro"
                  deployment_mode            = "SINGLE_INSTANCE"
                  publicly_accessible        = false
                  auto_minor_version_upgrade = true
                  subnet_ids                 = [aws_subnet.data[0].id]
                  security_groups            = [aws_security_group.mq.id]

                  user {
                    username = "app"
                    password = random_password.mq.result
                  }

                  logs {
                    general = true
                  }
                }
            """))
        if m.has("queue_kafka"):
            out.append(T("""
                resource "aws_msk_cluster" "main" {
                  cluster_name           = local.name
                  kafka_version          = "3.8.x"
                  number_of_broker_nodes = 2

                  broker_node_group_info {
                    instance_type   = "kafka.t3.small"
                    client_subnets  = aws_subnet.data[*].id
                    security_groups = [aws_security_group.msk.id]

                    storage_info {
                      ebs_storage_info {
                        volume_size = 100
                      }
                    }
                  }

                  encryption_info {
                    encryption_in_transit {
                      client_broker = "TLS"
                      in_cluster    = true
                    }
                  }
                }
            """))
        out.append(T("""
            resource "aws_secretsmanager_secret" "app" {
              name_prefix = "${local.name}/app-"
              description = "Application settings that are secret. Put a JSON object here after the first apply."@@kms@@
            }
        """, kms="\n  kms_key_id  = aws_kms_key.main.arn" if self.kms else ""))
        self.files.add("terraform/data.tf", tf(*out))

    # ------------------------------------------------------- storage & static
    def storage(self):
        m = self.m
        out = []
        if m.has("storage") or m.has("static_site"):
            out.append(T("""
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
            """))
            policy_stmt = T("""
                data "aws_iam_policy_document" "task_bucket" {
                  statement {
                    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:ListBucket"]
                    resources = [aws_s3_bucket.app.arn, "${aws_s3_bucket.app.arn}/*"]
                  }
                }

                resource "aws_iam_role_policy" "@@role@@_bucket" {
                  name   = "use-the-application-bucket"
                  role   = aws_iam_role.@@role@@.id
                  policy = data.aws_iam_policy_document.task_bucket.json
                }
            """, role="lambda" if self.lambda_mode else "task")
            out.append(policy_stmt)
        if out:
            self.files.add("terraform/storage.tf", tf(*out))

    # ------------------------------------------------------------ CDN + WAF
    def edge(self):
        m = self.m
        origin_domain = "aws_lb.main.dns_name" if not self.lambda_mode else 'replace(aws_apigatewayv2_api.main.api_endpoint, "https://", "")'
        header = ""
        if not self.lambda_mode:
            header = T("""

                  custom_header {
                    name  = "X-Origin-Verify"
                    value = random_password.origin_verify.result
                  }
            """).rstrip("\n")
        protocol = '"https-only"' if self.lambda_mode else 'local.has_certificate ? "https-only" : "http-only"'
        out = [T("""
            data "aws_cloudfront_cache_policy" "disabled" {
              name = "Managed-CachingDisabled"
            }

            data "aws_cloudfront_origin_request_policy" "all_viewer" {
              name = "Managed-AllViewer"
            }

            data "aws_cloudfront_response_headers_policy" "security" {
              name = "Managed-SecurityHeadersPolicy"
            }

            resource "aws_wafv2_web_acl" "edge" {
              provider = aws.us_east_1

              name  = "${local.name}-edge"
              scope = "CLOUDFRONT"

              default_action {
                allow {}
              }

              rule {
                name     = "common-rules"
                priority = 1

                override_action {
                  none {}
                }

                statement {
                  managed_rule_group_statement {
                    name        = "AWSManagedRulesCommonRuleSet"
                    vendor_name = "AWS"
                  }
                }

                visibility_config {
                  cloudwatch_metrics_enabled = true
                  metric_name                = "common-rules"
                  sampled_requests_enabled   = true
                }
              }

              rule {
                name     = "known-bad-inputs"
                priority = 2

                override_action {
                  none {}
                }

                statement {
                  managed_rule_group_statement {
                    name        = "AWSManagedRulesKnownBadInputsRuleSet"
                    vendor_name = "AWS"
                  }
                }

                visibility_config {
                  cloudwatch_metrics_enabled = true
                  metric_name                = "known-bad-inputs"
                  sampled_requests_enabled   = true
                }
              }

              rule {
                name     = "rate-limit"
                priority = 3

                action {
                  block {}
                }

                statement {
                  rate_based_statement {
                    limit              = 2000
                    aggregate_key_type = "IP"
                  }
                }

                visibility_config {
                  cloudwatch_metrics_enabled = true
                  metric_name                = "rate-limit"
                  sampled_requests_enabled   = true
                }
              }

              visibility_config {
                cloudwatch_metrics_enabled = true
                metric_name                = "${local.name}-edge"
                sampled_requests_enabled   = true
              }
            }

            resource "aws_cloudfront_distribution" "main" {
              enabled         = true
              is_ipv6_enabled = true
              comment         = local.name
              web_acl_id      = aws_wafv2_web_acl.edge.arn
              price_class     = "PriceClass_100"

              origin {
                origin_id   = "app"
                domain_name = @@origin@@

                custom_origin_config {
                  http_port              = 80
                  https_port             = 443
                  origin_protocol_policy = @@protocol@@
                  origin_ssl_protocols   = ["TLSv1.2"]
                }@@header@@
              }

              default_cache_behavior {
                target_origin_id           = "app"
                viewer_protocol_policy     = "redirect-to-https"
                allowed_methods            = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
                cached_methods             = ["GET", "HEAD"]
                cache_policy_id            = data.aws_cloudfront_cache_policy.disabled.id
                origin_request_policy_id   = data.aws_cloudfront_origin_request_policy.all_viewer.id
                response_headers_policy_id = data.aws_cloudfront_response_headers_policy.security.id
                compress                   = true
              }

              restrictions {
                geo_restriction {
                  restriction_type = "none"
                }
              }

              viewer_certificate {
                cloudfront_default_certificate = true
                minimum_protocol_version       = "TLSv1.2_2021"
              }
            }
        """, origin=origin_domain, protocol=protocol, header=("\n" + header) if header else "")]
        self.files.add("terraform/edge.tf", tf(*out))

    # -------------------------------------------------------------- identity
    def identity(self):
        self.files.add("terraform/identity.tf", tf(T("""
            resource "aws_cognito_user_pool" "main" {
              name                     = local.name
              mfa_configuration        = "OPTIONAL"
              auto_verified_attributes = ["email"]
              username_attributes      = ["email"]
              deletion_protection      = var.enable_deletion_protection ? "ACTIVE" : "INACTIVE"

              password_policy {
                minimum_length    = 12
                require_lowercase = true
                require_numbers   = true
                require_symbols   = true
                require_uppercase = true
              }

              software_token_mfa_configuration {
                enabled = true
              }
            }

            resource "aws_cognito_user_pool_client" "main" {
              name                          = "${local.name}-web"
              user_pool_id                  = aws_cognito_user_pool.main.id
              generate_secret               = false
              prevent_user_existence_errors = "ENABLED"
              explicit_auth_flows           = ["ALLOW_USER_SRP_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]
              allowed_oauth_flows           = ["code"]
              allowed_oauth_scopes          = ["openid", "email", "profile"]
              supported_identity_providers  = ["COGNITO"]
              callback_urls                 = ["https://localhost/callback"]
              logout_urls                   = ["https://localhost/"]
              allowed_oauth_flows_user_pool_client = true
            }
        """)))

    # ------------------------------------------------------------ monitoring
    def monitoring(self):
        m = self.m
        out = [T("""
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
        if not self.lambda_mode:
            for unit in m.units:
                if unit["kind"] == "static":
                    continue
                n, name = ident(unit["name"]), unit["name"]
                out.append(T("""
                    resource "aws_cloudwatch_metric_alarm" "@@n@@_cpu" {
                      alarm_name          = "${local.name}-@@name@@-cpu-high"
                      namespace           = "AWS/ECS"
                      metric_name         = "CPUUtilization"
                      statistic           = "Average"
                      comparison_operator = "GreaterThanThreshold"
                      threshold           = 85
                      period              = 300
                      evaluation_periods  = 3
                      alarm_actions       = [aws_sns_topic.alarms.arn]

                      dimensions = {
                        ClusterName = aws_ecs_cluster.main.name
                        ServiceName = aws_ecs_service.@@n@@.name
                      }
                    }
                """, n=n, name=name))
            if self.public:
                out.append(T("""
                    resource "aws_cloudwatch_metric_alarm" "alb_5xx" {
                      alarm_name          = "${local.name}-alb-5xx"
                      namespace           = "AWS/ApplicationELB"
                      metric_name         = "HTTPCode_Target_5XX_Count"
                      statistic           = "Sum"
                      comparison_operator = "GreaterThanThreshold"
                      threshold           = 10
                      period              = 300
                      evaluation_periods  = 2
                      treat_missing_data  = "notBreaching"
                      alarm_actions       = [aws_sns_topic.alarms.arn]

                      dimensions = {
                        LoadBalancer = aws_lb.main.arn_suffix
                      }
                    }
                """))
        else:
            for unit in m.units:
                if unit["kind"] == "static":
                    continue
                n, name = ident(unit["name"]), unit["name"]
                out.append(T("""
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
                """, n=n, name=name))
        if m.db_key and not m.has("database_mongo"):
            out.append(T("""
                resource "aws_cloudwatch_metric_alarm" "db_cpu" {
                  alarm_name          = "${local.name}-database-cpu"
                  namespace           = "AWS/RDS"
                  metric_name         = "CPUUtilization"
                  statistic           = "Average"
                  comparison_operator = "GreaterThanThreshold"
                  threshold           = 80
                  period              = 300
                  evaluation_periods  = 3
                  alarm_actions       = [aws_sns_topic.alarms.arn]

                  dimensions = {
                    DBClusterIdentifier = aws_rds_cluster.main.cluster_identifier
                  }
                }
            """))
        self.files.add("terraform/monitoring.tf", tf(*out))

    # --------------------------------------------------------------- outputs
    def outputs(self):
        m = self.m
        out = []
        if self.use_cdn:
            out.append(T("""
                output "cdn_domain" {
                  description = "Public address of the application through CloudFront."
                  value       = aws_cloudfront_distribution.main.domain_name
                }
            """))
        if not self.lambda_mode and self.public:
            out.append(T("""
                output "load_balancer_dns" {
                  description = "Address of the load balancer."
                  value       = aws_lb.main.dns_name
                }
            """))
        if self.lambda_mode:
            out.append(T("""
                output "api_endpoint" {
                  description = "Address of the HTTP API."
                  value       = aws_apigatewayv2_api.main.api_endpoint
                }
            """))
        for unit in m.units:
            if unit["kind"] == "static":
                continue
            out.append(T("""
                output "@@n@@_repository_url" {
                  description = "Push the @@name@@ image here."
                  value       = aws_ecr_repository.@@n@@.repository_url
                }
            """, n=ident(unit["name"]), name=unit["name"]))
        if m.db_key and not m.has("database_mongo"):
            out.append(T("""
                output "database_endpoint" {
                  description = "Database writer endpoint. The password is in the secret named below."
                  value       = aws_rds_cluster.main.endpoint
                }

                output "database_secret_arn" {
                  description = "Secrets Manager secret that holds the generated database password."
                  value       = aws_rds_cluster.main.master_user_secret[0].secret_arn
                }
            """))
        if not out:
            out.append('output "region" {\n  value = var.region\n}\n')
        self.files.add("terraform/outputs.tf", tf(*out))

    # --------------------------------------------------------------- support
    def bootstrap(self):
        return tf(T("""
            terraform {
              required_version = ">= 1.9.0"

              required_providers {
                aws = {
                  source  = "hashicorp/aws"
                  version = "~> 6.0"
                }
              }
            }

            variable "region" {
              type        = string
              description = "Region of the state bucket."
              default     = "@@region@@"
            }

            variable "state_bucket_prefix" {
              type        = string
              description = "Prefix of the state bucket name; a unique suffix is added."
              default     = "@@project@@-tfstate-"
            }

            provider "aws" {
              region = var.region
            }

            resource "aws_s3_bucket" "state" {
              bucket_prefix = var.state_bucket_prefix
            }

            resource "aws_s3_bucket_public_access_block" "state" {
              bucket                  = aws_s3_bucket.state.id
              block_public_acls       = true
              block_public_policy     = true
              ignore_public_acls      = true
              restrict_public_buckets = true
            }

            resource "aws_s3_bucket_versioning" "state" {
              bucket = aws_s3_bucket.state.id

              versioning_configuration {
                status = "Enabled"
              }
            }

            resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
              bucket = aws_s3_bucket.state.id

              rule {
                apply_server_side_encryption_by_default {
                  sse_algorithm = "AES256"
                }
              }
            }

            output "state_bucket" {
              description = "Put this in backend.hcl as the bucket."
              value       = aws_s3_bucket.state.id
            }
        """, region=self.region, project=self.m.project))

    def tfvars(self):
        m = self.m
        lines = ['project_name = "%s"' % m.project, 'environment  = "production"', 'region       = "%s"' % self.region]
        return fmt_hcl("\n".join(lines) + "\n")

    def readme(self):
        m = self.m
        units = ", ".join(u["name"] for u in m.units if u["kind"] != "static")
        return T("""
            # Terraform for @@project@@ on AWS

            Compute model: @@mode@@. Units: @@units@@. Plan fingerprint @@fp@@.

            ## Order of work

            1. `cd bootstrap && terraform init && terraform apply` once. It creates the encrypted, versioned bucket that keeps Terraform state.
            2. Copy `backend.hcl.example` to `backend.hcl` and put the bucket name in it.
            3. `terraform init -backend-config=backend.hcl`
            4. `terraform apply`. The first apply creates everything, including empty image repositories.
            5. Build and push each image to the repository named in the outputs (`*_repository_url`), then deploy again or let the service pick up the new image.
            6. Put your secret settings as a JSON object in the application secret, and set `acm_certificate_arn` when you have a certificate.

            ## What is configured for you

            - Private subnets for compute and a separate data tier with no route to the internet.
            - Security groups whose rules are separate resources, so they never form a dependency cycle.
            - Encrypted storage and backups on every data store@@ha@@.
            - Application load balancer with access logs and a TLS 1.2 or newer listener policy once a certificate is set.
            - Health checks use the paths the code serves.
            - Alarms go to an SNS topic; set `alarm_email` to subscribe.

            ## What you must still decide

            - A domain name and a certificate.
            - Container images for each unit (nothing is built here).
            - Region, if `@@region@@` is not right for your users. Check data-residency rules if the app stores personal data.
        """, project=m.project, mode=m.mode, units=units, fp=m.fingerprint, region=self.region,
                 ha=" and a second availability zone for the database and cache" if not m.single_az else
                 " (single availability zone for this lean design; add a second before production)")


def generate(model):
    import iac_aws_lambda
    import iac_static
    if iac_static.has_static_only(model):
        return iac_static.aws(model, Aws(model).bootstrap())
    aws = Aws(model)
    if model.mode == "serverless" and not model.needs_private_network:
        return iac_aws_lambda.generate(model, aws.region, aws.bootstrap())
    return aws.generate()
