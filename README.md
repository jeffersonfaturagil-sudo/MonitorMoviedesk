# Painel Movidesk

Painel local (roda no seu computador) para acompanhar seus tickets no Movidesk:
parados, mescláveis, pendências, satisfação, plantão e histórico de contatos.

> **Importante:** o painel **não consulta automaticamente**. Ele mostra os dados
> da última consulta salva no seu computador. Quem atualiza é você, clicando em
> **Consultar** na tela (ou em **Forçar tudo** para atualizar tudo, inclusive a
> pesquisa de satisfação).

## Requisitos

- Linux (testado no Linux Mint) com `python3`.
- Um **token pessoal** do Movidesk (Configurações > API > Tokens).

## Instalação (passo a passo)

1. Copie a pasta do projeto para o seu computador (zip ou `git clone`).
2. Abra o terminal na pasta e rode:

   ```bash
   cd movidesk-monitor
   bash instalar.sh
   ```

3. O instalador vai:
   - conferir o Python;
   - criar o arquivo **`.env`** e pedir seu **token**, **nome** e **e-mail**;
   - criar o atalho **Painel Movidesk** no menu e na área de trabalho;
   - abrir o painel (opcional).

4. Se algo estiver errado, edite o `.env` (não precisa reinstalar):

   ```ini
   MOVIDESK_TOKEN=
   MOVIDESK_AGENT_NAME=Seu Nome Completo
   MOVIDESK_AGENT_EMAIL=seu.email@faturagil.com.br
   MOVIDESK_WEB_URL=https://faturagil.movidesk.com
   MOVIDESK_PORT=8773
   MOVIDESK_KB_URL=https://atendimento.faturagil.com.br
   OPENROUTER_API_KEY=
   OPENROUTER_MODEL=openai/gpt-4o-mini
   ```

5. Abra pelo atalho, ou rode:

   ```bash
   python3 painel/server.py
   ```

   Depois acesse http://127.0.0.1:8773 (o atalho abre sozinho).

## Como usar

- **Consultar**: busca seus dados no Movidesk (respeita o limite de 10 req/min).
- **Forçar tudo**: ignora o cache da pesquisa de satisfação (12h) e refaz tudo.
- **Atendente** (topo): troca o atendente monitorado; "Todos os atendentes" traz tudo.
- **Minhas pendências / Kanban / Críticos / Atenção / OK**: situação dos tickets.
- **Mescláveis**: sugestões de mescla (inclusive entre atendentes) + fila local. A
  sugestão é local (similaridade); a **confirmação vem do Movidesk** — o painel cruza
  com `GET /tickets/merged` e marca "confirmado na API" com a data, em vez de depender
  só de alguém lembrar de marcar.
- **Base de conhecimento oficial**: na aba *Dúvidas / Histórico*, busca nos artigos do
  Movidesk (`GET /kb/article`) — a resposta canônica, ao lado da memória de tickets.
- **Dúvidas / Histórico**: busca nos tickets já resolvidos ("isso já foi resolvido?").
  - **Indexar período** / **Tudo desde 2021**: monta o índice histórico por intervalo
    de datas. Usa as duas rotas da API (`/tickets` + `/tickets/past`), mês a mês, e roda
    em segundo plano com barra de progresso. Como o limite é de 10 req/min, períodos
    longos demoram (ex.: ~1 mês ≈ 1 min; o histórico completo pode levar bastante).
  - **Solução pronta**: mostra a última resposta pública/nota interna do ticket.
- **Equipe / Ranking**: comparativo por atendente (carga, críticos, tempo médio parado,
  FCR, reabertos, CSAT/NPS) + coluna **Foco** (tickets novos/atualizados pelo cliente hoje,
  antigos &gt;7d parados, reclamações, implantação e fornecedores em aberto). Por padrão
  mostra só o **time de suporte** (Jefferson + Lucas); marque **"mostrar time de dev"**
  para incluir o time de desenvolvimento (Leandro e Matheus). Os times são
  configuráveis no `.env` (`MOVIDESK_AGENTES_SUPORTE`, `MOVIDESK_AGENTES_DEV`).
