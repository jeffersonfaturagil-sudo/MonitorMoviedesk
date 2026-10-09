#!/usr/bin/env bash
# ============================================================
#  Gera um zip pronto para enviar ao colega.
#  Uso:  bash empacotar.sh [caminho-do-zip]
#  Nao inclui .env, .token, cache/, relatorios/ nem __pycache__.
# ============================================================
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SAIDA="${1:-$(dirname "$DIR")/Painel-Movidesk.zip}"

python3 - "$DIR" "$SAIDA" <<'PY'
import os, sys, zipfile

origem, saida = sys.argv[1], sys.argv[2]
base = os.path.basename(origem.rstrip("/"))
excluir_dirs = {".git", "cache", "relatorios", "__pycache__", ".venv", "venv", ".idea", ".vscode"}
excluir_arqs = {".env", ".token", ".DS_Store"}

with zipfile.ZipFile(saida, "w", zipfile.ZIP_DEFLATED) as z:
    for root, dirs, files in os.walk(origem):
        dirs[:] = [d for d in dirs if d not in excluir_dirs]
        for f in files:
            if f in excluir_arqs or f.endswith((".log", ".pid", ".pyc")):
                continue
            full = os.path.join(root, f)
            rel = os.path.join(base, os.path.relpath(full, origem))
            z.write(full, rel)

print("Zip gerado:", saida)
with zipfile.ZipFile(saida) as z:
    print("Arquivos:", len(z.namelist()))
PY
