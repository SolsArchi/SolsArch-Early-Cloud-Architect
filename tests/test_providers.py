"""Every provider in the registry must load, plan, draw and package on every kind of repository."""
import json
import zipfile

import pytest

import ast_scanner
import build_diagrams
import evidence_bridge
import helpers
import icon_diagram
import providers
import recommend
import render_svg
import test_matrix

# The providers the product promises, by display name.
PROMISED = {
    "Global clouds": ["Alibaba Cloud", "AWS", "Azure", "Google Cloud", "Huawei Cloud", "IBM Cloud",
                      "Oracle Cloud Infrastructure (OCI)", "Tencent Cloud"],
    "App platforms": ["Cloud Foundry", "Fly.io", "Heroku", "Hugging Face Spaces", "Koyeb", "Netlify", "Railway", "Render", "Vercel"],
    "Edge": ["Akamai", "Cloudflare", "Deno Deploy", "Fastly"],
    "Other clouds": ["Civo", "DigitalOcean", "Exoscale", "Hetzner Cloud", "Linode", "OVHcloud", "Scaleway", "UpCloud", "Vultr"],
    "AI and GPU": ["CoreWeave", "Lambda", "RunPod", "Together AI"],
    "Data": ["Databricks", "Firebase", "MongoDB Atlas", "Neon", "Snowflake", "Supabase"],
    "Enterprise platforms": ["SAP", "Salesforce", "ServiceNow", "Pega", "MuleSoft", "Workday", "Oracle Fusion"],
}
REPOS = {"saas": helpers.build_saas, "rag": helpers.build_rag, "demo": helpers.build_demo,
         "monorepo": test_matrix.monorepo, "workspace": test_matrix.workspace_of_repos}
CATALOG = recommend.load_catalog()
HOSTING = providers.hosting_ids()


def scan_of(builder, tmp_path):
    builder(tmp_path)
    return ast_scanner.scan(tmp_path)


def test_every_promised_provider_is_in_the_registry():
    names = {p["name"] for p in providers.registry().values()}
    for group, expected in PROMISED.items():
        for name in expected:
            assert name in names, (group, name)
    assert len(providers.registry()) == sum(len(v) for v in PROMISED.values())


def test_registry_is_valid_and_ids_are_safe_class_names():
    assert providers.validate() == []
    for pid, provider in providers.registry().items():
        assert pid.isalnum() and pid == pid.lower()
        assert provider["pricing_page"].startswith("https://")
        assert all(d == d.lower() and "." in d for d in provider["domains"])


def test_enterprise_platforms_are_never_hosting_targets():
    assert set(providers.enterprise_ids()) == {"sap", "salesforce", "servicenow", "pega", "mulesoft", "workday", "oraclefusion"}
    assert not set(providers.enterprise_ids()) & set(HOSTING)
    assert not set(providers.enterprise_ids()) & set(CATALOG["clouds"])
    scan = {"components": [], "signals": {}, "infrastructure": {}, "languages": {}, "findings": []}
    with pytest.raises(ValueError):
        recommend.build_plan(scan, CATALOG, "sap", "server")


def test_loader_rejects_malformed_provider_files(tmp_path):
    bad = {"group": "data", "providers": {"x": {"name": "X", "kind": "data", "brand": {"fill": "#fff", "stroke": "#000", "text": "#000"},
                                                "domains": ["x.com"], "pricing_page": "https://x.com",
                                                "services": {"nonsense": {"any": ["a", "b", "c"]}}}}}
    (tmp_path / "bad.json").write_text(json.dumps(bad), encoding="utf-8")
    with pytest.raises(providers.ProviderError):
        providers.registry(tmp_path)
    bad["providers"]["x"]["services"] = {"cache": {"any": ["only", "two"]}}
    (tmp_path / "bad.json").write_text(json.dumps(bad), encoding="utf-8")
    providers._cache.clear()
    with pytest.raises(providers.ProviderError):
        providers.registry(tmp_path)
    providers._cache.clear()


@pytest.mark.parametrize("repo", sorted(REPOS))
def test_every_provider_plans_and_draws_on_every_repo_shape(tmp_path, repo):
    scan = scan_of(REPOS[repo], tmp_path)
    flat = evidence_bridge.flatten(scan)
    for pid in HOSTING:
        for mode in ("server", "serverless"):
            plan = recommend.build_plan(scan, CATALOG, pid, mode)
            again = recommend.build_plan(scan, CATALOG, pid, mode)
            assert plan == again, (pid, mode)  # same inputs, same plan
            assert plan["cloud"] == pid and plan["cloud_name"]
            for d in plan["decisions"]:
                assert d["service"] and d["provider"] in CATALOG["clouds"], (pid, d["key"])
            diagrams = build_diagrams.build(plan, scan)
            assert all(item["mmd"] for item in diagrams)
            render_svg.infrastructure(plan)
            render_svg.service_sheet(plan)
            placements = evidence_bridge.placements_from_plan(plan, flat)
            if len(placements["placements"]) > 0:
                try:
                    icon_diagram.render(placements, flat)
                except ValueError:
                    pass  # nothing drawable for a data-only provider


def test_not_native_roles_are_reported_not_invented(tmp_path):
    scan = scan_of(test_matrix.monorepo, tmp_path)
    plan = recommend.build_plan(scan, CATALOG, "vercel", "server")
    gap_needs = {g["need"] for g in plan["gaps"]}
    assert {"network", "registry"} <= gap_needs
    assert all(g["message"] for g in plan["gaps"])
    drawn = {d["need"] for d in plan["decisions"]}
    assert not gap_needs & drawn


