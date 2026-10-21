import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
for p in (ROOT, ROOT / 'painel'):
    s = str(p)
    if s not in sys.path:
        sys.path.insert(0, s)

from tests import test_perfil, test_processos, test_time, test_macros, test_indicadores, test_dados, test_feriados, test_analyzer
import unittest

suite = unittest.TestSuite()
for m in [test_perfil, test_processos, test_time, test_macros, test_indicadores, test_dados, test_feriados, test_analyzer]:
    suite.addTests(unittest.defaultTestLoader.loadTestsFromModule(m))
runner = unittest.TextTestRunner(verbosity=2)
r = runner.run(suite)
sys.exit(0 if r.wasSuccessful() else 1)
