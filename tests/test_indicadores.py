from datetime import datetime, timedelta, timezone

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


class _Congelado:
    """Substitui ``dados.datetime`` com o ``now()`` fixo nos testes.

    Qualquer outro atributo/``staticmethod`` cai no ``datetime`` de verdade,
    então só o relógio é congelado.
    """
    def __init__(self, quando):
        object.__setattr__(self, "_quando", quando)

    def now(self, tz=None):
        q = self._quando
        return q.astimezone(tz) if tz else q

    def __getattr__(self, nome):
        return getattr(datetime, nome)


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
    # Relógio congelado: o prazo de encerramento passou a ser contado em dias
    # úteis, e "restantes" depende do dia da semana (expediente, não corrida).
    agora = datetime(2026, 10, 16, 10, 0, tzinfo=timezone(timedelta(hours=-3)))  # sexta
    monkeypatch.setattr(dados, "datetime", _Congelado(agora))
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
    # Base = 15/10 (quinta). 5 dias ÚTEIS -> 22/10; 5 dias corridos -> 20/10.
    assert e["itens"][0]["prazo"] == "22/10/2026"
    # Sexta 10h até quinta 10h = 4 dias úteis de expediente.
    assert e["itens"][0]["restantes"] == 4.0
    assert e["perto_prazo"] == 0
    assert ind["implantacao"]["total"] == 1
    assert ind["reclamacoes"]["por_agente"].get("lucas@faturagil.com.br") == 1


def test_indicadores_encerramento_conta_no_prazo_zero(monkeypatch):
    """``restantes == 0`` ainda conta como "perto do prazo" (o `or 99` antigo não contava)."""
    from horario import add_dias_uteis

    base = datetime(2026, 10, 15, 10, 0, tzinfo=timezone(timedelta(hours=-3)))  # quinta
    prazo = add_dias_uteis(base, 5)  # 22/10
    monkeypatch.setattr(dados, "datetime", _Congelado(prazo))
    cache = {"por_agente": {"*": _bucket([
        _ativo(7, justification="Retorno do Cliente - Encerramento", last=base.isoformat()),
    ])}}
    monkeypatch.setattr(dados, "load_cache_raw", lambda: cache)
    monkeypatch.setattr(dados, "carregar_implantados", lambda: [])

    e = dados.build_indicadores()["retorno_encerrar"]
    assert e["total"] == 1
    assert e["itens"][0]["restantes"] == 0.0
    assert e["perto_prazo"] == 1


def _cache_perfis():
    """Carga de dois atendentes + bucket '*' (time inteiro)."""
    agora = _agora()
    jeff_for = _ativo(1, justification="Retorno de Fornecedor", tags=["fornecedor_c6_bank"],
                      owner_email="jefferson@faturagil.com.br")
    lucas_imp = _ativo(2, tags=["implantacao_em_andamento"],
                       owner_email="lucas@faturagil.com.br")
    lucas_for = _ativo(3, justification="Retorno de Fornecedor",
                       owner_email="lucas@faturagil.com.br")
    lucas_sla = _ativo(4, status="Novo", owner_email="lucas@faturagil.com.br",
                       last=(agora - timedelta(hours=26)).isoformat())
    return {"por_agente": {
        "*": _bucket([jeff_for, lucas_imp, lucas_for, lucas_sla]),
        "jefferson@faturagil.com.br": _bucket([jeff_for]),
        "lucas@faturagil.com.br": _bucket([lucas_imp, lucas_for, lucas_sla]),
    }}


def test_fornecedor_so_aparece_na_carga_do_perfil(monkeypatch):
    monkeypatch.setattr(dados, "load_cache_raw", _cache_perfis)
    monkeypatch.setattr(dados, "carregar_implantados", lambda: [])

    jeff = dados.build_indicadores("jefferson@faturagil.com.br")
    assert jeff["escopo"] == "carga"
    assert not jeff["carga_sem_cache"]
    # O ticket 3 é do Lucas: fora da carga do Jefferson, nem aparece.
    assert {x["id"] for x in jeff["fornecedor"]["itens"]} == {1}
    assert jeff["fornecedor"]["por_fornecedor"] == {"fornecedor_c6_bank": 1}

    lucas = dados.build_indicadores("lucas@faturagil.com.br")
    assert {x["id"] for x in lucas["fornecedor"]["itens"]} == {3}

    todos = dados.build_indicadores()
    assert todos["escopo"] == "todos"
    assert {x["id"] for x in todos["fornecedor"]["itens"]} == {1, 3}


