"""Regressão das abas de relatório de gestão (tempo, gargalos, inteligência).

O backend vive em ``dados.build_relatorio`` e é a fonte do endpoint
``/api/relatorio`` consumido pelo painel. Estes testes montam um cache sintético
e fixam "agora" para não depender do relógio.
"""
from datetime import datetime, timedelta

import dados


NOW = datetime(2026, 10, 15, 12, 0)


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S-03:00")


def _tk(ident, created, **kw):
    t = {
        "id": ident,
        "subject": "Boleto nao registrado",
        "baseStatus": "New",
        "clients": [{"businessName": "Cliente X"}],
        "owner": {"businessName": "Lucas", "email": "lucas@faturagil.com.br"},
        "createdDate": _iso(created),
    }
    t.update(kw)
    return t


def _raw(tickets, survey=None, agentes=None):
    return {
        "agentes": agentes or [{"nome": "Lucas", "email": "lucas@faturagil.com.br"}],
        "por_agente": {dados.CHAVE_TODOS: {"ativos": tickets, "resolvidos": []}},
        "historico": [],
        "survey": survey or [],
        "services": [],
    }


def _preparar(monkeypatch, raw):
    monkeypatch.setattr(dados, "load_cache_raw", lambda *a, **k: raw)
    monkeypatch.setattr(dados, "_rel_now", lambda: NOW.astimezone())
    monkeypatch.setattr(dados, "_map_recorrentes", lambda *a, **k: {})
    dados.definir_agente("*")


# ---------------------------------------------------------------- tempo

def test_tempo_conta_criados_resolvidos_e_fcr(monkeypatch):
    tickets = [
        _tk(1, NOW - timedelta(days=5), resolvedIn=_iso(NOW - timedelta(days=1)),
            resolvedInFirstCall=True, actionCount=1),
        _tk(2, NOW - timedelta(days=5), resolvedIn=_iso(NOW - timedelta(days=2)),
            actionCount=3),
        _tk(3, NOW - timedelta(days=5)),  # ainda aberto
    ]
    _preparar(monkeypatch, _raw(tickets))
    r = dados.build_relatorio_tempo(30)

    assert r["criados"] == 3
    assert r["resolvidos"] == 2
    assert r["fcr"] == 1
    assert r["fcr_pct"] == 50
    assert r["resolucao"]["n"] == 2
    assert r["contatos"]["n"] == 2


def test_tempo_exclui_fora_do_periodo(monkeypatch):
    tickets = [
        _tk(1, NOW - timedelta(days=40), resolvedIn=_iso(NOW - timedelta(days=35))),
        _tk(2, NOW - timedelta(days=3), resolvedIn=_iso(NOW - timedelta(days=1))),
    ]
    _preparar(monkeypatch, _raw(tickets))
    r = dados.build_relatorio_tempo(30)
    assert r["criados"] == 1
    assert r["resolvidos"] == 1


def test_tempo_primeira_resposta_so_com_acoes(monkeypatch):
    cliente = {"email": "cliente@exemplo.com"}
    interno = {"email": "lucas@faturagil.com.br"}
    created = NOW - timedelta(days=2)
    t = _tk(
        1, created,
        createdBy=cliente,
        actions=[{"createdDate": _iso(created + timedelta(hours=2)), "createdBy": interno}],
        resolvedIn=_iso(NOW - timedelta(days=1)),
    )
    sem_acao = _tk(2, NOW - timedelta(days=2), createdBy=cliente,
                   resolvedIn=_iso(NOW - timedelta(days=1)))
    _preparar(monkeypatch, _raw([t, sem_acao]))
    r = dados.build_relatorio_tempo(30)
    assert r["primeira_resposta"]["n"] == 1
    assert r["primeira_resposta"]["media"] == 2.0


def test_tempo_csat_e_negativos(monkeypatch):
    t = _tk(1, NOW - timedelta(days=3), resolvedIn=_iso(NOW - timedelta(days=1)))
    survey = [
        {"ticketId": 1, "responseDate": _iso(NOW - timedelta(days=1)),
         "type": 1, "value": 2, "commentary": "Demorou muito"},
        {"ticketId": 1, "responseDate": _iso(NOW - timedelta(hours=5)),
         "type": 1, "value": 1},
    ]
    _preparar(monkeypatch, _raw([t], survey=survey))
    r = dados.build_relatorio_tempo(30)
    assert r["csat"]["satisfeitos"] == 1
    assert r["csat"]["insatisfeitos"] == 1
    assert r["csat"]["respondidos"] == 2
    assert r["csat"]["pct"] == 50
    assert len(r["negativos"]) == 1
    assert r["negativos"][0]["rotulo"] == "Insatisfeito"


# ------------------------------------------------------------- gargalos

def _ticket_com_hist(ident, created, hist):
    return _tk(ident, created, ownerHistories=[
        {"changedDate": _iso(d), "ownerTeam": team} for d, team in hist])


def test_gargalos_encaminhados_e_devolvidos(monkeypatch):
    hist = [
        (NOW - timedelta(days=10), dados.SETOR_SUPORTE),
        (NOW - timedelta(days=8), dados.SETOR_DEV),
        (NOW - timedelta(days=5), dados.SETOR_SUPORTE),
    ]
    t = _ticket_com_hist(1, NOW - timedelta(days=12), hist)
    _preparar(monkeypatch, _raw([t]))
    g = dados.build_relatorio_gargalos(30)

    enc = {x["setor"]: x["n"] for x in g["encaminhados"]}
    assert enc.get(dados.SETOR_DEV) == 1
    assert g["devolvidos_total"] == 1
    assert g["devolvidos"][0]["de"] == dados.SETOR_DEV
    setores = {x["setor"] for x in g["tempo_setor"]}
    assert dados.SETOR_SUPORTE in setores and dados.SETOR_DEV in setores
    assert g["internos_vinculados"]["disponivel"] is False


