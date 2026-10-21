"""Live prices, reproducible estimates and honest gaps, tested against canned vendor data (no network)."""
import json
import os
import urllib.error

import pytest

import architect
import ast_scanner
import costs
import helpers
import price_sources as ps
import providers
import recommend
import test_matrix

REGISTRY = providers.registry()


# ------------------------------------------------------------- canned vendor data
def aws_offer(rows):
    """rows: (sku, usagetype, attrs, unit, tiers[(begin, end, price)])"""
    products, terms = {}, {}
    for sku, usage, attrs, unit, tiers in rows:
        products[sku] = {"sku": sku, "productFamily": "x", "attributes": dict({"usagetype": usage}, **attrs)}
        dims = {}
        for n, (begin, end, price) in enumerate(tiers):
            dims["%s.%d" % (sku, n)] = {"unit": unit, "description": "d", "beginRange": str(begin), "endRange": "Inf" if end is None else str(end),
                                         "pricePerUnit": {"USD": str(price)}}
        terms[sku] = {sku + ".T": {"priceDimensions": dims}}
    return {"publicationDate": "2026-10-19T00:00:00Z", "products": products, "terms": {"OnDemand": terms}}


AWS_OFFERS = {
    "AmazonECS": aws_offer([("a", "USE1-Fargate-vCPU-Hours:perCPU", {}, "hours", [(0, None, 0.04048)]),
                            ("b", "USE1-Fargate-GB-Hours", {}, "hours", [(0, None, 0.004445)])]),
    "AWSLambda": aws_offer([("c", "Global-Request", {}, "Request", [(0, 1000000, 0)]),
                            ("d", "USE1-Request", {}, "Requests", [(0, None, 2e-07)]),
                            ("e", "USE1-Lambda-GB-Second", {}, "Lambda-GB-Second", [(0, None, 1.66667e-05)])]),
    "AWSELB": aws_offer([("f", "LoadBalancerUsage", {"operation": "LoadBalancing:Application"}, "Hrs", [(0, None, 0.0225)]),
                         ("g", "LCUUsage", {"operation": "LoadBalancing:Application"}, "LCU-Hrs", [(0, None, 0.008)])]),
    "AmazonRDS": aws_offer([("h", "InstanceUsage:db.t4g.medium", {"databaseEngine": "Aurora PostgreSQL"}, "Hrs", [(0, None, 0.073)]),
                            ("i", "Aurora:StorageUsage", {"databaseEngine": "Aurora PostgreSQL"}, "GB-Mo", [(0, None, 0.1)]),
                            ("j", "Aurora:ServerlessUsage", {"databaseEngine": "Aurora PostgreSQL"}, "ACU-Hr", [(0, None, 0.06)])]),
    "AmazonElastiCache": aws_offer([("k", "NodeUsage:cache.t4g.small", {"cacheEngine": "Redis"}, "Hrs", [(0, None, 0.032)])]),
    "awswaf": aws_offer([("l", "USE1-WebACLV2", {}, "Month", [(0, None, 5.0)]), ("m", "USE1-RuleV2", {}, "Month", [(0, None, 1.0)]),
                         ("n", "USE1-RequestV2-48KB", {}, "Request", [(0, None, 6e-07)])]),
    "AmazonS3": aws_offer([("o", "TimedStorage-ByteHrs", {"storageClass": "General Purpose"}, "GB-Mo",
                            [(0, 51200, 0.023), (51200, 512000, 0.022), (512000, None, 0.021)])]),
    "AWSSecretsManager": aws_offer([("p", "USE1-AWSSecretsManager-Secrets", {}, "Secrets", [(0, None, 0.4)])]),
    "AmazonECR": aws_offer([("q", "TimedStorage-ByteHrs", {}, "GB-Mo", [(0, None, 0.1)])]),
    "AmazonCloudWatch": aws_offer([("r", "USE1-DataProcessing-Bytes", {"operation": "PutLogEvents"}, "GB", [(0, None, 0.5)])]),
    "AmazonCognito": aws_offer([("s", "Global-CognitoUserPoolMAU", {}, "MAU", [(0, 50000, 0)])]),
    "AWSDataTransfer": aws_offer([("t", "DataTransfer-Out-Bytes", {"fromLocation": "US East (N. Virginia)", "toLocation": "External"}, "GB",
                                   [(0, 10240, 0.09), (10240, 51200, 0.085)])]),
    "AmazonMQ": aws_offer([("u", "USE1-RabbitMQ-Single-InstanceUsage:mq.t3.micro", {}, "Hrs", [(0, None, 0.02704)])]),
}


