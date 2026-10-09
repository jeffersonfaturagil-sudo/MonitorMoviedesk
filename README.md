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
   MOVIDESK_TOKEN=seu_token_aqui
   MOVIDESK_AGENT_NAME=Seu Nome Completo
   MOVIDESK_AGENT_EMAIL=seu.email@faturagil.com.br
   MOVIDESK_WEB_URL=https://faturagil.movidesk.com
   MOVIDESK_PORT=8773
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
- **Mescláveis**: sugestões de mescla (inclusive entre atendentes) + fila local.
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
  para incluir quem migrou para desenvolvimento (ex.: Leandro). Os times são
  configuráveis no `.env` (`MOVIDESK_AGENTES_SUPORTE`, `MOVIDESK_AGENTES_DEV`).
- **SLA 1h**: regra dos playbooks — responder em até **1 hora** após a última ação do
  cliente (ou em tickets **novos**). Lista os tickets que estão nessa condição e mostra o
  alerta de **fila do dia** (tickets novos/atualizados pelo cliente hoje; >15 pede reforço
  até a fila voltar a ~8).
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
  abrir documento. Traz os playbooks de `painel/processos/*.txt` (indexados por
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
    feita por **casamento de texto** (ignora acento/maiúsculas).
- **Satisfação**: permite escolher o intervalo de datas (a API aceita
  `responseDateGreaterThan`/`LessThan`). O padrão são os últimos 365 dias.
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

## Onde ficam os dados

- `.env` — sua configuração e token (nunca compartilhe; fora do Git).
- `cache/` — dados das consultas e caches locais (contém dados de clientes).
- `relatorios/` — relatórios de texto gerados.
- `painel/implantados.json` — lista local de **clientes em implantação** (fonte usada
  pela aba **Implantação** e pela pill **Em implantação**). Gere/atualize a partir da
  pasta *CLIENTES - ATIVOS - IMPLANTAÇÃO* do Drive.

## Observações

- O token é **pessoal**. Nunca publique o `.env` nem o `.token` no GitHub.
  Cada pessoa usa o próprio token.
- Sem Python instalado: `sudo apt update && sudo apt install -y python3`.
- Se faltar `requests`: `sudo apt install -y python3-requests`.

## Testes

Os testes não consultam a API (usam arquivos com dados de exemplo). Rodam em CI
a cada push (GitHub Actions, `.github/workflows/tests.yml`).

```bash
python3 -m pip install pytest requests   # uma vez
python3 -m pytest -q
```
