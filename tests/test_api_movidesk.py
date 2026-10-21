"""Testes do cliente Movidesk: retry-after, paginação e rotas novas.

Cobrem o que foi verificado contra a API real (`/survey/questions`,
`/tickets/merged`, `/tickets/htmldescription`, `/kb/article`, `/storage/download`)
e o bug de `build_mesclas` (código colado do analyzer, que quebrava a aba).
"""
import dados
from api_client import MovideskClient, retry_after_seconds


class _Resp:
    def __init__(self, status=200, payload=None, headers=None, content=b"", text=""):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}
        self.content = content
        self.text = text or ""

    def json(self):
        if self._payload is None:
            raise ValueError("sem json")
        return self._payload


class _Sessao:
    """Sessão falsa: devolve as respostas na ordem e registra as chamadas."""

    def __init__(self, respostas):
        self.respostas = list(respostas)
        self.chamadas = []

    def get(self, url, params=None, timeout=None):
        self.chamadas.append((url, dict(params or {})))
        return self.respostas.pop(0) if self.respostas else _Resp(500)

    def post(self, url, json=None, timeout=None):
        self.chamadas.append((url, dict(json or {})))
        return self.respostas.pop(0) if self.respostas else _Resp(500)


def _cliente(respostas):
    c = MovideskClient(token="token-de-teste")
    c.session = _Sessao(respostas)
    return c


# --------------------------------------------------------------- retry-after
def test_retry_after_segundos_data_ausente_e_lixo():
    assert retry_after_seconds(_Resp(headers={"Retry-After": "60"})) == 60.0
    assert retry_after_seconds(_Resp(headers={"Retry-After": " 90 "})) == 90.0
    assert retry_after_seconds(_Resp(headers={"Retry-After": "0"})) == 0.0
    assert retry_after_seconds(_Resp(headers={})) is None
    assert retry_after_seconds(_Resp(headers={"Retry-After": "ontem"})) is None
    assert retry_after_seconds(None) is None
    # data HTTP no passado -> 0 (nunca negativo)
    assert retry_after_seconds(_Resp(headers={"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"})) == 0.0


def test_espera_rate_limit_respeita_header_e_piso_documentado():
    c = _cliente([])
    # header manda e fica acima do piso
    assert c._espera_rate_limit(_Resp(headers={"Retry-After": "90"}), 0) == 90.0
    # sem header -> piso documentado 60/120/300
    assert c._espera_rate_limit(_Resp(headers={}), 0) == 60.0
    assert c._espera_rate_limit(_Resp(headers={}), 1) == 120.0
    assert c._espera_rate_limit(_Resp(headers={}), 2) == 300.0
    assert c._espera_rate_limit(_Resp(headers={}), 9) == 300.0
    # header abaixo do piso nunca antecipa o retry
    assert c._espera_rate_limit(_Resp(headers={"Retry-After": "5"}), 1) == 120.0
    # teto evita travar o painel com um valor absurdo
    assert c._espera_rate_limit(_Resp(headers={"Retry-After": "99999"}), 0) <= 600.0


def test_get_usa_retry_after_do_429(monkeypatch):
    import api_client

    dormido = []
    monkeypatch.setattr(api_client.time, "sleep", lambda s: dormido.append(s))
    c = _cliente([_Resp(429, headers={"Retry-After": "90"}), _Resp(200, payload={"ok": True})])
    assert c._get("tickets", {}) == {"ok": True}
    assert dormido == [90.0]


def test_get_usa_o_piso_quando_retry_after_e_menor(monkeypatch):
    import api_client

    dormido = []
    monkeypatch.setattr(api_client.time, "sleep", lambda s: dormido.append(s))
    c = _cliente([_Resp(429, headers={"Retry-After": "45"}), _Resp(200, payload={"ok": True})])
    assert c._get("tickets", {}) == {"ok": True}
    assert dormido == [60.0]


def test_get_sem_retry_after_usa_backoff_fixo(monkeypatch):
    import api_client

    dormido = []
    monkeypatch.setattr(api_client.time, "sleep", lambda s: dormido.append(s))
    c = _cliente([_Resp(429), _Resp(200, payload=[])])
    assert c._get("tickets", {}) == []
    assert dormido == [60.0]


