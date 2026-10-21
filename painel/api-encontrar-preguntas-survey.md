# Perguntas da pesquisa — status: IMPLEMENTADO (texto oficial vem da API)

## Resumo

O documento anterior não sabia se existia forma de obter o **texto** da pergunta.
**Existe: `GET /survey/questions`.** O painel agora mostra o enunciado oficial em vez
de inventar o rótulo por `type`/`value`.

## Contrato verificado (14/10/2026)

`GET /public/v1/survey/questions` → lista de perguntas:

```json
[{
  "id": "XxqG",
  "languages": [
    {"cultureId": "pt-BR", "description": "Qual sua avaliação para esse atendimento?"},
    {"cultureId": "es",    "description": "¿Qual su avaliación para este atendimiento?"},
    {"cultureId": "en-US", "description": "What is your evaluation for this service?"}
  ],
  "isActive": true,
  "type": 1
}]
```

`GET /public/v1/survey/questions/{questionId}` devolve a mesma estrutura para uma
pergunta só.

Pontos de atenção:

- O texto está em `languages[].description`, escolhido por `cultureId` (`pt-BR`).
- `/survey/responses` continua trazendo apenas `questionId` — o painel cruza os dois.
- `type` aqui é o mesmo de `survey_assessment()` (1 = satisfeito/insatisfeito,
  2 = escala, 3 = NPS, 4 = sim/não); o `value` continua sendo o voto.
- A API real hoje devolve **uma** pergunta ativa (`XxqG`), então o catálogo é pequeno
  e barato de cachear.

## O que foi implementado

- `api_client.MovideskClient.list_survey_questions()` → `{questionId: {pergunta, tipo, ativo}}`,
  preferindo `pt-BR` e caindo para o primeiro idioma com texto.
- `api_client.MovideskClient.get_survey_question(id)` → uma pergunta.
- `painel/dados.py`: `pergunta_pesquisa(response, perguntas)` e
  `build_pesquisa(..., perguntas=...)` incluem por item:
  `pergunta` (texto oficial ou o rótulo) e `pergunta_oficial` (bool, para o frontend
  não fingir que é oficial quando não é) e `question_id`.
- `painel/server.py`: `_consultar` grava o catálogo em `cache/survey_questions`
  (TTL `MOVIDESK_PERGUNTAS_CACHE_HOURS`, padrão 24h).
- `painel/index.html`: a aba **Satisfação** mostra o enunciado oficial embaixo da nota.

## Fallback (continua existindo)

Se o catálogo não estiver disponível (API fora, pergunta nova que não veio no cache,
cache vazio), `survey_assessment()` segue traduzindo `type`/`value` em
"Satisfeito"/"Insatisfeito"/"NPS …". Nesse caso `pergunta_oficial` é `false` — o texto
aparece, mas marcado como rótulo, não como pergunta oficial.
