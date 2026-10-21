#!/usr/bin/env python3
"""Testador manual de um ou mais testcase-especificos.

Nao descobre testes automaticamente. Serve para rodar um ou mais modulos de
teste escolhidos via CLI, com suporte minimo a fixtures (monkeypatch e tmp_path)
para os casos que pedem.

Uso:
    python3 run_tests_manual.py                         # roda tests.test_comparativo
    python3 run_tests_manual.py tests.test_time        # roda outro modulo
    python3 run_tests_manual.py tests.test_cache tests.test_time  # varios modulos
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for p in (ROOT, ROOT / "painel"):
    s = str(p)
    if s not in sys.path:
        sys.path.insert(0, s)

import tempfile
import traceback


class MonkeyPatch:
    """Substituto minimo do fixture monkeypatch do pytest."""

    _SENTINELA = object()

    def __init__(self):
        self._undo: list = []

    def setattr(self, obj, name, value=True):
        antigo = getattr(obj, name, self._SENTINELA)
        self._undo.append((obj, name, antigo))
        setattr(obj, name, value)

    def delattr(self, obj, name, raising=True):
        antigo = getattr(obj, name, self._SENTINELA)
        if antigo is self._SENTINELA and raising:
            raise AttributeError(name)
        self._undo.append((obj, name, antigo))
        try:
            delattr(obj, name)
        except AttributeError:
            pass

    def setenv(self, name, value):
        antigo = os.environ.get(name, self._SENTINELA)
        self._undo.append((os.environ, name, antigo))
        os.environ[name] = str(value)

    def delenv(self, name, raising=True):
        antigo = os.environ.get(name, self._SENTINELA)
        if antigo is self._SENTINELA and raising:
            raise KeyError(name)
        self._undo.append((os.environ, name, antigo))
        os.environ.pop(name, None)

    def undo(self):
        for obj, name, antigo in reversed(self._undo):
            if antigo is self._SENTINELA:
                try:
                    delattr(obj, name)
                except AttributeError:
                    pass
            else:
                setattr(obj, name, antigo)
        self._undo = []


import os


def _fixture(nome: str, monkeypatch: MonkeyPatch, tmp_path: Path):
    if nome == "monkeypatch":
        return monkeypatch
    if nome in ("tmp_path", "tmpdir"):
        return tmp_path
    raise RuntimeError(f"fixture nao suportado pelo runner manual: {nome}")


def _importar_mod(nome: str):
    partes = nome.replace(".py", "").split(".")
    if partes and partes[0] == "tests":
        modulos = partes[1:] if len(partes) > 1 else ["_pacote_vazio"]
        pkg = "tests"
    else:
        pkg = ""
        modulos = partes
    try:
        mod = __import__(nome, fromlist=modulos if modulos else ["_"])
    except Exception as exc:
        print(f"nao foi possivel importar {nome}: {exc}")
        sys.exit(2)
    return mod


def _rodar_modulo(modulo):
    total = falhas = 0
    erros = []
    for nome in sorted(n for n in dir(modulo) if n.startswith("test_")):
        fn = getattr(modulo, nome)
        if not callable(fn):
            continue
        total += 1
        mp = MonkeyPatch()
        tmp = Path(tempfile.mkdtemp(prefix="painel_test_"))
        try:
            params = inspect.signature(fn).parameters
            kwargs = {p: _fixture(p, mp, tmp) for p in params}
            fn(**kwargs)
            print(f"ok   {modulo.__name__}::{nome}")
        except Exception:
            falhas += 1
            erros.append((modulo.__name__, nome, traceback.format_exc()))
            print(f"FAIL {modulo.__name__}::{nome}")
        finally:
            mp.undo()
    return total, falhas, erros


import inspect


def main():
    if len(sys.argv) < 2:
        sys.argv.append("tests.test_comparativo")
    total_global = falhas_global = 0
    todos_erros = []
    for arg in sys.argv[1:]:
        mod = _importar_mod(arg)
        t, f, errs = _rodar_modulo(mod)
        total_global += t
        falhas_global += f
        todos_erros.extend(errs)
    print("\n" + f"{total_global - falhas_global}/{total_global} testes passaram")
    for mod_nome, nome, tb in todos_erros:
        print(f"\n--- {mod_nome}::{nome} ---\n{tb}")
    return 0 if falhas_global == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
