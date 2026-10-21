#!/usr/bin/env python3
"""A monthly cost estimate for the planned architecture, from vendor prices read during the run.

Standard library only. The estimate prices the plan exactly as decided (it never changes a service) at the
sizing the capacity model implies. Every line shows its quantity, unit price, source URL and read date. A
service with no public price source is listed as unpriced with the way to price it; nothing is invented.

Reproducible: prices go into .solsarch/prices.json (the price book). Later runs read the book, so the same
inputs give the same numbers until you ask for fresh prices with --refresh. The 12-month projection, the
cost range (a fixed-sequence simulation, no randomness) and the optimisations are fixed rules over those lines.

Usage:
  python costs.py --repo REPO [--refresh] [--offline] [--prices FILE]
"""
import argparse
import json
import math
import re
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import price_sources as ps  # noqa: E402

HOURS = 730.0
SECONDS = HOURS * 3600.0
BOOK_FILE = "prices.json"
DEFAULT_REGION = {"aws": "us-east-1", "azure": "eastus", "gcp": "us-central1"}
GROWTH_PER_MONTH = {"prototype": 0.05, "growth": 0.08, "enterprise": 0.03}
RESERVED_GUIDE = {"savings_plan_1yr": 0.27, "savings_plan_3yr": 0.52, "reserved_1yr": 0.35, "reserved_3yr": 0.55,
                  "spot": 0.70, "gcp_committed_1yr": 0.28, "gcp_committed_3yr": 0.46}
BILLING_NOTES = {
    "aws": "Per-second billing with a 60-second minimum on EC2, per-second on Fargate, per-millisecond on Lambda. No automatic discounts.",
    "azure": "Per-second billing on most compute; Container Apps and Functions scale to zero. Enterprise Agreements can lower rates.",
    "gcp": "Per-second billing (one-minute minimum on Compute Engine); Cloud Run scales to zero. Sustained-use discounts apply automatically on some VMs.",
}
CONTRACT_NOTE = ("These are public list prices. Negotiated rates, enterprise agreements, reserved instances, credits and free tiers "
                 "(unless a line says otherwise) are not applied; confirm them with your account team before budgeting.")
MCP_HINTS = {
    "aws": "AWS Pricing MCP server (awslabs): call get_pricing with the exact service code (AmazonECS, AWSLambda, AmazonRDS, "
           "AmazonElastiCache, AWSELB, AmazonS3, awswaf, AWSSecretsManager, AmazonCloudWatch), a region and filters.",
    "azure": "Azure Retail Prices API (https://prices.azure.com/api/retail/prices) filtered by serviceName, armRegionName and meterName.",
    "gcp": "Google Cloud Billing Catalog API with a free API key (GOOGLE_API_KEY), or the pricing MCP your agent has.",
}


# ----------------------------------------------------------------------- book
class Book:
    """The price book: every price the estimate used, with its source."""

    def __init__(self, path):
        self.path = Path(path)
        self.entries = {}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            self.entries = data.get("entries", {}) if isinstance(data, dict) else {}
        except (OSError, ValueError):
            self.entries = {}

    @staticmethod
    def key(provider, meter, region):
        return "%s|%s|%s" % (provider, meter, region)

    def get(self, provider, meter, region):
        return self.entries.get(self.key(provider, meter, region))

    def put(self, item):
        self.entries[self.key(item["provider"], item["meter"], item["region"])] = item

    def save(self):
        ps.write_json_atomic(self.path, {"schema": 1, "note": "Prices read by SolsArch. Delete this file or run costs --refresh for fresh prices.",
                                         "entries": self.entries})


# -------------------------------------------------------------------- context
class Ctx:
    """Quantities the estimate is built from, taken from the plan, the design review and the profile."""

    def __init__(self, plan, design, profile):
        cap = design["capacity"]
        size = cap["sizing"]
        self.plan, self.design, self.profile, self.cap = plan, design, profile, cap
        self.mode = plan["compute_model"]
        self.units = [u for u in design["units"] if u["kind"] in ("api", "web", "worker", "service")] or [
            {"name": "app", "kind": "service", "folder": "."}]
        self.single_az = design["single_az"]
        self.replicas = size["replicas_per_unit"]
        self.vcpu = size["unit_vcpu"]
        self.mem = size["unit_memory_gb"]
        self.db_class = size["db_class"]
        self.cache_gb = size["cache_gb"]
        self.storage_gb = size["storage_gb"]
        self.monthly_requests = cap["daily_requests"] * 30.0
        self.avg_rps = cap["daily_requests"] / 86400.0
        self.latency = cap["avg_latency_seconds"]
        self.transfer_gb = cap["bandwidth_gb_per_day"] * 30.0
        self.dau = None
        self.log_gb = max(1.0, self.monthly_requests * 1024 / 1073741824.0)
        self.by_key = {d["key"]: d for d in plan["decisions"]}

    def compute_key(self, unit):
        wanted = {"web": "compute_web", "api": "compute_api", "worker": "compute_worker", "service": "compute_app"}.get(unit["kind"])
        if wanted in self.by_key:
            return wanted
        return next((k for k in self.by_key if k.startswith("compute_")), None)

    def replicas_for(self, unit):
        return 1 if unit["kind"] == "worker" else self.replicas


class FailedClient:
    """Stands in for a price reader that could not start (no key, bad region). Every lookup reports why."""

    def __init__(self, error, region):
        self._error, self.region = error, region

    def __getattr__(self, name):
        def fail(*args, **kwargs):
            raise self._error
        return fail


def spec(provider, meter, region, decision, label, qty, unit, fetch, scales="traffic", ha=False, note=""):
    return {"provider": provider, "meter": meter, "region": region, "decision": decision, "label": label,
            "qty": float(qty), "unit": unit, "fetch": fetch, "scales": scales, "ha": ha, "note": note}


def unpriced_spec(provider, decision, label, reason, how=""):
    return {"provider": provider, "decision": decision, "label": label, "unpriced": reason, "how": how}


AWS_PREFIX_LOCATION = {
    "us-east-1": "US East (N. Virginia)", "us-east-2": "US East (Ohio)", "us-west-1": "US West (N. California)",
    "us-west-2": "US West (Oregon)", "eu-west-1": "EU (Ireland)", "eu-west-2": "EU (London)", "eu-west-3": "EU (Paris)",
    "eu-central-1": "EU (Frankfurt)", "eu-north-1": "EU (Stockholm)", "ap-south-1": "Asia Pacific (Mumbai)",
    "ap-southeast-1": "Asia Pacific (Singapore)", "ap-southeast-2": "Asia Pacific (Sydney)",
    "ap-northeast-1": "Asia Pacific (Tokyo)", "ap-northeast-2": "Asia Pacific (Seoul)", "ca-central-1": "Canada (Central)",
    "sa-east-1": "South America (Sao Paulo)",
}
AWS_DB_CLASS = {"small": "db.t4g.medium", "medium": "db.r6g.large", "large": "db.r6g.2xlarge"}
AWS_ACU = {"small": 2, "medium": 8, "large": 32}
AZ_PG = {"small": ("Azure Database for PostgreSQL Flexible Server Burstable BS Series Compute", "B2S"),
         "medium": ("Azure Database for PostgreSQL Flexible Server General Purpose Ddsv5 Series Compute", "4 vCore"),
         "large": ("Azure Database for PostgreSQL Flexible Server General Purpose Ddsv5 Series Compute", "8 vCore")}


