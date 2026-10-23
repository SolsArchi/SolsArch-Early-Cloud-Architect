#!/usr/bin/env python3
"""A GitHub Actions pipeline that builds the images, scans the infrastructure code and applies it with a manual approval.

The pipeline authenticates with OpenID Connect where the cloud supports it (no long-lived keys in GitHub), runs the same
checks the skill ran (format, validate, Checkov, Trivy), saves the plan as an artifact, and applies only from the main
branch after an approval on the `production` environment. Provider credentials come from repository secrets named in the
README, never from the file.
"""
import re

from iac_common import Files, T, shq
from iac_k8s import dump

# Every third-party action is pinned to a commit, because a tag such as v4 can be moved to different code later. The tag it was
# resolved from is written next to each pin so a person can see what it is and update it on purpose.
PINS = {
    "actions/checkout": ("v4", "11d5960a326750d5838078e36cf38b85af677262"),
    "actions/setup-python": ("v5", "a26af69be951a213d495a4c3e4e4022e16d87065"),
    "github/codeql-action": ("v3", "4bfa242aa49c5f7c6770b98670d354a7dfac05f8"),
    "aws-actions/configure-aws-credentials": ("v4", "7474bc4690e29a8392af63c5b98e7449536d5c3a"),
    "azure/login": ("v2", "7184910d9eb2b1c5e48f7073824a90609bb9b6d6"),
    "google-github-actions/auth": ("v2", "c200f3691d83b41bf9bbd8638997a462592937ed"),
    "hashicorp/setup-terraform": ("v3", "b9cd54a3c349d3f38e8881555d616ced269862dd"),
    "aquasecurity/trivy-action": ("v0.28.0", "915b19bbe73b92a6cf82a1bc12b087c9a19a5fe2"),
    "bridgecrewio/checkov-action": ("v12", "c3e106d5f8f4253a6de5110efb41a080f83db294"),
    "actions/upload-artifact": ("v4", "ea165f8d65b6e75b540449e92b4886f43607fa02"),
    "actions/download-artifact": ("v4", "d3f86a106a0bac45b974a628896c90dbdf5c8093"),
    "infracost/actions": ("v3", "e9d6e6cd65e168e76b0de50ff9957d2fe8bb1832"),
}
# The gate downloads SolsArch itself. It uses a release tag, never the moving default branch, so a change upstream cannot reach
# a pipeline that holds write permissions.
SKILL_REF = "v1.0.0"


def use(action):
    """owner/repo[/path] pinned to a commit."""
    owner_repo = "/".join(action.split("/")[:2])
    return "%s@%s" % (action, PINS[owner_repo][1])


def annotate(text):
    """Add the tag each pinned commit came from as a comment, after the YAML has been written."""
    by_sha = {sha: tag for tag, sha in PINS.values()}
    return re.sub(r"(uses: \S+@([0-9a-f]{40}))", lambda m: "%s  # %s" % (m.group(1), by_sha.get(m.group(2), "")), text)


CHECKOUT = {"uses": use("actions/checkout"), "with": {"persist-credentials": False}}

