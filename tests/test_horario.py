"""Testes do calendário de horário comercial (painel/horario.py).

Fixa o fuso de São Paulo e usa dias úteis conhecidos de outubro/2026.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

import horario

TZ = ZoneInfo("America/Sao_Paulo")


def dt(s):
    return datetime.fromisoformat(s).replace(tzinfo=TZ)


def test_janela_padrao():
    assert horario.JANELAS == ((8 * 60, 12 * 60), (13 * 60, 18 * 60))
    assert horario.descricao_janela() == "08:00-12:00 e 13:00-18:00"
    assert horario.minutos_por_dia() == 540


def test_minutos_uteis_no_mesmo_dia():
    assert horario.minutos_uteis_entre(dt("2026-10-15T09:00"), dt("2026-10-15T11:00")) == 120


def test_minutos_uteis_atravessa_almoco():
    # 11:30-12:00 (30) + 13:00-14:00 (60)
    assert horario.minutos_uteis_entre(dt("2026-10-15T11:30"), dt("2026-10-15T14:00")) == 90


def test_minutos_uteis_pula_fim_de_semana():
    # sex 17:00-18:00 (60) + seg 08:00-09:00 (60)
    assert horario.minutos_uteis_entre(dt("2026-10-16T17:00"), dt("2026-10-19T09:00")) == 120


def test_minutos_uteis_negativo():
    r = horario.minutos_uteis_entre(dt("2026-10-15T10:00"), dt("2026-10-15T09:00"))
    assert r == -60


def test_add_minutos_uteis_atravessa_almoco():
    assert horario.add_minutos_uteis(dt("2026-10-15T11:30"), 60) == dt("2026-10-15T13:30")


def test_add_minutos_uteis_atravessa_fim_de_semana():
    assert horario.add_minutos_uteis(dt("2026-10-16T17:30"), 60) == dt("2026-10-19T08:30")


def test_add_dias_uteis():
    assert horario.add_dias_uteis(dt("2026-10-15T10:00"), 5) == dt("2026-10-22T10:00")


def test_add_dias_uteis_pula_feriado():
    # 30/10/2026 é sexta; 02/11 é Finados (segunda) -> cai em 03/11 (terça)
    assert horario.add_dias_uteis(dt("2026-10-30T10:00"), 1) == dt("2026-11-03T10:00")


def test_proximo_expediente_sabado():
    assert horario.proximo_expediente(dt("2026-10-17T10:00")) == dt("2026-10-19T08:00")


def test_proximo_expediente_dentro_da_janela_nao_muda():
    d = dt("2026-10-15T10:00")
    assert horario.proximo_expediente(d) == d


def test_minutos_uteis_entre_span_multidias():
    # seg 09:00 -> qui 09:00 = 480 (seg) + 540 (ter) + 540 (qua) + 60 (qui)
    assert horario.minutos_uteis_entre(dt("2026-10-19T09:00"), dt("2026-10-22T09:00")) == 1620
