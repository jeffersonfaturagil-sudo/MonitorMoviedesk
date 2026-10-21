# GET /tickets/merged — status: IMPLEMENTADO

## Resumo

O documento anterior dizia que a rota não existia e que a definição de "mesclado"
era desconhecida. **A rota existe e foi verificada contra a API real em 14/10/2026.**
O painel agora confirma a mescla pela própria API em vez de depender só da fila local.

## Contrato verificado (api.movidesk.com/public/v1/tickets/merged)

Parâmetros:

- `token` — obrigatório.
- `id` — número **ou protocolo** do ticket. Quando informado, `startDate`, `endDate`
  e `page` são ignorados.
- `startDate` / `endDate` — opcionais (`AAAA-MM-DD`).
- `page` — opcional (paginação da busca por intervalo).

Exemplo real (janela 01/09/2026 a 14/10/2026):

```json
{
  "startDate": "2026-09-01",
  "endDate": "2026-10-14",
  "count": "196",
  "pageNumber": "1 of 4",
  "mergedTickets": [
    {"ticketId": "27704", "mergedTickets": "2",
     "mergedTicketsIds": "32015;32183", "lastUpdate": "2026-09-04 16:44:09"}
  ]
}
```

Detalhes que importam na hora de consumir:

- Todos os campos numéricos vêm como **string** (`count`, `mergedTickets`, `ticketId`).
- `mergedTicketsIds` é `;`-separado (`"29645;29698;30078"`).
- A página tem ~50 itens; `pageNumber` diz o total (`"1 of 92"`).
- `ticketId` é quem **sobreviveu**; `mergedTicketsIds` são os que foram mesclados nele.
- Por `id` inexistente a API responde `404`.

## O que foi implementado

- `api_client.MovideskClient.list_merged_tickets(start_date, end_date, max_pages)` —
  pagina, normaliza strings e devolve `{total, paginas, itens:[{ticket_id, merged_ids, last_update}]}`.
- `api_client.MovideskClient.get_merged_ticket(id)` — consulta de um ticket.
- `painel/dados.py`: `build_mesclas()` agora marca cada candidato com
  `confirmado` (bool) e `confirmado_em` (data), cruzando a sugestão com a API.
  A sugestão por similaridade continua sendo local — ela sugere; a API confirma.
- `painel/server.py`: `_consultar` guarda as mesclas da janela recente em
  `cache/mesclas_confirmadas` (TTL `MOVIDESK_MESCLAS_CACHE_HOURS`, padrão 12h).
  A leitura é sempre do cache — a rota `GET /api/mesclas/confirmadas` foi removida
  para o painel só consultar o Movidesk ao clicar em **Consultar**.
- `painel/index.html`: a aba **Mescláveis** mostra o selo "confirmado na API" e o
  total confirmado. A fila local (`mesclado`/`ignorado`) continua para o que a API
  ainda não registrou.

## Por que isso é melhor que antes

Antes o painel sugeria mesclas e o agente marcava "mesclado" à mão numa fila local —
o que podia divergir da realidade. Agora a confirmação vem da fonte oficial, e o
que estiver confirmado pode ser escondido (checkbox "mostrar mescladas/ignoradas").

## Limitação restante

Não há (nem parece haver) endpoint para **executar** a mescla via API — o painel
confirma e sugere, mas mesclar continua sendo ação no Movidesk web.
