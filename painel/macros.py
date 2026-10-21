"""Rastreio de macros nos tickets.

A API publica do Movidesk nao expoe as macros e as acoes nao trazem metadado
de macro: quando um agente dispara, o TEXTO da macro entra na descricao da acao.
Entao a validacao e feita por casamento de texto (assinatura normalizada).

Indice de acoes combina, sem custo de API:
  - as acoes embutidas nos tickets do cache (ativos/resolvidos sincronizados);
  - acoes buscadas ao vivo (/api/acoes, /api/solucao) e gravadas aqui;
  - a varredura em background do indice de historico (job do servidor).
"""
import hashlib
import json
import re
import threading
import unicodedata
from datetime import datetime
from functools import lru_cache
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
import sys

sys.path.insert(0, str(BASE_DIR))

from analyzer import client_name, parse_date  # noqa: E402
from config import CACHE_FILE  # noqa: E402
# `normalizar` é alias da fonte única em util.py.
from util import norm_texto_ascii as normalizar  # noqa: E402

# Ordem de varredura do índice de ações: ativos primeiro, histórico por último.
ORDEM_INDEXACAO = ("ativos", "interagiu_hoje", "fora_carga", "resolvidos", "historico")
PRIO_FONTE = {fonte: i for i, fonte in enumerate(ORDEM_INDEXACAO)}

ACOES_FILE = CACHE_FILE.parent / "acoes_index.json"
MAX_ACAO_CHARS = 8000
MAX_TICKETS = 6000
INDEX_LOCK = threading.RLock()

# Só é considerada macro confirmada quando o mesmo texto aparece em
# LIMIAR_MACRO+ tickets distintos do índice (nada de "inventar" macro).
try:
    from config import LIMIAR_MACRO  # noqa: F401,E402
except Exception:
    LIMIAR_MACRO = 5
EXTRA_FILE = BASE_DIR / "painel" / "macros_locais.json"
_extra = {"mtime": None, "macros": [], "lock": threading.Lock()}

# Cache das estatísticas (hits de macros) por mtime do índice/cache/macros locais.
_EST_CACHE = {"chave": None, "valor": None, "lock": threading.Lock()}

# Macros só são disparadas pela equipe; ações do cliente (ou de e-mail citado
# do cliente) nunca entram como hit. Mesma fonte que dados.INTERNO.
DOMINIO_EQUIPE = "@faturagil.com.br"

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


def _ler_extras() -> list[dict]:
    """Lê painel/macros_locais.json (lista de macros definidas pelo time)."""
    if not EXTRA_FILE.exists():
        return []
    try:
        dados_extra = json.loads(EXTRA_FILE.read_text(encoding="utf-8")) or []
    except Exception:
        return []
    if not isinstance(dados_extra, list):
        return []
    return [m for m in dados_extra if isinstance(m, dict)]


def catalogo() -> list[dict]:
    """Catálogo base (semeado pelos playbooks) + macros definidas pelo time."""
    with _extra["lock"]:
        try:
            mtime = EXTRA_FILE.stat().st_mtime
        except OSError:
            return list(CATALOGO)
        if _extra["mtime"] != mtime:
            extras = []
            for m in _ler_extras():
                if m.get("id") and m.get("assinaturas"):
                    extras.append({
                        "id": m["id"],
                        "nome": m.get("nome") or m["id"],
                        "assinaturas": m["assinaturas"],
                        "fonte": m.get("fonte") or "Definida pelo time",
                    })
            _extra.update({"mtime": mtime, "macros": extras})
        return list(CATALOGO) + _extra["macros"]


