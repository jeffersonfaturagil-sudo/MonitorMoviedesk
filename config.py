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


def _lista_emails(valor: str) -> set[str]:
    return {e.strip().lower() for e in valor.split(",") if e.strip()}


# --- Times: suporte = Jefferson + Lucas; dev = Leandro + Matheus ---
AGENTES_SUPORTE = _lista_emails(
    _cfg("MOVIDESK_AGENTES_SUPORTE", "jefferson@faturagil.com.br,lucas@faturagil.com.br")
)
AGENTES_DEV = _lista_emails(
    _cfg("MOVIDESK_AGENTES_DEV", "leandro@faturagil.com.br,matheus@faturagil.com.br")
)
AGENTES_IMPLANTACAO = _lista_emails(_cfg("MOVIDESK_AGENTES_IMPLANTACAO", ""))
AGENTES_COORDENACAO = _lista_emails(_cfg("MOVIDESK_AGENTES_COORDENACAO", ""))
PLANTAO_ORDEM = [
    e.strip().lower()
    for e in _cfg(
        "MOVIDESK_PLANTAO_ORDEM", "lucas@faturagil.com.br,jefferson@faturagil.com.br"
    ).split(",")
    if e.strip()
]


def papel_agente(email: str | None) -> str:
    """Retorna 'suporte', 'dev', 'implantacao', 'coordenacao' ou 'outros'."""
    e = (email or "").strip().lower()
    if not e:
        return "outros"
    if e in AGENTES_DEV:
        return "dev"
    if e in AGENTES_SUPORTE:
        return "suporte"
    if e in AGENTES_IMPLANTACAO:
        return "implantacao"
    if e in AGENTES_COORDENACAO:
        return "coordenacao"
    return "outros"


_PAPEL_PARA_TIME = {
    "suporte": "suporte",
    "dev": "dev",
    "implantacao": "implantacao",
    "coordenacao": "coordenacao",
}


def time_do_agente(email: str | None) -> str | None:
    """Time de processos correspondente ao papel do atendente (ou None)."""
    return _PAPEL_PARA_TIME.get(papel_agente(email))

# --- Limiares ---
CRITICAL_HOURS = 48
WARNING_HOURS = 24
SIMILARITY_THRESHOLD = 0.7
SLA_RISK_HOURS = 4

# --- Horario comercial (janela em que o relogio de SLA corre) ---
# Formato "HH:MM-HH:MM[,HH:MM-HH:MM]". Fins de semana e feriados (feriados.py)
# nunca contam. Padrao: 08:00-12:00 e 13:00-18:00 (almoco das 12h as 13h).
HORARIO_COMERCIAL = _cfg("MOVIDESK_HORARIO", "08:00-12:00,13:00-18:00")
# Metas de SLA (playbook de suporte/movidesk). Contadas em horario comercial.
SLA_RESPOSTA_MINUTOS = 60          # responder em ate 1h util
SLA_ENCERRAMENTO_DIAS_UTEIS = 5    # "Retorno do cliente - Para encerramento" -> 5 dias uteis
SLA_CONCLUIR_DIAS_UTEIS = 3        # "Retorno do Cliente" (sugerimos solucao) -> 3 dias uteis
# "Proximo do vencimento": restante <= esta fracao da meta (0.25 = 25%).
SLA_PROXIMO_FRACAO = 0.25

# --- Movidesk ---
API_BASE_URL = "https://api.movidesk.com/public/v1"
MOVIDESK_WEB_URL = _cfg("MOVIDESK_WEB_URL", "https://faturagil.movidesk.com")
# Base da Base de Conhecimento (onde os links de artigo fazem sentido pro cliente;
# nos playbooks os links aparecem como atendimento.faturagil.com.br/kb/article/...).
MOVIDESK_KB_URL = _cfg("MOVIDESK_KB_URL", "https://atendimento.faturagil.com.br")
MOVIDESK_TOKEN = _load_token()

# Reuso do cache da pesquisa de satisfacao (evita gastar a cota de 10 req/min)
SURVEY_CACHE_HOURS = 12
RATE_LIMIT_REQUESTS = 10
RATE_LIMIT_WINDOW_SECONDS = 60
REQUEST_TIMEOUT = 30
MAX_RETRIES = 4


def _cfg_int(chave: str, padrao: int) -> int:
    try:
        return int(_cfg(chave, str(padrao)) or padrao)
    except ValueError:
        return padrao


# Tamanho da pagina OData ($top). Verificado contra a API real: /tickets aceita
# 1000 objetos por requisicao. A doc fala em teto de 100; por isso o laco de
# paginacao (api_client._list_tickets) avanca pelo total realmente devolvido e
# aprende o teto efetivo, funcionando igualmente se a API capar a pagina.
PAGE_SIZE = max(1, _cfg_int("MOVIDESK_PAGE_SIZE", 1000))
# Teto do Retry-After lido da API (o bloqueio documentado vai ate 300s).
RETRY_AFTER_MAX_SECONDS = max(1, _cfg_int("MOVIDESK_RETRY_AFTER_MAX", 600))
# Cache das perguntas da pesquisa e das mesclas confirmadas no Movidesk.
SURVEY_QUESTIONS_CACHE_HOURS = _cfg_int("MOVIDESK_PERGUNTAS_CACHE_HOURS", 24)
MERGED_CACHE_HOURS = _cfg_int("MOVIDESK_MESCLAS_CACHE_HOURS", 12)

try:
    PANEL_PORT = int(_cfg("MOVIDESK_PORT", "8773") or 8773)
except ValueError:
    PANEL_PORT = 8773

ACTIVE_STATUSES_EXCLUDED = ("Resolved", "Closed", "Canceled")

CACHE_FILE = BASE_DIR / "cache" / "tickets.json"
REPORTS_DIR = BASE_DIR / "relatorios"

OPEN_STATUSES = ("Open", "InAttendance", "Stopped", "Reopened", "Waiting")
_TIMES_PROCESSOS = ("suporte", "dev", "implantacao", "coordenacao")
LIMIAR_MACRO = int(_cfg("LIMIAR_MACRO", "5"))


def primeiro_nome() -> str:
    return (AGENT_NAME or "Tecnico").split()[0] if (AGENT_NAME or "").strip() else "Tecnico"


# --- OpenRouter (IA) ---
OPENROUTER_API_KEY = _cfg("OPENROUTER_API_KEY", "")
OPENROUTER_MODEL = _cfg("OPENROUTER_MODEL", "openai/gpt-4o-mini")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
