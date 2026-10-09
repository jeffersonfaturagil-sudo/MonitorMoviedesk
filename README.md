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
  FCR, reabertos, CSAT/NPS).
- **Satisfação**: permite escolher o intervalo de datas (a API aceita
  `responseDateGreaterThan`/`LessThan`). O padrão são os últimos 365 dias.
- **Filtro por serviço** (topo): restringe as listas ao serviço selecionado.
- **Plantão**: quem está de plantão hoje e os tickets novos do dia.
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

## Observações

- O token é **pessoal**. Nunca publique o `.env` nem o `.token` no GitHub.
  Cada pessoa usa o próprio token.
- Sem Python instalado: `sudo apt update && sudo apt install -y python3`.
- Se faltar `requests`: `sudo apt install -y python3-requests`.
