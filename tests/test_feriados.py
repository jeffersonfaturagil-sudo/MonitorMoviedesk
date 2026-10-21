from datetime import date, timedelta

import feriados as f


def test_12_outubro_2026_e_feriado():
    assert f.eh_feriado(date(2026, 10, 12))
    assert not f.eh_dia_util(date(2026, 10, 12))


def test_09_outubro_2026_e_dia_util():
    assert not f.eh_feriado(date(2026, 10, 9))
    assert f.eh_dia_util(date(2026, 10, 9))


def test_fim_de_semana_nao_e_dia_util():
    assert not f.eh_dia_util(date(2026, 10, 10))  # sabado
    assert not f.eh_dia_util(date(2026, 10, 11))  # domingo


def test_ultimo_dia_util_pula_fds_e_feriado():
    # terca 13/10 volta para sexta 09/10 (10 e 11 fim de semana, 12 feriado)
    assert f.ultimo_dia_util_antes(date(2026, 10, 13)) == date(2026, 10, 9)
    # sabado e domingo tambem apontam para sexta
    assert f.ultimo_dia_util_antes(date(2026, 10, 10)) == date(2026, 10, 9)
    assert f.ultimo_dia_util_antes(date(2026, 10, 11)) == date(2026, 10, 9)
    # o proprio feriado aponta para o dia util anterior
    assert f.ultimo_dia_util_antes(date(2026, 10, 12)) == date(2026, 10, 9)


def test_ultimo_dia_util_segunda_comum():
    # segunda 05/10 so volta para sexta 02/10 (dia anterior util era sexta)
    assert f.ultimo_dia_util_antes(date(2026, 10, 5)) == date(2026, 10, 2)


def test_ultimo_dia_util_dia_util_comum():
    assert f.ultimo_dia_util_antes(date(2026, 10, 8)) == date(2026, 10, 7)


def test_pascoa_dias_conhecidos():
    assert f._pascoa(2024) == date(2024, 3, 31)
    assert f._pascoa(2025) == date(2025, 4, 20)
    assert f._pascoa(2026) == date(2026, 4, 5)


def test_carnaval_e_feriado():
    p = f._pascoa(2026)
    assert f.eh_feriado(p - timedelta(days=48))  # segunda de carnaval
    assert f.eh_feriado(p - timedelta(days=47))  # terca de carnaval


def test_feriado_natal():
    assert f.eh_feriado(date(2026, 12, 25))
    assert f.eh_feriado(date(2027, 1, 1))