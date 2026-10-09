"""Rastreio de macros nos tickets.

A API publica do Movidesk nao expoe as macros e as acoes nao trazem metadado
de macro: quando um agente dispara, o TEXTO da macro entra na descricao da acao.
Entao a validacao e feita por casamento de texto (assinatura normalizada).

Indice de acoes combina, sem custo de API:
  - as acoes embutidas nos tickets do cache (ativos/resolvidos sincronizados);
  - acoes buscadas ao vivo (/api/acoes, /api/solucao) e gravadas aqui;
  - a varredura em background do indice de historico (job do servidor).
"""
import json
import re
import threading
import unicodedata
from datetime import datetime
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
import sys

sys.path.insert(0, str(BASE_DIR))

from analyzer import client_name, parse_date  # noqa: E402
from config import CACHE_FILE  # noqa: E402

ACOES_FILE = CACHE_FILE.parent / "acoes_index.json"
MAX_ACAO_CHARS = 8000
MAX_TICKETS = 6000
INDEX_LOCK = threading.RLock()

# Catálogo semeado pelos playbooks (nomes/trechos citados nos dois documentos).
CATALOGO = [
    {"id": "solicitacao_recebida", "nome": "Solicitação Recebida",
     "assinaturas": ["solicitação recebida", "solicitacao recebida"],
     "fonte": "Playbook plantão (recepção)"},
    {"id": "encaminhamento_implantacao", "nome": "Encaminhamento Implantação",
     "assinaturas": ["encaminhamento implantação", "encaminhamento implantacao",
                     "encaminhar para a equipe de implantação",
                     "encaminhar para a equipe de implantacao"],
     "fonte": "Playbook plantão"},
    {"id": "encaminhamento_desenvolvimento", "nome": "Encaminhamento Desenvolvimento",
     "assinaturas": ["encaminhamento desenvolvimento",
                     "encaminhar para a equipe de desenvolvimento",
                     "encaminhamento para o desenvolvimento"],
     "fonte": "Playbook plantão"},
    {"id": "aguardando_cliente", "nome": "Aguardando Cliente",
     "assinaturas": ["aguardando cliente", "aguardando o cliente"],
     "fonte": "Playbook plantão"},
    {"id": "boleto_fraudado", "nome": "Boleto - Análise / FRAUDADO",
     "assinaturas": ["boleto - análise", "boleto - analise",
                     "possibilidade de boleto fraudado", "boleto fraudado"],
     "fonte": "Playbook suporte (boletos)"},
    {"id": "sugestao_01", "nome": "Sugestão 01 - recepção/encaminhamento p/ análise",
     "assinaturas": ["sugestão 01", "sugestao 01"],
     "fonte": "Playbook suporte (sugestões)"},
    {"id": "sugestao_02", "nome": "Sugestão 02 - aprovado p/ análise e desenvolvimento",
     "assinaturas": ["sugestão 02", "sugestao 02"],
     "fonte": "Playbook suporte (sugestões)"},
    {"id": "sugestao_04", "nome": "Sugestão 04 - lista de sugestões sem previsão",
     "assinaturas": ["sugestão 04", "sugestao 04"],
     "fonte": "Playbook suporte (sugestões)"},
    {"id": "credenciamento_pjbank", "nome": "Credenciamento de boleto PJBank",
     "assinaturas": ["credenciamento de boleto pjbank", "credenciamento pjbank"],
     "fonte": "Playbook suporte (PJBank)"},
    {"id": "boleto_hibrido_pix_sicoob", "nome": "Boleto Híbrido com API PIX SICOOB",
     "assinaturas": ["boleto híbrido com api pix sicoob",
                     "boleto hibrido com api pix sicoob",
                     "boleto híbrido com api pix"],
     "fonte": "Playbook suporte (meios de pagamento)"},
]


def normalizar(texto: str) -> str:
    """Minúsculas, sem acentos e com espaços colapsados (para casar texto de macro)."""
    txt = unicodedata.normalize("NFKD", str(texto or ""))
    txt = txt.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"\s+", " ", txt).strip()


def _limpar_acao(a: dict) -> dict:
    d = parse_date(a.get("createdDate"))
    cb = a.get("createdBy") or {}
    texto = str(a.get("description") or "").strip()[:MAX_ACAO_CHARS]
    return {
        "id": a.get("id"),
        "data": d.isoformat() if d else "",
        "autor": cb.get("businessName") or cb.get("email") or "",
        "interna": a.get("type") == 1,
        "texto": texto,
    }


def _entry(tid: str, meta: dict, acoes: list[dict]) -> dict:
    return {
        "cliente": meta.get("cliente") or "",
        "assunto": (meta.get("assunto") or "")[:200],
        "atualizado_em": datetime.now().isoformat(timespec="seconds"),
        "acoes": acoes,
    }


def meta_do_ticket(t: dict) -> dict:
    return {"cliente": client_name(t), "assunto": t.get("subject") or ""}


