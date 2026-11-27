# Continuous demo: what does this pull request do to the architecture?

A small FastAPI service. In five commands you save its architecture, add a streaming AI endpoint and a worker queue, and see exactly what changes.

```bash
SOLSARCH=../../solsarch-early-cloud-architect/scripts/solsarch.py
python $SOLSARCH run --repo . --cloud aws --compute server      # scan, plan, build everything
python $SOLSARCH impact baseline --repo .                        # save this as the agreed architecture
cp change/stream.py.txt app/stream.py && echo celery >> requirements.txt   # the "pull request"
python $SOLSARCH impact check --repo .                          # see the change
cat .solsarch/impact-comment.md                                 # what a reviewer would read on the PR
```

Expected: streaming turns on (with `app/stream.py` and its lines as evidence), a background worker is added, and the note explains the 120 second timeout
requirement that streaming brings. Run `finish` again to see the monthly cost and quality score change as well.
