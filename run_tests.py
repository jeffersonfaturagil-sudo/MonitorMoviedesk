#!/usr/bin/env python3
"""Runner local de testes (sem pytest).

Emula os fixtures usados pelos testes (``tmp_path`` e ``monkeypatch``) para
rodar ``python3 run_tests.py`` em qualquer maquina. No CI continua valendo o
pytest (ver .github/workflows/tests.yml).

Uso:
    python3 run_tests.py            # roda tudo
    python3 run_tests.py macros     # roda so os modulos que casam com o filtro
"""
import importlib
import inspect
import os
import sys
import tempfile
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for _p in (ROOT, ROOT / "painel"):
    _s = str(_p)
    if _s not in sys.path:
        sys.path.insert(0, _s)

_SENTINELA = object()


class MonkeyPatch:
    """Substituto minimo do fixture monkeypatch do pytest."""

    def __init__(self):
        self._undo: list = []

    def setattr(self, obj, name, value=True):
        antigo = getattr(obj, name, _SENTINELA)
        self._undo.append((obj, name, antigo))
        setattr(obj, name, value)

    def delattr(self, obj, name, raising=True):
        antigo = getattr(obj, name, _SENTINELA)
        if antigo is _SENTINELA and raising:
            raise AttributeError(name)
        self._undo.append((obj, name, antigo))
        try:
            delattr(obj, name)
        except AttributeError:
            pass

    def setenv(self, name, value):
        antigo = os.environ.get(name, _SENTINELA)
        self._undo.append((os.environ, name, antigo))
        os.environ[name] = str(value)

    def delenv(self, name, raising=True):
        antigo = os.environ.get(name, _SENTINELA)
        if antigo is _SENTINELA and raising:
            raise KeyError(name)
        self._undo.append((os.environ, name, antigo))
        os.environ.pop(name, None)

    def undo(self):
        for obj, name, antigo in reversed(self._undo):
            if antigo is _SENTINELA:
                try:
                    delattr(obj, name)
                except AttributeError:
                    pass
            else:
                setattr(obj, name, antigo)
        self._undo = []


def _fixture(nome: str, monkeypatch: MonkeyPatch, tmp_path: Path):
    if nome == "monkeypatch":
        return monkeypatch
    if nome in ("tmp_path", "tmpdir"):
        return tmp_path
    raise RuntimeError(f"fixture nao suportado pelo runner: {nome}")


def _modulos(filtro: str):
    base = ROOT / "tests"
    arquivos = sorted(base.glob("test_*.py"))
    if filtro:
        arquivos = [a for a in arquivos if filtro in a.stem]
    return [importlib.import_module(f"tests.{a.stem}") for a in arquivos]


def main() -> int:
    filtro = sys.argv[1] if len(sys.argv) > 1 else ""
    total = falhas = 0
    erros = []
    for mod in _modulos(filtro):
        for nome in sorted(n for n in dir(mod) if n.startswith("test_")):
            fn = getattr(mod, nome)
            if not callable(fn):
                continue
            total += 1
            mp = MonkeyPatch()
            tmp = Path(tempfile.mkdtemp(prefix="painel_test_"))
            try:
                params = inspect.signature(fn).parameters
                kwargs = {p: _fixture(p, mp, tmp) for p in params}
                fn(**kwargs)
                print(f"ok   {mod.__name__}::{nome}")
            except Exception:
                falhas += 1
                erros.append((mod.__name__, nome, traceback.format_exc()))
                print(f"FAIL {mod.__name__}::{nome}")
            finally:
                mp.undo()
    print(f"\n{total - falhas}/{total} testes passaram")
    for mod_nome, nome, tb in erros:
        print(f"\n--- {mod_nome}::{nome} ---\n{tb}")
    return 1 if falhas else 0


if __name__ == "__main__":
    sys.exit(main())
