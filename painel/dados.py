import html
import json
import logging
import math
import os
import sys
import threading
from contextvars import ContextVar
from datetime import date, datetime, time, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from analyzer import (  # noqa: E402
    build_ticket_info,
    client_name,
    find_merge_candidates,
    hours_between,
    nivel_risco,
    normalize_subject,
    parse_date,
    sla_risks,
)
from config import AGENT_EMAIL, AGENT_NAME, CACHE_FILE, MOVIDESK_WEB_URL, ACTIVE_STATUSES_EXCLUDED, papel_agente  # noqa: E402
# _norm/_tokens são alias da fonte única em util.py.
from util import norm_texto as _norm, tokens_simples as _tokens  # noqa: E402

SLA_LEVELS = ("VENCIDO", "RISCO")

log = logging.getLogger("painel.dados")

CACHE_LOCK = threading.RLock()

# Memória do cache por thread. Cada requisição HTTP roda na própria thread
# (ThreadingHTTPServer), então o memo vive o tempo da requisição: leituras
# repetidas do mesmo arquivo não reparseiam os ~50 MB de JSON, e mutações de
# uma requisição nunca vazam para outra. O CLI e os indexadores de fundo têm,
# cada um, o seu próprio memo. A chave inclui identidade do arquivo (caminho +
# inode) e mtime/tamanho, então trocar o arquivo (ex.: testes) invalida.
_CACHE_MEMO = threading.local()


def _cache_stamp():
    """Assinatura do arquivo de cache para invalidar o memo por thread."""
    try:
        st = CACHE_FILE.stat()
        return (str(CACHE_FILE), st.st_ino, st.st_mtime_ns, st.st_size)
    except OSError:
        return None

# Versão do schema de cache/tickets.json. Todo mundo que grava (painel e CLI)
# passa por este módulo, então o formato é sempre o mesmo.
CACHE_SCHEMA = 2
CHAVE_TODOS = "*"

# Chaves que, no formato antigo, ficavam na raiz do cache e hoje vivem no bucket
# do atendente (por_agente).
_CHAVES_BUCKET = ("ativos", "resolvidos", "interagiu_hoje", "fora_carga",
                  "salvo_em", "last_sync_odata")

# Fontes de tickets dentro de um bucket de atendente (ordem usada historicamente).
FONTES_CACHE = ("ativos", "resolvidos", "interagiu_hoje", "fora_carga")

INTERNO = "@faturagil.com.br"

LIGACAO_KEYWORDS = (
    "chamada efetuada", "chamada recebida", "contatochamada realizada", "gravação da ligação",
    "gravacao da ligacao", "tentativa de contato", "tentei contato", "tentei entrar em contato",
    "tentei ligar", "liguei", "não atendeu", "nao atendeu", "caixa postal", "correio de voz",
    "não completou", "nao completou", "chamada cancelada", "ouvi recado",
)

CANAL_TICKET = {
    1: "Cliente", 2: "Agente", 3: "E-mail", 4: "Sistema", 5: "Chat",
    6: "Chat offline", 7: "E-mail sistema", 8: "Formulário", 9: "API",
    10: "Recorrente", 11: "Jira", 12: "Redmine", 13: "Ligação atendida",
    14: "Ligação efetuada", 15: "Ligação perdida", 16: "Ligação abandonada",
    17: "Acesso remoto", 18: "WhatsApp", 19: "Movidesk integrado",
    20: "Zenvia Chat", 22: "Facebook", 23: "WhatsApp Business",
    24: "Altu", 25: "WhatsApp Ativo",
}

ORIGIN_LIGACAO = {
    13: "Ligação atendida",
    14: "Ligação efetuada",
    15: "Ligação perdida",
    16: "Ligação abandonada",
}


def canal_nome(code) -> str:
    try:
        return CANAL_TICKET.get(int(code)) or ""
    except (TypeError, ValueError):
        return ""