# ---------------------------------------------------------------- paginação
def test_list_tickets_pagina_com_page_size(monkeypatch):
    import api_client

    monkeypatch.setattr(api_client, "PAGE_SIZE", 3)
    c = _cliente([
        _Resp(200, payload=[{"id": 1}, {"id": 2}, {"id": 3}]),
        _Resp(200, payload=[{"id": 4}]),
        _Resp(200, payload=[]),
    ])
    itens = c._list_tickets("true", "id asc")
    assert [i["id"] for i in itens] == [1, 2, 3, 4]
    assert c.session.chamadas[0][1]["$top"] == 3
    assert c.session.chamadas[0][1]["$skip"] == 0
    assert c.session.chamadas[1][1]["$skip"] == 3
    # A pagina curta nao encerra: confirma na pagina vazia (skip avanca pelo
    # que a API devolveu, nao pelo $top pedido).
    assert c.session.chamadas[2][1]["$skip"] == 4


def test_list_tickets_adapta_ao_teto_efetivo_da_api(monkeypatch):
    """Regressão: se a API capar a pagina abaixo do $top (doc fala em 100), a
    paginacao nao pode pular registros nem parar cedo na primeira pagina curta."""
    import api_client

    monkeypatch.setattr(api_client, "PAGE_SIZE", 1000)
    c = _cliente([
        _Resp(200, payload=[{"id": i} for i in range(100)]),  # capou em 100
        _Resp(200, payload=[{"id": i} for i in range(100, 200)]),
        _Resp(200, payload=[{"id": i} for i in range(200, 250)]),
        _Resp(200, payload=[]),
    ])
    itens = c._list_tickets("true", "id asc")
    assert len(itens) == 250
    assert [i["id"] for i in itens] == list(range(250))
    skips = [p["$skip"] for _, p in c.session.chamadas]
    assert skips == [0, 100, 200, 250]
    # depois de aprender o teto, pede paginas de 100 (nao de 1000)
    assert c.session.chamadas[1][1]["$top"] == 100


def test_page_size_padrao_e_mil():
    import config

    assert config.PAGE_SIZE == 1000


def test_list_active_tickets_filtra_status_e_owner():
    """Regressão: `ACTIVE_STATUSES_EXCLUDED` não estava importado e o método estourava."""
    import config

    c = _cliente([_Resp(200, payload=[])])
    assert c.list_active_tickets("lucas@faturagil.com.br") == []
    _, params = c.session.chamadas[0]
    flt = params["$filter"]
    for status in config.ACTIVE_STATUSES_EXCLUDED:
        assert f"baseStatus ne '{status}'" in flt
    assert "owner/email eq 'lucas@faturagil.com.br'" in flt
    assert params["$top"] == config.PAGE_SIZE
    assert "ownerHistories" in params["$expand"]


def test_list_participation_outside_load_filtra_fora_da_carga():
    c = _cliente([_Resp(200, payload=[])])
    c.list_participation_outside_load("lucas@faturagil.com.br")
    flt = c.session.chamadas[0][1]["$filter"]
    assert "actions/any(a: a/createdBy/email eq 'lucas@faturagil.com.br')" in flt
    assert "owner/email ne 'lucas@faturagil.com.br'" in flt
    assert "baseStatus ne 'Resolved'" in flt


def test_list_tickets_changed_since_nao_exclui_resolvidos():
    # O delta precisa trazer tambem quem acabou de mudar para Resolved/Closed/
    # Canceled, senao o _merge_delta nunca os tira de "ativos" e o ticket fica
    # eternamente preso na aba de encerramento (parecendo "vencido").
    c = _cliente([_Resp(200, payload=[])])
    c.list_tickets_changed_since("lucas@faturagil.com.br", "2026-10-13T10:00:00-03:00")
    flt = c.session.chamadas[0][1]["$filter"]
    assert "lastUpdate gt 2026-10-13T10:00:00-03:00" in flt
    assert "baseStatus" not in flt


# -------------------------------------------------------- perguntas de survey
def test_texto_pergunta_prefere_pt_br():
    M = MovideskClient
    pergunta = {
        "id": "XxqG", "type": 1, "isActive": True,
        "languages": [
            {"cultureId": "en-US", "description": "What is your evaluation?"},
            {"cultureId": "pt-BR", "description": "Qual sua avaliação para esse atendimento?"},
        ],
    }
    assert M._texto_pergunta(pergunta) == "Qual sua avaliação para esse atendimento?"
    assert M._texto_pergunta({"languages": []}) == ""
    assert M._texto_pergunta({}) == ""
    # sem pt-BR cai no primeiro idioma com texto
    assert M._texto_pergunta({"languages": [{"cultureId": "en-US", "description": "EN"}]}) == "EN"


