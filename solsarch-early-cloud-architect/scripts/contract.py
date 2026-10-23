#!/usr/bin/env python3
"""The shared architecture contract: one document that every later step reads and checks against.

    contract.py --repo REPO

The plan, the generated infrastructure code, the cost report and the diagrams are made by different steps. This file ties
them together. Each plan decision gets a stable resource id (`res.<decision>.<provider>`), each generated resource is mapped
to the decision that asked for it, and each cost line carries the same id, so "does every billable resource have a price"
and "does every resource trace to a decision" are joins on ids, not searches through text.

The file is a pure function of the other state files: no clock, no randomness, sorted keys.
"""
import argparse
import json
import re
import sys
from pathlib import Path

# Terraform resource types that cost money or define the shape of the deployment, and the plan decision each one belongs to.
# First match wins. A type with no match is a supporting resource (a policy, a tag, a random password) and needs no decision.
TYPE_RULES = [
    (r"nat_gateway|nat_instance|cloud_nat|router_nat|azurerm_nat|_nat_gw", "egress", True),
    (r"aws_lambda_function_url|aws_apigatewayv2_api|aws_api_gateway_rest_api|aws_lb$|aws_alb$|azurerm_application_gateway|azurerm_lb$|"
     r"google_compute_url_map|google_compute_forwarding_rule|_load_balancer$|cloudfront_distribution|azurerm_cdn|azurerm_frontdoor", "ingress", True),
    (r"aws_wafv2_web_acl|google_compute_security_policy|azurerm_web_application_firewall", "edge", True),
    (r"aws_docdb|azurerm_cosmosdb_account|mongodbatlas_advanced_cluster|mongodbatlas_cluster|mongodbatlas_serverless_instance", "database_mongo", True),
    (r"mysql", "database_mysql", True),
    (r"aws_db_instance|aws_rds_cluster|azurerm_postgresql|google_sql_database_instance|neon_project|"
     r"digitalocean_database_cluster|linode_database|vultr_database|scaleway_rdb_instance|alicloud_db_instance|tencentcloud_postgresql|"
     r"huaweicloud_rds_instance|ibm_database|oci_psql_db_system", "database", True),
    (r"aws_elasticache|azurerm_redis|google_redis|_redis_", "cache", True),
    (r"aws_mq_broker", "queue_rabbitmq", True),
    (r"aws_msk_cluster", "queue_kafka", True),
    (r"aws_secretsmanager_secret$|aws_ssm_parameter|azurerm_key_vault_secret|google_secret_manager_secret$|scaleway_secret$", "secrets", True),
    (r"aws_scheduler_schedule$|google_cloud_scheduler_job|azurerm_logic_app_trigger_recurrence", "scheduler", False),
    (r"aws_ecr_repository$|azurerm_container_registry|google_artifact_registry_repository|digitalocean_container_registry|"
     r"scaleway_registry_namespace", "registry", True),
    (r"aws_lambda_function$|aws_ecs_service|aws_apprunner_service|azurerm_container_app$|azurerm_linux_function_app|azurerm_linux_web_app|"
     r"google_cloud_run|google_cloudfunctions|hcloud_server|linode_instance|vultr_instance|digitalocean_droplet|digitalocean_app|"
     r"scaleway_container$|kubernetes_deployment|azurerm_kubernetes_cluster|google_container_cluster|aws_eks_cluster", "compute", True),
    (r"aws_s3_bucket$|azurerm_storage_account$|google_storage_bucket$|digitalocean_spaces_bucket|linode_object_storage_bucket", "storage", True),
    (r"aws_vpc$|azurerm_virtual_network|google_compute_network$|hcloud_network$|digitalocean_vpc|linode_vpc|vultr_vpc", "network", False),
    (r"aws_cloudwatch_log_group|azurerm_log_analytics_workspace|google_logging_project_bucket_config", "observability", False),
]
COMPILED = [(re.compile(p), key, billable) for p, key, billable in TYPE_RULES]
# Decisions that a generated resource may satisfy besides its own key (one static site is both storage and ingress, for example).
ALSO_SATISFIES = {"storage": ("static_site", "storage"), "ingress": ("ingress", "static_site", "edge"), "edge": ("edge", "ingress")}
# A container image repository belongs to whatever runs the image; it is not a plan decision of its own for functions.
IMPLIED_BY_COMPUTE = ("registry",)
RESOURCE_RE = re.compile(r'^resource\s+"([a-z0-9_]+)"\s+"([A-Za-z0-9_.-]+)"', re.M)


def resource_id(key, provider):
    return "res.%s.%s" % (key, provider)


