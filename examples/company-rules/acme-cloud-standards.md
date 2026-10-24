# Acme cloud standards (sample)

Copy this folder to `policies/` in your repository, or run `solsarch.py run --repo . --rules examples/company-rules`.
These are plain sentences, the way a company writes them. SolsArch picks out the ones it understands and lists the rest.

## Clouds and data

- Our approved clouds are AWS, Azure and Google Cloud only.
- All customer data must stay in the EU.
- Databases must not be publicly accessible.
- Storage buckets must never be public.

## Security

- All data must be encrypted at rest and in transit.
- Audit logs must be retained for 90 days.

## Cost and tagging

- Every resource must carry these tags: owner, cost-center.
- Monthly cloud spend must not exceed $1,500 per month.

## Not machine-checkable (listed in the report, not enforced)

- Nobody may deploy on Fridays.
- Production changes need two approvals.

## Exact form (always understood)

rule: compliance = soc2, iso27001 ; level=required
rule: ha_multi_az = true ; level=required
