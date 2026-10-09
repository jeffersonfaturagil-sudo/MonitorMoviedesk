import html
import json
import sys
import threading
from contextvars import ContextVar
from datetime import datetime, time, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from analyzer import build_ticket_info, find_merge_candidates, hours_between, parse_date, sla_risks  # noqa: E402
from config import AGENT_EMAIL, AGENT_NAME, CACHE_FILE, MOVIDESK_WEB_URL, papel_agente  # noqa: E402

SLA_LEVELS = ("VENCIDO", "RISCO")

CACHE_LOCK = threading.RLock()

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
        "sla_nivel": t.sla_level,
        "sla_detalhe": t.sla_detail,
        "urgencia": t.urgency,
        "owner": owner_name(t.raw),
        "etiqueta": info["etiqueta"],
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
    ok = [t for t in infos if t.level == "OK"]
    candidates = find_merge_candidates(infos)
    sla_in_risk = [t for t in sla_risks(infos)]

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

    pesquisa = build_pesquisa(infos, resolved_raw or [], survey_raw or [])
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

    return {
        "gerado_em": now.isoformat(timespec="seconds"),
        "movidesk_web": MOVIDESK_WEB_URL,
        "agente": agente_atual(),
        "resumo": {
            "ativos": len(infos),
            "criticos": len(critical),
            "atencao": len(warning),
            "ok": len(ok),
            "mesclas": len(candidates),
            "sla": len(sla_in_risk),
        },
        "criticos": [ticket_dict(t) for t in critical],
        "atencao": [ticket_dict(t) for t in warning],
        "ok": [ticket_dict(t) for t in ok],
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


def build_pesquisa(infos: list, resolved_raw: list[dict], survey_raw: list[dict]) -> dict:
    from analyzer import build_ticket_info, client_name, parse_date

    mapa: dict[int, tuple[str, str]] = {t.id: (t.client, t.subject) for t in infos}
    now = datetime.now()
    for r in resolved_raw:
        try:
            tid = int(r.get("id"))
        except (TypeError, ValueError):
            continue
        if tid not in mapa:
            info = build_ticket_info(r, now)
            mapa[tid] = (info.client, info.subject)

    itens = []
    for r in survey_raw:
        tid = r.get("ticketId")
        if not isinstance(tid, int) or tid not in mapa:
            continue
        cliente, assunto = mapa[tid]
        rotulo, satisfeito, insatisfeito = survey_assessment(r)
        data = parse_date(r.get("responseDate"))
        itens.append({
            "ticket": tid,
            "cliente": clean(cliente),
            "assunto": clean(assunto),
            "data": data.strftime("%d/%m/%Y %H:%M") if data else "",
            "data_iso": data.isoformat() if data else "",
            "rotulo": rotulo,
            "satisfeito": satisfeito,
            "insatisfeito": insatisfeito,
            "comentario": clean(r.get("commentary") or ""),
            "tipo": r.get("type"),
            "valor": r.get("value"),
        })
    itens.sort(key=lambda x: x["data_iso"], reverse=True)

    satisfeitos = sum(1 for i in itens if i["satisfeito"])
    insatisfeitos = sum(1 for i in itens if i["insatisfeito"])
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
            "pct_satisfeitos": pct,
            "nps": nps,
            "periodo": periodo,
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
    todos = [t for t in infos if t.base_status not in ('Resolved','Closed','Canceled')]
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
        'kanban':kanban
    }


