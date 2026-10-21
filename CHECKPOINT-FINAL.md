# CHECKPOINT-FINAL.md
# Validação do projeto + o que foi implementado (14/10/2026)

Este é o checkpoint **depois** da validação contra a API real. O checkpoint anterior
era só diagnóstico e continha suposições que se provaram erradas — elas estão
listadas em "Correções de documentação" abaixo.

## 1. O que foi validado contra a API real do Movidesk

Cada item foi testado com chamadas HTTP reais usando o token do projeto:

| Rota | Resultado |
|---|---|
| `GET /tickets?$top=1000` | 200, devolve 1000 objetos (aceita `$top=1000`) |
| `GET /tickets?$count=true` | 200, `$count` **ignorado** |
| `GET /tickets/past` | 200, existe (`/tickets` cobre janela recente) |
| `GET /tickets/merged` | 200, **existe** — mesclas confirmadas |
| `GET /tickets/merged?id=29611` | 200, 11 ids mesclados naquele ticket |
| `GET /survey/questions` | 200, **existe** — texto real das perguntas |
| `GET /survey/questions/{id}` | 200, estrutura igual |
| `GET /tickets/htmldescription?id=78` | 200, 16 ações com HTML |
| `GET /tickets?$select=htmlDescription` | 301 apontando para a rota acima |
| `GET /kb/article?q=nfse` | 200, **existe** — busca de artigos (`page=1` zera os itens) |
| `GET /article/199704` | 200, artigo com `contentHtml` |
| `GET /storage/download?id=HASH` | 200, 6825 bytes de um anexo real |
| `tickets.actions[].attachments` | `{fileName, path}`; `path` é o hash |
| `GET /articles` | 404 (a rota é `kb/article`, não `articles`) |
| `POST /api/consultar` (painel) | 200, `origem: "api"`, 56 ativos / 43 críticos / 1 mescla |

Limites medidos: **10 requisições/minuto** por token; sem headers de rate limit nas
respostas 200; bloqueio por 429 documentado em 60/120/300s com `Retry-After`.

## 2. O que foi implementado (código, não documento)

### `api_client.py`
- `Retry-After` respeitado (segundos ou data HTTP), com piso documentado 60/120/300 e
  teto configurável; backoff `5/15/30/60` só para falhas que não são 429.
- `PAGE_SIZE = 1000` (era 100) — 10× menos requisições para o mesmo dado.
- Métodos novos: `list_survey_questions`, `get_survey_question`,
  `list_merged_tickets`, `get_merged_ticket`, `get_ticket_html_description`,
  `search_articles`, `get_article`, `get_ticket_attachments`, `download_attachment`.
- **Classe `MovideskClient` duplicada removida** (a 1ª cópia, com um trecho de
  `RateLimiter.wait` colado dentro do `_get`, era código morto).

### `config.py`
- `PAGE_SIZE`, `RETRY_AFTER_MAX_SECONDS`, `SURVEY_QUESTIONS_CACHE_HOURS`,
  `MERGED_CACHE_HOURS`, `MOVIDESK_KB_URL` (todos sobrescrevíveis por `.env`;
  documentados no `.env.example`).

### `painel/dados.py`
- `pergunta_pesquisa()` + `build_pesquisa(..., perguntas=...)`: cada resposta de
  pesquisa passa a trazer o **enunciado oficial** (`pergunta`, `pergunta_oficial`,
  `question_id`) em vez de só o rótulo por `type`/`value`.
- `build_mesclas()` **corrigido** (ver item 3) e agora devolve
  `{total, itens, escopo, agente, confirmadas}`, com `confirmado`/`confirmado_em`
  por candidato, cruzando com `GET /tickets/merged`.
- Helpers `mesclas_confirmadas()`, `_indice_mesclas_confirmadas()`, `_mescla_confirmada()`.

### `painel/server.py`
- `_consultar` passou a cachear o catálogo de perguntas e as mesclas confirmadas.
- Rotas novas: `GET /api/mesclas/confirmadas`, `GET /api/artigos`,
  `GET /api/ticket-html`, `GET /api/ticket-anexos`, `GET /api/anexo`
  (proxy do anexo: o token nunca chega ao navegador).
- Cache em memória de 10 min para a busca de artigos.

### `painel/index.html`
- Aba **Mescláveis**: selo "confirmado na API" + contagem de mesclas confirmadas.
- Aba **Satisfação**: enunciado oficial embaixo da nota.
- Aba **Dúvidas / Histórico**: bloco "Base de conhecimento oficial" com busca e link.

### Limpezas / reparos de nomes
- `api_client.py`: import de `ACTIVE_STATUSES_EXCLUDED` adicionado (sem ele
  `list_active_tickets` estourava) e a classe duplicada removida.
- `painel/server.py`: as 3 chamadas `util.as_timezone_offset(...)` viraram `_odata(...)`
  (`util.odata_dt`), que é a função que realmente existe.
