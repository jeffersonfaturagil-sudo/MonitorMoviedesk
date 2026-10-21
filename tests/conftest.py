import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for caminho in (ROOT, ROOT / "painel"):
    s = str(caminho)
    if s not in sys.path:
        sys.path.insert(0, s)