def test_list_survey_questions_monta_catalogo():
    c = _cliente([_Resp(200, payload=[{
        "id": "XxqG", "type": 1, "isActive": True,
        "languages": [{"cultureId": "pt-BR", "description": "Qual sua avaliação?"}],
    }, {"id": "", "languages": []}, "lixo"])])
    catalogo = c.list_survey_questions()
    assert catalogo == {"XxqG": {"pergunta": "Qual sua avaliação?", "tipo": 1, "ativo": True}}
    assert c.session.chamadas[0][0].endswith("/survey/questions")


def test_get_survey_question_vazio_nao_chama_api():
    c = _cliente([])
    assert c.get_survey_question("") == {}
    assert c.session.chamadas == []


# ------------------------------------------------------------- mesclas merged
def test_paginas_de_e_ids_mesclados():
    M = MovideskClient
    assert M._paginas_de("1 of 92") == 92
    assert M._paginas_de("2 OF 4") == 4
    assert M._paginas_de("") is None
    assert M._paginas_de("lixo") is None
    assert M._ids_mesclados("11;12") == [11, 12]
    assert M._ids_mesclados("") == []
    assert M._ids_mesclados("11;x;13") == [11, 13]


def test_normaliza_mescla():
    M = MovideskClient
    assert M._normaliza_mescla({"ticketId": "10", "mergedTicketsIds": "11;12",
                                "lastUpdate": "2026-10-05 10:00:00"}) == {
        "ticket_id": 10, "merged_ids": [11, 12], "last_update": "2026-10-05 10:00:00"}
    assert M._normaliza_mescla({"ticketId": ""}) is None
    assert M._normaliza_mescla("lixo") is None


def test_list_merged_tickets_normaliza_e_pagina():
    c = _cliente([
        _Resp(200, payload={
            "startDate": "2026-10-01", "endDate": "2026-10-14", "count": "3",
            "pageNumber": "1 of 2",
            "mergedTickets": [{"ticketId": "10", "mergedTickets": "2",
                               "mergedTicketsIds": "11;12",
                               "lastUpdate": "2026-10-05 10:00:00"}],
        }),
        _Resp(200, payload={
            "count": "3", "pageNumber": "2 of 2",
            "mergedTickets": [{"ticketId": "20", "mergedTickets": "1",
                               "mergedTicketsIds": "21",
                               "lastUpdate": "2026-10-06 10:00:00"}],
        }),
    ])
    r = c.list_merged_tickets("2026-10-01", "2026-10-14", max_pages=5)
    assert r["total"] == 3
    assert r["paginas"] == 2
    assert r["itens"][0] == {"ticket_id": 10, "merged_ids": [11, 12],
                             "last_update": "2026-10-05 10:00:00"}
    assert len(c.session.chamadas) == 2
    assert c.session.chamadas[0][1]["page"] == 1
    assert c.session.chamadas[1][1]["page"] == 2
    assert c.session.chamadas[1][1]["startDate"] == "2026-10-01"


def test_list_merged_tickets_respeita_max_pages():
    paginas = [
        _Resp(200, payload={"count": "500", "pageNumber": "1 of 10",
                            "mergedTickets": [{"ticketId": str(n), "mergedTicketsIds": str(n + 1),
                                               "lastUpdate": ""} for n in range(50)]})
        for _ in range(10)
    ]
    c = _cliente(paginas)
    r = c.list_merged_tickets(max_pages=2)
    assert len(c.session.chamadas) == 2
    assert len(r["itens"]) == 100


def test_get_merged_ticket():
    c = _cliente([_Resp(200, payload={"ticketId": "29611", "mergedTickets": "2",
                                      "mergedTicketsIds": "1;2", "lastUpdate": "2026-09-17 16:30:27"})])
    assert c.get_merged_ticket(29611)["merged_ids"] == [1, 2]
    assert c.session.chamadas[0][1]["id"] == 29611