def _fmt_horas(h) -> str:
    if h is None:
        return ""
    try:
        h = float(h)
    except (TypeError, ValueError):
        return ""
    if h >= 24:
        dias = int(h // 24)
        return f"{dias}d {h - dias * 24:.0f}h"
    return f"{h:.1f}h"


def _fmt_min(mins) -> str:
    try:
        m = int(mins)
    except (TypeError, ValueError):
        return ""
    if m <= 0:
        return ""
    h, mm = divmod(m, 60)
    if h >= 24:
        d, hh = divmod(h, 24)
        return f"{d}d {hh}h"
    if h:
        return f"{h}h{mm:02d}"
    return f"{mm}min"

_AGENTE = ContextVar("agente_email", default=AGENT_EMAIL)


def agente_atual() -> str:
    return _AGENTE.get()


def eh_equipe(email: str) -> bool:
    return bool(email) and (email == agente_atual().lower() or email.endswith(INTERNO))


def acao_info(raw: dict) -> dict:
    """Classifica a última ação (minha x cliente) e mede tempo até a 1ª ação da equipe."""
    out = {
        "etiqueta": "",
        "ultima_acao_por": "",
        "primeira_acao_h": None,
        "criado_por": "",
        "tentativas": 0,
        "ultima_ligacao": "",
        "ligacoes_lista": [],
        "ligacoes_resumo": {},
    }
    acts = raw.get("actions")
    if not isinstance(acts, list) or not acts:
        return out
    try:
        parsed = []
        for a in acts:
            d = parse_date(a.get("createdDate"))
            if d:
                parsed.append((d, a))
        if not parsed:
            return out
        parsed.sort(key=lambda x: x[0])
        last_d, last_a = parsed[-1]

        cb = last_a.get("createdBy") or {}
        email = (cb.get("email") or "").lower()
        out["ultima_acao_por"] = cb.get("businessName") or email
        if eh_equipe(email):
            out["etiqueta"] = "Aguarda cliente"
        else:
            out["etiqueta"] = "Precisa sua resposta"

        created = parse_date(raw.get("createdDate"))
        criador = ((raw.get("createdBy") or {}).get("email") or "").lower()
        out["criado_por"] = "equipe" if eh_equipe(criador) else "cliente"
        if created and out["criado_por"] == "cliente":
            for d, a in parsed:
                em = ((a.get("createdBy") or {}).get("email") or "").lower()
                if eh_equipe(em) and (d - created).total_seconds() > 60:
                    out["primeira_acao_h"] = round((d - created).total_seconds() / 3600.0, 1)
                    break

        lig = []
        resumo_lig = {}
        for d, a in parsed:
            desc = (a.get("description") or "").strip()
            low = desc.lower()
            o = a.get("origin")
            try:
                o = int(o)
            except (TypeError, ValueError):
                o = None
            if o in ORIGIN_LIGACAO:
                tipo = ORIGIN_LIGACAO[o]
            elif any(k in low for k in LIGACAO_KEYWORDS):
                tipo = "⇦ recebida" if "chamada recebida" in low else (
                    "⇨ efetuada" if ("chamada efetuada" in low or "contatochamada realizada" in low)
                    else "Contato (nota)")
            else:
                continue
            resumo_lig[tipo] = resumo_lig.get(tipo, 0) + 1
            resumo = " ".join(desc.split())
            lig.append({
                "data": d.strftime("%d/%m/%Y %H:%M"),
                "origem": tipo,
                "autor": (a.get("createdBy") or {}).get("businessName") or "",
                "resumo": resumo[:160],
            })
        out["tentativas"] = len(lig)
        out["ligacoes_resumo"] = resumo_lig
        if lig:
            out["ultima_ligacao"] = lig[-1]["data"]
        out["ligacoes_lista"] = lig[-5:]
    except Exception:
        pass
    return out


def tempo_com_atendente(raw: dict, now: datetime) -> tuple[float | None, str]:
    """Horas desde a atribuição ao atendente atual (última entrada de ownerHistories)."""
    hs = raw.get("ownerHistories")
    if not isinstance(hs, list) or not hs:
        return None, ""
    ultimo = None
    for h in hs:
        d = parse_date(h.get("changedDate"))
        if d and (ultimo is None or d > ultimo):
            ultimo = d
    if not ultimo:
        return None, ""
    return hours_between(ultimo, now), ultimo.strftime("%d/%m/%Y")


def owner_name(raw: dict) -> str:
    own = raw.get("owner") or {}
    return own.get("businessName") or own.get("email") or ""


BLOQUEIOS = ("cliente", "dev", "fornecedor", "atendente", "indefinido")


def classificar_bloqueio(raw: dict, etiqueta: str) -> str:
    """Quem está segurando o ticket — o que realmente o mantém parado.

    Distingue o olhar do atendente (o cliente respondeu e eu preciso agir) do
    "aguarda cliente" (a bola está com o cliente) e de bloqueios externos
    (dev / fornecedor). Sem essa separação, 80% dos "críticos" eram na verdade
    tickets esperando o cliente, o que esvaziava a aba.

    Bloqueios explícitos (dev/fornecedor) vêm da justificativa e sobrepõem o
    sinal genérico de "última ação da equipe": um ticket "Enviado para Análise"
    está com o dev, ainda que a equipe tenha sido a última a escrever.
    """
    just = (raw.get("justification") or raw.get("status") or "").lower()
    tags = " ".join(str(x) for x in (raw.get("tags") or [])).lower()
    if "dev" in just or "análise" in just or "analise" in just or "desenvolvimento" in just:
        return "dev"
    if "fornecedor" in just or "fornecedor" in tags:
        return "fornecedor"
    if etiqueta == "Aguarda cliente":
        return "cliente"
    if etiqueta == "Precisa sua resposta":
        return "atendente"
    return "indefinido"


def ticket_dict(t) -> dict:
    info = acao_info(t.raw)
    now = datetime.now()
    tempo_h, desde = tempo_com_atendente(t.raw, now)
    sla_real = parse_date(t.raw.get("slaRealResponseDate"))
    sla_due = parse_date(t.raw.get("slaResponseDate"))
    sla_real_ok = None
    if sla_real and sla_due:
        sla_real_ok = sla_real <= sla_due
    serv_full = t.raw.get("serviceFull")
    servico = " / ".join(serv_full) if isinstance(serv_full, list) and serv_full else \
        " / ".join(x for x in (t.service_first, t.service_second) if x)
    pais = t.raw.get("parentTickets") or []
    filhos = t.raw.get("childrenTickets") or []
    filho_de = [str(p.get("id")) for p in pais if isinstance(p, dict) and p.get("id") is not None]
    pai_de = [str(c.get("id")) for c in filhos if isinstance(c, dict) and c.get("id") is not None]
    return {
        "id": t.id,
        "cliente": t.client,
        "assunto": t.subject,
        "status": t.status,
        "base_status": t.base_status,
        "servico": servico,
        "ultima_acao": t.last_action.strftime("%d/%m/%Y %H:%M") if t.last_action else "sem registro",
        "criado": t.created.strftime("%d/%m/%Y") if t.created else "",
        "horas_parado": round(t.hours_idle, 1) if t.hours_idle is not None else None,
        "parado_display": t.idle_display,
        "justification": clean(t.raw.get("justification") or ""),
        "dias_aberto": round(t.days_open, 1) if t.days_open is not None else None,
        "nivel": t.level,
        "risco": nivel_risco(t),
        "sla_nivel": t.sla_level,
        "sla_detalhe": t.sla_detail,
        "urgencia": t.urgency,
        "owner": owner_name(t.raw),
        "etiqueta": info["etiqueta"],
        "bloqueio": classificar_bloqueio(t.raw, info["etiqueta"]),
        "guia": guia_do_ticket(t.raw),
        "ultima_acao_por": info["ultima_acao_por"],
        "primeira_acao_h": info["primeira_acao_h"],
        "criado_por": info["criado_por"],
        "tentativas": info["tentativas"],
        "ultima_ligacao": info["ultima_ligacao"],
        "ligacoes_lista": info["ligacoes_lista"],
        "ligacoes_resumo": info.get("ligacoes_resumo") or {},
        "canal": canal_nome(t.raw.get("origin")),
        "origin": t.raw.get("origin"),
        "tags": [clean(x) for x in (t.raw.get("tags") or [])][:8],
        "fcr": bool(t.raw.get("resolvedInFirstCall")),
        "reaberto": bool(t.raw.get("reopenedIn")),
        "reaberto_em": (t.raw.get("reopenedIn") or "")[:10],
        "implantado": bool(cliente_implantado(t.client)),
        "recorrencia": _rc if (_rc := recorrencia_de(t.id)) else None,
        "tempo_util": _fmt_min(t.raw.get("lifeTimeWorkingTime")),
        "tempo_parado_util": _fmt_min(
            t.raw.get("stoppedTimeWorkingTime") or t.raw.get("stoppedTime")
        ),
        "sla_real_ok": sla_real_ok,
        "sla_real_resposta": sla_real.strftime("%d/%m/%Y %H:%M") if sla_real else "",
        "tempo_com_atendente": _fmt_horas(tempo_h),
        "com_atendente_desde": desde,
        "filho_de": filho_de,
        "pai_de": pai_de,
        "vinculado": bool(pais or filhos),
        "chamada_perdida": t.raw.get("origin") in (15, 16),
        "service_id": t.raw.get("serviceFirstLevelId"),
    }


def _acao_sugerida(d: dict) -> dict | None:
    """Por que o Suporte ainda precisa mexer neste ticket, mesmo com risco baixo.

    A aba "OK" deixa de ser "mexido nas últimas 24h" e passa a ser "sem risco
    operacional"; aí um risco BAIXO pode exigir ação do playbook (continuidade
    com 1h útil sem retorno do cliente, ou a bola com dev/fornecedor que a gente
    acompanha/cobra). ``precisa_ligar`` vem do ``guia_playbook`` (mesma régua de
    ``build_sla_vencimentos``), então não há regra sendo calculada em dobro.

    Retorna ``{"tipo": "acao"|"acompanhar", "motivo": str}`` ou ``None``.
    """
    if (d.get("guia") or {}).get("precisa_ligar"):
        return {"tipo": "acao", "motivo": "Ligar agora: 1h útil sem retorno do cliente"}
    bloqueio = d.get("bloqueio")
    if bloqueio == "fornecedor":
        return {"tipo": "acompanhar", "motivo": "Atualizar o cliente 1x/dia"}
    if bloqueio == "dev":
        return {"tipo": "acompanhar", "motivo": "Cobrar o dev e avisar o cliente"}
    return None


def build_payload(
    tickets_raw: list[dict],
    now: datetime | None = None,
    resolved_raw: list[dict] | None = None,
    survey_raw: list[dict] | None = None,
    interagiu_raw: list[dict] | None = None,
    fora_raw: list[dict] | None = None,
    agente_email: str | None = None,
) -> dict:
    if agente_email:
        _AGENTE.set(agente_email)
    now = now or datetime.now()
    infos = [build_ticket_info(t, now) for t in tickets_raw]
    infos.sort(key=lambda t: -(t.hours_idle or 0))

    critical = [t for t in infos if t.level == "CRITICO"]
    warning = [t for t in infos if t.level == "ATENCAO"]
    # criticos/atencao/ok são uma partição disjunta pelo tempo parado
    # (>48h / 24–48h / <24h) — usada pelo relatório e pelo teste de unicidade.
    ok = [t for t in infos if t.level == "OK"]
    candidates = find_merge_candidates(infos)
    sla_in_risk = [t for t in sla_risks(infos)]

    # Monta os dicts uma única vez (acao_info é caro) e particiona por id.
    dicts = [ticket_dict(t) for t in infos]
    for d in dicts:
        if (acao := _acao_sugerida(d)):
            d["acao"] = acao
    ids = lambda lista: {t.id for t in lista}
    id_crit, id_aten, id_ok = ids(critical), ids(warning), ids(ok)
    criticos_dict = [d for d in dicts if d["id"] in id_crit]
    atencao_dict = [d for d in dicts if d["id"] in id_aten]
    ok_dict = [d for d in dicts if d["id"] in id_ok]
    aguardando_cliente = [d for d in dicts if d["bloqueio"] == "cliente"]
    criticos_risco = [
        d for d in dicts
        if d["risco"] in ("ALTO", "MEDIO") and d["bloqueio"] != "cliente"
    ]
    # Aba "OK" = sem risco operacional (risco BAIXO), não "mexido nas últimas
    # 24h": parado >48h era crítico mesmo com a bola com o cliente.
    ok_risco = [d for d in dicts if d["risco"] == "BAIXO"]
    # Quem segura o ticket não é o Suporte: cliente, dev ou fornecedor.
    aguardando_terceiros = [
        d for d in dicts if d["bloqueio"] in ("cliente", "dev", "fornecedor")
    ]

    acoes = []
    for t in critical:
        acoes.append(
            f"Follow-up no ticket #{t.id} ({t.client}) — sem interação há {t.idle_display} "
            f"(status: {t.status}, segue aberto)"
        )
    for c in candidates:
        if c["action"] == "Mesclar":
            acoes.append(
                f"Mesclar ticket #{c['ticket_a']} em #{c['ticket_b']} ({c['client']}) — {c['reason'].lower()}"
            )
    for t in sla_in_risk:
        acoes.append(f"SLA {t.sla_level.lower()} no ticket #{t.id} ({t.client}) — {t.sla_detail}")

    # Tickets atendidos por mim que sairam da minha carga (reabertos/mesclados/transferidos).
    saidas_da_carga = []
    try:
        indice_mesclas = _indice_mesclas_confirmadas()
        ids_mesclados = set()
        for mid, info in (indice_mesclas or {}).items():
            ids_mesclados.add(int(mid))
            for x in (info.get("merged_ids") or []):
                ids_mesclados.add(int(x))
    except Exception:
        ids_mesclados = set()
    for t in (fora_raw or []):
        if not isinstance(t, dict) or t.get("id") is None:
            continue
        try:
            tid = int(t["id"])
        except (TypeError, ValueError):
            continue
        reaberto = bool(t.get("reopenedIn"))
        mesclado = tid in ids_mesclados
        if not (reaberto or mesclado):
            continue
        info = build_ticket_info(t, now)
        d = ticket_dict(info)
        d["reaberto"] = reaberto
        d["mesclado"] = mesclado
        saidas_da_carga.append(d)
    saidas_da_carga.sort(key=lambda x: (-(x.get("horas_parado") or 0), x["id"]))

    pesquisa = build_pesquisa(infos, resolved_raw or [], survey_raw or [],
                              perguntas=(load_cache_raw().get("survey_questions") or {}))
    pendencias = build_pendencias(infos, now)
    agenda = build_agenda(
        infos,
        resolved_raw or [],
        interagiu_raw or [],
        fora_raw or [],
        now,
        agente_nome=_agent_display(agente_email or agente_atual()),
    )
    for item in pesquisa["itens"]:
        if item["insatisfeito"]:
            acoes.append(
                f"Cliente insatisfeito no ticket #{item['ticket']} ({item['cliente']}) — "
                f"{item['rotulo']}{(': ' + item['comentario']) if item['comentario'] else ''}"
            )

    sla_oficial = any((t.get("slaSolutionDate") or t.get("slaResponseDate")) for t in tickets_raw if isinstance(t, dict))
    return {
        "gerado_em": now.isoformat(timespec="seconds"),
        "movidesk_web": MOVIDESK_WEB_URL,
        "agente": agente_atual(),
        "sla_oficial_disponivel": sla_oficial,
        "resumo": {
            "ativos": len(infos),
            "criticos": len(critical),
            "atencao": len(warning),
            "ok": len(ok),
            "ok_risco": len(ok_risco),
            "mesclas": len(candidates),
            "sla": len(sla_in_risk),
            "risco_alto": sum(1 for d in dicts if d["risco"] == "ALTO"),
            "risco_medio": sum(1 for d in dicts if d["risco"] == "MEDIO"),
            "criticos_risco": len(criticos_risco),
            "aguardando_cliente": len(aguardando_cliente),
            "aguardando_terceiros": len(aguardando_terceiros),
            "aguardando_dev": sum(1 for d in dicts if d["bloqueio"] == "dev"),
            "aguardando_fornecedor": sum(1 for d in dicts if d["bloqueio"] == "fornecedor"),
            "sem_contato": sum(1 for d in dicts if d["bloqueio"] == "indefinido"),
        },
        "criticos": criticos_dict,
        "criticos_risco": criticos_risco,
        "atencao": atencao_dict,
        "ok": ok_dict,
        "ok_risco": ok_risco,
        "aguardando_terceiros": aguardando_terceiros,
        "aguardando_cliente": aguardando_cliente,
        "mesclas": [
            {
                "ticket_a": c["ticket_a"],
                "ticket_b": c["ticket_b"],
                "cliente": c["client"],
                "motivo": c["reason"],
                "acao": c["action"],
                "pai": c["parent"],
                "assunto_a": c["subject_a"],
                "assunto_b": c["subject_b"],
                "similaridade": round(c["similarity"], 2),
                "situation_a": c.get("situation_a", ""),
                "situation_b": c.get("situation_b", ""),
                "service_a": c.get("service_a", ""),
                "service_b": c.get("service_b", ""),
                "ja_vinculado": c.get("ja_vinculado", False),
            }
            for c in candidates
        ],
        "sla": [ticket_dict(t) for t in sla_in_risk],
        "pesquisa": pesquisa,
        "pesquisa_periodo": (load_cache_raw().get("survey_periodo") or {}),
        "pendencias": pendencias,
        "agenda": agenda,
        "saidas_da_carga": saidas_da_carga,
        "acoes": acoes,
    }


def survey_assessment(response: dict) -> tuple[str, bool, bool]:
    tipo = response.get("type")
    valor = response.get("value")
    satisfeito = insatisfeito = False
    if tipo == 1:
        rotulo = {1: "Satisfeito", 2: "Insatisfeito"}.get(valor, f"valor {valor}")
        satisfeito, insatisfeito = valor == 1, valor == 2
    elif tipo == 2:
        rotulo = {1: "Muito insatisfeito", 2: "Insatisfeito", 3: "Neutro",
                  4: "Satisfeito", 5: "Muito satisfeito"}.get(valor, f"valor {valor}")
        satisfeito = isinstance(valor, int) and valor >= 4
        insatisfeito = isinstance(valor, int) and 1 <= valor <= 2
    elif tipo == 3:
        rotulo = f"NPS {valor}"
        satisfeito = isinstance(valor, int) and valor >= 9
        insatisfeito = isinstance(valor, int) and valor <= 6
    elif tipo == 4:
        rotulo = {1: "Sim", 2: "Não"}.get(valor, f"valor {valor}")
        satisfeito, insatisfeito = valor == 1, valor == 2
    else:
        rotulo = str(valor)
    return rotulo, satisfeito, insatisfeito


def pergunta_pesquisa(response: dict, perguntas: dict | None = None) -> str:
    """Enunciado real da pergunta (GET /survey/questions) para uma resposta.

    A resposta da pesquisa só traz ``questionId``; o texto oficial vem do catálogo
    de perguntas. Sem catálogo (ou sem a pergunta nele), devolve "" para o painel
    cair no rótulo por type/value.
    """
    if not isinstance(perguntas, dict):
        return ""
    qid = str((response or {}).get("questionId") or "").strip()
    if not qid:
        return ""
    entrada = perguntas.get(qid)
    if isinstance(entrada, dict):
        return str(entrada.get("pergunta") or "").strip()
    if isinstance(entrada, str):
        return entrada.strip()
    return ""


def build_pesquisa(infos: list, resolved_raw: list[dict], survey_raw: list[dict],
                   perguntas: dict | None = None) -> dict:
    from analyzer import build_ticket_info, client_name, parse_date

    mapa: dict[int, tuple[str, str, str]] = {t.id: (t.client, t.subject, owner_name(t.raw)) for t in infos}
    now = datetime.now()
    for r in resolved_raw:
        try:
            tid = int(r.get("id"))
        except (TypeError, ValueError):
            continue
        if tid not in mapa:
            info = build_ticket_info(r, now)
            mapa[tid] = (info.client, info.subject, owner_name(r))

    sem_ticket = 0
    itens = []
    for r in survey_raw:
        tid = r.get("ticketId")
        if not isinstance(tid, int) or tid not in mapa:
            sem_ticket += 1
            continue
        cliente, assunto, owner = mapa[tid]
        ...
        cliente, assunto, owner = mapa[tid]
        rotulo, satisfeito, insatisfeito = survey_assessment(r)
        pergunta = pergunta_pesquisa(r, perguntas)
        data = parse_date(r.get("responseDate"))
        itens.append({
            "ticket": tid,
            "cliente": clean(cliente),
            "assunto": clean(assunto),
            "owner": clean(owner or ""),
            "data": data.strftime("%d/%m/%Y %H:%M") if data else "",
            "data_iso": data.isoformat() if data else "",
            "rotulo": rotulo,
            "pergunta": pergunta or rotulo,
            "pergunta_oficial": bool(pergunta),
            "question_id": str(r.get("questionId") or ""),
            "satisfeito": satisfeito,
            "insatisfeito": insatisfeito,
            "comentario": clean(r.get("commentary") or ""),
            "tipo": r.get("type"),
            "valor": r.get("value"),
        })
    itens.sort(key=lambda x: x["data_iso"], reverse=True)

    satisfeitos = sum(1 for i in itens if i["satisfeito"])
    insatisfeitos = sum(1 for i in itens if i["insatisfeito"])
    neutros = len(itens) - satisfeitos - insatisfeitos
    respondidos = satisfeitos + insatisfeitos
    pct = round(100 * satisfeitos / respondidos) if respondidos else None

    nps_valores = [i["valor"] for i in itens if i["tipo"] == 3 and isinstance(i["valor"], int)]
    nps = None
    if nps_valores:
        prom = sum(1 for v in nps_valores if v >= 9)
        det = sum(1 for v in nps_valores if v <= 6)
        nps = round(100 * (prom - det) / len(nps_valores))

    datas = sorted(i["data_iso"] for i in itens if i["data_iso"])
    periodo = ""
    if datas:
        primeira = datetime.fromisoformat(datas[0]).strftime("%d/%m/%Y")
        ultima = datetime.fromisoformat(datas[-1]).strftime("%d/%m/%Y")
        periodo = f"{primeira} a {ultima}"

    return {
        "resumo": {
            "total": len(itens),
            "satisfeitos": satisfeitos,
            "insatisfeitos": insatisfeitos,
            "neutros": neutros,
            "pct_satisfeitos": pct,
            "nps": nps,
            "periodo": periodo,
            "sem_ticket": sem_ticket,
        },
        "itens": itens,
    }




def is_aguarda_cliente(just: str) -> bool:
    if not just:
        return False
    j=just.lower()
    return 'retorno do cliente' in j

def cliente_respondido(t, now):
    try:
        la = t.last_action
        if not la:
            return False
        la_cmp, now_cmp = la, now
        if la_cmp.tzinfo and not now_cmp.tzinfo:
            now_cmp = now_cmp.replace(tzinfo=la_cmp.tzinfo)
        elif now_cmp.tzinfo and not la_cmp.tzinfo:
            la_cmp = la_cmp.replace(tzinfo=now_cmp.tzinfo)
        h = (now_cmp - la_cmp).total_seconds() / 3600.0
        j = (t.raw.get("justification") or t.status or "").lower()
        if h <= 2 and "retorno do cliente" in j:
            return True
    except Exception:
        pass
    return False

def build_pendencias(infos: list, now: datetime) -> dict:
    todos = [t for t in infos if t.base_status not in ACTIVE_STATUSES_EXCLUDED]
    todos_sorted = sorted(todos, key=lambda t: (t.last_action.timestamp() if t.last_action else 1e18))
    aguardando_retorno = [t for t in todos if is_aguarda_cliente((t.raw.get('justification') or t.status))]
    aguardando_eu = [t for t in todos if not is_aguarda_cliente((t.raw.get('justification') or t.status))]
    sem_just = [t for t in todos if not (t.raw.get('justification') or '')]
    # kanban
    def bucket(t):
        j=t.raw.get('justification') or t.status or ''
        jl=j.lower()
        if 'dev' in jl or 'análise' in jl or 'desenvolvimento' in jl:
            return 'dev'
        if 'retorno do cliente' in jl:
            return 'retorno'
        if (t.status or '').lower() in ('em atendimento','em_atendimento'):
            return 'em_atendimento'
        return 'aguardando'
    kanban={'aguardando':[],'em_atendimento':[],'retorno':[],'dev':[]}
    for t in todos_sorted:
        b=bucket(t); kanban[b].append(ticket_dict(t))
    todos_dicts = [dict(ticket_dict(t), justification=(t.raw.get('justification') or t.status),
                        cliente_respondido=cliente_respondido(t, now)) for t in todos_sorted]
    from horario import minutos_uteis_entre, minutos_por_dia

    candidatos_encerramento = []
    for t, d in zip(todos_sorted, todos_dicts):
        j = (t.raw.get('justification') or t.status or '').lower()
        if 'retorno do cliente' in j and 'encerramento' in j:
            continue
        last = t.last_action
        espera_dias = None
        if last:
            la, nc = last, now
            if la.tzinfo and not nc.tzinfo:
                nc = nc.replace(tzinfo=la.tzinfo)
            elif nc.tzinfo and not la.tzinfo:
                la = la.replace(tzinfo=nc.tzinfo)
            m = minutos_uteis_entre(la, nc)
            if m is not None and m >= 0:
                espera_dias = round(m / minutos_por_dia(), 1)
        tent = d.get('tentativas', 0) or 0
        status = (d.get('status') or '').strip()
        ult_por_equipe = d.get('etiqueta') == 'Aguarda cliente'
        if (tent > 0 and espera_dias is not None and espera_dias > 1) or (
            status in ('Em atendimento', 'Novo') and ult_por_equipe):
            candidatos_encerramento.append({
                'id': d['id'],
                'cliente': d['cliente'],
                'assunto': d['assunto'],
                'status': status,
                'ultima_acao': d['ultima_acao'],
                'tentativas': tent,
                'espera_util_dias': espera_dias,
            })
    candidatos_encerramento.sort(key=lambda x: (-(x['espera_util_dias'] or 0), x['id']))
    tempos = sorted(x['primeira_acao_h'] for x in todos_dicts if x.get('primeira_acao_h') is not None)
    resumo = {
        'aguarda_cliente': sum(1 for x in todos_dicts if x.get('etiqueta') == 'Aguarda cliente'),
        'precisa_resposta': sum(1 for x in todos_dicts if x.get('etiqueta') == 'Precisa sua resposta'),
        'cliente_respondeu': sum(1 for x in todos_dicts if x.get('cliente_respondido')),
        'primeira_acao_media': round(sum(tempos) / len(tempos), 1) if tempos else None,
        'primeira_acao_mediana': round(tempos[len(tempos) // 2], 1) if tempos else None,
        'primeira_acao_total': len(tempos),
        'criado_pelo_cliente': sum(1 for x in todos_dicts if x.get('criado_por') == 'cliente'),
    }
    return {
        'todos': todos_dicts,
        'resumo': resumo,
        'aguardando_retorno':[dict(ticket_dict(t),justification=(t.raw.get('justification') or t.status)) for t in aguardando_retorno],
        'aguardando_eu':[dict(ticket_dict(t),justification=(t.raw.get('justification') or t.status)) for t in aguardando_eu],
        'sem_just':[dict(ticket_dict(t),justification='') for t in sem_just],
        'kanban':kanban,
        'candidatos_encerramento': candidatos_encerramento,
    }


def build_agenda(
    infos: list,
    resolvidos_raw: list[dict],
    interagiu_raw: list[dict],
    fora_raw: list[dict],
    now: datetime,
    agente_nome: str | None = None,
    modo: str = "anterior",
) -> dict:
    from analyzer import build_ticket_info
    from feriados import ultimo_dia_util_antes, dias_fechados_entre

    hoje = now.strftime("%Y-%m-%d")
    abertos = ACTIVE_STATUSES_EXCLUDED
    tecnico = agente_nome or AGENT_NAME or "Não configurado"

    # Janela da agenda:
    #   "anterior" (padrão): do último dia útil anterior até 23:59 de ontem.
    #   "hoje": de 00:00 de hoje até o momento atual (agenda do dia parcial).
    modo = "hoje" if modo == "hoje" else "anterior"
    if modo == "hoje":
        dia_base_date = now.date()
        dia_base = datetime.combine(now.date(), time.min)
        inicio = dia_base
        fim_exclusive = now
        dia_final = now.date()
        dias_cobertos = 1
    else:
        dia_base_date = ultimo_dia_util_antes(now.date())
        dia_base = datetime.combine(dia_base_date, time.min)
        inicio = dia_base
        fim_exclusive = now.replace(hour=0, minute=0, second=0, microsecond=0)
        dia_final = (fim_exclusive - timedelta(days=1)).date()
        dias_cobertos = (fim_exclusive.date() - dia_base_date).days
    dia_base_str = dia_base.strftime("%Y-%m-%d")
    fechados = dias_fechados_entre(dia_base_date, fim_exclusive.date())
    modo_label = "hoje (até o momento)" if modo == "hoje" else "anterior"

    def entre_base_ate_ontem(v: str | None) -> bool:
        if not v or len(v) < 10:
            return False
        try:
            dv = datetime.strptime(v[:10], "%Y-%m-%d")
        except ValueError:
            return False
        if dv >= inicio and dv < fim_exclusive:
            return True
        return False

    fora_da_carga = [build_ticket_info(r, now) for r in fora_raw]
    fora_da_carga = [t for t in fora_da_carga if t.base_status not in ACTIVE_STATUSES_EXCLUDED]
    parados = [t for t in infos if t.level == "CRITICO"]
    atencao = [t for t in infos if t.level == "ATENCAO"]

    interagiu = sorted(
        [build_ticket_info(r, now) for r in interagiu_raw],
        key=lambda t: -(t.hours_idle or 0),
    )

    todos_res: dict[int, dict] = {int(r["id"]): r for r in resolvidos_raw if r.get("id")}
    for r in interagiu_raw:
        if r.get("id"):
            todos_res.setdefault(int(r["id"]), r)

    resolvidos_base = []
    for r in todos_res.values():
        dia_resolvido = (r.get("resolvedIn") or "")
        dia_fechado = (r.get("closedIn") or "")
        if entre_base_ate_ontem(dia_resolvido) or entre_base_ate_ontem(dia_fechado):
            resolvidos_base.append(build_ticket_info(r, now))
    resolvidos_base.sort(key=lambda t: t.id)
    resolvidos_ids = {t.id for t in resolvidos_base}

    em_atendimento = [
        t for t in infos + fora_da_carga
        if t.base_status not in abertos
        and t.last_action is not None
    ]
    limite_48h = now - timedelta(hours=48)
    for t in em_atendimento:
        la = t.last_action
        if la and la.tzinfo is not None and limite_48h.tzinfo is None:
            limite_48h = limite_48h.replace(tzinfo=la.tzinfo)
    em_atendimento = [t for t in em_atendimento if t.last_action >= limite_48h]
    em_atendimento.sort(key=lambda t: t.id)

    def just(t) -> str:
        return t.raw.get("justification") or t.status

    cabecalho = f"TÉCNICO: {tecnico} Data: {now.strftime('%d/%m/%Y')}"
    if modo == "hoje":
        cabecalho += (
            f"\n📅 PERÍODO: {dia_base.strftime('%d/%m/%Y')} 00:00 até "
            f"{now.strftime('%d/%m/%Y %H:%M')} (até o momento)"
        )
    linhas = [
        cabecalho,
        "",
        "🚨 PLANTÃO: SIM",
        "",
        "📍 LOCAL DE TRABALHO: CASA",
        "",
        "⏲️ AGENDA DO DIA",
        "",
        " (08:30 - 09:30) RECEPCIONAR OS TICKETS NOVOS",
        " (09:30 - 10:30) VERIFICAR OS TICKETS EM ATENDIMENTO ",
        " (10:30 - 12:00) RECEPCIONAR OS TICKETS NOVOS",
        " (13:30 - 14:30 - 15:30 - 16:30 - 17:30) CONFERIR E RECEPCIONAR OS TICKETS NOVOS",
        " (17:30 - 18:00) CONFERIR E ATUALIZAR TICKETS PARADOS",
        "",
        "",
        f"🥳 TICKETS RESOLVIDOS = {len(resolvidos_base)}",
        "",
        "",
    ]
    linhas += [f"   Ticket - {t.id}" for t in resolvidos_base]
    linhas += ["", "", "⏭️ EM ATENDIMENTO", "", ""]
    linhas += [f"   Ticket - {t.id} - {just(t)}" for t in em_atendimento]
    linhas += ["", "", "🆘 TICKETS PARADOS", "", ""]
    linhas += [
        f"   Ticket - {t.id} - parado há {t.idle_display} - {clean(t.subject)[:70]}"
        for t in parados
    ]
    linhas += ["", "", "🆘 PRECISA DE AJUDA ( descrição breve/objetiva)", "", ""]
    texto = "\n".join(linhas)

    return {
        "data": now.strftime("%d/%m/%Y"),
        "tecnico": tecnico,
        "texto": texto,
        "periodo": {
            "de": dia_base.strftime("%d/%m/%Y"),
            "de_iso": dia_base_str,
            "ate": dia_final.strftime("%d/%m/%Y"),
            "ate_iso": dia_final.strftime("%Y-%m-%d"),
            "dias": dias_cobertos,
            "fechados": [d.strftime("%d/%m/%Y") for d in fechados],
            "modo": modo,
            "modo_label": modo_label,
            "ate_hora": now.strftime("%H:%M") if modo == "hoje" else "23:59",
        },
        "resumo": {
            "resolvidos": len(resolvidos_base),
            "resolvidos_base_dia": dia_base_str,
            "dias_cobertos": dias_cobertos,
            "em_atendimento": len(em_atendimento),
            "interagiu": len(interagiu),
            "parados": len(parados),
            "atencao": len(atencao),
            "fora_carga": len(fora_da_carga),
        },
        "resolvidos": [ticket_dict(t) for t in resolvidos_base],
        "resolvidos_ontem": [],
        "em_atendimento": [dict(ticket_dict(t), justification=just(t)) for t in em_atendimento],
        "interagiu": [ticket_dict(t) for t in interagiu],
        "parados": [ticket_dict(t) for t in parados],
        "fora_carga": [dict(ticket_dict(t), justification=just(t)) for t in fora_da_carga],
    }


def build_agenda_from_cache(agente: str | None = None, modo: str = "anterior") -> dict | None:
    """Monta a agenda direto do cache, sem tocar na API. Usado pelo seletor de
    período da aba Agenda (dia anterior / hoje até o momento)."""
    agente = (agente or AGENT_EMAIL).lower()
    raw = load_cache_raw()
    bucket = bucket_do_agente(agente, raw=raw)
    if not bucket or not isinstance(bucket.get("ativos"), list) or not bucket["ativos"]:
        return None
    now = datetime.now()
    infos = [build_ticket_info(t, now) for t in bucket["ativos"]]
    infos.sort(key=lambda t: -(t.hours_idle or 0))
    return build_agenda(
        infos,
        bucket.get("resolvidos") or [],
        bucket.get("interagiu_hoje") or [],
        bucket.get("fora_carga") or [],
        now,
        agente_nome=_agent_display(agente),
        modo=modo,
    )


def definir_agente(email: str = ""):
    """Define o atendente ativo (perfil) para as buscas de historico/parecidos."""
    _AGENTE.set((email or AGENT_EMAIL).lower())


def _agente_escopo():
    """Retorna (email, nome_normalizado) do atendente selecionado, ou (None, None)
    quando "Todos os atendentes" (fila `*`/vazia). Usado para limitar buscas ao perfil."""
    ag = agente_atual().lower()
    if not ag or ag == "*":
        return None, None
    nome = None
    for a in load_cache_raw().get("agentes") or []:
        if isinstance(a, dict) and (a.get("email") or "").lower() == ag:
            nome = _norm(a.get("nome") or a.get("businessName") or "")
            break
    return ag, nome


def _pertence_agente(t: dict, ag: str, anome: str) -> bool:
    ow = t.get("owner") or {}
    if (ow.get("email") or "").lower() == ag:
        return True
    if anome and _norm(ow.get("businessName") or ow.get("nome") or "") == anome:
        return True
    return False


def _entries_do_cache(incluir_ativos: bool = False) -> list[dict]:
    """Coleta deduplicada de tickets do cache: histórico (+ ativos/resolvidos
    dos buckets por_agente, se ``incluir_ativos``). Sem filtro de perfil."""
    ordem = ("historico",) + (("ativos", "resolvidos") if incluir_ativos else ("resolvidos",))
    return [t for t, _origem in iter_cache_tickets(ordem=ordem, por_bucket=True)]


def _filtrar_por_agente(entries: list[dict], ag: str | None = None,
                        anome: str | None = None) -> list[dict]:
    """Filtra tickets pelo atendente ativo (escopo do perfil). Sem agente = tudo."""
    if ag is None:
        ag, anome = _agente_escopo()
    if not ag:
        return list(entries)
    return [t for t in entries if _pertence_agente(t, ag, anome or "")]


def _historico_entries() -> list[dict]:
    return _filtrar_por_agente(_entries_do_cache(incluir_ativos=False))


def pertence_ao_escopo(tid) -> bool:
    """True se o ticket está no escopo do atendente ativo (ou escopo global)."""
    ag, anome = _agente_escopo()
    if not ag:
        return True
    try:
        alvo = int(tid)
    except (TypeError, ValueError):
        return False
    for t in _entries_do_cache(incluir_ativos=True):
        if int(t.get("id") or 0) == alvo:
            return _pertence_agente(t, ag, anome or "")
    return False


def _client_name(t: dict) -> str:
    cl = t.get("clients")
    if isinstance(cl, list) and cl:
        return cl[0].get("businessName") or cl[0].get("name") or ""
    return ""


def _owner_name(t: dict) -> str:
    ow = t.get("owner") or {}
    return ow.get("businessName") or ow.get("email") or ""


def buscar_historico(q: str, limite: int = 30) -> dict:
    from difflib import SequenceMatcher

    tokens = _tokens(q)
    consulta = " ".join(tokens)
    entries = _historico_entries()
    raw = load_cache_raw()
    solucoes = raw.get("solucoes") if isinstance(raw.get("solucoes"), dict) else {}
    resultados = []
    for t in entries:
        assunto = t.get("subject") or ""
        cliente = _client_name(t)
        serv = " ".join(filter(None, [t.get("serviceFirstLevel") or "", t.get("serviceSecondLevel") or ""]))
        alvo = " ".join([assunto, cliente, serv, t.get("justification") or ""])
        alvo_tok = set(_tokens(alvo))
        score = 0
        for tok in tokens:
            if tok in alvo_tok:
                score += 3 if tok in set(_tokens(assunto)) else 1
        ratio = 0.0
        if consulta:
            ratio = SequenceMatcher(None, consulta, " ".join(_tokens(alvo))).ratio()
            if ratio >= 0.6:
                score += int(ratio * 6)
        if not tokens:
            score = 0
        if score <= 0 and tokens:
            continue
        resultado = {
            "ticket": t.get("id"),
            "assunto": assunto,
            "cliente": cliente,
            "servico": serv,
            "canal": canal_nome(t.get("origin")),
            "owner": _owner_name(t),
            "resolvido_em": (t.get("resolvedIn") or "")[:10],
            "status": t.get("status") or "",
            "justification": t.get("justification") or "",
            "score": score,
            "similaridade": round(ratio, 2),
            "tem_solucao": str(t.get("id")) in solucoes,
        }
        resultados.append(resultado)
    resultados.sort(key=lambda r: (-r["score"], -r["similaridade"]))
    return {"total_indice": len(entries), "termos": tokens, "itens": resultados[:limite]}


def _all_active_entries() -> list[dict]:
    """Tickets ativos de todos os atendentes no cache, sem repetir id."""
    return [t for t, _origem in iter_cache_tickets(ordem=("ativos",))]


_DIAS_SEMANA = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]


def build_mapa_horarios(agente: str | None = None) -> dict:
    """Mapa de calor da abertura de tickets: dia da semana x hora.

    Junta todos os tickets conhecidos no cache (ativos + resolvidos + historico).
    Com ``agente`` (e-mail), filtra pela carga daquele atendente.
    """
    from analyzer import parse_date

    agente = (agente or "").lower()
    escopo_time = agente in ("", "*")

    # Mapa de calor = ativos + resolvidos + histórico (a mesma união usada antes).
    entradas = {int(t["id"]): t
                for t, _origem in iter_cache_tickets(ordem=("historico", "ativos", "resolvidos"),
                                                     por_bucket=True)}

    if not escopo_time:
        filtradas = {}
        for tid, t in entradas.items():
            ow = ((t.get("owner") or {}).get("email") or "").lower()
            if ow == agente:
                filtradas[tid] = t
        entradas = filtradas

    matriz = [[0] * 24 for _ in range(7)]
    datas: set[str] = set()
    for t in entradas.values():
        dt = parse_date(t.get("createdDate"))
        if not dt:
            continue
        matriz[dt.weekday()][dt.hour] += 1
        datas.add(dt.strftime("%Y-%m-%d"))

    por_hora = [sum(matriz[d][h] for d in range(7)) for h in range(24)]
    por_dia = [sum(row) for row in matriz]
    max_celula = max((max(row) for row in matriz), default=0)
    pico_dia = max(range(7), key=lambda d: por_dia[d]) if max_celula else 0
    pico_hora = max(range(24), key=lambda h: por_hora[h]) if max_celula else 0

    # por_agente: comparativo de quem atende mais em qual horário (só no escopo time)
    por_agente = {}
    if escopo_time:
        nomes = {}
        raw = load_cache_raw()
        for a in (raw.get("agentes") or []):
            if isinstance(a, dict) and a.get("email"):
                nomes[a["email"].lower()] = a.get("nome") or a["email"]
        for t in entradas.values():
            dt = parse_date(t.get("createdDate"))
            if not dt:
                continue
            ow = ((t.get("owner") or {}).get("email") or "").lower()
            if not ow:
                continue
            agg = por_agente.setdefault(ow, {"email": ow, "nome": nomes.get(ow, ow),
                                            "por_hora": [0]*24, "por_dia": [0]*7, "total": 0})
            agg["por_hora"][dt.hour] += 1
            agg["por_dia"][dt.weekday()] += 1
            agg["total"] += 1
        # ordenar por total desc
        por_agente = dict(sorted(por_agente.items(), key=lambda kv: -kv[1]["total"]))

    return {
        "matriz": matriz,
        "dias": _DIAS_SEMANA,
        "horas": list(range(24)),
        "total": sum(por_dia),
        "max": max_celula,
        "max_por_hora": max(por_hora) if max_celula else 0,
        "por_hora": por_hora,
        "por_dia": por_dia,
        "pico": {"dia": pico_dia, "dia_nome": _DIAS_SEMANA[pico_dia], "hora": pico_hora, "n": matriz[pico_dia][pico_hora]},
        "apice_hora": {"hora": pico_hora, "n": por_hora[pico_hora], "dia_nome": _DIAS_SEMANA[pico_dia]},
        "cobertura": {
            "de": min(datas) if datas else "",
            "ate": max(datas) if datas else "",
            "dias": len(datas),
        },
        "por_agente": por_agente,
    }


def build_parecidos(tid: int, modo: str = "parecidos") -> dict:
    """Tickets resolvidos parecidos (ou de mesmo assunto) a um ticket aberto."""
    alvo = None
    for t in _all_active_entries() + _historico_entries():
        if int(t.get("id") or 0) == tid:
            alvo = t
            break
    if alvo is None:
        return {"erro": f"ticket {tid} não encontrado no cache"}
    assunto = alvo.get("subject") or ""
    q = " ".join(
        filter(
            None,
            [
                assunto,
                _client_name(alvo),
                alvo.get("serviceFirstLevel") or "",
                alvo.get("serviceSecondLevel") or "",
            ],
        )
    )
    res = buscar_historico(q, limite=15)
    itens = [i for i in res["itens"] if i["ticket"] != tid]
    if modo == "assunto":
        chave = " ".join(_tokens(assunto))
        itens = (
            [i for i in itens if " ".join(_tokens(i.get("assunto") or "")) == chave]
            if chave
            else []
        )
    return {
        "ticket": tid,
        "assunto": assunto,
        "modo": modo,
        "itens": itens,
        "total_indice": res["total_indice"],
        "termos": res["termos"],
    }


def _cache_salvo_em(raw: dict) -> str:
    """Timestamp do sync mais recente entre os buckets do cache (para a idade)."""
    salvo = raw.get("salvo_em")
    for bucket in (raw.get("por_agente") or {}).values():
        s = bucket.get("salvo_em") if isinstance(bucket, dict) else None
        if s and (not salvo or s > salvo):
            salvo = s
    return salvo or ""


def _plantao_nome(raw: dict, email: str) -> str:
    if not email:
        return ""
    return _owner_label(raw, email) or email


def _plantao_dias(por_data: dict, raw: dict, inicio: "date", n: int,
                  hoje: str, so_uteis: bool = False) -> list[dict]:
    """Monta uma lista de dias (úteis ou corridos) com quem está de plantão.

    Fins de semana e feriados entram marcados como não úteis; com ``so_uteis``
    eles são pulados. ``n`` conta apenas os dias devolvidos.
    """
    from feriados import eh_dia_util, eh_feriado

    d = inicio
    out: list[dict] = []
    guarda = 0
    while len(out) < n and guarda < n * 8 + 60:
        guarda += 1
        iso = d.isoformat()
        util = eh_dia_util(d)
        if so_uteis and not util:
            d += timedelta(days=1)
            continue
        email = (por_data.get(iso) or "").lower()
        out.append({
            "data": iso,
            "dow": d.weekday(),
            "util": util,
            "feriado": bool(eh_feriado(d)),
            "email": email,
            "nome": _plantao_nome(raw, email),
            "hoje": iso == hoje,
        })
        d += timedelta(days=1)
    return out


def build_plantao(hoje_iso: str | None = None) -> dict:
    hoje = hoje_iso or date.today().isoformat()
    raw = load_cache_raw()
    plantao = raw.get("plantao") if isinstance(raw.get("plantao"), dict) else {}
    por_data = plantao.get("por_data") if isinstance(plantao.get("por_data"), dict) else {}
    hoje_email = (por_data.get(hoje) or "").lower()

    ativos = _all_active_entries()
    # "Novos do dia" enxerga também o que já foi resolvido hoje (senão o ticket
    # criado e encerrado no mesmo dia desaparecia da contagem do plantão).
    todos = [t for t, _ in iter_cache_tickets(ordem=("ativos", "resolvidos"))]
    novos = []
    for t in todos:
        created = (t.get("createdDate") or "")[:10]
        if created != hoje:
            continue
        ow = t.get("owner") or {}
        owner_email = (ow.get("email") or "").lower()
        novos.append({
            "ticket": t.get("id"),
            "assunto": t.get("subject") or "",
            "cliente": _client_name(t),
            "owner": ow.get("businessName") or owner_email or "(sem responsável)",
            "owner_email": owner_email,
            "status": t.get("status") or "",
            "criado_em": (t.get("createdDate") or "")[11:16],
            "sem_responsavel": not owner_email,
            "do_plantao": bool(hoje_email) and owner_email == hoje_email,
            "resolvido": (t.get("baseStatus") or "") in ACTIVE_STATUSES_EXCLUDED,
        })
    novos.sort(key=lambda x: (not x["do_plantao"], not x["sem_responsavel"], x["criado_em"]))

    resumo = {
        "novos": len(novos),
        "do_plantao": sum(1 for x in novos if x["do_plantao"]),
        "sem_responsavel": sum(1 for x in novos if x["sem_responsavel"]),
        "resolvidos": sum(1 for x in novos if x["resolvido"]),
        "outros": sum(1 for x in novos
                      if not x["do_plantao"] and not x["sem_responsavel"] and x["owner_email"]),
    }

    salvo = _cache_salvo_em(raw)
    idade_min = _idade_minutos(salvo) if salvo else None

    # Calendário: semana (segunda) da data consultada + 4 semanas.
    try:
        base = date.fromisoformat(hoje)
    except ValueError:
        base = date.today()
    segunda = base - timedelta(days=base.weekday())
    dias = _plantao_dias(por_data, raw, segunda, 28, hoje)
    proximos = _plantao_dias(por_data, raw, base, 10, hoje, so_uteis=True)

    return {
        "hoje": hoje,
        "real_hoje": date.today().isoformat(),
        "hoje_email": hoje_email,
        "hoje_nome": _plantao_nome(raw, hoje_email),
        "por_data": por_data,
        "nota": plantao.get("nota") or "",
        "novos": novos,
        "resumo": resumo,
        "total_novos": len(novos),
        "total_ativos_indice": len(ativos),
        "cache_salvo_em": salvo,
        "cache_idade_min": idade_min,
        "dias": dias,
        "proximos": proximos,
        "atualizado_em": plantao.get("atualizado_em") or "",
    }


def fila_mesclas() -> dict:
    raw = load_cache_raw()
    return raw.get("mesclas") if isinstance(raw.get("mesclas"), dict) else {}


def mesclas_confirmadas() -> dict:
    """Mesclagens que o Movidesk confirma (cache de GET /tickets/merged)."""
    raw = load_cache_raw()
    conf = raw.get("mesclas_confirmadas")
    return conf if isinstance(conf, dict) else {}


def _indice_mesclas_confirmadas() -> dict[int, dict]:
    """``{id_do_pai: {"ids": {filhos}, "quando": data}}`` a partir do cache.

    No Movidesk, ``ticket_id`` é o ticket que sobreviveu e ``merged_ids`` são os
    que foram mesclados nele.
    """
    itens = mesclas_confirmadas().get("itens")
    indice: dict[int, dict] = {}
    for item in itens or []:
        if not isinstance(item, dict):
            continue
        try:
            pai = int(item.get("ticket_id"))
        except (TypeError, ValueError):
            continue
        registro = indice.setdefault(pai, {"ids": set(), "quando": ""})
        for cid in item.get("merged_ids") or []:
            try:
                registro["ids"].add(int(cid))
            except (TypeError, ValueError):
                continue
        quando = str(item.get("last_update") or "")
        if quando > registro["quando"]:
            registro["quando"] = quando
    return indice


def _mescla_confirmada(indice: dict[int, dict], a: int, b: int) -> str:
    """Data da mescla já registrada no Movidesk entre a e b ('' quando não há)."""
    for pai, filho in ((a, b), (b, a)):
        registro = indice.get(pai)
        if registro and filho in registro["ids"]:
            return registro["quando"]
    return ""


def build_mesclas(escopo: str = "atual", agente: str | None = None) -> dict:
    """Candidatos a mescla cruzados com o que o Movidesk já confirma.

    A sugestão continua sendo por similaridade (``find_merge_candidates``); o que
    muda é que agora marcamos ``confirmado``/``confirmado_em`` quando a própria API
    (``GET /tickets/merged``) já registrou aquela mescla — em vez de depender só da
    fila local marcada à mão. Isso também fecha o ciclo do "sugere e nunca confirma".
    """
    now = datetime.now()
    if escopo == "todos":
        raw_tickets = _all_active_entries()
    else:
        raw_tickets = bucket_do_agente(agente or AGENT_EMAIL).get("ativos") or []
    infos = [build_ticket_info(t, now) for t in raw_tickets]
    candidates = find_merge_candidates(infos)
    fila = fila_mesclas()
    confirmadas = _indice_mesclas_confirmadas()
    itens = []
    for c in candidates:
        chave = f"{c['ticket_a']}:{c['ticket_b']}"
        try:
            quando = _mescla_confirmada(confirmadas, int(c["ticket_a"]), int(c["ticket_b"]))
        except (TypeError, ValueError):
            quando = ""
        itens.append({
            "ticket_a": c["ticket_a"],
            "ticket_b": c["ticket_b"],
            "cliente": c["client"],
            "motivo": c["reason"],
            "acao": c["action"],
            "pai": c["parent"],
            "assunto_a": c["subject_a"],
            "assunto_b": c["subject_b"],
            "similaridade": round(c["similarity"], 2),
            "situation_a": c.get("situation_a", ""),
            "situation_b": c.get("situation_b", ""),
            "service_a": c.get("service_a", ""),
            "service_b": c.get("service_b", ""),
            "chave": chave,
            "ja_vinculado": bool(c.get("ja_vinculado", False)),
            "status": (fila.get(chave) or {}).get("status", ""),
            "confirmado": bool(quando),
            "confirmado_em": quando,
        })
    conf = mesclas_confirmadas()
    return {
        "total": len(itens),
        "itens": itens,
        "escopo": escopo,
        "agente": agente or AGENT_EMAIL,
        "confirmadas": {
            "total": len(conf.get("itens") or []),
            "salvo_em": conf.get("salvo_em") or "",
            "janela": conf.get("janela") or {},
        },
    }



def salvar_fila_mescla(chave: str, status: str) -> dict:
    raw = load_cache_raw()
    fila = raw.get("mesclas") if isinstance(raw.get("mesclas"), dict) else {}
    if status:
        fila[chave] = {"status": status, "quando": datetime.now().isoformat(timespec="seconds")}
    else:
        fila.pop(chave, None)
    raw["mesclas"] = fila
    save_cache_raw(raw)
    return fila


def _owner_label(raw: dict, email: str) -> str:
    email = (email or "").lower()
    for a in raw.get("agentes") or []:
        if (a.get("email") or "").lower() == email:
            return a.get("nome") or email
    if email == AGENT_EMAIL.lower() and AGENT_NAME:
        return AGENT_NAME
    return email


def _agent_display(email: str | None) -> str:
    email = (email or "").lower()
    if email in ("", "*", "todos", "all"):
        return "Todos os atendentes"
    raw = load_cache_raw()
    return _owner_label(raw, email) or AGENT_NAME or email


def build_servicos() -> dict:
    """Catalogo de servicos com caminho (hierarquia), urgencia e categoria padrao."""
    raw = load_cache_raw()
    servicos = raw.get("services") if isinstance(raw.get("services"), list) else []
    by_id = {int(s["id"]): s for s in servicos if isinstance(s, dict) and s.get("id") is not None}
    _cache: dict[int, str] = {}

    def caminho(sid: int) -> str:
        if sid in _cache:
            return _cache[sid]
        partes, cur, visto = [], sid, set()
        while cur and cur not in visto:
            visto.add(cur)
            s = by_id.get(cur)
            if not s:
                break
            partes.insert(0, s.get("name") or "")
            cur = s.get("parentServiceId")
        _cache[sid] = " / ".join(p for p in partes if p)
        return _cache[sid]

    raiz: dict[int, dict] = {}
    for s in servicos:
        if not isinstance(s, dict) or s.get("id") is None:
            continue
        if s.get("isActive") is False:
            continue
        root_id = s.get("id")
        while by_id.get(root_id) and by_id[root_id].get("parentServiceId"):
            root_id = by_id[root_id].get("parentServiceId")
        r = raiz.setdefault(root_id, {"id": root_id, "nome": by_id.get(root_id, {}).get("name") or "",
                                      "urgencia": by_id.get(root_id, {}).get("defaultUrgency"),
                                      "categoria": by_id.get(root_id, {}).get("defaultCategory"),
                                      "filhos": []})
        if s.get("id") != root_id:
            r["filhos"].append({
                "id": s.get("id"),
                "nome": s.get("name"),
                "caminho": caminho(s.get("id")),
                "urgencia": s.get("defaultUrgency"),
                "categoria": s.get("defaultCategory"),
            })
    itens = sorted(raiz.values(), key=lambda x: (x.get("nome") or "").lower())
    for r in itens:
        r["filhos"].sort(key=lambda x: (x.get("nome") or "").lower())
    return {"total": len(itens), "itens": itens,
            "atualizado_em": raw.get("services_salvo_em") or ""}


_IMPLANTADOS: list[str] | None = None


def carregar_implantados() -> list[str]:
    """Lista de clientes ativos em implantação (vem de painel/implantados.json)."""
    global _IMPLANTADOS
    if _IMPLANTADOS is None:
        p = BASE_DIR / "painel" / "implantados.json"
        _IMPLANTADOS = []
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                data = {}
            _IMPLANTADOS = [_norm(x) for x in (data.get("itens") or [])]
    return _IMPLANTADOS


def cliente_implantado(cliente: str | None) -> bool:
    """True se o nome do cliente (aproximado) estiver na lista de implantação."""
    c = _norm(cliente)
    if not c:
        return False
    for nome in carregar_implantados():
        if len(nome) >= 4 and (nome in c or c in nome):
            return True
    return False


def _tags_de(t: dict) -> set[str]:
    return {str(x).lower() for x in (t.get("tags") or [])}


def _ultima_acao(t: dict) -> tuple[datetime | None, str]:
    """Ultima data de acao e quem foi: 'cliente', 'equipe' ou vazio."""
    last, autor = None, ""
    for a in t.get("actions") or []:
        d = parse_date(a.get("createdDate"))
        if d and (last is None or d > last):
            last = d
            em = ((a.get("createdBy") or {}).get("email") or "").lower()
            autor = "equipe" if em.endswith(INTERNO) else ("cliente" if em else autor)
    return last, autor


_RECORRENTES: dict = {"at": 0.0, "map": {}}
_RECORRENTES_LOCK = threading.Lock()


def _map_recorrentes() -> dict:
    """Mapa tid -> ocorrencias do mesmo cliente com assunto parecido (ativos+resolvidos)."""
    with _RECORRENTES_LOCK:
        if _RECORRENTES["at"] and datetime.now().timestamp() - _RECORRENTES["at"] < 300:
            return _RECORRENTES["map"]
        tickets: list[tuple[int, set[str], dict]] = []
        for t, _origem in iter_cache_tickets(ordem=("ativos", "resolvidos"), por_bucket=True):
            tokens = {w for w in normalize_subject(t.get("subject") or "").split() if len(w) > 2}
            c = _norm(client_name(t))
            if c and len(tokens) >= 2:
                tickets.append((int(t["id"]), tokens, t))
        por_cliente: dict[str, list] = {}
        for tid, toks, t in tickets:
            por_cliente.setdefault(_norm(client_name(t)), []).append((tid, toks, t))
        rec: dict[str, dict] = {}
        for grupo in por_cliente.values():
            if len(grupo) < 2:
                continue
            for i, (tid, toks, _) in enumerate(grupo):
                achou = 0
                ultima = None
                for j, (tid2, toks2, t2) in enumerate(grupo):
                    if i == j:
                        continue
                    if len(toks & toks2) >= 2:
                        achou += 1
                        d = parse_date(t2.get("resolvedIn") or t2.get("closedIn"))
                        if d and (ultima is None or d > ultima):
                            ultima = d
                if achou:
                    rec[str(tid)] = {
                        "total": achou + 1,
                        "ultima": ultima.strftime("%d/%m/%Y") if ultima else "",
                    }
        _RECORRENTES.update(at=datetime.now().timestamp(), map=rec)
        return rec


def recorrencia_de(tid: int) -> dict | None:
    a = None
    try:
        a = _map_recorrentes().get(str(tid))
    except Exception:
        pass
    return a or None


# O que fazer quando um atendente de suporte cai num cliente em implantacao
# (indicador implantacao e o chat de atendimento).
_FLUXO_IMPLANTACAO = {
    "suporte": "No chat: cliente em implantação → siga o fluxo e encaminhe ao implantador. "
               "O suporte não libera nem valida sozinho.",
    "implantacao": "Você é o implantador: conduza a implantação pelo roteiro de onboarding.",
}


_SEM_ACENTO = str.maketrans("áàâãäéèêëíìîïóòôõöúùûüç", "aaaaaeeeeiiiiooooouuuuc")


def _norm_just(s: str) -> str:
    """Minúsculo e sem acento, para casar justificativas do Movidesk."""
    return (s or "").strip().lower().translate(_SEM_ACENTO)


def guia_do_ticket(raw: dict, now: datetime | None = None,
                   autor: str | None = None, ultima: datetime | None = None) -> dict:
    """Orientação do playbook (responsável + próximo passo) para um ticket.

    Decide pelo status/justificativa, não pelo assunto (como manda o fluxo do
    Movidesk). Quando o chamador já tem a última ação calculada (``autor``/
    ``ultima``), ela é reaproveitada para não varrer as ações de novo.
    """
    from playbook_guia import guia_playbook

    now = now or datetime.now().astimezone()
    just = (raw.get("justification") or "").strip()
    justn = _norm_just(just)
    if autor is None or ultima is None:
        ultima, autor = _ultima_acao(raw)
    novo = not ultima and not raw.get("lastActionDate")
    espera = None
    if "retorno do cliente" in justn and "continuidade" in justn and ultima:
        try:
            from horario import minutos_uteis_entre

            espera = minutos_uteis_entre(ultima, now)
        except Exception:
            espera = None
    return guia_playbook(just, raw.get("status") or "", autor=autor or "",
                         espera_min=espera, novo=novo)


def ticket_por_id(tid) -> dict | None:
    """Acha o ticket cru pelo id em qualquer bucket/fonte do cache."""
    alvo = str(tid)
    raw = load_cache_raw()
    por_agente = raw.get("por_agente") if isinstance(raw.get("por_agente"), dict) else {}
    for bucket in por_agente.values():
        if not isinstance(bucket, dict):
            continue
        for fonte in FONTES_CACHE:
            for t in bucket.get(fonte) or []:
                if isinstance(t, dict) and str(t.get("id")) == alvo:
                    return t
    for t in raw.get("historico") or []:
        if isinstance(t, dict) and str(t.get("id")) == alvo:
            return t
    return None



def _sla_nivel(restante_min, total_min, fracao):
    """Classifica um item: VENCIDO / PROXIMO / OK a partir do tempo restante útil."""
    if restante_min is None:
        return ""
    if restante_min <= 0:  # <= e não <: -0.0 (prazo exato) tem de contar como vencido
        return "VENCIDO"
    if total_min and restante_min <= total_min * fracao:
        return "PROXIMO"
    return "OK"


def _fmt_restante(minutos, rotulo_negativo="vencido há ") -> str:
    if minutos is None:
        return ""
    negativo = minutos < 0
    m = abs(int(round(minutos)))
    txt = _fmt_min(m) or "0min"
    return (rotulo_negativo + txt) if negativo else txt


def build_sla_vencimentos(agente: str | None = None) -> dict:
    """Indicadores de SLA e vencimento contados em horário comercial.

    O Movidesk não devolve prazo/vencimento nesta conta, então o vencimento é
    derivado das regras do playbook:

    - **resposta** (1h útil): responder em até 1 hora após a última ação do
      cliente (ou em ticket novo). Base = última ação do cliente / criação.
    - **continuidade** ("Retorno do Cliente - Continuidade"): enviamos a ação e
      aguardamos o cliente. Passada 1 hora útil, o playbook manda ligar.
    - **encerramento** ("Retorno do Cliente - Encerramento"): resolvido aguardando
      consideração do cliente; resolve automaticamente em 5 dias úteis.
    - **concluir** ("Retorno do Cliente" genérico): sugerimos uma solução; se o
      cliente não der OK em 3 dias úteis, concluir.

    A contagem corre só dentro de ``config.HORARIO_COMERCIAL`` (seg-sex, sem
    feriados) e **pausa** enquanto o ticket aguarda o cliente (justificativas
    "Retorno do Cliente") — por isso esses casos nunca aparecem como SLA de
    resposta vencido.
    """
    from analyzer import client_name
    from horario import (JANELAS, add_dias_uteis, add_minutos_uteis,
                         descricao_janela, minutos_uteis_entre, minutos_por_dia)
    from config import (SLA_RESPOSTA_MINUTOS, SLA_ENCERRAMENTO_DIAS_UTEIS,
                        SLA_CONCLUIR_DIAS_UTEIS, SLA_PROXIMO_FRACAO)
    from playbook_guia import guia_playbook, orientacoes_referencia

    raw = load_cache_raw()
    por_agente = raw.get("por_agente") if isinstance(raw.get("por_agente"), dict) else {}
    ativos = []
    if isinstance(por_agente.get("*"), dict):
        ativos = por_agente["*"].get("ativos") or []
    if not ativos and por_agente:
        ativos = next((b.get("ativos") or [] for b in por_agente.values()
                       if isinstance(b, dict) and b.get("ativos")), [])

    ag = (agente or "").strip().lower()
    perfil = bool(ag) and ag != "*"
    papel = papel_agente(ag) if perfil else ""
    if perfil:
        bucket = por_agente.get(ag)
        carga = bucket.get("ativos") if isinstance(bucket, dict) else None
        carga_ids = {int(t["id"]) for t in (carga or [])
                     if isinstance(t, dict) and t.get("id") is not None}
        escopo = "carga"
        carga_sem_cache = not isinstance(carga, list)
    else:
        carga_ids = None
        escopo = "todos"
        carga_sem_cache = False

    now = datetime.now().astimezone()
    min_dia = minutos_por_dia()
    total_resposta = SLA_RESPOSTA_MINUTOS
    total_encerrar = SLA_ENCERRAMENTO_DIAS_UTEIS * min_dia
    total_concluir = SLA_CONCLUIR_DIAS_UTEIS * min_dia

    resposta, continuidade, encerramento, concluir = [], [], [], []
    ligar_depois = []
    areas = []

    for t in ativos:
        if not isinstance(t, dict) or t.get("id") is None:
            continue
        tid = int(t["id"])
        if carga_ids is not None and tid not in carga_ids:
            continue

        just = (t.get("justification") or "").strip()
        justn = _norm_just(just)
        cliente = client_name(t)
        assunto = clean(t.get("subject") or "")
        sv = " / ".join(x for x in [t.get("serviceFirstLevel"), t.get("serviceSecondLevel")] if x)
        info = acao_info(t)
        last, autor = _ultima_acao(t)
        base = last or parse_date(t.get("lastActionDate")) \
            or parse_date(t.get("lastUpdate")) or parse_date(t.get("createdDate"))
        novo = not last and not t.get("lastActionDate")
        g = guia_playbook(just, t.get("status") or "", autor=autor, novo=novo)

        def item(**extra):
            it = {
                "id": tid, "cliente": cliente, "assunto": assunto, "servico": sv,
                "status": just or (t.get("status") or ""),
                "por": autor,
                "ultima_acao": base.strftime("%d/%m/%Y %H:%M") if base else "",
                "tentativas": info.get("tentativas", 0),
                "ultima_ligacao": info.get("ultima_ligacao", ""),
                "ligacoes_resumo": info.get("ligacoes_resumo", {}),
                # Orientação do playbook (por status/justificativa, não pelo assunto):
                "responsavel": g.get("responsavel", "Suporte"),
                "do_suporte": bool(g.get("do_suporte", True)),
                "sugestao": g.get("sugestao", ""),
                "regra": g.get("regra", ""),
                "nao": g.get("nao", ""),
                "evidencia": bool(g.get("evidencia", False)),
            }
            it.update(extra)
            return it

        if "retorno do cliente" in justn and "encerramento" in justn:
            prazo = add_dias_uteis(base, SLA_ENCERRAMENTO_DIAS_UTEIS) if base else None
            restante = minutos_uteis_entre(now, prazo) if prazo else None
            # Passado o prazo de auto-resolução o sistema resolve sozinho (playbook):
            # NÃO é descumprimento de SLA, então não pode cair no card "Vencidos".
            pode_encerrar = bool(restante is not None and restante <= 0)
            nivel = "ATINGIDO" if pode_encerrar \
                else _sla_nivel(restante, total_encerrar, SLA_PROXIMO_FRACAO)
            encerramento.append(item(
                prazo=prazo.strftime("%d/%m/%Y %H:%M") if prazo else "",
                restante_min=round(restante, 1) if restante is not None else None,
                restante_txt=_fmt_restante(restante, "prazo atingido há "),
                nivel=nivel,
                pode_encerrar=pode_encerrar,
            ))

        elif "retorno do cliente" in justn and "continuidade" in justn:
            espera = minutos_uteis_entre(base, now) if base else None
            # A bola está com o cliente: só cobramos ligação se a última ação foi nossa.
            precisa_ligar = bool(autor == "equipe" and espera is not None
                                 and espera >= SLA_RESPOSTA_MINUTOS)
            nivel = _sla_nivel(SLA_RESPOSTA_MINUTOS - espera if espera is not None else None,
                               total_resposta, SLA_PROXIMO_FRACAO)
            # Reorienta a sugestão para "ligar agora" quando já passou 1h útil.
            g = guia_playbook(just, t.get("status") or "", autor=autor, espera_min=espera)
            it = item(
                espera_min=round(espera, 1) if espera is not None else None,
                espera_txt=_fmt_restante(espera),
                nivel=nivel,
                precisa_ligar=precisa_ligar,
            )
            continuidade.append(it)
            if precisa_ligar:
                ligar_depois.append(it)

        elif _norm_just(just) == "retorno do cliente":
            prazo = add_dias_uteis(base, SLA_CONCLUIR_DIAS_UTEIS) if base else None
            restante = minutos_uteis_entre(now, prazo) if prazo else None
            concluir.append(item(
                prazo=prazo.strftime("%d/%m/%Y %H:%M") if prazo else "",
                restante_min=round(restante, 1) if restante is not None else None,
                restante_txt=_fmt_restante(restante),
                nivel=_sla_nivel(restante, total_concluir, SLA_PROXIMO_FRACAO),
            ))

        elif g["responsavel"] not in ("Suporte", "Cliente"):
            # Aguardando outra área (análise/dev/versão/fornecedor/implantação...):
            # não é (só) do Suporte — a orientação diz o que acompanhar.
            areas.append(item())

        else:
            if autor == "cliente" or novo or (t.get("status") or "") == "Novo":
                prazo = add_minutos_uteis(base, SLA_RESPOSTA_MINUTOS) if base else None
                restante = minutos_uteis_entre(now, prazo) if prazo else None
                resposta.append(item(
                    prazo=prazo.strftime("%d/%m/%Y %H:%M") if prazo else "",
                    restante_min=round(restante, 1) if restante is not None else None,
                    restante_txt=_fmt_restante(restante),
                    nivel=_sla_nivel(restante, total_resposta, SLA_PROXIMO_FRACAO),
                ))

    def _ordena(itens):
        return sorted(itens, key=lambda x: (x.get("restante_min") if x.get("restante_min") is not None
                                            else x.get("espera_min") or 0))

    resposta = _ordena(resposta)
    encerramento = _ordena(encerramento)
    concluir = _ordena(concluir)
    continuidade.sort(key=lambda x: (x.get("espera_min") or 0), reverse=True)
    ligar_depois.sort(key=lambda x: (x.get("espera_min") or 0), reverse=True)
    areas.sort(key=lambda x: ((x.get("responsavel") or ""), (x.get("ultima_acao") or "")))

    def _conta(itens, chave="nivel"):
        return sum(1 for x in itens if x.get(chave) == "VENCIDO"), \
               sum(1 for x in itens if x.get(chave) == "PROXIMO")

    rv, rp = _conta(resposta)
    ev, ep = _conta(encerramento)
    cv, cp = _conta(concluir)
    # Encerramento não tem mais "VENCIDO": prazo passado vira ATINGIDO
    # (auto-resolução do sistema), contado à parte do card de SLA.
    ea = sum(1 for x in encerramento if x.get("nivel") == "ATINGIDO")

    sla_oficial = any((t.get("slaSolutionDate") or t.get("slaResponseDate")) for t in ativos if isinstance(t, dict))
    return {
        "gerado_em": now.isoformat(timespec="seconds"),
        "escopo": escopo,
        "papel": papel,
        "carga_sem_cache": carga_sem_cache,
        "sla_oficial_disponivel": sla_oficial,
        "janela": descricao_janela(),
        "regras": {
            "resposta_min": SLA_RESPOSTA_MINUTOS,
            "encerrar_dias_uteis": SLA_ENCERRAMENTO_DIAS_UTEIS,
            "concluir_dias_uteis": SLA_CONCLUIR_DIAS_UTEIS,
            "proximo_fracao": SLA_PROXIMO_FRACAO,
        },
        "resposta": {"total": len(resposta), "vencidos": rv, "proximos": rp, "itens": resposta[:80]},
        "continuidade": {
            "total": len(continuidade),
            "ligar": len(ligar_depois),
            "itens": continuidade[:80],
        },
        "encerramento": {
            "total": len(encerramento), "vencidos": ev, "atingidos": ea, "proximos": ep,
            "itens": encerramento[:80],
            "texto_padrao": (
                "Conforme playbook (Aguardando » Retorno do cliente - Para encerramento): "
                "o ticket foi resolvido e aguarda eventual consideração do cliente. "
                "Sem retorno em 5 dias úteis, é Resolvido automaticamente pelo sistema — "
                "encaminhando para encerramento."
            ),
        },
        "concluir": {"total": len(concluir), "vencidos": cv, "proximos": cp, "itens": concluir[:80]},
        "areas": {"total": len(areas), "itens": areas[:80]},
        "responsabilidades": orientacoes_referencia(),
        "escopo_suporte": (
            "O painel sugere o próximo passo conforme o playbook, pelo STATUS/JUSTIFICATIVA "
            "(não pelo assunto do ticket). Itens que aguardam outras áreas (análise, dev, "
            "versão, fornecedor, implantação...) vêm marcados com o responsável e o que "
            "cabe ao Suporte acompanhar — evidências evitam contestação depois."
        ),
        "avisos": [
            "O Movidesk não devolve prazo/vencimento nesta conta (slaSolutionDate/slaResponseDate "
            "vazios): o vencimento é derivado das regras do playbook.",
            f"Janela de horário comercial: {descricao_janela()} (seg-sex, sem feriados). "
            "Tickets aguardando cliente pausam o relógio de resposta.",
        ],
    }


def _eh_fila_do_dia(t: dict, now: datetime) -> bool:
    """Regra única de "fila do dia" (regra A/canônica).

    Ticket conta na fila do dia quando a última ação/estado é de HOJE e o
    status é "Novo" OU a última ação foi do cliente; exclui tickets que
    aguardam dev/fornecedor/encerramento/sugestão (não são da fila do Suporte).
    Reutilizada por ``build_indicadores`` e ``build_ranking``.
    """
    just_low = (t.get("justification") or "").lower()
    status = (t.get("status") or "").strip()
    aguarda_interno = (
        just_low.startswith("dev")
        or status.lower().startswith("dev")
        or "retorno de fornecedor" in just_low
        or "retorno do cliente" in just_low
        or "sugest" in just_low
    )
    if aguarda_interno:
        return False
    last, autor = _ultima_acao(t)
    base = last or parse_date(t.get("lastActionDate")) \
        or parse_date(t.get("lastUpdate")) or parse_date(t.get("createdDate"))
    if not base or base.date() != now.date():
        return False
    return status == "Novo" or autor == "cliente"


def build_indicadores(agente: str | None = None) -> dict:
    """Indicadores operacionais dos playbooks, calculados sobre o cache.

    - sla: tickets que exigem resposta nossa ha +1h (SLA de 1 hora).
    - fila: tickets novos/atualizados pelo cliente hoje (reforco acima de 15).
    - fornecedor: aguardando retorno de fornecedor/banco (tag fornecedor_*).
    - retorno_encerrar: Aguardando cliente - encerramento (auto-resolve em 5 dias uteis).
    - implantacao: clientes em implantacao (tags ou lista painel/implantados.json).
    - reclamacoes: tickets com a tag reclamacao_cliente.

    Escopo por perfil (``agente`` = email do atendente selecionado, ou ``""``/
    ``None``/``"*"`` para o cache inteiro):

    - **fornecedor, implantacao e reclamacoes** valem so para a **carga do
      perfil selecionado** ("na minha carga"). Com perfil concreto, um ticket
      que nao esta na carga dele nem aparece; sem perfil, valem para o bucket
      ``*`` (time inteiro).
    - **sla, fila e retorno_encerrar** continuam globais: sao alertas de fila do
      time, nao de uma pessoa.
    """
    from analyzer import client_name
    from collections import Counter
    from config import SLA_ENCERRAMENTO_DIAS_UTEIS
    from horario import add_dias_uteis, minutos_por_dia, minutos_uteis_entre

    raw = load_cache_raw()
    por_agente = raw.get("por_agente") if isinstance(raw.get("por_agente"), dict) else {}
    ativos = []
    if isinstance(por_agente.get("*"), dict):
        ativos = por_agente["*"].get("ativos") or []
    if not ativos and por_agente:
        ativos = next((b.get("ativos") or [] for b in por_agente.values()
                       if isinstance(b, dict) and b.get("ativos")), [])

    # Escopo do perfil: carga do atendente (so ativos dele) vs. time inteiro.
    ag = (agente or "").strip().lower()
    perfil = bool(ag) and ag != "*"
    papel = papel_agente(ag) if perfil else ""
    if perfil:
        bucket = por_agente.get(ag)
        carga = bucket.get("ativos") if isinstance(bucket, dict) else None
        carga_ids = {int(t["id"]) for t in (carga or [])
                     if isinstance(t, dict) and t.get("id") is not None}
        escopo = "carga"
        # Distingue "carga vazia" (legitimo) de "nunca consultei esse perfil"
        # (silencioso seria mostrar 0 e parecer que nao ha nada).
        carga_sem_cache = not isinstance(carga, list)
    else:
        carga_ids = None
        escopo = "todos"
        carga_sem_cache = False

    now = datetime.now()
    # O prazo de encerramento é contado em horário comercial, o que exige
    # datetime com fuso (datetime.now() volta sem fuso — naive).
    now_tz = now.astimezone()
    min_dia = minutos_por_dia()

    sla_itens, f_item, ret_itens, impl_itens = [], [], [], []
    rec_por: Counter = Counter()
    f_tags = Counter()
    fila_hoje: set[int] = set()

    rec_itens = []
    for t in ativos:
        if not isinstance(t, dict) or t.get("id") is None:
            continue
        tid = int(t["id"])
        # False quando ha perfil selecionado e o ticket nao esta na carga dele.
        na_carga = carga_ids is None or tid in carga_ids
        just = (t.get("justification") or "").strip()
        just_low = just.lower()
        tags = _tags_de(t)
        status = t.get("status") or ""
        last, autor = _ultima_acao(t)
        base = last or parse_date(t.get("lastActionDate")) \
            or parse_date(t.get("lastUpdate")) or parse_date(t.get("createdDate"))
        horas = hours_between(base, now) if base else None
        aguarda_interno = (
            just_low.startswith("dev")
            or status.lower().startswith("dev")
            or "retorno de fornecedor" in just_low
            or "retorno do cliente" in just_low
            or "sugest" in just_low
        )
        cliente = client_name(t)
        assunto = clean(t.get("subject") or "")
        sv = " / ".join(x for x in [t.get("serviceFirstLevel"), t.get("serviceSecondLevel")] if x)

        def dicio(**extra):
            item = {
                "id": tid, "cliente": cliente, "assunto": assunto, "servico": sv,
                "status": just or status,
                "owner": owner_name(t),
                "ultima_acao": base.strftime("%d/%m/%Y %H:%M") if base else "",
                "por": autor,
                "horas": round(horas, 1) if horas is not None else None,
            }
            item.update(extra)
            return item

        if (not aguarda_interno) and (status == "Novo" or autor == "cliente") \
                and horas is not None and horas > 1:
            sla_itens.append(dicio())

        if _eh_fila_do_dia(t, now):
            fila_hoje.add(tid)

        f = [x for x in tags if x.startswith("fornecedor")]
        if na_carga and (not just_low.startswith("dev") and not status.lower().startswith("dev")) \
                and ("retorno de fornecedor" in just_low or f):
            if f:
                for x in f:
                    f_tags[x] += 1
            else:
                f_tags["(retorno de fornecedor)"] += 1
            f_item.append(dicio(dias=round(horas / 24, 1) if horas is not None else None))

        if "retorno do cliente - encerramento" in just_low:
            # Mesma régua da aba SLA: 5 dias ÚTEIS (add_dias_uteis + horário
            # comercial), não 5 corridos — senão o mesmo ticket aparece
            # "vencido" aqui e "no prazo" na aba SLA.
            prazo = add_dias_uteis(base, SLA_ENCERRAMENTO_DIAS_UTEIS) if base else None
            minut = minutos_uteis_entre(now_tz, prazo) if prazo else None
            restantes = (minut / min_dia) if minut is not None else None
            ret_itens.append(dicio(
                prazo=prazo.strftime("%d/%m/%Y") if prazo else "",
                restantes=round(restantes, 1) if restantes is not None else None,
            ))

        if na_carga and (any(x in tags for x in
                             ("implantação", "implantacao", "implantacao_em_andamento"))
                         or cliente_implantado(cliente)):
            impl_itens.append(dicio())

        if na_carga and "reclamacao_cliente" in tags:
            ow = ((t.get("owner") or {}).get("email") or "").lower() or "*"
            rec_por[ow] += 1
            rec_itens.append(dicio())

    sla_itens.sort(key=lambda x: x["horas"] or 0, reverse=True)
    f_item.sort(key=lambda x: (x.get("dias") or 0), reverse=True)
    ret_itens.sort(key=lambda x: (x.get("restantes") if x.get("restantes") is not None else 99))
    impl_itens.sort(key=lambda x: (x["cliente"] or "").lower())
    rec_itens.sort(key=lambda x: (x["id"]))
    fila = len(fila_hoje)

    return {
        "gerado_em": now.isoformat(timespec="seconds"),
        "escopo": escopo,
        "papel": papel,
        "carga_sem_cache": carga_sem_cache,
        "implantados_config": len(carregar_implantados()),
        "sla": {"exigidos": len(sla_itens), "itens": sla_itens[:60]},
        "fila": {"hoje": fila, "limite": 15, "reforco": fila > 15},
        "fornecedor": {
            "total": len(f_item),
            "por_fornecedor": dict(f_tags.most_common()),
            "itens": f_item[:60],
            "escopo": escopo,
            "sem_cache": carga_sem_cache,
        },
        "retorno_encerrar": {
            "total": len(ret_itens),
            # "perto do prazo" = ainda dentro do prazo e chegando no fim
            # (0 <= restantes <= 3). Os já passados ficam de fora: são outro
            # estado — prazo atingido / auto-resolução.
            "perto_prazo": sum(1 for x in ret_itens
                               if x.get("restantes") is not None
                               and 0 <= x["restantes"] <= 3),
            "itens": ret_itens[:60],
        },
        "implantacao": {
            "total": len(impl_itens),
            "itens": impl_itens[:60],
            "escopo": escopo,
            "papel": papel,
            "sem_cache": carga_sem_cache,
            "fluxo": _FLUXO_IMPLANTACAO.get(papel, ""),
        },
        "reclamacoes": {
            "total": sum(rec_por.values()),
            "por_agente": dict(rec_por),
            "itens": rec_itens[:60],
            "escopo": escopo,
        },
    }


def _esperas_cliente(raw: dict) -> list[float]:
    """Horas de espera do cliente: de cada ação do cliente até a próxima ação da equipe."""
    acts = raw.get("actions")
    if not isinstance(acts, list) or not acts:
        return []
    parsed = []
    for a in acts:
        d = parse_date(a.get("createdDate"))
        if d:
            cb = a.get("createdBy") or {}
            email = (cb.get("email") or "").lower()
            parsed.append((d, email))
    parsed.sort(key=lambda x: x[0])
    saida = []
    for i, (d, email) in enumerate(parsed):
        if eh_equipe(email):
            continue
        for j in range(i + 1, len(parsed)):
            if eh_equipe(parsed[j][1]):
                saida.append((parsed[j][0] - d).total_seconds() / 3600.0)
                break
    return saida


def build_ranking(incluir_dev: bool = False, periodo_dias: int = 90,
                  papel_filtro: str | None = None) -> dict:
    """Comparativo por atendente: carga, risco, tempo medio parado, FCR,
    reaberturas, CSAT/NPS e tendencia.

    Por padrao esconde o time de dev (Leandro e Matheus); passe incluir_dev=True
    para comparar tambem com quem nao esta mais no suporte.

    ``periodo_dias`` limita resolvidos/FCR/reaberturas/CSAT aos ultimos N dias
    (0 = todo o cache). O bucket agregado ``*`` (cache do time inteiro) e
    ignorado — ele nao e um atendente.

    Coluna "Foco" (fila do dia, reclamacoes, implantação, fornecedor) usa a mesma
    regra de fila do dia de build_indicadores: último movimento/estado hoje E status
    novo OU última ação do cliente (ver comentário final deste arquivo)."""
    from analyzer import build_ticket_info, nivel_risco

    raw = load_cache_raw()
    por_agente = raw.get("por_agente") if isinstance(raw.get("por_agente"), dict) else {}
    survey = raw.get("survey") if isinstance(raw.get("survey"), list) else []
    now = datetime.now()
    if now.tzinfo is None:
        now = now.astimezone()
    corte = (now - timedelta(days=periodo_dias)) if periodo_dias and periodo_dias > 0 else None

    t2owner: dict[int, str] = {}
    for t, _origem in iter_cache_tickets(ordem=("ativos", "resolvidos"), por_bucket=True):
        ow = ((t.get("owner") or {}).get("email") or "").lower()
        if ow:
            t2owner.setdefault(int(t["id"]), ow)

    csat: dict[str, dict] = {}
    for r in survey:
        try:
            tid = int(r.get("ticketId"))
        except (TypeError, ValueError):
            continue
        ow = t2owner.get(tid)
        if not ow:
            continue
        if corte:
            d = parse_date(r.get("responseDate"))
            if not d or d < corte:
                continue
        _, sat, insat = survey_assessment(r)
        c = csat.setdefault(ow, {"sat": 0, "insat": 0, "nps": []})
        if sat:
            c["sat"] += 1
        if insat:
            c["insat"] += 1
        if r.get("type") == 3 and isinstance(r.get("value"), int):
            c["nps"].append(r["value"])

    # Tendencia: resolvidos por semana (4 semanas, da mais antiga p/ a atual).
    semanas: dict[str, list[int]] = {}
    ini_semana = now - timedelta(days=28)

    itens = []
    salvo_max = None
    for email, bucket in por_agente.items():
        if not isinstance(bucket, dict):
            continue
        email_low = (email or "").lower()
        if email_low in ("", "*"):
            continue
        papel = papel_agente(email)
        if papel_filtro:
            if papel != papel_filtro:
                continue
        elif papel == "dev" and not incluir_dev:
            continue
        ativos = bucket.get("ativos") or []
        resolvidos = bucket.get("resolvidos") or []
        if not ativos and not resolvidos:
            continue

        resolv_periodo = []
        for t in resolvidos:
            d = parse_date(t.get("resolvedIn") or t.get("closedIn"))
            if corte and (not d or d < corte):
                continue
            resolv_periodo.append(t)

        infos = [build_ticket_info(t, now) for t in ativos]
        ids_tempo = [t.hours_idle for t in infos if t.hours_idle is not None]
        primeiras = []
        esperas = []
        for t in ativos:
            ai = acao_info(t)
            if ai.get("primeira_acao_h") is not None:
                primeiras.append(ai["primeira_acao_h"])
            esperas.extend(_esperas_cliente(t))
        criticos = sum(1 for t in infos if t.level == "CRITICO")
        risco_alto = sum(1 for t in infos if nivel_risco(t) == "ALTO")
        risco_medio = sum(1 for t in infos if nivel_risco(t) == "MEDIO")
        antigos = sum(1 for t in infos if (t.hours_idle or 0) > 168)
        fcr = sum(1 for t in resolv_periodo if t.get("resolvedInFirstCall"))
        reab = sum(1 for t in resolv_periodo if t.get("reopenedIn"))
        hoje_dt = now.date()
        k_fila = k_rec = k_imp = k_f = 0
        for ti in infos:
            tags = _tags_de(ti.raw)
            if _eh_fila_do_dia(ti.raw, now):
                k_fila += 1
            if "reclamacao_cliente" in tags:
                k_rec += 1
            if any(x in tags for x in ("implantação", "implantacao", "implantacao_em_andamento")):
                k_imp += 1
            if any(x.startswith("fornecedor") for x in tags) or \
                    "retorno de fornecedor" in str(ti.raw.get("justification") or "").lower():
                k_f += 1
        c = csat.get(email, {})
        sat, insat = c.get("sat", 0), c.get("insat", 0)
        resp = sat + insat
        npsv = c.get("nps", [])
        nps = None
        if npsv:
            nps = round(100 * (sum(1 for v in npsv if v >= 9) - sum(1 for v in npsv if v <= 6)) / len(npsv))
        nres = len(resolv_periodo)

        # Tendencia semanal de resolvidos dentro do periodo.
        trend = [0, 0, 0, 0]
        for t in resolv_periodo:
            d = parse_date(t.get("resolvedIn") or t.get("closedIn"))
            if not d or d < ini_semana:
                continue
            idx = min(3, (d - ini_semana).days // 7)
            trend[idx] += 1

        salvo = parse_date(bucket.get("salvo_em")) if bucket.get("salvo_em") else None
        if salvo:
            if salvo_max is None or salvo > salvo_max:
                salvo_max = salvo
        stale = round((now - salvo).total_seconds() / 86400, 1) if salvo else None

        itens.append({
            "email": email,
            "nome": _owner_label(raw, email),
            "papel": papel,
            "ativos": len(ativos),
            "criticos": criticos,
            "risco_alto": risco_alto,
            "risco_medio": risco_medio,
            "antigos_7d": antigos,
            "fila_hoje": k_fila,
            "reclamacoes": k_rec,
            "implantacao": k_imp,
            "fornecedor": k_f,
            "tempo_medio_parado": round(sum(ids_tempo) / len(ids_tempo), 1) if ids_tempo else None,
            "primeira_resposta_media_h": round(sum(primeiras) / len(primeiras), 1) if primeiras else None,
            "tempo_espera_cliente_medio_h": round(sum(esperas) / len(esperas), 1) if esperas else None,
            "resolvidos": nres,
            "fcr": fcr,
            "fcr_pct": round(100 * fcr / nres) if nres else None,
            "reabertos": reab,
            "reopen_rate_pct": round(100 * reab / nres) if nres else None,
            "sat": sat,
            "insat": insat,
            "csat_pct": round(100 * sat / resp) if resp else None,
            "respondidos": resp,
            "response_rate_pct": round(100 * resp / nres) if nres else None,
            "nps": nps,
            "trend": trend,
            "salvo_em": bucket.get("salvo_em"),
            "stale_dias": stale,
        })
    itens.sort(key=lambda x: (-x["ativos"], -(x["tempo_medio_parado"] or 0)))
    return {
        "total": len(itens),
        "itens": itens,
        "incluir_dev": bool(incluir_dev),
        "periodo_dias": periodo_dias,
        "dados_ate": salvo_max.isoformat(timespec="minutes") if salvo_max else None,
    }


# --------------------------------------------------------------------
# Fila do dia / "novos hoje": como a regra é calculada hoje
# --------------------------------------------------------------------
#
# Existem 3 cálculos diferentes de "quantos tickets novos hoje" no projeto:
#
# 1) Indicadores -> fila do dia (`build_indicadores`)           [regra A]
#    Critério: última ação/estado **HOJE** EOU status == "Novo" OU
#              última ação foi do cliente; exclui dev/fornecedor/encerramento.
#    Use: aba SLA 1h / Fila do dia, banner de reforço (>15).
#
# 2) Ranking coluna "Foco/Fila" (`build_ranking`)               [regra B = igual a A]
#    Critério idêntico ao de indicadores, mas por atendente.
#
# 3) Plantão "Tickets novos hoje" (`build_plantao`)             [regra C]
#    Critério: ticket criado HOJE (createdDate), independente de
#              status/última ação; serve para a escala do plantão.
#
# Esses números podem divergir: um ticket criado hoje mas que o sistema
# atualizou (ex.: auto-weighth, SLA, atribuição) entra na C mas não na A/B;
# um ticket antigo mas reativado pelo cliente hoje entra em A/B mas não C.
#
# Se quiser uma regra única, decida qual é a canônica (recomendo a do
# indicador/playbook = A) e ajustar plantão e/ou ranking para ela.
# --------------------------------------------------------------------


def _comparativo_stats(entries: list[dict], ini: datetime, fim: datetime) -> dict:
    """KPIs de período sobre os tickets: criados, resolvidos, FCR, reabertos e
    tempo médio de resolução (horas) dentro da janela [ini, fim)."""
    criados = resolvidos = fcr = reabertos = 0
    tempos: list[float] = []
    for t in entries:
        created = parse_date(t.get("createdDate"))
        solved = parse_date(t.get("resolvedIn") or t.get("closedIn"))
        reopened = parse_date(t.get("reopenedIn"))
        if created and ini <= created < fim:
            criados += 1
        if solved and ini <= solved < fim:
            resolvidos += 1
            if t.get("resolvedInFirstCall"):
                fcr += 1
            if created and solved >= created:
                tempos.append((solved - created).total_seconds() / 3600.0)
        if reopened and ini <= reopened < fim:
            reabertos += 1
    return {
        "criados": criados,
        "resolvidos": resolvidos,
        "fcr": fcr,
        "fcr_pct": round(100 * fcr / resolvidos) if resolvidos else None,
        "reabertos": reabertos,
        "tempo_medio_h": round(sum(tempos) / len(tempos), 1) if tempos else None,
    }


def _comparativo_csat(survey: list[dict], ini: datetime, fim: datetime,
                      ids_permitidos: set[int] | None = None) -> dict:
    sat = insat = 0
    for r in survey:
        if not isinstance(r, dict):
            continue
        if ids_permitidos is not None:
            try:
                tid = int(r.get("ticketId"))
            except (TypeError, ValueError):
                continue
            if tid not in ids_permitidos:
                continue
        d = parse_date(r.get("responseDate"))
        if not d or not (ini <= d < fim):
            continue
        _, s, i = survey_assessment(r)
        if s:
            sat += 1
        if i:
            insat += 1
    resp = sat + insat
    return {
        "satisfeitos": sat,
        "insatisfeitos": insat,
        "respondidos": resp,
        "pct": round(100 * sat / resp) if resp else None,
    }


def _comparativo_delta(atual, anterior) -> tuple:
    if atual is None or anterior is None:
        return None, None
    dif = atual - anterior
    pct = round(100 * dif / anterior) if anterior else None
    if isinstance(dif, float):
        dif = round(dif, 1)
    return dif, pct


def _comparativo_metrica(nome: str, atual, anterior, tipo: str, melhor: str) -> dict:
    delta, pct = _comparativo_delta(atual, anterior)
    return {
        "nome": nome, "tipo": tipo, "melhor": melhor,
        "atual": atual, "anterior": anterior, "delta": delta, "pct": pct,
    }


def _comparativo_bloco(rotulo: str, dias: int, entries: list[dict], survey: list[dict],
                       ids: set[int] | None, now: datetime) -> dict:
    atual_ini = now - timedelta(days=dias)
    ant_ini = now - timedelta(days=2 * dias)
    a = _comparativo_stats(entries, atual_ini, now)
    b = _comparativo_stats(entries, ant_ini, atual_ini)
    a.update(_comparativo_csat(survey, atual_ini, now, ids))
    b.update(_comparativo_csat(survey, ant_ini, atual_ini, ids))
    metricas = [
        _comparativo_metrica("Resolvidos", a["resolvidos"], b["resolvidos"], "count", "up"),
        _comparativo_metrica("Criados", a["criados"], b["criados"], "count", "neutro"),
        _comparativo_metrica("FCR (1º contato)", a["fcr_pct"], b["fcr_pct"], "pct", "up"),
        _comparativo_metrica("Reabertos", a["reabertos"], b["reabertos"], "count", "down"),
        _comparativo_metrica("Tempo médio de resolução", a["tempo_medio_h"], b["tempo_medio_h"], "horas", "down"),
        _comparativo_metrica("Satisfação", a["pct"], b["pct"], "pct", "up"),
        _comparativo_metrica("Insatisfeitos", a["insatisfeitos"], b["insatisfeitos"], "count", "down"),
    ]
    return {
        "rotulo": rotulo,
        "dias": dias,
        "periodo": {"de": atual_ini.strftime("%Y-%m-%d"), "ate": now.strftime("%Y-%m-%d")},
        "periodo_anterior": {"de": ant_ini.strftime("%Y-%m-%d"), "ate": atual_ini.strftime("%Y-%m-%d")},
        "metricas": metricas,
    }


def build_comparativo(now: datetime | None = None) -> dict:
    """Comparativo dos KPIs: últimos 7 e 30 dias contra os períodos anteriores
    equivalentes. Usa o cache (histórico + ativos/resolvidos) já filtrado pelo
    atendente ativo (escopo do perfil)."""
    now = now or datetime.now()
    if now.tzinfo is None:
        now = now.astimezone()
    ag, anome = _agente_escopo()
    entries = _filtrar_por_agente(_entries_do_cache(incluir_ativos=True))
    raw = load_cache_raw()
    survey = raw.get("survey") if isinstance(raw.get("survey"), list) else []
    ids: set[int] | None = None
    if ag:
        ids = set()
        for t in entries:
            try:
                ids.add(int(t.get("id")))
            except (TypeError, ValueError):
                continue
    return {
        "gerado_em": now.isoformat(timespec="seconds"),
        "agente": agente_atual(),
        "escopo": "perfil" if ag else "todos",
        "total_indice": len(entries),
        "semana": _comparativo_bloco("Últimos 7 dias", 7, entries, survey, ids, now),
        "mes": _comparativo_bloco("Últimos 30 dias", 30, entries, survey, ids, now),
    }


def montar_cronologia(actions: list[dict]) -> dict:
    """Linha do tempo de um ticket a partir das ações cruas da API.

    Cada ação traz autor, data, tipo (nota interna x resposta pública), canal
    (origin) e texto. Com isso dá para reconstruir a linha do tempo, saber quem
    respondeu por último e medir o tempo entre interações (por isso guardamos
    ``horas_desde_anterior`` em cada item e os resumos no fim).
    """
    itens = []
    for a in actions or []:
        d = parse_date(a.get("createdDate"))
        cb = a.get("createdBy") or {}
        email = (cb.get("email") or "").lower()
        tipo = a.get("type")
        itens.append({
            "data": d.strftime("%d/%m/%Y %H:%M") if d else "",
            "data_iso": d.isoformat() if d else "",
            "_ts": d,
            "autor": cb.get("businessName") or email or "",
            "email": email,
            "equipe": eh_equipe(email),
            "interno": tipo == 1,
            "publico": tipo == 2,
            "tipo": tipo,
            "canal": canal_nome(a.get("origin")),
            "descricao": html.unescape((a.get("description") or "").strip()),
        })
    itens.sort(key=lambda x: (x["_ts"] is None, x["_ts"] or datetime.min))

    gaps = []
    for i, it in enumerate(itens):
        ant = itens[i - 1]["_ts"] if i else None
        if ant and it["_ts"]:
            h = round((it["_ts"] - ant).total_seconds() / 3600.0, 2)
            it["horas_desde_anterior"] = h
            gaps.append(h)
        else:
            it["horas_desde_anterior"] = None

    ultima = itens[-1] if itens else None
    primeira_equipe = next((it for it in itens if it["equipe"] and it["publico"]), None)
    agora = datetime.now(ultima["_ts"].tzinfo) if ultima and ultima["_ts"] else datetime.now()
    resumo = {
        "total": len(itens),
        "internas": sum(1 for it in itens if it["interno"]),
        "publicas": sum(1 for it in itens if it["publico"]),
        "ultima_em": ultima["data"] if ultima else "",
        "ultima_por": ultima["autor"] if ultima else "",
        "ultima_interna": bool(ultima and ultima["interno"]),
        "ultima_equipe": bool(ultima and ultima["equipe"]),
        "horas_desde_ultima": round((agora - ultima["_ts"]).total_seconds() / 3600.0, 2)
        if ultima and ultima["_ts"] else None,
        "primeira_resposta_em": primeira_equipe["data"] if primeira_equipe else "",
        "intervalo_medio_h": round(sum(gaps) / len(gaps), 2) if gaps else None,
        "maior_intervalo_h": round(max(gaps), 2) if gaps else None,
    }
    for it in itens:
        it.pop("_ts", None)
    return {"itens": itens, "resumo": resumo}


def montar_solucao(actions: list[dict]) -> dict:
    """Extrai a 'resposta pronta': ultima resposta publica da equipe, e a ultima nota interna."""
    parsed = []
    for a in actions or []:
        d = parse_date(a.get("createdDate"))
        if d:
            parsed.append((d, a))
    parsed.sort(key=lambda x: x[0])
    resposta = obs = None
    for d, a in parsed:
        cb = a.get("createdBy") or {}
        email = (cb.get("email") or "").lower()
        desc = " ".join((a.get("description") or "").split())
        if not desc:
            continue
        item = {
            "data": d.strftime("%d/%m/%Y %H:%M"),
            "autor": cb.get("businessName") or email,
            "texto": html.unescape(desc),
            "publica": a.get("type") == 2,
        }
        if a.get("type") == 2 and eh_equipe(email):
            resposta = item
        if a.get("type") == 1 and eh_equipe(email):
            obs = item
    return {
        "resposta": resposta,
        "observacao": obs,
        "total_acoes": len(parsed),
        "ultima": parsed[-1][0].strftime("%d/%m/%Y %H:%M") if parsed else "",
    }


def _chave_agente_padrao() -> str:
    return (AGENT_EMAIL or "").lower() or CHAVE_TODOS


def _arquivo_legado() -> Path:
    """Backup do cache no formato antigo, gravado antes da migração."""
    return CACHE_FILE.with_name(f"{CACHE_FILE.stem}.legado{CACHE_FILE.suffix}")


def _mtime_iso() -> str:
    try:
        return datetime.fromtimestamp(CACHE_FILE.stat().st_mtime).isoformat(timespec="seconds")
    except OSError:
        return datetime.now().isoformat(timespec="seconds")


def _bucket_legado(ativos, origem: str, salvo_em: str | None = None) -> dict:
    return {
        "salvo_em": salvo_em or datetime.now().isoformat(timespec="seconds"),
        "ativos": [t for t in (ativos or []) if isinstance(t, dict)],
        "resolvidos": [],
        "origem": origem,
    }


def migrar_cache(raw, salvo_em: str | None = None) -> dict:
    """Normaliza o cache para o schema atual SEM perder dados.

    Formatos antigos aceitos:
    - **lista pura**: o CLI antigo (`main.py`) gravava só a lista de tickets ativos
      no mesmo arquivo do painel — era isso que apagava o cache do painel;
    - **dict com `ativos`/`resolvidos` na raiz**: painel anterior ao `por_agente`.
    """
    if isinstance(raw, list):
        return {
            "schema": CACHE_SCHEMA,
            "por_agente": {_chave_agente_padrao(): _bucket_legado(raw, "cli-legado", salvo_em)},
        }
    if not isinstance(raw, dict) or "por_agente" in raw or not isinstance(raw.get("ativos"), list):
        return raw
    bucket = {k: raw[k] for k in _CHAVES_BUCKET if k in raw}
    novo = {k: v for k, v in raw.items() if k not in _CHAVES_BUCKET}
    novo["schema"] = CACHE_SCHEMA
    novo["por_agente"] = {_chave_agente_padrao(): bucket}
    return novo


def load_cache_raw() -> dict:
    stamp = _cache_stamp()
    if stamp is not None:
        memo = getattr(_CACHE_MEMO, "value", None)
        if memo is not None and getattr(_CACHE_MEMO, "stamp", None) == stamp:
            return memo
    with CACHE_LOCK:
        # Reconfere dentro do lock: outra thread pode ter gravado enquanto
        # esperávamos, e o arquivo pode ter mudado de assinatura.
        stamp = _cache_stamp()
        if stamp is None:
            _CACHE_MEMO.stamp = None
            _CACHE_MEMO.value = {}
            return {}
        memo = getattr(_CACHE_MEMO, "value", None)
        if memo is not None and getattr(_CACHE_MEMO, "stamp", None) == stamp:
            return memo
        try:
            texto = CACHE_FILE.read_text(encoding="utf-8")
            raw = json.loads(texto)
        except (ValueError, OSError):
            return {}
        if not isinstance(raw, (dict, list)):
            return {}
        migrado = migrar_cache(raw, salvo_em=_mtime_iso())
        if migrado is not raw:
            log.warning(
                "Cache em formato antigo (%s): migrando para o schema %s (backup: %s)",
                type(raw).__name__, CACHE_SCHEMA, _arquivo_legado().name,
            )
            try:
                _arquivo_legado().write_text(texto, encoding="utf-8")
            except OSError:
                log.warning("Nao foi possivel gravar o backup do cache antigo")
            # save_cache_raw atualiza o memo desta thread.
            save_cache_raw(migrado)
            return migrado
        _CACHE_MEMO.stamp = stamp
        _CACHE_MEMO.value = migrado
        return migrado


def _ticket_id(t) -> int | None:
    if not isinstance(t, dict) or t.get("id") is None:
        return None
    try:
        return int(t["id"])
    except (TypeError, ValueError):
        return None


def iter_cache_tickets(raw: dict | None = None,
                       ordem: tuple[str, ...] = ("historico",) + FONTES_CACHE,
                       dedup: bool = True,
                       por_bucket: bool = False):
    """Percorre os tickets do cache, devolvendo ``(ticket, origem)``.

    Ponto **único** de varredura do cache: quem precisa dos tickets escolhe a ordem
    das fontes em vez de repetir o laço `por_agente → chave` com a sua própria regra.

    - ``ordem``: fontes na sequência desejada (``"historico"`` ou chave do bucket).
    - ``dedup``: com ``True`` (padrão), a primeira ocorrência de cada id vence — é o
      que permitia a um ticket repetido em vários buckets aparecer duas vezes.
    - ``por_bucket``: com ``True``, varre bucket por bucket em vez de fonte por fonte.
      Como um ticket pode ter cópias em vários buckets (ex.: ``jefferson/ativos`` e
      ``*/ativos``) e cada cópia vem de um sync diferente, essa ordem define qual
      cópia sobrevive — quem comparava cópias no código antigo passa ``por_bucket=True``
      e mantém o mesmo resultado. O histórico entra antes dos buckets quando é a
      primeira fonte da ``ordem``, e depois deles caso contrário.
    """
    cache = raw if isinstance(raw, dict) else load_cache_raw()
    buckets = cache.get("por_agente")
    buckets = list(buckets.values()) if isinstance(buckets, dict) else []
    historico = cache.get("historico")
    historico = historico if isinstance(historico, list) else []
    vistos: set[int] = set()

    def pular(t) -> bool:
        tid = _ticket_id(t)
        if tid is None:
            return True
        if dedup and tid in vistos:
            return True
        vistos.add(tid)
        return False

    def tickets_e_origens():
        if not por_bucket:
            for nome in ordem:
                if nome == "historico":
                    for t in historico:
                        yield t, nome
                    continue
                for b in buckets:
                    if not isinstance(b, dict):
                        continue
                    for t in b.get(nome) or []:
                        yield t, nome
            return
        fontes = tuple(n for n in ordem if n != "historico")
        com_historico = "historico" in ordem
        historico_primeiro = com_historico and ordem[0] == "historico"
        if historico_primeiro:
            for t in historico:
                yield t, "historico"
        for b in buckets:
            if not isinstance(b, dict):
                continue
            for nome in fontes:
                for t in b.get(nome) or []:
                    yield t, nome
        if com_historico and not historico_primeiro:
            for t in historico:
                yield t, "historico"

    for t, origem in tickets_e_origens():
        if pular(t):
            continue
        yield t, origem


def save_cache_raw(raw: dict) -> None:
    """Grava o cache inteiro de forma atômica (tmp + replace) e com o schema atual.

    Escrita atômica evita deixar o arquivo truncado/corrompido se outra thread
    (ou outro processo, como o CLI) gravar ao mesmo tempo.
    """
    with CACHE_LOCK:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        saida = dict(raw) if isinstance(raw, dict) else {}
        saida["schema"] = CACHE_SCHEMA
        tmp = CACHE_FILE.with_name(CACHE_FILE.name + ".tmp")
        tmp.write_text(
            json.dumps(saida, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(tmp, CACHE_FILE)
        # Mantém o memo desta thread coerente com o que acabou de ir ao disco.
        _CACHE_MEMO.stamp = _cache_stamp()
        _CACHE_MEMO.value = saida


def salvar_bucket_agente(agente: str, ativos: list[dict],
                         resolvidos: list[dict] | None = None,
                         extras: dict | None = None) -> dict:
    """Grava (read-modify-write) o bucket de um atendente preservando o resto do cache.

    Caminho único de escrita para quem só quer atualizar tickets: mantém pesquisa de
    satisfação, histórico, soluções, filas locais e plantão intactos.
    """
    chave = ((agente or AGENT_EMAIL or "").lower()) or CHAVE_TODOS
    with CACHE_LOCK:
        raw = load_cache_raw()
        bucket = raw.setdefault("por_agente", {}).setdefault(chave, {})
        if resolvidos is not None:
            bucket["resolvidos"] = list(resolvidos)
        bucket["ativos"] = list(ativos or [])
        bucket["salvo_em"] = datetime.now().isoformat(timespec="seconds")
        if extras:
            bucket.update(extras)
        save_cache_raw(raw)
        return dict(bucket)


def _idade_minutos(iso: str | None) -> float | None:
    """Minutos desde o timestamp (aceita ISO com ou sem timezone; None se inválido)."""
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return (datetime.now() - dt).total_seconds() / 60.0


def bucket_do_agente(agente: str | None = None, raw: dict | None = None) -> dict:
    """Bucket de cache de um atendente (ativos/resolvidos/interações/fora da carga).

    Acesso único ao cache compartilhado: usa o painel, o CLI e os relatórios.
    Passe ``raw`` quando o cache já estiver carregado, para não reler o arquivo.
    """
    cache = raw if isinstance(raw, dict) else load_cache_raw()
    chave = ((agente or AGENT_EMAIL or "").lower()) or CHAVE_TODOS
    buckets = cache.get("por_agente")
    bucket = buckets.get(chave) if isinstance(buckets, dict) else None
    return bucket if isinstance(bucket, dict) else {}


def tickets_do_cache(agente: str | None = None,
                     max_age_minutes: int | None = None) -> list[dict] | None:
    """Tickets ativos salvos para o atendente. ``None`` se não houver nada ou se os
    dados forem mais velhos que ``max_age_minutes``."""
    bucket = bucket_do_agente(agente)
    ativos = bucket.get("ativos")
    if not isinstance(ativos, list) or not ativos:
        return None
    if max_age_minutes is not None:
        idade = _idade_minutos(bucket.get("salvo_em"))
        if idade is not None and idade > max_age_minutes:
            return None
    return ativos


def merge_historico(novos: list[dict]) -> int:
    """Faz upsert de tickets no indice de historico de forma atomica. Retorna o total."""
    with CACHE_LOCK:
        raw = load_cache_raw()
        indice: dict[int, dict] = {}
        for t in raw.get("historico") or []:
            if isinstance(t, dict) and t.get("id"):
                indice[int(t["id"])] = t
        for t in novos:
            if isinstance(t, dict) and t.get("id"):
                indice[int(t["id"])] = t
        raw["historico"] = list(indice.values())
        raw["historico_salvo_em"] = datetime.now().isoformat(timespec="seconds")
        save_cache_raw(raw)
        return len(indice)


def historico_cobertura() -> dict:
    """Informa o intervalo coberto pelo indice de historico (por resolvedIn)."""
    raw = load_cache_raw()
    datas = []
    for t in raw.get("historico") or []:
        d = parse_date(t.get("resolvedIn") or t.get("closedIn") or t.get("createdDate"))
        if d:
            datas.append(d)
    if not datas:
        return {"total": 0, "min": "", "max": "", "salvo_em": raw.get("historico_salvo_em") or ""}
    return {
        "total": len(raw.get("historico") or []),
        "min": min(datas).strftime("%d/%m/%Y"),
        "max": max(datas).strftime("%d/%m/%Y"),
        "salvo_em": raw.get("historico_salvo_em") or "",
    }


def load_cache_payload(agente: str | None = None) -> dict | None:
    agente = (agente or AGENT_EMAIL).lower()
    raw = load_cache_raw()
    bucket = bucket_do_agente(agente, raw=raw)
    if not bucket or not isinstance(bucket.get("ativos"), list) or not bucket["ativos"]:
        return None
    survey = raw.get("survey") if isinstance(raw.get("survey"), list) else bucket.get("survey")
    payload = build_payload(
        bucket["ativos"],
        resolved_raw=bucket.get("resolvidos") or [],
        survey_raw=survey or [],
        interagiu_raw=bucket.get("interagiu_hoje") or [],
        fora_raw=bucket.get("fora_carga") or [],
        agente_email=agente,
    )
    salvo = bucket.get("salvo_em") or ""
    idade_min = _idade_minutos(salvo) if salvo else None
    payload["cache_salvo_em"] = salvo
    payload["cache_idade_horas"] = idade_min / 60.0 if idade_min is not None else None
    return payload


# =====================================================================
# Relatórios gerenciais — 3 abas:
#   * Tempo e produtividade
#   * Gargalos entre setores
#   * Inteligência de problemas
#
# Tudo é derivado do cache local (histórico + ativos/resolvidos + survey +
# ações indexadas). Nada consulta a API: onde o dado não existe no cache, o
# payload traz um aviso em vez de inventar número.
# =====================================================================

REL_PERIODOS = (0, 7, 30, 90)
SETOR_SUPORTE = "Atendimento ao Cliente"
SETOR_DEV = "Engenharia e Tecnologia"
SETOR_IMPL = "Implantação"
SETORES_EXTERNOS = (SETOR_DEV, SETOR_IMPL)
CATEGORIAS_INCIDENTE = ("Instabilidade", "Problema de Sistema", "Problema Operacional")


def _rel_now() -> datetime:
    now = datetime.now()
    return now.astimezone() if now.tzinfo is None else now


def _rel_dias(dias) -> int:
    try:
        d = int(dias)
    except (TypeError, ValueError):
        d = 30
    return d if d in REL_PERIODOS else 30


def _rel_periodo(dias: int):
    now = _rel_now()
    ini = now - timedelta(days=dias) if dias else None
    return now, ini


def _rel_iso(d: datetime | None):
    return d.isoformat(timespec="seconds") if d else None


def _rel_pctile(vals, p):
    vals = sorted(v for v in vals if v is not None)
    if not vals:
        return None
    k = (len(vals) - 1) * p / 100.0
    lo, hi = int(math.floor(k)), int(math.ceil(k))
    if lo == hi:
        return round(vals[lo], 1)
    return round(vals[lo] * (hi - k) + vals[hi] * (k - lo), 1)


def _rel_stats(vals) -> dict:
    vals = [v for v in vals if v is not None]
    if not vals:
        return {"n": 0, "media": None, "mediana": None, "p90": None, "max": None}
    return {
        "n": len(vals),
        "media": round(sum(vals) / len(vals), 1),
        "mediana": _rel_pctile(vals, 50),
        "p90": _rel_pctile(vals, 90),
        "max": round(max(vals), 1),
    }


def _rel_top(cnt: dict, limite: int = 15) -> list[dict]:
    ordenado = sorted(cnt.items(), key=lambda kv: (-kv[1], str(kv[0])))
    return [{"nome": k, "n": n} for k, n in ordenado[:limite]]


def _rel_base_por_id(raw: dict | None = None) -> dict:
    """Um registro por ticket do cache, preferindo a cópia mais rica: ativos e
    resolvidos vêm expandidos (actions/ownerHistories); o histórico cobre o
    restante. Usa ``iter_cache_tickets`` (primeira ocorrência vence) com a ordem
    ativos → resolvidos → histórico."""
    raw = raw if isinstance(raw, dict) else load_cache_raw()
    por_id: dict[int, dict] = {}
    for t, _origem in iter_cache_tickets(
            raw=raw, ordem=("ativos", "resolvidos", "historico"), por_bucket=True):
        tid = _ticket_id(t)
        if tid is not None:
            por_id[tid] = t
    return por_id


def _rel_escopo(por_id: dict, raw: dict | None = None):
    raw = raw if isinstance(raw, dict) else load_cache_raw()
    ag = agente_atual().lower()
    if not ag or ag == "*":
        return por_id, None
    anome = None
    for a in raw.get("agentes") or []:
        if isinstance(a, dict) and (a.get("email") or "").lower() == ag:
            anome = _norm(a.get("nome") or a.get("businessName") or "")
            break
    return {tid: t for tid, t in por_id.items()
            if _pertence_agente(t, ag, anome or "")}, ag


def _rel_primeira_resposta_h(t: dict):
    """Horas entre a abertura pelo cliente e a 1ª ação da equipe (e-mail do
    domínio interno). Só sai quando o array ``actions`` existe no cache."""
    created = parse_date(t.get("createdDate"))
    if not created:
        return None
    criador = ((t.get("createdBy") or {}).get("email") or "").lower()
    if criador.endswith(INTERNO):
        return None
    acts = t.get("actions")
    if not isinstance(acts, list) or not acts:
        return None
    parsed = []
    for a in acts:
        d = parse_date(a.get("createdDate"))
        if d:
            parsed.append((d, a))
    parsed.sort(key=lambda x: x[0])
    for d, a in parsed:
        em = ((a.get("createdBy") or {}).get("email") or "").lower()
        if em.endswith(INTERNO) and (d - created).total_seconds() > 60:
            return round((d - created).total_seconds() / 3600.0, 2)
    return None


def _rel_acoes_index(raw: dict | None = None) -> dict:
    """Índice de ações (persistido + embutidas no cache), tid -> entry."""
    try:
        from macros import coletar_acoes
        return coletar_acoes(raw)
    except Exception:
        return {}


def build_relatorio_tempo(dias: int = 30, por_id: dict | None = None,
                          raw: dict | None = None) -> dict:
    now, ini = _rel_periodo(_rel_dias(dias))
    raw = raw if isinstance(raw, dict) else load_cache_raw()
    por_id = por_id if por_id is not None else _rel_base_por_id(raw)
    por_id, ag = _rel_escopo(por_id, raw)
    ids_escopo = set(por_id)
    survey = [r for r in (raw.get("survey") or []) if isinstance(r, dict)]

    def no_periodo(d):
        return d is not None and (ini is None or d >= ini) and d < now

    criados = resolvidos = reabertos = fcr = 0
    t_res, t_aberto, t_util, t_parado, contatos, primeira = [], [], [], [], [], []
    reab_atend, reab_assunto, reab_cat = {}, {}, {}
    cobertura_reopen = 0
    for t in por_id.values():
        created = parse_date(t.get("createdDate"))
        resolved = parse_date(t.get("resolvedIn") or t.get("closedIn"))
        closed = parse_date(t.get("closedIn"))
        reopened = parse_date(t.get("reopenedIn"))
        if "reopenedIn" in t:
            cobertura_reopen += 1
        if no_periodo(created):
            criados += 1
        if reopened and no_periodo(reopened):
            reabertos += 1
            ow = _owner_name(t) or "—"
            reab_atend[ow] = reab_atend.get(ow, 0) + 1
            asu = (t.get("subject") or "—").strip() or "—"
            reab_assunto[asu] = reab_assunto.get(asu, 0) + 1
            cat = t.get("category") or "Sem categoria"
            reab_cat[cat] = reab_cat.get(cat, 0) + 1
        if resolved and no_periodo(resolved):
            resolvidos += 1
            if t.get("resolvedInFirstCall"):
                fcr += 1
            if created and resolved >= created:
                t_res.append((resolved - created).total_seconds() / 3600.0)
                fim = closed if (closed and closed >= created) else resolved
                t_aberto.append((fim - created).total_seconds() / 3600.0)
            for campo, destino in (("lifeTimeWorkingTime", t_util),
                                   ("stoppedTimeWorkingTime", t_parado)):
                v = t.get(campo)
                if isinstance(v, (int, float)) and v >= 0:
                    destino.append(v / 60.0)
        ac = t.get("actionCount")
        if created and no_periodo(created) and isinstance(ac, (int, float)):
            contatos.append(ac)
        pr = _rel_primeira_resposta_h(t)
        if created and no_periodo(created) and pr is not None:
            primeira.append(pr)

    # CSAT: qualidade, quantidade e taxa de resposta.
    sat = insat = 0
    nps_vals, negativos = [], []
    for r in survey:
        d = parse_date(r.get("responseDate"))
        if not no_periodo(d):
            continue
        try:
            tid = int(r.get("ticketId"))
        except (TypeError, ValueError):
            continue
        if tid not in ids_escopo:
            continue
        rotulo, s, i = survey_assessment(r)
        if s:
            sat += 1
        if i:
            insat += 1
        if r.get("type") == 3 and isinstance(r.get("value"), int):
            nps_vals.append(r["value"])
        if i:
            base = por_id.get(tid) or {}
            negativos.append({
                "ticket": tid,
                "cliente": clean(client_name(base)) if base else "",
                "assunto": clean(base.get("subject") or ""),
                "data": d.strftime("%d/%m/%Y") if d else "",
                "rotulo": rotulo,
                "comentario": clean(r.get("commentary") or ""),
            })
    respondidos = sat + insat
    nps = None
    if nps_vals:
        nps = round(100 * (sum(1 for v in nps_vals if v >= 9)
                           - sum(1 for v in nps_vals if v <= 6)) / len(nps_vals))

    # Resolvidos sem ação pública final (ações: type 1 = interna).
    indice = _rel_acoes_index(raw)
    sem_publica = []
    cob_acoes = 0
    for tid, t in por_id.items():
        resolved = parse_date(t.get("resolvedIn") or t.get("closedIn"))
        if not (resolved and no_periodo(resolved)):
            continue
        entry = indice.get(str(tid))
        if not entry:
            continue
        cob_acoes += 1
        acoes = sorted(entry.get("acoes") or [], key=lambda a: a.get("data") or "")
        tem_publica = any(not a.get("interna") for a in acoes)
        if not acoes or not tem_publica or acoes[-1].get("interna"):
            sem_publica.append({
                "ticket": tid,
                "cliente": clean(client_name(t)),
                "assunto": clean(t.get("subject") or ""),
                "data": resolved.strftime("%d/%m/%Y"),
                "acoes": len(acoes),
            })

    # Backlog inicial x final.
    def aberto_em(quando):
        n = 0
        for t in por_id.values():
            c = parse_date(t.get("createdDate"))
            r = parse_date(t.get("resolvedIn") or t.get("closedIn"))
            if c and c < quando and (r is None or r >= quando):
                n += 1
        return n
    datas_criacao = [parse_date(t.get("createdDate")) for t in por_id.values()]
    datas_criacao = [d for d in datas_criacao if d]
    ini_bk = ini or (min(datas_criacao) if datas_criacao else now)
    backlog_ini = aberto_em(ini_bk)
    backlog_fim = aberto_em(now)

    # Resolvidos por dia / semana / mês.
    por_dia, por_sem, por_mes = {}, {}, {}
    for t in por_id.values():
        r = parse_date(t.get("resolvedIn") or t.get("closedIn"))
        if not (r and no_periodo(r)):
            continue
        dia = r.strftime("%Y-%m-%d")
        por_dia[dia] = por_dia.get(dia, 0) + 1
        iso = r.isocalendar()
        sem = f"{iso[0]}-S{iso[1]:02d}"
        por_sem[sem] = por_sem.get(sem, 0) + 1
        mes = r.strftime("%Y-%m")
        por_mes[mes] = por_mes.get(mes, 0) + 1

    return {
        "periodo": {"dias": _rel_dias(dias), "de": _rel_iso(ini), "ate": _rel_iso(now)},
        "primeira_resposta": _rel_stats(primeira),
        "resolucao": _rel_stats(t_res),
        "tempo_aberto": _rel_stats(t_aberto),
        "tempo_util": _rel_stats(t_util),
        "tempo_parado": _rel_stats(t_parado),
        "contatos": _rel_stats(contatos),
        "resolvidos": resolvidos,
        "criados": criados,
        "reabertos": reabertos,
        "fcr": fcr,
        "fcr_pct": round(100 * fcr / resolvidos) if resolvidos else None,
        "csat": {
            "satisfeitos": sat, "insatisfeitos": insat, "respondidos": respondidos,
            "pct": round(100 * sat / respondidos) if respondidos else None,
            "nps": nps, "resolvidos": resolvidos,
            "taxa_resposta_pct": round(100 * respondidos / resolvidos) if resolvidos else None,
        },
        "negativos": sorted(negativos, key=lambda x: x["data"], reverse=True),
        "reabertura": {
            "por_atendente": _rel_top(reab_atend, 10),
            "por_assunto": _rel_top(reab_assunto, 10),
            "por_categoria": _rel_top(reab_cat, 10),
        },
        "sem_acao_publica": {
            "itens": sem_publica[:40], "total": len(sem_publica), "cobertura": cob_acoes,
        },
        "backlog": {"inicial": backlog_ini, "final": backlog_fim,
                    "delta": backlog_fim - backlog_ini},
        "resolvidos_por_dia": [{"dia": k, "n": v} for k, v in sorted(por_dia.items())],
        "resolvidos_por_semana": [{"semana": k, "n": v} for k, v in sorted(por_sem.items())],
        "resolvidos_por_mes": [{"mes": k, "n": v} for k, v in sorted(por_mes.items())],
        "cobertura": {"acoes": len(primeira), "reopen": cobertura_reopen, "total": len(por_id)},
    }


def build_relatorio_gargalos(dias: int = 30, por_id: dict | None = None,
                             raw: dict | None = None) -> dict:
    now, ini = _rel_periodo(_rel_dias(dias))
    raw = raw if isinstance(raw, dict) else load_cache_raw()
    por_id = por_id if por_id is not None else _rel_base_por_id(raw)
    por_id, ag = _rel_escopo(por_id, raw)

    def no_periodo(d):
        return d is not None and (ini is None or d >= ini) and d < now

    seq_por_ticket = {}
    for tid, t in por_id.items():
        hs = t.get("ownerHistories")
        if not isinstance(hs, list) or not hs:
            continue
        seq = []
        for h in hs:
            d = parse_date(h.get("changedDate"))
            if d:
                seq.append((d, h.get("ownerTeam") or ""))
        if seq:
            seq.sort(key=lambda x: x[0])
            seq_por_ticket[tid] = seq

    tempo_setor, encaminhados, enc_lista, devolvidos, transicoes = {}, {}, [], [], {}
    for tid, seq in seq_por_ticket.items():
        t = por_id[tid]
        resolved = parse_date(t.get("resolvedIn") or t.get("closedIn"))
        for i, (d, team) in enumerate(seq):
            prox = seq[i + 1][0] if i + 1 < len(seq) else (resolved or now)
            horas = (prox - d).total_seconds() / 3600.0
            if horas >= 0:
                tempo_setor.setdefault(team or "—", []).append(horas)
        for i in range(len(seq) - 1):
            a_team, b_team = seq[i][1], seq[i + 1][1]
            b_data = seq[i + 1][0]
            transicoes[(a_team, b_team)] = transicoes.get((a_team, b_team), 0) + 1
            if a_team in SETORES_EXTERNOS and b_team == SETOR_SUPORTE and no_periodo(b_data):
                devolvidos.append({
                    "ticket": tid, "cliente": clean(client_name(t)),
                    "assunto": clean(t.get("subject") or ""),
                    "de": a_team, "data": b_data.strftime("%d/%m/%Y"),
                })
        for j, (d, team) in enumerate(seq):
            if team in SETORES_EXTERNOS and no_periodo(d):
                anteriores = [x[1] for x in seq[:j] if x[1]]
                entrou_de = anteriores[-1] if anteriores else ""
                if entrou_de != team:
                    encaminhados[team] = encaminhados.get(team, 0) + 1
                    enc_lista.append({
                        "ticket": tid, "cliente": clean(client_name(t)),
                        "assunto": clean(t.get("subject") or ""),
                        "setor": team, "data": d.strftime("%d/%m/%Y"),
                    })

    ordem_setores = (SETOR_SUPORTE, SETOR_DEV, SETOR_IMPL)
    tempo_lista = [{"setor": s, "stats": _rel_stats(tempo_setor[s])}
                   for s in ordem_setores if s in tempo_setor]
    tempo_lista += [{"setor": s, "stats": _rel_stats(vals)}
                    for s, vals in tempo_setor.items() if s not in ordem_setores]

    try:
        rec = _map_recorrentes()
    except Exception:
        rec = {}
    rec_itens = []
    for tid in por_id:
        r = rec.get(str(tid))
        if r and (r.get("total") or 0) >= 2:
            t = por_id[tid]
            rec_itens.append({
                "ticket": tid, "cliente": clean(client_name(t)),
                "assunto": clean(t.get("subject") or ""),
                "ocorrencias": r.get("total"), "ultima": r.get("ultima") or "",
            })
    rec_itens.sort(key=lambda x: (-x["ocorrencias"], x["cliente"]))

    return {
        "periodo": {"dias": _rel_dias(dias), "de": _rel_iso(ini), "ate": _rel_iso(now)},
        "encaminhados": [{"setor": s, "n": n}
                         for s, n in sorted(encaminhados.items(), key=lambda kv: -kv[1])],
        "encaminhados_lista": enc_lista[:60],
        "tempo_setor": tempo_lista,
        "devolvidos": devolvidos[:60],
        "devolvidos_total": len(devolvidos),
        "transicoes": [{"de": a or "—", "para": b or "—", "n": n}
                       for (a, b), n in sorted(transicoes.items(), key=lambda kv: -kv[1])],
        "recorrentes": rec_itens[:40],
        "internos_vinculados": {"disponivel": False},
        "cobertura": {"setores": len(seq_por_ticket), "total": len(por_id)},
    }


def build_relatorio_inteligencia(dias: int = 30, por_id: dict | None = None,
                                 raw: dict | None = None) -> dict:
    now, ini = _rel_periodo(_rel_dias(dias))
    raw = raw if isinstance(raw, dict) else load_cache_raw()
    por_id = por_id if por_id is not None else _rel_base_por_id(raw)
    por_id, ag = _rel_escopo(por_id, raw)

    def no_periodo(d):
        return d is not None and (ini is None or d >= ini) and d < now

    assuntos, cats, servicos, clientes, diario = {}, {}, {}, {}, {}
    erros, incidentes, sugestoes = {}, {}, []
    datas_por_cliente = {}
    for t in por_id.values():
        created = parse_date(t.get("createdDate"))
        if not no_periodo(created):
            continue
        subj = normalize_subject(t.get("subject") or "") or (t.get("subject") or "").strip() or "—"
        cat = t.get("category") or "Sem categoria"
        sf = t.get("serviceFirstLevel") or "Sem serviço"
        cl = clean(client_name(t)) or "—"
        assuntos[subj] = assuntos.get(subj, 0) + 1
        cats[cat] = cats.get(cat, 0) + 1
        servicos[sf] = servicos.get(sf, 0) + 1
        clientes[cl] = clientes.get(cl, 0) + 1
        dia = created.strftime("%Y-%m-%d")
        diario[dia] = diario.get(dia, 0) + 1
        if cat in CATEGORIAS_INCIDENTE:
            erros[(cl, sf)] = erros.get((cl, sf), 0) + 1
            incidentes[cat] = incidentes.get(cat, 0) + 1
        if cat == "Sugestão":
            sugestoes.append({"ticket": _ticket_id(t), "cliente": cl,
                              "assunto": clean(t.get("subject") or "")})
        datas_por_cliente.setdefault(cl, []).append(created)

    erros_lista = [{"cliente": k[0], "modulo": k[1], "n": v}
                   for k, v in sorted(erros.items(), key=lambda kv: (-kv[1], kv[0]))][:25]

    # Clientes que abrem muitos tickets em sequência (rajada em janela de 7 dias).
    clientes_seq = []
    for cl, datas in datas_por_cliente.items():
        if len(datas) < 3:
            continue
        datas = sorted(datas)
        j = 0
        rajada = 0
        for i in range(len(datas)):
            while datas[i] - datas[j] > timedelta(days=7):
                j += 1
            rajada = max(rajada, i - j + 1)
        clientes_seq.append({"cliente": cl, "total": len(datas), "rajada_7d": rajada,
                             "ultima": datas[-1].strftime("%d/%m/%Y")})
    clientes_seq.sort(key=lambda x: (-x["rajada_7d"], -x["total"], x["cliente"]))

    # Picos de chamados por volume diário (proxy — não há log de releases).
    dias = sorted(diario.items())
    vals = [v for _, v in dias]
    picos = []
    perfil = {"media_dia": None, "max_dia": None}
    if vals:
        media = sum(vals) / len(vals)
        desvio = (sum((v - media) ** 2 for v in vals) / len(vals)) ** 0.5
        limiar = max(media + 1.5 * desvio, media * 1.5)
        picos = [{"dia": d, "n": n} for d, n in dias if n > limiar and n > 1]
        picos.sort(key=lambda x: -x["n"])
        perfil = {"media_dia": round(media, 1), "max_dia": max(vals)}

    return {
        "periodo": {"dias": _rel_dias(dias), "de": _rel_iso(ini), "ate": _rel_iso(now)},
        "top_assuntos": _rel_top(assuntos, 15),
        "top_categorias": _rel_top(cats, 15),
        "top_servicos": _rel_top(servicos, 15),
        "top_clientes": _rel_top(clientes, 15),
        "erros_recorrentes": erros_lista,
        "incidentes": _rel_top(incidentes, 10),
        "sugestoes": sugestoes[:40],
        "sugestoes_total": len(sugestoes),
        "clientes_sequencia": clientes_seq[:25],
        "picos": {"dias": picos[:15], "perfil": perfil,
                  "diario": [{"dia": d, "n": n} for d, n in dias]},
        "versoes_disponivel": False,
    }


def build_relatorio(dias: int = 30, agente: str | None = None) -> dict:
    """Payload único das 3 abas de relatório."""
    if agente is not None:
        definir_agente(agente)
    dias = _rel_dias(dias)
    raw = load_cache_raw()
    por_id = _rel_base_por_id(raw)
    tempo = build_relatorio_tempo(dias, por_id=por_id, raw=raw)
    gargalos = build_relatorio_gargalos(dias, por_id=por_id, raw=raw)
    intelig = build_relatorio_inteligencia(dias, por_id=por_id, raw=raw)
    cob = tempo.get("cobertura", {})
    cobg = gargalos.get("cobertura", {})
    avisos = [
        f"1ª resposta / ações públicas: {cob.get('acoes', 0)} de {cob.get('total', 0)} "
        "tickets têm ações no cache (só ativos e alguns resolvidos recentes).",
        f"Reabertura: {cob.get('reopen', 0)} de {cob.get('total', 0)} tickets trazem "
        "reopenedIn (o histórico longo não guarda esse campo).",
        f"Setores / tempo por setor: {cobg.get('setores', 0)} de {cobg.get('total', 0)} "
        "tickets têm histórico de encaminhamento (ownerHistories).",
        "Módulo/versão e log de releases não existem no Movidesk — usamos serviço e "
        "categoria como proxy e picos por volume diário.",
        "Tickets internos vinculados (parentTickets/childrenTickets) não vêm no cache.",
    ]
    ag_atual = agente_atual().lower()
    ag_atual = None if (not ag_atual or ag_atual == "*") else ag_atual
    return {
        "gerado_em": _rel_now().isoformat(timespec="seconds"),
        "dias": dias,
        "agente": agente_atual(),
        "escopo": "perfil" if ag_atual else "todos",
        "avisos": avisos,
        "tempo": tempo,
        "gargalos": gargalos,
        "inteligencia": intelig,
    }


def clean(text: str) -> str:
    return html.unescape(text or "")