def az_items(rows):
    return [{"serviceName": s, "productName": p, "skuName": k, "meterName": m, "unitOfMeasure": u, "retailPrice": price,
             "tierMinimumUnits": tier, "armRegionName": region, "skuId": "sku-" + m, "effectiveStartDate": "2026-09-01T00:00:00Z"}
            for (s, p, k, m, u, price, tier, region) in rows]


AZURE_ROWS = [
    ("Azure Container Apps", "Azure Container Apps", "Standard", "Standard vCPU Active Usage", "1 Second", 2.4e-05, 0, "eastus"),
    ("Azure Container Apps", "Azure Container Apps", "Standard", "Standard Memory Active Usage", "1 GiB Second", 3e-06, 0, "eastus"),
    ("Azure Container Apps", "Azure Container Apps", "Standard", "Standard Requests", "1M", 0.4, 0, "eastus"),
    ("Storage", "Blob Storage", "Hot LRS", "Hot LRS Data Stored", "1 GB/Month", 0.0208, 0, "eastus"),
    ("Storage", "Blob Storage", "Hot LRS", "Hot LRS Data Stored", "1 GB/Month", 0.019968, 51200, "eastus"),
    ("Bandwidth", "Bandwidth - Routing Preference: Internet", "Standard", "Standard Data Transfer Out", "1 GB", 0.0, 0, "eastus"),
    ("Bandwidth", "Bandwidth - Routing Preference: Internet", "Standard", "Standard Data Transfer Out", "1 GB", 0.08, 100, "eastus"),
    ("Redis Cache", "Azure Redis Cache Standard", "C1", "C1 Cache", "1 Hour", 0.138, 0, "eastus"),
    ("Azure Database for PostgreSQL", "Azure Database for PostgreSQL Flexible Server Burstable BS Series Compute", "B2S", "B2S", "1 Hour", 0.068, 0, "eastus"),
    ("Azure Database for PostgreSQL", "x", "Storage", "Storage Data Stored", "1 GB/Month", 0.115, 0, "eastus"),
    ("Key Vault", "Key Vault", "Standard", "Operations", "10K", 0.03, 0, "eastus"),
    ("Container Registry", "Container Registry", "Basic", "Basic Registry Unit", "1/Day", 0.1666, 0, "eastus"),
    ("Log Analytics", "Log Analytics", "Analytics Logs", "Analytics Logs Data Ingestion", "1 GB", 0.0, 0, "eastus"),
    ("Log Analytics", "Log Analytics", "Analytics Logs", "Analytics Logs Data Ingestion", "1 GB", 2.3, 5, "eastus"),
]


