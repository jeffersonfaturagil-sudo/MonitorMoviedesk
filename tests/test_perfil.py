import dados


def _tk(ident, subject, owner_email, cliente="Cliente A"):
    nome = owner_email.split("@")[0].capitalize()
    return {
        "id": ident,
        "subject": subject,
        "baseStatus": "Resolved",
        "resolvedIn": "2026-09-02T15:00:00-03:00",
        "clients": [{"businessName": cliente}],
        "owner": {"email": owner_email, "businessName": nome},
        "createdDate": "2026-09-01T08:00:00-03:00",
    }


def _raw():
    lucas = "lucas@faturagil.com.br"
    jeff = "jefferson@faturagil.com.br"
    return {
        "agentes": [
            {"nome": "Lucas", "email": lucas},
            {"nome": "Jefferson", "email": jeff},
        ],
        "historico": [
            _tk(1, "Boleto pendente", lucas, "Cliente A"),
            _tk(2, "Boleto vencido", jeff, "Cliente B"),
            _tk(5, "NF-e rejeitada", lucas, "Cliente A"),
        ],
        "por_agente": {
            "*": {"ativos": [_tk(6, "Boleto atual", lucas, "Cliente A")], "resolvidos": [_tk(3, "NFS-e emitida", lucas, "Cliente C")]},
            jeff: {"ativos": [], "resolvidos": [_tk(4, "NFS-e cancelada", jeff, "Cliente D")]},
        },
    }


def _ids(q):
    return [i["ticket"] for i in dados.buscar_historico(q).get("itens", [])]


def test_historico_com_perfil_filtra_do_atendente(monkeypatch):
    monkeypatch.setattr(dados, "load_cache_raw", lambda: _raw())
    dados.definir_agente("lucas@faturagil.com.br")
    try:
        assert set(_ids("boleto")) == {1}
        assert set(_ids("nfs")) == {3}
        total = dados.buscar_historico("").get("total_indice")
        assert total == 3
    finally:
        dados.definir_agente("")


def test_historico_sem_perfil_traz_todos(monkeypatch):
    monkeypatch.setattr(dados, "load_cache_raw", lambda: _raw())
    dados.definir_agente("*")
    try:
        assert set(_ids("boleto")) == {1, 2}
        assert set(_ids("nfs")) == {3, 4}
    finally:
        dados.definir_agente("")


def test_parecidos_respeita_escopo_do_atendente(monkeypatch):
    monkeypatch.setattr(dados, "load_cache_raw", lambda: _raw())
    dados.definir_agente("lucas@faturagil.com.br")
    try:
        res = dados.build_parecidos(6)
        assert "erro" not in res
        assert {i["ticket"] for i in res["itens"]} == {1}
    finally:
        dados.definir_agente("")


def test_parecidos_sem_perfil_traz_todos(monkeypatch):
    monkeypatch.setattr(dados, "load_cache_raw", lambda: _raw())
    dados.definir_agente("*")
    try:
        res = dados.build_parecidos(6)
        assert "erro" not in res
        assert {i["ticket"] for i in res["itens"]} == {1, 2}
    finally:
        dados.definir_agente("")