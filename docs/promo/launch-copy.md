# Launch copy

Every number below can be checked in the repository: 40 clouds and platforms (`solsarch.py questions`), the rules in
`solsarch-early-cloud-architect/scripts/rules_extract.py`, the tests in `tests/`. Do not add testimonials or user counts that do not exist.

## One line (GitHub description)

Early cloud architecture from your codebase and your company rules, not from a prompt: Terraform, diagrams, costs and a deploy guide for 40 clouds.

## Short pitch

SolsArch is a cloud architect, DevOps and AIOps assistant for your team. Point it at a repository and attach the standards you already
write (approved clouds, regions, tags, budgets, encryption, retention, compliance) as docs, PDFs, Markdown or JSON. It chooses services with
fixed rules, writes the Terraform, Kubernetes, Ansible and CI, estimates the cost, checks every company rule and hands you a deploy guide.
The same inputs give the same files, whichever AI agent runs it.

## For platform and DevOps teams

Your golden path is a document today, so every new service starts with someone reading it and hoping. Attach it once. SolsArch reads the
repo, applies the house rules (clouds, regions, tags, name prefix, log retention), generates the first architecture and infrastructure code,
and reports every rule as pass, fail or not verified with the sentence it came from. The OPA policy it writes keeps the pipeline enforcing the
same rules after day one. Exceptions need a reason, an owner and an expiry date.

## For consultancies

Compare the same codebase on AWS, Azure and Google Cloud (or any of 40 platforms) under the client's own rules. Blocked clouds are marked and
the reason is cited. Deliver a report, diagrams, a cost estimate and infrastructure code in one zip, and show where each decision came from.

## For startups

Say which kind of account you have (free trial, always-free, pay as you go). The design is sized for it and the quality gate fails a plan that
would bill by the hour on an always-free account. The deploy guide is written for someone who has never deployed.

## Social posts

1. Prompt-to-architecture tools answer differently every time you rephrase. SolsArch reads your code and your company rules instead, and
   gives the same Terraform, diagrams and cost estimate on every run. Open source, 40 clouds. https://solsarch.com/
2. Your cloud standards are a PDF nobody reads. Attach it next to the repo: SolsArch extracts the rules, limits the cloud and region, adds the
   tags to the Terraform and reports every rule with the sentence it came from.
3. Not a prompt. Your codebase plus your rules, in; architecture, IaC, costs and a deploy guide, out. No cloud keys ever shared with the tool.

## Show HN / Reddit post

**Title:** Show HN: Early cloud architecture from your codebase and your company rules (not a prompt)

I kept seeing tools that turn a prompt into an architecture diagram or Terraform. The output changes with the wording and ignores the
company's own standards. SolsArch goes the other way: a script reads the repository, a second set of scripts reads the standards you attach
(md, pdf, docx, json, yaml), and fixed rules choose the services. No model decides anything, so the same inputs give byte-identical files.

What you get in one zip: report, C4 and icon diagrams, Terraform for 17 clouds and a native config file for the app platforms, Kubernetes, Ansible, a CI
pipeline, a cost estimate with labels (live price, estimate, unpriced), a deploy guide for your account type, and a quality score out of 100
with the evidence for each point. Company rules get their own gate and an OPA policy for your pipeline.

Limits, stated in the README: it reads files and never logs in to a cloud; rules written as sentences are matched by fixed patterns, so some
are reported as "not understood"; a compliance name is a mapping to checks, not a certification; Terraform is validated, not applied.
Python standard library only. MIT or Apache-2.0.

## 60-second demo script

1. (0:00) "Here is a FastAPI service and the policies folder our platform team already has." Show `policies/cloud-standards.md`.
2. (0:10) Run `python scripts/solsarch.py rules ingest --repo .`: nine rules, each with its source sentence; one sentence listed as not enforced.
3. (0:25) Run `plan --cloud hetzner`: refused, "hetzner is not in the approved clouds", with the three allowed clouds.
4. (0:35) Run `run --repo . --cloud aws --compute server`. Open `docs/solsarch-quality.md`: gate 8, each rule PASS or FAIL.
5. (0:50) Open `terraform/company-tags.tf` and the `eu-central-1` region the rule picked. "Same inputs, same files, every time."