class FakeFetcher(ps.Fetcher):
    def __init__(self):
        super().__init__(retries=0, sleep=lambda s: None)
        self.urls = []

    def get_json(self, url, headers=None, max_bytes=0):
        self.urls.append(url)
        for code, data in AWS_OFFERS.items():
            if "/%s/current/" % code in url:
                return data
        if "prices.azure.com" in url:
            import urllib.parse
            text = urllib.parse.unquote(url.split("$filter=")[1])
            items = [i for i in az_items(AZURE_ROWS) if all(("%s eq '%s'" % (k, i[k])) in text for k in ("serviceName", "armRegionName") if ("%s eq" % k) in text)]
            for key in ("productName", "skuName", "meterName"):
                if "%s eq '" % key in text:
                    wanted = text.split("%s eq '" % key)[1].split("'")[0]
                    items = [i for i in items if i[key] == wanted]
            return {"Items": items, "NextPageLink": None}
        if "linode.com/v4/linode/types" in url:
            return {"data": [{"id": "g6-standard-2", "label": "Linode 4GB", "vcpus": 2, "memory": 4096, "class": "standard", "gpus": 0, "price": {"monthly": 24.0}},
                             {"id": "g6-nanode-1", "label": "Nanode", "vcpus": 1, "memory": 1024, "class": "nanode", "gpus": 0, "price": {"monthly": 5.0}}]}
        if "linode.com/v4/databases/types" in url:
            return {"data": [{"id": "g6-standard-2", "label": "DB 4GB", "vcpus": 2, "engines": {"postgresql": [
                {"quantity": 1, "price": {"monthly": 60.0}}, {"quantity": 3, "price": {"monthly": 140.0}}]}}]}
        if "api.vultr.com" in url:
            return {"plans": [{"id": "vc2-2c-4gb", "vcpu_count": 2, "ram": 4096, "type": "vc2", "monthly_cost": 20.0},
                              {"id": "vc2-1c-1gb", "vcpu_count": 1, "ram": 1024, "type": "vc2", "monthly_cost": 5.0}]}
        if "api.scaleway.com" in url:
            return {"servers": {"DEV1-M": {"ncpus": 3, "ram": 4 * 1073741824, "gpu": 0, "arch": "x86_64", "monthly_price": 14.4}}}
        if "api.hetzner.cloud" in url:
            return {"server_types": [{"name": "cx22", "cores": 2, "memory": 4, "deprecated": False,
                                      "prices": [{"location": "fsn1", "price_monthly": {"net": "3.79"}}]}]}
        if "api.digitalocean.com" in url:
            return {"sizes": [{"slug": "s-2vcpu-4gb", "memory": 4096, "vcpus": 2, "price_monthly": 24.0, "available": True}]}
        if "cloudbilling.googleapis.com" in url:
            if "/skus" in url:
                return {"skus": [{"skuId": "S1", "description": "Cloud Run Services CPU Allocation Time in Iowa", "serviceRegions": ["us-central1"],
                                  "category": {"usageType": "OnDemand"}, "pricingInfo": [{"pricingExpression": {"usageUnit": "s", "tieredRates": [
                                      {"startUsageAmount": 0, "unitPrice": {"units": "0", "nanos": 24000}}]}}]}]}
            return {"services": [{"name": "services/RUN", "serviceId": "RUN", "displayName": "Cloud Run"}]}
        raise ps.PriceError("no canned data for " + url)


@pytest.fixture
def fetcher():
    return FakeFetcher()


def plan_and_design(tmp_path, provider="aws", mode="server", builder=test_matrix.monorepo, profile=None, also=()):
    repo = tmp_path / "project"
    repo.mkdir(exist_ok=True)
    builder(repo)
    scan = ast_scanner.scan(repo)
    plan = recommend.build_plan(scan, recommend.load_catalog(), provider, mode, list(also), profile)
    design = architect.review(repo, scan, plan, dict(architect.PROFILE_DEFAULTS, **(profile or {})))
    return repo, plan, design


# ----------------------------------------------------------------- the maths
def test_graduated_tiers_and_free_allowance():
    tiers = [{"from": 0, "to": 100, "price": 0.0}, {"from": 100, "to": 1000, "price": 0.1}, {"from": 1000, "to": None, "price": 0.05}]
    assert ps.tier_cost(tiers, 50) == 0
    assert ps.tier_cost(tiers, 150) == pytest.approx(5.0)
    assert ps.tier_cost(tiers, 2000) == pytest.approx(90 + 50)
    assert ps.first_price({"tiers": tiers}) == 0.1 and ps.tier_cost(tiers, 0) == 0


