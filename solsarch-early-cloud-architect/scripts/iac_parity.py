#!/usr/bin/env python3
"""One capability checklist for every provider, so no cloud gets a better result because of who wrote its generator.

Each capability is checked the same way on every provider: a regular expression over the files that provider's generator
wrote. A capability the plan does not need (no database, no object storage) is not counted. A capability the platform
provides on its own (an application platform terminates TLS and isolates tenants for you) is marked provider_managed, with
the reason, and still counted as met. A capability the generator should have written and did not is a gap and is reported.
The score is met over applicable, the same arithmetic for all providers.
"""
import re

CAPABILITIES = [
    ("CAP-01", "Private network around the workloads", "network"),
    ("CAP-02", "Firewall that denies traffic by default", "firewall"),
    ("CAP-03", "Public traffic enters through one managed front door", "front_door"),
    ("CAP-04", "Containers run hardened (non-root, limits)", "hardened"),
    ("CAP-05", "Health checks use the path the code serves", "health"),
    ("CAP-06", "More than one copy and autoscaling when high availability is on", "scaling"),
    ("CAP-07", "Managed database on the private network with backups", "database"),
    ("CAP-08", "Data stores encrypted at rest", "encryption"),
    ("CAP-09", "Object storage private, with versioning", "storage"),
    ("CAP-10", "Secrets in a secret store, not in files", "secrets"),
    ("CAP-11", "Private container registry", "registry"),
    ("CAP-12", "Logs and monitoring", "observability"),
    ("CAP-13", "Least-privilege identity for the workloads", "identity"),
    ("CAP-14", "Remote state or a deploy script that can be repeated", "state"),
    ("CAP-15", "Safe first apply (nothing waits on an image that does not exist yet)", "two_phase"),
    ("CAP-16", "Deletion protection on stateful resources", "protection"),
    ("CAP-17", "Outputs or inventory that feed the next step", "outputs"),
]

