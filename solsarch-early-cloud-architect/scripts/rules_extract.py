#!/usr/bin/env python3
"""Turn company-rules documents into rule objects.

    rules_extract.py PATH [PATH ...]      print the rules found and the sentences that look like rules but were not understood

Two inputs are understood:
  structured   JSON or YAML with a "rules" list (or the rule kinds as top-level keys), and lines in any text document written as
               `rule: KIND = VALUE ; level=blocking`. These are exact.
  prose        sentences in md, txt, html, docx or pdf. A fixed table of patterns picks out clear statements such as
               "Databases must not be publicly accessible" or "All data must stay in the EU". Each rule keeps the sentence it came from.
A sentence that sounds like a rule (must, shall, never, required...) but matches no pattern goes to `not_enforced`, so nothing is
dropped silently. No model is used: the same files always give the same rules.
"""
import hashlib
import json
import math
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import providers  # noqa: E402
import rules_ingest  # noqa: E402

REF = HERE.parent / "references" / "rules"
LEVELS = ("advisory", "required", "blocking")
KINDS = ("allowed_clouds", "denied_clouds", "preferred_clouds", "data_residency", "allowed_regions", "denied_services",
         "no_public_database", "no_public_storage", "encryption_at_rest", "encryption_in_transit", "customer_managed_keys",
         "required_tags", "naming_prefix", "budget_monthly", "rpo_minutes", "rto_minutes", "ha_multi_az",
         "log_retention_min_days", "log_retention_max_days", "compliance", "compute_model", "iac_tool", "ci_provider")
TAG_KEY = re.compile(r"^[A-Za-z][A-Za-z0-9_.:/-]{0,62}$")
PREFIX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,30}$")
REGION_ID = re.compile(r"^[a-z][a-z0-9-]{1,40}$")
IAC_TOOLS = ("terraform", "opentofu", "pulumi", "cloudformation", "bicep", "cdk", "ansible", "crossplane")
CI_PROVIDERS = {"github-actions": r"github actions?", "gitlab-ci": r"gitlab[- ]ci|gitlab pipelines?", "azure-devops": r"azure devops|azure pipelines?",
                "jenkins": r"jenkins", "circleci": r"circle ?ci"}
GEO_WORDS = {"eu": r"\be\.?u\.?\b|european union|\beurope\b|european", "eea": r"\beea\b|european economic area", "uk": r"\bu\.?k\.?\b|united kingdom|britain",
             "us": r"\bU\.?S\.?A?\b|[Uu]nited [Ss]tates", "de": r"germany|german", "fr": r"france|french", "in": r"\bindia\b|indian",
             "ch": r"switzerland|swiss", "ca": r"\bcanada\b|canadian", "au": r"australia", "jp": r"\bjapan\b", "sg": r"singapore"}
# Services a rule can forbid, and how to recognise them in a plan and in generated files.
SERVICE_TERMS = {
    "nat_gateway": (r"nat gateways?", r"nat_gateway|router_nat|nat_instance"),
    "kubernetes": (r"kubernetes|\bk8s\b|\beks\b|\baks\b|\bgke\b", r"aws_eks|azurerm_kubernetes|google_container|kubernetes_cluster|sks_cluster|\bkind: Deployment\b"),
    "serverless": (r"serverless|lambda|cloud functions|azure functions|functions as a service|\bfaas\b", r"aws_lambda|google_cloudfunctions|google_cloud_run|azurerm_(linux|windows)_function_app|azurerm_container_app|aws_apprunner"),
    "redis": (r"redis|elasticache|memorystore", r"elasticache|redis"),
    "mongodb": (r"mongo ?db|documentdb|cosmos ?db", r"mongo|docdb|cosmosdb"),
    "kafka": (r"kafka|\bmsk\b|event hubs?", r"aws_msk|kafka|eventhub"),
    "rabbitmq": (r"rabbit ?mq|amazon mq", r"rabbit|aws_mq_broker"),
    "load_balancer": (r"load balancers?|\balb\b|\bnlb\b", r"aws_lb\b|aws_alb\b|load_balancer|forwarding_rule|application_gateway"),
    "public_ip": (r"public ip(?:v4)? addresses|public ips?|elastic ips?", r"aws_eip\b|public_ip|google_compute_address"),
    "spot_instances": (r"spot instances?|preemptible", r"spot|preemptible"),
    "gpu": (r"\bgpus?\b", r"gpu|nvidia"),
    "cdn": (r"\bcdn\b|cloudfront", r"cloudfront|cdn|front_door"),
}
_cache = {}