def aws_specs(c, aws):
    r, out, by = aws.region, [], c.by_key
    S = lambda d, name: by[d]["service"] if d in by else name  # noqa: E731

    if c.mode == "server":
        for u in c.units:
            key = c.compute_key(u)
            if not key:
                continue
            reps = c.replicas_for(u)
            for part, qty, ha in (("", 1, False), (" (extra replicas for availability)", reps - 1, True)):
                if qty <= 0:
                    continue
                out.append(spec("aws", "fargate_vcpu_hour", r, key, "Fargate vCPU: %s%s" % (u["name"], part), qty * c.vcpu * HOURS, "vCPU-hours",
                                lambda: aws.find("AmazonECS", r"Fargate-vCPU-Hours:perCPU$", "fargate_vcpu_hour"), ha=ha))
                out.append(spec("aws", "fargate_gb_hour", r, key, "Fargate memory: %s%s" % (u["name"], part), qty * c.mem * HOURS, "GB-hours",
                                lambda: aws.find("AmazonECS", r"Fargate-GB-Hours$", "fargate_gb_hour"), ha=ha))
    else:
        for u in c.units:
            key = c.compute_key(u)
            if not key:
                continue
            share = c.monthly_requests / max(1, len(c.units))
            out.append(spec("aws", "lambda_request", r, key, "Lambda requests: %s" % u["name"], share, "requests",
                            lambda: aws.find("AWSLambda", r"(?:^|-)Request$", "lambda_request", exclude=r"^Global|Edge|Managed|ARM")))
            out.append(spec("aws", "lambda_gb_second", r, key, "Lambda duration: %s" % u["name"], share * c.latency * max(0.5, c.mem), "GB-seconds",
                            lambda: aws.find("AWSLambda", r"Lambda-GB-Second$", "lambda_gb_second", exclude=r"^Global|Edge|Managed|ARM|Storage")))
    if "ingress" in by:
        if c.mode == "server":
            out.append(spec("aws", "alb_hour", r, "ingress", "Application Load Balancer hours", HOURS, "hours",
                            lambda: aws.find("AWSELB", r"^(?:[A-Z0-9]+-)?LoadBalancerUsage$", "alb_hour", {"operation": "LoadBalancing:Application"}), scales="fixed"))
            out.append(spec("aws", "alb_lcu_hour", r, "ingress", "Load balancer capacity units", max(1.0, math.ceil(c.avg_rps / 25.0)) * HOURS, "LCU-hours",
                            lambda: aws.find("AWSELB", r"^(?:[A-Z0-9]+-)?LCUUsage$", "alb_lcu_hour", {"operation": "LoadBalancing:Application"})))
        else:
            out.append(spec("aws", "apigw_request", r, "ingress", "API Gateway requests", c.monthly_requests, "requests",
                            lambda: aws.find("AmazonApiGateway", r"ApiGatewayRequest$", "apigw_request")))
    if "edge" in by:
        out.append(spec("aws", "waf_web_acl", r, "edge", "WAF web ACL", 1, "web ACL-months",
                        lambda: aws.find("awswaf", r"(?:^|-)WebACLV2$", "waf_web_acl", exclude="Shield"), scales="fixed"))
        out.append(spec("aws", "waf_rule", r, "edge", "WAF rules (5 managed or custom)", 5, "rule-months",
                        lambda: aws.find("awswaf", r"(?:^|-)RuleV2$", "waf_rule", exclude="Shield"), scales="fixed"))
        out.append(spec("aws", "waf_request", r, "edge", "WAF requests inspected", c.monthly_requests, "requests",
                        lambda: aws.find("awswaf", r"(?:^|-)RequestV2-48KB$", "waf_request", exclude="Shield")))
    db_key = "database" if "database" in by else ("database_mysql" if "database_mysql" in by else None)
    if db_key:
        engine = "Aurora PostgreSQL" if db_key == "database" else "Aurora MySQL"
        if c.mode == "serverless":
            out.append(spec("aws", "aurora_acu_" + engine[-5:].lower(), r, db_key, "Aurora Serverless v2 capacity (%d ACU average)" % AWS_ACU[c.db_class],
                            AWS_ACU[c.db_class] * HOURS, "ACU-hours",
                            lambda: aws.find("AmazonRDS", r"Aurora:ServerlessUsage$", "aurora_acu", {"databaseEngine": engine})))
        else:
            cls = AWS_DB_CLASS[c.db_class]
            for part, qty, ha in (("writer", 1, False), ("reader for high availability", 0 if c.single_az else 1, True)):
                if qty:
                    out.append(spec("aws", "aurora_%s_%s" % (engine[-5:].lower(), cls), r, db_key, "Aurora %s %s: %s" % (engine.split()[1], cls, part), HOURS, "instance-hours",
                                    lambda cls=cls: aws.find("AmazonRDS", r"^InstanceUsage:" + re.escape(cls) + "$", "aurora_" + cls,
                                                             {"databaseEngine": engine}), scales="fixed", ha=ha))
        out.append(spec("aws", "aurora_storage_" + engine[-5:].lower(), r, db_key, "Aurora storage", c.storage_gb, "GB-months",
                        lambda: aws.find("AmazonRDS", r"Aurora:StorageUsage$", "aurora_storage", {"databaseEngine": engine}), scales="fixed",
                        note="I/O request charges are not estimated; Aurora I/O-Optimized avoids them."))
    if "database_mongo" in by:
        for part, qty, ha in (("primary", 1, False), ("replica for high availability", 0 if c.single_az else 1, True)):
            if qty:
                out.append(spec("aws", "docdb_t4g_medium", r, "database_mongo", "DocumentDB db.t4g.medium: " + part, HOURS, "instance-hours",
                                lambda: aws.find("AmazonDocDB", r"^InstanceUsage:db\.t4g\.medium$", "docdb_t4g_medium"), scales="fixed", ha=ha))
    if "cache" in by:
        node = "cache.t4g.small" if c.cache_gb <= 1.3 else "cache.t4g.medium"
        for part, qty, ha in (("primary", 1, False), ("replica for high availability", 0 if c.single_az else 1, True)):
            if qty:
                out.append(spec("aws", "elasticache_" + node, r, "cache", "ElastiCache %s: %s" % (node, part), HOURS, "node-hours",
                                lambda node=node: aws.find("AmazonElastiCache", r"^NodeUsage:" + re.escape(node) + "$", "elasticache_" + node,
                                                           {"cacheEngine": "Redis"}), scales="fixed", ha=ha))
    if "queue_rabbitmq" in by:
        out.append(spec("aws", "mq_rabbit_t3_micro", r, "queue_rabbitmq", "Amazon MQ RabbitMQ mq.t3.micro (single instance)", HOURS, "broker-hours",
                        lambda: aws.find("AmazonMQ", r"RabbitMQ-Single-InstanceUsage:mq\.t3\.micro$", "mq_rabbit_t3_micro"), scales="fixed",
                        note="A clustered broker for high availability costs more than this single instance."))
    if "queue_kafka" in by:
        out.append(spec("aws", "msk_t3_small", r, "queue_kafka", "MSK kafka.t3.small brokers (3)", 3 * HOURS, "broker-hours",
                        lambda: aws.find("AmazonMSK", r"Kafka\.t3\.small$", "msk_t3_small"), scales="fixed",
                        note="Broker storage is not estimated."))
    for key in ("storage", "static_site"):
        if key in by:
            out.append(spec("aws", "s3_standard_gb", r, key, "S3 Standard storage", c.storage_gb if key == "storage" else 5, "GB-months",
                            lambda: aws.find("AmazonS3", r"(?:^|-)TimedStorage-ByteHrs$", "s3_standard_gb", {"storageClass": "General Purpose"}), scales="fixed"))
    if "identity" in by:
        out.append(spec("aws", "cognito_mau", r, "identity", "Cognito monthly active users (assumed 3 per daily user)", c.cap["daily_requests"] / 40.0 * 3.0, "MAU",
                        lambda: aws.find("AmazonCognito", r"Global-CognitoUserPoolMAU$", "cognito_mau")))
    if "secrets" in by:
        out.append(spec("aws", "secrets_manager_secret", r, "secrets", "Secrets Manager secrets (assumed 5)", 5, "secret-months",
                        lambda: aws.find("AWSSecretsManager", r"AWSSecretsManager-Secrets$", "secrets_manager_secret"), scales="fixed"))
    if "registry" in by:
        out.append(spec("aws", "ecr_gb_month", r, "registry", "ECR image storage (assumed 5 GB)", 5, "GB-months",
                        lambda: aws.find("AmazonECR", r"(?:^|-)TimedStorage-ByteHrs$", "ecr_gb_month"), scales="fixed"))
    if "observability" in by:
        out.append(spec("aws", "cloudwatch_logs_gb", r, "observability", "CloudWatch log ingestion", c.log_gb, "GB",
                        lambda: aws.find("AmazonCloudWatch", r"(?:^|-)DataProcessing-Bytes$", "cloudwatch_logs_gb", {"operation": "PutLogEvents"})))
    if "egress" in by:
        nat = 1 if c.single_az else 2
        out.append(spec("aws", "nat_gateway_hour", r, "egress", "NAT gateway hours (%d)" % nat, nat * HOURS, "hours",
                        lambda: ps.reference("aws", "nat_gateway_hour", r), scales="fixed", ha=False))
        out.append(spec("aws", "nat_gateway_gb", r, "egress", "NAT gateway data processed", c.transfer_gb * 0.3, "GB",
                        lambda: ps.reference("aws", "nat_gateway_gb", r), note="Assumes 30% of response traffic crosses the NAT gateway."))
    location = AWS_PREFIX_LOCATION.get(r)
    if location and ("ingress" in by or "static_site" in by):
        out.append(spec("aws", "data_out_gb", r, "ingress" if "ingress" in by else "static_site", "Data transfer out to the internet", c.transfer_gb, "GB",
                        lambda: aws.find("AWSDataTransfer", r"(?:^|-)DataTransfer-Out-Bytes$", "data_out_gb",
                                         {"fromLocation": location, "toLocation": "External"}), scales="transfer"))
    out.append(unpriced_spec("aws", "edge", "CloudFront requests and transfer", "CloudFront request and transfer prices are not in the regional price file.",
                             "https://aws.amazon.com/cloudfront/pricing/") if "edge" in by or "static_site" in by else None)
    for key, why in (("gpu", "EC2 GPU instance prices are in a very large file; price them with the AWS Pricing Calculator or the Pricing MCP."),
                     ("vector_service", "OpenSearch prices are not read yet; use the AWS Pricing Calculator."),
                     ("search", "OpenSearch prices are not read yet; use the AWS Pricing Calculator."),
                     ("warehouse", "Redshift prices are not read yet; use the AWS Pricing Calculator."),
                     ("managed_ai", "Bedrock is priced per model and token; see https://aws.amazon.com/bedrock/pricing/.")):
        if key in by:
            out.append(unpriced_spec("aws", key, by[key]["service"], why, MCP_HINTS["aws"]))
    return [s for s in out if s]