- `analyzer.py`: `classify_sla` estava **definido duas vezes** (idêntico) — removido.
- `painel/dados.py`: imports mortos `collections` e `re` removidos.
- `painel/server.py`: `from dados import acao_info` morto removido.

## 3. Bugs reais encontrados na validação

1. **Botão "Consultar" quebrado (HTTP 502).** Dois nomes usados e nunca definidos:
   - `api_client.py` usa `ACTIVE_STATUSES_EXCLUDED` em `list_active_tickets`,
     `list_tickets_changed_since` e `list_participation_outside_load`, mas o import
     de `config` não trazia esse nome → `NameError`.
   - `painel/server.py._consultar` chamava `util.as_timezone_offset(...)` em 3 lugares.
     Ao unificar a formatação OData em `util.odata_dt`, os call sites não foram
     atualizados → `NameError` (o módulo `util` nem estava importado como módulo).

   O erro era engolido pelo `except Exception` de `_consultar`, que devolvia
   `{"erro": ...}` com 502 — ou seja, dava para achar que o painel estava só "sem
   dados novos". Corrigido: import adicionado e as 3 chamadas passaram a usar `_odata`
   (alias de `util.odata_dt` já importado). **Validado com um Consultar real**: log
   `Sync incremental ... desde 2026-10-13T17:50:05-03:00`, 2273 respostas de pesquisa,
   8 tickets alterados e `last_sync_odata` novo (`2026-10-14T09:25:22-03:00`).
2. **Aba "Mescláveis" quebrada (HTTP 500).** `dados.build_mesclas()` tinha o corpo de
   `analyzer.find_merge_candidates` colado no meio: usava `SIMILARITY_THRESHOLD`,
   `a`, `b`, `client`, `same_sub_service` sem que nada disso existisse, e devolvia a
   lista errada. Reproduzido com `NameError: name 'SIMILARITY_THRESHOLD' is not defined`.
   Reescrito; teste de regressão em `tests/test_api_movidesk.py`.
3. **`api_client.py` com classe duplicada** (ver item 2) — 427 linhas de código morto
   dentro do arquivo que fala com a API.
4. **`analyzer.classify_sla` duplicado** — a segunda definição silenciosamente
   sobrescrevia a primeira.
5. **Documentação desatualizada**: quatro documentos afirmavam que endpoints não
   existiam, quando existem e funcionam.

Os itens 1, 2 e 4 têm a mesma raiz: edições anteriores deixaram o arquivo num estado
meio aplicado, e **nada exercitava esses caminhos** — os testes cobriam funções puras,
nenhum teste tocava `_consultar` nem `_list_tickets` via API. Duas proteções entraram:
`tests/test_referencias.py` (varre a AST atrás de nome global indefinido, que é
exatamente essa classe de erro) e `test_http_consultar_monta_odata_e_cacheia`
(o caminho do Consultar com a API falsa).

## 4. Verificação

- `python3 run_tests.py` → **161/161** e `pytest -q` → **161 passed** (eram 115).
  Novos: `tests/test_api_movidesk.py`, `tests/test_referencias.py` e casos de rota em
  `tests/test_http.py`.
- Endpoints exercitados no painel rodando (`127.0.0.1:8773`): `/api/mesclas` (200),
  `/api/mesclas/confirmadas` (212 mesclas em 45 dias), `/api/artigos` (200),
  `/api/ticket-html` (200), `/api/dados` (206 respostas com pergunta oficial),
  `/api/indicadores`, `/api/ranking` (200).
- Verificação visual no navegador: aba Mescláveis com o selo novo, busca de artigos
  retornando artigo real, Satisfação com o enunciado oficial. **Zero erros no console
  e todas as requisições 200.**

## 5. O que ainda NÃO foi feito

- **UI da descrição HTML e dos anexos**: as rotas `/api/ticket-html`,
  `/api/ticket-anexos` e `/api/anexo` existem e foram validadas via HTTP, mas a tela
  ainda não as consome (a aba "Ver ações" continua em texto plano).
- **Mesclar pela API**: não há endpoint para executar a mescla; o painel só confirma.
- **Reindexar o histórico** com o novo `PAGE_SIZE` (o índice local foi montado com
  `top=100`; uma reindexação futura fica ~10× mais rápida).
- A busca de artigos é por palavra-chave informada pelo agente; não há sugestão
  automática a partir do assunto do ticket.

## 6. Como reverter / onde olhar

- Backups das versões anteriores desta sessão: `/tmp/api_client.bak.py`,
  `/tmp/dados.bak.py`, `/tmp/analyzer.bak.py`.
- Contratos detalhados por rota: `painel/api-confirmar-mescla.md`,
  `painel/api-encontrar-preguntas-survey.md`,
  `painel/api-lacuna-htmldescription-anexos.md`,
  `painel/api-article-base-conhecimento.md`, `api_client-limites-e-retry.md`.
