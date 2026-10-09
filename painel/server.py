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
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PAINEL_DIR = Path(__file__).resolve().parent
BASE_DIR = PAINEL_DIR.parent
sys.path.insert(0, str(BASE_DIR))
sys.path.insert(0, str(PAINEL_DIR))

from config import AGENT_EMAIL, AGENT_NAME, MOVIDESK_WEB_URL, PANEL_PORT, SURVEY_CACHE_HOURS  # noqa: E402
from dados import load_cache_raw, save_cache_raw  # noqa: E402

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


def server_url(path: str = "/") -> str:
    return f"http://{HOST}:{PORT}{path}"


def _parse_query(query: str) -> dict:
    from urllib.parse import parse_qs, unquote

    out = {}
    for k, v in parse_qs(query, keep_blank_values=True).items():
        out[unquote(k)] = unquote(v[0]) if v else ""
    return out


def _merge_delta(previous: dict, delta: list[dict], since_iso_ref: str) -> tuple[list[dict], list[dict]]:
    """Aplica o delta (lastUpdate) sobre o cache anterior e devolve (ativos, resolvidos)."""
    from analyzer import parse_date

    abertos = ("Resolved", "Closed", "Canceled")
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
        if base in abertos:
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


def _odata(dt: datetime) -> str:
    tz = dt.astimezone().strftime("%z")
    tz = f"{tz[:3]}:{tz[3:]}" if tz else "-03:00"
    return f"{dt.strftime('%Y-%m-%dT%H:%M:%S')}{tz}"


def _meses(desde: datetime, ate: datetime):
    from datetime import timedelta

    ini = desde.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while ini < ate:
        nxt = (ini.replace(day=28) + timedelta(days=4)).replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        yield ini, min(nxt, ate)
        ini = nxt