def azure_specs(c, az):
    r, out, by = az.region, [], c.by_key
    if c.mode == "server":
        for u in c.units:
            key = c.compute_key(u)
            if not key:
                continue
            reps = c.replicas_for(u)
            for part, qty, ha in (("", 1, False), (" (extra replicas for availability)", reps - 1, True)):
                if qty <= 0:
                    continue
                out.append(spec("azure", "aca_vcpu_second", r, key, "Container Apps vCPU: %s%s" % (u["name"], part), qty * c.vcpu * SECONDS, "vCPU-seconds",
                                lambda: az.find("aca_vcpu_second", "Azure Container Apps", meter_name="Standard vCPU Active Usage"), ha=ha))
                out.append(spec("azure", "aca_mem_second", r, key, "Container Apps memory: %s%s" % (u["name"], part), qty * c.mem * SECONDS, "GiB-seconds",
                                lambda: az.find("aca_mem_second", "Azure Container Apps", meter_name="Standard Memory Active Usage"), ha=ha))
        out.append(spec("azure", "aca_requests", r, c.compute_key(c.units[0]) or "compute_api", "Container Apps requests", c.monthly_requests / 1e6, "million requests",
                        lambda: az.find("aca_requests", "Azure Container Apps", meter_name="Standard Requests")))
    else:
        for u in c.units:
            key = c.compute_key(u)
            if not key:
                continue
            share = c.monthly_requests / max(1, len(c.units))
            out.append(spec("azure", "functions_executions", r, key, "Functions executions: %s" % u["name"], share / 10.0, "10-execution blocks",
                            lambda: az.find("functions_executions", "Functions", meter_name="Standard Total Executions", product="Functions", sku="Standard")))
            out.append(spec("azure", "functions_gb_second", r, key, "Functions execution time: %s" % u["name"], share * c.latency * max(0.5, c.mem), "GB-seconds",
                            lambda: az.find("functions_gb_second", "Functions", meter_name="Standard Execution Time", product="Functions", sku="Standard")))
    if "ingress" in by and c.mode == "server":
        out.append(spec("azure", "appgw_fixed", r, "ingress", "Application Gateway (WAF v2) fixed cost", HOURS, "hours",
                        lambda: az.find("appgw_fixed", "Application Gateway", meter_name="Standard Fixed Cost", product="Application Gateway WAF v2"), scales="fixed"))
        out.append(spec("azure", "appgw_capacity", r, "ingress", "Application Gateway capacity units", max(1.0, math.ceil(c.avg_rps / 50.0)) * HOURS, "capacity-unit-hours",
                        lambda: az.find("appgw_capacity", "Application Gateway", meter_name="Standard Capacity Units", product="Application Gateway WAF v2")))
    if "edge" in by:
        zones = ["Zone 1", "Global", "Zone 3"]
        out.append(spec("azure", "frontdoor_policy", r, "edge", "Front Door Standard base", 1, "month",
                        lambda: az.find("frontdoor_policy", "Azure Front Door Service", meter_name="Standard Policy", region=zones), scales="fixed"))
        out.append(spec("azure", "frontdoor_requests", r, "edge", "Front Door requests", c.monthly_requests / 10000.0, "10K requests",
                        lambda: az.find("frontdoor_requests", "Azure Front Door Service", meter_name="Standard Requests", region=zones)))
    db_key = "database" if "database" in by else None
    if db_key:
        product, sku = AZ_PG[c.db_class]
        for part, qty, ha in (("primary", 1, False), ("zone-redundant standby", 0 if c.single_az else 1, True)):
            if qty:
                out.append(spec("azure", "pg_" + sku.replace(" ", "").lower(), r, db_key, "PostgreSQL Flexible Server %s: %s" % (sku, part), HOURS, "hours",
                                lambda product=product, sku=sku: az.find("pg_" + sku, "Azure Database for PostgreSQL", product=product, sku=sku, unit="Hour"),
                                scales="fixed", ha=ha))
        out.append(spec("azure", "pg_storage", r, db_key, "PostgreSQL storage", c.storage_gb, "GB-months",
                        lambda: az.find("pg_storage", "Azure Database for PostgreSQL", meter_name="Storage Data Stored"), scales="fixed"))
    if "cache" in by:
        sku = "C1" if c.cache_gb <= 1 else "C2"
        out.append(spec("azure", "redis_standard_" + sku, r, "cache", "Azure Cache for Redis Standard %s (primary and replica)" % sku, HOURS, "hours",
                        lambda: az.find("redis_standard_" + sku, "Redis Cache", product="Azure Redis Cache Standard", sku=sku, meter_name=sku + " Cache"),
                        scales="fixed", note="Standard includes a replica, so it is the high-availability tier."))
    if "storage" in by or "static_site" in by:
        out.append(spec("azure", "blob_hot_gb", r, "storage" if "storage" in by else "static_site", "Blob Storage (Hot, LRS)", c.storage_gb if "storage" in by else 5, "GB-months",
                        lambda: az.find("blob_hot_gb", "Storage", meter_name="Hot LRS Data Stored", product="Blob Storage"), scales="fixed"))
    if "secrets" in by:
        out.append(spec("azure", "keyvault_ops", r, "secrets", "Key Vault operations (assumed 100,000)", 10, "10K operations",
                        lambda: az.find("keyvault_ops", "Key Vault", meter_name="Operations", product="Key Vault", sku="Standard"), scales="fixed"))
    if "registry" in by:
        out.append(spec("azure", "acr_basic_day", r, "registry", "Container Registry Basic", 30, "days",
                        lambda: az.find("acr_basic_day", "Container Registry", meter_name="Basic Registry Unit", sku="Basic"), scales="fixed"))
    if "observability" in by:
        out.append(spec("azure", "loganalytics_gb", r, "observability", "Log Analytics ingestion", c.log_gb, "GB",
                        lambda: az.find("loganalytics_gb", "Log Analytics", meter_name="Analytics Logs Data Ingestion")))
    if "ingress" in by or "static_site" in by:
        out.append(spec("azure", "bandwidth_out_gb", r, "ingress" if "ingress" in by else "static_site", "Data transfer out to the internet", c.transfer_gb, "GB",
                        lambda: az.find("bandwidth_out_gb", "Bandwidth", meter_name="Standard Data Transfer Out", product="Bandwidth - Routing Preference: Internet"),
                        scales="transfer"))
    for key, why in (("egress", "NAT Gateway prices are not in the retail price list for this region; see https://azure.microsoft.com/pricing/details/azure-nat-gateway/."),
                     ("database_mongo", "Cosmos DB for MongoDB is priced per vCore or request unit; use the Azure pricing calculator."),
                     ("identity", "Microsoft Entra External ID is priced per monthly active user; see https://learn.microsoft.com/entra/external-id/external-identities-pricing."),
                     ("gpu", "N-series VM prices depend on the series; use the Azure pricing calculator."),
                     ("managed_ai", "Azure OpenAI is priced per model and token."),
                     ("search", "Azure AI Search is priced per search unit; use the Azure pricing calculator."),
                     ("vector_service", "Azure AI Search is priced per search unit; use the Azure pricing calculator."),
                     ("queue_kafka", "Event Hubs is priced per throughput unit; use the Azure pricing calculator."),
                     ("queue_rabbitmq", "CloudAMQP is billed by CloudAMQP; see its pricing page."),
                     ("warehouse", "Synapse is priced per data warehouse unit; use the Azure pricing calculator.")):
        if key in by:
            out.append(unpriced_spec("azure", key, by[key]["service"], why, MCP_HINTS["azure"]))
    return out


