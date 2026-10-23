#!/usr/bin/env python3
"""Where prices come from: vendor price APIs, prices you supply, and a few dated reference values.

Standard library only. Each reader returns price entries; none of them guesses. An entry always says where it came
from (a URL), when it was read, and whether it is live, supplied or a dated reference value:

  live        read from the vendor's own price API during this run (AWS Price List, Azure Retail Prices,
              Linode, Vultr and Scaleway public APIs, Google Cloud Billing Catalog with your API key)
  supplied    given by you or your agent with `costs add` or --prices FILE, each with a URL and a quote
  reference   a dated value written into SolsArch for a meter the vendor publishes only in very large files

Network access goes through one function (Fetcher.get) so tests can replace it. Large files are streamed to a
temporary file with a size cap. HTTP 429 and 5xx answers are retried with backoff.

Entry shape:
  {"provider", "meter", "region", "unit", "currency", "tiers": [{"from": 0, "to": None, "price": 0.04048}],
   "source": URL, "retrieved": "YYYY-MM-DD", "origin": "live|supplied|reference", "sku": ..., "description": ...}
"""
import datetime
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

import safe_http  # noqa: F401  (installs the https-only redirect rules)

USER_AGENT = "SolsArch-costs/1.0 (+https://github.com/SolsArchi/SolsArch-Early-Cloud-Architect)"
MAX_JSON_BYTES = 90 * 1024 * 1024
ORIGINS = ("live", "supplied", "reference")


class PriceError(Exception):
    """A price could not be read; the message says why and is safe to show to the user."""


def today():
    return datetime.date.today().isoformat()


class Fetcher:
    """HTTP GET with retries. Replace `get` in tests."""

    def __init__(self, timeout=30, retries=4, pause=0.0, sleep=time.sleep):
        self.timeout, self.retries, self.pause, self.sleep = timeout, retries, pause, sleep
        self.requests = 0

    def get(self, url, headers=None, max_bytes=MAX_JSON_BYTES):
        """Return the body as bytes. Raise PriceError after the retries are used up."""
        try:
            safe_http.checked(url)
        except ValueError as error:
            raise PriceError(str(error))
        request = urllib.request.Request(url, headers=dict({"User-Agent": USER_AGENT, "Accept": "application/json"}, **(headers or {})))
        last = None
        for attempt in range(self.retries + 1):
            if self.pause:
                self.sleep(self.pause)
            self.requests += 1
            try:
                # Bandit B310 note: only https is opened (the address was checked and redirects are limited by safe_http)
                with urllib.request.urlopen(request, timeout=self.timeout) as response:  # nosec B310
                    return self._read(response, max_bytes)
            except urllib.error.HTTPError as error:
                last = "HTTP %s" % error.code
                if error.code in (401, 403, 404, 400):
                    raise PriceError("%s from %s" % (last, _safe(url)))
                if error.code == 429 or error.code >= 500:
                    delay = _retry_after(error) or min(30, 2 ** attempt * 2)
                    self.sleep(delay)
                    continue
                raise PriceError("%s from %s" % (last, _safe(url)))
            except (urllib.error.URLError, TimeoutError, OSError) as error:
                last = str(getattr(error, "reason", error))
                self.sleep(min(10, 2 ** attempt))
        raise PriceError("could not read %s (%s)" % (_safe(url), last))

    @staticmethod
    def _read(response, max_bytes):
        chunks, total = [], 0
        while True:
            block = response.read(1 << 20)
            if not block:
                break
            total += len(block)
            if total > max_bytes:
                raise PriceError("response larger than %d MB" % (max_bytes >> 20))
            chunks.append(block)
        return b"".join(chunks)

    def get_json(self, url, headers=None, max_bytes=MAX_JSON_BYTES):
        body = self.get(url, headers, max_bytes)
        try:
            return json.loads(body.decode("utf-8"))
        except ValueError:
            raise PriceError("%s did not return JSON" % _safe(url))


