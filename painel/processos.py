"""Guia de processos do time: playbooks locais com busca simples.

Cada arquivo de ``painel/processos/*.txt`` é um processo. O ``index.json``
define título, times (suporte/dev/implantacao/coordenacao) e tags (assuntos)
de cada arquivo para filtrar. As seções são detectadas por cabeçalhos
``# Título``; arquivos sem cabeçalho viram uma seção única.

A busca é determinística (sem IA): casa as palavras da consulta com o texto
normalizado do processo e devolve só as seções relevantes.
"""

import json
import os
import re

_BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "processos")
_HEADER_RE = re.compile(r"^\s{0,3}#{1,4}\s+(.*)$")
_SEP_RE = re.compile(r"^\s*[-=_*~]{3,}\s*$")
from config import _TIMES_PROCESSOS as _TIMES  # noqa: F401,E402
# _norm/_tokens são alias da fonte única em util.py.
from util import norm_texto as _norm, tokens_simples as _tokens  # noqa: E402

_CACHE = []
_CACHE_CHAVES = None
# Vocabulario topico declarado: tags + titulos do index.json (cache por _CACHE_CHAVES).
_VOCAB = set()
_VOCAB_CHAVE = object()


def _secoes_de(texto, titulo_padrao):
    # Linhas puramente decorativas (----- / =====) não são conteúdo: se ficarem,
    # o texto "cru" aparece cheio de traços no painel.
    linhas = [ln for ln in (texto or "").splitlines() if not _SEP_RE.match(ln)]
    secoes = []
    atual = None
    inicio = []
    for ln in linhas:
        m = _HEADER_RE.match(ln)
        if m:
            if atual is not None:
                secoes.append(atual)
            atual = {"titulo": m.group(1).strip(), "linhas": []}
        elif atual is None:
            if ln.strip():
                inicio.append(ln)
        else:
            atual["linhas"].append(ln)
    if atual is not None:
        secoes.append(atual)
    saida = []
    # Preâmbulo (antes do 1º cabeçalho): só vira seção se tiver texto além do
    # título — senão o título do processo apareceria duplicado como 1ª seção.
    corpo_ini = "\n".join(inicio).strip()
    if corpo_ini and _norm(corpo_ini) != _norm(titulo_padrao):
        primeiro = next((l.strip() for l in inicio if l.strip()), titulo_padrao)
        saida.append({"titulo": primeiro[:80], "texto": corpo_ini})
    for s in secoes:
        corpo = "\n".join(s["linhas"]).strip()
        # Descarta seção sem corpo e seção cujo corpo é só o próprio título.
        if corpo and _norm(corpo) != _norm(s["titulo"]):
            saida.append({"titulo": s["titulo"].strip(), "texto": corpo})
    if not saida:
        saida.append({"titulo": titulo_padrao, "texto": "\n".join(linhas).strip()})
    return saida


def _listar_do_index():
    with open(os.path.join(_BASE, "index.json"), encoding="utf-8") as f:
        index = json.load(f)
    docs = []
    for arquivo, meta in index.items():
        caminho = os.path.join(_BASE, arquivo)
        if not os.path.isfile(caminho):
            continue
        with open(caminho, encoding="utf-8", errors="replace") as f:
            texto = f.read()
        titulo = meta.get("titulo") or arquivo
        times = [t for t in meta.get("times", []) if t in _TIMES]
        tags = meta.get("tags", [])
        docs.append({
            "arquivo": arquivo,
            "titulo": titulo,
            "times": times,
            "tags": tags,
            "secoes": _secoes_de(texto, titulo),
        })
    return docs


def carregar():
    """Carrega os processos com cache por conteúdo (tamanho+mtime)."""
    global _CACHE, _CACHE_CHAVES
    chaves = []
    for arquivo in os.listdir(_BASE):
        if not arquivo.endswith(".txt"):
            continue
        p = os.path.join(_BASE, arquivo)
        st = os.stat(p)
        chaves.append((arquivo, st.st_size, st.st_mtime))
    if chaves != _CACHE_CHAVES:
        _CACHE = _listar_do_index()
        _CACHE_CHAVES = chaves
    return _CACHE