def gcp_specs(c, gcp):
    """Google Cloud lines. The SKU descriptions are matched by pattern, so each line shows the SKU it found."""
    r, out, by = gcp.region, [], c.by_key
    pat = lambda p: p  # noqa: E731
    for u in c.units:
        key = c.compute_key(u)
        if not key:
            continue
        reps = c.replicas_for(u)
        out.append(spec("gcp", "cloudrun_vcpu_second", r, key, "Cloud Run vCPU: %s" % u["name"], reps * c.vcpu * SECONDS, "vCPU-seconds",
                        lambda: gcp.find("Cloud Run", pat(r"CPU.*(Allocation|Allocated)|Services CPU"), "cloudrun_vcpu_second", exclude="Jobs|Idle|Tier 2|Committed|Gen2 Execution"),
                        scales="traffic"))
        out.append(spec("gcp", "cloudrun_mem_second", r, key, "Cloud Run memory: %s" % u["name"], reps * c.mem * SECONDS, "GiB-seconds",
                        lambda: gcp.find("Cloud Run", pat(r"Memory.*(Allocation|Allocated)|Services Memory"), "cloudrun_mem_second", exclude="Jobs|Idle|Tier 2|Committed")))
    if "database" in by or "database_mysql" in by:
        key = "database" if "database" in by else "database_mysql"
        vcpu = {"small": 1, "medium": 2, "large": 8}[c.db_class]
        mem = {"small": 3.75, "medium": 7.5, "large": 30}[c.db_class]
        engine = "PostgreSQL" if key == "database" else "MySQL"
        for part, qty, ha in (("primary", 1, False), ("high-availability standby", 0 if c.single_az else 1, True)):
            if qty:
                out.append(spec("gcp", "cloudsql_vcpu_" + engine.lower(), r, key, "Cloud SQL %s vCPU (%d): %s" % (engine, vcpu, part), qty * vcpu * HOURS, "vCPU-hours",
                                lambda engine=engine: gcp.find("Cloud SQL", r"%s.*Standalone.*vCPU|%s.*vCPU.*(Zonal|standalone)" % (engine, engine), "cloudsql_vcpu_" + engine.lower(), exclude="Regional|Committed|Enterprise Plus"),
                                scales="fixed", ha=ha))
                out.append(spec("gcp", "cloudsql_ram_" + engine.lower(), r, key, "Cloud SQL %s memory (%.1f GB): %s" % (engine, mem, part), qty * mem * HOURS, "GiB-hours",
                                lambda engine=engine: gcp.find("Cloud SQL", r"%s.*Standalone.*RAM|%s.*RAM.*(Zonal|standalone)" % (engine, engine), "cloudsql_ram_" + engine.lower(), exclude="Regional|Committed|Enterprise Plus"),
                                scales="fixed", ha=ha))
        out.append(spec("gcp", "cloudsql_storage_ssd", r, key, "Cloud SQL SSD storage", c.storage_gb, "GiB-months",
                        lambda: gcp.find("Cloud SQL", r"SSD storage.*(Zonal|Standalone)|Storage.*SSD", "cloudsql_storage_ssd", exclude="Regional|Backup"), scales="fixed"))
    if "storage" in by or "static_site" in by:
        out.append(spec("gcp", "gcs_standard_gb", r, "storage" if "storage" in by else "static_site", "Cloud Storage Standard", c.storage_gb if "storage" in by else 5, "GiB-months",
                        lambda: gcp.find("Cloud Storage", r"^Standard Storage.*(Iowa|Region|US)", "gcs_standard_gb", exclude="Dual|Multi|Coldline|Nearline|Archive|Operations"), scales="fixed"))
    for key, why in (("cache", "Memorystore is priced per GiB of capacity and tier; use the Google Cloud pricing calculator."),
                     ("ingress", "Cloud Load Balancing is priced per forwarding rule and data processed; use the Google Cloud pricing calculator."),
                     ("edge", "Cloud CDN and Cloud Armor are priced per policy, request and GiB; use the Google Cloud pricing calculator."),
                     ("egress", "Cloud NAT is priced per gateway hour and GiB processed; use the Google Cloud pricing calculator."),
                     ("secrets", "Secret Manager is priced per active secret version and access; use the Google Cloud pricing calculator."),
                     ("observability", "Cloud Logging is priced per GiB ingested beyond the free allocation; use the Google Cloud pricing calculator."),
                     ("registry", "Artifact Registry is priced per GB stored; use the Google Cloud pricing calculator."),
                     ("identity", "Identity Platform is priced per monthly active user; use the Google Cloud pricing calculator."),
                     ("queue_kafka", "Managed Service for Apache Kafka is priced per vCPU and GiB; use the Google Cloud pricing calculator."),
                     ("database_mongo", "MongoDB Atlas is billed by MongoDB; see https://www.mongodb.com/pricing."),
                     ("gpu", "GPU prices depend on the accelerator; use the Google Cloud pricing calculator."),
                     ("managed_ai", "Vertex AI is priced per model and token."),
                     ("search", "Elastic Cloud is billed by Elastic."), ("vector_service", "Vertex AI Vector Search is priced per node hour."),
                     ("queue_rabbitmq", "CloudAMQP is billed by CloudAMQP."), ("warehouse", "BigQuery is priced per TiB scanned or per slot; see https://cloud.google.com/bigquery/pricing.")):
        if key in by:
            out.append(unpriced_spec("gcp", key, by[key]["service"], why, MCP_HINTS["gcp"]))
    return out


