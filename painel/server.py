#!/usr/bin/env python3
"""Servidor do painel Movidesk.

Uso: clique no atalho da Área de Trabalho (ou rode `python3 painel/server.py`).
Sobe em background se ainda não estiver rodando e abre o navegador.
"""
import json
import logging
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PAINEL_DIR = Path(__file__).resolve().parent
BASE_DIR = PAINEL_DIR.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(PAINEL_DIR))

from config import (  # noqa: E402
    ACTIVE_STATUSES_EXCLUDED,
    OPENROUTER_API_KEY,
    OPENROUTER_MODEL,
    OPENROUTER_MODELS,
    OPENROUTER_URL,
    AGENT_EMAIL,
    AGENT_NAME,
    MERGED_CACHE_HOURS,
    MOVIDESK_KB_URL,
    MOVIDESK_WEB_URL,
    PANEL_PORT,
    SURVEY_CACHE_HOURS,
    SURVEY_QUESTIONS_CACHE_HOURS,
)
from dados import (  # noqa: E402
    build_comparativo,
    build_indicadores,
    build_mesclas,
    build_mapa_horarios,
    build_parecidos,
    build_plantao,
    build_payload,
    build_ranking,
    build_relatorio,
    build_servicos,
    build_sla_vencimentos,
    buscar_historico,
    definir_agente,
    load_cache_raw,
    montar_solucao,
    merge_historico,
    salvar_fila_mescla,
    save_cache_raw,
)
from util import odata_dt as _odata  # noqa: E402

PORT = PANEL_PORT
HOST = "127.0.0.1"
INDEX_FILE = PAINEL_DIR / "index.html"
LOG_FILE = PAINEL_DIR / "server.log"
PID_FILE = PAINEL_DIR / "server.pid"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    filename=LOG_FILE,
    filemode="a",
)
log = logging.getLogger("painel")

# Cache em memoria da busca de artigos (a cota e 10 req/min; digitar no campo nao
# deve consumir uma requisicao por tecla).
_artigos_cache: dict = {}
_artigos_lock = threading.Lock()
ARTIGOS_CACHE_SECONDS = 600


def server_url(path: str = "/") -> str:
    return f"http://{HOST}:{PORT}{path}"


def _cache_vencido(salvo_em, now: datetime, horas: int) -> bool:
    """True quando o carimbo ISO do cache nao existe, e invalido ou venceu."""
    if not salvo_em or horas <= 0:
        return True
    try:
        salvo = datetime.fromisoformat(str(salvo_em))
    except (ValueError, TypeError):
        return True
    return (now - salvo) > timedelta(hours=horas)


def _buscar_mesclas_confirmadas(client, now: datetime, dias: int = 30, max_pages: int = 6) -> dict:
    """Le GET /tickets/merged na janela recente e monta o registro de cache."""
    ate = now.strftime("%Y-%m-%d")
    desde = (now - timedelta(days=int(dias))).strftime("%Y-%m-%d")
    try:
        dados = client.list_merged_tickets(desde, ate, max_pages=int(max_pages))
    except Exception as exc:
        log.warning("Falha ao listar mesclas confirmadas: %s", exc)
        return {"salvo_em": "", "itens": [], "janela": {"desde": desde, "ate": ate}, "erro": str(exc)}
    return {
        "salvo_em": now.isoformat(timespec="seconds"),
        "itens": dados.get("itens") or [],
        "total": dados.get("total"),
        "janela": {"desde": desde, "ate": ate},
    }


def _parse_query(query: str) -> dict:
    from urllib.parse import parse_qs, unquote

    out = {}
    for k, v in parse_qs(query, keep_blank_values=True).items():
        out[unquote(k)] = unquote(v[0]) if v else ""
    return out


def _merge_delta(previous: dict, delta: list[dict], since_iso_ref: str) -> tuple[list[dict], list[dict]]:
    """Aplica o delta (lastUpdate) sobre o cache anterior e devolve (ativos, resolvidos).

    O delta **não** filtra por status: quem aparecer com baseStatus de encerrado
    sai de ``ativos`` e entra em ``resolvidos``. É isso que impede um ticket
    auto-resolvido de ficar preso (e contado) como pendente/encerramento vencido.
    """
    from analyzer import parse_date

    fechados = ACTIVE_STATUSES_EXCLUDED
    ref = parse_date(since_iso_ref)
    ativos: dict[int, dict] = {
        int(t["id"]): t for t in (previous.get("ativos") or []) if t.get("id") is not None
    }
    resolvidos: dict[int, dict] = {
        int(t["id"]): t for t in (previous.get("resolvidos") or []) if t.get("id") is not None
    }
    for t in delta:
        try:
            tid = int(t.get("id"))
        except (TypeError, ValueError):
            continue
        base = t.get("baseStatus") or ""
        if base in fechados:
            ativos.pop(tid, None)
            rd = parse_date(t.get("resolvedIn") or t.get("closedIn"))
            if ref is None or rd is None or rd >= ref:
                resolvidos[tid] = t
        else:
            resolvidos.pop(tid, None)
            ativos[tid] = t
    if ref is not None:
        for tid, r in list(resolvidos.items()):
            rd = parse_date(r.get("resolvedIn") or r.get("closedIn"))
            if rd is not None and rd < ref:
                resolvidos.pop(tid, None)
    return list(ativos.values()), list(resolvidos.values())


def is_running() -> bool:
    try:
        with socket.create_connection((HOST, PORT), timeout=1):
            return True
    except OSError:
        return False


def _meses(desde: datetime, ate: datetime):
    from datetime import timedelta

    ini = desde.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while ini < ate:
        nxt = (ini.replace(day=28) + timedelta(days=4)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        yield ini, min(nxt, ate)
        ini = nxt


_historico_job: dict = {"ativo": False}
_historico_lock = threading.Lock()
_STATUS_HISTORICO = ACTIVE_STATUSES_EXCLUDED

_acoes_job: dict = {"ativo": False}
_acoes_lock = threading.Lock()


def _indexar_historico(desde: datetime, ate: datetime) -> None:
    """Indexa, em background, todos os tickets resolvidos no intervalo [desde, ate),
    consultando as rotas /tickets e /tickets/past mes a mes (chama o RateLimiter)."""
    from dados import merge_historico

    try:
        from api_client import MovideskClient

        client = MovideskClient()
    except Exception as exc:
        with _historico_lock:
            _historico_job.update({"ativo": False, "erro": str(exc)})
        log.exception("Falha ao iniciar indexacao")
        return

    janelas = list(_meses(desde, ate))
    processados = 0
    erros = 0
    for i, (ini, fim) in enumerate(janelas, 1):
        with _historico_lock:
            if not _historico_job.get("ativo"):
                log.info("Indexacao cancelada")
                return
            _historico_job.update({
                "mes": i, "meses": len(janelas),
                "janela": f"{ini.strftime('%m/%Y')}",
                "processados": processados, "erros": erros,
            })
        try:
            lote = client.list_tickets_range(
                _odata(ini), _odata(fim), campo="resolvedIn", status=_STATUS_HISTORICO
            )
            total = merge_historico(lote)
            processados += len(lote)
            with _historico_lock:
                _historico_job.update({"processados": processados, "no_indice": total})
        except Exception as exc:
            erros += 1
            log.warning("Erro na janela %s: %s", ini.strftime("%m/%Y"), exc)

    with _historico_lock:
        _historico_job.update({
            "ativo": False, "erro": None, "processados": processados,
            "erros": erros, "terminado_em": datetime.now().isoformat(timespec="seconds"),
        })
    log.info("Indexacao concluida: %d tickets (%d erros)", processados, erros)


def _iniciar_indexacao(desde: datetime, ate: datetime) -> dict:
    with _historico_lock:
        if _historico_job.get("ativo"):
            return {"erro": "Ja existe uma indexacao em andamento."}
        _historico_job.clear()
        _historico_job.update({
            "ativo": True, "erro": None, "desde": desde.strftime("%Y-%m-%d"),
            "ate": ate.strftime("%Y-%m-%d"), "mes": 0, "meses": 0,
            "processados": 0, "erros": 0, "no_indice": 0,
            "iniciado_em": datetime.now().isoformat(timespec="seconds"),
        })
    threading.Thread(target=_indexar_historico, args=(desde, ate), daemon=True).start()
    return {"ok": True, "desde": desde.strftime("%Y-%m-%d"), "ate": ate.strftime("%Y-%m-%d")}


def _flush_acoes(lote: dict) -> None:
    from macros import salvar_lote

    salvar_lote(lote)


def _indexar_acoes() -> None:
    """Busca as ações de cada ticket ainda sem registro no índice de macros
    (1 requisição por ticket, respeitando o limite de 10 req/min)."""
    from macros import candidatos_para_indexar, registrar_acoes

    try:
        from api_client import MovideskClient

        client = MovideskClient()
    except Exception as exc:
        with _acoes_lock:
            _acoes_job.update({"ativo": False, "erro": str(exc)})
        log.exception("Falha ao iniciar indexacao de acoes")
        return

    try:
        candidatos = candidatos_para_indexar()
    except Exception as exc:
        with _acoes_lock:
            _acoes_job.update({"ativo": False, "erro": str(exc)})
        return

    total = len(candidatos)
    processados = erros = 0
    lote: dict[str, dict] = {}
    with _acoes_lock:
        _acoes_job.update({"ativo": True, "total": total, "processados": 0,
                           "erros": 0, "pos": 0, "atual": None, "erro": None})
    for pos, c in enumerate(candidatos, 1):
        with _acoes_lock:
            if not _acoes_job.get("ativo"):
                if lote:
                    _flush_acoes(lote)
                log.info("Indexacao de acoes cancelada")
                return
            _acoes_job.update({"pos": pos, "atual": c["id"], "processados": processados,
                               "erros": erros})
        try:
            acts = client.get_ticket_actions(c["id"])
            entry = registrar_acoes(
                c["id"], acts,
                meta={"cliente": c["cliente"], "assunto": c["assunto"]},
                salvar=False,
            )
            lote[str(c["id"])] = entry
            processados += 1
            if len(lote) >= 20:
                _flush_acoes(lote)
                lote = {}
        except Exception as exc:
            erros += 1
            log.warning("Erro ao indexar acoes do ticket %s: %s", c["id"], exc)
    if lote:
        _flush_acoes(lote)
    with _acoes_lock:
        _acoes_job.update({"ativo": False, "pos": total, "atual": None,
                           "processados": processados, "erros": erros,
                           "terminado_em": datetime.now().isoformat(timespec="seconds")})
    log.info("Indexacao de acoes concluida: %d (%d erros)", processados, erros)


def _iniciar_indexacao_acoes() -> dict:
    with _acoes_lock:
        if _acoes_job.get("ativo"):
            return {"erro": "Ja existe uma indexacao de acoes em andamento."}
        _acoes_job.clear()
        _acoes_job.update({
            "ativo": True, "erro": None, "total": 0, "processados": 0,
            "erros": 0, "pos": 0, "atual": None,
            "iniciado_em": datetime.now().isoformat(timespec="seconds"),
        })
    threading.Thread(target=_indexar_acoes, daemon=True).start()
    return {"ok": True}


def open_browser() -> None:
    threading.Timer(1.0, lambda: webbrowser.open(server_url())).start()


def spawn_daemon() -> None:
    log_file = open(LOG_FILE, "ab")
    subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "--daemon"],
        cwd=str(BASE_DIR),
        stdout=log_file,
        stderr=log_file,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
    )
    log_file.close()


