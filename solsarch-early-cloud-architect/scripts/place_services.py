#!/usr/bin/env python3
"""Map scanner evidence to cloud services with fixed rules.

Standard library only. The rules live in references/placement-rules.json. Because the mapping is
code, not judgement, every model gets the same placements for the same evidence, cloud and
compute model.

Usage: python place_services.py EVIDENCE.json --cloud aws|azure|gcp --model server|serverless
Prints placements JSON to stdout (save it as placements.json).
Exit codes: 0 ok, 2 usage or input error.
"""
import argparse
import json
import os
import sys

CLOUDS = ("aws", "azure", "gcp")
MODELS = ("server", "serverless")
DEFAULT_RULES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "references", "placement-rules.json")
IDS_PER_PLACEMENT = 4
NOT_A_SERVICE_KINDS = ("language", "port", "server", "iam", "network", "iac", "proxy", "volume", "orm", "framework",
                       "container", "realtime")


def matches(item, matcher):
    if item.get("kind") != matcher.get("kind"):
        return False
    names = matcher.get("names")
    if names is not None and item.get("name") not in names:
        return False
    signals = matcher.get("signals")
    if signals is not None and item.get("signal") not in signals:
        return False
    return True


def matching_items(evidence, matchers):
    out = []
    for item in evidence:
        if any(matches(item, m) for m in matchers):
            out.append(item)
    return sorted(out, key=lambda e: e["id"])


def place(evidence_doc, rules_doc, cloud, model):
    if cloud not in CLOUDS or model not in MODELS:
        raise ValueError("cloud must be one of %s and model one of %s" % (", ".join(CLOUDS), ", ".join(MODELS)))
    evidence = evidence_doc.get("evidence", [])
    signals = {e.get("signal") for e in evidence if e.get("signal")}
    placements, used, caveats_given = [], set(), set()
    for rule in rules_doc["rules"]:
        if "models" in rule and model not in rule["models"]:
            continue
        hits = matching_items(evidence, rule["when"])
        if not hits:
            continue
        if rule.get("unless") and matching_items(evidence, rule["unless"]):
            continue
        service = rule["services"][cloud][model]
        names = []
        for h in hits:
            if h["name"] not in names:
                names.append(h["name"])
        confirmed = bool(rule.get("confirmed_when") and matching_items(evidence, rule["confirmed_when"]))
        confidence = "Confirmed" if (rule.get("confidence") == "Confirmed" or confirmed) else "Inferred"
        cited = [h["id"] for h in hits][:IDS_PER_PLACEMENT]
        used.update(h["id"] for h in hits)
        caveats = [c["text"] for c in rules_doc.get("caveats", [])
                   if c["if_signal"] in signals and model in c["models"]
                   and rule["id"] in c.get("rules", [rule["id"]])
                   and _caveat_applies(rule, c)]
        caveats = [c for c in caveats if c not in caveats_given]
        caveats_given.update(caveats)
        placements.append({
            "id": "P%02d" % (len(placements) + 1),
            "rule": rule["id"],
            "category": rule["category"],
            "service": service,
            "evidence_ids": cited,
            "confidence": confidence,
            "why": rule["why"].format(evidence_names=", ".join(names[:4]), service=service,
                                      model="server-based" if model == "server" else "serverless"),
            "alternative": rule["alternative"][model],
            "caveats": caveats,
        })
    unplaced = sorted(e["id"] for e in evidence
                      if e["id"] not in used and e.get("kind") not in NOT_A_SERVICE_KINDS and e.get("kind") != "secret")
    return {
        "cloud": cloud,
        "compute_model": model,
        "rules_version": rules_doc.get("version"),
        "placements": placements,
        "unplaced_evidence": unplaced,
    }


def _caveat_applies(rule, caveat):
    """Attach a caveat only to the placement it is about."""
    about = {
        "streaming": ("R-COMPUTE-API", "R-COMPUTE-CONTAINER", "R-ENTRY", "R-REALTIME-SERVERLESS"),
        "websocket": ("R-COMPUTE-API", "R-COMPUTE-CONTAINER", "R-ENTRY", "R-REALTIME-SERVERLESS"),
        "sqlite": ("R-DB-POSTGRES",),
        "queue": ("R-WORKER",),
    }
    return rule["id"] in about.get(caveat["if_signal"], ())


def main(argv=None):
    ap = argparse.ArgumentParser(description="Map evidence to cloud services with fixed rules.")
    ap.add_argument("evidence")
    ap.add_argument("--cloud", required=True, choices=CLOUDS)
    ap.add_argument("--model", required=True, choices=MODELS)
    ap.add_argument("--rules", default=DEFAULT_RULES)
    args = ap.parse_args(argv)
    try:
        with open(args.evidence, "r", encoding="utf-8") as fh:
            evidence_doc = json.load(fh)
        with open(args.rules, "r", encoding="utf-8") as fh:
            rules_doc = json.load(fh)
        result = place(evidence_doc, rules_doc, args.cloud, args.model)
    except (OSError, ValueError, KeyError) as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    json.dump(result, sys.stdout, indent=2, ensure_ascii=True, sort_keys=False)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