def vm_nodes(c, vcpu_per_node=2):
    total = sum(c.replicas_for(u) * c.vcpu for u in c.units)
    nodes = max(1 if c.single_az else 2, int(math.ceil(total / vcpu_per_node)))
    return nodes


def vm_specs(c, provider, client, db_support=None):
    """Providers that sell virtual machines: compute is priced as the number of 2 vCPU / 4 GB nodes the units need."""
    out, by = [], c.by_key
    nodes = vm_nodes(c)
    first = next((k for k in by if k.startswith("compute_")), None)
    if first:
        out.append(spec(provider, "vm_2vcpu_4gb", "global", first, "Servers (2 vCPU, 4 GB) x %d for %d unit(s)" % (nodes, len(c.units)), nodes, "server-months",
                        lambda: client.vm("vm_2vcpu_4gb", 2, 4), scales="fixed"))
    if db_support and "database" in by:
        vcpu = {"small": 1, "medium": 2, "large": 4}[c.db_class]
        nodes_db = 1 if c.single_az else 3
        out.append(spec(provider, "db_pg_%dvcpu_%dn" % (vcpu, nodes_db), "global", "database", "Managed PostgreSQL (%d vCPU, %d node(s))" % (vcpu, nodes_db), 1, "cluster-months",
                        lambda: db_support.database("db_pg", "postgresql", vcpu, nodes_db), scales="fixed", ha=False))
    if db_support and "database_mysql" in by:
        vcpu = {"small": 1, "medium": 2, "large": 4}[c.db_class]
        nodes_db = 1 if c.single_az else 3
        out.append(spec(provider, "db_mysql_%dvcpu_%dn" % (vcpu, nodes_db), "global", "database_mysql", "Managed MySQL (%d vCPU, %d node(s))" % (vcpu, nodes_db), 1, "cluster-months",
                        lambda: db_support.database("db_mysql", "mysql", vcpu, nodes_db), scales="fixed"))
    return out


def reason_unpriced(provider, name, page):
    return ("%s publishes no public price API that SolsArch reads. Open %s, or give the price with: solsarch.py costs add "
            "--provider %s ..." % (name, page, provider))


# ------------------------------------------------------------------ pricing run
class Pricer:
    def __init__(self, plan, design, profile, book, fetcher, online=True, refresh=False, registry=None):
        self.included_items = []
        self.plan, self.design, self.profile, self.book = plan, design, profile, book
        self.fetcher, self.online, self.refresh = fetcher, online, refresh
        self.ctx = Ctx(plan, design, profile)
        self.registry = registry or {}
        self.reasons = {}
        self.fetched = 0

    def region_for(self, provider):
        return self.profile.get("region") or DEFAULT_REGION.get(provider, "global")

    def client(self, provider):
        region = self.region_for(provider)
        import os
        if provider == "aws":
            return ps.AwsPrices(self.fetcher, region)
        if provider == "azure":
            return ps.AzurePrices(self.fetcher, region)
        if provider == "gcp":
            return ps.GcpPrices(self.fetcher, ps.gcp_key(), region)
        if provider == "linode":
            return ps.LinodePrices(self.fetcher)
        if provider == "vultr":
            return ps.VultrPrices(self.fetcher)
        if provider == "scaleway":
            return ps.ScalewayPrices(self.fetcher)
        if provider == "hetzner":
            return ps.HetznerPrices(self.fetcher, os.environ.get("HCLOUD_TOKEN", ""))
        if provider == "digitalocean":
            return ps.DigitalOceanPrices(self.fetcher, os.environ.get("DIGITALOCEAN_TOKEN", ""))
        raise ps.PriceError("no live price source for %s" % provider)

    def specs(self):
        by_provider = {}
        for d in self.plan["decisions"]:
            by_provider.setdefault(d.get("provider") or self.plan["cloud"], []).append(d)
        out = []
        for provider in sorted(by_provider, key=lambda p: (p != self.plan["cloud"], p)):
            own = by_provider[provider]
            sub = Ctx(dict(self.plan, decisions=own), self.design, self.profile)
            sub.by_key = {d["key"]: d for d in own}
            if provider in ("aws", "azure", "gcp", "linode", "vultr", "scaleway", "hetzner", "digitalocean"):
                try:
                    try:
                        client = self.client(provider)
                    except ps.PriceError as error:
                        client = FailedClient(error, self.region_for(provider))  # lines stay visible and can take supplied prices
                    if provider == "aws":
                        out += aws_specs(sub, client)
                    elif provider == "azure":
                        out += azure_specs(sub, client)
                    elif provider == "gcp":
                        out += gcp_specs(sub, client)
                    elif provider == "linode":
                        out += vm_specs(sub, provider, client, client)
                    else:
                        out += vm_specs(sub, provider, client)
                    covered = {s["decision"] for s in out if s["provider"] == provider}
                    for d in own:
                        if d["key"] not in covered and not d.get("builtin"):
                            out.append(unpriced_spec(provider, d["key"], d["service"], reason_unpriced(
                                provider, self.registry.get(provider, {}).get("name", provider),
                                self.registry.get(provider, {}).get("pricing_page", "the provider's pricing page")), ""))
                except ps.PriceError as error:
                    for d in own:
                        out.append(unpriced_spec(provider, d["key"], d["service"], str(error), ""))
            else:
                info = self.registry.get(provider, {})
                for d in own:
                    if d.get("builtin"):
                        continue
                    out.append(unpriced_spec(provider, d["key"], d["service"], reason_unpriced(
                        provider, info.get("name", provider), info.get("pricing_page", "the provider's pricing page")), ""))
        return out

    def resolve(self, s):
        """The price entry for a line: from the book, supplied, or read now. Returns (entry, reason)."""
        book_entry = None if self.refresh else self.book.get(s["provider"], s["meter"], s["region"])
        supplied_entry = self.book.get(s["provider"], s["meter"], "global")
        if book_entry:
            return book_entry, None
        if supplied_entry and supplied_entry.get("origin") == "supplied":
            return supplied_entry, None
        if not self.online:
            return None, "not in the price book and the run is offline"
        try:
            item = s["fetch"]()
        except ps.PriceError as error:
            return None, str(error)
        except (KeyError, IndexError, TypeError, ValueError) as error:
            return None, "the vendor's price data had an unexpected shape (%s)" % error
        item = dict(item, provider=s["provider"], meter=s["meter"], region=s["region"])  # the line's own key, so the book finds it again
        self.book.put(item)
        self.fetched += 1
        return item, None

    def included(self, specs):
        """Decisions that cost nothing on their own: built-in platform services, things that run on servers priced elsewhere, free networks."""
        covered = {(s["provider"], s["decision"]) for s in specs}
        out = []
        for d in self.plan["decisions"]:
            provider = d.get("provider") or self.plan["cloud"]
            if (provider, d["key"]) in covered:
                continue
            note = None
            if d.get("builtin"):
                note = "Included in the platform at no separate charge."
            elif d.get("selfmanaged"):
                note = "Runs on the servers or nodes priced above; you run, patch and back it up yourself."
            elif d["key"] == "network" and provider in ("aws", "azure", "gcp", "oci", "ibm", "alibaba", "huawei", "tencent"):
                note = "The private network has no separate charge; costs come from the services inside it."
            elif d["key"] == "vector" and d["short"] == "pgvector":
                note = "pgvector runs inside the database; its storage is part of the database lines."
            elif d["key"].startswith("compute_") and provider in ("linode", "vultr", "scaleway", "hetzner", "digitalocean") and \
                    any(sp["decision"].startswith("compute_") and sp["provider"] == provider for sp in specs if "unpriced" not in sp):
                note = "Runs on the servers priced in the first compute line."
            if note:
                out.append({"decision": d["key"], "provider": provider, "service": d["service"], "note": note})
        return out

    def run(self):
        lines, unpriced = [], []
        all_specs = self.specs()
        included = self.included([x for x in all_specs if "unpriced" not in x])
        skip = {(i["provider"], i["decision"]) for i in included}
        for s in all_specs:
            if (s["provider"], s["decision"]) in skip and "unpriced" in s:
                continue
            if "unpriced" in s:
                unpriced.append({"decision": s["decision"], "provider": s["provider"], "service": s["label"], "reason": s["unpriced"],
                                 "how": s.get("how") or ""})
                continue
            item, reason = self.resolve(s)
            if item is None:
                unpriced.append({"decision": s["decision"], "provider": s["provider"], "service": s["label"], "reason": reason,
                                 "meter": s["meter"], "region": s["region"], "unit": s["unit"], "quantity": round(s["qty"], 3),
                                 "how": "solsarch.py costs add --provider %s --meter %s --unit-price PRICE --unit '%s' --url URL --quote QUOTE"
                                        % (s["provider"], s["meter"], s["unit"])})
                continue
            cost = ps.tier_cost(item["tiers"], s["qty"])
            lines.append({"decision": s["decision"], "provider": s["provider"], "label": s["label"], "quantity": round(s["qty"], 4),
                          "unit": s["unit"], "unit_price": round(ps.first_price(item), 8), "monthly": round(cost, 4),
                          "scales": s["scales"], "ha": s["ha"], "note": s["note"], "meter": s["meter"], "region": s["region"],
                          "price_unit": item.get("unit"), "origin": item["origin"], "source": item["source"],
                          "retrieved": item["retrieved"], "sku": item.get("sku"), "sku_description": item.get("description")})
        self.included_items = included
        return lines, unpriced


