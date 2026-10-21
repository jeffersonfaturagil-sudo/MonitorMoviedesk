"""Smoke tests das rotas HTTP do painel (sem abrir socket).

Instancia o Handler com um request falso (path + wfile em memoria) e chama
do_GET/do_POST direto, validando status e JSON.
"""
import io
import json
from urllib.parse import quote

import server


class _FakeHandler(server.Handler):
    def __init__(self, path, body=b"", headers=None):
        self.path = path
        self.headers = {"Content-Length": str(len(body))}
        if headers:
            self.headers.update(headers)
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.code = None
        self._hdrs = []

    def send_response(self, code, message=None):
        self.code = code

    def send_header(self, key, value):
        self._hdrs.append((key, value))

    def end_headers(self):
        pass

    def log_message(self, *args):
        pass

    def json_body(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def _get(path):
    h = _FakeHandler(path)
    h.do_GET()
    return h


def _get_h(path, headers):
    h = _FakeHandler(path, headers=headers)
    h.do_GET()
    return h


def _post(path, payload=None, headers=None):
    body = json.dumps(payload or {}).encode("utf-8")
    h = _FakeHandler(path, body, headers=headers)
    h.do_POST()
    return h


def _patch_raw(monkeypatch, raw):
    import dados

    monkeypatch.setattr(dados, "load_cache_raw", lambda: raw)
    monkeypatch.setattr(server, "load_cache_raw", lambda: raw)


def test_http_health():
    h = _get("/health")
    assert h.code == 200
    assert h.json_body()["status"] == "ok"


def test_http_404():
    h = _get("/rota-que-nao-existe")
    assert h.code == 404


def test_http_agentes(monkeypatch):
    _patch_raw(monkeypatch, {"agentes": [{"nome": "Lucas", "email": "lucas@faturagil.com.br"}]})
    h = _get("/api/agentes")
    assert h.code == 200
    j = h.json_body()
    assert any(a.get("email") == "lucas@faturagil.com.br" for a in j["agentes"])


def test_http_historico(monkeypatch):
    raw = {
        "historico": [
            {
                "id": 1,
                "subject": "Boleto pendente",
                "baseStatus": "Resolved",
                "owner": {"email": "lucas@faturagil.com.br", "businessName": "Lucas"},
                "clients": [{"businessName": "ACME"}],
            }
        ]
    }
    _patch_raw(monkeypatch, raw)
    h = _get("/api/historico?q=boleto&agente=*")
    assert h.code == 200
    j = h.json_body()
    assert any(i["ticket"] == 1 for i in j["itens"])


def test_http_relatorio(monkeypatch):
    import dados

    raw = {
        "agentes": [],
        "por_agente": {"*": {"ativos": [{
            "id": 1,
            "subject": "Boleto pendente",
            "baseStatus": "New",
            "clients": [{"businessName": "ACME"}],
            "owner": {"email": "lucas@faturagil.com.br", "businessName": "Lucas"},
            "createdDate": "2026-10-14T10:00:00-03:00",
        }], "resolvidos": []}},
        "historico": [],
        "survey": [],
    }
    _patch_raw(monkeypatch, raw)
    monkeypatch.setattr(dados, "_map_recorrentes", lambda *a, **k: {})
    monkeypatch.setattr(dados, "_rel_acoes_index", lambda *a, **k: {})
    h = _get("/api/relatorio?dias=30&agente=*")
    assert h.code == 200
    j = h.json_body()
    assert j["dias"] == 30
    assert "tempo" in j and "gargalos" in j and "inteligencia" in j


def test_http_sla(monkeypatch):
    from datetime import datetime, timedelta

    base = (datetime.now().astimezone() - timedelta(days=3)).strftime("%Y-%m-%dT%H:%M:%S-03:00")
    raw = {
        "agentes": [],
        "por_agente": {"*": {"ativos": [{
            "id": 1,
            "subject": "Boleto pendente",
            "baseStatus": "New",
            "status": "Em Atendimento",
            "justification": "",
            "clients": [{"businessName": "ACME"}],
            "owner": {"email": "lucas@faturagil.com.br", "businessName": "Lucas"},
            "createdDate": base,
            "lastUpdate": base,
            "actions": [{"createdDate": base, "createdBy": {"email": "cliente@exemplo.com"}}],
        }], "resolvidos": []}},
        "historico": [],
    }
    _patch_raw(monkeypatch, raw)
    h = _get("/api/sla?agente=*")
    assert h.code == 200
    j = h.json_body()
    assert j["resposta"]["total"] == 1
    assert j["resposta"]["itens"][0]["nivel"] == "VENCIDO"
    assert j["janela"] == "08:00-12:00 e 13:00-18:00"


def test_http_processos():
    h = _get("/api/processos?q=" + quote("erro de faturamento"))
    assert h.code == 200
    j = h.json_body()
    assert j["processos"]
    assert j["times"]


def test_http_solucao_fora_do_perfil(monkeypatch):
    raw = {
        "agentes": [{"nome": "Lucas", "email": "lucas@faturagil.com.br"}],
        "historico": [
            {
                "id": 99,
                "subject": "Assunto",
                "baseStatus": "Resolved",
                "owner": {"email": "outro@faturagil.com.br", "businessName": "Outro"},
                "clients": [{"businessName": "ACME"}],
            }
        ],
    }
    _patch_raw(monkeypatch, raw)
    h = _get("/api/solucao?id=99&agente=lucas@faturagil.com.br")
    assert h.code == 403


def test_http_acoes_timeline(monkeypatch):
    import api_client
    import macros

    acts = [
        {"createdDate": "2026-10-15T09:00:00-03:00",
         "createdBy": {"businessName": "Lucas", "email": "lucas@faturagil.com.br"},
         "type": 2, "origin": 2, "description": "Bom dia"},
        {"createdDate": "2026-10-15T11:00:00-03:00",
         "createdBy": {"businessName": "Joana", "email": "joana@cliente.com"},
         "type": 2, "origin": 5, "description": "Ok"},
    ]
    fake = type("C", (), {"get_ticket_actions": lambda self, tid: acts})
    monkeypatch.setattr(api_client, "MovideskClient", lambda *a, **k: fake())
    monkeypatch.setattr(macros, "registrar_acoes", lambda *a, **k: None)

    h = _get("/api/acoes?id=31392")
    assert h.code == 200
    j = h.json_body()
    assert j["ticket"] == 31392
    assert len(j["acoes"]) == 2
    assert j["acoes"][1]["autor"] == "Joana"
    assert j["acoes"][1]["horas_desde_anterior"] == 2.0
    assert j["resumo"]["ultima_por"] == "Joana"
    assert j["resumo"]["publicas"] == 2


def test_http_acoes_id_invalido():
    h = _get("/api/acoes?id=abc")
    assert h.code == 400


def test_http_macros_listar_definidas(monkeypatch, tmp_path):
    import macros

    monkeypatch.setattr(macros, "EXTRA_FILE", tmp_path / "macros_locais.json")
    h = _get("/api/macros/listar-definidas")
    assert h.code == 200
    assert h.json_body()["total"] == 0


def test_http_macros_remover_inexistente(monkeypatch, tmp_path):
    import macros

    monkeypatch.setattr(macros, "EXTRA_FILE", tmp_path / "macros_locais.json")
    h = _post("/api/macros/remover", {"id": "nao_existe"})
    assert h.code == 200
    assert h.json_body().get("erro")


def test_http_macros_definir_e_post_nao_get(monkeypatch, tmp_path):
    """Regressao: o frontend chama /api/macros/definir via POST; a rota so pode
    existir no do_POST (antes estava registrada no do_GET e devolvia 404)."""
    import macros

    monkeypatch.setattr(macros, "EXTRA_FILE", tmp_path / "macros_locais.json")
    assert _get("/api/macros/definir").code == 404
    h = _post("/api/macros/definir", {"nome": "x", "assinatura": "curta"})
    assert h.code == 200
    assert h.json_body().get("erro")


def test_http_checklist():
    h = _get("/api/checklist?q=" + quote("erro de faturamento"))
    assert h.code == 200
    j = h.json_body()
    assert j["passos"]
    assert "Fluxo: Erro de Faturamento" in j["fontes"]


def test_http_checklist_sem_query():
    h = _get("/api/checklist?q=")
    assert h.code == 200
    assert h.json_body()["passos"] == []


def test_http_mesclas_contrato(monkeypatch):
    _patch_raw(monkeypatch, {"por_agente": {}, "mesclas": {}})
    h = _get("/api/mesclas?escopo=atual&agente=lucas@faturagil.com.br")
    assert h.code == 200
    j = h.json_body()
    assert sorted(j.keys()) == ["agente", "confirmadas", "escopo", "itens", "total"]
    assert j["total"] == 0
    assert j["itens"] == []


def test_http_consultar_monta_odata_e_cacheia(monkeypatch):
    """Caminho real do botão Consultar, com o cliente da API trocado por um falso.

    Cobre a regressão que deixou o Consultar quebrado: `util.as_timezone_offset`
    (nome inexistente) e o import faltando de `ACTIVE_STATUSES_EXCLUDED`.
    """
    import re

    import api_client
    import dados

    chamadas = {}

    class _ClienteFalso:
        def list_agents(self):
            return [{"nome": "Jefferson", "email": "jefferson@faturagil.com.br"}]

        def list_survey_responses(self, since, until=None):
            chamadas["since_survey"] = since
            return []

        def list_survey_questions(self):
            return {"XxqG": {"pergunta": "Qual sua avaliação?", "tipo": 1, "ativo": True}}

        def list_services(self):
            return [{"id": 1, "name": "Atendimento ao Cliente"}]

        def list_merged_tickets(self, desde, ate, max_pages=6):
            chamadas["merged"] = (desde, ate, max_pages)
            return {"total": 0, "paginas": 1, "itens": []}

        def list_active_tickets(self, owner):
            chamadas["owner"] = owner
            return []

        def list_resolved_tickets_since(self, owner, since):
            chamadas["resolvidos_since"] = since
            return []

        def list_tickets_with_my_actions_since(self, owner, since):
            chamadas["acoes_since"] = since
            return []

        def list_participation_outside_load(self, owner):
            return []

    monkeypatch.setattr(api_client, "MovideskClient", _ClienteFalso)
    cache: dict = {}
    monkeypatch.setattr(server, "load_cache_raw", lambda: cache)
    monkeypatch.setattr(server, "save_cache_raw", lambda raw: cache.update(raw))
    monkeypatch.setattr(dados, "build_payload", lambda *a, **k: {"resumo": {}, "mesclas": []})

    h = _post("/api/consultar?agente=jefferson@faturagil.com.br")
    assert h.code == 200, h.json_body()
    assert h.json_body()["origem"] == "api"

    odata = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}$")
    bucket = cache["por_agente"]["jefferson@faturagil.com.br"]
    assert odata.match(bucket["last_sync_odata"]), bucket["last_sync_odata"]
    assert odata.match(chamadas["resolvidos_since"]), chamadas["resolvidos_since"]
    assert odata.match(chamadas["acoes_since"]), chamadas["acoes_since"]
    assert chamadas["owner"] == "jefferson@faturagil.com.br"
    assert chamadas["since_survey"].endswith("T00:00:00")
    assert isinstance(cache["survey_questions"], dict)
    assert cache["mesclas_confirmadas"]["itens"] == []
    assert cache["services"][0]["name"] == "Atendimento ao Cliente"