def _retry_after(error):
    try:
        return min(60, float(error.headers.get("Retry-After")))
    except (TypeError, ValueError, AttributeError):
        return None


def _safe(url):
    """The URL without any key or token in its query string."""
    return re.sub(r"(?i)([?&](?:key|api_key|apikey|token|access_token)=)[^&]+", r"\1REDACTED", url)


def entry(provider, meter, region, unit, tiers, source, origin="live", sku=None, description=None, currency="USD", retrieved=None):
    if origin not in ORIGINS:
        raise ValueError("origin must be one of %s" % ", ".join(ORIGINS))
    return {"provider": provider, "meter": meter, "region": region, "unit": unit, "currency": currency,
            "tiers": tiers, "source": _safe(source), "retrieved": retrieved or today(), "origin": origin,
            "sku": sku, "description": (description or "")[:140]}


def tier_cost(tiers, quantity):
    """Cost of `quantity` units under graduated tiers [{"from", "to", "price"}]."""
    remaining, total = float(quantity), 0.0
    for tier in sorted(tiers, key=lambda t: t["from"]):
        if remaining <= 0:
            break
        upper = tier["to"] if tier["to"] is not None else float("inf")
        span = min(remaining, max(0.0, upper - tier["from"]))
        total += span * tier["price"]
        remaining -= span
        if tier["to"] is None:
            break
    return total


def first_price(item):
    """The price a single unit costs once any free allowance is used up."""
    paid = [t["price"] for t in sorted(item["tiers"], key=lambda t: t["from"]) if t["price"] > 0]
    return paid[0] if paid else 0.0


# ------------------------------------------------------------------------- AWS
AWS_OFFER_URL = "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/%s/current/%s/index.json"
AWS_REGIONS = {"us-east-1", "us-east-2", "us-west-1", "us-west-2", "eu-west-1", "eu-west-2", "eu-west-3", "eu-central-1",
               "eu-north-1", "eu-south-1", "ap-south-1", "ap-southeast-1", "ap-southeast-2", "ap-northeast-1",
               "ap-northeast-2", "ca-central-1", "sa-east-1", "me-south-1", "af-south-1"}


class AwsPrices:
    """Reads AWS Price List files (public, no account needed). One file per service and region."""

    def __init__(self, fetcher, region="us-east-1"):
        if region not in AWS_REGIONS:
            raise PriceError("AWS region '%s' is not in the supported list; use one of: %s" % (region, ", ".join(sorted(AWS_REGIONS))))
        self.fetcher, self.region = fetcher, region
        self._offers = {}

    def offer(self, code):
        if code not in self._offers:
            url = AWS_OFFER_URL % (code, self.region)
            self._offers[code] = (self.fetcher.get_json(url), url)
        return self._offers[code]

    def find(self, code, usagetype, meter, attrs=None, unit_hint=None, exclude=None):
        """The first on-demand price whose usagetype matches the regular expression and whose attributes equal `attrs`."""
        data, url = self.offer(code)
        rx = re.compile(usagetype)
        skip = re.compile(exclude) if exclude else None
        best = None
        for sku, product in sorted(data["products"].items()):
            a = product.get("attributes", {})
            usage = a.get("usagetype", "")
            if not rx.search(usage) or (skip and skip.search(usage)):
                continue
            if attrs and any(a.get(k) != v for k, v in attrs.items()):
                continue
            terms = (data.get("terms", {}).get("OnDemand", {}) or {}).get(sku)
            if not terms:
                continue
            dims = []
            for term in terms.values():
                dims.extend(term["priceDimensions"].values())
            if unit_hint and not any(unit_hint.lower() in d["unit"].lower() for d in dims):
                continue
            tiers = [{"from": float(d.get("beginRange") or 0),
                      "to": None if d.get("endRange") in (None, "Inf") else float(d["endRange"]),
                      "price": float(d["pricePerUnit"].get("USD", 0))} for d in sorted(dims, key=lambda d: float(d.get("beginRange") or 0))]
            candidate = entry("aws", meter, self.region, dims[0]["unit"], tiers, url, "live", sku, dims[0].get("description"),
                              retrieved=(data.get("publicationDate") or today())[:10])
            if best is None or usage < best[0]:
                best = (usage, candidate)
        if best is None:
            raise PriceError("no AWS price matched %s in %s for %s" % (usagetype, code, meter))
        return best[1]


