import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _load_env_file() -> dict:
    """Le um .env simples (KEY=VALUE) sem dependencias externas."""
    valores: dict[str, str] = {}
    for candidato in (BASE_DIR / ".env", BASE_DIR.parent / ".env"):
        if not candidato.exists():
            continue
        try:
            linhas = candidato.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for linha in linhas:
            linha = linha.strip()
            if not linha or linha.startswith("#") or "=" not in linha:
                continue
            chave, valor = linha.split("=", 1)
            valores.setdefault(chave.strip(), valor.strip().strip('"').strip("'"))
    return valores


_ENV = _load_env_file()


def _cfg(chave: str, padrao: str = "") -> str:
    return os.environ.get(chave) or _ENV.get(chave) or padrao


def _load_token() -> str:
    token = _cfg("MOVIDESK_TOKEN").strip()
    if token:
        return token
    token_file = BASE_DIR / ".token"
    if token_file.exists():
        return token_file.read_text(encoding="utf-8").strip()
    return ""


# --- Identificacao do atendente monitorado (ajuste no .env) ---
AGENT_NAME = _cfg("MOVIDESK_AGENT_NAME", "Seu Nome Completo")
AGENT_EMAIL = _cfg("MOVIDESK_AGENT_EMAIL", "").lower()

# --- Limiares ---
CRITICAL_HOURS = 48
WARNING_HOURS = 24
SIMILARITY_THRESHOLD = 0.7
SLA_RISK_HOURS = 4

# --- Movidesk ---
API_BASE_URL = "https://api.movidesk.com/public/v1"
MOVIDESK_WEB_URL = _cfg("MOVIDESK_WEB_URL", "https://faturagil.movidesk.com")
MOVIDESK_TOKEN = _load_token()

# Reuso do cache da pesquisa de satisfacao (evita gastar a cota de 10 req/min)
SURVEY_CACHE_HOURS = 12
RATE_LIMIT_REQUESTS = 10
RATE_LIMIT_WINDOW_SECONDS = 60
REQUEST_TIMEOUT = 30
MAX_RETRIES = 4

try:
    PANEL_PORT = int(_cfg("MOVIDESK_PORT", "8773") or 8773)
except ValueError:
    PANEL_PORT = 8773

ACTIVE_STATUSES_EXCLUDED = ("Resolved", "Closed", "Canceled")

CACHE_FILE = BASE_DIR / "cache" / "tickets.json"
REPORTS_DIR = BASE_DIR / "relatorios"

OPEN_STATUSES = ("Open", "InAttendance", "Stopped", "Reopened", "Waiting")


def primeiro_nome() -> str:
    return (AGENT_NAME or "Tecnico").split()[0] if (AGENT_NAME or "").strip() else "Tecnico"