# --------------------------------------------------------------- htmldescription
def test_get_ticket_html_description_aceita_lista_e_dict():
    c = _cliente([_Resp(200, payload=[{"id": 1, "description": "<p>a</p>"},
                                      {"id": 2, "description": None}])])
    assert c.get_ticket_html_description(78) == [
        {"id": 1, "description": "<p>a</p>"}, {"id": 2, "description": ""}]
    assert c.session.chamadas[0][1]["id"] == 78
    assert "actionId" not in c.session.chamadas[0][1]

    c2 = _cliente([_Resp(200, payload={"id": 3, "description": "<b>b</b>"})])
    assert c2.get_ticket_html_description(78, action_id=3) == [{"id": 3, "description": "<b>b</b>"}]
    assert c2.session.chamadas[0][1]["actionId"] == 3


# ------------------------------------------------------------- base de conhecimento
def test_search_articles_omite_page_na_primeira():
    c = _cliente([_Resp(200, payload={"pageSize": 2, "totalSize": 1,
                                      "items": [{"id": 269455, "title": "Faturamento MANUAL"}]})])
    r = c.search_articles("nfse", page=1, page_size=2)
    assert "page" not in c.session.chamadas[0][1]
    assert c.session.chamadas[0][1]["q"] == "nfse"
    assert r["total"] == 1
    assert r["itens"][0]["title"] == "Faturamento MANUAL"


def test_search_articles_manda_page_quando_maior_que_um():
    c = _cliente([_Resp(200, payload={"totalSize": 40, "items": []})])
    c.search_articles("nfse", page=3, page_size=2)
    assert c.session.chamadas[0][1]["page"] == 3


def test_get_article_devolve_dict():
    c = _cliente([_Resp(200, payload={"id": 199704, "contentHtml": "<p>x</p>"}), _Resp(200, payload=[])])
    assert c.get_article(199704)["id"] == 199704
    assert c.get_article(199704) == {}


# ------------------------------------------------------------------------ anexos
def test_get_ticket_attachments_deduplica_por_hash():
    c = _cliente([_Resp(200, payload=[{"id": 7, "actions": [
        {"id": 1, "attachments": [{"fileName": "a.png", "path": "H1"}]},
        {"id": 2, "attachments": [{"fileName": "a.png", "path": "H1"},
                                  {"fileName": "b.pdf", "path": "H2"}]},
        {"id": 3, "attachments": []},
        "lixo",
    ]}])])
    anexos = c.get_ticket_attachments(7)
    assert len(anexos) == 2
    por_hash = {a["hash"]: a for a in anexos}
    assert por_hash["H1"]["nome"] == "a.png"
    assert por_hash["H1"]["acao_ids"] == [1, 2]
    assert por_hash["H2"]["acao_ids"] == [2]


def test_get_ticket_attachments_sem_ticket():
    c = _cliente([_Resp(200, payload=[])])
    assert c.get_ticket_attachments(1) == []


def test_download_attachment():
    c = _cliente([_Resp(200, content=b"conteudo-do-arquivo")])
    assert c.download_attachment("HASH") == b"conteudo-do-arquivo"
    _, params = c.session.chamadas[0]
    assert params["id"] == "HASH"


def test_download_attachment_sem_hash():
    from api_client import MovideskError

    c = _cliente([])
    try:
        c.download_attachment("")
    except MovideskError:
        pass
    else:
        raise AssertionError("deveria recusar hash vazio")


# --------------------------------------------------- painel: pergunta e mesclas
def test_pergunta_pesquisa_usa_catalogo():
    assert dados.pergunta_pesquisa(
        {"questionId": "XxqG"}, {"XxqG": {"pergunta": "Qual sua avaliação?"}}
    ) == "Qual sua avaliação?"
    assert dados.pergunta_pesquisa({"questionId": "XxqG"}, {"XxqG": "Texto puro"}) == "Texto puro"
    assert dados.pergunta_pesquisa({"questionId": "XxqG"}, {}) == ""
    assert dados.pergunta_pesquisa({}, {"XxqG": {"pergunta": "x"}}) == ""
    assert dados.pergunta_pesquisa(None, None) == ""