def _limpar_acao(a: dict) -> dict:
    d = parse_date(a.get("createdDate"))
    cb = a.get("createdBy") or {}
    email = str(cb.get("email") or "").strip().lower()
    texto = str(a.get("description") or "").strip()[:MAX_ACAO_CHARS]
    return {
        "id": a.get("id"),
        "data": d.isoformat() if d else "",
        "autor": cb.get("businessName") or cb.get("email") or "",
        "interna": a.get("type") == 1,
        # Sem e-mail não dá para saber: mantém como equipe (não perde hit antigo).
        "equipe": (not email) or email.endswith(DOMINIO_EQUIPE),
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
    from dados import FONTES_CACHE, iter_cache_tickets

    try:
        alvo = int(ticket_id)
    except (TypeError, ValueError):
        return {}
    # buckets primeiro, histórico por último (como antes)
    for t, _origem in iter_cache_tickets(ordem=FONTES_CACHE + ("historico",), por_bucket=True):
        if int(t["id"]) == alvo:
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
        with _EST_CACHE["lock"]:
            _EST_CACHE.update({"chave": None, "valor": None})


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
    from dados import FONTES_CACHE, iter_cache_tickets, load_cache_raw

    if raw is None:
        raw = load_cache_raw()
    out: dict[str, dict] = {}
    for tid, entry in (carregar_indice().get("tickets") or {}).items():
        if isinstance(entry, dict):
            out[str(tid)] = entry
    # dedup=False + por_bucket: mantém exatamente a precedência antiga (a última
    # cópia varrida vencia quando o mesmo ticket aparecia em mais de um bucket).
    for t, _origem in iter_cache_tickets(raw, ordem=FONTES_CACHE, dedup=False, por_bucket=True):
        acoes = _acoes_do_cache(t)
        if not acoes:
            continue
        tid = str(int(t["id"]))
        out[tid] = _entry(tid, meta_do_ticket(t), acoes)
    return out


@lru_cache(maxsize=8192)
def _norm_mapa(texto: str) -> tuple[str, tuple[int, ...]]:
    """Normaliza (sem acento, minúsculas, não-alfanumérico→espaço, espaços
    colapsados) mantendo um mapa posição→posição de volta ao texto original.

    Diferente de ``norm_texto_ascii`` (que quebra o alinhamento), aqui cada
    caractere de saída aponta para o índice do caractere de origem — dá para
    achar o trecho no texto original sem chutar. Cacheado: o mesmo texto é
    normalizado uma vez por assinatura do catálogo.
    """
    out: list[str] = []
    mapa: list[int] = []
    espaco = True  # começa "em espaço" para descartar separadores iniciais
    for idx, ch in enumerate(texto):
        base = "".join(c for c in unicodedata.normalize("NFD", ch)
                       if unicodedata.category(c) != "Mn").lower()
        for b in base:
            if b.isascii() and b.isalnum():
                out.append(b)
                mapa.append(idx)
                espaco = False
            elif not espaco:
                out.append(" ")
                mapa.append(idx)
                espaco = True
    if out and out[-1] == " ":  # descarta separador final
        out.pop()
        mapa.pop()
    return "".join(out), tuple(mapa)


def _trecho(texto: str, assinaturas: list[str]) -> tuple[str, str] | None:
    """Retorna (trecho, assinatura) da primeira ocorrência, ou None.

    Casa ignorando acento/caixa e exigindo **fronteira de palavra**: a assinatura
    tem de aparecer inteira, não como pedaço de uma palavra/expressão maior (evita
    "sugestão 01" bater dentro de "sugestão 012").
    """
    if not texto:
        return None
    norm, mapa = _norm_mapa(texto)
    for sig in assinaturas:
        snorm = _norm_mapa(str(sig))[0].strip()
        if not snorm:
            continue
        m = re.search(r"(?<![a-z0-9])" + re.escape(snorm) + r"(?![a-z0-9])", norm)
        if not m:
            continue
        i = mapa[m.start()] if m.start() < len(mapa) else 0
        ini = max(0, i - 90)
        fim = min(len(texto), i + len(str(sig)) + 320)
        sufixo = "…" if fim < len(texto) else ""
        prefixo = "…" if ini > 0 else ""
        return f"{prefixo}{texto[ini:fim].strip()}{sufixo}", sig
    return None


def ocorrencias(entry: dict, assinaturas: list[str]) -> list[dict]:
    hits = []
    for a in entry.get("acoes") or []:
        # Macro é disparada pela equipe; ação do cliente (ou texto de e-mail do
        # cliente citado) não conta como uso de macro.
        if not a.get("equipe", True):
            continue
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


def catalogo_efetivo(fontes: dict[str, dict]) -> list[dict]:
    """Catálogo semeado/definido + macros aprendidas do próprio dado."""
    return catalogo() + _aprendidas(fontes)


def _macro_hits(fontes: dict[str, dict]) -> list[dict]:
    # Deduplica textos de ação: a mesma macro se repete entre tickets, então
    # casar cada texto único uma vez é muito mais barato que ação por ação.
    unicos: dict[str, dict] = {}
    for tid, entry in fontes.items():
        t = tid
        for a in entry.get("acoes") or []:
            if not a.get("equipe", True):
                continue
            texto = a.get("texto") or ""
            if not texto:
                continue
            norm = _norm_mapa(texto)[0]
            if not norm:
                continue
            slot = unicos.get(norm)
            if slot is None:
                unicos[norm] = slot = {"tids": set(), "data": ""}
            slot["tids"].add(t)
            d = a.get("data") or ""
            if d > slot["data"]:
                slot["data"] = d
    itens = list(unicos.items())
    saida = []
    for macro in catalogo_efetivo(fontes):
        sigs = []
        for sig in macro["assinaturas"]:
            s = _norm_mapa(str(sig))[0]
            if s:
                sigs.append((s, re.compile(r"(?<![a-z0-9])" + re.escape(s) + r"(?![a-z0-9])")))
        if not sigs:
            continue
        por_data: dict[str, str] = {}
        tids: set[str] = set()
        ultima = ""
        for norm, slot in itens:
            for s, pat in sigs:
                if s in norm and pat.search(norm):  # 'in' filtra barato; regex só nos candidatos
                    tids |= slot["tids"]
                    if slot["data"] > ultima:
                        ultima = slot["data"]
                    for t in slot["tids"]:
                        d = por_data.get(t)
                        if not d or slot["data"] > d:
                            por_data[t] = slot["data"]
                    break
        recentes = sorted(tids, key=lambda t: por_data.get(t, ""), reverse=True)
        saida.append({**macro, "tickets": recentes[:10],
                      "tickets_total": len(tids),
                      "recorrente": len(tids) >= LIMIAR_MACRO,
                      "ocorrencias": len(tids), "ultima": ultima[:10]})
    return saida


def _chave_cache_mtime() -> tuple:
    """Chave para invalidar o cache de estatísticas: (arquivo, mtime) de cada fonte."""
    def par(p: Path):
        try:
            return (str(p), p.stat().st_mtime)
        except OSError:
            return (str(p), None)

    return (par(ACOES_FILE), par(CACHE_FILE), par(EXTRA_FILE))


def _estatisticas_impl(raw: dict | None = None) -> dict:
    fontes = coletar_acoes(raw)
    total_acoes = sum(len(e.get("acoes") or []) for e in fontes.values())
    return {
        "catalogo": _macro_hits(fontes),
        "limiar_macro": LIMIAR_MACRO,
        "indice": {
            "tickets_com_acoes": len(fontes),
            "acoes": total_acoes,
            "salvo_em": carregar_indice().get("salvo_em") or "",
        },
    }


def estatisticas(raw: dict | None = None) -> dict:
    """Hits de todas as macros. Sem ``raw`` explícito, usa cache por mtime."""
    if raw is not None:
        return _estatisticas_impl(raw)
    chave = _chave_cache_mtime()
    with _EST_CACHE["lock"]:
        if _EST_CACHE["chave"] == chave and _EST_CACHE["valor"] is not None:
            return _EST_CACHE["valor"]
    valor = _estatisticas_impl(None)
    with _EST_CACHE["lock"]:
        _EST_CACHE["chave"] = chave
        _EST_CACHE["valor"] = valor
    return valor


def analisar_entrada(tid: str, entry: dict, fonte: str,
                     fontes: dict[str, dict] | None = None) -> dict:
    """Valida quais macros (catálogo + aprendidas) aparecem em um ticket.

    ``nao_encontradas`` lista só o catálogo base (playbooks/definidas) — as
    aprendidas são muitas e apareceriam sempre, então ficam de fora desse resumo.
    """
    if fontes is None:
        fontes = coletar_acoes()
    base = catalogo()
    encontradas = []
    for macro in base + _aprendidas(fontes):
        occ = ocorrencias(entry, macro["assinaturas"])
        if occ:
            encontradas.append({
                "id": macro["id"], "nome": macro["nome"], "fonte": macro["fonte"],
                "auto": bool(macro.get("auto")), "ocorrencias": occ, "quantos": len(occ),
            })
    ids_enf = {e["id"] for e in encontradas}
    nao = [m["nome"] for m in base if m["id"] not in ids_enf]
    indice = {}
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


def _fragmentos_repetidos(fontes: dict[str, dict], n: int, limite: int | None = None) -> list[dict]:
    """Frases (sentenças) que aparecem na mesma forma em >= n tickets distintos.

    Considera só ações da equipe (macro é disparada por atendente). Retorna
    candidatos ordenados por nº de tickets, com o texto normalizado e um exemplo
    cru para exibição.
    """
    contagem: dict[str, dict] = {}
    for tid, entry in fontes.items():
        vistos = set()
        for a in entry.get("acoes") or []:
            if not a.get("equipe", True):
                continue
            texto = re.sub(r"\s+", " ", (a.get("texto") or "")).strip()
            partes = re.split(r"(?<=[.;!?])\s+|\n", texto)
            for fragmento in partes:
                norm = normalizar(fragmento).strip(" .;!?")
                if 30 <= len(norm) <= 240 and " " in norm and not norm.isdigit():
                    if norm in vistos:
                        continue
                    vistos.add(norm)
                    info = contagem.setdefault(norm, {"tickets": set(), "exemplo": ""})
                    info["tickets"].add(int(tid))
                    if not info["exemplo"] or len(fragmento.strip()) > len(info["exemplo"]):
                        info["exemplo"] = re.sub(r"\s+", " ", fragmento).strip()
    candidatos = []
    for norm, info in contagem.items():
        ts = sorted(info["tickets"])
        if len(ts) < n:
            continue
        if _ja_coberta(norm):
            continue
        candidatos.append({"texto": norm, "tickets": len(ts), "exemplo": info["exemplo"]})
    candidatos.sort(key=lambda c: (-c["tickets"], c["texto"]))
    return candidatos[:limite] if limite else candidatos


def _texto_repetido_em(n: int, raw: dict | None = None) -> dict:
    """Sugestões de nova macro: frases repetidas em >= n tickets do índice."""
    fontes = coletar_acoes(raw)
    return {"candidatos": _fragmentos_repetidos(fontes, n, limite=12), "fontes": len(fontes)}


# Assinatura/rodapé de e-mail não é macro: filtra trechos com e-mail/URL/telefone
# ou que começam com uma saudação de encerramento (repetem por serem rodapé, não
# por serem conteúdo disparado).
_ASSINATURA_RE = re.compile(r"@|https?|www|\+?\d[\d ()+-]{7,}")
_ASSINATURA_PREFIXO = re.compile(
    r"^(atenciosamente|abraco|abracos|att|at te|grato|obrigado|cordialmente|saudacoes)\b")


def _parece_assinatura(norm: str) -> bool:
    return bool(_ASSINATURA_RE.search(norm) or _ASSINATURA_PREFIXO.match(norm))


def _aprendidas(fontes: dict[str, dict], n: int | None = None,
                limite: int = 25) -> list[dict]:
    """Macros aprendidas do dado: trechos repetidos em >= n tickets viram macros.

    Complementa o catálogo semeado pelos playbooks — sem precisar cadastrar o
    texto real. O nome é o começo do próprio trecho; a assinatura é o texto
    inteiro, casado (sem acento/fronteira) como qualquer macro do catálogo.
    """
    n = n or LIMIAR_MACRO
    saida = []
    for c in _fragmentos_repetidos(fontes, n, limite=limite):
        if _parece_assinatura(c["texto"]):
            continue
        ident = "auto_" + hashlib.md5(c["texto"].encode("utf-8")).hexdigest()[:10]
        exemplo = c["exemplo"]
        saida.append({
            "id": ident,
            "nome": exemplo[:90] + ("…" if len(exemplo) > 90 else ""),
            "assinaturas": [exemplo],
            "fonte": f"Aprendida automaticamente (repetida em {c['tickets']} tickets)",
            "auto": True,
        })
    return saida


def _ja_coberta(frase: str) -> bool:
    """Já existe macro/assinatura que cobre essa frase."""
    for m in catalogo():
        for sig in m.get("assinaturas") or []:
            s = normalizar(sig)
            if s and (s in frase or frase in s):
                return True
    return False


def sugerir_macros(raw: dict | None = None) -> dict:
    """Sugestões de novas macros: texto repetido em >= LIMIAR_MACRO tickets."""
    return _texto_repetido_em(LIMIAR_MACRO, raw)


def listar_definidas() -> dict:
    """Macros definidas pelo time (persistidas em painel/macros_locais.json)."""
    with _extra["lock"]:
        atuais = _ler_extras()
    itens = [
        {
            "id": m.get("id"),
            "nome": m.get("nome") or m.get("id"),
            "assinaturas": m.get("assinaturas") or [],
            "texto_original": m.get("texto_original") or "",
            "fonte": m.get("fonte") or "Definida pelo time",
            "definida_em": m.get("definida_em") or "",
        }
        for m in atuais
        if m.get("id")
    ]
    return {"macros": itens, "total": len(itens)}


def remover_macro(ident: str) -> dict:
    """Remove uma macro definida pelo time pelo seu id."""
    ident = (ident or "").strip()
    if not ident:
        return {"erro": "Informe o id da macro."}
    with _extra["lock"]:
        atuais = _ler_extras()
        restantes = [m for m in atuais if str(m.get("id")) != ident]
        if len(restantes) == len(atuais):
            return {"erro": "Macro não encontrada."}
        EXTRA_FILE.write_text(json.dumps(restantes, ensure_ascii=False, indent=2),
                              encoding="utf-8")
        _extra.update({"mtime": None, "macros": []})
        with _EST_CACHE["lock"]:
            _EST_CACHE.update({"chave": None, "valor": None})
    return {"ok": True, "id": ident, "total": len(restantes)}


def definir_macro(nome: str, assinatura: str, raw: dict | None = None,
                  fonte: str = "Definida pelo time") -> dict:
    """Só define a macro se a assinatura já aparece em >= LIMIAR_MACRO tickets."""
    nome = (nome or "").strip()
    sig = normalizar(assinatura).strip()
    if not sig or len(sig) < 20:
        return {"erro": "A assinatura precisa ser uma frase completa (mín. 20 caracteres)."}
    if not nome:
        return {"erro": "Informe um nome para a macro."}
    if _ja_coberta(sig):
        return {"erro": "Já existe uma macro cobrindo esse texto."}
    fontes = coletar_acoes(raw)
    contagem = 0
    exemplo = ""
    for entry in fontes.values():
        achou = None
        for a in entry.get("acoes") or []:
            texto = (a.get("texto") or "").strip()
            norm = normalizar(texto)
            if sig in norm:
                achou = texto
                break
        if achou is not None:
            contagem += 1
            if len(achou) > len(exemplo):
                exemplo = re.sub(r"\s+", " ", achou)
    n = contagem
    if n < LIMIAR_MACRO:
        return {
            "erro": f"Aparece em só {n} ticket(s). Uma macro só é confirmada com {LIMIAR_MACRO}+;"
                    f" busque no índice o texto exato antes de definir.",
            "tickets": n,
        }
    if _ja_coberta(sig):
        return {"erro": "Já existe uma macro cobrindo esse texto."}
    with _extra["lock"]:
        try:
            atuais = json.loads(EXTRA_FILE.read_text(encoding="utf-8")) if EXTRA_FILE.exists() else []
        except Exception:
            atuais = []
        atuais = atuais if isinstance(atuais, list) else []
        ident = re.sub(r"[^a-z0-9]+", "_", normalizar(nome)).strip("_") or "macro"
        ident = f"{ident}_{int(datetime.now().timestamp())}"
        atuais.append({
            "id": ident,
            "nome": nome,
            "assinaturas": [sig] if sig else [],
            "texto_original": (assinatura or "").strip(),
            "fonte": fonte,
            "definida_em": datetime.now().isoformat(timespec="seconds"),
        })
        EXTRA_FILE.write_text(json.dumps(atuais, ensure_ascii=False, indent=2),
                              encoding="utf-8")
        _extra.update({"mtime": None, "macros": []})
        with _EST_CACHE["lock"]:
            _EST_CACHE.update({"chave": None, "valor": None})
    return {"ok": True, "nome": nome, "id": ident, "tickets": n, "total": len(atuais)}


def candidatos_para_indexar(raw: dict | None = None) -> list[dict]:
    """Tickets do cache/histórico sem ações no índice, priorizando os ativos."""
    from dados import iter_cache_tickets, load_cache_raw

    if raw is None:
        raw = load_cache_raw()
    ja: set[str] = set(carregar_indice().get("tickets") or {})
    vistos: dict[int, dict] = {}
    # Todas as cópias são consideradas (dedup=False) e fica a de menor prioridade —
    # ativos > interagiu_hoje > fora_carga > resolvidos > histórico.
    for t, origem in iter_cache_tickets(raw, ordem=ORDEM_INDEXACAO, dedup=False):
        tid = int(t["id"])
        if str(tid) in ja or _acoes_do_cache(t):
            continue
        item = {"id": tid, "prio": PRIO_FONTE[origem], **meta_do_ticket(t),
                "data": t.get("resolvedIn") or t.get("createdDate") or ""}
        atual = vistos.get(tid)
        if atual is None or item["prio"] < atual["prio"]:
            vistos[tid] = item
    saida = sorted(vistos.values(), key=lambda x: (x["prio"], _inverso(x.get("data"))))
    return saida


def _inverso(iso: str) -> str:
    """Ordenação decrescente de data em string ISO/parcial (inverte comparando o complemento)."""
    return "".join(chr(255 - ord(c)) for c in (iso or ""))