OIDC = {
    "aws": [{"name": "Sign in to AWS", "uses": use("aws-actions/configure-aws-credentials"),
             "with": {"role-to-assume": "${{ vars.AWS_ROLE_ARN }}", "aws-region": "${{ vars.AWS_REGION }}"}}],
    "azure": [{"name": "Sign in to Azure", "uses": use("azure/login"),
               "with": {"client-id": "${{ vars.AZURE_CLIENT_ID }}", "tenant-id": "${{ vars.AZURE_TENANT_ID }}", "subscription-id": "${{ vars.AZURE_SUBSCRIPTION_ID }}"}}],
    "gcp": [{"name": "Sign in to Google Cloud", "uses": use("google-github-actions/auth"),
             "with": {"workload_identity_provider": "${{ vars.GCP_WORKLOAD_IDENTITY_PROVIDER }}", "service_account": "${{ vars.GCP_SERVICE_ACCOUNT }}"}}],
}
TOKEN_ENV = {
    "hetzner": {"HCLOUD_TOKEN": "${{ secrets.HCLOUD_TOKEN }}"},
    "linode": {"LINODE_TOKEN": "${{ secrets.LINODE_TOKEN }}"},
    "akamai": {"LINODE_TOKEN": "${{ secrets.LINODE_TOKEN }}"},
    "vultr": {"VULTR_API_KEY": "${{ secrets.VULTR_API_KEY }}"},
    "civo": {"CIVO_TOKEN": "${{ secrets.CIVO_TOKEN }}"},
    "upcloud": {"UPCLOUD_USERNAME": "${{ secrets.UPCLOUD_USERNAME }}", "UPCLOUD_PASSWORD": "${{ secrets.UPCLOUD_PASSWORD }}"},
    "exoscale": {"EXOSCALE_API_KEY": "${{ secrets.EXOSCALE_API_KEY }}", "EXOSCALE_API_SECRET": "${{ secrets.EXOSCALE_API_SECRET }}"},
    "digitalocean": {"DIGITALOCEAN_TOKEN": "${{ secrets.DIGITALOCEAN_TOKEN }}"},
    "scaleway": {"SCW_ACCESS_KEY": "${{ secrets.SCW_ACCESS_KEY }}", "SCW_SECRET_KEY": "${{ secrets.SCW_SECRET_KEY }}"},
    "ovhcloud": {"OVH_APPLICATION_KEY": "${{ secrets.OVH_APPLICATION_KEY }}", "OVH_APPLICATION_SECRET": "${{ secrets.OVH_APPLICATION_SECRET }}",
                 "OVH_CONSUMER_KEY": "${{ secrets.OVH_CONSUMER_KEY }}"},
    "oci": {"TF_VAR_tenancy_ocid": "${{ secrets.OCI_TENANCY_OCID }}", "OCI_USER_OCID": "${{ secrets.OCI_USER_OCID }}", "OCI_FINGERPRINT": "${{ secrets.OCI_FINGERPRINT }}",
            "OCI_PRIVATE_KEY": "${{ secrets.OCI_PRIVATE_KEY }}"},
    "alibaba": {"ALICLOUD_ACCESS_KEY": "${{ secrets.ALICLOUD_ACCESS_KEY }}", "ALICLOUD_SECRET_KEY": "${{ secrets.ALICLOUD_SECRET_KEY }}"},
    "tencent": {"TENCENTCLOUD_SECRET_ID": "${{ secrets.TENCENTCLOUD_SECRET_ID }}", "TENCENTCLOUD_SECRET_KEY": "${{ secrets.TENCENTCLOUD_SECRET_KEY }}"},
    "huawei": {"HW_ACCESS_KEY": "${{ secrets.HW_ACCESS_KEY }}", "HW_SECRET_KEY": "${{ secrets.HW_SECRET_KEY }}"},
    "ibm": {"IC_API_KEY": "${{ secrets.IC_API_KEY }}"},
}
TERRAFORM_PROVIDERS = tuple(["aws", "azure", "gcp"] + list(TOKEN_ENV))


