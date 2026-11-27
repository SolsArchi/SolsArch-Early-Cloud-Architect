# Working the way the SolsArch app does

Use this when the user asks for "the app way", a Phase 1 or full analysis, an architecture review with readiness, or staged results.
You play the reasoning agents; the scripts provide the facts and the checks.

## Modes

| | Phase 1 (default) | Full |
| :--- | :--- | :--- |
| Goal | Read, understand, draw and validate an architecture | Phase 1 plus cost, infrastructure code, documentation and deeper security |
| Stages | 0, 0.5, 1, 2, 3, 4, 5, 6, 8, 11.5, 12, 13 | all, adding 5.5, 7, 9, 9.5, 10, 11 |
| Cost | never produced and never mentioned in your words (no prices, no free-tier claims) | quoted only as the cost report gives it |
| Delivered | `docs/solsarch-phase1.zip` and `docs/solsarch-readiness.md` | `docs/solsarch-output.zip` and `docs/solsarch-readiness.md` |

Offloaded stages are skipped, not hidden: say which, and that the full mode includes them.

## Run it

```bash
python scripts/solsarch.py app intake --repo REPO                          # open questions
python scripts/solsarch.py app intake --repo REPO --set cloud=aws --set compute=server
python scripts/solsarch.py app run --repo REPO --mode phase1               # or --mode full
python scripts/solsarch.py app check --repo REPO --mode phase1             # after any fix or new input
python scripts/solsarch.py app stages --mode full                          # what each stage does
```

## Your part, stage by stage

1. **Intake.** Ask only the open questions, all at once. Unanswered ones become stated assumptions; never guess an answer for the user. Follow-ups appear only when the repository gives a reason.
2. **Codebase, API.** From the scan, describe the services, entry points, data stores and endpoints, each with its file and line. If you read more in the code, say you read it and cite it.
3. **Governance.** Read the company documents completely. A rule you can read but the scripts missed goes in a rules file as written; never add one the documents do not state.
4. **History.** If a baseline or earlier architecture notes exist, say what changed (`impact check`).
5. **Traffic.** Ask for real numbers. Without them, every load figure is an assumption and you say so.
6. **Cloud.** Recommend exactly one cloud, with the reason for each service. When it was your recommendation, name the runner-up, and say when the scores are close that it is a near tie.
7. **Diagrams.** Walk through context, containers, components, infrastructure and sequences. Do not draw a service the plan did not choose, and never draw a replaced technology as live.
8. **Security checklist.** Explain each FAIL with its file and line, and each NOT_CHECKED with how to check it. You may review CORS, authentication, debug flags and plain HTTP by reading the code; report what you saw as your own review, not as a script PASS.
9. **Validation.** Read the readiness file. Fix wording that contradicts the facts. Never change facts to satisfy a check.
10. **Summary.** Open with the recommended architecture at a glance, then blockers, then what is not verified, then next steps. Use only figures and names present in the files.

## The two statements never merge

*Analysis completeness* (0 to 100) says how much was examined: architecture understanding 25, diagram coverage 20, requirements and API 20, governance and inputs 15, security checklist 20.
*Deployment readiness* says what the evidence supports: **Blocked**, **Not verified for deployment**, or **Ready**. A complete analysis is often still Not verified. Never describe it as ready.

## What you may and may not say

| Say | Do not say |
| :--- | :--- |
| "Works across AWS, Azure and Google Cloud with the same scoring for each." | "Unbiased", "the best cloud for you" |
| "Every result lists how it was checked; anything that did not run says why." | "Fully verified", "audited", "security scanned" without naming what ran |
| "The same code and answers give the same files." (true of these scripts) | That your own written explanations are identical between runs |
| "Run it again after a change to see what moved." | "Real time", "on every change" |

## Differences from the app

The skill has no web interface, accounts or hosted runs. Its diagrams are C4, infrastructure, sequence and map views; the app also draws security, data-flow, recovery, threat-model and data-schema views, which you may describe in prose but must not present as generated. The consistency checks cover I2, I3, I7, I8, I9, I14, I15 and I16. Dependency, CORS, authentication, debug and plain-HTTP checks are NOT_CHECKED by the scripts.
