#!/usr/bin/env python3
"""Infrastructure code for the chosen provider: one entry point that picks the right generators.

    iac.build(plan, design, profile, scan, project)  ->  {"files": {path: text}, "manifest": {...}}

What each provider gets:
  Terraform + Ansible + Kubernetes + CI   the three major clouds and the clouds that sell servers, networks and databases
  Application platforms and edge          the platform's own configuration file plus a deploy script
  Data platforms                          Terraform modules beside whatever hosts the application

The same capability checklist is run over every provider's files (iac_parity), so the result is comparable and a missing
capability shows up as a gap instead of a quiet omission. Output is a pure function of the plan, design, profile and scan, so
running it twice gives byte-identical files.
"""
import hashlib
import importlib
import json
import re

import contract
import iac_ci
import iac_common
import iac_docker
import iac_paas
import iac_parity
import providers as provider_registry

TERRAFORM = {
    "aws": "iac_aws", "azure": "iac_azure", "gcp": "iac_gcp", "hetzner": "iac_hetzner", "linode": "iac_linode", "akamai": "iac_linode",
    "vultr": "iac_vultr", "digitalocean": "iac_digitalocean", "scaleway": "iac_scaleway", "ovhcloud": "iac_ovhcloud", "oci": "iac_oci",
    "alibaba": "iac_alibaba", "tencent": "iac_tencent", "huawei": "iac_huawei", "ibm": "iac_ibm",
    "civo": "iac_civo", "upcloud": "iac_upcloud", "exoscale": "iac_exoscale",
}
DATA_MODULES = {"neon": "neon", "mongodbatlas": "mongodbatlas"}
# Providers whose application runs on Kubernetes; they also get the manifests, the Helm chart and the add-ons playbook.
KUBERNETES = {"linode": "nginx", "akamai": "nginx", "vultr": "nginx", "ovhcloud": "nginx", "coreweave": "nginx", "civo": "nginx", "upcloud": "nginx", "exoscale": "nginx"}
KUBERNETES_SERVERLESS_EXCLUDED = {"tencent"}  # Tencent's serverless design uses functions, not a cluster
SERVER_ANSIBLE = {"hetzner", "lambdalabs"}


def tf_dir(provider):
    return "terraform"


def build(plan, design, profile, scan, project=None):
    import iac_ansible
    import iac_data
    import iac_k8s
    provider = plan["cloud"]
    model = iac_common.Model(plan, design, profile, scan, project)
    kind = (provider_registry.registry().get(provider) or {}).get("kind", "cloud")
    out = {}
    families = []

    def add(files, family, label):
        paths = sorted(files.files)
        for p in paths:
            out[p] = files.files[p]
        families.append({"family": family, "label": label, "files": len(paths)})

    if provider in TERRAFORM:
        mod = importlib.import_module(TERRAFORM[provider])
        add(mod.generate(model), "terraform", "Terraform for %s" % plan["cloud_name"])
    if provider in KUBERNETES or (provider == "tencent" and model.mode == "server"):
        add(iac_k8s.generate(model), "kubernetes", "Kubernetes manifests, Kustomize base and Helm chart")
        add(iac_ansible.addons(model), "ansible", "Ansible add-ons playbook for the cluster")
    if provider in SERVER_ANSIBLE:
        add(iac_ansible.generate(model), "ansible", "Ansible configuration for the servers")
    if provider in iac_paas.GENERATORS:
        add(iac_paas.generate(model, provider), "platform", "%s deployment configuration" % plan["cloud_name"])
    if provider in DATA_MODULES:
        add(iac_data.generate(model, DATA_MODULES[provider]), "terraform", "Terraform for %s" % plan["cloud_name"])
    for overlay in plan.get("overlays", []):
        if overlay in DATA_MODULES:
            add(iac_data.generate(model, DATA_MODULES[overlay]), "terraform", "Terraform for the %s overlay" % overlay)
        elif overlay in iac_paas.GENERATORS:
            add(iac_paas.generate(model, overlay), "platform", "%s configuration for the overlay" % overlay)
    docker = iac_docker.generate(model)
    if docker.files:
        add(docker, "docker", "Dockerfiles for units without one")
    ci_dir = "terraform" if provider in iac_ci.TERRAFORM_PROVIDERS else "."
    add(iac_ci.generate(model, provider, ci_dir), "ci", "GitHub Actions pipeline")
    parity = iac_parity.evaluate(model, provider, kind, out)
    manifest = {
        "schema": 1, "provider": provider, "provider_name": plan["cloud_name"], "provider_kind": kind, "compute_model": plan["compute_model"],
        "plan_fingerprint": plan["fingerprint"], "families": families, "file_count": len(out), "parity": parity,
        "digest": hashlib.sha256(json.dumps({p: out[p] for p in sorted(out)}, sort_keys=True).encode("utf-8")).hexdigest()[:16],
        "next_steps": next_steps(provider, kind, out),
        "resources": contract.parse_resources(out),
        "resource_types": sorted({m for p, t in out.items() if p.endswith(".tf") for m in re.findall(r'^resource "([a-z0-9_]+)"', t, re.M)}),
        "streaming_compatible": any("RESPONSE_STREAM" in t for p, t in out.items() if p.endswith(".tf")),
        "has_scheduler": any("aws_scheduler_schedule" in t for p, t in out.items() if p.endswith(".tf")),
        "injected_env_vars": sorted({v["name"] for v in model.config_inputs}),
    }
    return {"files": out, "manifest": manifest}