def _vocabulario():
    """Palavras que os próprios playbooks declaram como assunto.

    Entra o que o time rotulou de propósito: as ``tags`` e o ``titulo`` do
    ``index.json`` mais os cabeçalhos de seção (``# Título``) — um cabeçalho
    existe justamente para nomear o assunto daquilo que vem abaixo.

    É o vocabulário **temático** do corpus: "faturamento", "erro", "boleto",
    "nota fiscal", "fornecedor", "e-mail"... Servem para separar assunto de
    conversa comum. Frequência no corpus não resolve isso — "nota" e "boleto"
    aparecem em vários playbooks justamente porque são assuntos centrais, enquanto
    "ainda"/"também" aparecem 1-3 vezes por acaso. Só o index.json e os cabeçalhos
    dizem o que é assunto.
    """
    global _VOCAB, _VOCAB_CHAVE
    if _VOCAB_CHAVE != _CACHE_CHAVES:
        v = set()
        for d in carregar():
            for t in d.get("tags") or []:
                v.update(_norm(t).split())
            v.update(_norm(d.get("titulo") or "").split())
            for s in d.get("secoes") or []:
                v.update(_norm(s.get("titulo") or "").split())
        _VOCAB, _VOCAB_CHAVE = v, _CACHE_CHAVES
    return _VOCAB


def _variantes(t):
    v = {t}
    if len(t) > 3:
        if t.endswith("oes"):
            v.add(t[:-3] + "ao")
        elif t.endswith("ais"):
            v.add(t[:-3] + "al")
        elif t.endswith("eis"):
            v.add(t[:-3] + "el")
        elif t.endswith("ns"):
            v.add(t[:-2] + "m")
        elif t.endswith("es"):
            v.add(t[:-2])
        elif t.endswith("s"):
            v.add(t[:-1])
    return v


def _marca(texto, palavras, tokens):
    """Quantas palavras do texto casam com os termos (prefixo de palavra)."""
    total = 0
    for t in tokens:
        variantes = _variantes(t)
        if any(v in palavras or any(p.startswith(v) for p in palavras) for v in variantes):
            total += 1
    return total


def _palavras_por_secao(d):
    return [_norm(s["titulo"] + " " + s["texto"]).split() for s in d["secoes"]]


# Limite de termos de uma consulta longa (texto colado do chat): só para limitar
# o custo. NÃO se escolhe "os mais raros" — nesse corpus isso descarta o assunto
# ("nota", "faturamento" estão em vários playbooks) e deixa palavra de conversa.
_MAX_TERMS_BUSCA = 40
# Termos de assunto exigidos para sugerir processo com confianca.
_MIN_DISTINTIVOS = 2
# Quanto texto do chat vira consulta (evita estourar o custo da busca).
_MAX_CHARS_CHAT = 4000


def _conjunto_do_doc(d):
    conj = set()
    for palavras in _palavras_por_secao(d):
        conj.update(palavras)
    return conj


def _contagens_termos(docs, tokens):
    """Em quantos documentos cada termo da consulta casa.

    Eh a medida de genericidade: 'erro'/'boleto' casam quase tudo, 'cnab'/'nfse'
    casam um ou dois. Serve para exigir evidencia real e para filtrar o chat colado.
    """
    conjuntos = [_conjunto_do_doc(d) for d in docs]
    return {t: sum(1 for conj in conjuntos if _marca("", conj, {t})) for t in tokens}


def _termos_informativos(tokens, contagens):
    """Limita uma consulta longa (chat colado) ao que pode casar, na ordem do chat.

    Só consulta longa passa por aqui; consulta curta vai inteira (o termo de typo
    precisa chegar ao fuzzy). Para o texto longo:

    1. descarta termo que nao existe em nenhum processo (nome de cliente, número
       de protocolo) — ele nunca casa e só pesa;
    2. se nada existir, devolve os primeiros termos para o fuzzy tentar;
    3. corta no limite de custo, preservando a ordem da conversa.

    NÃO se ordena por raridade: nesse corpus o termo raro costuma ser conversa
    comum ("ontem", "relata") e o assunto ("nota", "faturamento") aparece em
    vários playbooks. O filtro de assunto fica em ``_distintivo``.

    Devolve (uteis, ignorados) na ordem original da consulta.
    """
    if len(tokens) <= _MAX_TERMS_BUSCA:
        # Consulta curta vai inteira — inclusive termo de typo, que precisa
        # chegar ao fuzzy (caso contrário "faturamentoo" nunca casaria).
        return tokens, []
    presentes = [t for t in tokens if contagens.get(t, 0) >= 1]
    if not presentes:
        # Nada do texto existe nos playbooks: deixa os primeiros termos para o
        # fuzzy tentar, em vez de devolver lista vazia sem explicar.
        base = tokens[:_MAX_TERMS_BUSCA]
        resto = set(base)
        return base, [t for t in tokens if t not in resto]
    uteis = presentes[:_MAX_TERMS_BUSCA]
    resto = set(uteis)
    return uteis, [t for t in tokens if t not in resto]


