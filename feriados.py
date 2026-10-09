"""Feriados e dias uteis para a agenda.

Considera feriados nacionais brasileiros (fixos + moveis, calculados a partir
da Pascoa) e feriados extras informados pelo usuario:

- arquivo `feriados.txt` na raiz do projeto (uma data por linha, AAAA-MM-DD);
- variavel `MOVIDESK_FERIADOS` no .env (datas separadas por virgula).

Ex.: preferencia por trabalhar em feriado? basta nao cadastrar / remover a data.
"""

from __future__ import annotations

import os
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _pascoa(ano: int) -> date:
    """Domingo de Pascoa (algoritmo Meeus/Jones/Butcher, calendario gregoriano)."""
    a = ano % 19
    b = ano // 100
    c = ano % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    mes = (h + l - 7 * m + 114) // 31
    dia = ((h + l - 7 * m + 114) % 31) + 1
    return date(ano, mes, dia)


@lru_cache(maxsize=None)
def feriados_do_ano(ano: int) -> frozenset[date]:
    """Feriados nacionais do ano (fixos + Carnaval, Sexta-feira Santa, Corpus Christi)."""
    fixos = [
        (1, 1),    # Confraternizacao Universal
        (4, 21),   # Tiradentes
        (5, 1),    # Dia do Trabalho
        (9, 7),    # Independencia
        (10, 12),  # Nossa Senhora Aparecida
        (11, 2),   # Finados
        (11, 15),  # Proclamacao da Republica
        (11, 20),  # Consciencia Negra (nacional desde 2024)
        (12, 25),  # Natal
    ]
    dados = {date(ano, m, d) for m, d in fixos}
    p = _pascoa(ano)
    dados.add(p - timedelta(days=48))  # Segunda de Carnaval
    dados.add(p - timedelta(days=47))  # Terca de Carnaval
    dados.add(p - timedelta(days=2))   # Sexta-feira Santa
    dados.add(p + timedelta(days=60))  # Corpus Christi
    return frozenset(dados)


def _parse_data(valor: str) -> date | None:
    valor = (valor or "").strip()
    if not valor:
        return None
    for fmt in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            from datetime import datetime

            return datetime.strptime(valor, fmt).date()
        except ValueError:
            continue
    return None


@lru_cache(maxsize=1)
def feriados_extras() -> frozenset[date]:
    """Feriados cadastrados manualmente (arquivo feriados.txt + env MOVIDESK_FERIADOS)."""
    datas: set[date] = set()

    arquivo = BASE_DIR / "feriados.txt"
    if arquivo.exists():
        try:
            for linha in arquivo.read_text(encoding="utf-8").splitlines():
                linha = linha.split("#", 1)[0].strip()
                d = _parse_data(linha)
                if d:
                    datas.add(d)
        except OSError:
            pass

    env = os.environ.get("MOVIDESK_FERIADOS", "")
    if not env:
        try:
            from config import _cfg  # le tambem o .env

            env = _cfg("MOVIDESK_FERIADOS")
        except Exception:
            env = ""
    for parte in env.split(","):
        d = _parse_data(parte)
        if d:
            datas.add(d)

    return frozenset(datas)


def eh_feriado(d: date) -> bool:
    return d in feriados_do_ano(d.year) or d in feriados_extras()


def eh_dia_util(d: date) -> bool:
    """Segunda a sexta que nao seja feriado."""
    return d.weekday() < 5 and not eh_feriado(d)


def ultimo_dia_util_antes(ref: date) -> date:
    """Dia util imediatamente anterior a `ref` (pula fim de semana e feriado)."""
    d = ref - timedelta(days=1)
    while not eh_dia_util(d):
        d -= timedelta(days=1)
    return d


def dias_fechados_entre(inicio: date, fim: date) -> list[date]:
    """Dias nao uteis (fim de semana/feriado) em [inicio, fim)."""
    dias = []
    d = inicio
    while d < fim:
        if not eh_dia_util(d):
            dias.append(d)
        d += timedelta(days=1)
    return dias
