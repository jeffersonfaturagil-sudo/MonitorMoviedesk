from datetime import datetime, timezone

from analyzer import build_ticket_info, client_name, hours_between, nivel_risco, parse_date


def _tk(**kw):
    base = {
        "id": 1,
        "subject": "Dúvida nota de emissão",
        "baseStatus": "Open",
        "owner": {"businessName": "Lucas"},
        "clients": [{"businessName": "Cliente X"}],
        "createdDate": "2026-10-09T08:00:00-03:00",
    }
    base.update(kw)
    return base


def test_client_name():
    assert client_name(_tk()) == "Cliente X"
    assert client_name({"clients": []}) == "N/D"
    assert client_name({}) == "N/D"


def test_parse_date_isodate_com_fuso():
    d = parse_date("2026-10-09T08:00:00-03:00")
    assert d is not None
    assert d.astimezone(timezone.utc).hour == 11
    assert d.astimezone(timezone.utc).date().isoformat() == "2026-10-09"


def test_parse_date_isodate_com_fracao_e_fuso():
    d = parse_date("2026-10-09T08:00:00.500123-03:00")
    assert d is not None
    assert d.astimezone(timezone.utc).hour == 11
    assert d.astimezone(timezone.utc).minute == 0


def test_parse_date_vazio_ou_invalido():
    assert parse_date(None) is None
    assert parse_date("") is None
    assert parse_date("data invalida") is None


def test_hours_between():
    a = datetime(2026, 10, 9, 8, 0, tzinfo=timezone.utc)
    b = datetime(2026, 10, 9, 12, 0, tzinfo=timezone.utc)
    assert hours_between(a, b) == 4.0
    assert hours_between(None, b) is None


def test_build_ticket_info_basico():
    t = build_ticket_info(_tk(), datetime(2026, 10, 9, 18, 0))
    assert t.id == 1
    assert t.client == "Cliente X"
    assert t.base_status == "Open"
    assert "nota" in t.subject.lower()


def test_build_ticket_info_resolvido():
    t = build_ticket_info(
        _tk(
            id=2,
            baseStatus="Resolved",
            resolvedIn="2026-10-09T15:00:00-03:00",
            lastUpdate="2026-10-09T15:00:00-03:00",
        ),
        datetime(2026, 10, 9, 18, 0),
    )
    assert t.base_status == "Resolved"
    assert t.last_action is not None


def test_parse_date_sentinela_0001_eh_ignorada():
    assert parse_date("0001-01-01T00:00:00") is None
    assert parse_date("0001-01-01T00:00:00-03:00") is None


def test_nivel_risco_por_urgencia():
    agora = datetime(2026, 10, 14, 12, 0)
    urgente = build_ticket_info(_tk(urgency="Urgente", lastActionDate="2026-10-14T11:00:00-03:00"), agora)
    alta = build_ticket_info(_tk(urgency="Alta", lastActionDate="2026-10-14T11:00:00-03:00"), agora)
    baixa = build_ticket_info(_tk(urgency="Baixa", lastActionDate="2026-10-14T11:00:00-03:00"), agora)
    assert nivel_risco(urgente) == "ALTO"
    assert nivel_risco(alta) == "MEDIO"
    assert nivel_risco(baixa) == "BAIXO"


def test_nivel_risco_parado_uma_semana():
    agora = datetime(2026, 10, 14, 12, 0)
    velho = build_ticket_info(_tk(urgency="Baixa", lastActionDate="2026-10-01T11:00:00-03:00"), agora)
    assert nivel_risco(velho) == "MEDIO"