# ------------------------------------------------------------------- analysis
def halton(index, base):
    f, r, i = 1.0, 0.0, index
    while i > 0:
        f /= base
        r += f * (i % base)
        i //= base
    return r


def _beta_free_quantile(u):
    return u


def cost_range(lines, confidence_low, points=2048):
    """P10, P50 and P90 of the monthly total, from a fixed-sequence simulation (no randomness).

    Traffic multiplies the lines that grow with use (lognormal, wide when traffic is an assumption) and data
    transfer varies between half and double. Fixed lines do not move."""
    fixed = sum(l["monthly"] for l in lines if l["scales"] == "fixed")
    traffic = sum(l["monthly"] for l in lines if l["scales"] == "traffic")
    transfer = sum(l["monthly"] for l in lines if l["scales"] == "transfer")
    sigma = 0.35 if confidence_low else 0.12
    nd = statistics.NormalDist()
    totals, xs, ys = [], [], []
    for i in range(1, points + 1):
        t = math.exp(sigma * nd.inv_cdf(min(max(halton(i, 2), 1e-6), 1 - 1e-6)))
        x = 0.5 + 1.5 * halton(i, 3)
        totals.append(fixed + traffic * t + transfer * x)
        xs.append(t)
        ys.append(x)
    ordered = sorted(totals)
    pick = lambda q: ordered[min(len(ordered) - 1, int(q * len(ordered)))]  # noqa: E731

    def corr(a, b):
        ma, mb = sum(a) / len(a), sum(b) / len(b)
        num = sum((p - ma) * (q - mb) for p, q in zip(a, b))
        den = math.sqrt(sum((p - ma) ** 2 for p in a) * sum((q - mb) ** 2 for q in b))
        return abs(num / den) if den else 0.0

    sens = {"traffic volume": round(corr(xs, totals), 3), "data transfer": round(corr(ys, totals), 3)}
    driver = max(sens, key=lambda k: (sens[k], k))
    return {"p10": round(pick(0.10), 2), "p50": round(pick(0.50), 2), "p90": round(pick(0.90), 2), "points": points,
            "method": "fixed-sequence simulation (Halton points), no randomness, so the range repeats exactly",
            "sensitivity": sens, "top_driver": driver if sum(sens.values()) else None,
            "assumptions": ["traffic: lognormal spread %.2f around the estimate" % sigma, "data transfer: between 0.5x and 2x"]}


def projection(lines, tier, months=12):
    growth = GROWTH_PER_MONTH.get(tier, 0.05)
    fixed = sum(l["monthly"] for l in lines if l["scales"] == "fixed")
    usage = sum(l["monthly"] for l in lines if l["scales"] != "fixed")
    rows, cumulative = [], 0.0
    for m in range(1, months + 1):
        total = fixed + usage * (1 + growth) ** (m - 1)
        cumulative += total
        rows.append({"month": m, "monthly": round(total, 2), "cumulative": round(cumulative, 2)})
    return {"growth_per_month": growth, "assumption": "usage-driven lines grow %.0f%% a month for the %s tier; fixed lines stay flat" % (growth * 100, tier),
            "months": rows, "year_one_total": round(cumulative, 2), "month_12_run_rate_annual": round(rows[-1]["monthly"] * 12, 2)}


def commitments(lines, provider):
    """Indicative discounts on the lines that commitments apply to. The percentages are planning constants, not quotes."""
    compute = [l for l in lines if re.match(r"compute_", l["decision"]) and l["scales"] != "fixed" or l["decision"].startswith("compute_") and l["provider"] == provider]
    base = sum(l["monthly"] for l in lines if l["decision"].startswith("compute_") and l["provider"] == provider)
    data = sum(l["monthly"] for l in lines if l["decision"] in ("database", "database_mysql", "database_mongo", "cache") and l["provider"] == provider)
    if provider not in ("aws", "azure", "gcp") or base + data <= 0:
        return None
    names = {"aws": ("Savings Plans", "Reserved instances"), "azure": ("Savings Plans", "Reserved instances"),
             "gcp": ("Spend-based committed use", "Resource-based committed use")}[provider]
    one = RESERVED_GUIDE["gcp_committed_1yr"] if provider == "gcp" else RESERVED_GUIDE["savings_plan_1yr"]
    three = RESERVED_GUIDE["gcp_committed_3yr"] if provider == "gcp" else RESERVED_GUIDE["savings_plan_3yr"]
    return {"flexible": names[0], "fixed": names[1], "note": "Indicative planning discounts, not vendor quotes. Confirm the rate for your region and term.",
            "compute_monthly": round(base, 2), "data_monthly": round(data, 2),
            "flexible_1yr_monthly": round(base * (1 - one) , 2), "flexible_3yr_monthly": round(base * (1 - three), 2),
            "reserved_1yr_data_monthly": round(data * (1 - RESERVED_GUIDE["reserved_1yr"]), 2),
            "reserved_3yr_data_monthly": round(data * (1 - RESERVED_GUIDE["reserved_3yr"]), 2),
            "discounts": {"flexible_1yr": one, "flexible_3yr": three, "reserved_1yr": RESERVED_GUIDE["reserved_1yr"], "reserved_3yr": RESERVED_GUIDE["reserved_3yr"]}}