# ----------------------------------------------------------------------- Azure
AZURE_URL = "https://prices.azure.com/api/retail/prices?currencyCode=USD&$filter="


class AzurePrices:
    """Reads the Azure Retail Prices API (public, no account needed)."""

    def __init__(self, fetcher, region="eastus"):
        if not re.fullmatch(r"[a-z0-9]+", region):
            raise PriceError("Azure region '%s' should be an ARM name such as eastus or northeurope" % region)
        self.fetcher, self.region = fetcher, region
        self._cache = {}

    def query(self, filter_text, pause=0.4):
        key = filter_text
        if key in self._cache:
            return self._cache[key]
        url = AZURE_URL + urllib.parse.quote(filter_text)
        items, pages = [], 0
        while url and pages < 12:
            data = self.fetcher.get_json(url)
            items.extend(data.get("Items", []))
            url = data.get("NextPageLink")
            pages += 1
            if url:
                time.sleep(pause)
        self._cache[key] = items
        return items

    def find(self, meter, service, meter_name=None, product=None, sku=None, region=None, unit=None, price_type="Consumption", extra=None):
        regions = region if isinstance(region, (list, tuple)) else [region or self.region]
        last = None
        for candidate in regions:
            try:
                return self._find(meter, service, meter_name, product, sku, candidate, unit, price_type, extra)
            except PriceError as error:
                last = error
        raise last

    def _find(self, meter, service, meter_name, product, sku, region, unit, price_type, extra):
        clauses = ["serviceName eq '%s'" % service, "armRegionName eq '%s'" % region, "priceType eq '%s'" % price_type]
        if meter_name:
            clauses.append("meterName eq '%s'" % meter_name)
        if product:
            clauses.append("productName eq '%s'" % product)
        if sku:
            clauses.append("skuName eq '%s'" % sku)
        if extra:
            clauses.append(extra)
        items = self.query(" and ".join(clauses))
        if unit:
            items = [i for i in items if unit.lower() in i.get("unitOfMeasure", "").lower()]
        if not items:
            raise PriceError("no Azure price for %s (%s) in %s" % (meter, meter_name or product or service, region))
        items = sorted(items, key=lambda i: (i.get("tierMinimumUnits", 0), i["retailPrice"]))
        by_tier = {}
        for item in items:
            by_tier.setdefault(item.get("tierMinimumUnits", 0), item)
        starts = sorted(by_tier)
        tiers = [{"from": float(s), "to": float(starts[n + 1]) if n + 1 < len(starts) else None, "price": float(by_tier[s]["retailPrice"])}
                 for n, s in enumerate(starts)]
        head = by_tier[starts[0]]
        return entry("azure", meter, region, head.get("unitOfMeasure", ""), tiers,
                     "https://prices.azure.com/api/retail/prices", "live", head.get("skuId"),
                     "%s - %s - %s" % (head.get("productName"), head.get("skuName"), head.get("meterName")),
                     retrieved=(head.get("effectiveStartDate") or today())[:10])