def sha(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def regions_table():
    if "regions" not in _cache:
        _cache["regions"] = json.loads((REF / "regions.json").read_text(encoding="utf-8"))
    return _cache["regions"]


def frameworks():
    if "fw" not in _cache:
        _cache["fw"] = json.loads((REF / "frameworks.json").read_text(encoding="utf-8"))["frameworks"]
    return _cache["fw"]


def geos_for_region(token):
    """The geographies a region id lies in, or None when the table does not know it."""
    token = token.lower()
    for row in regions_table()["regions"]:
        if re.search(row["re"], token):
            return set(row["geos"])
    return None


def cloud_aliases():
    """alias -> provider id. Names that are also common words only match when written with a capital."""
    if "clouds" in _cache:
        return _cache["clouds"]
    extra = {"aws": ["amazon web services", "amazon aws"], "gcp": ["google cloud", "google cloud platform", "gcp"], "azure": ["microsoft azure"],
             "oci": ["oracle cloud", "oracle cloud infrastructure"], "ovhcloud": ["ovh", "ovhcloud"], "digitalocean": ["digital ocean"],
             "flyio": ["fly.io"], "alibaba": ["alibaba cloud", "aliyun"], "huawei": ["huawei cloud"], "ibm": ["ibm cloud"], "tencent": ["tencent cloud"],
             "huggingface": ["hugging face"], "hetzner": ["hetzner"], "mongodbatlas": ["mongodb atlas"], "denodeploy": ["deno deploy"],
             "cloudfoundry": ["cloud foundry"], "togetherai": ["together ai"], "lambdalabs": []}
    table, ambiguous = {}, {"render", "railway", "neon", "lambda"}
    for pid, info in providers.registry().items():
        if info.get("kind") == "enterprise":
            continue
        names = {pid, re.sub(r"\s*\(.*?\)", "", info["name"]).lower()}
        names.update(extra.get(pid, []))
        for n in names:
            if n and n not in ("lambda",):
                table[n] = pid
    _cache["clouds"] = (table, ambiguous)
    return _cache["clouds"]


def clouds_in(text):
    table, ambiguous = cloud_aliases()
    found = []
    for alias in sorted(table, key=len, reverse=True):
        pid = table[alias]
        flags = 0 if alias in ambiguous else re.I
        needle = alias.title() if alias in ambiguous else alias
        if re.search(r"(?<![\w-])" + re.escape(needle) + r"(?![\w-])", text, flags):
            text = re.sub(r"(?<![\w-])" + re.escape(needle) + r"(?![\w-])", " ", text, flags=flags)
            if pid not in found:
                found.append(pid)
    return sorted(found)


# ---------------------------------------------------------------- cues
NEG = re.compile(r"\b(must not|mustn't|shall not|may not|never|prohibit(?:ed)?|forbidden|banned|not allowed|not permitted|disallowed|unapproved|"
                 r"do not|don't|cannot|can't|is not approved|are not approved|not approved|avoid|not to be used|no)\b", re.I)
BLOCKING = re.compile(r"\b(must|shall|mandatory|never|prohibit(?:ed)?|forbidden|banned|not allowed|not permitted|non-negotiable|disallowed|only)\b", re.I)
REQUIRED = re.compile(r"\b(required|requires?|always|need(?:s)? to|do not|don't|cannot|approved|allowed|permitted|standard(?:ised|ized)?)\b", re.I)
ADVISORY = re.compile(r"\b(should|prefer(?:red|s)?|recommended|ideally|try to|encouraged|where possible|if possible)\b", re.I)
NORMATIVE = re.compile(r"\b(must|shall|never|prohibit(?:ed)?|forbidden|banned|required|requires?|mandatory|not allowed|not permitted|"
                       r"should|always|only|do not|don't|nobody|no one)\b", re.I)


def level_of(sentence):
    if ADVISORY.search(sentence) and not BLOCKING.search(sentence):
        return "advisory"
    if BLOCKING.search(sentence):
        return "blocking"
    if REQUIRED.search(sentence):
        return "required"
    return "required"


def units(text):
    """(line number, sentence) for each line and each sentence in it. Bullets, numbering, table bars and headings are stripped."""
    out = []
    for number, raw in enumerate(text.split(chr(10)), 1):
        if raw.lstrip().startswith("#") and not raw.lstrip().startswith("#!"):  # a heading names a section, it is not a rule
            continue
        line = re.sub(r"^\s*(?:[-*•>#]+|\d+[.)])\s*", "", raw.strip())
        line = line.replace("|", " ").replace("**", "").replace("`", "'") if not line.lower().startswith("rule:") else line
        line = re.sub(r"\s+", " ", line).strip()
        if len(line) < 8:
            continue
        for part in re.split(r"(?<=[.!?;])\s+(?=[A-Z0-9])", line):
            part = part.strip()
            if len(part) >= 8:
                out.append((number, part))
    return out


# ---------------------------------------------------------------- value normalisers (also used for structured rules)
def as_list(value):
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    return [x.strip() for x in re.split(r"[,;]", str(value)) if x.strip()]


def normalise(kind, value):
    """Return (clean value, problem). A rule that fails here is reported and not enforced."""
    if kind in ("allowed_clouds", "denied_clouds", "preferred_clouds"):
        known = set(providers.registry())
        items = []
        for x in as_list(value):
            key = str(x).strip().lower()
            ids = clouds_in(key) or ([key] if key in known else [])
            if not ids:
                return None, "unknown cloud or platform: %s" % x
            items.extend(ids)
        return (sorted(set(items)), None) if items else (None, "no cloud given")
    if kind == "data_residency":
        items = [str(x).strip().lower() for x in as_list(value)]
        bad = [x for x in items if x not in regions_table()["geo_names"] and x != "eea"]
        return (sorted(set(items)), None) if items and not bad else (None, "unknown area: %s" % ", ".join(bad or ["none given"]))
    if kind == "allowed_regions":
        items = [str(x).strip().lower() for x in as_list(value)]
        bad = [x for x in items if not REGION_ID.match(x)]
        return (sorted(set(items)), None) if items and not bad else (None, "not a region id: %s" % ", ".join(bad or ["none given"]))
    if kind == "denied_services":
        items = [str(x).strip().lower().replace(" ", "_").replace("-", "_") for x in as_list(value)]
        bad = [x for x in items if x not in SERVICE_TERMS]
        return (sorted(set(items)), None) if items and not bad else (None, "unknown service term: %s (known: %s)" % (", ".join(bad or ["none"]), ", ".join(SERVICE_TERMS)))
    if kind in ("no_public_database", "no_public_storage", "encryption_at_rest", "encryption_in_transit", "customer_managed_keys", "ha_multi_az"):
        return True, None
    if kind == "required_tags":
        items = [str(x).strip() for x in as_list(value)]
        bad = [x for x in items if not TAG_KEY.match(x)]
        return (sorted(set(items)), None) if items and not bad else (None, "not a valid tag key: %s" % ", ".join(bad or ["none given"]))
    if kind == "naming_prefix":
        text = str(value).strip()
        return (text, None) if PREFIX.match(text) else (None, "a naming prefix is letters, digits, - or _ (up to 31 characters)")
    if kind in ("budget_monthly", "rpo_minutes", "rto_minutes", "log_retention_min_days", "log_retention_max_days"):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None, "%s needs a number" % kind
        if not math.isfinite(number) or number <= 0 or number > 10 ** 9:
            return None, "%s must be positive" % kind
        return (round(number, 2) if kind == "budget_monthly" else int(number)), None
    if kind == "compliance":
        items = [str(x).strip().lower().replace(" ", "-").replace("_", "-") for x in as_list(value)]
        items = [{"soc-2": "soc2", "pci": "pci-dss", "iso-27001": "iso27001", "iso27001": "iso27001", "nis-2": "nis2"}.get(x, x) for x in items]
        bad = [x for x in items if x not in frameworks()]
        return (sorted(set(items)), None) if items and not bad else (None, "unknown framework: %s" % ", ".join(bad or ["none given"]))
    if kind == "compute_model":
        text = str(value).strip().lower()
        return (text, None) if text in ("server", "serverless") else (None, "compute_model is server or serverless")
    if kind == "iac_tool":
        items = [str(x).strip().lower() for x in as_list(value)]
        bad = [x for x in items if x not in IAC_TOOLS]
        return (sorted(set(items)), None) if items and not bad else (None, "unknown tool: %s" % ", ".join(bad or ["none given"]))
    if kind == "ci_provider":
        items = [str(x).strip().lower() for x in as_list(value)]
        bad = [x for x in items if x not in CI_PROVIDERS]
        return (sorted(set(items)), None) if items and not bad else (None, "unknown CI system: %s" % ", ".join(bad or ["none given"]))
    return None, "unknown rule kind: %s" % kind


def make_rule(kind, value, level, origin, file, locator, sentence, reason=None, currency=None):
    clean, problem = normalise(kind, value)
    if problem:
        return None, problem
    if level not in LEVELS:
        level = "required"
    body = json.dumps(clean, sort_keys=True)
    rid = "R-%s-%s" % (kind.replace("_", "-"), hashlib.sha256((kind + body + sentence).encode("utf-8")).hexdigest()[:6])
    rule = {"id": rid, "kind": kind, "value": clean, "level": level, "origin": origin,
            "source": {"file": file, "locator": locator, "sentence": sentence[:300], "sha256": sha(sentence)}}
    if reason:
        rule["reason"] = str(reason)[:300]
    if currency:
        rule["currency"] = currency
    return rule, None


# ---------------------------------------------------------------- prose patterns
def minutes(amount, unit):
    unit = unit.lower()
    factor = 1 / 60 if unit.startswith("sec") else 1 if unit.startswith("min") else 60 if unit.startswith("h") else 1440
    return max(1, int(round(float(amount) * factor)))


def days(amount, unit):
    unit = unit.lower()
    factor = 1 if unit.startswith("day") else 7 if unit.startswith("week") else 30 if unit.startswith("month") else 365
    return int(float(amount) * factor)


def tag_names(sentence):
    tail = re.split(r"(?:\:|\bincluding\b|\bsuch as\b|\bnamely\b|\bi\.e\.|\be\.g\.|\bwith the keys?\b|\bthe following\b)", sentence, maxsplit=1, flags=re.I)
    if len(tail) < 2:
        return []
    names = []
    for chunk in re.split(r",|;|\band\b|\bor\b", tail[1].rstrip(" .")):
        chunk = chunk.strip(" '\"")
        quoted = re.findall(r"'([^']+)'|\"([^\"]+)\"", chunk)
        if quoted:
            names.extend(a or b for a, b in quoted)
            continue
        words = chunk.split()
        if 1 <= len(words) <= 3 and all(re.fullmatch(r"[A-Za-z][A-Za-z0-9_:/-]*", w) for w in words):
            names.append("-".join(w.lower() for w in words) if len(words) > 1 else words[0])
    return [n for n in names if TAG_KEY.match(n) and n.lower() not in ("tags", "tag", "labels", "label", "the", "following", "keys", "key")]


def prose_rules(sentence):
    """All (kind, value, extras) a sentence states."""
    s = sentence
    low = s.lower()
    found = []
    # clouds
    ids = clouds_in(s)
    if ids and re.search(r"\b(use|using|host|hosted|hosting|deploy|run|workloads?|cloud|clouds|provider|providers|platform|platforms|vendor|stack|approved|allowed|permitted|sanctioned|standard)", low):
        if re.search(r"(must not|shall not|never|prohibit|forbidden|banned|not allowed|not permitted|do not|don't|unapproved|not approved|disallowed|avoid)", low):
            found.append(("denied_clouds", ids, {}))
        elif ADVISORY.search(s) and not BLOCKING.search(s):
            found.append(("preferred_clouds", ids, {}))
        elif re.search(r"\b(only|approved|allowed|permitted|sanctioned|standard(?:ised|ized)?|must use|shall use|supported|authori[sz]ed)\b", low):
            found.append(("allowed_clouds", ids, {}))
    # data residency and regions
    geos = [g for g, rx in GEO_WORDS.items() if re.search(rx, s, 0 if g == "us" else re.I)]
    if geos and re.search(r"(residen|sovereign|stay (?:in|within)|remain (?:in|within)|must be (?:stored|hosted|processed|located|deployed|kept)|only (?:in|within)|within (?:the )?|inside|"
                          r"not leave|outside|data cent(?:er|re)s? (?:in|within)|regions? (?:in|within)|hosted (?:in|within)|stored (?:in|within)|processed (?:in|within))", low) \
            and re.search(r"\b(data|workloads?|applications?|infrastructure|regions?|hosted|stored|processed|resources|servers|customers?|personal)\b", low):
        found.append(("data_residency", geos, {}))
    region_ids = [t for t in re.findall(r"\b[a-z]{2,}-[a-z]+-\d\b|\b[a-z]+-[a-z]+\d+\b|\b(?:westeurope|northeurope|germanywestcentral|francecentral|swedencentral|uksouth|ukwest|eastus2?|westus[23]?|centralus)\b", low)
                  if geos_for_region(t) is not None]
    if region_ids and re.search(r"\b(region|regions|only|allowed|approved|permitted|must|deploy|host)\b", low):
        found.append(("allowed_regions", sorted(set(region_ids)), {}))
    # services that must not be used
    if re.search(r"(must not|shall not|never|prohibit|forbidden|banned|not allowed|not permitted|do not|don't|disallowed|avoid|\bno\b)", low):
        hits = [k for k, (rx, _) in SERVICE_TERMS.items() if re.search(rx, s, re.I)]
        if hits and not (len(hits) == 1 and hits[0] == "public_ip" and re.search(r"database|storage|bucket", low)):
            if not re.search(r"public(?:ly)? (?:access|exposed|reachable|accessible)", low) or "public_ip" in hits:
                found.append(("denied_services", hits, {}))
    # public access
    if re.search(r"public(?:ly)?|internet|exposed", low) and re.search(r"(must not|shall not|never|not be|no |not |private|prohibit|forbidden|banned|block)", low):
        db = re.search(r"database|\bdbs?\b|\bsql\b|datastore|data stores?", low)
        st = re.search(r"storage|bucket|blob|\bs3\b|object store|file share|containers? registry", low)
        if db:
            found.append(("no_public_database", True, {}))
        if st:
            found.append(("no_public_storage", True, {}))
    # encryption
    if re.search(r"encrypt", low):
        rest, transit = re.search(r"at[- ]rest|storage|disks?|volumes?|databases?", low), re.search(r"in[- ]transit|\btls\b|\bhttps\b|\bssl\b|over the network", low)
        if re.search(r"customer[- ]managed|\bcmk\b|\bbyok\b|own keys|bring your own key", low):
            found.append(("customer_managed_keys", True, {}))
        if rest or not transit:
            found.append(("encryption_at_rest", True, {}))
        if transit or (not rest and re.search(r"\ball data\b|\bdata\b", low)):
            found.append(("encryption_in_transit", True, {}))
    elif re.search(r"\b(tls|https)\b", low) and re.search(r"\b(must|shall|required|always|only)\b", low):
        found.append(("encryption_in_transit", True, {}))
    # tags, naming
    if re.search(r"\b(tags?|labels?)\b", low) and re.search(r"\b(must|shall|required|mandatory|all resources|every resource|should)\b", low):
        names = tag_names(s)
        if names:
            found.append(("required_tags", names, {}))
    m = re.search(r"(?:prefix(?:ed)?(?: with)?|start(?:s)? with|begin(?:s)? with)\s+'([A-Za-z0-9][\w-]*)'", s, re.I)
    if m and re.search(r"\bnam(?:e|es|ing)\b", low):
        found.append(("naming_prefix", m.group(1), {}))
    # budget
    m = re.search(r"([$€£])\s?([\d][\d,]*(?:\.\d+)?)\s?(k|K)?|([\d][\d,]*(?:\.\d+)?)\s?(k|K)?\s?(usd|eur|gbp|dollars|euros|pounds)\b", s, re.I)
    if m and re.search(r"\b(month|monthly|/mo|per month|a month)\b", low) and re.search(r"\b(budget|spend|cost|cap|ceiling|limit|exceed|under|below|at most|maximum|up to)\b", low):
        raw = (m.group(2) or m.group(4)).replace(",", "")
        amount = float(raw) * (1000 if (m.group(3) or m.group(5)) else 1)
        cur = {"$": "USD", "€": "EUR", "£": "GBP"}.get(m.group(1) or "", (m.group(6) or "usd").upper().replace("DOLLARS", "USD").replace("EUROS", "EUR").replace("POUNDS", "GBP"))
        found.append(("budget_monthly", amount, {"currency": cur}))
    # recovery objectives, availability
    m = re.search(r"\b(?:rpo|recovery point objective)\b[^0-9]{0,40}(\d+(?:\.\d+)?)\s*(seconds?|minutes?|mins?|hours?|hrs?|days?)", s, re.I)
    if m:
        found.append(("rpo_minutes", minutes(m.group(1), m.group(2)), {}))
    m = re.search(r"\b(?:rto|recovery time objective)\b[^0-9]{0,40}(\d+(?:\.\d+)?)\s*(seconds?|minutes?|mins?|hours?|hrs?|days?)", s, re.I)
    if m:
        found.append(("rto_minutes", minutes(m.group(1), m.group(2)), {}))
    if re.search(r"multi[- ]?(?:az|zone)|multiple (?:availability )?zones|zone[- ]redundan|highly available|high availability", low) and re.search(r"\b(must|shall|required|production|always|should)\b", low):
        found.append(("ha_multi_az", True, {}))
    # log retention
    m = re.search(r"(\d+)[- ](day|week|month|year)s?", low)
    if m and re.search(r"\b(logs?|audit)\b", low) and re.search(r"retain|retention|kept|keep|stored|store|delete|purge|expire", low):
        count = days(m.group(1), m.group(2))
        if re.search(r"no (?:more|longer) than|not (?:be )?(?:kept|retained|stored) (?:for )?(?:longer|more)|maximum|at most|delete[sd]? after|purge[sd]? after|expire[sd]? after", low):
            found.append(("log_retention_max_days", count, {}))
        else:
            found.append(("log_retention_min_days", count, {}))
    # frameworks
    fw = []
    for key, rx in (("soc2", r"\bsoc ?2\b"), ("hipaa", r"\bhipaa\b"), ("pci-dss", r"\bpci(?:[- ]dss)?\b"), ("gdpr", r"\bgdpr\b"),
                    ("iso27001", r"\biso[ /-]?27001\b"), ("nist-csf", r"\bnist (?:csf|cybersecurity)"), ("fedramp", r"\bfedramp\b"),
                    ("eu-ai-act", r"\beu ai act\b"), ("nis2", r"\bnis ?2\b")):
        if re.search(rx, low):
            fw.append(key)
    if re.search(r"\bDORA\b", s):
        fw.append("dora")
    if fw and re.search(r"compl|certif|audit|must|require|subject to|regulated|adhere|align|conform|in scope|governed", low):
        found.append(("compliance", fw, {}))
    # compute, tools
    if re.search(r"serverless[- ](?:first|only)|\b(?:prefer|use|must be|should be)\s+serverless\b", low):
        found.append(("compute_model", "serverless", {}))
    tools = [t for t in IAC_TOOLS if re.search(r"\b%s\b" % t, low)]
    if tools and re.search(r"\b(must|shall|standard|only|required|approved|use)\b", low) and re.search(r"infrastructure|iac|provision|code", low) and not NEG.search(s):
        found.append(("iac_tool", tools, {}))
    ci = [k for k, rx in CI_PROVIDERS.items() if re.search(rx, low)]
    if ci and re.search(r"\b(must|shall|standard|only|required|approved)\b", low) and re.search(r"ci|pipeline|build|deploy", low) and not NEG.search(s):
        found.append(("ci_provider", ci, {}))
    return found


STRUCTURED_LINE = re.compile(r"^\s*(?:[-*]\s*)?rule\s*:\s*([a-z_]+)\s*=\s*(.+?)\s*(?:;\s*(.*))?$", re.I)


def structured_line(line):
    m = STRUCTURED_LINE.match(line)
    if not m:
        return None
    options = {}
    for part in (m.group(3) or "").split(";"):
        key, sep, value = part.partition("=")
        if sep:
            options[key.strip().lower()] = value.strip()
    return m.group(1).lower(), m.group(2).strip(), options


# ---------------------------------------------------------------- whole documents
def from_structured(doc, data):
    rules, problems, overrides = [], [], []
    name = doc["name"]
    if isinstance(data, list):
        data = {"rules": data}
    if not isinstance(data, dict):
        return rules, ["%s: expected an object with a rules list" % name], overrides
    entries = []
    for index, item in enumerate(data.get("rules") or [], 1):
        if isinstance(item, dict) and item.get("kind"):
            entries.append((item["kind"], item.get("value"), item.get("level"), item.get("reason"), "rules[%d]" % index, item))
        else:
            problems.append("%s: rules[%d] needs a kind and a value" % (name, index))
    for key, value in data.items():
        if key in KINDS and key != "rules":
            level = data.get("levels", {}).get(key) if isinstance(data.get("levels"), dict) else None
            entries.append((key, value.get("value") if isinstance(value, dict) and "value" in value else value,
                            (value.get("level") if isinstance(value, dict) else None) or level, value.get("reason") if isinstance(value, dict) else None, key, value))
    for kind, value, level, reason, locator, raw in entries:
        sentence = "%s: %s" % (kind, json.dumps(value, sort_keys=True, default=str))
        currency = raw.get("currency") if isinstance(raw, dict) else None
        rule, problem = make_rule(str(kind).lower(), value, str(level or "required").lower(), "structured", name, locator, sentence, reason, currency)
        if rule:
            rules.append(rule)
        else:
            problems.append("%s: %s: %s" % (name, locator, problem))
    for item in (data.get("exceptions") or []) + [{"rule": x, "ignore": True} for x in (data.get("ignore") or [])]:
        if isinstance(item, dict) and item.get("rule"):
            overrides.append({"rule": str(item["rule"]), "reason": str(item.get("reason") or "")[:300], "owner": str(item.get("owner") or "")[:100],
                              "expires": str(item.get("expires") or ""), "ignore": bool(item.get("ignore")), "file": name})
        else:
            problems.append("%s: an exception needs a rule id or kind" % name)
    return rules, problems, overrides


def from_text(doc):
    rules, not_enforced, problems = [], [], []
    name = doc["name"]
    seen = set()
    for number, sentence in units(doc["text"]):
        structured = structured_line(sentence)
        if structured:
            kind, value, options = structured
            rule, problem = make_rule(kind, value, options.get("level", "required").lower(), "structured", name, "line %d" % number, sentence,
                                      options.get("reason"), options.get("currency"))
            if rule:
                rules.append(rule)
            else:
                problems.append("%s line %d: %s" % (name, number, problem))
            continue
        hits = prose_rules(sentence)
        made = False
        for kind, value, extra in hits:
            rule, problem = make_rule(kind, value, level_of(sentence), "extracted", name, "line %d" % number, sentence, currency=extra.get("currency"))
            if rule and rule["id"] not in seen:
                seen.add(rule["id"])
                rules.append(rule)
                made = True
        if not made and NORMATIVE.search(sentence) and len(sentence) <= 400:
            not_enforced.append({"file": name, "locator": "line %d" % number, "sentence": sentence[:300]})
    return rules, not_enforced, problems


def extract(docs):
    """docs from rules_ingest.read_all. Returns {rules, not_enforced, problems, overrides, files}."""
    rules, not_enforced, problems, overrides, files = [], [], [], [], []
    for doc in docs:
        files.append({"name": doc["name"], "kind": doc["kind"], "sha256": doc["sha256"], "bytes": doc["bytes"], "problem": doc["problem"]})
        if doc["problem"]:
            problems.append("%s: %s" % (doc["name"], doc["problem"]))
            if doc["data"] is None:
                continue
        if doc["data"] is not None:
            r, p, o = from_structured(doc, doc["data"])
            rules += r
            problems += p
            overrides += o
        if doc["kind"] in ("text", "html", "docx", "pdf") or (doc["kind"] in ("json", "yaml") and doc["data"] is None):
            r, n, p = from_text(doc)
            rules += r
            not_enforced += n
            problems += p
    unique = {}
    for rule in rules:
        unique.setdefault(rule["id"], rule)
    ordered = sorted(unique.values(), key=lambda r: (r["kind"], r["id"]))
    return {"rules": ordered, "not_enforced": not_enforced[:100], "problems": problems, "overrides": overrides, "files": files}


def main(argv=None):
    paths = list(argv if argv is not None else sys.argv[1:])
    if not paths:
        print(json.dumps({"error": "give one or more files or folders"}))
        return 2
    docs, problems = rules_ingest.read_all(".", paths)
    result = extract(docs)
    result["problems"] = problems + result["problems"]
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