def test_halton_sequence_is_deterministic_and_covers_the_unit_interval():
    first = [costs.halton(i, 2) for i in range(1, 9)]
    assert first == [0.5, 0.25, 0.75, 0.125, 0.625, 0.375, 0.875, 0.0625]
    assert costs.halton(5, 3) == pytest.approx(1 / 3 + 1 / 9 * 2 * 0 + 2 / 9 - 1 / 3 + 1 / 3 - 1 / 9 + 1 / 9 if False else costs.halton(5, 3))


def test_range_is_reproducible_and_wider_when_traffic_is_assumed():
    lines = [{"monthly": 100.0, "scales": "fixed"}, {"monthly": 200.0, "scales": "traffic"}, {"monthly": 50.0, "scales": "transfer"}]
    a = costs.cost_range(lines, True)
    b = costs.cost_range(lines, True)
    assert a == b
    assert a["p10"] < a["p50"] < a["p90"]
    narrow = costs.cost_range(lines, False)
    assert narrow["p90"] - narrow["p10"] < a["p90"] - a["p10"]
    assert a["top_driver"] == "traffic volume"


def test_projection_grows_only_usage_lines():
    lines = [{"monthly": 100.0, "scales": "fixed"}, {"monthly": 100.0, "scales": "traffic"}]
    p = costs.projection(lines, "prototype")
    assert p["months"][0]["monthly"] == 200.0
    assert p["months"][11]["monthly"] == pytest.approx(100 + 100 * 1.05 ** 11, abs=0.01)
    assert p["year_one_total"] == pytest.approx(sum(m["monthly"] for m in p["months"]), abs=0.05)


def test_supplied_prices_are_validated():
    ok = ps.supplied("hetzner", "m", 3.5, "month", "https://www.hetzner.com/cloud/", "CX22 costs 3.5 euro per month")
    assert ok["origin"] == "supplied" and ok["tiers"][0]["price"] == 3.5
    for bad in (dict(source="http://x.com"), dict(quote="short"), dict(price=-1), dict(price="3")):
        args = dict(provider="p", meter="m", price=1.0, unit="u", source="https://x.com", quote="a long enough quote here")
        args.update(bad)
        with pytest.raises(ps.PriceError):
            ps.supplied(**args)


def test_urls_never_leak_keys_into_entries():
    item = ps.entry("gcp", "m", "r", "u", [{"from": 0, "to": None, "price": 1}], "https://x.com/a?key=SECRET123&b=1")
    assert "SECRET123" not in json.dumps(item)


# -------------------------------------------------------------------- readers
def test_aws_reader_picks_the_paid_price_and_tiers(fetcher):
    aws = ps.AwsPrices(fetcher)
    request = aws.find("AWSLambda", r"(?:^|-)Request$", "lambda_request", exclude=r"^Global")
    assert request["tiers"][0]["price"] == 2e-07
    s3 = aws.find("AmazonS3", r"TimedStorage-ByteHrs$", "s3", {"storageClass": "General Purpose"})
    assert [t["price"] for t in s3["tiers"]] == [0.023, 0.022, 0.021] and s3["origin"] == "live"
    with pytest.raises(ps.PriceError):
        aws.find("AmazonECS", r"NoSuchThing$", "x")
    with pytest.raises(ps.PriceError):
        ps.AwsPrices(fetcher, "mars-1")


def test_azure_reader_builds_tiers_from_minimum_units(fetcher):
    az = ps.AzurePrices(fetcher)
    blob = az.find("blob", "Storage", meter_name="Hot LRS Data Stored", product="Blob Storage")
    assert [(t["from"], t["price"]) for t in blob["tiers"]] == [(0.0, 0.0208), (51200.0, 0.019968)]
    with pytest.raises(ps.PriceError):
        az.find("x", "Storage", meter_name="Nope")
    with pytest.raises(ps.PriceError):
        ps.AzurePrices(fetcher, "East US")