def gate_files(model, provider, tf_dir, uses_terraform, env):
    """A pull-request gate that re-runs the SolsArch checks, and (for Terraform) a weekly drift check."""
    files = Files(model.fingerprint)
    nl = chr(10)
    run = nl.join(["git -c advice.detachedHead=false clone --depth 1 --branch %s https://github.com/SolsArchi/SolsArch-Early-Cloud-Architect.git .solsarch-skill" % SKILL_REF,
                   "SKILL=.solsarch-skill/solsarch-early-cloud-architect/scripts/solsarch.py",
                   "python $SKILL run --repo . --cloud %s --compute %s" % (provider, model.mode),
                   "python $SKILL scorecard --repo .",
                   "python $SKILL gate --repo . --sarif solsarch.sarif --comment solsarch-comment.md"])
    steps = [dict(CHECKOUT), {"uses": use("actions/setup-python"), "with": {"python-version": "3.12"}},
             {"name": "Run the SolsArch checks", "id": "gate", "run": run, "env": {"SOLSARCH_OFFLINE": "1"}, "continue-on-error": True},
             {"name": "Show findings on the pull request", "uses": use("github/codeql-action/upload-sarif"), "if": "always()", "with": {"sarif_file": "solsarch.sarif"}},
             {"name": "Comment on the pull request", "if": "always() && github.event_name == 'pull_request'", "env": {"GH_TOKEN": "${{ github.token }}"},
              "run": "gh pr comment ${{ github.event.pull_request.number }} --body-file solsarch-comment.md"},
             {"name": "Fail when the gate failed", "if": "steps.gate.outcome == 'failure'", "run": "exit 1"}]
    if uses_terraform:
        steps.insert(5, {"name": "Set up Infracost (needs the INFRACOST_API_KEY secret)", "if": "env.INFRACOST_API_KEY != ''",
                         "uses": use("infracost/actions/setup"), "with": {"api-key": "${{ secrets.INFRACOST_API_KEY }}"},
                         "env": {"INFRACOST_API_KEY": "${{ secrets.INFRACOST_API_KEY }}"}})
        steps.insert(6, {"name": "Show the cost change", "if": "env.INFRACOST_API_KEY != ''", "env": {"INFRACOST_API_KEY": "${{ secrets.INFRACOST_API_KEY }}"},
                         "run": "infracost breakdown --path %s --format table" % shq(tf_dir)})
    gate = {"name": "SolsArch gate", "on": {"pull_request": {}}, "permissions": {"contents": "read", "pull-requests": "write", "security-events": "write"},
            "jobs": {"gate": {"runs-on": "ubuntu-latest", "steps": steps}}}
    files.add(".github/workflows/solsarch-gate.yml", annotate(dump(gate)))
    if uses_terraform:
        drift_steps = [dict(CHECKOUT), {"uses": use("hashicorp/setup-terraform"), "with": {"terraform_version": "1.9.8"}}] + OIDC.get(provider, []) + [
            {"name": "Initialise", "run": "terraform init -backend-config=backend.hcl", "working-directory": tf_dir, **({"env": env} if env else {})},
            {"name": "Compare the cloud with the code", "id": "plan", "run": "terraform plan -input=false -detailed-exitcode -lock=false", "working-directory": tf_dir,
             "continue-on-error": True, **({"env": env} if env else {})},
            {"name": "Open an issue when they differ", "if": "steps.plan.outputs.exitcode == '2'", "env": {"GH_TOKEN": "${{ github.token }}"},
             "run": "gh issue create --title \"Cloud drift detected\" --body \"terraform plan found changes between the code and the cloud. Open the run: ${{ github.server_url }}/${{ github.repository }}/actions/runs/${{ github.run_id }}\""}]
        drift = {"name": "Drift check", "on": {"schedule": [{"cron": "17 5 * * 1"}], "workflow_dispatch": {}},
                 "permissions": {"contents": "read", "issues": "write", "id-token": "write"}, "jobs": {"drift": {"runs-on": "ubuntu-latest", "steps": drift_steps}}}
        files.add(".github/workflows/drift.yml", annotate(dump(drift)))
    return files


