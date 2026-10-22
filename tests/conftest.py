import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
for entry in (HERE, HERE.parent / "solsarch-early-cloud-architect" / "scripts", HERE.parent / "tools"):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))
import os
os.environ.setdefault("SOLSARCH_OFFLINE", "1")