# ------------------------------------------------------- virtual-machine clouds
class LinodePrices:
    TYPES = "https://api.linode.com/v4/linode/types?page_size=500"
    DATABASES = "https://api.linode.com/v4/databases/types?page_size=500"

    def __init__(self, fetcher):
        self.fetcher = fetcher
        self._types = None
        self._dbs = None

    def plans(self):
        if self._types is None:
            self._types = self.fetcher.get_json(self.TYPES).get("data", [])
        return self._types

    def vm(self, meter, vcpu, ram_gb):
        fits = [p for p in self.plans() if p.get("vcpus", 0) >= vcpu and p.get("memory", 0) >= ram_gb * 1024
                and not p.get("gpus") and p.get("class") in ("standard", "dedicated", "nanode", "shared")
                and (p.get("price") or {}).get("monthly") is not None]
        if not fits:
            raise PriceError("no Linode plan with %s vCPU and %s GB" % (vcpu, ram_gb))
        best = min(fits, key=lambda p: (p["price"]["monthly"], p["id"]))
        return entry("linode", meter, "global", "month", [{"from": 0, "to": None, "price": float(best["price"]["monthly"])}],
                     self.TYPES, "live", best["id"], "%s: %s vCPU, %s MB" % (best.get("label"), best.get("vcpus"), best.get("memory")))

    def database(self, meter, engine, vcpu, nodes):
        if self._dbs is None:
            self._dbs = self.fetcher.get_json(self.DATABASES).get("data", [])
        fits = [d for d in self._dbs if engine in (d.get("engines") or {}) and d.get("vcpus", 0) >= vcpu]
        for d in sorted(fits, key=lambda d: d.get("vcpus", 0)):
            for option in d["engines"][engine]:
                if option.get("quantity") == nodes:
                    return entry("linode", meter, "global", "month", [{"from": 0, "to": None, "price": float(option["price"]["monthly"])}],
                                 self.DATABASES, "live", d.get("id"), "%s %s, %s node(s)" % (d.get("label"), engine, nodes))
        raise PriceError("no Linode managed %s with %s node(s)" % (engine, nodes))


class VultrPrices:
    PLANS = "https://api.vultr.com/v2/plans?type=all&per_page=500"

    def __init__(self, fetcher):
        self.fetcher = fetcher
        self._plans = None

    def vm(self, meter, vcpu, ram_gb):
        if self._plans is None:
            self._plans = self.fetcher.get_json(self.PLANS).get("plans", [])
        fits = [p for p in self._plans if p.get("vcpu_count", 0) >= vcpu and p.get("ram", 0) >= ram_gb * 1024
                and p.get("type") in ("vc2", "vhf", "vdc") and p.get("monthly_cost", 0) > 0]
        if not fits:
            raise PriceError("no Vultr plan with %s vCPU and %s GB" % (vcpu, ram_gb))
        best = min(fits, key=lambda p: (p["monthly_cost"], p["id"]))
        return entry("vultr", meter, "global", "month", [{"from": 0, "to": None, "price": float(best["monthly_cost"])}],
                     self.PLANS, "live", best["id"], "%s: %s vCPU, %s MB" % (best["id"], best.get("vcpu_count"), best.get("ram")))


class ScalewayPrices:
    SERVERS = "https://api.scaleway.com/instance/v1/zones/%s/products/servers?per_page=100"

    def __init__(self, fetcher, zone="fr-par-1"):
        self.fetcher, self.zone = fetcher, zone
        self._servers = None

    def vm(self, meter, vcpu, ram_gb):
        if self._servers is None:
            self._servers = self.fetcher.get_json(self.SERVERS % self.zone).get("servers", {})
        fits = [(name, s) for name, s in self._servers.items() if s.get("ncpus", 0) >= vcpu and s.get("ram", 0) >= ram_gb * 1073741824
                and not s.get("gpu") and s.get("monthly_price") and s.get("arch") == "x86_64"]
        if not fits:
            raise PriceError("no Scaleway instance with %s vCPU and %s GB" % (vcpu, ram_gb))
        name, best = min(fits, key=lambda kv: (kv[1]["monthly_price"], kv[0]))
        return entry("scaleway", meter, self.zone, "month", [{"from": 0, "to": None, "price": float(best["monthly_price"])}],
                     self.SERVERS % self.zone, "live", name, "%s: %s vCPU, %s GB" % (name, best["ncpus"], round(best["ram"] / 1073741824)))


