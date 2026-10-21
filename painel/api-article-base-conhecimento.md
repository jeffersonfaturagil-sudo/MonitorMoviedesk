# Base de conhecimento (artigos) — status: IMPLEMENTADO

O documento anterior supunha que a base de conhecimento só existia como link manual
nos playbooks. **Existe API pública de artigos, e ela foi verificada em 14/10/2026.**
O painel agora busca a resposta oficial além da memória de tickets.

## Contrato verificado

### Busca: GET /public/v1/kb/article

Parâmetros: `q`, `page`, `pageSize` (padrão 30), e também `title`, datas, `status`,
`tags`, `category`, `menu`.

```json
{
  "pageSize": 2,
  "totalSize": 1,
  "items": [{
    "id": 269455,
    "title": "Faturamento MANUAL Boleto CNAB",
    "summary": null,
    "updatedAt": "2025-09-18T20:46:17.2009024Z",
    "createdAt": "2022-02-21T12:09:56.2742366Z",
    "status": 1,
    "tags": [],
    "category": [{"id": 71987, "name": "Faturamento"}],
    "menu": {"id": 6299, "name": "Base de Conhecimento"}
  }]
}
```

**Atenção:** enviar `page=1` devolve `totalSize` correto mas `items` **vazio**. A
primeira página tem que omitir `page` — o cliente já faz isso.

### Artigo individual: GET /public/v1/article/{id}

Verificado com `199704` ("Olá, que bom te ver por aqui! 🙂"): devolve `id`,
`articleStatus`, `attachments`, `categories`, `contentHtml` (1131 chars no exemplo),
`contentText` e `title`.

### Onde o link deve apontar

Os playbooks usam `https://atendimento.faturagil.com.br/kb/article/{id}` — que é a
base de conhecimento vista pelo cliente, **diferente** da URL do Movidesk web. Por
isso existe a configuração `MOVIDESK_KB_URL` (padrão `https://atendimento.faturagil.com.br`).

## O que foi implementado

- `api_client`: `search_articles(q, page, page_size)` e `get_article(id)`.
- `painel/server.py`: `GET /api/artigos?q=&pagina=&tamanho=` com cache em memória de
  10 minutos (a cota é 10 req/min e ninguém deve gastar uma requisição por tecla).
  Cada item já sai com `url` pronta para abrir.
- `painel/index.html`: bloco **"Base de conhecimento oficial"** na aba
  *Dúvidas / Histórico*, acima/ao lado da busca no histórico local. Colunas: artigo,
  título, categoria, atualizado e botão Abrir.
- `config.py`: `MOVIDESK_KB_URL` (documentada no `.env.example`).

## Por que os dois convivem

- **Histórico** (`/api/historico`): "o que já aconteceu com um caso parecido".
- **Base de conhecimento** (`/api/artigos`): "qual é o procedimento/resposta oficial".

Juntos respondem melhor o "isso já foi resolvido?" — um dá evidência, o outro dá a
resposta canônica.

## Limitação restante

A busca é por palavra-chave (`q`), sem relevância própria: o painel mostra o que o
Movidesk devolve, na ordem dele. Não há integração automática com o assunto do
ticket aberto (o agente escolhe o termo na aba Dúvidas).
