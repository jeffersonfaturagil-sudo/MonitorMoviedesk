#!/usr/bin/env python3
"""Quatro runners de teste, com propósito separado e compatíveis com o runner local.

Comportamento como four runners:
1. python3 run_tests.py
   - runner local completo sem pytest: emula fixtures (tmp_path, monkeypatch),
     descobre os módulos tests/test_*.py e roda cada função test_*,
     relatando ok / FAIL e total de passos.
   - Útil como "teste local portátil" quando não se tem pytest instalado.

2. python3 run_tests_manual.py
   - runner manual/estudo: só configura o sys.path (projeto + painel) para que
     importações manuais no repl ou em scripts de exploracao funcionem.
   - Não descobre nem executa testes por si só.

3. python3 run_local.py
   - runner unittest nativo: usa unittest.TestLoader.discover("tests", pattern="test*.py")
     e roda com verbosity=2.
   - Equivalente unittest do mesmo conjunto de testes, para checagem alternativa.

4. python3 runit.py
   - runner unittest explícito: importa modules específicos de teste e monta
     uma suite manualmente, depois roda com verbosity=2.
   - Útil para rodar um subconjunto definido de modules sem descoberta automática.

Observacao: os testes de fato são definidos em tests/test_*.py. Estes scripts sao
apenas formas diferentes de carregar e executar o mesmo conjunto ou subconjuntos.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for p in (ROOT, ROOT / "painel"):
    s = str(p)
    if s not in sys.path:
        sys.path.insert(0, s)

import unittest  # noqa: E402


def run():
    loader = unittest.TestLoader()
    suite = loader.discover("tests", pattern="test*.py")
    runner = unittest.TextTestRunner(verbosity=2)
    result = runner.run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(run())