class HetznerPrices:
    """Hetzner Cloud server prices. The API needs a read-only token in HCLOUD_TOKEN."""
    URL = "https://api.hetzner.cloud/v1/server_types?per_page=50"

    def __init__(self, fetcher, token):
        if not token:
            raise PriceError("set HCLOUD_TOKEN (a read-only Hetzner Cloud API token) to read Hetzner prices live")
        self.fetcher, self.token = fetcher, token
        self._types = None

    def vm(self, meter, vcpu, ram_gb):
        if self._types is None:
            self._types = self.fetcher.get_json(self.URL, {"Authorization": "Bearer " + self.token}).get("server_types", [])
        fits = []
        for t in self._types:
            if t.get("deprecated") or t.get("cores", 0) < vcpu or t.get("memory", 0) < ram_gb or not t.get("prices"):
                continue
            price = t["prices"][0].get("price_monthly", {}).get("net")
            if price is not None:
                fits.append((float(price), t["name"], t))
        if not fits:
            raise PriceError("no Hetzner server type with %s vCPU and %s GB" % (vcpu, ram_gb))
        price, name, best = min(fits, key=lambda f: (f[0], f[1]))
        return entry("hetzner", meter, best["prices"][0].get("location", "global"), "month",
                     [{"from": 0, "to": None, "price": price}], self.URL, "live", name,
                     "%s: %s vCPU, %s GB (net price)" % (name, best.get("cores"), best.get("memory")))


class DigitalOceanPrices:
    """DigitalOcean Droplet sizes. The API needs a read-only token in DIGITALOCEAN_TOKEN."""
    URL = "https://api.digitalocean.com/v2/sizes?per_page=200"

    def __init__(self, fetcher, token):
        if not token:
            raise PriceError("set DIGITALOCEAN_TOKEN (a read-only API token) to read DigitalOcean prices live")
        self.fetcher, self.token = fetcher, token
        self._sizes = None

    def vm(self, meter, vcpu, ram_gb):
        if self._sizes is None:
            self._sizes = self.fetcher.get_json(self.URL, {"Authorization": "Bearer " + self.token}).get("sizes", [])
        fits = [s for s in self._sizes if s.get("available") and s.get("vcpus", 0) >= vcpu and s.get("memory", 0) >= ram_gb * 1024
                and not s.get("gpu_info") and str(s.get("slug", "")).startswith("s-")]
        if not fits:
            raise PriceError("no DigitalOcean size with %s vCPU and %s GB" % (vcpu, ram_gb))
        best = min(fits, key=lambda s: (s["price_monthly"], s["slug"]))
        return entry("digitalocean", meter, "global", "month", [{"from": 0, "to": None, "price": float(best["price_monthly"])}],
                     self.URL, "live", best["slug"], "%s: %s vCPU, %s MB" % (best["slug"], best["vcpus"], best["memory"]))


# ------------------------------------------------------------------ Google Cloud
GCP_BASE = "https://cloudbilling.googleapis.com/v1/"


