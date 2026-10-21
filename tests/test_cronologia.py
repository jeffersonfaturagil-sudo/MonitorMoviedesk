"""Regressão da linha do tempo de um ticket (dados.montar_cronologia).

Cobre a montagem da cronologia a partir das ações cruas da API: ordenação,
autor, tipo (nota interna x pública), canal, resumo e o tempo entre interações.
"""
from datetime import timedelta

import dados

NOW = dados.parse_date("2026-10-15T12:00:00-03:00")

EQUIPE = {"businessName": "Lucas", "email": "lucas@faturagil.com.br"}
CLIENTE = {"businessName": "Joana", "email": "joana@cliente.com"}


def _acao(quando_antes_de_agora_h, createdBy=None, tipo=2, origin=2, desc="ok"):
    d = NOW - timedelta(hours=quando_antes_de_agora_h)
    return {
        "createdDate": d.strftime("%Y-%m-%dT%H:%M:%S-03:00"),
        "createdBy": createdBy if createdBy is not None else EQUIPE,
        "type": tipo,
        "origin": origin,
        "description": desc,
    }


def test_cronologia_ordena_e_mede_intervalos():
    acts = [
        _acao(1, EQUIPE),      # mais recente
        _acao(20, CLIENTE),    # abertura
        _acao(5, EQUIPE),      # meio
    ]
    c = dados.montar_cronologia(acts)
    its = c["itens"]

    assert [it["horas_desde_anterior"] for it in its] == [None, 15.0, 4.0]
    assert c["resumo"]["total"] == 3
    assert c["resumo"]["intervalo_medio_h"] == 9.5
    assert c["resumo"]["maior_intervalo_h"] == 15.0


def test_cronologia_resumo_ultima_por_e_tipos():
    acts = [
        _acao(3, CLIENTE, tipo=2, origin=5),
        _acao(2, EQUIPE, tipo=1, origin=9),   # nota interna via API
        _acao(1, CLIENTE, tipo=2, origin=5),
    ]
    c = dados.montar_cronologia(acts)
    r = c["resumo"]

    assert r["internas"] == 1
    assert r["publicas"] == 2
    assert r["ultima_por"] == "Joana"
    assert r["ultima_equipe"] is False
    assert r["ultima_interna"] is False
    assert r["horas_desde_ultima"] is not None and r["horas_desde_ultima"] > 0
    assert c["itens"][1]["interno"] is True
    assert c["itens"][1]["publico"] is False
    assert c["itens"][1]["canal"] == "API"


def test_cronologia_primeira_resposta_equipe():
    acts = [
        _acao(30, CLIENTE, tipo=1, origin=1),
        _acao(28, CLIENTE, tipo=1, origin=1),
        _acao(20, EQUIPE, tipo=2, origin=2),
        _acao(2, EQUIPE, tipo=2, origin=2),
    ]
    c = dados.montar_cronologia(acts)["resumo"]
    assert c["primeira_resposta_em"] == dados.montar_cronologia(acts)["itens"][2]["data"]


def test_cronologia_autor_e_equipe_e_descr_unescape():
    acts = [
        _acao(2, CLIENTE, desc="Boleto &amp; nota"),
        _acao(1, {"email": "sem_nome@x.com"}, desc=""),
    ]
    its = dados.montar_cronologia(acts)["itens"]
    assert its[0]["autor"] == "Joana"
    assert its[0]["equipe"] is False
    assert its[0]["descricao"] == "Boleto & nota"
    assert its[1]["autor"] == "sem_nome@x.com"


def test_cronologia_vazia():
    c = dados.montar_cronologia([])
    assert c["itens"] == []
    assert c["resumo"]["total"] == 0
    assert c["resumo"]["intervalo_medio_h"] is None
    assert c["resumo"]["horas_desde_ultima"] is None
    assert c["resumo"]["ultima_por"] == ""