def optimizations(plan, design, lines, total):
    out = []
    provider = plan["cloud"]
    comp = sum(l["monthly"] for l in lines if l["decision"].startswith("compute_"))
    workers = sum(l["monthly"] for l in lines if l["decision"] == "compute_worker")
    nat = sum(l["monthly"] for l in lines if l["decision"] == "egress")
    logs = sum(l["monthly"] for l in lines if l["decision"] == "observability")
    ha = sum(l["monthly"] for l in lines if l["ha"])
    com = commitments(lines, provider)
    if com and comp > 0:
        save = comp - com["flexible_1yr_monthly"]
        out.append({"id": "OPT-01", "title": "Commit to steady compute with %s" % com["flexible"], "category": "commitment",
                    "monthly_savings": round(save, 2), "basis": "%.0f%% indicative discount on %s of compute" % (com["discounts"]["flexible_1yr"] * 100, "$%.2f" % comp),
                    "risk": "A commitment is a spend you pay whether or not you use it; start after two months of stable usage.",
                    "payback": "Immediate on the committed share; no upfront cost for a no-upfront plan."})
    if workers > 0 and provider in ("aws", "azure", "gcp"):
        out.append({"id": "OPT-02", "title": "Run background workers on interruptible capacity (spot) with a 20% on-demand fallback",
                    "category": "spot", "monthly_savings": round(workers * RESERVED_GUIDE["spot"] * 0.8, 2),
                    "basis": "%.0f%% indicative spot discount on 80%% of worker compute, 20%% kept on-demand" % (RESERVED_GUIDE["spot"] * 100),
                    "risk": "Workers must tolerate interruption and retry. Never use spot for primary databases, stateful services or payment processing.",
                    "payback": "Immediate."})
    if nat > 0 and not design["single_az"]:
        out.append({"id": "OPT-03", "title": "Share one outbound gateway across services in non-production environments", "category": "network",
                    "monthly_savings": round(nat / 2.0, 2), "basis": "one NAT gateway instead of two outside production",
                    "risk": "A single gateway is a single point of failure; keep two in production.", "payback": "Immediate."})
    if ha > 0:
        out.append({"id": "OPT-04", "title": "Use one availability zone outside production", "category": "right-sizing",
                    "monthly_savings": round(ha, 2), "basis": "the standby database, replica nodes and extra replicas marked as availability cost",
                    "risk": "Do not do this in production: it removes the failover the design relies on.", "payback": "Immediate."})
    if logs > 5:
        out.append({"id": "OPT-05", "title": "Sample or filter debug logs before ingestion", "category": "observability",
                    "monthly_savings": round(logs * 0.4, 2), "basis": "40% fewer ingested log bytes",
                    "risk": "Keep error and audit logs unsampled.", "payback": "Immediate."})
    if plan["compute_model"] == "server" and design["archetype"]["name"] in ("monolith", "greenfield", "microservice") and comp > 0 and \
            design["capacity"]["tier"] == "prototype":
        out.append({"id": "OPT-06", "title": "Scale a prototype to one replica per unit until real traffic arrives", "category": "right-sizing",
                    "monthly_savings": round(sum(l["monthly"] for l in lines if l["decision"].startswith("compute_") and l["ha"]), 2),
                    "basis": "the extra replicas marked as availability cost", "risk": "A single replica restarts with downtime during deploys.",
                    "payback": "Immediate."})
    return [o for o in out if o["monthly_savings"] > 0]


def risks(plan, design, lines, total):
    ha = round(sum(l["monthly"] for l in lines if l["ha"]), 2)
    pilot = round(0.15 * total, 2)
    warm = round(0.45 * total, 2)
    items = [
        {"id": "RISK-01", "title": "Single-region deployment", "detail": "All services run in one region. A regional outage stops the application."},
        {"id": "RISK-02", "title": "High-availability cost", "detail": "Standby and replica capacity adds $%.2f a month (%.0f%% of the estimate)." % (ha, (ha / total * 100) if total else 0)},
        {"id": "RISK-03", "title": "Disaster recovery estimate",
         "detail": "A pilot-light second region is roughly $%.2f a month (about 15%% of production) and a warm standby roughly $%.2f (about 45%%). "
                   "These are planning ratios, not measured." % (pilot, warm)},
    ]
    if any(l["scales"] == "transfer" for l in lines):
        items.append({"id": "RISK-04", "title": "Data transfer growth", "detail": "Outbound traffic is the line most likely to surprise; it scales with users and payload size."})
    return items


def maturity(lines, unpriced, optimisations, profile, rng):
    spend = sum(l["monthly"] for l in lines) or 1.0
    live = sum(l["monthly"] for l in lines if l["origin"] == "live")
    score, parts = 40, [("A cost model exists", 40)]
    if live / spend >= 0.8:
        score += 20
        parts.append(("80% or more of the spend uses live vendor prices", 20))
    elif live / spend >= 0.4:
        score += 10
        parts.append(("40% or more of the spend uses live vendor prices", 10))
    if all(l.get("source") for l in lines):
        score += 10
        parts.append(("Every line cites a price source", 10))
    score += 10
    parts.append(("A 12-month projection is included", 10))
    if rng:
        score += 10
        parts.append(("A cost range is included", 10))
    if optimisations:
        score += 5
        parts.append(("Optimisations with savings are included", 5))
    if profile.get("daily_requests") or profile.get("peak_concurrent_users"):
        score += 5
        parts.append(("Traffic came from the user, not an assumption", 5))
    return {"score": min(100, score), "parts": parts, "meaning": "Quality of this analysis, not of your FinOps practice."}


def build(plan, design, profile, book_path, fetcher=None, online=True, refresh=False, registry=None):
    fetcher = fetcher or ps.Fetcher(pause=0.0)
    book = Book(book_path)
    pricer = Pricer(plan, design, profile, book, fetcher, online, refresh, registry)
    lines, unpriced = pricer.run()
    if pricer.fetched:
        book.save()
    lines.sort(key=lambda l: (l["decision"], l["label"]))
    total = round(sum(l["monthly"] for l in lines), 2)
    cap = design["capacity"]
    live = sum(l["monthly"] for l in lines if l["origin"] == "live")
    sources = {}
    for l in lines:
        sources.setdefault(l["source"], {"url": l["source"], "origin": l["origin"], "retrieved": l["retrieved"], "lines": 0})["lines"] += 1
    status = "live" if lines and live >= 0.999 * (sum(l["monthly"] for l in lines) or 1) else ("partial" if lines else "none")
    if lines and status == "live" and any(l["origin"] != "live" for l in lines):
        status = "partial"
    rng = cost_range(lines, cap["confidence"] == "low") if lines else None
    opts = optimizations(plan, design, lines, total)
    result = {
        "schema": 1, "currency": "USD", "provider": plan["cloud"], "provider_name": plan["cloud_name"], "region": pricer.region_for(plan["cloud"]),
        "compute_model": plan["compute_model"], "tier": cap["tier"], "traffic": {k: cap[k] for k in ("source", "note", "daily_requests", "peak_rps", "peak_concurrent", "bandwidth_gb_per_day")},
        "sizing": cap["sizing"], "pricing_status": status, "lines": lines, "unpriced": sorted(unpriced, key=lambda u: (u["decision"], u["service"])),
        "totals": {"monthly": total, "annual": round(total * 12, 2), "priced_lines": len(lines), "unpriced_lines": len(unpriced),
                   "live_share": round(live / total, 3) if total else 0.0},
        "included": sorted(pricer.included_items, key=lambda i: (i["decision"], i["service"])),
        "sources": sorted(sources.values(), key=lambda s: s["url"]),
        "projection": projection(lines, cap["tier"]) if lines else None, "range": rng,
        "commitments": commitments(lines, plan["cloud"]), "optimizations": opts, "risks": risks(plan, design, lines, total) if lines else [],
        "finops_maturity": maturity(lines, unpriced, opts, profile, rng) if lines else None,
        "billing_notes": BILLING_NOTES.get(plan["cloud"]), "disclaimers": [CONTRACT_NOTE,
                                                                           "Quantities come from a labelled capacity estimate unless you supplied traffic.",
                                                                           "Taxes, support plans and third-party services (language model APIs, hosted vendors outside the plan) are not included."],
        "ha_monthly": round(sum(l["monthly"] for l in lines if l["ha"]), 2),
    }
    return result


# ------------------------------------------------------------------ rendering
def money(value):
    return "$%s" % format(value, ",.2f")