- **Tempo e produtividade / Gargalos entre setores / Inteligência de problemas**: três
  abas de relatório (endpoint `GET /api/relatorio?dias=&agente=`) com seletor de período
  (7/30/90 dias ou tudo), no escopo do atendente ou de todos.
  - **Tempo e produtividade**: resolvidos/criados/reabertos, FCR, 1ª resposta, resolução,
    tempo útil/parado (minutos → h/d), contatos, backlog, CSAT e avaliações negativas.
  - **Gargalos entre setores**: encaminhamentos e tempo por setor (Suporte x Engenharia x
    Implantação), devoluções ao suporte e chamados recorrentes.
  - **Inteligência de problemas**: categorias, serviços e assuntos que mais geram
    chamados, clientes que abrem em sequência, erros recorrentes, incidentes, sugestões e
    picos de volume diário.
  - Cada aba mostra um painel **"Cobertura de dados / limitações"** com o quanto do cache
    sustenta cada número (ações, `ownerHistories`, `reopenedIn`) — dados que o Movidesk
    não expõe (módulo/versão, releases, `parentTickets`) aparecem como indisponíveis.
- **SLA e vencimentos**: regras do regras internas contadas em **horário comercial**
  (seg-sex, `08:00-12:00` e `13:00-18:00`, sem feriados): resposta em até **1 hora útil**
  após a última ação do cliente (ou em tickets **novos**); tickets aguardando cliente
  **pausam** o relógio. A aba reúne os vencidos/próximos do prazo e três filas de trabalho:
  - **Resposta (1h útil)**: quem precisa de resposta nossa.
  - **Continuidade** (`Retorno do Cliente - Continuidade`): passada 1 hora útil do nosso
    envio, o regras internas manda **ligar** — os itens "para ligar" ficam marcados (📞).
  - **Encerramento** (`Retorno do Cliente - Encerramento`): resolvido aguardando o cliente;
    vira **Resolvido automaticamente em 5 dias úteis** — traz um botão para copiar a
    justificativa padrão.
  - **Concluir** (`Retorno do Cliente` genérico): sugerimos a solução; sem OK do cliente em
    **3 dias úteis**, concluir.
  - **Orientação por status (responsável + próximo passo)**: cada linha traz o
    **Responsável** e um **"próximo passo"** extraído do regras internas, decidido pelo
    *status/justificativa* (**não pelo assunto**) e sempre com a **regra citada**
    (arquivo:linha) — assim dá pra mostrar pro time o que manda o regras internas.
    Quando a pendência **não é (só) do Suporte**, o painel diz explicitamente o que
    o Suporte deve acompanhar e **o que não faz** (ex.: análise/dev não encaminha sem
    evidências, fornecedor é do fornecedor, implantação vai ao implantador).
  - **Aguardando outra área**: fila com os tickets que esperam **análise, dev,
    liberação de versão, fornecedor ou implantação** — marcados com o responsável e
    com o que cabe ao Suporte (cobrar, manter no nome, atualizar o cliente).
  - A aba abre com a tabela **"O que fazer por status — o que é (e o que NÃO é) do
    Suporte"** (referência das regras do `regras internas-movidesk.txt` e
    `regras internas-suporte.txt`, incluindo as regras de evidências: "sempre ter
    evidências para não ter motivo de contestação").
  Como o Movidesk não devolve prazo nesta conta (`slaSolutionDate`/`slaResponseDate` vazios),
  o vencimento é **derivado** do regras internas (painel de "Cobertura de dados / limitações").
  A aba também mostra a **fila do dia** (tickets novos/atualizados pelo cliente hoje;
  >15 pede reforço até a fila voltar a ~8).
- **Fornecedor**: tickets com justificativa **Retorno de Fornecedor** ou tags
  `fornecedor_*` (C6, PJBank, Sicoob, Sicredi, ...). Regra: atualizar o cliente
  **diariamente**, mesmo sem retorno do fornecedor.