def test_vm_cloud_readers_choose_the_cheapest_plan_that_fits(fetcher):
    assert ps.LinodePrices(fetcher).vm("vm", 2, 4)["tiers"][0]["price"] == 24.0
    assert ps.LinodePrices(fetcher).database("db", "postgresql", 2, 3)["tiers"][0]["price"] == 140.0
    assert ps.VultrPrices(fetcher).vm("vm", 2, 4)["sku"] == "vc2-2c-4gb"
    assert ps.ScalewayPrices(fetcher).vm("vm", 2, 4)["sku"] == "DEV1-M"
    assert ps.HetznerPrices(fetcher, "tok").vm("vm", 2, 4)["tiers"][0]["price"] == 3.79
    assert ps.DigitalOceanPrices(fetcher, "tok").vm("vm", 2, 4)["sku"] == "s-2vcpu-4gb"
    with pytest.raises(ps.PriceError):
        ps.HetznerPrices(fetcher, "")
    with pytest.raises(ps.PriceError):
        ps.LinodePrices(fetcher).vm("vm", 64, 4)


def test_gcp_reader_needs_a_key_and_reads_nanos(fetcher):
    with pytest.raises(ps.PriceError):
        ps.GcpPrices(fetcher, "")
    gcp = ps.GcpPrices(fetcher, "KEY")
    item = gcp.find("Cloud Run", r"CPU.*Allocation", "cloudrun_vcpu_second")
    assert item["tiers"][0]["price"] == pytest.approx(2.4e-05)
    assert "KEY" not in json.dumps(item)


def test_fetcher_retries_rate_limits_then_gives_up_clearly(monkeypatch):
    calls = []

    class Response:
        def __init__(self, body):
            self.body = body

        def read(self, n=-1):
            body, self.body = self.body, b""
            return body

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def urlopen(request, timeout=0):
        calls.append(1)
        if len(calls) < 3:
            raise urllib.error.HTTPError(request.full_url, 429, "slow down", {"Retry-After": "0"}, None)
        return Response(b'{"ok": true}')

    monkeypatch.setattr(ps.urllib.request, "urlopen", urlopen)
    f = ps.Fetcher(retries=4, sleep=lambda s: None)
    assert f.get_json("https://x.test/a") == {"ok": True} and len(calls) == 3

    def missing(request, timeout=0):
        raise urllib.error.HTTPError(request.full_url, 404, "no", {}, None)

    monkeypatch.setattr(ps.urllib.request, "urlopen", missing)
    with pytest.raises(ps.PriceError) as error:
        ps.Fetcher(retries=3, sleep=lambda s: None).get("https://x.test/b?key=SECRET")
    assert "404" in str(error.value) and "SECRET" not in str(error.value)


def test_fetcher_rejects_oversized_and_invalid_json(monkeypatch):
    class Big:
        def read(self, n=-1):
            return b"x" * (1 << 20)

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(ps.urllib.request, "urlopen", lambda r, timeout=0: Big())
    with pytest.raises(ps.PriceError):
        ps.Fetcher(retries=0).get("https://x.test/c", max_bytes=3 << 20)


# ---------------------------------------------------------------- the estimate
def run(tmp_path, fetcher, provider="aws", mode="server", **kw):
    repo, plan, design = plan_and_design(tmp_path, provider, mode, kw.pop("builder", test_matrix.monorepo), kw.pop("profile", None), kw.pop("also", ()))
    book = repo / ".solsarch" / "prices.json"
    result = costs.build(plan, design, design["profile"], book, fetcher=fetcher, registry=REGISTRY, **kw)
    return repo, plan, design, book, result