def test_gargalos_tempo_setor_usa_intervalos(monkeypatch):
    hist = [
        (NOW - timedelta(days=10), dados.SETOR_SUPORTE),
        (NOW - timedelta(days=9), dados.SETOR_DEV),
    ]
    t = _ticket_com_hist(1, NOW - timedelta(days=11), hist)
    _preparar(monkeypatch, _raw([t]))
    g = dados.build_relatorio_gargalos(30)
    por_setor = {x["setor"]: x["stats"] for x in g["tempo_setor"]}
    assert por_setor[dados.SETOR_SUPORTE]["n"] == 1
    assert por_setor[dados.SETOR_SUPORTE]["media"] == 24.0  # 1 dia entre as trocas


def test_gargalos_cobertura(monkeypatch):
    com = _ticket_com_hist(1, NOW - timedelta(days=3),
                           [(NOW - timedelta(days=2), dados.SETOR_SUPORTE)])
    sem = _tk(2, NOW - timedelta(days=3))
    _preparar(monkeypatch, _raw([com, sem]))
    g = dados.build_relatorio_gargalos(30)
    assert g["cobertura"] == {"setores": 1, "total": 2}


# ---------------------------------------------------------- inteligencia

def test_inteligencia_top_categorias_e_incidentes(monkeypatch):
    tickets = [
        _tk(1, NOW - timedelta(days=2), category="Dúvida", serviceFirstLevel="Nota Fiscal (NFS-e)"),
        _tk(2, NOW - timedelta(days=2), category="Dúvida", serviceFirstLevel="Nota Fiscal (NFS-e)"),
        _tk(3, NOW - timedelta(days=1), category="Instabilidade", serviceFirstLevel="Boletos"),
    ]
    _preparar(monkeypatch, _raw(tickets))
    i = dados.build_relatorio_inteligencia(30)
    cats = {x["nome"]: x["n"] for x in i["top_categorias"]}
    assert cats["Dúvida"] == 2
    assert i["incidentes"] == [{"nome": "Instabilidade", "n": 1}]
    erros = {x["modulo"]: x["n"] for x in i["erros_recorrentes"]}
    assert erros["Boletos"] == 1
    assert i["versoes_disponivel"] is False


def test_inteligencia_clientes_sequencia(monkeypatch):
    tickets = [
        _tk(n, NOW - timedelta(days=6) + timedelta(hours=n),
            clients=[{"businessName": "Cliente Y"}])
        for n in range(1, 5)
    ]
    _preparar(monkeypatch, _raw(tickets))
    i = dados.build_relatorio_inteligencia(30)
    seq = {x["cliente"]: x for x in i["clientes_sequencia"]}
    assert seq["Cliente Y"]["total"] == 4
    assert seq["Cliente Y"]["rajada_7d"] == 4


def test_inteligencia_picos_acima_do_limiar(monkeypatch):
    tickets = []
    ident = 0
    # quatro dias "normais" com 3 chamados cada
    for dias in (10, 9, 8, 7):
        for _ in range(3):
            ident += 1
            tickets.append(_tk(ident, NOW - timedelta(days=dias)))
    # um dia de pico bem acima dos demais
    for _ in range(20):
        ident += 1
        tickets.append(_tk(ident, NOW - timedelta(days=3)))
    _preparar(monkeypatch, _raw(tickets))
    i = dados.build_relatorio_inteligencia(30)
    assert i["picos"]["perfil"]["max_dia"] == 20
    assert any(x["n"] == 20 for x in i["picos"]["dias"])


# ------------------------------------------------------------ agregador

def test_build_relatorio_shape_e_avisos(monkeypatch):
    t = _tk(1, NOW - timedelta(days=2), resolvedIn=_iso(NOW - timedelta(days=1)))
    _preparar(monkeypatch, _raw([t]))
    r = dados.build_relatorio(30, agente="*")

    assert set(("gerado_em", "dias", "agente", "escopo", "avisos",
                "tempo", "gargalos", "inteligencia")) <= set(r)
    assert r["escopo"] == "todos"
    assert r["dias"] == 30
    assert len(r["avisos"]) == 5
    assert any("parentTickets" in a for a in r["avisos"])


def test_build_relatorio_escopo_por_agente(monkeypatch):
    tickets = [
        _tk(1, NOW - timedelta(days=2), owner={"businessName": "Lucas", "email": "lucas@faturagil.com.br"},
            resolvedIn=_iso(NOW - timedelta(days=1))),
        _tk(2, NOW - timedelta(days=2), owner={"businessName": "Jeff", "email": "jeff@faturagil.com.br"},
            resolvedIn=_iso(NOW - timedelta(days=1))),
    ]
    _preparar(monkeypatch, _raw(tickets))
    r = dados.build_relatorio(30, agente="lucas@faturagil.com.br")
    assert r["escopo"] == "perfil"
    assert r["tempo"]["resolvidos"] == 1
    dados.definir_agente("*")


def test_build_relatorio_dias_invalido_cai_em_30(monkeypatch):
    _preparar(monkeypatch, _raw([_tk(1, NOW - timedelta(days=2))]))
    r = dados.build_relatorio("abc", agente="*")
    assert r["dias"] == 30