def meta_local(ticket_id) -> dict:
    """Cliente/assunto do ticket no cache local (sem custo de API)."""
    from dados import load_cache_raw

    alvo = str(int(ticket_id))
    raw = load_cache_raw()
    por_agente = raw.get("por_agente") if isinstance(raw.get("por_agente"), dict) else {}
    for bucket in por_agente.values():
        if not isinstance(bucket, dict):
            continue
        for chave in ("ativos", "resolvidos", "interagiu_hoje", "fora_carga"):
            for t in bucket.get(chave) or []:
                if isinstance(t, dict) and t.get("id") is not None and str(int(t["id"])) == alvo:
                    return meta_do_ticket(t)
    for t in raw.get("historico") or []:
        if isinstance(t, dict) and t.get("id") is not None and str(int(t["id"])) == alvo:
            return meta_do_ticket(t)
    return {}


def carregar_indice() -> dict:
    with INDEX_LOCK:
        if not ACOES_FILE.exists():
            return {"salvo_em": "", "tickets": {}}
        try:
            raw = json.loads(ACOES_FILE.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            return {"salvo_em": "", "tickets": {}}
        if not isinstance(raw, dict) or not isinstance(raw.get("tickets"), dict):
            return {"salvo_em": "", "tickets": {}}
        return raw


def salvar_indice(raw: dict) -> None:
    with INDEX_LOCK:
        tickets = raw.get("tickets") or {}
        if len(tickets) > MAX_TICKETS:
            for tid in sorted(tickets, key=lambda k: tickets[k].get("atualizado_em") or "")[
                : len(tickets) - MAX_TICKETS
            ]:
                tickets.pop(tid, None)
        raw["salvo_em"] = datetime.now().isoformat(timespec="seconds")
        ACOES_FILE.parent.mkdir(parents=True, exist_ok=True)
        ACOES_FILE.write_text(
            json.dumps(raw, ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
        )


def registrar_acoes(ticket_id, acoes: list[dict], meta: dict | None = None,
                    salvar: bool = True) -> dict:
    """Grava ações brutas (formato da API) no índice persistido. Retorna a entry."""
    raw = carregar_indice()
    tid = str(int(ticket_id))
    entry = _entry(tid, meta or {}, [_limpar_acao(a) for a in acoes or []])
    raw.setdefault("tickets", {})[tid] = entry
    if salvar:
        salvar_indice(raw)
    return entry


def salvar_lote(entries: dict[str, dict]) -> None:
    """Grava várias entries de uma vez (economiza I/O no job de indexação)."""
    if not entries:
        return
    raw = carregar_indice()
    raw.setdefault("tickets", {}).update(entries)
    salvar_indice(raw)


def _acoes_do_cache(t: dict) -> list[dict]:
    acoes = t.get("actions")
    if not isinstance(acoes, list) or not acoes:
        return []
    return [_limpar_acao(a) for a in acoes]


def coletar_acoes(raw: dict | None = None) -> dict[str, dict]:
    """Índice completo: persistido + ações embutidas no cache (cache tem prioridade)."""
    from dados import load_cache_raw

    if raw is None:
        raw = load_cache_raw()
    out: dict[str, dict] = {}
    for tid, entry in (carregar_indice().get("tickets") or {}).items():
        if isinstance(entry, dict):
            out[str(tid)] = entry
    por_agente = raw.get("por_agente") if isinstance(raw.get("por_agente"), dict) else {}
    for bucket in por_agente.values():
        if not isinstance(bucket, dict):
            continue
        for chave in ("ativos", "resolvidos", "interagiu_hoje", "fora_carga"):
            for t in bucket.get(chave) or []:
                if not isinstance(t, dict) or t.get("id") is None:
                    continue
                acoes = _acoes_do_cache(t)
                if not acoes:
                    continue
                out[str(int(t["id"]))] = _entry(str(int(t["id"])), meta_do_ticket(t), acoes)
    return out


def _trecho(texto: str, assinaturas: list[str]) -> tuple[str, str] | None:
    """Retorna (trecho, assinatura) da primeira ocorrência, ou None."""
    baixo = texto.lower()
    for sig in assinaturas:
        i = baixo.find(sig.lower())
        if i >= 0:
            ini = max(0, i - 90)
            fim = min(len(texto), i + len(sig) + 320)
            sufixo = "…" if fim < len(texto) else ""
            prefixo = "…" if ini > 0 else ""
            return f"{prefixo}{texto[ini:fim].strip()}{sufixo}", sig
    norm = normalizar(texto)
    for sig in assinaturas:
        if normalizar(sig) and normalizar(sig) in norm:
            return texto[:400].strip() + ("…" if len(texto) > 400 else ""), sig
    return None


def ocorrencias(entry: dict, assinaturas: list[str]) -> list[dict]:
    hits = []
    for a in entry.get("acoes") or []:
        achado = _trecho(a.get("texto") or "", assinaturas)
        if not achado:
            continue
        trecho, sig = achado
        hits.append({
            "data": a.get("data") or "",
            "autor": a.get("autor") or "",
            "interna": bool(a.get("interna")),
            "assinatura": sig,
            "trecho": re.sub(r"\s+", " ", trecho)[:700],
        })
    return hits


def _macro_hits(fontes: dict[str, dict]) -> list[dict]:
    saida = []
    for macro in CATALOGO:
        hits = []
        for tid, entry in fontes.items():
            occ = ocorrencias(entry, macro["assinaturas"])
            if occ:
                hits.append({"ticket": tid, "entry": entry, "ocorrencias": occ})
        hits.sort(key=lambda h: h["ocorrencias"][-1].get("data") or "", reverse=True)
        ultima = hits[0]["ocorrencias"][-1].get("data", "") if hits else ""
        saida.append({**macro, "tickets": [h["ticket"] for h in hits[:10]],
                      "ocorrencias": len(hits), "ultima": ultima[:10]})
    return saida


def estatisticas(raw: dict | None = None) -> dict:
    fontes = coletar_acoes(raw)
    total_acoes = sum(len(e.get("acoes") or []) for e in fontes.values())
    return {
        "catalogo": _macro_hits(fontes),
        "indice": {
            "tickets_com_acoes": len(fontes),
            "acoes": total_acoes,
            "salvo_em": carregar_indice().get("salvo_em") or "",
        },
    }


def analisar_entrada(tid: str, entry: dict, fonte: str,
                     fontes: dict[str, dict] | None = None) -> dict:
    """Valida quais macros do catálogo aparecem em um ticket + compara com o índice."""
    encontradas, nao = [], []
    for macro in CATALOGO:
        occ = ocorrencias(entry, macro["assinaturas"])
        item = {"id": macro["id"], "nome": macro["nome"], "fonte": macro["fonte"],
                "ocorrencias": occ, "quantos": len(occ)}
        if occ:
            encontradas.append(item)
        else:
            nao.append(macro["nome"])
    indice = {}
    if fontes is None:
        fontes = coletar_acoes()
    for m in _macro_hits(fontes):
        indice[m["id"]] = {"tickets": m["ocorrencias"], "ultima": m["ultima"]}
    return {
        "ticket": int(tid),
        "fonte": fonte,
        "acoes": len(entry.get("acoes") or []),
        "cliente": entry.get("cliente") or "",
        "assunto": entry.get("assunto") or "",
        "encontradas": encontradas,
        "nao_encontradas": nao,
        "indice": indice,
    }


def buscar(consulta: str, raw: dict | None = None) -> dict:
    """Busca livre (todos os termos) nas ações do índice."""
    termos = [t for t in normalizar(consulta).split() if t]
    if not termos:
        return {"erro": "consulta vazia", "total": 0, "itens": []}
    fontes = coletar_acoes(raw)
    itens = []
    for tid, entry in fontes.items():
        for a in entry.get("acoes") or []:
            norm = normalizar(a.get("texto") or "")
            if not all(t in norm for t in termos):
                continue
            achado = _trecho(a.get("texto") or "", termos) or (a.get("texto", "")[:400], termos[0])
            itens.append({
                "ticket": int(tid),
                "cliente": entry.get("cliente") or "",
                "assunto": entry.get("assunto") or "",
                "data": a.get("data") or "",
                "autor": a.get("autor") or "",
                "interna": bool(a.get("interna")),
                "trecho": re.sub(r"\s+", " ", achado[0])[:700],
            })
            break
    itens.sort(key=lambda i: i["data"], reverse=True)
    return {"consulta": consulta, "termos": termos, "total": len(itens),
            "tickets_indice": len(fontes), "itens": itens[:40]}


def candidatos_para_indexar(raw: dict | None = None) -> list[dict]:
    """Tickets do cache/histórico sem ações no índice, priorizando os ativos."""
    from dados import load_cache_raw

    if raw is None:
        raw = load_cache_raw()
    ja: set[str] = set(carregar_indice().get("tickets") or {})
    vistos: dict[int, dict] = {}

    def add(t: dict, prio: int) -> None:
        if not isinstance(t, dict) or t.get("id") is None:
            return
        tid = int(t["id"])
        if str(tid) in ja or _acoes_do_cache(t):
            return
        item = {"id": tid, "prio": prio, **meta_do_ticket(t),
                "data": t.get("resolvedIn") or t.get("createdDate") or ""}
        atual = vistos.get(tid)
        if atual is None or prio < atual["prio"]:
            vistos[tid] = item

    por_agente = raw.get("por_agente") if isinstance(raw.get("por_agente"), dict) else {}
    for bucket in por_agente.values():
        if not isinstance(bucket, dict):
            continue
        add_all = (("ativos", 0), ("interagiu_hoje", 1), ("fora_carga", 2), ("resolvidos", 3))
        for chave, prio in add_all:
            for t in bucket.get(chave) or []:
                add(t, prio)
    for t in raw.get("historico") or []:
        add(t, 4)
    saida = sorted(vistos.values(), key=lambda x: (x["prio"], _inverso(x.get("data"))))
    return saida


def _inverso(iso: str) -> str:
    """Ordenação decrescente de data em string ISO/parcial (inverte comparando o complemento)."""
    return "".join(chr(255 - ord(c)) for c in (iso or ""))
