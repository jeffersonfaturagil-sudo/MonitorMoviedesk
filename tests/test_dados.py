from datetime import date as _RealDate, datetime, timedelta

import dados


def _tk(i, dia):
    return {
        "id": i,
        "subject": "teste",
        "baseStatus": "Resolved",
        "resolvedIn": dia + "T15:00:00-03:00",
        "clients": [{"businessName": "Cliente X"}],
        "owner": {"businessName": "Lucas"},
        "createdDate": "2026-09-01T08:00:00-03:00",
    }


def test_agenda_13_out_pula_fds_e_feriado():
    now = datetime(2026, 10, 13, 8, 0)
    res = [_tk(i, d) for i, d in [
        (1, "2026-10-07"),
        (2, "2026-10-09"),
        (3, "2026-10-10"),
        (4, "2026-10-11"),
        (5, "2026-10-12"),  # feriado
    ]]
    ag = dados.build_agenda([], res, [], [], now, agente_nome="Lucas")
    assert ag["resumo"]["resolvidos"] == 4
    assert {t["id"] for t in ag["resolvidos"]} == {2, 3, 4, 5}
    assert ag["periodo"]["de_iso"] == "2026-10-09"
    assert ag["periodo"]["ate_iso"] == "2026-10-12"
    assert ag["periodo"]["dias"] == 4
    assert "10/10/2026" in ag["periodo"]["fechados"]
    assert "11/10/2026" in ag["periodo"]["fechados"]
    assert "12/10/2026" in ag["periodo"]["fechados"]
    assert "07/10/2026" not in ag["periodo"]["fechados"]
    assert ag["tecnico"] == "Lucas"


def test_agenda_nao_inclui_tickets_fora_do_periodo():
    now = datetime(2026, 10, 13, 8, 0)
    res = [_tk(i, d) for i, d in [(1, "2026-10-06"), (2, "2026-10-09"), (3, "2026-10-13")]]
    ag = dados.build_agenda([], res, [], [], now, agente_nome="Lucas")
    assert {t["id"] for t in ag["resolvidos"]} == {2}  # 13/10 ainda nao entrou


def test_agenda_sexta_comum_usa_quinta():
    now = datetime(2026, 10, 9, 8, 0)
    ag = dados.build_agenda([], [], [], [], now, agente_nome="Lucas")
    assert ag["periodo"]["de_iso"] == "2026-10-08"
    assert ag["periodo"]["dias"] == 1


def test_agenda_modo_hoje_pega_do_zero_ate_agora():
    now = datetime(2026, 10, 13, 14, 30)
    res = [_tk(i, d) for i, d in [
        (1, "2026-10-09"),  # dia anterior - fora
        (2, "2026-10-13"),  # hoje - entra
        (3, "2026-10-14"),  # futuro - fora
    ]]
    ag = dados.build_agenda([], res, [], [], now, agente_nome="Lucas", modo="hoje")
    assert {t["id"] for t in ag["resolvidos"]} == {2}
    assert ag["periodo"]["modo"] == "hoje"
    assert ag["periodo"]["de_iso"] == "2026-10-13"
    assert ag["periodo"]["ate_iso"] == "2026-10-13"
    assert ag["periodo"]["dias"] == 1
    assert ag["periodo"]["fechados"] == []
    assert ag["periodo"]["ate_hora"] == "14:30"

    assert ag["periodo"]["fechados"] == []