def test_http_consultar_devolve_erro_quando_a_api_falha(monkeypatch):
    import api_client

    class _ClienteQuebrado:
        # list_agents e list_survey_questions têm try/except próprio (só logam);
        # a falha de list_survey_responses é a que derruba a consulta.
        def list_agents(self):
            return []

        def list_survey_responses(self, since, until=None):
            raise RuntimeError("sem token")

    monkeypatch.setattr(api_client, "MovideskClient", _ClienteQuebrado)
    monkeypatch.setattr(server, "load_cache_raw", lambda: {})
    monkeypatch.setattr(server, "save_cache_raw", lambda raw: None)

    h = _post("/api/consultar?agente=jefferson@faturagil.com.br")
    assert h.code == 502
    assert "sem token" in h.json_body()["erro"]


def test_cache_vencido():
    from datetime import datetime, timedelta

    import server

    agora = datetime(2026, 10, 14, 12, 0)
    assert server._cache_vencido(None, agora, 12) is True
    assert server._cache_vencido("lixo", agora, 12) is True
    assert server._cache_vencido((agora - timedelta(hours=1)).isoformat(), agora, 12) is False
    assert server._cache_vencido((agora - timedelta(hours=13)).isoformat(), agora, 12) is True
    assert server._cache_vencido(agora.isoformat(), agora, 0) is True