def generate(model, provider, tf_dir="terraform"):
    files = Files(model.fingerprint)
    env = dict(TOKEN_ENV.get(provider, {}))
    uses_terraform = provider in TERRAFORM_PROVIDERS
    jobs = {}
    jobs["images"] = {
        "name": "Build and scan images", "runs-on": "ubuntu-latest", "permissions": {"contents": "read", "id-token": "write"},
        "steps": [dict(CHECKOUT),
                  {"name": "Build every unit", "run": "for d in " + " ".join(shq(u["folder"].strip("./") or ".") for u in model.units if u["kind"] != "static")
                   + '; do docker build "$d" -t "$(basename "$d"):${{ github.sha }}"; done'},
                  {"name": "Scan the images", "uses": use("aquasecurity/trivy-action"),
                   "with": {"scan-type": "fs", "scan-ref": ".", "severity": "HIGH,CRITICAL", "exit-code": "1", "ignore-unfixed": True}}]}
    if uses_terraform:
        steps = [dict(CHECKOUT), {"uses": use("hashicorp/setup-terraform"), "with": {"terraform_version": "1.9.8"}}] + OIDC.get(provider, []) + [
            {"name": "Check format", "run": "terraform fmt -check -recursive", "working-directory": tf_dir},
            {"name": "Initialise", "run": "terraform init -backend-config=backend.hcl", "working-directory": tf_dir},
            {"name": "Validate", "run": "terraform validate", "working-directory": tf_dir},
            {"name": "Security scan", "uses": use("bridgecrewio/checkov-action"), "with": {"directory": tf_dir, "framework": "terraform", "soft_fail": False}},
            {"name": "Plan", "run": "terraform plan -input=false -out=tfplan", "working-directory": tf_dir},
            {"name": "Keep the plan", "uses": use("actions/upload-artifact"), "with": {"name": "tfplan", "path": tf_dir + "/tfplan"}}]
        for s in steps:
            if s.get("working-directory") and env:
                s["env"] = env
        jobs["plan"] = {"name": "Plan", "runs-on": "ubuntu-latest", "needs": ["images"], "permissions": {"contents": "read", "id-token": "write"}, "steps": steps}
        apply_steps = [dict(CHECKOUT), {"uses": use("hashicorp/setup-terraform"), "with": {"terraform_version": "1.9.8"}}] + OIDC.get(provider, []) + [
            {"name": "Get the plan", "uses": use("actions/download-artifact"), "with": {"name": "tfplan", "path": tf_dir}},
            {"name": "Initialise", "run": "terraform init -backend-config=backend.hcl", "working-directory": tf_dir, **({"env": env} if env else {})},
            {"name": "Apply the saved plan", "run": "terraform apply -input=false tfplan", "working-directory": tf_dir, **({"env": env} if env else {})}]
        jobs["apply"] = {"name": "Apply (needs approval)", "runs-on": "ubuntu-latest", "needs": ["plan"], "if": "github.ref == 'refs/heads/main'",
                         "environment": "production", "permissions": {"contents": "read", "id-token": "write"}, "steps": apply_steps}
    workflow = {"name": "Deploy %s" % model.project, "on": {"push": {"branches": ["main"]}, "pull_request": {}},
                "concurrency": {"group": "deploy-${{ github.ref }}", "cancel-in-progress": False}, "permissions": {"contents": "read"}, "jobs": jobs}
    text = dump(workflow)
    files.add(".github/workflows/deploy.yml", annotate(text))
    files.update(gate_files(model, provider, tf_dir, uses_terraform, env))
    secrets = sorted({v.split("secrets.")[1].rstrip(" }") for v in env.values() if "secrets." in v})
    variables = sorted({v.split("vars.")[1].rstrip(" }") for s in OIDC.get(provider, []) for v in s["with"].values() if "vars." in v})
    files.add("docs/ci-setup.md", T("""
        # Pipeline setup for @@project@@

        The workflow in `.github/workflows/deploy.yml` builds and scans the images, then (for infrastructure that Terraform manages)
        formats, validates, scans and plans the code, and applies the saved plan only from `main` after someone approves the
        `production` environment.

        1. Create an environment named `production` in the repository settings and add required reviewers.
        2. @@creds@@
        3. Create the state bucket described in `@@tf@@/README.md` and commit `backend.hcl` (it holds no secret).

        ## Supply chain

        - Every third-party action is pinned to a full commit (the tag it came from is in a comment). Update them on purpose, for example with Dependabot's `github-actions` ecosystem.
        - The gate workflow downloads SolsArch at the release tag `@@ref@@`, not the moving default branch, so a later change upstream cannot run inside a pipeline that has write permissions. Change the tag in `.github/workflows/solsarch-gate.yml` when you choose to upgrade.
    """, project=model.project, tf=tf_dir, ref=SKILL_REF,
        creds=("Add these repository secrets: %s." % ", ".join(secrets) if secrets else
               ("Add these repository variables for OpenID Connect sign-in: %s. No long-lived key is stored in GitHub." % ", ".join(variables) if variables else
                "This platform deploys with its own CLI; add its token as a repository secret and call it in a final step."))))
    return files
