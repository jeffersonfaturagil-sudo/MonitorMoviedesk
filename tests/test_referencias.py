"""Guarda contra nomes usados e nunca definidos (o bug que quebrou o "Consultar").

`api_client` usava `ACTIVE_STATUSES_EXCLUDED` sem importar, e `server._consultar`
chamava `util.as_timezone_offset`, que não existe (virou `util.odata_dt`). Os dois só
apareciam ao clicar em Consultar — nenhum teste cobria o caminho. Este teste varre a
AST e acusa qualquer nome global carregado que não venha de import/atribuição/def.
"""
import ast
import builtins
from pathlib import Path

RAIZ = Path(__file__).resolve().parent.parent

ARQUIVOS = (
    "api_client.py", "config.py", "analyzer.py", "feriados.py", "report.py", "main.py",
    "painel/dados.py", "painel/server.py", "painel/macros.py", "painel/processos.py",
    "painel/util.py",
)

# Atributos que o Python injeta no módulo.
MAGICOS = {"__file__", "__name__", "__doc__", "__package__", "__spec__", "__loader__"}

BUILTINS = set(dir(builtins)) | MAGICOS


def _definidos_no_modulo(tree: ast.Module) -> set:
    nomes: set = set()
    for n in tree.body:
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            nomes.add(n.name)
        elif isinstance(n, ast.Assign):
            for alvo in n.targets:
                for x in ast.walk(alvo):
                    if isinstance(x, ast.Name):
                        nomes.add(x.id)
        elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            nomes.add(n.target.id)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                nomes.add(a.asname or a.name.split(".")[0])
        elif isinstance(n, ast.Try):
            corpos = list(n.body) + [s for h in n.handlers for s in h.body] + list(n.orelse) + list(n.finalbody)
            for st in corpos:
                if isinstance(st, (ast.Import, ast.ImportFrom)):
                    for a in st.names:
                        nomes.add(a.asname or a.name.split(".")[0])
                elif isinstance(st, ast.Assign):
                    for alvo in st.targets:
                        for x in ast.walk(alvo):
                            if isinstance(x, ast.Name):
                                nomes.add(x.id)
    return nomes


def _ligados_em_qualquer_escopo(tree: ast.Module) -> set:
    nomes: set = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            nomes.add(n.id)
        elif isinstance(n, (ast.arg,)):
            nomes.add(n.arg)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            nomes.add(n.name)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            nomes.add(n.name)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            for a in n.names:
                nomes.add(a.asname or a.name.split(".")[0])
        elif isinstance(n, ast.Global):
            nomes.update(n.names)
        elif isinstance(n, ast.Nonlocal):
            nomes.update(n.names)
    return nomes


def test_sem_nomes_globais_indefinidos():
    problemas = []
    for arq in ARQUIVOS:
        caminho = RAIZ / arq
        if not caminho.exists():
            continue
        tree = ast.parse(caminho.read_text(encoding="utf-8"))
        conhecidos = _definidos_no_modulo(tree) | _ligados_em_qualquer_escopo(tree) | BUILTINS
        for n in ast.walk(tree):
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load) and n.id not in conhecidos:
                problemas.append(f"{arq}:{n.lineno} usa {n.id!r} sem definir/importar")
    assert not problemas, "nomes indefinidos:\n" + "\n".join(sorted(set(problemas)))


def test_as_timezone_offset_nao_e_usado_em_lugar_nenhum():
    """O nome antigo virou `util.odata_dt`; sobrou só em chamadas quebradas."""
    achados = []
    for caminho in RAIZ.rglob("*.py"):
        if ".freebuff" in str(caminho) or "/tests/" in str(caminho):
            continue
        texto = caminho.read_text(encoding="utf-8", errors="ignore")
        if "as_timezone_offset" in texto:
            achados.append(str(caminho.relative_to(RAIZ)))
    assert not achados, f"as_timezone_offset ainda aparece em: {achados}"