def _distintivo(t, contagens, total_docs):
    """Termo eh de ASSUNTO quando o index.json o declara e nao e palavra corriqueira.

    Duas condicoes, e as duas sao necessarias:

    1. aparece nas tags/titulos de algum processo (o time ja disse que aquilo e
       assunto) — sem isso, "ainda"/"tambem"/"relata" passariam por informativos
       so porque caem em 1-3 playbooks por acaso;
    2. nao esta em mais da metade dos processos — "boleto" esta em 7 de 13, entao
       so ele nao identifica nada.
    """
    if not contagens or total_docs <= 1:
        return True
    if contagens.get(t, 0) > max(1, total_docs // 2):
        return False
    return _marca("", _vocabulario(), {t}) > 0


def _confianca(casados, distintivos):
    """Nivel de evidencia: forte (varios termos de assunto), media (um), fraco (nenhum)."""
    if not casados:
        return "fraco"
    if len(casados) == 1:
        # Uma palavra so nunca vira "siga este processo": o chat pode dizer outra coisa.
        return "media" if distintivos else "fraco"
    if len(distintivos) >= _MIN_DISTINTIVOS:
        return "forte"
    return "media" if distintivos else "fraco"


def _match_exato(d, tokens, contagens=None, total_docs=0):
    palavras_sec = _palavras_por_secao(d)
    conj_doc = set()
    for w in palavras_sec:
        conj_doc.update(w)
    casados = [t for t in tokens if _marca("", conj_doc, {t})]
    if not casados:
        return None
    distintivos = [t for t in casados if _distintivo(t, contagens, total_docs)]
    # Exige pelo menos 2 termos casados (ou todos, se forem menos de 2).
    exigido = min(_MIN_DISTINTIVOS, len(tokens))
    if len(casados) < exigido:
        return None
    casadas = []
    score = len(casados) * 1000
    for s, palavras in zip(d["secoes"], palavras_sec):
        n = _marca("", set(palavras), tokens)
        if n <= 0:
            continue
        # Cabeçalho pesa 3x: a seção certa costuma nomear o assunto no título
        # ("FLUXO PARA ERRO DE FATURAMENTO"), enquanto o corpo de qualquer
        # seção longa engole palavra de conversa ("ainda", "também", "precisa").
        n_tit = _marca("", set(_norm(s["titulo"]).split()), tokens)
        casadas.append((n + 3 * n_tit, n, s))
        score += n * 100 + n_tit * 300
    casadas.sort(key=lambda x: -x[0])
    casadas = [s for _, _, s in casadas]
    if _marca("", set(_norm(d["titulo"]).split()), tokens) == len(tokens):
        score += 5000
    if not casadas:
        casadas = [d["secoes"][0]]
    out = dict(d)
    if len(casadas) != len(d["secoes"]):
        out["secoes"] = casadas
    out["_score"] = score
    out["_termos"] = tokens
    out["_casados"] = casados
    out["_distintivos"] = distintivos
    out["_confianca"] = _confianca(casados, distintivos)
    return out


def _match_fuzzy(d, tokens):
    """Casa por similaridade de palavras (tolera erros de digitacao).

    So considera quando TODOS os termos relevantes (>=4 letras) tem uma palavra
    suficientemente parecida no documento, evitando casar documentos só por um
    termo comum como "erro".
    """
    from difflib import SequenceMatcher

    elegiveis = [t for t in tokens if len(t) >= 4]
    if not elegiveis:
        return None
    conj = set()
    for palavras in _palavras_por_secao(d):
        conj.update(palavras)
    melhor = 0.0
    for t in elegiveis:
        best = 0.0
        for v in _variantes(t):
            for p in conj:
                if abs(len(p) - len(v)) <= 2:
                    r = SequenceMatcher(None, v, p).ratio()
                    if r > best:
                        best = r
        if best < 0.82:
            return None
        melhor = max(melhor, best)
    out = dict(d)
    out["_score"] = int(melhor * 1000)
    out["_termos"] = tokens
    out["_fuzzy"] = True
    # Fuzzy exige TODOS os termos >=4 letras parecidos no doc, mas ainda e
    # aproximacao: entra como evidencia "media", nunca como "forte".
    out["_casados"] = list(tokens)
    out["_distintivos"] = list(elegiveis)
    out["_confianca"] = "media"
    return out


def buscar(q="", time=None, tag=None):
    # Chat colado pode ter milhares de palavras: corta o excesso antes de
    # tokenizar, senão a busca vira lenta sem ganho de evidência.
    if len(q or "") > _MAX_CHARS_CHAT:
        q = q[:_MAX_CHARS_CHAT]
    docs = carregar()
    # Deduplica preservando a ordem: a evidência é "quais termos do caso batem",
    # não quantas vezes a mesma palavra apareceu. Sem isso, um chat que repete
    # "erro" duas vezes casaria sozinho e viraria "evidência forte".
    tokens = list(dict.fromkeys(_tokens(q)))
    time_n = _norm(time) if time else None
    tag_n = _norm(tag) if tag else None

    candidatos = []
    tags_vistas = []
    for d in docs:
        if time_n and time_n not in [_norm(t) for t in d["times"]]:
            continue
        if tag_n and tag_n not in [_norm(t) for t in d["tags"]]:
            continue
        for t in d["tags"]:
            if t not in tags_vistas:
                tags_vistas.append(t)
        candidatos.append(d)

    if not tokens:
        return candidatos, tags_vistas, tokens

    # Mede a genericidade de cada termo no corpus, depois reduz o chat colado
    # aos termos realmente informativos.
    contagens = _contagens_termos(candidatos, tokens)
    uteis, ignorados = _termos_informativos(tokens, contagens)
    total = len(candidatos)
    # Pode haver centenas de palavras num chat: guarda so um pouco, com o total.
    meta_ignor = {"lista": ignorados[:12], "total": len(ignorados)}

    resultados = []
    for d in candidatos:
        out = _match_exato(d, uteis, contagens, total)
        if out:
            out["_ignorados"] = meta_ignor
            resultados.append(out)
    if not resultados:
        for d in candidatos:
            out = _match_fuzzy(d, uteis)
            if out:
                out["_ignorados"] = meta_ignor
                resultados.append(out)
    resultados.sort(key=lambda d: (-d["_score"], d["titulo"]))
    return resultados, tags_vistas, uteis


_ACAO_RE = re.compile(
    r"\b(verifique|verificar|conferir|confira|confere|realize|realizar|informe|informar|"
    r"valide|validar|atualize|atualizar|checar|checagem|envie|enviar|abra|abrir|feche|fechar|"
    r"registre|registrar|encaminhe|encaminhar|solicite|solicitar|execute|executar|questionar|"
    r"questione|aguarde|aguardar|refature|refaturar|reprocesse|reprocessar|reenvie|reenviar|"
    r"teste|testar|corrija|corrigir|ajuste|ajustar|oriente|orientar|explique|explicar|"
    r"combinar|combine|negocie|negociar|priorize|priorizar|libere|liberar|acione|acionar)\b",
    re.I,
)


def _passos_do_texto(texto, limite=40):
    """Extrai passos acionáveis de um texto de playbook (heurística determinística).

    Quebra em sentenças/linhas e mantém as que começam/contêm um verbo de ação
    típico de checklist ou terminam em pergunta (pontos de verificação)."""
    partes = re.split(r"(?<=[.;!?])\s+|\n+", texto or "")
    passos = []
    for p in partes:
        s = " ".join(p.split()).strip(" -•*#\t")
        if len(s) < 15 or len(s) > 220:
            continue
        if not re.search(r"[A-Za-zÀ-ÿ]", s):
            continue
        if _ACAO_RE.search(s) or s.endswith("?"):
            passos.append(s)
    return passos[:limite]


def checklist(q="", time=None, limite=8):
    """Sugere um checklist de encerramento a partir dos playbooks do time.

    Usa a mesma busca do guia (``buscar``) e extrai os passos acionáveis das
    seções que casaram com a consulta (serviço/assunto do ticket)."""
    if not _tokens(q):
        return {"consulta": q or "", "passos": [], "fontes": [], "termos": []}
    docs, _, tokens = buscar(q, time)
    # Só sugere passos com evidência que não seja fraca: um termo genérico do
    # chat não basta para mandar seguir um processo sem ter lido o caso.
    firmes = [d for d in docs if d.get("_confianca") != "fraco"]
    passos = []
    fontes = []
    vistos = set()
    for d in firmes[:3]:
        achou = False
        for s in d["secoes"]:
            for p in _passos_do_texto(s["texto"]):
                chave = _norm(p)
                if chave in vistos:
                    continue
                vistos.add(chave)
                passos.append(p)
                achou = True
                if len(passos) >= limite:
                    break
            if len(passos) >= limite:
                break
        if achou:
            fontes.append(d["titulo"])
        if len(passos) >= limite:
            break
    conf = firmes[0].get("_confianca") if firmes else ""
    aviso = ""
    if not passos and docs:
        aviso = ("Só termos genéricos casaram com estes processos, então nenhum passo "
                 "é sugerido. Use termos concretos (\"erro de faturamento\", \"NFS-e\", "
                 "\"credenciamento\") ou cole o texto do chat na aba Processos.")
    elif conf == "media":
        aviso = "Evidência indireta: confirme no atendimento antes de seguir os passos."
    return {"consulta": q or "", "passos": passos[:limite], "fontes": fontes,
            "termos": tokens, "confianca": conf, "aviso": aviso}