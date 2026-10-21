from datetime import datetime, timedelta

import dados


def _tk(ident, created, resolved=None, fcr=False, reopened=None, owner="lucas@faturagil.com.br"):
    t = {
        "id": ident,
        "subject": "teste",
        "baseStatus": "Resolved" if resolved else "New",
        "clients": [{"businessName": "Cliente X"}],
        "owner": {"email": owner, "businessName": owner.split("@")[0].capitalize()},
        "createdDate": created,
        "resolvedInFirstCall": fcr,
    }
    if resolved:
        t["resolvedIn"] = resolved
    if reopened:
        t["reopenedIn"] = reopened
    return t


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S-03:00")


def _raw(entries, survey=None):
    return {
        "agentes": [{"nome": "Lucas", "email": "lucas@faturagil.com.br"}],
        "historico": entries,
        "survey": survey or [],
        "por_agente": {},
    }


def _metricas(bloco):
    return {m["nome"]: m for m in bloco["metricas"]}


def test_comparativo_delta_resolvidos(monkeypatch):
    now = datetime(2026, 10, 13, 12, 0)
    entries = [
        _tk(1, _iso(now - timedelta(days=20)), _iso(now - timedelta(days=3))),
        _tk(2, _iso(now - timedelta(days=20)), _iso(now - timedelta(days=2))),
        _tk(3, _iso(now - timedelta(days=20)), _iso(now - timedelta(days=1))),
        _tk(4, _iso(now - timedelta(days=20)), _iso(now - timedelta(days=10))),
    ]
    monkeypatch.setattr(dados, "load_cache_raw", lambda: _raw(entries))
    dados.definir_agente("*")
    try:
        comp = dados.build_comparativo(now)
        m = _metricas(comp["semana"])["Resolvidos"]
        assert m["atual"] == 3
        assert m["anterior"] == 1
        assert m["delta"] == 2
        assert m["pct"] == 200
    finally:
        dados.definir_agente("*")


def test_comparativo_fcr_e_tempo_medio(monkeypatch):
    now = datetime(2026, 10, 13, 12, 0)
    entries = [
        _tk(1, _iso(now - timedelta(days=3, hours=10)), _iso(now - timedelta(days=3)), fcr=True),
        _tk(2, _iso(now - timedelta(days=2, hours=20)), _iso(now - timedelta(days=2))),
    ]
    monkeypatch.setattr(dados, "load_cache_raw", lambda: _raw(entries))
    dados.definir_agente("*")
    try:
        comp = dados.build_comparativo(now)
        m = _metricas(comp["semana"])
        assert m["FCR (1º contato)"]["atual"] == 50
        assert m["Tempo médio de resolução"]["atual"] == 15.0
    finally:
        dados.definir_agente("*")


def test_comparativo_satisfacao_usando_survey(monkeypatch):
    now = datetime(2026, 10, 13, 12, 0)
    entries = [_tk(1, _iso(now - timedelta(days=3)), _iso(now - timedelta(days=3)))]
    survey = [
        {"ticketId": 1, "type": 1, "value": 1, "responseDate": _iso(now - timedelta(days=2))},
    ]
    monkeypatch.setattr(dados, "load_cache_raw", lambda: _raw(entries, survey))
    dados.definir_agente("*")
    try:
        comp = dados.build_comparativo(now)
        m = _metricas(comp["semana"])["Satisfação"]
        assert m["atual"] == 100
    finally:
        dados.definir_agente("*")


def test_comparativo_respeita_escopo_do_atendente(monkeypatch):
    now = datetime(2026, 10, 13, 12, 0)
    entries = [
        _tk(1, _iso(now - timedelta(days=3)), _iso(now - timedelta(days=2)), owner="lucas@faturagil.com.br"),
        _tk(2, _iso(now - timedelta(days=3)), _iso(now - timedelta(days=2)), owner="jefferson@faturagil.com.br"),
    ]
    monkeypatch.setattr(dados, "load_cache_raw", lambda: _raw(entries))
    dados.definir_agente("lucas@faturagil.com.br")
    try:
        comp = dados.build_comparativo(now)
        assert comp["escopo"] == "perfil"
        assert comp["total_indice"] == 1
        assert _metricas(comp["semana"])["Resolvidos"]["atual"] == 1
    finally:
        dados.definir_agente("*")


def test_comparativo_vazio(monkeypatch):
    monkeypatch.setattr(dados, "load_cache_raw", lambda: _raw([]))
    dados.definir_agente("*")
    try:
        comp = dados.build_comparativo(datetime(2026, 10, 13, 12, 0))
        assert comp["total_indice"] == 0
        assert _metricas(comp["semana"])["Resolvidos"]["atual"] == 0
        assert _metricas(comp["semana"])["Resolvidos"]["anterior"] == 0
    finally:
        dados.definir_agente("*")