def test_buscar_mesclas_confirmadas_com_cliente_falso():
    from datetime import datetime

    import server

    chamadas = []

    class _Fake:
        def list_merged_tickets(self, desde, ate, max_pages=6):
            chamadas.append((desde, ate, max_pages))
            return {"total": 1, "paginas": 1,
                    "itens": [{"ticket_id": 1, "merged_ids": [2], "last_update": ""}]}

    reg = server._buscar_mesclas_confirmadas(_Fake(), datetime(2026, 10, 14, 12, 0),
                                            dias=30, max_pages=4)
    assert chamadas == [("2026-09-14", "2026-10-14", 4)]
    assert reg["janela"] == {"desde": "2026-09-14", "ate": "2026-10-14"}
    assert reg["itens"][0]["ticket_id"] == 1
    assert reg["salvo_em"] == "2026-10-14T12:00:00"


def test_buscar_mesclas_confirmadas_nao_propaga_erro():
    from datetime import datetime

    import server

    class _Fake:
        def list_merged_tickets(self, *a, **k):
            raise RuntimeError("sem rede")

    reg = server._buscar_mesclas_confirmadas(_Fake(), datetime(2026, 10, 14, 12, 0))
    assert reg["itens"] == []
    assert "sem rede" in reg["erro"]


def test_http_artigos_sem_query(monkeypatch):
    h = _get("/api/artigos?q=")
    assert h.code == 400
    assert h.json_body().get("erro")