def build_agenda(
    infos: list,
    resolvidos_raw: list[dict],
    interagiu_raw: list[dict],
    fora_raw: list[dict],
    now: datetime,
    agente_nome: str | None = None,
) -> dict:
    from analyzer import build_ticket_info
    from feriados import ultimo_dia_util_antes, dias_fechados_entre

    hoje = now.strftime("%Y-%m-%d")
    abertos = ("Resolved", "Closed", "Canceled")
    tecnico = agente_nome or AGENT_NAME or "Não configurado"

    dia_base_date = ultimo_dia_util_antes(now.date())
    dia_base = datetime.combine(dia_base_date, time.min)
    dia_base_str = dia_base.strftime("%Y-%m-%d")
    inicio = dia_base
    fim_exclusive = now.replace(hour=0, minute=0, second=0, microsecond=0)
    fechados = dias_fechados_entre(dia_base_date, fim_exclusive.date())
    dias_cobertos = (fim_exclusive.date() - dia_base_date).days

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
    fora_da_carga = [t for t in fora_da_carga if t.base_status not in ("Resolved", "Closed", "Canceled")]
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

    linhas = [
        f"TÉCNICO: {tecnico} Data: {now.strftime('%d/%m/%Y')}",
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
            "ate": (fim_exclusive - timedelta(days=1)).strftime("%d/%m/%Y"),
            "ate_iso": (fim_exclusive - timedelta(days=1)).strftime("%Y-%m-%d"),
            "dias": dias_cobertos,
            "fechados": [d.strftime("%d/%m/%Y") for d in fechados],
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


STOPWORDS = {
    "a", "o", "as", "os", "um", "uma", "de", "da", "do", "das", "dos", "e", "em", "no", "na",
    "nos", "nas", "para", "por", "com", "sem", "que", "se", "ao", "aos", "nao", "sim", "the",
    "isso", "esta", "esse", "essa", "como", "meu", "minha", "favor", "bom", "dia", "ola",
    "cliente", "ticket", "atendimento", "suporte", "preciso", "quero", "poderia", "ajuda",
}


def _tokens(text: str) -> list[str]:
    import unicodedata

    t = unicodedata.normalize("NFKD", (text or "").lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    raw = "".join(c if c.isalnum() else " " for c in t).split()
    return [w for w in raw if len(w) >= 3 and w not in STOPWORDS]


def _historico_entries() -> list[dict]:
    raw = load_cache_raw()
    entries: dict[int, dict] = {}
    for t in raw.get("historico") or []:
        if isinstance(t, dict) and t.get("id"):
            entries[t["id"]] = t
    por_agente = raw.get("por_agente")
    if isinstance(por_agente, dict):
        for bucket in por_agente.values():
            if not isinstance(bucket, dict):
                continue
            for t in bucket.get("resolvidos") or []:
                if isinstance(t, dict) and t.get("id"):
                    entries.setdefault(t["id"], t)
    return list(entries.values())


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
    raw = load_cache_raw()
    entries: dict[int, dict] = {}
    por_agente = raw.get("por_agente")
    if isinstance(por_agente, dict):
        for bucket in por_agente.values():
            if not isinstance(bucket, dict):
                continue
            for t in bucket.get("ativos") or []:
                if isinstance(t, dict) and t.get("id"):
                    entries.setdefault(t["id"], t)
    return list(entries.values())


_DIAS_SEMANA = ["Seg", "Ter", "Qua", "Qui", "Sex", "Sáb", "Dom"]


def build_mapa_horarios() -> dict:
    """Mapa de calor da abertura de tickets: dia da semana x hora.

    Junta todos os tickets conhecidos no cache (ativos + resolvidos + historico).
    """
    from analyzer import parse_date

    raw = load_cache_raw()
    entradas: dict[int, dict] = {}
    for t in raw.get("historico") or []:
        if isinstance(t, dict) and t.get("id"):
            entradas[int(t["id"])] = t
    por_agente = raw.get("por_agente")
    if isinstance(por_agente, dict):
        for bucket in por_agente.values():
            if not isinstance(bucket, dict):
                continue
            for chave in ("ativos", "resolvidos"):
                for t in bucket.get(chave) or []:
                    if isinstance(t, dict) and t.get("id"):
                        entradas.setdefault(int(t["id"]), t)

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


def build_plantao(hoje_iso: str | None = None) -> dict:
    from datetime import date

    hoje = hoje_iso or date.today().isoformat()
    raw = load_cache_raw()
    plantao = raw.get("plantao") if isinstance(raw.get("plantao"), dict) else {}
    por_data = plantao.get("por_data") if isinstance(plantao.get("por_data"), dict) else {}
    hoje_email = (por_data.get(hoje) or "").lower()

    ativos = _all_active_entries()
    novos = []
    for t in ativos:
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
        })
    novos.sort(key=lambda x: (not x["do_plantao"], not x["sem_responsavel"], x["criado_em"]))
    return {
        "hoje": hoje,
        "hoje_email": hoje_email,
        "por_data": por_data,
        "nota": plantao.get("nota") or "",
        "novos": novos,
        "total_novos": len(novos),
        "total_ativos_indice": len(ativos),
        "atualizado_em": plantao.get("atualizado_em") or "",
    }


def fila_mesclas() -> dict:
    raw = load_cache_raw()
    return raw.get("mesclas") if isinstance(raw.get("mesclas"), dict) else {}


def build_mesclas(escopo: str = "atual", agente: str | None = None) -> dict:
    now = datetime.now()
    if escopo == "todos":
        raw_tickets = _all_active_entries()
    else:
        raw = load_cache_raw()
        bucket = _agent_bucket(raw, (agente or AGENT_EMAIL).lower())
        raw_tickets = (bucket or {}).get("ativos") or []
    infos = [build_ticket_info(t, now) for t in raw_tickets]
    candidates = find_merge_candidates(infos)
    fila = fila_mesclas()
    itens = []
    for c in candidates:
        chave = f"{c['ticket_a']}:{c['ticket_b']}"
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
            "ja_vinculado": c.get("ja_vinculado", False),
            "status": (fila.get(chave) or {}).get("status", ""),
        })
    return {"escopo": escopo, "total": len(itens), "itens": itens, "fila": fila}


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