class Handler(BaseHTTPRequestHandler):
    server_version = "MovideskPainel/1.0"

    def log_message(self, fmt, *args):
        log.debug("%s - %s", self.address_string(), fmt % args)

    def _send(self, code: int, body: bytes, content_type: str = "application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, code: int = 200):
        self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"))

    def _origem_ok(self) -> bool:
        """Defesa contra CSRF e DNS rebinding.

        O painel só é usado por 127.0.0.1/localhost. Uma página externa aberta no
        navegador não pode disparar POSTs aqui (nem consumir a cota de 10 req/min
        da API) porque o navegador manda ``Origin``/``Sec-Fetch-Site`` de outro
        site. Navegação normal do próprio painel não manda esses cabeçalhos, então
        continua passando.
        """
        locais = {"127.0.0.1", "localhost", "::1"}

        def host_local(valor: str) -> bool:
            valor = (valor or "").strip()
            if not valor:
                return True
            try:
                from urllib.parse import urlparse

                partes = urlparse(valor if "//" in valor else "//" + valor)
            except ValueError:
                return False
            nome = (partes.hostname or "").lower()
            if nome and nome not in locais:
                return False
            if partes.port is not None and partes.port != PORT:
                return False
            return True

        sfs = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if sfs and sfs not in ("same-origin", "none"):
            return False
        return host_local(self.headers.get("Host")) and host_local(self.headers.get("Origin"))

    def do_GET(self):
        if not self._origem_ok():
            self._send(403, b"origem nao permitida", "text/plain; charset=utf-8")
            return
        path, _, query = self.path.partition("?")
        params = _parse_query(query)
        if path.startswith("/static/"):
            self._static(path)
            return
        rotas = {
            "/": self._pagina,
            "/index.html": self._pagina,
            "/health": self._health,
            "/api/agentes": self._agentes_rota,
            "/api/historico": self._historico,
            "/api/mapa-horarios": self._mapa_horarios,
            "/api/macros": self._macros,
            "/api/macros/validar": self._macro_validar,
            "/api/macros/buscar": self._macro_buscar,
            "/api/macros/sugerir": self._macro_sugerir,
            "/api/macros/listar-definidas": self._macro_listar,
            "/api/macros/status": self._macro_status,
            "/api/parecidos": self._parecidos,
            "/api/plantao": self._plantao_get,
            "/api/mesclas": self._mesclas_get,
            "/api/acoes": self._acoes_rota,
            "/api/servicos": self._servicos_rota,
            "/api/ranking": self._ranking_rota,
            "/api/indicadores": self._indicadores_rota,
            "/api/processos": self._processos_rota,
            "/api/checklist": self._checklist_rota,
            "/api/comparativo": self._comparativo_rota,
            "/api/relatorio": self._relatorio_rota,
            "/api/sla": self._sla_rota,
            "/api/solucao": self._solucao,
            "/api/historico/status": self._historico_status_rota,
            "/api/dados": self._dados_rota,
            "/api/agenda": self._agenda_rota,
            "/api/artigos": self._artigos_get,
            "/api/ia/cota": self._ia_cota,
            "/api/busca-semantica": self._busca_semantica,
            "/api/ia/feedback/stats": self._ia_feedback_stats,
            "/api/ia/casos": self._ia_casos_get,
            "/api/ticket-html": self._ticket_html,
            "/api/ticket-anexos": self._ticket_anexos,
            "/api/anexo": self._anexo,
        }
        rota = rotas.get(path)
        if rota is None:
            self._send(404, b"nao encontrado", "text/plain; charset=utf-8")
            return
        rota(params)

    # --- Rotas GET ---
    def _pagina(self, params: dict):
        if not INDEX_FILE.exists():
            self._send(404, b"index.html nao encontrado", "text/plain; charset=utf-8")
            return
        self._send(200, INDEX_FILE.read_bytes(), "text/html; charset=utf-8")

    def _static(self, path: str):
        import os
        rel = path[len("/static/"):].lstrip("/")
        base = (PAINEL_DIR / "static").resolve()
        alvo = (base / rel).resolve()
        if not str(alvo).startswith(str(base)) or not alvo.is_file():
            self._send(404, b"nao encontrado", "text/plain; charset=utf-8")
            return
        if alvo.suffix == ".css":
            ctype = "text/css; charset=utf-8"
        elif alvo.suffix == ".js":
            ctype = "application/javascript; charset=utf-8"
        else:
            ctype = "application/octet-stream"
        self._send(200, alvo.read_bytes(), ctype)

    def _health(self, params: dict):
        self._json({"status": "ok"})

    def _agentes_rota(self, params: dict):
        self._agentes()

    def _historico(self, params: dict):
        definir_agente(params.get("agente") or "")
        self._json(buscar_historico(params.get("q") or ""))

    def _mapa_horarios(self, params: dict):
        self._json(build_mapa_horarios(params.get("agente") or ""))

    def _macros(self, params: dict):
        from macros import estatisticas

        payload = estatisticas()
        with _acoes_lock:
            payload["job"] = dict(_acoes_job)
        self._json(payload)

    def _macro_buscar(self, params: dict):
        from macros import buscar

        q = (params.get("q") or "").strip()
        if not q:
            self._json({"erro": "informe q (trecho para buscar)"}, code=400)
            return
        self._json(buscar(q))

    def _macro_sugerir(self, params: dict):
        from macros import sugerir_macros

        self._json(sugerir_macros())

    def _macro_definir(self, params: dict):
        from macros import definir_macro

        try:
            corpo = self._read_body()
        except Exception:
            corpo = {}
        self._json(definir_macro(corpo.get("nome") or "", corpo.get("assinatura") or ""))

    def _macro_listar(self, params: dict):
        from macros import listar_definidas

        self._json(listar_definidas())

    def _macro_status(self, params: dict):
        from macros import candidatos_para_indexar, carregar_indice

        with _acoes_lock:
            job = dict(_acoes_job)
        indice = carregar_indice()
        try:
            restantes = len(candidatos_para_indexar())
        except Exception:
            restantes = None
        self._json({
            "job": job,
            "tickets_indexados": len(indice.get("tickets") or {}),
            "salvo_em": indice.get("salvo_em") or "",
            "restantes": restantes,
        })

    def _parecidos(self, params: dict):
        definir_agente(params.get("agente") or "")
        try:
            tid = int(params.get("id") or "")
        except (TypeError, ValueError):
            tid = 0
        self._json(build_parecidos(tid, (params.get("modo") or "parecidos").lower()))

    def _plantao_get(self, params: dict):
        self._json(build_plantao(params.get("data") or None))

    def _mesclas_get(self, params: dict):
        escopo = params.get("escopo") or "atual"
        agente = (params.get("agente") or AGENT_EMAIL).lower()
        self._json(build_mesclas(escopo, agente))

    def _artigos_get(self, params: dict):
        """Busca na base de conhecimento oficial (GET /kb/article)."""
        from api_client import MovideskClient

        q = (params.get("q") or "").strip()
        if not q:
            self._json({"erro": "informe q (termo para buscar na base de conhecimento)"}, code=400)
            return
        try:
            pagina = max(1, int(params.get("pagina") or 1))
        except ValueError:
            pagina = 1
        try:
            tamanho = min(50, max(1, int(params.get("tamanho") or 20)))
        except ValueError:
            tamanho = 20
        chave = (q.lower(), pagina, tamanho)
        agora = time.time()
        with _artigos_lock:
            guardado = _artigos_cache.get(chave)
        if guardado and agora - guardado[0] < ARTIGOS_CACHE_SECONDS:
            self._json({**guardado[1], "cache": True})
            return
        try:
            resultado = MovideskClient().search_articles(q, page=pagina, page_size=tamanho)
        except Exception as exc:
            log.exception("Falha ao buscar artigos da base de conhecimento")
            self._json({"erro": str(exc)}, code=502)
            return
        for item in resultado.get("itens") or []:
            if isinstance(item, dict) and item.get("id"):
                item["url"] = f"{MOVIDESK_KB_URL.rstrip('/')}/kb/article/{item['id']}"
        with _artigos_lock:
            if len(_artigos_cache) > 100:
                _artigos_cache.clear()
            _artigos_cache[chave] = (agora, resultado)
        self._json({**resultado, "cache": False})

    def _acoes_rota(self, params: dict):
        self._acoes(params.get("id") or "")

    def _servicos_rota(self, params: dict):
        self._json(build_servicos())

    def _ranking_rota(self, params: dict):
        incluir_dev = (params.get("dev") or "").lower() in ("1", "true", "sim", "on")
        try:
            periodo = int(params.get("periodo") or 90)
        except (TypeError, ValueError):
            periodo = 90
        if periodo not in (0, 7, 30, 90):
            periodo = 90
        agente = (params.get("agente") or "").lower()
        papel_filtro = None
        if agente and agente != "*":
            from config import papel_agente
            papel_filtro = papel_agente(agente)
            if papel_filtro == "outros":
                papel_filtro = None
        self._json(build_ranking(incluir_dev=incluir_dev, periodo_dias=periodo,
                                papel_filtro=papel_filtro))

    def _indicadores_rota(self, params: dict):
        # ``agente`` escopa fornecedor/implantação/reclamações na carga do perfil
        # selecionado; vazio/"*" mantém o comportamento de time inteiro.
        self._json(build_indicadores(params.get("agente") or ""))

    def _processos_rota(self, params: dict):
        import processos

        q = params.get("q") or ""
        time = params.get("time") or None
        tag = params.get("tag") or None
        agente = params.get("agente") or ""
        papel_time = None
        if not time and agente and agente != "*":
            from config import time_do_agente

            papel_time = time_do_agente(agente)
        docs, tags, termos = processos.buscar(q, time or papel_time, tag)
        self._json({
            "processos": docs,
            "times": list(processos._TIMES),
            "tags": tags,
            "termos": termos,
            "total": len(processos.carregar()),
            "texto_truncado": len(q) > processos._MAX_CHARS_CHAT,
            "restrito_por_papel": papel_time or "",
        })

    def _checklist_rota(self, params: dict):
        import processos

        q = params.get("q") or params.get("servico") or ""
        time = params.get("time") or None
        self._json(processos.checklist(q, time))

    def _comparativo_rota(self, params: dict):
        definir_agente(params.get("agente") or "")
        self._json(build_comparativo())

    def _relatorio_rota(self, params: dict):
        self._json(build_relatorio(params.get("dias") or 30, params.get("agente")))

    def _sla_rota(self, params: dict):
        self._json(build_sla_vencimentos(params.get("agente") or ""))

    def _historico_status_rota(self, params: dict):
        self._historico_status()

    def _dados_rota(self, params: dict):
        from dados import load_cache_payload

        agente = (params.get("agente") or AGENT_EMAIL).lower()
        payload = load_cache_payload(agente)
        if payload is None:
            self._json({"vazio": True, "mensagem": "Sem dados. Clique em Consultar."})
            return
        payload["origem"] = "cache"
        self._json(payload)

    def _agenda_rota(self, params: dict):
        """Reabre a agenda em outro período (dia anterior / hoje até o momento)
        sem refazer a consulta da API: só recálculo local a partir do cache."""
        from dados import build_agenda_from_cache

        agente = (params.get("agente") or AGENT_EMAIL).lower()
        modo = (params.get("periodo") or params.get("modo") or "anterior").lower()
        agenda = build_agenda_from_cache(agente, modo=modo)
        if agenda is None:
            self._json({"vazio": True, "mensagem": "Sem dados. Clique em Consultar."})
            return
        self._json({"agenda": agenda, "periodo": agenda.get("periodo", {})})

    def _acoes(self, ticket_id: str):
        try:
            tid = int(ticket_id)
        except (TypeError, ValueError):
            self._json({"erro": "id invalido"}, code=400)
            return
        try:
            from api_client import MovideskClient
            from dados import guia_do_ticket, montar_cronologia, ticket_por_id

            acts = MovideskClient().get_ticket_actions(tid)
            self._registrar_no_indice(tid, acts)
            crono = montar_cronologia(acts)
            bruto = ticket_por_id(tid)
            guia = guia_do_ticket(bruto) if bruto else None
            self._json({"ticket": tid, "acoes": crono["itens"], "resumo": crono["resumo"],
                        "guia": guia})
        except Exception as exc:
            log.exception("Falha ao buscar acoes do ticket %s", ticket_id)
            self._json({"erro": str(exc)}, code=502)

    @staticmethod
    def _registrar_no_indice(tid: int, acts: list[dict], meta: dict | None = None) -> dict | None:
        """Guarda as ações buscadas ao vivo no índice de macros. Retorna a entry (ou None)."""
        if not acts:
            return None
        try:
            from macros import meta_local, registrar_acoes

            m = meta or {}
            if not m.get("cliente"):
                m = meta_local(tid)
            return registrar_acoes(tid, acts, meta=m)
        except Exception as exc:
            log.warning("Falha ao registrar acoes do ticket %s no indice: %s", tid, exc)
            return None

    def _macro_validar(self, params: dict):
        """Confere se as macros do catálogo aparecem nas ações de um ticket.

        Padrão: busca ao vivo (1 requisição) e atualiza o índice; use vivo=0 para
        responder só com o que já está no cache/índice.
        """
        try:
            tid = int(params.get("id") or "")
        except (TypeError, ValueError):
            self._json({"erro": "id invalido"}, code=400)
            return
        vivo = (params.get("vivo") or "1").lower() not in ("0", "false", "nao", "não")
        try:
            from macros import analisar_entrada, coletar_acoes

            local = coletar_acoes().get(str(tid))
            entry, fonte = None, None
            if local and not vivo:
                entry, fonte = local, "cache"
            else:
                try:
                    from api_client import MovideskClient

                    acts = MovideskClient().get_ticket_actions(tid)
                    if acts:
                        meta = {
                            "cliente": (local or {}).get("cliente") or "",
                            "assunto": (local or {}).get("assunto") or "",
                        }
                        entry = self._registrar_no_indice(tid, acts, meta)
                        fonte = "vivo"
                        if entry is None:
                            from macros import _limpar_acao

                            entry = {"cliente": meta["cliente"], "assunto": meta["assunto"],
                                     "acoes": [_limpar_acao(a) for a in acts]}
                    elif local:
                        entry, fonte = local, "cache"
                    else:
                        entry, fonte = {"acoes": [], "cliente": "", "assunto": ""}, "vivo"
                except Exception as exc:
                    if local:
                        entry, fonte = local, "cache"
                    else:
                        self._json({"erro": str(exc)}, code=502)
                        return
            self._json(analisar_entrada(str(tid), entry, fonte or ""))
        except Exception as exc:
            log.exception("Falha ao validar macros do ticket %s", params.get("id"))
            self._json({"erro": str(exc)}, code=502)

    def _solucao(self, params: dict):
        from dados import load_cache_raw, montar_solucao, pertence_ao_escopo

        definir_agente(params.get("agente") or "")
        ticket_id = params.get("id") or ""
        try:
            tid = int(ticket_id)
        except (TypeError, ValueError):
            self._json({"erro": "id invalido"}, code=400)
            return
        if not pertence_ao_escopo(tid):
            self._json({"erro": "Tíquete fora do perfil do atendente selecionado."}, code=403)
            return
        raw = load_cache_raw()
        cache = raw.get("solucoes") if isinstance(raw.get("solucoes"), dict) else {}
        key = str(tid)
        if key in cache:
            self._json(cache[key])
            return
        try:
            from api_client import MovideskClient

            acts = MovideskClient().get_ticket_actions(tid)
            self._registrar_no_indice(tid, acts)
            sol = montar_solucao(acts)
            sol["ticket"] = tid
            cache[key] = sol
            raw["solucoes"] = cache
            save_cache_raw(raw)
            self._json(sol)
        except Exception as exc:
            log.exception("Falha ao montar solucao do ticket %s", ticket_id)
            self._json({"erro": str(exc)}, code=502)

    def _ticket_html(self, params: dict):
        try:
            tid = int(params.get("id") or "")
        except (TypeError, ValueError):
            self._json({"erro": "id invalido"}, code=400)
            return
        try:
            from api_client import MovideskClient

            acts = MovideskClient().get_ticket_html_description(tid)
            self._json({"ticket": tid, "acoes": acts})
        except Exception as exc:
            log.exception("Falha ao buscar HTML do ticket %s", tid)
            self._json({"erro": str(exc)}, code=502)

    def _ticket_anexos(self, params: dict):
        try:
            tid = int(params.get("id") or "")
        except (TypeError, ValueError):
            self._json({"erro": "id invalido"}, code=400)
            return
        try:
            from api_client import MovideskClient

            anexos = MovideskClient().get_ticket_attachments(tid)
            self._json({"ticket": tid, "anexos": anexos})
        except Exception as exc:
            log.exception("Falha ao listar anexos do ticket %s", tid)
            self._json({"erro": str(exc)}, code=502)

    def _anexo(self, params: dict):
        file_hash = (params.get("id") or params.get("hash") or "").strip()
        if not file_hash:
            self._json({"erro": "hash do anexo vazio"}, code=400)
            return
        try:
            from api_client import MovideskClient

            content = MovideskClient().download_attachment(file_hash)
            self._send(200, content, "application/octet-stream")
        except Exception as exc:
            log.exception("Falha ao baixar anexo %s", file_hash)
            self._json({"erro": str(exc)}, code=502)

    def _historico_completo(self, params: dict | None = None):
        from datetime import timedelta

        params = params or {}
        desde_s = (params.get("desde") or "").strip()
        ate_s = (params.get("ate") or "").strip()
        try:
            desde = datetime.fromisoformat(desde_s) if desde_s else datetime.now() - timedelta(days=365)
            ate = (datetime.fromisoformat(ate_s) + timedelta(days=1)) if ate_s else datetime.now() + timedelta(days=1)
        except ValueError:
            self._json({"erro": "data invalida (use AAAA-MM-DD)"}, code=400)
            return
        if desde >= ate:
            self._json({"erro": "a data inicial deve ser anterior a final"}, code=400)
            return
        self._json(_iniciar_indexacao(desde, ate))

    def _historico_status(self):
        from dados import historico_cobertura

        with _historico_lock:
            job = dict(_historico_job)
        self._json({"job": job, "cobertura": historico_cobertura()})

    def _pesquisa(self, params: dict):
        from datetime import timedelta

        from dados import load_cache_raw, save_cache_raw

        desde_s = (params.get("desde") or "").strip()
        ate_s = (params.get("ate") or "").strip()
        try:
            since_dt = datetime.fromisoformat(desde_s) if desde_s else datetime.now() - timedelta(days=365)
            until_dt = (datetime.fromisoformat(ate_s) + timedelta(days=1)) if ate_s else None
        except ValueError:
            self._json({"erro": "data invalida (use AAAA-MM-DD)"}, code=400)
            return
        try:
            from api_client import MovideskClient

            respostas = MovideskClient().list_survey_responses(
                since_dt.strftime("%Y-%m-%dT00:00:00"),
                until_dt.strftime("%Y-%m-%dT00:00:00") if until_dt else None,
            )
            raw = load_cache_raw()
            raw["survey"] = respostas
            raw["survey_salvo_em"] = datetime.now().isoformat(timespec="seconds")
            raw["survey_periodo"] = {
                "desde": since_dt.strftime("%Y-%m-%d"),
                "ate": (until_dt - timedelta(days=1)).strftime("%Y-%m-%d") if until_dt else "",
            }
            save_cache_raw(raw)
            self._json({"ok": True, "total": len(respostas), **raw["survey_periodo"]})
        except Exception as exc:
            log.exception("Falha ao buscar pesquisa")
            self._json({"erro": str(exc)}, code=502)

    def _agentes(self):
        from config import PLANTAO_ORDEM, papel_agente

        raw = load_cache_raw()
        agentes = raw.get("agentes") if isinstance(raw.get("agentes"), list) else []
        if not agentes:
            try:
                from api_client import MovideskClient

                agentes = MovideskClient().list_agents()
                raw["agentes"] = agentes
                save_cache_raw(raw)
            except Exception as exc:
                log.warning("Falha ao listar atendentes: %s", exc)
        if AGENT_EMAIL and not any(a.get("email") == AGENT_EMAIL.lower() for a in agentes):
            agentes = [{"nome": AGENT_NAME or AGENT_EMAIL, "email": AGENT_EMAIL.lower()}] + agentes
        ordem_papel = {"suporte": 0, "dev": 1, "outros": 2}
        agentes = [
            {**a, "papel": papel_agente(a.get("email"))}
            for a in agentes
        ]
        agentes.sort(key=lambda a: (ordem_papel.get(a["papel"], 3), (a.get("nome") or "").lower()))
        # Atendentes presentes nos tickets do cache mas ausentes na lista da API.
        from dados import iter_cache_tickets

        emails_cache = set()
        try:
            raw2 = load_cache_raw()
            for t, _ in iter_cache_tickets(raw2, por_bucket=True):
                ow = ((t.get("owner") or {}).get("email") or "").lower()
                if ow:
                    emails_cache.add(ow)
        except Exception:
            pass
        listados = { (a.get("email") or "").lower() for a in agentes }
        nao_listados = sorted(e for e in emails_cache if e not in listados)
        self._json({
            "agentes": agentes,
            "default": AGENT_EMAIL.lower() or "*",
            "agente_nome": AGENT_NAME,
            "plantao_ordem": PLANTAO_ORDEM,
            "nao_listados": nao_listados,
        })

    def do_POST(self):
        if not self._origem_ok():
            self._send(403, b"origem nao permitida", "text/plain; charset=utf-8")
            return
        path, _, query = self.path.partition("?")
        params = _parse_query(query)
        rotas = {
            "/api/consultar": self._consultar_post,
            "/api/historico/completo": self._historico_completo,
            "/api/macros/indexar": self._macro_indexar,
            "/api/macros/definir": self._macro_definir,
            "/api/macros/remover": self._macro_remover,
            "/api/pesquisa": self._pesquisa,
            "/api/plantao": self._plantao_post,
            "/api/mesclas": self._mesclas_post,
            "/api/ia": self._ia_post,
            "/api/ia/feedback": self._ia_feedback,
            "/api/ia/casos": self._ia_casos_post,
        }
        rota = rotas.get(path)
        if rota is None:
            self._send(404, b"nao encontrado", "text/plain; charset=utf-8")
            return
        rota(params)

    # --- Rotas POST ---
    def _consultar_post(self, params: dict):
        agente = (params.get("agente") or AGENT_EMAIL).lower()
        self._consultar(agente=agente, forcar="forcar" in params)

    def _macro_indexar(self, params: dict):
        body = self._read_body()
        if body.get("parar"):
            with _acoes_lock:
                _acoes_job["ativo"] = False
                job = dict(_acoes_job)
            self._json({"ok": True, "parando": True, "job": job})
            return
        self._json(_iniciar_indexacao_acoes())

    def _macro_remover(self, params: dict):
        from macros import remover_macro

        try:
            corpo = self._read_body()
        except Exception:
            corpo = {}
        self._json(remover_macro(corpo.get("id") or ""))

    def _plantao_post(self, params: dict):
        self._salvar_plantao()

    def _mesclas_post(self, params: dict):
        from dados import salvar_fila_mescla

        body = self._read_body()
        chave = str(body.get("chave") or "").strip()
        if not chave:
            self._json({"erro": "chave obrigatoria"}, code=400)
            return
        self._json({"fila": salvar_fila_mescla(chave, str(body.get("status") or ""))})

    def _ia_post(self, params: dict):
        body = self._read_body()
        texto = (body.get("texto") or "").strip()
        historico = body.get("historico") or []
        ticket_id = body.get("ticket_id")
        comando = (body.get("comando") or "").strip()
        stream = bool(body.get("stream"))
        arquivo_base64 = body.get("arquivo_base64")
        arquivo_nome = (body.get("arquivo_nome") or "").strip()
        caminho_pdf = (body.get("caminho_pdf") or "").strip()
        if not texto and not ticket_id and not arquivo_base64 and not caminho_pdf:
            self._json({"erro": "informe texto"}, code=400)
            return
        if not OPENROUTER_API_KEY:
            self._json({"erro": "OPENROUTER_API_KEY nao configurada"}, code=400)
            return
        import hashlib, json
        from pathlib import Path
        cache_path = Path(__file__).resolve().parent.parent / "cache" / "ia_cache.json"
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
        except Exception:
            cache = {}
        if not isinstance(cache, dict):
            cache = {}
        chave = hashlib.sha256((texto + "|" + (body.get("historico") and str(body.get("historico")) or "")).encode("utf-8")).hexdigest()
        if chave in cache:
            c = cache[chave]
            self._json({"ok": True, "resposta": c.get("resposta", ""), "modelo": c.get("modelo"), "artigos": c.get("artigos", []), "macros": c.get("macros", []), "intencao": c.get("intencao"), "resposta_id": c.get("resposta_id"), "fontes": c.get("fontes", []), "cache": True})
            return
        import uuid
        resposta_id = "resp-" + uuid.uuid4().hex[:12]
        fontes_utilizadas = []
        low_texto = (texto or "").lower()
        identidade = any(w in low_texto for w in (
            "qual empresa", "qual a empresa", "voce responde", "você responde",
            "voce e um", "você é um", "quem e voce", "quem é você",
            "chatbot", "assistente", "o que voce faz", "o que você faz",
            "com base em que", "para que serve", "qual sua funcao", "qual sua função",
            "voce e da", "você é da",
        ))
        # Contexto do ticket selecionado (se houver) + extrai IDs do texto
        contexto_ticket = ""
        ids_no_texto = []
        try:
            import re
            for m in re.finditer(r"(?:#|ticket\s*)(\d{4,})", texto or "", re.I):
                ids_no_texto.append(int(m.group(1)))
        except Exception:
            pass
        if ticket_id:
            try:
                ids_no_texto.insert(0, int(ticket_id))
            except (TypeError, ValueError):
                pass
        vistos = set()
        ids_unicos = []
        for tid in ids_no_texto:
            if tid in vistos:
                continue
            vistos.add(tid)
            ids_unicos.append(tid)
        if ids_unicos:
            try:
                from dados import ticket_por_id
                for tid in ids_unicos[:3]:
                    t = ticket_por_id(tid)
                    if t:
                        contexto_ticket += ("\n## TICKET #%d\nAssunto: %s\nServico: %s\nStatus: %s\nJustificativa: %s\nOwner: %s\n" % (
                            tid,
                            (t.get("subject") or "")[:200],
                            " / ".join(filter(None, [t.get("serviceFirstLevel"), t.get("serviceSecondLevel")])),
                            (t.get("status") or ""),
                            (t.get("justification") or "")[:300],
                            ((t.get("owner") or {}).get("businessName") or (t.get("owner") or {}).get("email") or ""),
                        ))
            except Exception:
                pass
        if False and ticket_id:
            try:
                tid = int(ticket_id)
                from dados import ticket_por_id
                t = ticket_por_id(tid)
                if t:
                    contexto_ticket = ("\n## TICKET EM FOCO #%d\nAssunto: %s\nServico: %s\nStatus: %s\nJustificativa: %s\nOwner: %s\n" % (
                        tid,
                        (t.get("subject") or "")[:200],
                        " / ".join(filter(None, [t.get("serviceFirstLevel"), t.get("serviceSecondLevel")])),
                        (t.get("status") or ""),
                        (t.get("justification") or "")[:300],
                        ((t.get("owner") or {}).get("businessName") or (t.get("owner") or {}).get("email") or ""),
                    ))
            except Exception:
                pass

        # Contexto limitado: processos relevantes + tickets similares + artigos
        contexto = ""
        if identidade:
            self._json({"ok": True, "resposta": "Sou seu copiloto técnico pessoal para o suporte da FaturAgil. Posso ajudar com dúvidas, procedimentos, históricos, análise de erros, mesclas e geração de textos. Pergunte o que precisar.", "modelo": "local", "artigos": [], "macros": [], "intencao": "responder", "fontes": [], "cache": False})
            return
        # Conhecimento da empresa (compacto)
        try:
            from pathlib import Path
            cf = Path(__file__).resolve().parent.parent / "painel" / "conhecimento_faturagil.txt"
            if cf.exists():
                txt = cf.read_text(encoding="utf-8")[:1500]
                contexto = (contexto or "") + "\n## CONHECIMENTO DA EMPRESA\n" + txt + "\n"
                fontes_utilizadas.append({"tipo": "conhecimento", "id": "faturagil"})
        except Exception:
            pass
        if contexto_ticket:
            contexto = (contexto or "") + contexto_ticket
        # Casos de referencia validados (prioridade)
        try:
            if identidade:
                raise Exception("skip")
            rawc, _ = self._carregar_casos()
            casos = [c for c in (rawc.get("casos") or []) if c.get("status") == "resolvido"]
            # relevancia simples por tokens
            toks = set((texto or "").lower().split())
            relevantes = []
            for c in casos:
                base = " ".join([c.get("pergunta",""), c.get("diagnostico",""), c.get("procedimento","")]).lower()
                score = sum(1 for t in toks if t and t in base)
                if score > 0:
                    relevantes.append((score, c))
            relevantes.sort(key=lambda x: -x[0])
            for _, c in relevantes[:3]:
                fontes_utilizadas.append({"tipo": "caso", "id": c.get("id"), "versao": c.get("versao")})
                contexto += "\n## CASO VALIDADO: %s\nDiagnostico: %s\nProcedimento: %s\nResposta aprovada: %s\n" % (
                    (c.get("pergunta") or "")[:200],
                    (c.get("diagnostico") or "")[:400],
                    (c.get("procedimento") or "")[:400],
                    (c.get("resposta_aprovada") or "")[:400],
                )
        except Exception:
            pass
        try:
            if identidade:
                raise Exception("skip")
            import processos
            docs, _, _ = processos.buscar(texto[:500])
            for d in (docs or [])[:3]:
                if d.get("_confianca") == "fraco":
                    continue
                fontes_utilizadas.append({"tipo": "processo", "id": d.get("arquivo") or d.get("titulo")})
                for sec in (d.get("secoes") or [])[:2]:
                    contexto += "\n## " + (sec.get("titulo") or "") + "\n" + (sec.get("texto") or "")[:800] + "\n"
        except Exception:
            pass
        try:
            if identidade:
                raise Exception("skip")
            from dados import buscar_historico
            hist = buscar_historico(texto, limite=5)
            for it in (hist.get("itens") or [])[:5]:
                if (it.get("score") or 0) <= 0:
                    continue
                fontes_utilizadas.append({"tipo": "ticket", "id": it.get("ticket")})
                contexto += "\n- Ticket #%s: %s (%s)" % (it.get("ticket"), (it.get("assunto") or "")[:120], (it.get("resolvido_em") or ""))
        except Exception:
            pass
        # Anexo PDF/imagem
        anexo_texto = ""
        try:
            import base64, os, subprocess, tempfile
            if caminho_pdf:
                if os.path.isfile(caminho_pdf) and caminho_pdf.lower().endswith(".pdf"):
                    out = subprocess.run(["pdftotext", "-layout", caminho_pdf, "-"], capture_output=True, text=True, timeout=60)
                    if out.returncode == 0:
                        anexo_texto = (out.stdout or "")[:6000]
            elif arquivo_base64 and arquivo_nome.lower().endswith(".pdf"):
                raw = base64.b64decode(arquivo_base64)
                fd, tmp = tempfile.mkstemp(suffix=".pdf")
                os.write(fd, raw); os.close(fd)
                out = subprocess.run(["pdftotext", "-layout", tmp, "-"], capture_output=True, text=True, timeout=60)
                if out.returncode == 0:
                    anexo_texto = (out.stdout or "")[:6000]
                try:
                    os.remove(tmp)
                except Exception:
                    pass
            elif arquivo_base64 and arquivo_nome:
                anexo_texto = "[Imagem anexada: %s (base64, %d bytes)]" % (arquivo_nome, len(arquivo_base64))
        except Exception:
            anexo_texto = ""
        if anexo_texto:
            contexto = (contexto or "") + "\n## ANEXO\n" + anexo_texto[:6000] + "\n"

        # Mesclas e tickets da mesma empresa (se relevante)
        try:
            low = (texto or "").lower()
            if any(w in low for w in ("mesclar", "duplicado", "mesma empresa", "mesmo cliente", "mesmo assunto")):
                from dados import build_mesclas
                m = build_mesclas("atual", None)
                for it in (m.get("itens") or [])[:8]:
                    contexto += "\n- Mescla candidata: #%s e #%s (%s) - %s" % (it.get("ticket_a"), it.get("ticket_b"), it.get("cliente"), it.get("motivo"))
        except Exception:
            pass
        if contexto_ticket and contexto_ticket not in (contexto or ""):
            contexto = (contexto or "") + contexto_ticket
        contexto = contexto[:4000]
        prompt = "CONTEXTO:\n" + contexto + "\n\nPERGUNTA/CHAT:\n"
        try:
            import requests
            _sys = ("Você é meu copiloto técnico pessoal para o suporte da FaturAgil. "
                    "Analise o problema com base no contexto fornecido. Responda de forma direta e natural, adaptada à tarefa. "
                    "Não use seções obrigatórias. Diferencie fatos confirmados, hipóteses e informações ausentes. "
                    "Não invente ações, prazos ou resultados. Se faltar evidência, diga o que precisa ser coletado. "
                    "Respeite limites entre Suporte, Implantação, Desenvolvimento, Financeiro e terceiros. "
                    "Use o contexto do ticket selecionado quando disponível. Sugira macros e artigos apenas quando relevantes. "
                    "Se o texto for um chat de atendimento, inclua uma devolutiva pública para o cliente e o próximo passo. "
                    "Verifique nos playbooks/casos se a funcionalidade existe antes de afirmar.")
            if comando:
                _sys += " Tarefa: " + comando + ". Gere o texto solicitado."
            if stream:
                self._ia_stream_response(texto, contexto, None, _sys, comando, ticket_id, identidade, fontes_utilizadas, resposta_id, chave, cache, cache_path, body)
                return
            messages = [{"role": "system", "content": _sys}]
            for h in (historico or []):
                if isinstance(h, dict) and h.get("role") and h.get("content"):
                    messages.append({"role": h["role"], "content": str(h["content"])[:2000]})
            messages.append({"role": "user", "content": (prompt + "\n\n" + texto[:3000])})
            ultimo_erro = None
            for modelo in (OPENROUTER_MODELS or [OPENROUTER_MODEL]):
                try:
                    resp = requests.post(
                        OPENROUTER_URL,
                        headers={"Authorization": "Bearer " + OPENROUTER_API_KEY,
                                 "Content-Type": "application/json"},
                        json={"model": modelo,
                              "messages": messages,
                              "max_tokens": 600},
                        timeout=60,
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        resposta = ""
                        choices = data.get("choices") or []
                        if choices:
                            resposta = (choices[0].get("message") or {}).get("content") or ""
                        # Sugestoes de artigos e macros
                        artigos = []
                        macros = []
                        try:
                            from api_client import MovideskClient
                            res = MovideskClient().search_articles(texto[:80], page_size=5)
                            for it in (res.get("itens") or [])[:5]:
                                artigos.append({"id": it.get("id"), "title": it.get("title"),
                                                "url": "https://atendimento.faturagil.com.br/kb/article/%s" % it.get("id")})
                        except Exception:
                            pass
                        if not identidade and fontes_utilizadas:
                            try:
                                from macros import sugerir_macros
                                sug = sugerir_macros()
                                toks = {t for t in (texto or "").lower().split() if len(t) >= 4}
                                for m in (sug.get("candidatos") or []):
                                    mt = (m.get("texto") or "").lower()
                                    if toks and any(t in mt for t in toks):
                                        macros.append({"nome": m.get("texto", "")[:80], "tickets": m.get("tickets")})
                                        if len(macros) >= 3:
                                            break
                            except Exception:
                                pass
                        low = (texto or "").lower()
                        if identidade:
                            intencao = "responder"
                        elif any(w in low for w in ("encerrar", "resolver", "concluir", "finalizar")):
                            intencao = "encerrar"
                        elif any(w in low for w in ("mesclar", "duplicado", "mesmo assunto", "mesma empresa")):
                            intencao = "mesclar"
                        elif any(w in low for w in ("escalar", "dev", "analise", "fornecedor", "implantacao")):
                            intencao = "escalar"
                        else:
                            intencao = "responder"
                        try:
                            cache[chave] = {"resposta": resposta, "modelo": modelo, "artigos": artigos, "macros": macros, "intencao": intencao, "resposta_id": resposta_id, "contexto": contexto, "fontes": fontes_utilizadas}
                            # limita cache a 200
                            if len(cache) > 200:
                                for k in list(cache.keys())[:len(cache)-200]:
                                    cache.pop(k, None)
                            cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
                        except Exception:
                            pass
                        self._json({"ok": True, "resposta": resposta, "modelo": modelo,
                                    "artigos": artigos, "macros": macros, "intencao": intencao,
                                    "resposta_id": resposta_id, "fontes": fontes_utilizadas})
                        return
                    ultimo_erro = "OpenRouter HTTP %d: %s" % (resp.status_code, resp.text[:200])
                except Exception as exc:
                    ultimo_erro = str(exc)
            self._json({"erro": ultimo_erro or "falha"}, code=502)
        except Exception as exc:
            log.exception("Falha IA")
            self._json({"erro": str(exc)}, code=502)

    def _ia_cota(self, params: dict):
        if not OPENROUTER_API_KEY:
            self._json({"erro": "OPENROUTER_API_KEY nao configurada"}, code=400)
            return
        import hashlib, json
        from pathlib import Path
        cache_path = Path(__file__).resolve().parent.parent / "cache" / "ia_cache.json"
        try:
            cache = json.loads(cache_path.read_text(encoding="utf-8")) if cache_path.exists() else {}
        except Exception:
            cache = {}
        if not isinstance(cache, dict):
            cache = {}
        chave = hashlib.sha256((texto + "|" + (body.get("historico") and str(body.get("historico")) or "")).encode("utf-8")).hexdigest()
        if chave in cache:
            c = cache[chave]
            self._json({"ok": True, "resposta": c.get("resposta", ""), "modelo": c.get("modelo"), "artigos": c.get("artigos", []), "macros": c.get("macros", []), "intencao": c.get("intencao"), "resposta_id": c.get("resposta_id"), "fontes": c.get("fontes", []), "cache": True})
            return
        try:
            import requests
            # tenta /api/v1/auth/key
            r = requests.get("https://openrouter.ai/api/v1/auth/key",
                             headers={"Authorization": "Bearer " + OPENROUTER_API_KEY},
                             timeout=30)
            if r.status_code == 200:
                data = r.json().get("data") or {}
                self._json({"ok": True, "uso": data.get("usage"), "limite": data.get("limit"),
                            "restante": data.get("limit") - data.get("usage") if data.get("limit") is not None and data.get("usage") is not None else None})
                return
            # fallback credits
            r2 = requests.get("https://openrouter.ai/api/v1/credits",
                              headers={"Authorization": "Bearer " + OPENROUTER_API_KEY},
                              timeout=30)
            self._json({"ok": r2.status_code == 200, "status": r2.status_code, "dados": r2.json() if r2.status_code==200 else r2.text[:300]})
        except Exception as exc:
            self._json({"erro": str(exc)}, code=502)

    def _ia_feedback(self, params: dict):
        body = self._read_body()
        texto = (body.get("texto") or "").strip()
        nota = body.get("nota")
        # valida nota
        try:
            nota = int(nota)
        except (TypeError, ValueError):
            nota = None
        if nota not in (1, -1, None):
            nota = None
        if not texto:
            self._json({"erro": "informe texto"}, code=400)
            return
        motivo = (body.get("motivo") or "").strip() or None
        motivos_validos = {
            "informacao_incorreta", "generica", "sem_evidencia",
            "procedimento_incorreto", "desatualizada", "busca_inadequada", "outro",
        }
        if motivo is not None and motivo not in motivos_validos:
            motivo = "outro"
        resposta_corrigida = (body.get("resposta_corrigida") or "").strip() or None
        if resposta_corrigida is not None and len(resposta_corrigida) > 4000:
            resposta_corrigida = resposta_corrigida[:4000]
        resposta_id = (body.get("resposta_id") or "").strip() or None
        fontes_body = body.get("fontes")
        if not isinstance(fontes_body, list):
            fontes_body = None
        # fontes/contexto: usa enviados ou recomputa
        contexto = (body.get("contexto") or "").strip() or None
        fontes = body.get("fontes")
        if not isinstance(fontes, dict):
            fontes = None
        if contexto is None or fontes is None:
            # recomputa (custo baixo)
            try:
                import processos
                ctx = ""
                docs, _, _ = processos.buscar(texto[:500])
                for d in (docs or [])[:3]:
                    for sec in (d.get("secoes") or [])[:2]:
                        ctx += "\n## " + (sec.get("titulo") or "") + "\n" + (sec.get("texto") or "")[:800] + "\n"
                from dados import buscar_historico
                hist = buscar_historico(texto, limite=5)
                for it in (hist.get("itens") or [])[:5]:
                    ctx += "\n- Ticket #%s: %s" % (it.get("ticket"), (it.get("assunto") or "")[:120])
                contexto = ctx[:4000] if contexto is None else contexto
            except Exception:
                contexto = contexto or ""
            if fontes is None:
                fontes = {}
        import json
        from pathlib import Path
        p = Path(__file__).resolve().parent.parent / "cache" / "ia_feedback.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        try:
            dados = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
        except Exception:
            dados = []
        if not isinstance(dados, list):
            dados = []
        from datetime import datetime
        registro = {
            "texto": texto[:1000],
            "nota": nota,
            "motivo": motivo,
            "resposta_corrigida": resposta_corrigida,
            "resposta_id": resposta_id,
            "contexto": contexto,
            "fontes": fontes if fontes is not None else fontes_body,
            "ts": datetime.now().isoformat(timespec="seconds"),
        }
        # preserva campos antigos (nota/texto/ts) - ja presentes
        dados.append(registro)
        # limita a 500
        dados = dados[-500:]
        try:
            p.write_text(json.dumps(dados, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            self._json({"erro": "falha ao gravar: %s" % exc}, code=500)
            return
        self._json({"ok": True})

    def _carregar_casos(self):
        import json
        from pathlib import Path
        p = Path(__file__).resolve().parent.parent / "cache" / "ia_casos.json"
        try:
            raw = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"schema": 1, "casos": []}
        except Exception:
            raw = {"schema": 1, "casos": []}
        if not isinstance(raw, dict):
            raw = {"schema": 1, "casos": []}
        raw.setdefault("schema", 1)
        if not isinstance(raw.get("casos"), list):
            raw["casos"] = []
        return raw, p

    def _ia_casos_get(self, params: dict):
        raw, _ = self._carregar_casos()
        status = (params.get("status") or "").strip()
        casos = raw.get("casos") or []
        if status:
            casos = [c for c in casos if c.get("status") == status]
        self._json({"schema": raw.get("schema"), "total": len(casos), "casos": casos})

    def _ia_casos_post(self, params: dict):
        body = self._read_body()
        acao = (body.get("acao") or "criar").strip()
        raw, p = self._carregar_casos()
        casos = raw.get("casos") or []
        if acao == "criar":
            import uuid
            caso = {
                "id": body.get("id") or ("caso-" + uuid.uuid4().hex[:8]),
                "pergunta": (body.get("pergunta") or "").strip(),
                "contexto": (body.get("contexto") or "").strip(),
                "diagnostico": (body.get("diagnostico") or "").strip(),
                "procedimento": (body.get("procedimento") or "").strip(),
                "fontes": body.get("fontes") if isinstance(body.get("fontes"), list) else [],
                "resposta_aprovada": (body.get("resposta_aprovada") or "").strip(),
                "status": (body.get("status") or "pendente").strip(),
                "validado_por": (body.get("validado_por") or "").strip(),
                "validado_em": body.get("validado_em"),
                "versao": int(body.get("versao") or 1),
            }
            if caso["status"] not in ("resolvido", "pendente", "hipotese", "escalonado"):
                caso["status"] = "pendente"
            if not caso["pergunta"]:
                self._json({"erro": "pergunta obrigatoria"}, code=400)
                return
            # aprovacao explicita: status resolvido exige validado_por ou resposta_aprovada
            if caso["status"] == "resolvido" and not (caso["validado_por"] or caso["resposta_aprovada"]):
                caso["status"] = "pendente"
            if not caso["validado_em"] and caso["status"] == "resolvido":
                from datetime import datetime
                caso["validado_em"] = datetime.now().isoformat(timespec="seconds")
            casos.append(caso)
            raw["casos"] = casos
            try:
                p.write_text(__import__("json").dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
            except Exception as exc:
                self._json({"erro": "falha ao gravar: %s" % exc}, code=500)
                return
            self._json({"ok": True, "caso": caso})
            return
        if acao == "aprovar":
            cid = (body.get("id") or "").strip()
            for c in casos:
                if c.get("id") == cid:
                    c["status"] = "resolvido"
                    if body.get("resposta_aprovada"):
                        c["resposta_aprovada"] = str(body.get("resposta_aprovada")).strip()
                    if body.get("validado_por"):
                        c["validado_por"] = str(body.get("validado_por")).strip()
                    from datetime import datetime
                    c["validado_em"] = datetime.now().isoformat(timespec="seconds")
                    c["versao"] = int(c.get("versao") or 1) + 1
                    raw["casos"] = casos
                    try:
                        p.write_text(__import__("json").dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
                    except Exception as exc:
                        self._json({"erro": "falha ao gravar: %s" % exc}, code=500)
                        return
                    self._json({"ok": True, "caso": c})
                    return
            self._json({"erro": "caso nao encontrado"}, code=404)
            return
        self._json({"erro": "acao invalida"}, code=400)

    def _ia_feedback_stats(self, params: dict):
        import json
        from pathlib import Path
        from collections import Counter
        p = Path(__file__).resolve().parent.parent / "cache" / "ia_feedback.json"
        try:
            dados = json.loads(p.read_text(encoding="utf-8")) if p.exists() else []
        except Exception:
            dados = []
        if not isinstance(dados, list):
            dados = []
        pos = sum(1 for d in dados if d.get("nota") == 1)
        neg = sum(1 for d in dados if d.get("nota") == -1)
        motivos = Counter(d.get("motivo") for d in dados if d.get("motivo"))
        self._json({"total": len(dados), "positivos": pos, "negativos": neg,
                    "motivos": dict(motivos)})

    def _busca_semantica(self, params: dict):
        q = (params.get("q") or "").strip()
        if not q:
            self._json({"erro": "informe q"}, code=400)
            return
        try:
            import math, re
            from dados import buscar_historico
            hist = buscar_historico(q, limite=50)
            docs = []
            for it in (hist.get("itens") or []):
                docs.append({"tipo": "ticket", "id": it.get("ticket"),
                             "texto": " ".join([it.get("assunto") or "", it.get("cliente") or "", it.get("servico") or ""])})
            # artigos KB
            try:
                from api_client import MovideskClient
                res = MovideskClient().search_articles(q, page_size=10)
                for a in (res.get("itens") or []):
                    docs.append({"tipo": "artigo", "id": a.get("id"),
                                 "texto": " ".join([a.get("title") or ""] + [c.get("name","") for c in (a.get("category") or [])])})
            except Exception:
                pass

            def toks(t):
                return [w for w in re.findall(r"[a-z0-9]{3,}", (t or "").lower())]

            # IDF
            N = len(docs) + 1
            df = {}
            for d in docs:
                for w in set(toks(d["texto"])):
                    df[w] = df.get(w, 0) + 1
            idf = {w: math.log((N+1)/(df.get(w,0)+1)) + 1 for w in df}

            def vec(text):
                v = {}
                for w in toks(text):
                    v[w] = v.get(w, 0) + 1
                # tf-idf
                for w in list(v.keys()):
                    v[w] = v[w] * idf.get(w, 1.0)
                return v

            def cos(a, b):
                dot = sum(a.get(w,0)*b.get(w,0) for w in set(a)|set(b))
                na = math.sqrt(sum(x*x for x in a.values()))
                nb = math.sqrt(sum(x*x for x in b.values()))
                if na == 0 or nb == 0:
                    return 0.0
                return dot/(na*nb)

            qv = vec(q)
            scored = []
            for d in docs:
                dv = vec(d["texto"])
                sc = cos(qv, dv)
                if sc > 0.05:
                    scored.append({"tipo": d["tipo"], "id": d["id"], "score": round(sc, 3), "texto": d["texto"][:120]})
            scored.sort(key=lambda x: -x["score"])
            self._json({"q": q, "total": len(scored), "itens": scored[:15]})
        except Exception as exc:
            self._json({"erro": str(exc)}, code=502)

    def _ia_stream_response(self, texto, contexto, _unused, _sys, comando, ticket_id, identidade, fontes_utilizadas, resposta_id, chave, cache, cache_path, body):
        import json, requests
        historico = body.get("historico") or []
        messages = [{"role": "system", "content": _sys}]
        for h in (historico or []):
            if isinstance(h, dict) and h.get("role") and h.get("content"):
                messages.append({"role": h["role"], "content": str(h["content"])[:2000]})
        messages.append({"role": "user", "content": ("CONTEXTO:\n" + (contexto or "") + "\n\nPERGUNTA/CHAT:\n" + texto[:3000])})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        def send(obj):
            try:
                self.wfile.write(("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode("utf-8"))
                self.wfile.flush()
            except Exception:
                pass
        send({"tipo": "inicio"})
        resposta = ""
        modelo_usado = None
        ultimo_erro = None
        for modelo in (OPENROUTER_MODELS or [OPENROUTER_MODEL]):
            try:
                resp = requests.post(
                    OPENROUTER_URL,
                    headers={"Authorization": "Bearer " + OPENROUTER_API_KEY,
                             "Content-Type": "application/json"},
                    json={"model": modelo,
                          "messages": messages,
                          "max_tokens": 600,
                          "stream": True},
                    timeout=60,
                    stream=True,
                )
                if resp.status_code != 200:
                    ultimo_erro = "HTTP %d" % resp.status_code
                    continue
                modelo_usado = modelo
                for line in resp.iter_lines():
                    if not line:
                        continue
                    line = line.decode("utf-8", errors="ignore")
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        obj = json.loads(data)
                    except Exception:
                        continue
                    choices = obj.get("choices") or []
                    if not choices:
                        continue
                    delta = (choices[0].get("delta") or {}).get("content")
                    if delta:
                        resposta += delta
                        send({"token": delta})
                break
            except Exception as exc:
                ultimo_erro = str(exc)
                continue
        if modelo_usado is None:
            send({"erro": ultimo_erro or "falha"})
            return
        low = (texto or "").lower()
        if identidade:
            intencao = "responder"
        elif any(w in low for w in ("encerrar", "resolver", "concluir", "finalizar")):
            intencao = "encerrar"
        elif any(w in low for w in ("mesclar", "duplicado", "mesmo assunto", "mesma empresa")):
            intencao = "mesclar"
        elif any(w in low for w in ("escalar", "dev", "analise", "fornecedor", "implantacao")):
            intencao = "escalar"
        else:
            intencao = "responder"
        artigos = []
        macros = []
        try:
            from api_client import MovideskClient
            res = MovideskClient().search_articles(texto[:80], page_size=5)
            for it in (res.get("itens") or [])[:5]:
                artigos.append({"id": it.get("id"), "title": it.get("title"),
                                "url": "https://atendimento.faturagil.com.br/kb/article/%s" % it.get("id")})
        except Exception:
            pass
        if not identidade and fontes_utilizadas:
            try:
                from macros import sugerir_macros
                sug = sugerir_macros()
                toks = {t for t in (texto or "").lower().split() if len(t) >= 4}
                for m in (sug.get("candidatos") or []):
                    mt = (m.get("texto") or "").lower()
                    if toks and any(t in mt for t in toks):
                        macros.append({"nome": m.get("texto", "")[:80], "tickets": m.get("tickets")})
                        if len(macros) >= 3:
                            break
            except Exception:
                pass
        try:
            cache[chave] = {"resposta": resposta, "modelo": modelo_usado, "artigos": artigos, "macros": macros, "intencao": intencao, "resposta_id": resposta_id, "contexto": contexto, "fontes": fontes_utilizadas}
            if len(cache) > 200:
                for k in list(cache.keys())[:len(cache)-200]:
                    cache.pop(k, None)
            cache_path.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
        except Exception:
            pass
        send({"fim": True, "resposta": resposta, "modelo": modelo_usado, "artigos": artigos, "macros": macros, "intencao": intencao, "resposta_id": resposta_id, "fontes": fontes_utilizadas})

    def _read_body(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}

    def _salvar_plantao(self):
        from dados import load_cache_raw, save_cache_raw

        body = self._read_body()
        raw = load_cache_raw()
        plantao = raw.get("plantao") if isinstance(raw.get("plantao"), dict) else {}
        if isinstance(body.get("por_data"), dict):
            plantao["por_data"] = {
                str(k): str(v).lower() for k, v in body["por_data"].items() if v
            }
        if "nota" in body:
            plantao["nota"] = str(body.get("nota") or "")
        plantao["atualizado_em"] = datetime.now().isoformat(timespec="seconds")
        raw["plantao"] = plantao
        save_cache_raw(raw)
        from dados import build_plantao

        self._json(build_plantao())

    def _consultar(self, agente: str = AGENT_EMAIL, forcar: bool = False):
        from datetime import timedelta

        from dados import build_payload

        agente = (agente or AGENT_EMAIL).lower()
        owner = None if agente in ("*", "todos", "") else agente
        key = agente if owner else "*"
        try:
            from api_client import MovideskClient

            client = MovideskClient()
            now = datetime.now()
            hoje_dt = now.replace(hour=0, minute=0, second=0, microsecond=0)
            since_dt = now - timedelta(days=90)
            survey_dt = now - timedelta(days=365)
            since_odata = _odata(since_dt.replace(hour=0, minute=0, second=0, microsecond=0))
            since_survey = survey_dt.strftime('%Y-%m-%dT00:00:00')
            hoje_odata = _odata(hoje_dt)

            # load_cache_raw() já migra formatos antigos para o schema por_agente.
            raw = load_cache_raw()
            por_agente = raw.setdefault("por_agente", {})

            # Atendentes (cache compartilhado).
            if not raw.get("agentes") and owner:
                try:
                    raw["agentes"] = client.list_agents()
                except Exception as exc:
                    log.warning("Falha ao listar atendentes: %s", exc)

            # Pesquisa de satisfação: global (conta), reusa cache local se recente.
            survey = None
            survey_salvo_em = raw.get("survey_salvo_em")
            survey_reuso = False
            if not forcar and isinstance(raw.get("survey"), list):
                try:
                    salvo_dt = datetime.fromisoformat(survey_salvo_em) if survey_salvo_em else None
                except (ValueError, TypeError):
                    salvo_dt = None
                if salvo_dt is not None and (now - salvo_dt) <= timedelta(hours=SURVEY_CACHE_HOURS):
                    survey = raw["survey"]
                    survey_reuso = True
                    log.info("Pesquisa reutilizada do cache (salvo em %s)", survey_salvo_em)
            if survey is None:
                survey = client.list_survey_responses(since_survey)
                survey_salvo_em = now.isoformat(timespec="seconds")
                raw["survey"] = survey
                raw["survey_salvo_em"] = survey_salvo_em

            # Enunciado oficial das perguntas (GET /survey/questions). A resposta da
            # pesquisa so traz questionId; o texto vem daqui e fica cacheado.
            if _cache_vencido(raw.get("survey_questions_salvo_em"), now, SURVEY_QUESTIONS_CACHE_HOURS) \
                    or not isinstance(raw.get("survey_questions"), dict):
                try:
                    raw["survey_questions"] = client.list_survey_questions()
                    raw["survey_questions_salvo_em"] = now.isoformat(timespec="seconds")
                except Exception as exc:
                    log.warning("Falha ao listar perguntas da pesquisa: %s", exc)

            # Mesclas confirmadas no Movidesk (GET /tickets/merged).
            confirmadas = raw.get("mesclas_confirmadas") if isinstance(raw.get("mesclas_confirmadas"), dict) else {}
            if _cache_vencido(confirmadas.get("salvo_em"), now, MERGED_CACHE_HOURS) \
                    or not isinstance(confirmadas.get("itens"), list):
                raw["mesclas_confirmadas"] = _buscar_mesclas_confirmadas(client, now)

            # Servicos (catalogo global, cacheado).
            if forcar or not isinstance(raw.get("services"), list):
                try:
                    raw["services"] = client.list_services()
                    raw["services_salvo_em"] = now.isoformat(timespec="seconds")
                except Exception as exc:
                    log.warning("Falha ao listar servicos: %s", exc)

            previous = por_agente.get(key) if isinstance(por_agente.get(key), dict) else None
            last_sync = (previous or {}).get("last_sync_odata")
            use_delta = (not forcar) and bool(last_sync) and bool((previous or {}).get("ativos"))

            fetch_start = now - timedelta(minutes=1)
            new_last_sync = _odata(fetch_start)

            if use_delta:
                log.info("Sync incremental do agente %s desde %s", key, last_sync)
                delta = client.list_tickets_changed_since(owner, last_sync)
                tickets, resolvidos = _merge_delta(previous, delta, since_odata)
            else:
                tickets = client.list_active_tickets(owner)
                resolvidos = client.list_resolved_tickets_since(owner, since_odata)
            if owner:
                interagiu = client.list_tickets_with_my_actions_since(owner, hoje_odata)
                fora = client.list_participation_outside_load(owner)
            else:
                interagiu, fora = [], []
            por_agente[key] = {
                "salvo_em": now.isoformat(timespec="seconds"),
                "last_sync_odata": new_last_sync,
                "ativos": tickets, "resolvidos": resolvidos,
                "interagiu_hoje": interagiu, "fora_carga": fora,
            }
            save_cache_raw(raw)
        except Exception as exc:
            log.exception("Falha ao consultar a API")
            self._json({"erro": str(exc)}, code=502)
            return

        payload = build_payload(
            tickets, now,
            resolved_raw=resolvidos,
            survey_raw=survey,
            interagiu_raw=interagiu,
            fora_raw=fora,
            agente_email=agente,
        )
        payload["origem"] = "api"
        payload["movidesk_web"] = MOVIDESK_WEB_URL
        payload["pesquisa_cache"] = survey_reuso
        self._json(payload)


def main() -> int:
    if "--daemon" not in sys.argv:
        if is_running():
            print(f"Painel ja esta rodando em {server_url()}")
            open_browser()
            return 0
        spawn_daemon()
        print("Iniciando painel...")
        for _ in range(30):
            if is_running():
                break
            import time
            time.sleep(0.3)
        else:
            print(f"Nao subiu. Veja {LOG_FILE}", file=sys.stderr)
            return 1
        print(server_url())
        open_browser()
        return 0

    PID_FILE.write_text(str(__import__("os").getpid()), encoding="utf-8")
    try:
        server = ThreadingHTTPServer((HOST, PORT), Handler)
    except OSError as exc:
        log.error("Nao foi possivel abrir a porta %s: %s", PORT, exc)
        return 1
    log.info("Painel rodando em %s", server_url())
    print(f"Painel rodando em {server_url()}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        if PID_FILE.exists():
            PID_FILE.unlink()
    return 0


if __name__ == "__main__":
    sys.exit(main())