- **Encerrar 5d**: justificativa **Retorno do Cliente - Encerramento** — o ticket é
  resolvido **automaticamente em 5 dias** se o cliente não responder; mostra o prazo e os
  que estão chegando no limite.
- **Implantação**: tickets de clientes em implantação (por tag ou pela lista local
  `painel/implantados.json`, gerada da pasta *CLIENTES - ATIVOS - IMPLANTAÇÃO* do Drive).
  Mostra também as **reclamações** em aberto por atendente.
- **Processos**: guia local de processos do time — digite o caso ("erro de
  faturamento", "credenciamento boleto", "e-mail NFS-e") e veja o passo a passo sem
  abrir documento. Traz os regras internass de `painel/processos/*.txt` (indexados por
  cabeçalhos `# seção`), com **seletor de agente** (suporte/dev/implantação/
  coordenação), chips de assunto e síntese por seção. Nas listas de tickets, o botão
  **📘 Processo** abre o passo a passo do serviço daquele ticket. Diretório/índice:
  `index.json` mapa título→times/tags; edite o `.txt` e o painel lê na hora.
- **Macros**: confere se uma macro foi usada num ticket e cruza com os outros tickets
  onde a mesma macro aparece.
  - **Validar ticket**: digite o número do ticket; o painel lê as ações dele e mostra
    quais macros do catálogo aparecem no texto (com quem escreveu e o trecho). Compara
    o resultado com o **índice de ações** de outros tickets.
  - **Buscar trecho**: procura um texto livre (ex.: "boleto não registrado") nas ações
    de todos os tickets indexados — mostra ticket, autor, data e o trecho.
  - **Indexar ações**: monta o índice varrendo as ações dos tickets em segundo plano
    (1 req/ticket, respeitando o limite de 10 req/min; dá para **parar** a qualquer
    momento). O índice é salvo em `cache/acoes_index.json`.
  - Observação: a API do Movidesk não expõe o nome da macro usada, então a detecção é
    feita por **casamento de texto** (ignora acento/maiúsculas e exige fronteira de
    palavra).
  - **Aprendidas do dado**: além do catálogo semeado pelos regras internass, o painel
    **aprende sozinho** as frases que se repetem em **5+ tickets** (aparecem com a
    etiqueta `aprendida`). Assim macros reais — cujo texto não bate com o título do
    regras internas — também são reconhecidas. Rodapés/assinaturas de e-mail são ignorados.
- **Satisfação**: permite escolher o intervalo de datas (a API aceita
  `responseDateGreaterThan`/`LessThan`). O padrão são os últimos 365 dias. A coluna
  **Nota** mostra o **enunciado oficial** da pergunta (`GET /survey/questions`), e não
  só o rótulo deduzido de `type`/`value`.
- **Filtro por serviço** (topo): restringe as listas ao serviço selecionado.
- **Pills nas listas**: recorrência (mesmo cliente + assunto que já apareceu, com a
  última resolução), **Reclamação** (tag `reclamacao_cliente`), **Em implantação**
  (lista local), fornecedores (tags `fornecedor_*`) e **Checklist** ✅ por serviço
  (credenciamento/boleto de banco, NFS-e, API/Integração e faturamento) — abre o passo a
  passo direto na linha do ticket.
- **Sugestão / tickets parecidos** (💡 nas listas de tickets): busca no índice histórico
  tickets **já resolvidos** parecidos (assunto, cliente, serviço) ou de **mesmo assunto**,
  com botão "Solução pronta" — acelera respostas que já foram dadas antes.
- **Horários**: mapa de calor de abertura de tickets (dia da semana × hora), a partir do
  cache + histórico — ajuda a dimensionar a escala. Use junto com o Ranking da equipe.
- **Plantão**: quem está de plantão hoje e os tickets novos do dia. A ordem do
  rodízio padrão é **Lucas → Jefferson** (configurável em `MOVIDESK_PLANTAO_ORDEM`).
- **Contato** (📞): histórico de ligações/tentativas registradas no ticket.
- **Agenda do dia**: mostra os tickets **resolvidos/encerrados desde o último dia
  útil até ontem**, pulando **sábados, domingos e feriados**. Consulte todo dia útil
  pela manhã; se pular dias (fim de semana/feriado), ela **acumula tudo** desde o
  último dia útil — nenhum ticket fica de fora (ex.: resolvidos de sexta 09/10
  aparecem na terça 13/10, junto com sáb/dom e o feriado de segunda 12/10).
  - Feriados: os **nacionais** já são automáticos (inclui Carnaval, Sexta-feira Santa,
    Corpus Christi). Para feriados municipais/pontos facultativos, use o arquivo
    `feriados.txt` (um `AAAA-MM-DD` por linha) ou a variável `MOVIDESK_FERIADOS`
    no `.env`. O nome do técnico no texto sai do atendente selecionado no topo.
  - O texto editável continua sendo salvo no navegador.

> **Histórico da API:** a rota `/tickets` só devolve tickets com `lastUpdate` dos
> últimos 90 dias; os mais antigos ficam em `/tickets/past`. O painel consulta as
> duas rotas, então dá para indexar desde o início da conta (2021).

## Relatório em texto (CLI, opcional)

O painel é a interface principal, mas existe um relatório em texto para quem prefere
arquivo/log (ou precisa colar num chat):

```bash
python3 main.py            # relatório do dia em relatorios/relatorio_AAAAMMDD_HHMMSS.txt
python3 main.py --json     # + resumo em JSON (criticos, atencao, candidatos_mescla, sla_risco)
python3 main.py --cache    # usa o cache local se estiver fresco (--max-age MIN, padrão 30)
python3 main.py --nota     # pede confirmação e adiciona nota interna nos críticos
```

Ele **não tem regra própria**: monta o mesmo payload do painel (`dados.build_payload`)
com o cache compartilhado, então os números e as **ações sugeridas** são idênticos aos
da tela — o `report.py` só formata.

## Onde ficam os dados

- `.env` — sua configuração e token (nunca compartilhe; fora do Git).
- `cache/tickets.json` — cache **compartilhado** entre o painel e o CLI (`main.py`),
  com schema versionado (`schema: 2`, organizado por atendente em `por_agente`).
  Toda gravação é read-modify-write e atômica: preserva pesquisa de satisfação,
  histórico, soluções, filas locais e plantão. Se encontrar um arquivo de formato
  antigo (lista pura do CLI antigo ou `ativos` na raiz), ele é migrado sem perder
  nada e o original fica salvo em `cache/tickets.legado.json`.
- `cache/` — demais dados das consultas e caches locais (contém dados de clientes).
- `relatorios/` — relatórios de texto gerados.
- `painel/implantados.json` — lista local de **clientes em implantação** (fonte usada
  pela aba **Implantação** e pela pill **Em implantação**). Gere/atualize a partir da
  pasta *CLIENTES - ATIVOS - IMPLANTAÇÃO* do Drive.

## Observações

- Normalização/tokenização de texto tem **fonte única** em `painel/util.py`
  (`norm_texto`, `norm_texto_ascii`, `tokens_simples` + `STOPWORDS_COMUNS`).
  `dados`, `processos` e `macros` importam essas funções — não crie outra cópia.
  O único caso propositalmente distinto é `analyzer.normalize_subject`, que
  compara assunto par a par (levenshtein/jaccard) nas **mesclas**.
- O token é **pessoal**. Nunca publique o `.env` nem o `.token` no GitHub.
  Cada pessoa usa o próprio token.
- Sem Python instalado: `sudo apt update && sudo apt install -y python3`.
- Se faltar `requests`: `sudo apt install -y python3-requests`.

## Testes

Os testes não consultam a API (usam arquivos com dados de exemplo). Rodam em CI
a cada push (GitHub Actions, `.github/workflows/tests.yml`).

```bash
python3 -m pip install -r requirements-dev.txt   # uma vez (instala pytest e requests)
python3 -m pytest -q
```