def test_plantao_dias_so_uteis_pula_fim_de_semana_e_feriado():
    raw = {"agentes": [{"email": "lucas@faturagil.com.br", "nome": "Lucas Bernardi"}],
           "por_agente": {}}
    por_data = {
        "2026-10-16": "lucas@faturagil.com.br",  # sexta
        "2026-10-17": "lucas@faturagil.com.br",  # sábado (não útil)
        "2026-10-19": "lucas@faturagil.com.br",  # segunda
        "2026-10-20": "jefferson@faturagil.com.br",
    }
    dias = dados._plantao_dias(por_data, raw, dados.date(2026, 10, 16), 3,
                               "2026-10-19", so_uteis=True)
    assert [d["data"] for d in dias] == ["2026-10-16", "2026-10-19", "2026-10-20"]
    assert all(d["util"] for d in dias)
    assert dias[1]["nome"] == "Lucas Bernardi"
    assert dias[1]["hoje"] is True
    assert dias[2]["nome"] == "Jefferson Teles de Oliveira"

    corridos = dados._plantao_dias(por_data, raw, dados.date(2026, 10, 16), 3,
                                   "2026-10-19")
    assert [d["data"] for d in corridos] == ["2026-10-16", "2026-10-17", "2026-10-18"]
    assert corridos[1]["util"] is False


def test_build_plantao_expoe_resumo_hoje_e_proximos(monkeypatch):
    ativos = [
        _ativo(1, owner={"businessName": "Lucas", "email": "lucas@faturagil.com.br"},
               createdDate="2026-10-19T08:10:00-03:00"),
        _ativo(2, owner={"businessName": "Jefferson", "email": "jefferson@faturagil.com.br"},
               createdDate="2026-10-19T09:20:00-03:00"),
        _ativo(3, owner={}, createdDate="2026-10-19T10:30:00-03:00"),
        _ativo(4, owner={"businessName": "Lucas", "email": "lucas@faturagil.com.br"},
               createdDate="2026-10-18T10:30:00-03:00"),  # ontem - fora
    ]
    raw = {
        "agentes": [{"email": "lucas@faturagil.com.br", "nome": "Lucas Bernardi"}],
        "salvo_em": "2026-10-19T11:00:00-03:00",
        "plantao": {"por_data": {"2026-10-19": "lucas@faturagil.com.br",
                                 "2026-10-20": "jefferson@faturagil.com.br"},
                    "nota": "plantão do suporte"},
        "por_agente": {"*": {"ativos": ativos, "resolvidos": []}},
    }
    monkeypatch.setattr(dados, "load_cache_raw", lambda: raw)
    monkeypatch.setattr(dados, "date", _FakeDate)

    p = dados.build_plantao(hoje_iso="2026-10-19")
    assert p["hoje"] == "2026-10-19"
    assert p["real_hoje"] == "2026-10-19"
    assert p["hoje_email"] == "lucas@faturagil.com.br"
    assert p["hoje_nome"] == "Lucas Bernardi"
    assert p["nota"] == "plantão do suporte"
    assert p["total_ativos_indice"] == 4
    assert p["resumo"] == {"novos": 3, "do_plantao": 1, "sem_responsavel": 1,
                           "resolvidos": 0, "outros": 1}
    assert [n["ticket"] for n in p["novos"]] == [1, 3, 2]
    prox = [d["data"] for d in p["proximos"]]
    assert prox[0] == "2026-10-19"
    assert not any(d in prox for d in ("2026-10-24", "2026-10-25"))
    assert any(not d["util"] for d in p["dias"])


def test_canal_nome():
    assert dados.canal_nome(5) == "Chat"
    assert dados.canal_nome(13) == "Ligação atendida"
    assert dados.canal_nome(15) == "Ligação perdida"
    assert dados.canal_nome(23) == "WhatsApp Business"
    assert dados.canal_nome("lixo") == ""
    assert dados.canal_nome(None) == ""


def test_tokens_normaliza_acento_e_stopwords():
    tok = dados._tokens("Não consigo emitir a Nota Fiscal do cliente")
    assert "consigo" in tok
    assert "emitir" in tok
    assert "nota" in tok
    assert "fiscal" in tok
    # "nao" e "cliente" sao stopwords do dominio (nao entram no indice)
    assert "nao" not in tok
    assert "cliente" not in tok
    assert all(len(w) >= 3 for w in tok)


def test_build_parecidos_ticket_inexistente():
    res = dados.build_parecidos(999999999)
    assert "erro" in res


