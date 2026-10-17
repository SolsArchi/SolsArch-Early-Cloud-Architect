# Narrative for the report

Save your version as `.solsarch/narrative.md` and pass it to `scripts/build_report.py --narrative`.
Do not add decision, blocker or component tables: the script generates them from the scan and memory.
Keep each section short and written for a non-engineer.

## Executive summary

Two or three sentences: what will run where, and the single biggest change from today.
Name the top blocker if there is one.

## Cost drivers

List what will drive the monthly bill (compute hours, database size, NAT traffic, data egress, logging volume).
Give a dollar figure only with a dated source, such as a vendor pricing page or a number the user supplied.
If none, write "No estimate made" and compare the drivers to the budget ceiling in words.

## Open questions

- One line each, for the user or a stakeholder. Include anything marked Assumed that matters.