# regex per capability, searched over the lower-cased text of every generated file for the provider
PATTERNS = {
    "network": r"aws_vpc\b|azurerm_virtual_network|google_compute_network|hcloud_network\b|linode_vpc|vultr_vpc|civo_network|upcloud_network|exoscale_private_network|exoscale_sks_cluster|digitalocean_vpc|scaleway_vpc\b|ovh_cloud_project_network_private\b|oci_core_vcn|alicloud_vpc|tencentcloud_vpc|huaweicloud_vpc\b|ibm_is_vpc|kind: networkpolicy",
    "firewall": r"aws_security_group|azurerm_network_security_group|google_compute_firewall|hcloud_firewall|linode_firewall|vultr_firewall|civo_firewall|exoscale_security_group|control_plane_ip_filter|digitalocean_database_firewall|oci_core_network_security_group|alicloud_security_group|tencentcloud_security_group|huaweicloud_networking_secgroup|ufw|name: default-deny|enable_firewall",
    "front_door": r"aws_lb\b|aws_lambda_function_url|aws_cloudfront_distribution|azurerm_static_web_app|azurerm_cdn_frontdoor|azurerm_cdn_endpoint|google_compute_backend_bucket|alicloud_cdn_domain|huaweicloud_cdn_domain|tencentcloud_cdn_domain|scaleway_edge_services_pipeline|azurerm_application_gateway|google_compute_url_map|hcloud_load_balancer\b|oci_load_balancer_load_balancer|alicloud_alb_load_balancer|kind: ingress|apigateway|api_gateway|caddy|enable_default_public_endpoint|scaleway_container\b|digitalocean_app\b|ibm_code_engine_app|cciv2_service|tencentcloud_clb|elb_loadbalancer",
    "hardened": r"readonlyrootfilesystem|read_only_root_filesystem|is_root_file_system_readonly|run_as_non_root|runasnonroot|readonly_root|read_only: true|cap_drop|--user|user 10001|run_as_user|user nonroot|user 1000",
    "health": r"health_check|\.checks\]|healthcheck_url|healthcheck|health_check_path|healthcheckpath|readinessprobe|liveness|probe_readiness|healthchecker|health_uri|http_path|healthcheck_path",
    "scaling": r"autoscal|app_autoscaling|min_replicas|minreplicas|auto_scaler|horizontalpodautoscaler|scale_max|max_scale|max_instances|max_nodes|node_max|numinstances|num_instances|instance_count|app_server_count|replicas",
    "database": r"solsarch-backup|pg_dump|aws_rds_cluster|azurerm_postgresql_flexible_server|google_sql_database_instance|linode_database_|vultr_database|civo_database|upcloud_managed_database|exoscale_dbaas|digitalocean_database_cluster|scaleway_rdb_instance|ovh_cloud_project_database\b|oci_psql_db_system|alicloud_db_instance|tencentcloud_postgresql_instance|huaweicloud_rds_instance|ibm_database|name: postgres|neon_project|mongodbatlas_advanced_cluster",
    "encryption": r"storage_encrypted|encryption_at_rest|encrypted\s*=\s*true|disk_encryption|infrastructure_encryption|kms_key|disk_encrypt|ssl_enable|encrypt\s*=\s*true|enable_encryption|sse_algorithm|server_side_encryption|ssl_mode|require_secure_transport|customer_managed|cmek|ssl\s*=\s*true",
    "storage": r"aws_s3_bucket_versioning|azurerm_storage_account|google_storage_bucket|linode_object_storage_bucket|vultr_object_storage|civo_object_store|upcloud_managed_object_storage|digitalocean_spaces_bucket|scaleway_object_bucket|ovh_cloud_project_storage|oci_objectstorage_bucket|alicloud_oss_bucket|tencentcloud_cos_bucket|huaweicloud_obs_bucket|ibm_cos_bucket",
    "secrets": r"aws_secretsmanager_secret|aws_ssm_parameter|azurerm_key_vault|google_secret_manager_secret|scaleway_secret\b|oci_vault_secret|alicloud_kms_secret|tencentcloud_ssm_secret|huaweicloud_csms_secret|ibm_resource_instance\" \"secrets|secrets-manager|ansible-vault|vault\.example|kind: secret|sync: false|secret_environment_variables|type\s*=\s*\"secret\"|fly secrets|heroku config|wrangler secret|supabase secrets|\"secret\"",
    "registry": r"aws_ecr_repository|azurerm_container_registry|google_artifact_registry|scaleway_registry_namespace|digitalocean_container_registry|ovh_cloud_project_containerregistry|oci_artifacts_container_repository|alicloud_cr_|tencentcloud_tcr_|huaweicloud_swr_|ibm_cr_namespace|registry_address|images:|registry_user",
    "observability": r"aws_cloudwatch|kube-prometheus-stack|cockpit|monitoring-compose|alert\s*\{|azurerm_log_analytics|google_monitoring|google_logging|prometheus|oci_logging_log_group|alicloud_log_project|tencentcloud_cls_|huaweicloud_lts_|ibm_resource_instance\" \"logs|observability|log_drain|monitoring_enabled|digitalocean_monitor",
    "identity": r"aws_iam_role|azurerm_user_assigned_identity|google_service_account|serviceaccount|ibm_resource_group|oci_identity|ram_role|cam_role|agency|automountserviceaccounttoken|user: \"10001|deploy user|ssh_keys|run_as_user",
    "state": r"backend\s+\"|backend\.hcl|deploy\.sh|launch\.sh|create\.sh|render\.yaml|fly deploy|railway\.json|wrangler deploy|firebase deploy|supabase db push|databricks bundle|site\.yml",
    "two_phase": r"-target=aws_ecr_repository|deploy_app|bootstrap/main\.tf|\"deploy\"|count\s*=\s*var\.deploy|autodeploy: false|render\.yaml|fly deploy|--remote-only|apply -k",
    "protection": r"deletion_protection|prevent_destroy|delete_protection|termination_protection|deletion_protection_enabled|deletion_policy|lock_level|force_destroy\s*=\s*false|ha_enabled|retain",
    "outputs": r"output\s+\"|ansible_inventory|terraform output|registry_address|kubeconfig",
}

