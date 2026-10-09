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
import unicodedata

_BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "processos")
_HEADER_RE = re.compile(r"^\s{0,3}#{1,4}\s+(.*)$")
_TIMES = ("suporte", "dev", "implantacao", "coordenacao")

_CACHE = []
_CACHE_CHAVES = None


def _norm(s):
    s = unicodedata.normalize("NFD", s or "")
    return "".join(c for c in s if unicodedata.category(c) != "Mn").lower()


def _secoes_de(texto, titulo_padrao):
    secoes = []
    atual = None
    inicio = []
    for ln in texto.splitlines():
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
    if inicio or not secoes:
        corpo = "\n".join(inicio).strip()
        if corpo:
            primeiro = next((l.strip() for l in inicio if l.strip()), titulo_padrao)
            secoes.insert(0, {"titulo": primeiro[:80], "linhas": inicio})
    saida = []
    for s in secoes:
        texto = "\n".join(s["linhas"]).strip()
        if texto:
            saida.append({"titulo": s["titulo"].strip(), "texto": texto})
    if not saida:
        saida.append({"titulo": titulo_padrao, "texto": texto.strip()})
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


def _casa(texto_normalizado, tokens):
    return all(t in texto_normalizado for t in tokens)


def buscar(q="", time=None, tag=None):
    docs = carregar()
    tokens = [t for t in _norm(q).split() if t]
    time_n = _norm(time) if time else None
    tag_n = _norm(tag) if tag else None

    resultados = []
    tags_vistas = []
    for d in docs:
        if time_n and time_n not in [_norm(t) for t in d["times"]]:
            continue
        if tag_n and tag_n not in [_norm(t) for t in d["tags"]]:
            continue
        for t in d["tags"]:
            if t not in tags_vistas:
                tags_vistas.append(t)
        if not tokens:
            resultados.append(d)
            continue
        casadas = []
        for s in d["secoes"]:
            alvo = _norm(s["titulo"] + " " + s["texto"])
            if _casa(alvo, tokens):
                casadas.append(s)
        if casadas:
            out = dict(d)
            if len(casadas) != len(d["secoes"]):
                out["secoes"] = casadas
            score = len(casadas) * 100
            if _casa(_norm(d["titulo"]), tokens):
                score += 1000
            out["_score"] = score
            resultados.append(out)
    if tokens:
        resultados.sort(key=lambda d: (-d["_score"], d["titulo"]))
    return resultados, tags_vistas