# imports-mortos.md — inventário e desfecho

## Situação agora: resolvido

Do inventário anterior, **três imports foram removidos** (confirmados sem uso) e um
foi mantido por ser equivocado classificar como morto.

## Removidos (confirmado por busca no arquivo + suíte de testes verde)

| Arquivo | Import | Evidência |
|---|---|---|
| `painel/dados.py` | `import collections` | só era usado via `from collections import Counter` dentro de uma função (import local, linha 1254) |
| `painel/dados.py` | `import re` | zero ocorrências de `re.` no arquivo |
| `painel/server.py` | `from dados import acao_info` (dentro de `_acoes`) | `acao_info` nunca era usado naquele método |

## Mantido, com correção da análise anterior

- `analyzer.py`: `from __future__ import annotations` **não é import morto**. É a
  diretiva da linguagem que adia a avaliação das anotações de tipo; o nome
  `annotations` não aparece no código justamente porque ele não é usado como nome.
  A análise anterior (AST procurando uso do nome) foi o que gerou o falso positivo.

## Método usado para decidir

1. Busca textual do nome no arquivo (não só AST de `Name`/`Attribute`), para não
   repetir o falso positivo do `__future__`.
2. Verificação de uso em runtime (`getattr`, `import(...)`, strings) — nenhum caso.
3. `python3 -c "import ast; ast.parse(...)"` nos arquivos alterados.
4. `python3 run_tests.py` → 151/151 depois das remoções.

## Complemento: duplicações encontradas no mesmo tipo de análise

- `analyzer.py`: `classify_sla` estava **definido duas vezes** (blocos idênticos,
  linhas 79-104 e 107-132). A segunda definição sobrescrevia a primeira. Removida
  uma cópia.
- `api_client.py`: a classe `MovideskClient` estava **definida duas vezes** (a
  primeira parcial, com código de `RateLimiter` colado dentro do `_get`). Removida a
  primeira cópia.

Detalhes em `CHECKPOINT-FINAL.md`.
