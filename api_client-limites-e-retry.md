# Cliente Movidesk: limites e retry — status: CORRIGIDO

O documento anterior era um inventário com decisões pendentes. As dúvidas foram
resolvidas **medindo a API real** (14/10/2026) e o cliente já foi ajustado.

## 1. `$top`: 1000 funciona (era 100)

Antes: `top = 100` em `_list_tickets`.

Medição real: `GET /tickets?$top=1000&$select=id` devolveu **1000 objetos** (12976
bytes). E uma varredura completa de `/tickets/past` fez 20634 tickets em
**21 requisições / 133s**; com `top=100` seriam ~207 requisições.

Decisão: `PAGE_SIZE = 1000` (`config.PAGE_SIZE`, ajustável em `MOVIDESK_PAGE_SIZE`).
A documentação pública é contraditória (há um trecho citando 100 por página), mas o
comportamento observado na API desta conta aceita 1000 — e o teste
`test_list_tickets_pagina_com_page_size` protege a paginação.

## 2. `$count` continua sem efeito

`GET /tickets?...&$count=true` devolveu exatamente o mesmo corpo do pedido sem
`$count` — a API ignora o parâmetro. Nada no cliente depende dele (e nada foi
adicionado); para saber "quantos são", use `/tickets/merged` (que traz `count`) ou
paginar até o fim.

## 3. `429` + `Retry-After` — agora respeitado

Antes: retry fixo `5/15/30/60s`, ignorando o `Retry-After`.

Documentação oficial: depois de sequências de erro a API de tickets bloqueia,
começando em **60s** e subindo para **120s** e **300s**, justamente com o header
`Retry-After`. Nas respostas 200 medidas **não** há header de rate limit.

Novo comportamento (`MovideskClient._espera_rate_limit`):

- lê `Retry-After` em segundos **ou** data HTTP;
- usa `max(piso_documentado, Retry-After)` — nunca espera menos do que o bloqueio
  documentado, e obedece a API quando ela pede mais;
- teto de `RETRY_AFTER_MAX_SECONDS` (padrão 600s) para um valor absurdo não travar o painel;
- sem header, cai no piso `60/120/300`;
- erros que não são 429 (rede, 5xx, JSON inválido) seguem no backoff `5/15/30/60`.

Aplicado em `_get`, `_post` e no download de anexo.

## 4. 90 dias em `/tickets` e o viés de "D-1" em `/tickets/past`

Continua valendo: `/tickets` cobre janela recente e o histórico antigo vem de
`/tickets/past`. `list_tickets_range` consulta **as duas** rotas e deduplica por id,
que é a proteção contra a fronteira dos 90 dias. Baixar `top` para 100 faria isso
custar 10× mais requisições numa cota de 10/min — motivação extra para o item 1.

## 5. Limites gerais observados

- **10 requisições/minuto** por token — continua sendo a restrição que dita o design
  (daí o `RateLimiter` de janela deslizante e os caches de pesquisa/mesclas/perguntas).
- Corpos grandes são normais: 300 tickets com ações deram 326 KB; pedir só o que se
  usa (`$select`) importa.

## 6. O que mudou no arquivo

Além dos itens acima, `api_client.py` tinha a classe `MovideskClient` **definida duas
vezes** (a primeira, parcial, com um trecho de `RateLimiter.wait` colado dentro do
`_get`, depois do `raise`). A segunda é que valia em runtime. A primeira foi removida:
o arquivo caiu de 889 para 462 linhas sem perder nenhum método.

## Limitação restante

Não forcei um 429 real para ver o header em produção — a semântica vem da
documentação oficial e a leitura do header está coberta por teste unitário
(`test_espera_rate_limit_respeita_header_e_piso_documentado`).
