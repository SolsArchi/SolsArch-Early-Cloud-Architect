# Code-to-Cloud benchmark

A public, repeatable way to test what SolsArch claims, and to set it beside any other workflow.

```bash
python solsarch-early-cloud-architect/scripts/benchmark.py list                       # the 20 fixtures
python solsarch-early-cloud-architect/scripts/benchmark.py run --out solsarch.json    # build, plan and finish each one, offline
python solsarch-early-cloud-architect/scripts/benchmark.py compare solsarch.json other.json
```

## What it measures
Twenty small repositories, four in each of five categories (simple full-stack, database-intensive, AI and agentic, microservices, existing infrastructure). For each one: quality score, whether it was capped, deploy-ready, blockers, contradictions between the plan and the generated code, gate results, evidence-ledger records and the share backed by your files or a vendor page, services chosen, unpriced cost lines, generated files, and "verified" claims without proof. Wall time is recorded separately and is the only value that varies between runs.

## How to compare another workflow
Fill a result file with the same fields (for example a frontier model asked for the architecture and code, or an engineer working by hand, scored by a reviewer using the same checks) and set `"tool": "name"`. `compare` prints the table. SolsArch does not check results it did not produce.

## Honest limits
- It runs offline, so it says nothing about live prices. Cost accuracy needs your real bills as input.
- A clean static check or a high score is not production success. Only a real deploy and test shows that.
- The fixtures are small and generated. Real repositories are messier.
- Known gap found while building it: a site whose only front-end dependency is Vite (no React or Vue) is not recognised as a static site. The plan then has no compute service while the generator falls back to a default server stack. The quality gate reports this as four contradictions and caps the score; add a React or Vue dependency, or choose a static platform, until the scanner learns it.
- No result here is a claim of being better than anything else. It is a tool for finding out.