def render_markdown(costs):
    """The cost report as markdown. Every dollar figure is in a table with its source."""
    out = ["# Cost estimate: %s" % costs["provider_name"], "",
           "Region %s. Compute model: %s. Traffic tier: %s. Prices are USD list prices, on demand." % (costs["region"], costs["compute_model"], costs["tier"]), ""]
    t = costs["totals"]
    status = {"live": "All priced lines use live vendor prices read during a SolsArch run.",
              "partial": "Some lines use live vendor prices; others use supplied or dated reference values, as each line says.",
              "none": "No line could be priced from a public source."}[costs["pricing_status"]]
    out += ["## Summary", "", status, ""]
    if costs["lines"]:
        out += ["| Measure | Value |", "| :--- | :--- |", "| Estimated monthly cost (priced lines) | %s |" % money(t["monthly"]),
                "| Estimated first-year cost | %s |" % money(costs["projection"]["year_one_total"]),
                "| Priced lines | %d |" % t["priced_lines"], "| Lines without a public price | %d |" % t["unpriced_lines"]]
        if costs["range"]:
            out.append("| Monthly range (P10, P50, P90) | %s, %s, %s |" % (money(costs["range"]["p10"]), money(costs["range"]["p50"]), money(costs["range"]["p90"])))
        out += [""]
    else:
        out += ["No monthly total is shown because no line was priced. See the unpriced services below.", ""]
    out += ["## What the numbers assume", "", "- %s" % costs["traffic"]["note"],
            "- Traffic estimate: %s requests a day, peak %s requests a second, %s concurrent." % (
                format(costs["traffic"]["daily_requests"], ","), costs["traffic"]["peak_rps"], costs["traffic"]["peak_concurrent"]),
            "- Sizing: %s units, %s replicas each, %s vCPU and %s GB per replica, database class %s." % (
                costs["sizing"]["units"], costs["sizing"]["replicas_per_unit"], costs["sizing"]["unit_vcpu"], costs["sizing"]["unit_memory_gb"], costs["sizing"]["db_class"])]
    out += ["- %s" % d for d in costs["disclaimers"]]
    if costs.get("billing_notes"):
        out.append("- Billing: %s" % costs["billing_notes"])
    out.append("")
    if costs["lines"]:
        out += ["## Itemised monthly cost", "", "| Service | Item | Quantity | Unit price | Monthly | Price source |", "| :--- | :--- | ---: | ---: | ---: | :--- |"]
        for l in costs["lines"]:
            out.append("| %s | %s | %s %s | %s | %s | %s (%s, read %s) |" % (
                l["decision"], l["label"] + (" - " + l["note"] if l["note"] else ""), format(l["quantity"], ",.2f"), l["unit"],
                ("$%.6g per %s" % (l["unit_price"], l["price_unit"] or "unit")) if l["unit_price"] else "free tier or none",
                money(l["monthly"]), l["source"], l["origin"], l["retrieved"]))
        out += ["", "Total: **%s** a month." % money(t["monthly"]), ""]
    if costs.get("included"):
        out += ["## Included at no separate charge", "", "| Service | Why there is no line |", "| :--- | :--- |"]
        out += ["| %s | %s |" % (i["service"], i["note"]) for i in costs["included"]] + [""]
    if costs["unpriced"]:
        out += ["## Services without a public price", "", "| Service | Why | How to price it |", "| :--- | :--- | :--- |"]
        for u in costs["unpriced"]:
            out.append("| %s | %s | %s |" % (u["service"], u["reason"].replace("|", "/"), (u.get("how") or "-").replace("|", "/")))
        out.append("")
    if costs["projection"]:
        p = costs["projection"]
        out += ["## 12-month projection", "", p["assumption"] + ".", "", "| Month | Monthly cost | Cumulative |", "| ---: | ---: | ---: |"]
        out += ["| %d | %s | %s |" % (m["month"], money(m["monthly"]), money(m["cumulative"])) for m in p["months"]]
        out += ["", "First-year total %s. Month-12 run rate %s a year." % (money(p["year_one_total"]), money(p["month_12_run_rate_annual"])), ""]
    if costs["range"]:
        r = costs["range"]
        out += ["## Cost range", "", "%s median, with P10 %s and P90 %s. Main driver: %s. Method: %s." % (
            money(r["p50"]), money(r["p10"]), money(r["p90"]), r["top_driver"] or "none", r["method"]), ""]
        out += ["- %s" % a for a in r["assumptions"]] + [""]
    if costs["commitments"]:
        c = costs["commitments"]
        out += ["## Commitment options", "", c["note"], "", "| Option | Compute per month | Data stores per month |", "| :--- | ---: | ---: |",
                "| On demand (this estimate) | %s | %s |" % (money(c["compute_monthly"]), money(c["data_monthly"])),
                "| %s, 1 year (%.0f%% off) | %s | - |" % (c["flexible"], c["discounts"]["flexible_1yr"] * 100, money(c["flexible_1yr_monthly"])),
                "| %s, 3 years (%.0f%% off) | %s | - |" % (c["flexible"], c["discounts"]["flexible_3yr"] * 100, money(c["flexible_3yr_monthly"])),
                "| %s, 1 year (%.0f%% off) | - | %s |" % (c["fixed"], c["discounts"]["reserved_1yr"] * 100, money(c["reserved_1yr_data_monthly"])),
                "| %s, 3 years (%.0f%% off) | - | %s |" % (c["fixed"], c["discounts"]["reserved_3yr"] * 100, money(c["reserved_3yr_data_monthly"])), ""]
    if costs["optimizations"]:
        out += ["## Optimisations", "", "| ID | Change | Monthly savings | Basis | Risk |", "| :--- | :--- | ---: | :--- | :--- |"]
        out += ["| %s | %s | %s | %s | %s |" % (o["id"], o["title"], money(o["monthly_savings"]), o["basis"], o["risk"]) for o in costs["optimizations"]]
        out.append("")
    if costs["risks"]:
        out += ["## Cost risks", ""] + ["- **%s.** %s" % (r["title"], r["detail"]) for r in costs["risks"]] + [""]
    if costs["finops_maturity"]:
        m = costs["finops_maturity"]
        out += ["## Analysis quality score", "", "**%d out of 100.** %s" % (m["score"], m["meaning"]), ""] + ["- %s (+%d)" % (n, p) for n, p in m["parts"]] + [""]
    if costs["sources"]:
        out += ["## Price sources", "", "| Source | Origin | Read on | Lines |", "| :--- | :--- | :--- | ---: |"]
        out += ["| %s | %s | %s | %d |" % (s["url"], s["origin"], s["retrieved"], s["lines"]) for s in costs["sources"]] + [""]
    return chr(10).join(out)


def add_supplied(book_path, provider, meter, price, unit, source, quote, region="global"):
    book = Book(book_path)
    item = ps.supplied(provider, meter, price, unit, source, quote, region)
    book.put(item)
    book.save()
    return item


def load_prices_file(book_path, path):
    """Merge a file of supplied prices: [{provider, meter, price, unit, source, quote, region?}]."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    rows = data.get("prices", data) if isinstance(data, dict) else data
    if not isinstance(rows, list):
        raise ps.PriceError("the prices file must be a list of price objects")
    book = Book(book_path)
    added = []
    for row in rows:
        item = ps.supplied(row.get("provider"), row.get("meter"), row.get("price"), row.get("unit", "unit"), row.get("source"),
                           row.get("quote"), row.get("region", "global"))
        book.put(item)
        added.append(item)
    book.save()
    return added


def main(argv=None):
    parser = argparse.ArgumentParser(description="Estimate the monthly cost of the planned architecture")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--refresh", action="store_true", help="Read fresh prices instead of the price book")
    parser.add_argument("--offline", action="store_true", help="Never use the network; use the price book only")
    args = parser.parse_args(argv)
    repo = Path(args.repo)
    try:
        plan = json.loads((repo / ".solsarch" / "plan.json").read_text(encoding="utf-8"))
        design = json.loads((repo / ".solsarch" / "design.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        sys.stderr.write("error: run scan, plan and evidence first" + chr(10))
        return 2
    import architect
    import providers
    result = build(plan, design, architect.load_profile(repo), repo / ".solsarch" / BOOK_FILE, online=not args.offline,
                   refresh=args.refresh, registry=providers.registry())
    sys.stdout.write(json.dumps(result, indent=2) + chr(10))
    return 0


if __name__ == "__main__":
    sys.exit(main())
