from datetime import datetime, timedelta

import dados


def _ativo(i, **kw):
    base = {
        "id": i,
        "subject": kw.pop("subject", "Boleto X"),
        "baseStatus": "InProgress",
        "status": kw.pop("status", "Aguardando"),
        "justification": kw.pop("justification", ""),
        "tags": kw.pop("tags", []),
        "clients": [{"businessName": kw.pop("cliente", "Empresa Teste")}],
        "owner": {"businessName": kw.pop("owner_nome", "Lucas"),
                  "email": kw.pop("owner_email", "lucas@faturagil.com.br")},
        "serviceFirstLevel": kw.pop("servico", "Boleto"),
        "lastActionDate": kw.pop("last", "2026-10-08T23:00:00-03:00"),
        "createdDate": kw.pop("created", "2026-10-01T08:00:00-03:00"),
        "resolvedIn": kw.pop("resolvedIn", None),
        "closedIn": kw.pop("closedIn", None),
        "actions": kw.pop("actions", []),
    }
    return base


def _bucket(ativos, resolvidos=()):
    return {"ativos": ativos, "resolvidos": list(resolvidos)}


def _agora():
    return datetime.now().astimezone()


def test_indicadores_sla_fila_fornecedor(monkeypatch):
    agora = _agora()
    cliente_acao = {"createdDate": agora.isoformat(), "createdBy": {"email": "cliente@x.com"}}
    cache = {"por_agente": {"*": _bucket([
        _ativo(1, status="Novo", last=(agora - timedelta(hours=26)).isoformat()),
        _ativo(2, last=agora.isoformat(), actions=[cliente_acao]),
        _ativo(3, justification="Retorno de Fornecedor",
               tags=["fornecedor_pjbank"], last=(agora - timedelta(days=2)).isoformat()),
    ])}}
    monkeypatch.setattr(dados, "load_cache_raw", lambda: cache)

    ind = dados.build_indicadores()
    ids_sla = {x["id"] for x in ind["sla"]["itens"]}
    assert ids_sla == {1}
    assert ind["fila"]["hoje"] == 1
    assert not ind["fila"]["reforco"]
    ids_for = {x["id"] for x in ind["fornecedor"]["itens"]}
    assert ids_for == {3}
    assert ind["fornecedor"]["por_fornecedor"].get("fornecedor_pjbank") == 1
    assert ind["fornecedor"]["itens"][0]["dias"] == 2.0


def test_indicadores_fila_reforco_acima_do_limite(monkeypatch):
    agora = _agora()
    ativos = [_ativo(i, status="Novo", last=(agora - timedelta(minutes=5)).isoformat())
              for i in range(16)]
    monkeypatch.setattr(dados, "load_cache_raw", lambda: {"por_agente": {"*": _bucket(ativos)}})
    ind = dados.build_indicadores()
    assert ind["fila"]["hoje"] == 16
    assert ind["fila"]["reforco"] is True


def test_indicadores_encerramento_implantacao_reclamacao(monkeypatch):
    agora = _agora()
    cache = {"por_agente": {"*": _bucket([
        _ativo(4, justification="Retorno do Cliente - Encerramento",
               last=(agora - timedelta(days=1)).isoformat()),
        _ativo(5, tags=["implantacao_em_andamento"], owner_email="jefferson@faturagil.com.br"),
        _ativo(6, tags=["reclamacao_cliente"], owner_email="lucas@faturagil.com.br"),
    ])}}
    monkeypatch.setattr(dados, "load_cache_raw", lambda: cache)
    monkeypatch.setattr(dados, "carregar_implantados", lambda: [])

    ind = dados.build_indicadores()
    e = ind["retorno_encerrar"]
    assert e["total"] == 1
    assert 3.5 < e["itens"][0]["restantes"] < 4.5
    assert ind["implantacao"]["total"] == 1
    assert ind["reclamacoes"]["por_agente"].get("lucas@faturagil.com.br") == 1


def test_cliente_implantado(monkeypatch):
    monkeypatch.setattr(dados, "carregar_implantados", lambda: ["acme correios"])
    assert dados.cliente_implantado("ACME CORREIOS LTDA")
    assert dados.cliente_implantado("ACME")
    assert not dados.cliente_implantado("Outra Empresa S.A.")


def test_recorrencia_mesmo_cliente_e_assunto(monkeypatch):
    monkeypatch.setattr(dados, "load_cache_raw", lambda: {"por_agente": {"*": _bucket(
        [_ativo(10, subject="Boleto em atraso", cliente="Comercio Alfa")],
        [_ativo(11, subject="Boleto em atraso", cliente="Comercio Alfa",
                resolvedIn="2026-10-05T15:00:00-03:00")],
    )}})
    dados._RECORRENTES.update(at=0, map={})
    rc = dados.recorrencia_de(10)
    assert rc and rc["total"] == 2
    assert rc.get("ultima") == "05/10/2026"


def test_ranking_kpis_por_agente(monkeypatch):
    agora = _agora()
    resolv = _ativo(24, owner_email="lucas@faturagil.com.br",
                    resolvedIn="2026-10-01T10:00:00-03:00")
    resolv["resolvedInFirstCall"] = True
    lucas = {
        "ativos": [
            _ativo(20, tags=["reclamacao_cliente"], owner_email="lucas@faturagil.com.br"),
            _ativo(21, tags=["implantacao_em_andamento"], owner_email="lucas@faturagil.com.br"),
            _ativo(22, justification="Retorno de Fornecedor",
                   tags=["fornecedor_c6_bank"], owner_email="lucas@faturagil.com.br"),
            _ativo(23, owner_email="lucas@faturagil.com.br",
                   last=(agora - timedelta(days=9)).isoformat()),
        ],
        "resolvidos": [resolv],
    }
    monkeypatch.setattr(dados, "load_cache_raw",
                        lambda: {"por_agente": {"lucas@faturagil.com.br": lucas}, "survey": []})
    r = dados.build_ranking()
    p = next(x for x in r["itens"] if x["email"] == "lucas@faturagil.com.br")
    assert p["reclamacoes"] == 1
    assert p["implantacao"] == 1
    assert p["fornecedor"] == 1
    assert p["antigos_7d"] == 1
    assert p["fcr"] == 1 and p["fcr_pct"] == 100