def _ativo(i, **kw):
    base = {
        "id": i,
        "subject": "assunto de teste",
        "baseStatus": "New",
        "clients": [{"businessName": "Cliente X"}],
        "owner": {"businessName": "Lucas"},
        "createdDate": "2026-10-01T08:00:00-03:00",
        "lastActionDate": "2026-10-10T08:00:00-03:00",
    }
    base.update(kw)
    return base


class _FakeDate:
    """Substituto de ``datetime.date`` fixado em 2026-10-19 para os testes."""

    _hoje = _RealDate(2026, 10, 19)

    @classmethod
    def today(cls):
        return cls._hoje

    @classmethod
    def fromisoformat(cls, s):
        return _RealDate.fromisoformat(s)


def test_classificar_bloqueio():
    assert dados.classificar_bloqueio({}, "Aguarda cliente") == "cliente"
    assert dados.classificar_bloqueio({"justification": "Dev 1 - Em Análise"}, "") == "dev"
    assert dados.classificar_bloqueio({"tags": ["fornecedor_bradesco"]}, "") == "fornecedor"
    assert dados.classificar_bloqueio({"justification": "Retorno de Fornecedor"}, "") == "fornecedor"
    assert dados.classificar_bloqueio({}, "Precisa sua resposta") == "atendente"
    assert dados.classificar_bloqueio({}, "") == "indefinido"


def test_ticket_dict_expoe_risco_e_bloqueio():
    from analyzer import build_ticket_info

    now = datetime(2026, 10, 14, 8, 0)
    info = build_ticket_info(_ativo(1, urgency="Urgente"), now)
    d = dados.ticket_dict(info)
    assert d["risco"] == "ALTO"
    assert d["bloqueio"] in dados.BLOQUEIOS


def test_ticket_dict_inclui_guia_do_playbook():
    from analyzer import build_ticket_info

    now = datetime(2026, 10, 14, 8, 0)
    info = build_ticket_info(
        _ativo(1, justification="Aguardando » Equipe de análise"), now)
    d = dados.ticket_dict(info)
    assert d["guia"]["responsavel"] == "Engenharia e Tecnologia"
    assert d["guia"]["do_suporte"] is False
    assert d["guia"]["regra"]


def test_guia_do_ticket_decide_pelo_status_e_nao_pelo_assunto():
    # Assunto "como mesclar tickets" não muda o responsável: o status manda.
    g = dados.guia_do_ticket({"subject": "como mesclar", "status": "Novo"})
    assert g["responsavel"] == "Suporte"
    g2 = dados.guia_do_ticket({"justification": "Aguardando » Retorno de Fornecedor"})
    assert g2["responsavel"] == "Fornecedor"


def test_ticket_por_id_acha_em_qualquer_bucket(monkeypatch):
    raw = {"por_agente": {"*": {"ativos": [_ativo(7)]},
                          "lucas@faturagil.com.br": {"resolvidos": [_ativo(9)]}}}
    monkeypatch.setattr(dados, "load_cache_raw", lambda: raw)
    assert dados.ticket_por_id(7)["id"] == 7
    assert dados.ticket_por_id("9")["id"] == 9
    assert dados.ticket_por_id(123) is None



def test_build_payload_separa_aguardando_cliente_dos_criticos():
    now = datetime(2026, 10, 14, 8, 0)
    # ticket cujo último contato foi da equipe -> aguarda cliente (mesmo urgente)
    cliente = _ativo(1, urgency="Urgente", actions=[
        {"createdDate": "2026-10-10T09:00:00-03:00",
         "createdBy": {"email": "lucas@faturagil.com.br"}},
    ])
    # ticket travado com o time de dev -> risco ALTO, não aguarda cliente
    dev = _ativo(2, urgency="Urgente", justification="Dev 1 - Em Análise")

    p = dados.build_payload([cliente, dev], now=now)

    assert p["resumo"]["aguardando_cliente"] == 1
    assert [t["id"] for t in p["aguardando_cliente"]] == [1]
    ids_risco = [t["id"] for t in p["criticos_risco"]]
    assert 1 not in ids_risco
    assert 2 in ids_risco
    # campos novos do resumo existem
    for k in ("risco_alto", "risco_medio", "criticos_risco", "aguardando_dev"):
        assert k in p["resumo"]

