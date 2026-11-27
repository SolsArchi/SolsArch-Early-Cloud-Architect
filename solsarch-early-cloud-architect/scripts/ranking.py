#!/usr/bin/env python3
"""Rank clouds for one codebase by what the user cares about, under their company rules.

    ranking.py --repo REPO [--clouds auto|all|aws,azure,gcp] [--priority balanced] [--compute server]

The same scan is placed on each cloud with the fixed placement rules. Each cloud then gets five scores from 0 to 100, all computed from
data the plan already holds, and a priority picks the fixed weights that combine them:

    fit          share of needs a native service covers, minus a small penalty per caveat
    ops          fewer self-managed pieces to run yourself
    portability  share of the design that is not tied to the cloud's own services
    reliability  share of design rules met (a bent rule counts half)
    cost         cheapest priced monthly total = 100. Only when prices were read; otherwise it is left out, said so, and the other weights are scaled up

Clouds the company rules block are excluded with the reason. The weights are shown in the output; the score is a ranking aid, not a
prediction of performance or of the bill. Standard library only.
"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

WEIGHTS = {
    "balanced":    {"fit": 0.30, "ops": 0.20, "portability": 0.15, "reliability": 0.20, "cost": 0.15},
    "cost":        {"fit": 0.15, "ops": 0.10, "portability": 0.05, "reliability": 0.10, "cost": 0.60},
    "performance": {"fit": 0.45, "ops": 0.15, "portability": 0.05, "reliability": 0.30, "cost": 0.05},
    "reliability": {"fit": 0.25, "ops": 0.15, "portability": 0.05, "reliability": 0.50, "cost": 0.05},
    "portability": {"fit": 0.15, "ops": 0.15, "portability": 0.55, "reliability": 0.10, "cost": 0.05},
}
METRICS = ("fit", "ops", "portability", "reliability", "cost")
FIT_FLOOR = 50.0  # a cloud that covers less than half of what the app needs is not offered as a choice, however well it scores elsewhere
MULTICLOUD_NOTE = ("Do not split one app across clouds unless a rule or a missing service forces it: data leaving a cloud is billed, identity and networking "
                   "must be built twice, and there are two things to operate. One primary cloud plus a portable add-on (a managed database, a CDN) is the usual limit.")


def clamp(value):
    return max(0.0, min(100.0, value))


def plan_scores(plan, design):
    decisions, gaps = plan["decisions"], plan["gaps"]
    n = len(decisions) or 1
    caveats = sum(len(d.get("caveats") or []) for d in decisions)
    fit = clamp(100.0 * len(decisions) / (len(decisions) + len(gaps) or 1) - 3.0 * caveats)
    ops = clamp(100.0 * (1 - sum(1 for d in decisions if d.get("selfmanaged")) / n))
    portable = sum(1 for d in decisions if d.get("selfmanaged") or d.get("partner"))
    portability = clamp(100.0 * portable / n)
    counts = (design or {}).get("rule_counts") or {}
    rated = counts.get("met", 0) + counts.get("bent", 0) + counts.get("violated", 0)
    reliability = clamp(100.0 * (counts.get("met", 0) + 0.5 * counts.get("bent", 0)) / rated) if rated else None
    return {"fit": round(fit, 1), "ops": round(ops, 1), "portability": round(portability, 1),
            "reliability": round(reliability, 1) if reliability is not None else None, "caveats": caveats, "gaps": len(gaps)}


def combine(scores, weights):
    """Weighted mean over the metrics that exist, with the weights of missing ones shared out."""
    used = {m: weights[m] for m in METRICS if scores.get(m) is not None}
    total = sum(used.values())
    if not total:
        return 0.0, {}
    return round(sum(scores[m] * w for m, w in used.items()) / total, 1), {m: round(w / total, 3) for m, w in used.items()}


def trade_off(cloud, scores):
    have = [(m, scores[m]) for m in METRICS if scores.get(m) is not None]
    best, worst = max(have, key=lambda x: (x[1], x[0])), min(have, key=lambda x: (x[1], x[0]))
    if best[1] == worst[1]:
        return "%s is even across the measures." % cloud
    return "%s is strongest on %s (%.0f) and weakest on %s (%.0f)." % (cloud, best[0], best[1], worst[0], worst[1])


def monthly_cost(plan, design, repo, online):
    """Priced monthly total, or None when the run could not price every line."""
    try:
        import architect
        import costs as costs_engine
        import providers
        result = costs_engine.build(plan, design, architect.load_profile(repo), Path(repo) / ".solsarch" / costs_engine.BOOK_FILE, online=online, registry=providers.registry())
    except Exception:  # a pricing failure leaves cost out of the ranking rather than inventing a number
        return None
    totals = result.get("totals", {})
    if result.get("pricing_status") in ("none", "error") or totals.get("unpriced_lines", 0) or not totals.get("priced_lines"):
        return None
    return totals["monthly"]


def rank(repo, scan, clouds, compute, priority, online=False, rules_data=None, with_cost=True):
    import architect
    import rules as company_rules
    import recommend
    catalog = recommend.load_catalog()
    weights = WEIGHTS[priority]
    allowed, excluded, rows = [], [], []
    for cloud in clouds:
        blocked = company_rules.blocking(company_rules.violations(rules_data, cloud, compute)) if rules_data else []
        if blocked:
            excluded.append({"cloud": cloud, "reason": "; ".join(v["message"] for v in blocked)})
            continue
        allowed.append(cloud)
    for cloud in allowed:
        try:
            plan = recommend.build_plan(scan, catalog, cloud, compute, (), architect.load_profile(repo))
            design = architect.review(repo, scan, plan, architect.load_profile(repo))
        except ValueError as error:
            excluded.append({"cloud": cloud, "reason": str(error)})
            continue
        scores = plan_scores(plan, design)
        scores["cost"] = None
        scores["monthly"] = monthly_cost(plan, design, repo, online) if with_cost else None
        if scores["fit"] < FIT_FLOOR:
            excluded.append({"cloud": cloud, "reason": "covers only %.0f%% of what this app needs with native services (minimum %.0f%%)" % (scores["fit"], FIT_FLOOR)})
            continue
        rows.append({"cloud": cloud, "name": plan["cloud_name"], "scores": scores, "fingerprint": plan["fingerprint"]})
    priced = [r["scores"]["monthly"] for r in rows if r["scores"]["monthly"] is not None]
    if len(priced) == len(rows) and rows:
        cheapest, dearest = min(priced), max(priced)
        for r in rows:
            m = r["scores"]["monthly"]
            r["scores"]["cost"] = 100.0 if dearest == cheapest else round(100.0 * (dearest - m) / (dearest - cheapest) * 0.8 + 20.0, 1)
    for r in rows:
        r["total"], r["weights_used"] = combine(r["scores"], weights)
        r["trade_off"] = trade_off(r["name"], r["scores"])
    rows.sort(key=lambda r: (-r["total"], r["cloud"]))
    for position, r in enumerate(rows, 1):
        r["rank"] = position
    return {"status": "ok", "priority": priority, "compute_model": compute, "weights": weights, "ranking": rows, "excluded": excluded,
            "cost_used": bool(rows) and all(r["scores"]["cost"] is not None for r in rows), "multicloud": MULTICLOUD_NOTE}


def render(result):
    out = ["# Cloud ranking", "",
           "Priority: **%s**. Compute: %s. The same scan was placed on each cloud; scores run 0 to 100 and come from the plan, not from a benchmark. "
           "They help you order the options and are not a forecast of speed or of the bill." % (result["priority"], result["compute_model"]), "",
           "Weights: " + ", ".join("%s %d%%" % (m, round(w * 100)) for m, w in result["weights"].items()) + ".", ""]
    if not result["cost_used"]:
        out += ["Cost is left out of the total: not every cloud could be priced in this run (offline, or a service has no public price). "
                "The other weights are scaled up to fill its share. Run `costs` for the cloud you pick.", ""]
    out += ["| Rank | Cloud | Total | Fit | Ops | Portability | Reliability | Cost | Monthly |", "|---|---|---|---|---|---|---|---|---|"]
    show = lambda v: "n/a" if v is None else ("%.0f" % v)  # noqa: E731
    for r in result["ranking"]:
        s = r["scores"]
        out.append("| %d | %s | %.1f | %s | %s | %s | %s | %s | %s |" % (r["rank"], r["name"], r["total"], show(s["fit"]), show(s["ops"]), show(s["portability"]),
                                                                        show(s["reliability"]), show(s["cost"]), "n/a" if s["monthly"] is None else "$%.2f" % s["monthly"]))
    out += [""] + ["- " + r["trade_off"] for r in result["ranking"]]
    if result["excluded"]:
        out += ["", "## Not considered", ""] + ["- %s: %s" % (e["cloud"], e["reason"]) for e in result["excluded"]]
    out += ["", "## One cloud or several?", "", result["multicloud"], "",
            "To continue with the top choice: `solsarch.py plan --repo REPO --cloud %s --compute %s`." % (result["ranking"][0]["cloud"] if result["ranking"] else "CLOUD", result["compute_model"]), ""]
    return chr(10).join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Rank clouds by priority under company rules")
    parser.add_argument("--repo", default=".")
    parser.add_argument("--clouds", default="auto")
    parser.add_argument("--priority", choices=sorted(WEIGHTS), default="balanced")
    parser.add_argument("--compute", choices=("server", "serverless"), default="server")
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args(argv)
    import providers
    import rules as company_rules
    state = Path(args.repo) / ".solsarch"
    try:
        scan = json.loads((state / "scan.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        print(json.dumps({"status": "error", "message": "No scan found. Run the scan step first."}))
        return 2
    hosting = providers.hosting_ids()
    chosen = hosting if args.clouds in ("auto", "all") else [c.strip().lower() for c in args.clouds.split(",") if c.strip()]
    unknown = [c for c in chosen if c not in hosting]
    if unknown:
        print(json.dumps({"status": "error", "message": "unknown provider: %s" % ", ".join(unknown)}))
        return 2
    import os
    online = not (args.offline or os.environ.get("SOLSARCH_OFFLINE"))
    result = rank(args.repo, scan, chosen, args.compute, args.priority, online=online, rules_data=company_rules.ensure(args.repo),
                  with_cost=online)
    if not result["ranking"]:
        print(json.dumps({"status": "error", "message": "No cloud is allowed by the company rules.", "excluded": result["excluded"]}))
        return 1
    state.mkdir(parents=True, exist_ok=True)
    (state / "ranking.json").write_text(json.dumps(result, indent=2, sort_keys=True) + chr(10), encoding="utf-8", newline=chr(10))
    docs = Path(args.repo) / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "solsarch-cloud-ranking.md").write_bytes(render(result).encode("utf-8"))
    print(json.dumps({"status": "written", "file": str(docs / "solsarch-cloud-ranking.md"), "priority": args.priority, "cost_used": result["cost_used"],
                      "top": [{"cloud": r["cloud"], "total": r["total"]} for r in result["ranking"][:5]], "excluded": [e["cloud"] for e in result["excluded"]]}, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
