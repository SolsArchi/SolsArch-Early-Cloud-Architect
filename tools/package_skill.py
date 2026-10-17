#!/usr/bin/env python3
"""Zip the skill folder for upload (Claude.ai Settings > Capabilities > Skills).

Usage: python tools/package_skill.py [output_dir]
The zip contains the skill folder as its single top-level entry and skips caches.
"""
import os
import sys
import zipfile

SKILL = "solsarch-early-cloud-architect"
SKIP_DIRS = {"__pycache__", ".pytest_cache", ".solsarch"}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    here = os.path.dirname(os.path.abspath(__file__))
    repo = os.path.dirname(here)
    src = os.path.join(repo, SKILL)
    if not os.path.isfile(os.path.join(src, "SKILL.md")):
        print("error: SKILL.md not found in %s" % src, file=sys.stderr)
        return 1
    out_dir = os.path.abspath(argv[0]) if argv else repo
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, SKILL + ".zip")
    count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for dirpath, dirnames, filenames in os.walk(src):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for fname in sorted(filenames):
                if fname.endswith((".pyc", ".tmp")):
                    continue
                full = os.path.join(dirpath, fname)
                arc = os.path.join(SKILL, os.path.relpath(full, src)).replace(os.sep, "/")
                zf.write(full, arc)
                count += 1
    print("wrote %s (%d files)" % (out, count))
    return 0


if __name__ == "__main__":
    sys.exit(main())
