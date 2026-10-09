from datetime import datetime

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
    assert ag["periodo"]["fechados"] == []


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