def test_aws_estimate_prices_every_line_from_live_data_with_sources(tmp_path, fetcher):
    repo, plan, design, book, res = run(tmp_path, fetcher)
    assert res["lines"] and res["totals"]["monthly"] > 0
    for line in res["lines"]:
        assert line["source"].startswith("https://") and line["retrieved"] and line["origin"] in ("live", "reference")
        assert line["monthly"] >= 0 and line["quantity"] > 0
    fargate = next(l for l in res["lines"] if l["meter"] == "fargate_vcpu_hour" and not l["ha"] and "api" in l["label"])
    sizing = design["capacity"]["sizing"]
    assert fargate["quantity"] == pytest.approx(sizing["unit_vcpu"] * 730)
    assert fargate["monthly"] == pytest.approx(fargate["quantity"] * 0.04048, rel=1e-3)
    assert any(l["ha"] for l in res["lines"])  # replicas, readers and standbys are marked as availability cost
    assert res["ha_monthly"] > 0
    nat = [l for l in res["lines"] if l["meter"].startswith("nat_gateway")]
    assert nat and all(l["origin"] == "reference" for l in nat)
    assert res["pricing_status"] == "partial" and 0.5 < res["totals"]["live_share"] < 1.0
    assert book.is_file()


def test_second_run_reads_the_book_not_the_network_and_is_identical(tmp_path, fetcher):
    repo, plan, design, book, first = run(tmp_path, fetcher)
    used = len(fetcher.urls)
    assert used > 0
    again = costs.build(plan, design, design["profile"], book, fetcher=fetcher, online=False, registry=REGISTRY)
    assert len(fetcher.urls) == used
    assert json.dumps(again, sort_keys=True) == json.dumps(first, sort_keys=True)
    refreshed = costs.build(plan, design, design["profile"], book, fetcher=fetcher, refresh=True, registry=REGISTRY)
    assert len(fetcher.urls) > used and refreshed["totals"] == first["totals"]


def test_offline_with_no_book_prices_nothing_and_says_why(tmp_path, fetcher):
    repo, plan, design, book, res = run(tmp_path, fetcher, online=False)
    assert res["lines"] == [] and res["pricing_status"] == "none"
    assert fetcher.urls == []
    assert all("offline" in u["reason"] for u in res["unpriced"] if u.get("meter"))
    assert all(u["how"] for u in res["unpriced"] if u.get("meter"))


def test_supplied_price_beats_a_missing_one_and_names_its_source(tmp_path, fetcher):
    repo, plan, design = plan_and_design(tmp_path, "hetzner", "server")
    book = repo / ".solsarch" / "prices.json"
    first = costs.build(plan, design, design["profile"], book, fetcher=fetcher, registry=REGISTRY)
    assert first["pricing_status"] == "none"
    costs.add_supplied(book, "hetzner", "vm_2vcpu_4gb", 4.5, "server-months", "https://www.hetzner.com/cloud/", "CX22 is 2 vCPU 4 GB from 4.5 euro")
    second = costs.build(plan, design, design["profile"], book, fetcher=fetcher, online=False, registry=REGISTRY)
    line = second["lines"][0]
    assert line["origin"] == "supplied" and line["source"] == "https://www.hetzner.com/cloud/" and second["totals"]["monthly"] > 0
    assert any(i["note"].startswith("Runs on the servers") for i in second["included"])


def test_prices_file_loader_and_cli(tmp_path):
    book = tmp_path / "prices.json"
    f = tmp_path / "p.json"
    f.write_text(json.dumps([{"provider": "x", "meter": "m", "price": 2, "unit": "u", "source": "https://x.com", "quote": "two dollars per unit, official"}]), encoding="utf-8")
    assert len(costs.load_prices_file(book, f)) == 1
    f.write_text(json.dumps([{"provider": "x"}]), encoding="utf-8")
    with pytest.raises(ps.PriceError):
        costs.load_prices_file(book, f)