def test_http_comparativo(monkeypatch):
    _patch_raw(monkeypatch, {"historico": [], "agentes": []})
    h = _get("/api/comparativo?agente=*")
    assert h.code == 200
    j = h.json_body()
    assert j["escopo"] in ("todos", "perfil")
    assert "semana" in j and "mes" in j


def test_http_post_de_origem_externa_e_bloqueado(monkeypatch):
    monkeypatch.setattr(server, "load_cache_raw", lambda: {})
    monkeypatch.setattr(server, "save_cache_raw", lambda raw: None)

    h = _post("/api/plantao", {}, headers={"Origin": "https://evil.example"})
    assert h.code == 403

    h = _post("/api/plantao", {}, headers={"Host": "evil.example"})
    assert h.code == 403

    h = _post("/api/plantao", {}, headers={"Sec-Fetch-Site": "cross-site"})
    assert h.code == 403


def test_http_get_de_origem_externa_e_bloqueado(monkeypatch):
    _patch_raw(monkeypatch, {"historico": [], "agentes": []})
    h = _get_h("/api/comparativo?agente=*", {"Origin": "https://evil.example"})
    assert h.code == 403


def test_http_origem_local_e_permitida(monkeypatch):
    monkeypatch.setattr(server, "load_cache_raw", lambda: {})
    monkeypatch.setattr(server, "save_cache_raw", lambda raw: None)
    from urllib.parse import urlparse

    porta = urlparse(server.server_url()).port
    h = _post(
        "/api/plantao",
        {},
        headers={"Host": f"127.0.0.1:{porta}", "Origin": f"http://127.0.0.1:{porta}",
                 "Sec-Fetch-Site": "same-origin"},
    )
    assert h.code == 200


def test_http_acoes_inclui_guia_do_playbook(monkeypatch):
    import api_client
    import dados

    class _ClienteFalso:
        def get_ticket_actions(self, tid):
            return [{
                "createdDate": "2026-10-10T09:00:00-03:00",
                "createdBy": {"email": "lucas@faturagil.com.br", "businessName": "Lucas"},
                "description": "Bom dia", "origin": 1,
            }]

    monkeypatch.setattr(api_client, "MovideskClient", _ClienteFalso)
    monkeypatch.setattr(server.Handler, "_registrar_no_indice",
                        staticmethod(lambda *a, **k: None))
    monkeypatch.setattr(dados, "ticket_por_id", lambda tid: {
        "id": tid, "justification": "Aguardando » Equipe de análise", "status": "Aguardando",
    })

    h = _get("/api/acoes?id=33124")
    assert h.code == 200
    j = h.json_body()
    assert j["ticket"] == 33124
    assert j["guia"]["responsavel"] == "Engenharia e Tecnologia"
    assert j["guia"]["do_suporte"] is False