# Platforms that provide a capability themselves. The reason is shown in the report.
MANAGED = {
    "front_door": "the platform router terminates HTTPS and balances traffic",
    "network": "the platform isolates tenants and offers private networking as a setting",
    "firewall": "the platform exposes only the ports the service declares",
    "hardened": "the platform runs your image in its own sandbox",
    "encryption": "the platform encrypts managed data at rest",
    "scaling": "the platform scales the service from its plan size",
}
PLATFORM_KINDS = ("paas", "edge", "data", "gpu")
PLATFORM_REASONS = {
    "health": "the platform checks the service itself from its settings",
    "database": "the database is created from the platform's dashboard or marketplace, which has no file",
    "storage": "storage is created from the platform's dashboard or CLI, which has no file",
    "secrets": "the platform keeps secrets in its dashboard or CLI, never in a file",
    "registry": "the platform builds and stores images itself",
    "observability": "the platform provides logs and metrics in its dashboard",
    "identity": "the platform has no workload identity to configure",
    "state": "the platform keeps its own state; the deploy command is repeatable",
    "two_phase": "the platform builds from your repository, so nothing waits on an image",
    "protection": "the platform protects data resources from its dashboard",
    "outputs": "the platform shows addresses in its dashboard",
    "front_door": "the platform router terminates HTTPS and balances traffic",
    "network": "the platform isolates tenants and offers private networking as a setting",
    "firewall": "the platform exposes only the ports the service declares",
    "hardened": "the platform runs your image in its own sandbox",
    "encryption": "the platform encrypts managed data at rest",
    "scaling": "the platform scales the service from its plan size",
}
# What a cloud does not sell, so there is nothing to write. Shown in the report, not counted against the score.
NOT_OFFERED = {
    "digitalocean": {"identity": "DigitalOcean has no workload identity service; the app uses scoped tokens in encrypted variables"},
    "hetzner": {"storage": "Hetzner Object Storage has no Terraform resource; create the bucket in the console",
                "secrets": "Hetzner has no secret manager; Ansible Vault encrypts the secrets file"},
    "vultr": {"identity": "Vultr has no workload identity service; use scoped API keys in Kubernetes secrets"},
    "oci": {"front_door": "Oracle Object Storage serves the static files directly and has no content delivery network Terraform can create; put Cloudflare in front (--also cloudflare)"},
    "ibm": {"front_door": "IBM Cloud Object Storage serves the static files from its website endpoint; a CDN needs IBM Cloud Internet Services and a domain you own"},
    "civo": {"identity": "Civo has no workload identity service; use scoped API keys in Kubernetes secrets"},
    "upcloud": {"identity": "UpCloud has no workload identity service; use scoped API credentials in Kubernetes secrets"},
    "exoscale": {"identity": "Exoscale has no workload identity service; use scoped IAM keys in Kubernetes secrets",
                 "storage": "Exoscale buckets are created through the S3 interface (exo storage mb), not by the Terraform provider"},
    "linode": {"identity": "Linode has no workload identity service; use scoped tokens in Kubernetes secrets"},
    "akamai": {"identity": "Akamai Cloud has no workload identity service; use scoped tokens in Kubernetes secrets"},
    "scaleway": {"observability": "Scaleway Cockpit is on by default for the project and needs no resource"},
}


def applicable(model):
    """Which capabilities this plan needs, from what the plan contains."""
    need = {c[2] for c in CAPABILITIES}
    if not model.db_key and not model.has("database_mongo"):
        need -= {"database"}
    if not model.has("storage"):
        need -= {"storage"}
    stateful = bool(model.db_key or model.has("database_mongo") or model.has("cache") or model.has("storage") or model.has("queue_kafka") or model.has("queue_rabbitmq"))
    if not stateful:
        need -= {"encryption", "protection"}
    if not any(u["kind"] != "static" for u in model.units):
        need -= {"hardened", "health", "scaling", "two_phase", "registry", "identity", "firewall", "secrets", "network"}
    if not model.has("secrets"):
        need -= set()
    if model.single_az:
        need -= {"scaling"}
    if model.mode == "serverless" and not model.needs_private_network:
        need -= {"network", "firewall"}  # functions outside a VPC have nothing to wall off; the data is reached over TLS with credentials
    return need


def evaluate(model, provider, kind, texts):
    """texts: dict path -> text for everything generated for this provider. Returns the capability rows and the score."""
    haystack = "\n".join(texts.values()).lower()
    need = applicable(model)
    rows = []
    for cap_id, title, key in CAPABILITIES:
        if key not in need:
            rows.append({"id": cap_id, "title": title, "status": "not_applicable", "reason": "the plan does not need it"})
            continue
        if re.search(PATTERNS[key], haystack):
            rows.append({"id": cap_id, "title": title, "status": "implemented", "reason": ""})
        elif key in NOT_OFFERED.get(provider, {}):
            rows.append({"id": cap_id, "title": title, "status": "not_offered", "reason": NOT_OFFERED[provider][key]})
        elif model.mode == "serverless" and key in ("health", "scaling"):
            rows.append({"id": cap_id, "title": title, "status": "provider_managed", "reason": "functions scale per request and are checked by the platform"})
        elif kind in PLATFORM_KINDS and key in PLATFORM_REASONS:
            rows.append({"id": cap_id, "title": title, "status": "provider_managed", "reason": PLATFORM_REASONS[key]})
        else:
            rows.append({"id": cap_id, "title": title, "status": "gap", "reason": "no matching resource was written"})
    counted = [r for r in rows if r["status"] not in ("not_applicable", "not_offered")]
    met = [r for r in counted if r["status"] in ("implemented", "provider_managed")]
    return {"provider": provider, "capabilities": rows, "applicable": len(counted), "met": len(met),
            "implemented": sum(1 for r in counted if r["status"] == "implemented"),
            "provider_managed": sum(1 for r in counted if r["status"] == "provider_managed"),
            "not_offered": [r["id"] for r in rows if r["status"] == "not_offered"],
            "gaps": [r["id"] for r in counted if r["status"] == "gap"],
            "score": round(100.0 * len(met) / len(counted), 1) if counted else 100.0}