def build_ranking(incluir_dev: bool = False) -> dict:
    """Comparativo por atendente: carga, tempo medio parado, FCR, reaberturas e CSAT/NPS.

    Por padrao esconde o time de dev (Leandro migrou para dev); passe incluir_dev=True
    para comparar tambem com quem nao esta mais no suporte.
    """
    from analyzer import build_ticket_info

    raw = load_cache_raw()
    por_agente = raw.get("por_agente") if isinstance(raw.get("por_agente"), dict) else {}
    survey = raw.get("survey") if isinstance(raw.get("survey"), list) else []
    now = datetime.now()

    t2owner: dict[int, str] = {}
    for bucket in por_agente.values():
        if not isinstance(bucket, dict):
            continue
        for t in (bucket.get("ativos") or []) + (bucket.get("resolvidos") or []):
            if not isinstance(t, dict):
                continue
            try:
                tid = int(t.get("id"))
            except (TypeError, ValueError):
                continue
            ow = ((t.get("owner") or {}).get("email") or "").lower()
            if ow:
                t2owner.setdefault(tid, ow)

    csat: dict[str, dict] = {}
    for r in survey:
        try:
            tid = int(r.get("ticketId"))
        except (TypeError, ValueError):
            continue
        ow = t2owner.get(tid)
        if not ow:
            continue
        _, sat, insat = survey_assessment(r)
        c = csat.setdefault(ow, {"sat": 0, "insat": 0, "nps": []})
        if sat:
            c["sat"] += 1
        if insat:
            c["insat"] += 1
        if r.get("type") == 3 and isinstance(r.get("value"), int):
            c["nps"].append(r["value"])

    itens = []
    for email, bucket in por_agente.items():
        if not isinstance(bucket, dict):
            continue
        papel = papel_agente(email)
        if papel == "dev" and not incluir_dev:
            continue
        ativos = bucket.get("ativos") or []
        resolvidos = bucket.get("resolvidos") or []
        if not ativos and not resolvidos:
            continue
        infos = [build_ticket_info(t, now) for t in ativos]
        ids_tempo = [t.hours_idle for t in infos if t.hours_idle is not None]
        criticos = sum(1 for t in infos if t.level == "CRITICO")
        fcr = sum(1 for t in resolvidos if t.get("resolvedInFirstCall"))
        reab = sum(1 for t in resolvidos if t.get("reopenedIn"))
        c = csat.get(email, {})
        sat, insat = c.get("sat", 0), c.get("insat", 0)
        resp = sat + insat
        npsv = c.get("nps", [])
        nps = None
        if npsv:
            nps = round(100 * (sum(1 for v in npsv if v >= 9) - sum(1 for v in npsv if v <= 6)) / len(npsv))
        itens.append({
            "email": email,
            "nome": _owner_label(raw, email),
            "papel": papel,
            "ativos": len(ativos),
            "criticos": criticos,
            "tempo_medio_parado": round(sum(ids_tempo) / len(ids_tempo), 1) if ids_tempo else None,
            "resolvidos": len(resolvidos),
            "fcr": fcr,
            "fcr_pct": round(100 * fcr / len(resolvidos)) if resolvidos else None,
            "reabertos": reab,
            "sat": sat,
            "insat": insat,
            "csat_pct": round(100 * sat / resp) if resp else None,
            "nps": nps,
        })
    itens.sort(key=lambda x: (-x["ativos"], -(x["tempo_medio_parado"] or 0)))
    return {"total": len(itens), "itens": itens, "incluir_dev": bool(incluir_dev)}


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


def load_cache_raw() -> dict:
    with CACHE_LOCK:
        if not CACHE_FILE.exists():
            return {}
        try:
            raw = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {}
        if not isinstance(raw, dict):
            return {}
        return raw


def save_cache_raw(raw: dict) -> None:
    with CACHE_LOCK:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")


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


def _agent_bucket(raw: dict, agente: str) -> dict | None:
    por_agente = raw.get("por_agente")
    if isinstance(por_agente, dict):
        return por_agente.get(agente)
    if agente == AGENT_EMAIL.lower() and isinstance(raw.get("ativos"), list):
        return raw
    return None


def load_cache_payload(agente: str | None = None) -> dict | None:
    agente = (agente or AGENT_EMAIL).lower()
    raw = load_cache_raw()
    bucket = _agent_bucket(raw, agente)
    if not bucket or not isinstance(bucket.get("ativos"), list) or not bucket["ativos"]:
        return None
    survey = raw.get("survey") if isinstance(raw.get("survey"), list) else bucket.get("survey")
    return build_payload(
        bucket["ativos"],
        resolved_raw=bucket.get("resolvidos") or [],
        survey_raw=survey or [],
        interagiu_raw=bucket.get("interagiu_hoje") or [],
        fora_raw=bucket.get("fora_carga") or [],
        agente_email=agente,
    )


def clean(text: str) -> str:
    return html.unescape(text or "")
