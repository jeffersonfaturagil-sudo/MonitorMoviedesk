# Descrição HTML e anexos — pesquisa da API (rotas do painel REMOVIDAS)

> **Status atual:** as rotas HTTP `GET /api/ticket-html`, `GET /api/ticket-anexos` e
> `GET /api/anexo` foram **removidas** do `painel/server.py`. O painel só consulta o
> Movidesk ao clicar em **Consultar** (carga em cache), e nenhuma dessas rotas era
> usada pelo frontend. Os métodos correspondentes do `api_client`
> (`get_ticket_html_description`, `get_ticket_attachments`, `download_attachment`)
> **continuam disponíveis e testados** para uso de CLI/script.
> A pesquisa abaixo sobre a API do Movidesk segue válida.

O documento anterior dizia que não existia forma de trazer a descrição de abertura
nem os anexos. **Existe, e os três caminhos foram verificados contra a API real em
14/10/2026.**

## 1. GET /tickets/htmldescription

Parâmetros: `id` ou `protocol` (obrigatório um dos dois) e `actionId` (opcional).

Resposta — lista de ações com o HTML em `description`:

```json
[{"id": 1, "description": "<p><span style=\"font-size: 10pt\">Olá KELIN…"}]
```

Verificado com o ticket 78: 16 ações com HTML, a primeira é a mensagem de abertura.

Importante: pedir `htmlDescription` no `$select` de `/tickets` **não** funciona — a
API responde `301` com a mensagem "The field HtmlDescription has been moved to the
v1/tickets/htmldescription endpoint". Ou seja, essa rota é o único caminho.

## 2. Anexos: actions[].attachments

Os anexos vêm dentro das ações:

```json
[{"id": 78, "actions": [
  {"id": 6, "attachments": [
    {"fileName": "454D4E4F63C5A1FD84579A39635ABD0D.png",
     "path": "E9ED841FE970D152AFEC1463C4C21C5D",
     "createdDate": "2021-06-21T13:17:31.2111508"}
  ]}
]}]
```

- `path` é o **hash** do arquivo (usado para baixar).
- O mesmo arquivo reenviado em várias ações aparece repetido — o painel deduplica
  por hash e guarda a lista de ações que o carregam.
- `description` puro (texto) fica na própria ação; `description` **não** é campo do
  ticket (`$select=description` devolve 400).

## 3. GET /storage/download?id=HASH

Baixa o conteúdo do anexo com o `path` obtido acima. Verificado: retornou 6825 bytes
de um PNG real.

## O que foi implementado

- `api_client` (mantido): `get_ticket_html_description(id, actionId=None)`,
  `get_ticket_attachments(id)` (deduplicado), `download_attachment(hash)` (bytes,
  com o mesmo retry/rate-limit do resto do cliente).
- `painel/server.py`: as rotas `GET /api/ticket-html`, `GET /api/ticket-anexos` e
  `GET /api/anexo` foram **removidas** (não eram usadas pelo frontend e batiam no
  Movidesk fora do **Consultar**).