_historico_job: dict = {"ativo": False}
_historico_lock = threading.Lock()
_STATUS_HISTORICO = ("Resolved", "Closed", "Canceled")

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

    def do_GET(self):
        path, _, query = self.path.partition("?")
        params = _parse_query(query)
        if path in ("/", "/index.html"):
            if not INDEX_FILE.exists():
                self._send(404, b"index.html nao encontrado", "text/plain; charset=utf-8")
                return
            self._send(200, INDEX_FILE.read_bytes(), "text/html; charset=utf-8")
        elif path == "/health":
            self._json({"status": "ok"})
        elif path == "/api/agentes":
            self._agentes()
        elif path == "/api/historico":
            from dados import buscar_historico

            self._json(buscar_historico(params.get("q") or ""))
        elif path == "/api/mapa-horarios":
            from dados import build_mapa_horarios

            self._json(build_mapa_horarios())
        elif path == "/api/macros":
            from macros import estatisticas

            payload = estatisticas()
            with _acoes_lock:
                payload["job"] = dict(_acoes_job)
            self._json(payload)
        elif path == "/api/macros/validar":
            self._macro_validar(params)
        elif path == "/api/macros/buscar":
            from macros import buscar

            q = (params.get("q") or "").strip()
            if not q:
                self._json({"erro": "informe q (trecho para buscar)"}, code=400)
                return
            self._json(buscar(q))
        elif path == "/api/macros/status":
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
        elif path == "/api/parecidos":
            from dados import build_parecidos

            try:
                tid = int(params.get("id") or "")
            except (TypeError, ValueError):
                tid = 0
            self._json(build_parecidos(tid, (params.get("modo") or "parecidos").lower()))
        elif path == "/api/plantao":
            from dados import build_plantao

            self._json(build_plantao(params.get("data") or None))
        elif path == "/api/mesclas":
            from dados import build_mesclas

            escopo = params.get("escopo") or "atual"
            agente = (params.get("agente") or AGENT_EMAIL).lower()
            self._json(build_mesclas(escopo, agente))
        elif path == "/api/acoes":
            self._acoes(params.get("id") or "")
        elif path == "/api/servicos":
            from dados import build_servicos

            self._json(build_servicos())
        elif path == "/api/ranking":
            from dados import build_ranking

            incluir_dev = (params.get("dev") or "").lower() in ("1", "true", "sim", "on")
            self._json(build_ranking(incluir_dev=incluir_dev))
        elif path == "/api/indicadores":
            from dados import build_indicadores

            self._json(build_indicadores())
        elif path == "/api/solucao":
            self._solucao(params.get("id") or "")
        elif path == "/api/historico/status":
            self._historico_status()
        elif path == "/api/dados":
            from dados import load_cache_payload

            agente = (params.get("agente") or AGENT_EMAIL).lower()
            payload = load_cache_payload(agente)
            if payload is None:
                self._json({"vazio": True, "mensagem": "Sem dados. Clique em Consultar."})
                return
            payload["origem"] = "cache"
            self._json(payload)
        else:
            self._send(404, b"nao encontrado", "text/plain; charset=utf-8")

    def _acoes(self, ticket_id: str):
        from dados import acao_info

        try:
            tid = int(ticket_id)
        except (TypeError, ValueError):
            self._json({"erro": "id invalido"}, code=400)
            return
        try:
            from analyzer import parse_date
            from api_client import MovideskClient

            acts = MovideskClient().get_ticket_actions(tid)
            self._registrar_no_indice(tid, acts)
            itens = []
            for a in acts:
                d = parse_date(a.get("createdDate"))
                cb = a.get("createdBy") or {}
                desc = (a.get("description") or "").strip()
                itens.append({
                    "data": d.strftime("%d/%m/%Y %H:%M") if d else "",
                    "data_iso": d.isoformat() if d else "",
                    "autor": cb.get("businessName") or cb.get("email") or "",
                    "interno": a.get("type") == 1,
                    "descricao": desc,
                })
            itens.sort(key=lambda x: x["data_iso"])
            self._json({"ticket": tid, "acoes": itens})
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

    def _solucao(self, ticket_id: str):
        from dados import load_cache_raw, montar_solucao, save_cache_raw

        try:
            tid = int(ticket_id)
        except (TypeError, ValueError):
            self._json({"erro": "id invalido"}, code=400)
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
        self._json({
            "agentes": agentes,
            "default": AGENT_EMAIL.lower() or "*",
            "agente_nome": AGENT_NAME,
            "plantao_ordem": PLANTAO_ORDEM,
        })

    def do_POST(self):
        path, _, query = self.path.partition("?")
        params = _parse_query(query)
        if path == "/api/consultar":
            agente = (params.get("agente") or AGENT_EMAIL).lower()
            self._consultar(agente=agente, forcar="forcar" in params)
        elif path == "/api/historico/completo":
            self._historico_completo(params)
        elif path == "/api/macros/indexar":
            body = self._read_body()
            if body.get("parar"):
                with _acoes_lock:
                    _acoes_job["ativo"] = False
                    job = dict(_acoes_job)
                self._json({"ok": True, "parando": True, "job": job})
                return
            self._json(_iniciar_indexacao_acoes())
        elif path == "/api/pesquisa":
            self._pesquisa(params)
        elif path == "/api/plantao":
            self._salvar_plantao()
        elif path == "/api/mesclas":
            from dados import salvar_fila_mescla

            body = self._read_body()
            chave = str(body.get("chave") or "").strip()
            if not chave:
                self._json({"erro": "chave obrigatoria"}, code=400)
                return
            self._json({"fila": salvar_fila_mescla(chave, str(body.get("status") or ""))})
        else:
            self._send(404, b"nao encontrado", "text/plain; charset=utf-8")

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
            tz = since_dt.astimezone().strftime("%z")
            tz = f"{tz[:3]}:{tz[3:]}" if tz else "-03:00"
            since_odata = f"{since_dt.strftime('%Y-%m-%dT00:00:00')}{tz}"
            since_survey = survey_dt.strftime("%Y-%m-%dT00:00:00")
            hoje_odata = f"{hoje_dt.strftime('%Y-%m-%dT00:00:00')}{tz}"

            raw = load_cache_raw()
            if "por_agente" not in raw and isinstance(raw.get("ativos"), list):
                raw = {"agentes": raw.get("agentes") or [], "survey": raw.get("survey"),
                       "survey_salvo_em": raw.get("survey_salvo_em"),
                       "por_agente": {AGENT_EMAIL.lower(): raw}}
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
            tz2 = fetch_start.astimezone().strftime("%z")
            tz2 = f"{tz2[:3]}:{tz2[3:]}" if tz2 else "-03:00"
            new_last_sync = f"{fetch_start.strftime('%Y-%m-%dT%H:%M:%S')}{tz2}"

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