def test_build_pesquisa_traz_enunciado_oficial():
    resolved = [{"id": 5, "subject": "Dúvida", "baseStatus": "Resolved",
                 "clients": [{"businessName": "ACME"}]}]
    survey = [{"ticketId": 5, "questionId": "XxqG", "type": 1, "value": 1,
               "responseDate": "2026-10-13T10:00:00"}]
    r = dados.build_pesquisa([], resolved, survey,
                             perguntas={"XxqG": {"pergunta": "Qual sua avaliação?"}})
    item = r["itens"][0]
    assert item["pergunta"] == "Qual sua avaliação?"
    assert item["pergunta_oficial"] is True
    assert item["question_id"] == "XxqG"
    assert item["rotulo"] == "Satisfeito"


def test_build_pesquisa_sem_catalogo_usa_rotulo():
    resolved = [{"id": 5, "subject": "Dúvida", "baseStatus": "Resolved",
                 "clients": [{"businessName": "ACME"}]}]
    survey = [{"ticketId": 5, "questionId": "XxqG", "type": 1, "value": 2,
               "responseDate": "2026-10-13T10:00:00"}]
    item = dados.build_pesquisa([], resolved, survey)["itens"][0]
    assert item["pergunta"] == "Insatisfeito"
    assert item["pergunta_oficial"] is False


_CANDIDATO = {
    "ticket_a": 10, "ticket_b": 11, "subject_a": "A", "subject_b": "B",
    "client": "ACME", "similarity": 0.9, "reason": "Assunto similar (90%)",
    "action": "Mesclar", "ja_vinculado": False, "parent": 10, "merge_into": 11,
    "situation_a": "Aberta", "situation_b": "Aberta",
    "service_a": "Cobranças / Atendimento", "service_b": "Cobranças / Atendimento",
}


def _mesclas_fake(monkeypatch, confirmadas=None, fila=None):
    monkeypatch.setattr(dados, "bucket_do_agente", lambda email: {"ativos": []})
    monkeypatch.setattr(dados, "find_merge_candidates", lambda infos: [dict(_CANDIDATO)])
    monkeypatch.setattr(dados, "load_cache_raw", lambda: {
        "mesclas": fila or {},
        "mesclas_confirmadas": confirmadas or {"salvo_em": "", "itens": []},
    })


def test_build_mesclas_marca_confirmado_no_movidesk(monkeypatch):
    _mesclas_fake(
        monkeypatch,
        confirmadas={"salvo_em": "2026-10-13T10:00:00", "itens": [
            {"ticket_id": 10, "merged_ids": [11], "last_update": "2026-10-05 12:00:00"}]},
        fila={"10:11": {"status": "ignorado", "quando": "2026-10-01T10:00:00"}},
    )
    r = dados.build_mesclas("atual", "lucas@faturagil.com.br")
    assert sorted(r.keys()) == ["agente", "confirmadas", "escopo", "itens", "total"]
    assert r["total"] == 1
    item = r["itens"][0]
    assert item["chave"] == "10:11"
    assert item["confirmado"] is True
    assert item["confirmado_em"] == "2026-10-05 12:00:00"
    assert item["status"] == "ignorado"
    assert item["similaridade"] == 0.9
    assert r["confirmadas"]["total"] == 1
    assert r["confirmadas"]["salvo_em"] == "2026-10-13T10:00:00"


def test_build_mesclas_confirma_na_ordem_inversa(monkeypatch):
    _mesclas_fake(monkeypatch, confirmadas={"itens": [
        {"ticket_id": 11, "merged_ids": [10], "last_update": "2026-10-06 09:00:00"}]})
    item = dados.build_mesclas("atual")["itens"][0]
    assert item["confirmado"] is True
    assert item["confirmado_em"] == "2026-10-06 09:00:00"


def test_build_mesclas_sem_confirmacao(monkeypatch):
    _mesclas_fake(monkeypatch)
    r = dados.build_mesclas("atual")
    item = r["itens"][0]
    assert item["confirmado"] is False
    assert item["confirmado_em"] == ""
    assert item["status"] == ""
    assert r["confirmadas"]["total"] == 0


def test_build_mesclas_cache_sem_itens_nao_quebra(monkeypatch):
    monkeypatch.setattr(dados, "bucket_do_agente", lambda email: {"ativos": []})
    monkeypatch.setattr(dados, "find_merge_candidates", lambda infos: [])
    monkeypatch.setattr(dados, "load_cache_raw", lambda: {"mesclas_confirmadas": "lixo"})
    r = dados.build_mesclas("todos")
    assert r["total"] == 0 and r["itens"] == []