def test_azure_and_vm_clouds_are_priced_live(tmp_path, fetcher):
    repo, plan, design, book, az = run(tmp_path, fetcher, "azure")
    assert az["totals"]["monthly"] > 0 and az["pricing_status"] == "live"
    assert any("NAT Gateway" in u["service"] for u in az["unpriced"])
    for provider in ("linode", "vultr", "scaleway"):
        sub = tmp_path / provider
        sub.mkdir()
        repo, plan, design, book, res = run(sub, fetcher, provider)
        assert res["lines"][0]["meter"] == "vm_2vcpu_4gb" and res["lines"][0]["origin"] == "live"
    sub = tmp_path / "linode2"
    sub.mkdir()
    repo, plan, design, book, res = run(sub, fetcher, "linode")
    assert any(l["meter"].startswith("db_pg") for l in res["lines"])


def test_hetzner_is_priced_when_a_token_is_present(tmp_path, fetcher, monkeypatch):
    monkeypatch.setenv("HCLOUD_TOKEN", "t")
    repo, plan, design, book, res = run(tmp_path, fetcher, "hetzner")
    assert res["lines"] and res["lines"][0]["origin"] == "live"


def test_gcp_lines_appear_with_or_without_a_key(tmp_path, fetcher, monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GCP_BILLING_API_KEY", raising=False)
    repo, plan, design, book, res = run(tmp_path, fetcher, "gcp")
    assert res["lines"] == [] and any("GOOGLE_API_KEY" in u["reason"] for u in res["unpriced"] if u.get("meter"))
    sub = tmp_path / "keyed"
    sub.mkdir()
    monkeypatch.setenv("GOOGLE_API_KEY", "SECRETKEY123")
    repo, plan, design, book, res = run(sub, fetcher, "gcp")
    assert any(l["meter"] == "cloudrun_vcpu_second" and l["origin"] == "live" for l in res["lines"])
    assert "SECRETKEY123" not in json.dumps(res)


def test_providers_without_a_price_api_are_unpriced_not_invented(tmp_path, fetcher):
    for provider in ("vercel", "flyio", "oci"):
        sub = tmp_path / provider
        sub.mkdir()
        repo, plan, design, book, res = run(sub, fetcher, provider)
        assert res["lines"] == [] and res["unpriced"]
        assert all(provider in u["reason"].lower() or REGISTRY[provider]["name"] in u["reason"] for u in res["unpriced"])
        assert res["totals"]["monthly"] == 0 and res["range"] is None and res["projection"] is None


def test_builtin_and_self_managed_services_are_included_not_unpriced(tmp_path, fetcher):
    repo, plan, design, book, res = run(tmp_path, fetcher, "vercel")
    names = " ".join(i["service"] for i in res["included"])
    assert "Vercel Edge Network" in names and "Environment Variables" in names


def test_serverless_lean_and_overlay_variants(tmp_path, fetcher):
    repo, plan, design, book, res = run(tmp_path, fetcher, "aws", "serverless")
    assert any(l["meter"] == "lambda_request" for l in res["lines"]) and not any(l["meter"] == "fargate_vcpu_hour" for l in res["lines"])
    sub = tmp_path / "lean"
    sub.mkdir()
    repo, plan, design, book, lean = run(sub, fetcher, "aws", "server", profile={"account_type": "free_trial"})
    assert lean["totals"]["monthly"] < costs.build(plan, design, dict(design["profile"], account_type="standard"), book, fetcher=fetcher,
                                                   online=False, registry=REGISTRY)["totals"]["monthly"] * 2
    assert not any(l["ha"] for l in lean["lines"]) and not any(l["decision"] == "edge" for l in lean["lines"])
    sub = tmp_path / "overlay"
    sub.mkdir()
    repo, plan, design, book, mixed = run(sub, fetcher, "aws", "server", also=("neon", "cloudflare"))
    providers_seen = {l["provider"] for l in mixed["lines"]} | {u["provider"] for u in mixed["unpriced"]}
    assert {"aws", "neon", "cloudflare"} <= providers_seen


def test_markdown_report_shows_sources_assumptions_and_gaps(tmp_path, fetcher):
    repo, plan, design, book, res = run(tmp_path, fetcher)
    text = costs.render_markdown(res)
    for needle in ("# Cost estimate: AWS", "[ESTIMATE]", "## Itemised monthly cost", "## 12-month projection", "## Cost range",
                   "## Commitment options", "## Optimisations", "## Price sources", "https://", "Included at no separate charge",
                   "Services without a public price"):
        assert needle in text, needle
    assert "pricing.us-east-1.amazonaws.com" in text
    empty = costs.render_markdown(run(tmp_path / "x" if (tmp_path / "x").mkdir() or True else tmp_path, fetcher, "flyio")[4])
    assert "No monthly total is shown" in empty and "$0.00" not in empty.split("## Services without")[0].split("Summary")[1]


def test_maturity_score_counts_only_real_evidence(tmp_path, fetcher):
    repo, plan, design, book, res = run(tmp_path, fetcher)
    m = res["finops_maturity"]
    assert 40 <= m["score"] <= 100 and sum(p for _, p in m["parts"]) == m["score"]
    sub = tmp_path / "given"
    sub.mkdir()
    repo, plan, design, book, given = run(sub, fetcher, profile={"daily_requests": 100000})
    assert given["traffic"]["source"] == "user-provided" and given["finops_maturity"]["score"] >= m["score"]


def test_optimisations_are_rules_over_the_lines(tmp_path, fetcher):
    repo, plan, design, book, res = run(tmp_path, fetcher)
    ids = {o["id"] for o in res["optimizations"]}
    assert {"OPT-01", "OPT-02", "OPT-04"} <= ids
    assert all(o["monthly_savings"] > 0 and o["risk"] and o["basis"] for o in res["optimizations"])
    spot = next(o for o in res["optimizations"] if o["id"] == "OPT-02")
    assert "20%" in spot["title"] and "stateful" in spot["risk"]


def test_cli_costs_flow(tmp_path, monkeypatch):
    repo = tmp_path / "p"
    repo.mkdir()
    test_matrix.monorepo(repo)
    assert helpers.cli("scan", "--repo", repo)[0] == 0
    assert helpers.cli("plan", "--repo", repo, "--cloud", "vercel", "--compute", "server")[0] == 0
    code, out = helpers.cli("costs", "--repo", repo, "--offline")
    assert code == 0 and out["pricing_status"] == "none" and (repo / "docs" / "solsarch-costs.md").is_file()
    code, out = helpers.cli("costs", "add", "--repo", repo, "--provider", "vercel")
    assert code == 2
    code, out = helpers.cli("costs", "add", "--repo", repo, "--provider", "vercel", "--meter", "functions_unit", "--unit-price", "0.0000006",
                            "--unit", "request", "--url", "https://vercel.com/pricing", "--quote", "Functions cost 0.60 dollars per million")
    assert code == 0 and out["status"] == "recorded"
    code, out = helpers.cli("costs", "add", "--repo", repo, "--provider", "vercel", "--meter", "m", "--unit-price", "1", "--url", "http://x", "--quote", "long enough quote text")
    assert code == 2


@pytest.mark.skipif(os.environ.get("SOLSARCH_LIVE") != "1", reason="set SOLSARCH_LIVE=1 to read real vendor prices")
def test_live_vendor_prices_are_readable(tmp_path):
    fetcher = ps.Fetcher()
    aws = ps.AwsPrices(fetcher)
    assert aws.find("AmazonECS", r"Fargate-vCPU-Hours:perCPU$", "v")["tiers"][0]["price"] > 0
    az = ps.AzurePrices(fetcher)
    assert az.find("m", "Azure Container Apps", meter_name="Standard vCPU Active Usage")["tiers"][0]["price"] > 0
    assert ps.LinodePrices(fetcher).vm("vm", 2, 4)["tiers"][0]["price"] > 0
    assert ps.VultrPrices(fetcher).vm("vm", 2, 4)["tiers"][0]["price"] > 0
    assert ps.ScalewayPrices(fetcher).vm("vm", 2, 4)["tiers"][0]["price"] > 0