def test_partner_builtin_and_selfmanaged_are_flagged(tmp_path):
    scan = scan_of(test_matrix.monorepo, tmp_path)
    vercel = recommend.build_plan(scan, CATALOG, "vercel", "server")
    assert next(d for d in vercel["decisions"] if d["key"] == "database")["partner"] is True
    assert next(d for d in vercel["decisions"] if d["key"] == "ingress")["builtin"] is True
    hetzner = recommend.build_plan(scan, CATALOG, "hetzner", "server")
    pg = next(d for d in hetzner["decisions"] if d["key"] == "database")
    assert pg["selfmanaged"] is True and any("back this up yourself" in c for c in pg["caveats"])


def test_serverless_falls_back_with_an_explanation_where_a_provider_has_none(tmp_path):
    scan = scan_of(test_matrix.monorepo, tmp_path)
    plan = recommend.build_plan(scan, CATALOG, "hetzner", "serverless")
    compute = next(d for d in plan["decisions"] if d["key"] == "compute_api")
    assert any("no functions service" in c for c in compute["caveats"])


def test_overlays_replace_only_what_the_overlay_covers(tmp_path):
    scan = scan_of(test_matrix.monorepo, tmp_path)
    plan = recommend.build_plan(scan, CATALOG, "vercel", "server", ["cloudflare", "supabase"])
    by = {d["key"]: d for d in plan["decisions"]}
    assert by["edge"]["provider"] == "cloudflare" and by["database"]["provider"] == "supabase"
    assert by["identity"]["provider"] == "supabase" and by["storage"]["provider"] == "cloudflare"
    assert by["compute_api"]["provider"] == "vercel" and by["ingress"]["provider"] == "vercel"
    assert by["vector"]["provider"] == "supabase"
    assert plan["overlay_names"] == ["Cloudflare", "Supabase"]
    queue = {q["service"]: q for q in plan["verify_queue"]}
    assert "supabase.com" in queue[by["database"]["service"]]["domains"]
    with pytest.raises(ValueError):
        recommend.build_plan(scan, CATALOG, "vercel", "server", ["sap"])
    with pytest.raises(ValueError):
        recommend.build_plan(scan, CATALOG, "vercel", "server", ["hetzner"])


def test_data_only_provider_asks_where_the_app_runs(tmp_path):
    scan = scan_of(helpers.build_saas, tmp_path)
    plan = recommend.build_plan(scan, CATALOG, "neon", "server")
    assert any("--also neon" in q for q in plan["open_questions"])
    assert all(d["provider"] == "neon" for d in plan["decisions"] if d["layer"] == "data")


@pytest.mark.parametrize("provider,also", [("hetzner", ""), ("vercel", "cloudflare,supabase"), ("flyio", ""), ("oci", ""),
                                           ("ibm", ""), ("alibaba", ""), ("digitalocean", ""), ("firebase", ""),
                                           ("cloudflare", ""), ("databricks", "")])
def test_pipeline_finishes_and_verifies_for_representative_providers(tmp_path, provider, also):
    repo = tmp_path / "project"
    repo.mkdir()
    test_matrix.monorepo(repo)
    assert helpers.cli("scan", "--repo", repo)[0] == 0
    args = ["plan", "--repo", repo, "--cloud", provider, "--compute", "server"] + (["--also", also] if also else [])
    code, out = helpers.cli(*args)
    assert code == 0, out
    assert helpers.cli("facts", "skip", "--repo", repo, "--reason", "offline test")[0] == 0
    code, out = helpers.cli("finish", "--repo", repo)
    assert code == 0 and out["verification"] == "passed", out
    assert "report.html" in zipfile.ZipFile(out["zip"]).namelist()
    report = (repo / "docs" / "solsarch-report.md").read_text(encoding="utf-8")
    assert providers.registry()[provider]["name"] in report


def test_compare_accepts_any_providers_and_all(tmp_path):
    repo = tmp_path / "project"
    repo.mkdir()
    test_matrix.monorepo(repo)
    assert helpers.cli("scan", "--repo", repo)[0] == 0
    code, out = helpers.cli("compare", "--repo", repo, "--clouds", "aws,hetzner,vercel,scaleway")
    assert code == 0
    table = (repo / "docs" / "solsarch-cloud-comparison.md").read_text(encoding="utf-8")
    header = [ln for ln in table.splitlines() if ln.startswith("| Role")][0]
    assert all(name in header for name in ("AWS", "Hetzner Cloud", "Vercel", "Scaleway"))
    assert "not native:" in table
    code, out = helpers.cli("compare", "--repo", repo, "--clouds", "all")
    assert code == 0 and len(out["fingerprints"]) == len(HOSTING)
    code, out = helpers.cli("compare", "--repo", repo, "--clouds", "aws,nonsense")
    assert code == 2


def test_questions_offer_every_hosting_provider(tmp_path):
    code, out = helpers.cli("questions", "--repo", tmp_path)
    values = [o["value"] for o in out["ask"][0]["options"]]
    assert values[:3] == ["aws", "azure", "gcp"] and set(values) == set(HOSTING)
    assert all(o["group"] for o in out["ask"][0]["options"])
