#!/usr/bin/env bash
# ============================================================
#  Instalador do Painel Movidesk (Linux Mint / Ubuntu)
#  Uso:  bash instalar.sh
# ============================================================
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

echo "============================================"
echo " Instalador do Painel Movidesk"
echo " Pasta: $DIR"
echo "============================================"

# ---------- 1) Python ----------
if ! command -v python3 >/dev/null 2>&1; then
  echo "ERRO: python3 nao encontrado."
  echo "Instale com:  sudo apt update && sudo apt install -y python3"
  exit 1
fi
echo "[ok] $(python3 --version 2>&1)"

# O projeto usa apenas a biblioteca padrao do Python. Confere 'requests'.
if ! python3 -c "import requests" >/dev/null 2>&1; then
  echo "[aviso] O modulo 'requests' nao foi encontrado."
  echo "        Instale com:  sudo apt install -y python3-requests"
fi

# ---------- 2) .env ----------
if [ ! -f .env ]; then
  cp .env.example .env
  echo "[ok] Arquivo .env criado a partir de .env.example"
else
  echo "[ok] Arquivo .env ja existe"
fi
chmod 600 .env 2>/dev/null || true

get_env() { grep -E "^$1=" .env | head -1 | cut -d= -f2- || true; }
set_env() {
  python3 - "$1" "$2" <<'PY'
import sys, pathlib
chave, valor = sys.argv[1], sys.argv[2]
p = pathlib.Path(".env")
linhas = p.read_text(encoding="utf-8").splitlines()
achou = False
for i, l in enumerate(linhas):
    if l.startswith(chave + "="):
        linhas[i] = f"{chave}={valor}"
        achou = True
        break
if not achou:
    linhas.append(f"{chave}={valor}")
p.write_text("\n".join(linhas) + "\n", encoding="utf-8")
PY
}

echo
echo "--- Configuracao do atendente (fica em .env, nao vai pro Git) ---"
if [ -t 0 ]; then
  atual_token="$(get_env MOVIDESK_TOKEN)"
  if [ -n "$atual_token" ]; then
    echo "Token atual: ${atual_token:0:6}******** (enter = manter)"
  fi
  read -rp "Token pessoal do Movidesk (enter = manter/ignorar): " in_token
  [ -n "${in_token:-}" ] && set_env MOVIDESK_TOKEN "$in_token"

  atual_nome="$(get_env MOVIDESK_AGENT_NAME)"
  read -rp "Nome do atendente [$atual_nome]: " in_nome
  [ -n "${in_nome:-}" ] && set_env MOVIDESK_AGENT_NAME "$in_nome"

  atual_email="$(get_env MOVIDESK_AGENT_EMAIL)"
  read -rp "E-mail do atendente [$atual_email]: " in_email
  [ -n "${in_email:-}" ] && set_env MOVIDESK_AGENT_EMAIL "$in_email"
  chmod 600 .env 2>/dev/null || true
else
  echo "(sem terminal interativo) Edite o arquivo .env manualmente e preencha:"
  echo "  MOVIDESK_TOKEN, MOVIDESK_AGENT_NAME, MOVIDESK_AGENT_EMAIL"
fi

# ---------- 3) Atalho na area de trabalho e no menu ----------
PY3="$(command -v python3)"
APPS="$HOME/.local/share/applications"
mkdir -p "$APPS"

DESK=""
if command -v xdg-user-dir >/dev/null 2>&1; then
  DESK="$(xdg-user-dir DESKTOP 2>/dev/null || true)"
fi
if [ -z "$DESK" ]; then
  for d in "$HOME/Desktop" "$HOME/Área de trabalho" "$HOME/Area de trabalho"; do
    [ -d "$d" ] && DESK="$d" && break
  done
fi
[ -d "$DESK" ] || DESK=""

gerar_desktop() {
  local destino="$1"
  cat > "$destino" <<EOF
[Desktop Entry]
Version=1.0
Type=Application
Name=Painel Movidesk
Comment=Monitor de tickets do Movidesk (nao consulta automaticamente)
Exec=$PY3 $DIR/painel/server.py
Path=$DIR/painel
Icon=/usr/share/icons/hicolor/scalable/apps/org.gnome.SystemMonitor.svg
Terminal=false
Categories=Utility;
StartupNotify=false
EOF
  chmod +x "$destino" 2>/dev/null || true
}

gerar_desktop "$APPS/painel-movidesk.desktop" || true
echo "[ok] Atalho no menu: $APPS/painel-movidesk.desktop"
if [ -n "$DESK" ] && [ -d "$DESK" ]; then
  gerar_desktop "$DESK/Painel Movidesk.desktop" || true
  echo "[ok] Atalho na area de trabalho: $DESK/Painel Movidesk.desktop"
  if command -v gio >/dev/null 2>&1; then
    gio set "$DESK/Painel Movidesk.desktop" metadata::trusted true 2>/dev/null || true
  fi
fi

# ---------- 4) Pronto ----------
echo
echo "============================================"
echo " Instalacao concluida!"
echo "============================================"
echo " Para abrir o painel:"
echo "   - clique duas vezes no atalho 'Painel Movidesk', ou"
echo "   - rode:  python3 $DIR/painel/server.py"
echo
echo " O painel abre em http://127.0.0.1:$(get_env MOVIDESK_PORT || echo 8773)"
echo " IMPORTANTE: ele NAO consulta sozinho. Clique em 'Consultar' na tela."
echo

if [ -t 0 ]; then
  read -rp "Abrir o painel agora? [S/n]: " abrir
  if [ -z "${abrir:-}" ] || [[ "${abrir,,}" == "s" || "${abrir,,}" == "y" ]]; then
    nohup "$PY3" "$DIR/painel/server.py" >/dev/null 2>&1 &
    echo "Abrindo..."
  fi
fi
