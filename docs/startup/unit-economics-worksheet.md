# Unit economics worksheet (template)

Fill from measured values only. Run `benchmark.py run --out run.json` and use the `seconds` field.

| Item | Value | Source |
| :--- | :--- | :--- |
| Median seconds per analysis | | benchmark `seconds` |
| Compute price per hour | | your cloud bill |
| Compute cost per analysis | | seconds / 3600 x price |
| Support time per partner per week | | your log |
| Price you would test | | design partner answer |

SolsArch runs locally and offline by default, so its own compute cost may be near zero; the cost to count is support and the model tokens an agent uses to drive it.
