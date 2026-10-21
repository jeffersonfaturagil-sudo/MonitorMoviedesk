"""Calendário de horário comercial para o relógio de SLA.

O Movidesk não devolve prazos/vencimentos nesta conta, então o SLA é calculado
por nós sobre uma janela de horário comercial. Regras:

- A janela vem de ``config.HORARIO_COMERCIAL`` (padrão 08:00-12:00 e 13:00-18:00).
- Fins de semana e feriados (``feriados.eh_dia_util``) não contam.
- Todo cálculo usa o fuso do ``datetime`` de entrada (os tickets vêm em -03:00,
  então o resultado também fica em -03:00).

Funções principais:
- ``minutos_uteis_entre(inicio, fim)`` — minutos de expediente entre dois instantes
  (negativo se ``fim`` for antes de ``inicio``).
- ``add_minutos_uteis(inicio, minutos)`` — instante após somar N minutos úteis.
- ``add_dias_uteis(inicio, n)`` — mesmo horário, N dias úteis depois.
- ``proximo_expediente(dt)`` — empurra um instante parado (fora da janela) para o
  início do próximo expediente.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

from feriados import eh_dia_util

MINUTOS_DIA = 24 * 60
_UM_DIA = timedelta(days=1)
_LIMITE_DIAS = 4000  # trava de segurança (≈11 anos) contra laços infinitos


def _parse_janelas(bruto: str) -> tuple[tuple[int, int], ...]:
    janelas = []
    for parte in (bruto or "").split(","):
        parte = parte.strip()
        if "-" not in parte:
            continue
        ini, fim = parte.split("-", 1)
        try:
            h1, m1 = (int(x) for x in ini.strip().split(":"))
            h2, m2 = (int(x) for x in fim.strip().split(":"))
        except (ValueError, TypeError):
            continue
        a, b = h1 * 60 + m1, h2 * 60 + m2
        if 0 <= a < b <= MINUTOS_DIA:
            janelas.append((a, b))
    janelas.sort()
    return tuple(janelas) or ((8 * 60, 12 * 60), (13 * 60, 18 * 60))


try:
    from config import HORARIO_COMERCIAL

    JANELAS = _parse_janelas(HORARIO_COMERCIAL)
except Exception:  # pragma: no cover - fallback se config indisponível
    JANELAS = ((8 * 60, 12 * 60), (13 * 60, 18 * 60))


def descricao_janela() -> str:
    partes = [f"{a // 60:02d}:{a % 60:02d}-{b // 60:02d}:{b % 60:02d}" for a, b in JANELAS]
    return " e ".join(partes)


def minutos_por_dia() -> int:
    """Minutos úteis em um dia útil cheio (soma das janelas)."""
    return sum(b - a for a, b in JANELAS)


def _min_do_dia(dt: datetime) -> int:
    return dt.hour * 60 + dt.minute


def _em_dia_util(dt: datetime) -> bool:
    return eh_dia_util(dt.date())


def _inicio_dia_util_seguinte(dt: datetime) -> datetime:
    """Meia-noite do próximo dia útil depois de ``dt`` (mesmo fuso)."""
    d = dt.date() + _UM_DIA
    guarda = 0
    while not eh_dia_util(d):
        d += _UM_DIA
        guarda += 1
        if guarda > _LIMITE_DIAS:
            break
    return datetime.combine(d, time(0, 0), tzinfo=dt.tzinfo)


def proximo_expediente(dt: datetime) -> datetime:
    """Primeiro instante de expediente >= ``dt`` (empurra fim de semana/feriado/fora da janela)."""
    d = dt
    guarda = 0
    while True:
        guarda += 1
        if guarda > _LIMITE_DIAS:
            return d
        if not _em_dia_util(d):
            d = datetime.combine(_inicio_dia_util_seguinte(d).date(), time(0, 0), tzinfo=dt.tzinfo)
            continue
        m = _min_do_dia(d)
        for w0, w1 in JANELAS:
            if m < w0:
                return d.replace(hour=w0 // 60, minute=w0 % 60, second=0, microsecond=0)
            if w0 <= m < w1:
                return d
        # depois da última janela do dia -> próximo expediente
        d = datetime.combine(_inicio_dia_util_seguinte(d).date(), time(0, 0), tzinfo=dt.tzinfo)


def minutos_uteis_entre(inicio: datetime | None, fim: datetime | None) -> float | None:
    """Minutos de expediente entre dois instantes (negativo se ``fim`` < ``inicio``)."""
    if inicio is None or fim is None:
        return None
    if fim < inicio:
        r = minutos_uteis_entre(fim, inicio)
        return -r if r is not None else None
    tz = inicio.tzinfo
    total = 0.0
    d = inicio.date()
    fim_d = fim.date()
    guarda = 0
    while d <= fim_d and guarda <= _LIMITE_DIAS:
        guarda += 1
        if eh_dia_util(d):
            for w0, w1 in JANELAS:
                a = datetime.combine(d, time(w0 // 60, w0 % 60), tzinfo=tz)
                b = datetime.combine(d, time(w1 // 60, w1 % 60), tzinfo=tz)
                seg = a if a > inicio else inicio
                term = b if b < fim else fim
                if term > seg:
                    total += (term - seg).total_seconds() / 60.0
        d += _UM_DIA
    return total


def add_minutos_uteis(inicio: datetime, minutos: float) -> datetime:
    """Instante resultante de somar ``minutos`` de expediente a partir de ``inicio``."""
    if minutos < 0:
        return _sub_minutos_uteis(inicio, -minutos)
    restante = float(minutos)
    d = proximo_expediente(inicio)
    guarda = 0
    while restante > 0 and guarda <= _LIMITE_DIAS:
        guarda += 1
        m = _min_do_dia(d)
        avancou = False
        for w0, w1 in JANELAS:
            if m < w0:
                d = d.replace(hour=w0 // 60, minute=w0 % 60, second=0, microsecond=0)
                m = w0
            if w0 <= m < w1:
                disponivel = w1 - m
                if restante <= disponivel:
                    return d + timedelta(minutes=restante)
                restante -= disponivel
                d = d.replace(hour=w1 // 60, minute=w1 % 60, second=0, microsecond=0)
                avancou = True
                break
            avancou = True
        if restante <= 0:
            return d
        if not avancou or _min_do_dia(d) >= JANELAS[-1][1]:
            d = datetime.combine(_inicio_dia_util_seguinte(d).date(), time(0, 0), tzinfo=inicio.tzinfo)
    return d


def _sub_minutos_uteis(inicio: datetime, minutos: float) -> datetime:
    restante = float(minutos)
    d = inicio
    if not _em_dia_util(d) or _min_do_dia(d) <= JANELAS[0][0]:
        d = proximo_expediente(d) - _UM_DIA
        d = d.replace(hour=JANELAS[-1][1] // 60, minute=JANELAS[-1][1] % 60, second=0, microsecond=0)
    guarda = 0
    while restante > 0 and guarda <= _LIMITE_DIAS:
        guarda += 1
        m = _min_do_dia(d)
        for w0, w1 in reversed(JANELAS):
            if m > w1:
                d = d.replace(hour=w1 // 60, minute=w1 % 60, second=0, microsecond=0)
                m = w1
            if w0 < m <= w1:
                disponivel = m - w0
                if restante <= disponivel:
                    return d - timedelta(minutes=restante)
                restante -= disponivel
                d = d.replace(hour=w0 // 60, minute=w0 % 60, second=0, microsecond=0)
                break
        if restante <= 0:
            return d
        # recua um dia inteiro
        anterior = d.date() - _UM_DIA
        while not eh_dia_util(anterior):
            anterior -= _UM_DIA
        d = datetime.combine(anterior, time(JANELAS[-1][1] // 60, JANELAS[-1][1] % 60),
                             tzinfo=inicio.tzinfo)
    return d


def add_dias_uteis(inicio: datetime, n: int) -> datetime:
    """Mesmo horário, ``n`` dias úteis depois (fins de semana/feriados pulados)."""
    d = inicio
    restantes = max(0, int(n))
    guarda = 0
    while restantes > 0 and guarda <= _LIMITE_DIAS:
        guarda += 1
        d += _UM_DIA
        if eh_dia_util(d.date()):
            restantes -= 1
    return d