def next_steps(provider, kind, files):
    steps = []
    if any(p.startswith("terraform/") for p in files):
        steps.append("Read terraform/README.md, then run terraform init, plan and apply as it describes.")
    if any(p.startswith("kubernetes/") for p in files):
        steps.append("After the cluster exists, follow kubernetes/README.md (or ansible/README.md for the add-ons).")
    if any(p.startswith("ansible/roles") for p in files):
        steps.append("Copy the Terraform ansible_inventory output into ansible/inventory/production.ini and run the playbook.")
    for family in ("render", "fly", "railway", "heroku", "vercel", "netlify", "cloudflare", "deno", "fastly", "firebase", "supabase",
                   "databricks", "snowflake", "coreweave", "lambdalabs", "runpod", "together", "huggingface", "cloudfoundry", "koyeb"):
        if "%s/README.md" % family in files:
            steps.append("Follow %s/README.md." % family)
    if ".github/workflows/deploy.yml" in files:
        steps.append("Set up the pipeline with docs/ci-setup.md.")
    return steps


def write(files, out_dir):
    """Write the files under out_dir with Unix line endings, in a fixed order."""
    from pathlib import Path
    root = Path(out_dir)
    base = root.resolve()
    for path in sorted(files):
        target = root / path
        if base not in target.resolve().parents:  # defence in depth: never write outside the output folder
            raise ValueError('refusing to write outside %s: %s' % (root, path))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(files[path].encode("utf-8"))
    return sorted(files)


def render_markdown(manifest):
    p = manifest["parity"]
    lines = ["# Infrastructure code for %s" % manifest["provider_name"], "",
             "Plan `%s`. %d files in %d groups." % (manifest["plan_fingerprint"], manifest["file_count"], len(manifest["families"])), "",
             "| Group | Files |", "|---|---|"]
    lines += ["| %s | %d |" % (f["label"], f["files"]) for f in manifest["families"]]
    lines += ["", "## Capability checklist", "",
              "The same %d checks are run for every provider. Score: %s%% (%d of %d applicable)." % (len(p["capabilities"]), p["score"], p["met"], p["applicable"]), "",
              "| ID | Capability | Result | Note |", "|---|---|---|---|"]
    for r in p["capabilities"]:
        lines.append("| %s | %s | %s | %s |" % (r["id"], r["title"], r["status"].replace("_", " "), r["reason"]))
    if manifest["next_steps"]:
        lines += ["", "## Next steps", ""] + ["%d. %s" % (i, s) for i, s in enumerate(manifest["next_steps"], 1)]
    return "\n".join(lines) + "\n"