def read(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def parse_resources(files):
    """[{type, name, file}] for every Terraform resource in the generated files, sorted."""
    found = []
    for path in sorted(files):
        if path.endswith(".tf"):
            for rtype, name in RESOURCE_RE.findall(files[path]):
                found.append({"type": rtype, "name": name, "file": path})
    return found


def classify(rtype):
    """(decision key or None, billable) for a resource type."""
    for rx, key, billable in COMPILED:
        if rx.search(rtype):
            return key, billable
    return None, False


def decision_for(rtype, plan_keys):
    """The plan decision key that explains this resource type, or None when the plan did not ask for it."""
    key, _ = classify(rtype)
    if key is None:
        return None
    if key == "compute" or (key in IMPLIED_BY_COMPUTE and key not in plan_keys):
        return next((k for k in sorted(plan_keys) if k.startswith("compute_")), None)
    if key in plan_keys:
        return key
    for alt in ALSO_SATISFIES.get(key, ()):
        if alt in plan_keys:
            return alt
    if key.startswith("database") and any(k.startswith("database") for k in plan_keys):
        return next(k for k in sorted(plan_keys) if k.startswith("database"))
    return None


def build(repo):
    """The contract for a repository, from the state files the other steps wrote. Returns None when there is no plan yet."""
    state = Path(repo) / ".solsarch"
    scan, plan, design = read(state / "scan.json"), read(state / "plan.json"), read(state / "design.json")
    if not (scan and plan and design):
        return None
    manifest = read(state / "iac-manifest.json") or {}
    costs = read(state / "costs.json") or {}
    cloud = plan["cloud"]
    profile = plan.get("profile") or {}

    decisions = []
    for d in sorted(plan["decisions"], key=lambda x: x["key"]):
        provider = d.get("provider") or cloud
        decisions.append({"id": resource_id(d["key"], provider), "key": d["key"], "service": d["service"], "provider": provider,
                          "layer": d.get("layer"), "evidence": d.get("evidence", []), "confidence": d.get("confidence")})
    keys = {d["key"] for d in decisions}  # decisions another provider supplies (a free database cluster, say) have resources too

    resources = []
    for r in manifest.get("resources", []):
        key, billable = classify(r["type"])
        if "/bootstrap/" in "/" + r["file"]:  # the bucket that keeps Terraform state is plumbing, not part of the design
            key, billable = "state", False
        resources.append({"type": r["type"], "name": r["name"], "file": r["file"], "billable": billable,
                          "decision": None if key == "state" else decision_for(r["type"], keys), "category": key})

    priced = {}
    for l in costs.get("lines", []):
        priced.setdefault(l["decision"], []).append(l.get("resource_id") or resource_id(l["decision"], l["provider"]))
    for u in costs.get("unpriced", []):
        priced.setdefault(u["decision"], []).append(resource_id(u["decision"], u["provider"]))
    for i in costs.get("included", []):
        priced.setdefault(i["decision"], []).append(resource_id(i["decision"], i["provider"]))

    units = [{"name": u["name"], "kind": u["kind"], "folder": u.get("folder"), "port": u.get("port"), "entrypoint": u.get("entrypoint"),
              "streaming": bool(u.get("streaming")), "scheduler": bool(u.get("scheduler")), "health_path": u.get("health_path"),
              "frameworks": sorted(u.get("frameworks", []))} for u in design.get("units", [])]
    return {
        "schema": 1, "plan_fingerprint": plan["fingerprint"],
        "application": {
            "components": sorted(({"name": c["name"], "category": c["category"],
                                   "evidence": [{"file": e.get("file"), "line": e.get("line")} for e in c.get("evidence", [])[:3]]}
                                  for c in scan.get("components", [])), key=lambda c: (c["category"], c["name"])),
            "dependencies": sorted(d.get("name", "") for d in scan.get("dependencies", []) if isinstance(d, dict)) if scan.get("dependencies") else [],
            "runtime_requirements": units,
            "env_vars": sorted(({"name": v["name"], "required": v["required"], "category": v["category"]} for v in scan.get("env_vars", [])),
                               key=lambda v: v["name"]),
            "signals": scan.get("signals", {}),
        },
        "constraints": {"cloud": cloud, "region": profile.get("region"), "budget": (design.get("profile") or {}).get("monthly_budget"),
                        "deployment_mode": plan["compute_model"], "account_type": profile.get("account_type", "standard"),
                        "compliance": profile.get("compliance", [])},
        "architecture": {"decisions": decisions, "resources": resources,
                         "priced_decisions": {k: sorted(set(v)) for k, v in sorted(priced.items())},
                         "connections": connections(decisions)},
        "validation": {"compatibility": [], "infrastructure": [], "security": [], "cost": [], "consistency": []},
        "evaluation": {"score": None, "critical_findings": [], "warnings": [], "verified": False, "deploy_ready": False},
    }


def connections(decisions):
    """Who talks to whom, from layers: entry points call compute, compute calls data and support services."""
    by_layer = {}
    for d in decisions:
        by_layer.setdefault(d["layer"], []).append(d["id"])
    pairs = []
    for src, dst in (("ingress", "compute"), ("edge", "ingress"), ("compute", "data"), ("compute", "support")):
        for a in by_layer.get(src, []):
            for b in by_layer.get(dst, []):
                pairs.append({"from": a, "to": b})
    return pairs


def untraced(contract):
    """Generated billable resources that no plan decision explains."""
    return [r for r in contract["architecture"]["resources"] if r["billable"] and not r["decision"]]


def unpriced_decisions(contract):
    """Decisions whose generated resources are billable but which have no cost line, included note or unpriced entry."""
    priced = contract["architecture"]["priced_decisions"]
    wanted = {r["decision"] for r in contract["architecture"]["resources"] if r["billable"] and r["decision"]}
    return sorted(k for k in wanted if k not in priced)


def write(repo):
    data = build(repo)
    if data is None:
        return None
    target = Path(repo) / ".solsarch" / "contract.json"
    old = read(target)
    if old and old.get("plan_fingerprint") == data["plan_fingerprint"]:  # keep what the quality stage recorded for this same plan
        data["validation"], data["evaluation"] = old.get("validation", data["validation"]), old.get("evaluation", data["evaluation"])
    target.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    return data


def main(argv=None):
    parser = argparse.ArgumentParser(description="Build the shared architecture contract")
    parser.add_argument("--repo", default=".")
    args = parser.parse_args(argv)
    data = write(args.repo)
    if data is None:
        print(json.dumps({"status": "error", "message": "No scan, plan and design found. Run the scan and plan steps first."}))
        return 1
    print(json.dumps({"status": "written", "file": str(Path(args.repo) / ".solsarch" / "contract.json"),
                      "decisions": len(data["architecture"]["decisions"]), "resources": len(data["architecture"]["resources"]),
                      "untraced_billable": len(untraced(data)), "unpriced_decisions": unpriced_decisions(data)}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