def test_abas_ok_e_terceiros_usam_eixos_novos_sem_quebrar_o_trio_do_tempo():
    """OK = risco baixo; aguardando terceiros = bola fora do Suporte."""
    now = datetime(2026, 10, 14, 8, 0)
    tranquilo = _ativo(1, urgency="baixa", lastActionDate="2026-10-14T07:30:00-03:00",
                       actions=[{"createdDate": "2026-10-14T07:30:00-03:00",
                                 "createdBy": {"email": "lucas@faturagil.com.br"}}])
    dev = _ativo(2, urgency="Urgente", justification="Dev 1 - Em Análise")

    p = dados.build_payload([tranquilo, dev], now=now)

    # eixo risco: só o recente é baixo -> é ele que a aba OK passa a mostrar
    assert [t["id"] for t in p["ok_risco"]] == [1]
    assert p["resumo"]["ok_risco"] == 1
    # eixo "quem segura o ticket": cliente e dev entram em aguardando_terceiros
    assert {t["id"] for t in p["aguardando_terceiros"]} == {1, 2}
    assert p["resumo"]["aguardando_terceiros"] == 2
    assert 2 not in [t["id"] for t in p["ok_risco"]]
    # trio do tempo parado continua partindo o cache inteiro, sem repetição
    trio = ([t["id"] for t in p["criticos"]] + [t["id"] for t in p["atencao"]]
            + [t["id"] for t in p["ok"]])
    assert sorted(trio) == [1, 2]
    assert len(trio) == len(set(trio))


def test_acao_sugerida_sinaliza_ligacao_de_continuidade_na_aba_ok():
    # a guia usa o relógio de verdade (guia_do_ticket), então a ação fica 3h atrás
    atraso = (datetime.now().astimezone() - timedelta(hours=3)).isoformat(timespec="seconds")
    tk = _ativo(7, urgency="baixa", justification="Retorno do Cliente - Continuidade",
                lastActionDate=atraso,
                actions=[{"createdDate": atraso,
                          "createdBy": {"email": "lucas@faturagil.com.br"}}])

    p = dados.build_payload([tk], now=datetime.now())

    ok = [t for t in p["ok_risco"] if t["id"] == 7]
    assert ok, "risco baixo deve cair na aba OK"
    acao = ok[0].get("acao")
    assert acao and acao["tipo"] == "acao"
    assert acao["motivo"].startswith("Ligar agora")


def test_acao_sugerida_separa_acompanhamento_de_dev_e_fornecedor():
    now = datetime(2026, 10, 14, 8, 0)
    dev = _ativo(3, urgency="baixa", justification="Dev 1 - Em Análise")
    forn = _ativo(4, urgency="baixa", tags=["fornecedor_bradesco"],
                  lastActionDate="2026-10-14T07:00:00-03:00",
                  actions=[{"createdDate": "2026-10-14T07:00:00-03:00",
                            "createdBy": {"email": "lucas@faturagil.com.br"}}])
    # bola com o atendente: o pill de bloqueio já avisa, não gera "acao" extra
    resposta = _ativo(5, urgency="baixa",
                      lastActionDate="2026-10-14T07:00:00-03:00",
                      actions=[{"createdDate": "2026-10-14T07:00:00-03:00",
                                "createdBy": {"email": "cliente@exemplo.com"}}])

    p = dados.build_payload([dev, forn, resposta], now=now)
    todos = {t["id"]: t for t in (p["criticos"] + p["atencao"] + p["ok"]
                                  + p["ok_risco"] + p["aguardando_terceiros"])}

    assert todos[3]["acao"]["motivo"] == "Cobrar o dev e avisar o cliente"
    assert todos[4]["acao"]["motivo"] == "Atualizar o cliente 1x/dia"
    assert todos[3]["acao"]["tipo"] == todos[4]["acao"]["tipo"] == "acompanhar"
    assert "acao" not in todos[5]