class GcpPrices:
    """Reads the Cloud Billing Catalog API. It needs a free API key (GOOGLE_API_KEY or GCP_BILLING_API_KEY)."""

    def __init__(self, fetcher, key, region="us-central1"):
        if not key:
            raise PriceError("set GOOGLE_API_KEY (a free Cloud Billing API key) to read Google Cloud prices live")
        self.fetcher, self.key, self.region = fetcher, key, region
        self._services = None
        self._skus = {}

    def _page(self, path, params):
        out, token = [], None
        for _ in range(20):
            query = dict(params, key=self.key, pageSize=5000)
            if token:
                query["pageToken"] = token
            data = self.fetcher.get_json(GCP_BASE + path + "?" + urllib.parse.urlencode(query))
            out.extend(data.get("services") or data.get("skus") or [])
            token = data.get("nextPageToken")
            if not token:
                break
        return out

    def service_id(self, display_name):
        if self._services is None:
            self._services = self._page("services", {})
        for s in self._services:
            if s.get("displayName") == display_name:
                return s["serviceId"].split("/")[-1] if "/" in s.get("serviceId", "") else s["name"].split("/")[-1]
        raise PriceError("Google Cloud service '%s' was not found in the catalogue" % display_name)

    def find(self, service, description, meter, usage_type="OnDemand", exclude=None):
        sid = self.service_id(service)
        if sid not in self._skus:
            self._skus[sid] = self._page("services/%s/skus" % sid, {"currencyCode": "USD"})
        rx = re.compile(description, re.I)
        skip = re.compile(exclude, re.I) if exclude else None
        for sku in sorted(self._skus[sid], key=lambda s: s.get("description", "")):
            if not rx.search(sku.get("description", "")) or (skip and skip.search(sku["description"])):
                continue
            if self.region not in sku.get("serviceRegions", []) or sku.get("category", {}).get("usageType") != usage_type:
                continue
            expression = sku["pricingInfo"][0]["pricingExpression"]
            tiers = []
            rates = sorted(expression["tieredRates"], key=lambda r: float(r.get("startUsageAmount", 0)))
            for n, rate in enumerate(rates):
                price = float(rate["unitPrice"].get("units", 0) or 0) + rate["unitPrice"].get("nanos", 0) / 1e9
                end = float(rates[n + 1].get("startUsageAmount", 0)) if n + 1 < len(rates) else None
                tiers.append({"from": float(rate.get("startUsageAmount", 0)), "to": end, "price": price})
            return entry("gcp", meter, self.region, expression.get("usageUnit", ""), tiers,
                         "https://cloud.google.com/skus", "live", sku.get("skuId"), sku["description"])
        raise PriceError("no Google Cloud SKU matched '%s' for %s in %s" % (description, meter, self.region))


# ------------------------------------------------------- supplied and reference
def supplied(provider, meter, price, unit, source, quote, region="global", retrieved=None):
    """A price someone read from an official page or a vendor tool and recorded with a source."""
    if not re.match(r"https://", source or ""):
        raise PriceError("a supplied price needs an https source URL")
    if len((quote or "").strip()) < 12:
        raise PriceError("a supplied price needs a quote of at least 12 characters from the source")
    if not isinstance(price, (int, float)) or price < 0:
        raise PriceError("the price must be a number of zero or more")
    return entry(provider, meter, region, unit, [{"from": 0, "to": None, "price": float(price)}], source, "supplied",
                 None, quote.strip(), retrieved=retrieved)


# Dated values for meters the vendor publishes only in very large files. Region-specific, never applied to another region.
REFERENCE_PRICES = {
    ("aws", "nat_gateway_hour", "us-east-1"): (0.045, "hour", "https://aws.amazon.com/vpc/pricing/", "NAT Gateway, per hour"),
    ("aws", "nat_gateway_gb", "us-east-1"): (0.045, "GB", "https://aws.amazon.com/vpc/pricing/", "NAT Gateway, per GB processed"),
}
REFERENCE_DATE = "2026-10-22"


def reference(provider, meter, region):
    found = REFERENCE_PRICES.get((provider, meter, region))
    if not found:
        raise PriceError("no reference price for %s %s in %s" % (provider, meter, region))
    price, unit, source, description = found
    return entry(provider, meter, region, unit, [{"from": 0, "to": None, "price": price}], source, "reference", None,
                 description + " (dated reference value, not read live)", retrieved=REFERENCE_DATE)


def gcp_key():
    return os.environ.get("GOOGLE_API_KEY") or os.environ.get("GCP_BILLING_API_KEY") or ""


def write_json_atomic(path, data):
    path = os.fspath(path)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".prices-", suffix=".tmp", dir=os.path.dirname(os.path.abspath(path)))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, sort_keys=True)
            handle.write(chr(10))
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