def test_implantacao_so_aparece_na_carga_do_perfil(monkeypatch):
    monkeypatch.setattr(dados, "load_cache_raw", _cache_perfis)
    monkeypatch.setattr(dados, "carregar_implantados", lambda: [])

    jeff = dados.build_indicadores("jefferson@faturagil.com.br")
    # Nada de implantação na carga do Jefferson: o indicador fica zerado.
    assert jeff["implantacao"]["total"] == 0
    assert jeff["implantacao"]["itens"] == []

    lucas = dados.build_indicadores("lucas@faturagil.com.br")
    assert {x["id"] for x in lucas["implantacao"]["itens"]} == {2}

    assert dados.build_indicadores()["implantacao"]["total"] == 1


def test_sla_e_fila_continuam_globais_mesmo_com_perfil(monkeypatch):
    """Fila/SLA são alerta de time, não de uma pessoa: não escopam."""
    monkeypatch.setattr(dados, "load_cache_raw", _cache_perfis)
    monkeypatch.setattr(dados, "carregar_implantados", lambda: [])
    jeff = dados.build_indicadores("jefferson@faturagil.com.br")
    assert 4 in {x["id"] for x in jeff["sla"]["itens"]}, "ticket do Lucas fora da carga dele"
    assert jeff["escopo"] == "carga"


def test_implantacao_explica_fluxo_para_o_suporte(monkeypatch):
    monkeypatch.setattr(dados, "load_cache_raw", _cache_perfis)
    monkeypatch.setattr(dados, "carregar_implantados", lambda: [])
    ind = dados.build_indicadores("jefferson@faturagil.com.br")
    imp = ind["implantacao"]
    assert imp["papel"] == "suporte"
    assert "implantador" in imp["fluxo"]
    # Sem perfil não há papel: nada de mandar alguém encaminhar.
    assert dados.build_indicadores()["implantacao"]["fluxo"] == ""


def test_perfil_sem_cache_de_carga_e_sinalizado(monkeypatch):
    """Perfil nunca consultado não pode parecer que não há nada em aberto."""
    cache = _cache_perfis()
    cache["por_agente"].pop("jefferson@faturagil.com.br")
    monkeypatch.setattr(dados, "load_cache_raw", lambda: cache)
    monkeypatch.setattr(dados, "carregar_implantados", lambda: [])
    ind = dados.build_indicadores("jefferson@faturagil.com.br")
    assert ind["escopo"] == "carga"
    assert ind["carga_sem_cache"] is True
    assert ind["fornecedor"]["sem_cache"] is True
    assert ind["fornecedor"]["total"] == 0
    assert ind["implantacao"]["sem_cache"] is True


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
            _ativo(20, tags=["reclamacao_cliente"], owner_email="lucas@faturagil.com.br",
                   last=(agora - timedelta(hours=1)).isoformat()),
            _ativo(21, tags=["implantacao_em_andamento"], owner_email="lucas@faturagil.com.br",
                   last=(agora - timedelta(hours=2)).isoformat()),
            _ativo(22, justification="Retorno de Fornecedor",
                   tags=["fornecedor_c6_bank"], owner_email="lucas@faturagil.com.br",
                   last=(agora - timedelta(hours=3)).isoformat()),
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


def test_ranking_periodo_e_risco(monkeypatch):
    agora = _agora()
    recente = _ativo(30, owner_email="lucas@faturagil.com.br",
                     resolvedIn=(agora - timedelta(days=5)).isoformat())
    recente["resolvedInFirstCall"] = True
    antigo = _ativo(31, owner_email="lucas@faturagil.com.br",
                    resolvedIn=(agora - timedelta(days=200)).isoformat())
    antigo["reopenedIn"] = (agora - timedelta(days=199)).isoformat()
    lucas = {"ativos": [], "resolvidos": [recente, antigo], "salvo_em": agora.isoformat()}
    monkeypatch.setattr(dados, "load_cache_raw",
                        lambda: {"por_agente": {"lucas@faturagil.com.br": lucas}, "survey": []})
    r = dados.build_ranking(periodo_dias=30)
    p = r["itens"][0]
    assert p["resolvidos"] == 1
    assert p["fcr"] == 1
    assert p["reabertos"] == 0
    assert p["stale_dias"] is not None
    assert p["trend"][3] == 1


def test_ranking_exclui_bucket_time_e_inclui_risco(monkeypatch):
    agora = _agora()
    ativos = [
        _ativo(40, owner_email="lucas@faturagil.com.br", last=agora.isoformat()),
        _ativo(41, owner_email="lucas@faturagil.com.br", last=agora.isoformat()),
    ]
    ativos[0]["urgency"] = "Urgente"
    ativos[1]["urgency"] = "Baixa"
    monkeypatch.setattr(dados, "load_cache_raw", lambda: {
        "por_agente": {
            "*": {"ativos": ativos, "resolvidos": []},
            "lucas@faturagil.com.br": {"ativos": ativos, "resolvidos": []},
        },
        "survey": [],
    })
    r = dados.build_ranking()
    assert r["total"] == 1
    p = r["itens"][0]
    assert p["email"] == "lucas@faturagil.com.br"
    assert p["risco_alto"] == 1
    assert p["risco_medio"] == 0