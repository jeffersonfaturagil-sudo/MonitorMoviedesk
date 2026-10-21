# Auditoria Técnica — Painel de Monitoramento do Suporte (Movidesk)

> Relatório fiel ao estado do código em `/home/suporte03/movidesk-monitor` (data da auditoria: 20/10/2026).
> Nenhum arquivo do projeto foi alterado para produzir este relatório.

## 1. Arquitetura do projeto

### 1.1 Stack e estrutura

- **Linguagem/runtime:** Python 3.11/3.12; dependência de execução `requests>=2.28`; testes via `pytest>=7` ou runner próprio `run_tests.py`.
- **Servidor web:** `http.server.ThreadingHTTPServer` (stdlib) servindo `127.0.0.1:8773`. Frontend é HTML/CSS/JS vanilla em `painel/index.html` (single-file, ~2.7k linhas), sem framework SPA.
- **Persistência:** arquivos JSON locais (sem banco de dados). `cache/tickets.json` (schema versionado `schema: 2`, organizado por atendente em `por_agente`), `cache/acoes_index.json` (índice de ações para macros), `painel/implantados.json` (clientes em implantação), `painel/macros_locais.json` (macros definidas pelo time), `painel/processos/*.txt` + `index.json` (playbooks), `feriados.txt`.
- **CLI opcional:** `main.py` gera relatório em texto em `relatorios/` usando o mesmo payload do painel (`dados.build_payload`).
- **Estrutura:**
  - raiz: `api_client.py`, `config.py`, `analyzer.py`, `feriados.py`, `main.py`, `report.py`, `run_tests.py`, `run_tests_manual.py`, `runit.py`, `run_local.py`.
  - `painel/`: `server.py`, `dados.py`, `macros.py`, `processos.py`, `playbook_guia.py`, `horario.py`, `util.py`, `index.html`, playbooks em `painel/processos/`.
  - `tests/`: ~27 arquivos de teste; CI em `.github/workflows/tests.yml`.
  - `cache/`, `relatorios/`.

### 1.2 Comunicação entre camadas

- **Frontend (navegador)** → HTTP local (`http://127.0.0.1:8773`) → `painel/server.py` (`Handler.do_GET/do_POST`) → `painel/dados.py`/`analyzer.py` → `api_client.MovideskClient` → API REST do Movidesk (`https://api.movidesk.com/public/v1`).
- O painel **não consulta automaticamente**: exibe o cache local; a atualização ocorre ao clicar em **Consultar** (`POST /api/consultar`) ou **Forçar tudo**.
- CLI (`main.py`) e painel compartilham o mesmo `cache/tickets.json` (read-modify-write atômico; preserva pesquisa, histórico, soluções, filas locais e plantão).
- Cache em memória por thread no servidor (`_CACHE_MEMO` em `dados.py`) evita reparsear o JSON a cada requisição.

### 1.3 Autenticação e credenciais

- **Token pessoal do Movidesk** (gerado em Configurações > API > Tokens) é enviado como query param `token` em cada requisição HTTP à API.
- Armazenamento: `.env` (`MOVIDESK_TOKEN=`) ou arquivo `.token` na raiz (fallback em `config._load_token()`). Ambos com permissão `600` e listados em `.gitignore`.
- Configurações adicionais no `.env`: `MOVIDESK_AGENT_NAME`, `MOVIDESK_AGENT_EMAIL`, `MOVIDESK_WEB_URL`, `MOVIDESK_PORT`, listas de atendentes por time, feriados, horário comercial, limiares.
- O painel local não tem login de usuário (single-user, roda em localhost). Defesa contra CSRF/DNS rebinding em `server.Handler._origem_ok()` (valida `Host`/`Origin`/`Sec-Fetch-Site` contra 127.0.0.1/localhost e a porta).
- Anexos são baixados via proxy (`download_attachment`) para que o token não chegue ao navegador (a rota HTTP de anexo foi removida; o método segue disponível para CLI/script).

## 2. Integração com a API do Movidesk

### 2.1 Endpoints utilizados (confirmados no código)

Base: `https://api.movidesk.com/public/v1`. Token via query param.

| Método | Rota | Onde no código | Parâmetros principais |
|---|---|---|---|
| GET | `tickets` | `_list_tickets` (list_active_tickets, changed_since, resolved_since, get_ticket_actions, get_ticket, attachments, list_tickets_range) | `$filter`, `$select`, `$expand`, `$orderby`, `$top`, `$skip` |
| GET | `tickets/past` | `list_tickets_range` (rota alternativa) | mesmos OData |
| GET | `tickets/htmldescription` | `get_ticket_html_description` | `id` ou `protocol`, `actionId` |
| GET | `tickets/merged` | `list_merged_tickets`, `get_merged_ticket` | `page`, `startDate`, `endDate`, `id` |
| GET | `survey/responses` | `list_survey_responses` | `limit`, `responseDateGreaterThan`, `responseDateLessThan`, `startingAfter` |
| GET | `survey/questions` | `list_survey_questions` | (sem params) |
| GET | `survey/questions/{id}` | `get_survey_question` | id no path |
| GET | `kb/article` | `search_articles` | `q`, `page`, `pageSize`, `title`, datas, `status`, `tags`, `category`, `menu` |
| GET | `article/{id}` | `get_article` | id no path |
| GET | `storage/download` | `download_attachment` | `token`, `id` (hash) |
| GET | `persons` | `list_agents` | `$filter=profileType eq 3 and isActive eq true`, `$select`, `$top` |
| GET | `services` | `list_services` | `$select`, `$top` |
| POST | `tickets` | `add_internal_note` | body `{"actions":[{"type":1,"description":...}]}` |

### 2.2 Campos retornados e efetivamente utilizados

- **Tickets:** `TICKET_SELECT` (id, protocol, subject, createdDate, lastUpdate, lastActionDate, status, baseStatus, serviceFirstLevel/SecondLevel/ThirdLevel/Full/Id, origin, tags, resolvedInFirstCall, reopenedIn, lifeTimeWorkingTime, stoppedTime/WorkingTime, parentTickets, childrenTickets, slaSolutionDate, slaResponseDate, slaRealResponseDate, slaSolutionTime, slaResponseTime, slaAgreement, urgency, category, actionCount, justification, resolvedIn, closedIn) + expand `clients`, `owner`, `createdBy`, `actions` (id, createdDate, createdBy, type, origin, description), `ownerHistories` (expand owner, changedBy).
- **Survey responses:** `ticketId`, `responseDate`, `type`, `value`, `commentary`, `questionId`, `id` (para paginação).
- **Survey questions:** `id`, `languages[].cultureId/description`, `isActive`, `type`.
- **Merged:** `count`, `pageNumber`, `mergedTickets[]` com `ticketId`, `mergedTickets`, `mergedTicketsIds` (string `;`-separada), `lastUpdate`.
- **KB articles:** `pageSize`, `totalSize`, `page`, `items[]` com id, title, summary, createdAt, updatedAt, status, tags, category[], menu.
- **Article:** id, articleStatus, attachments, categories, contentHtml, contentText, title.
- **Attachments:** `actions[].attachments[]` com `fileName`, `path` (hash), `createdDate`.
- **Persons:** id, businessName, userName, accountEmail, isActive.
- **Services:** id, name, parentServiceId, defaultCategory, defaultUrgency, isActive, serviceForTicketType.

### 2.3 Filtros, paginação, ordenação e limites

- Filtro OData por owner (`owner/email eq '...'`), `baseStatus ne 'Resolved/Closed/Canceled'`, `lastUpdate gt ...`, `resolvedIn gt ...`, `id eq ...`, `actions/any(...)`.
- Paginação: `$top=PAGE_SIZE` (padrão 1000, configurável `MOVIDESK_PAGE_SIZE`), `$skip` incrementado pelo número realmente retornado; adapta ao teto efetivo da API; trava de segurança `MAX_TICKETS_PAGINADOS = 2_000_000`.
- Ordenação: `lastUpdate asc`, `resolvedIn desc`, `lastActionDate desc`.
- Survey: paginação por cursor (`startingAfter = último id`), `limit=100`.
- Merged: ~50 itens/página, `page`, `max_pages` padrão 30 (painel usa 6).
- KB: `pageSize` padrão 30; primeira página omite `page` (enviar `page=1` zera os itens — confirmado).
- Rate limit: 10 req/min por token (`RateLimiter` janela deslizante de 60s).
- Timeout: `REQUEST_TIMEOUT = 30` s; `MAX_RETRIES = 4`.

### 2.4 Frequência de atualização e cache

- **Sem polling automático.** Atualização manual via **Consultar** / **Forçar tudo**.
- Cache de tickets: `cache/tickets.json` com `salvo_em` por bucket; sync incremental por `lastUpdate` quando existe `last_sync_odata` e `ativos` não vazios; caso contrário carga completa.
- Survey: reuso se salvo há ≤ `SURVEY_CACHE_HOURS` (12h), a menos que `forcar`.
- Survey questions: TTL `SURVEY_QUESTIONS_CACHE_HOURS` (24h).
- Mesclas confirmadas: TTL `MERGED_CACHE_HOURS` (12h).
- Serviços: carregados se forçar ou ausentes.
- Artigos (KB): cache em memória de 10 min por `(q, página, tamanho)` (máx. 100 entradas).
- Ações (macros): índice persistente `cache/acoes_index.json`, atualizado sob demanda/background.
- Histórico: índice persistente em `cache/tickets.json` (`historico`), montado por intervalo de datas em segundo plano.

### 2.5 Tratamento de erros, timeout, rate limit e falhas de sync

- `MovideskError` para erros de API; `_get`/`_post` retentam em rede, 5xx, JSON inválido e 429.
- 429: lê `Retry-After` (segundos ou data HTTP), usa `max(piso_documentado, Retry-After)` com piso 60/120/300s e teto `RETRY_AFTER_MAX_SECONDS` (padrão 600s).
- 401/403: levanta imediatamente (sem retry).
- Demais 4xx: levanta com corpo truncado.
- Falhas de sync no painel são capturadas no `_consultar` e retornadas como HTTP 502 com mensagem.
- Índice de histórico: erros por janela são contados (`erros`) e não interrompem o job; cancelável a qualquer momento.
- `_merge_delta` trata tickets que mudaram de ativo para resolvido (evita ticket preso como pendente).

## 3. Funcionalidades já implementadas

### 3.1 Telas/abas (frontend)

22 abas: Críticos, Pendências, Agenda do dia, Plantão, Atenção, Mescláveis, Satisfação, SLA e vencimentos, Ações sugeridas, Dúvidas/Histórico, Horários, Macros, Encerrar 5d, Implantação, Processos, Equipe/Ranking, Comparativo, Tempo e produtividade, Gargalos entre setores, Inteligência de problemas, Aguardando terceiros, OK. Além de filtro por atendente (topo), filtro por serviço, pills nas listas, sugestão de tickets parecidos (💡), checklist por serviço.

### 3.2 Dashboards, gráficos, tabelas, filtros, indicadores

- **Resumo/topo:** ativos, críticos, atenção, aguardando, mesclas, satisfação, 1ª ação média, interagiu hoje.
- **Críticos (risco):** tickets com risco ALTO/MEDIO e não aguardando cliente.
- **Atenção:** parados 24–48h.
- **OK:** risco BAIXO.
- **Pendências:** todos os ativos; aguardando retorno do cliente vs. precisa resposta; Kanban por status/justificativa (aguardando, em atendimento, retorno, dev); métricas de 1ª ação (média/mediana/total, criados pelo cliente).
- **Agenda do dia:** tickets resolvidos/encerrados desde o último dia útil até ontem (pula sáb/dom/feriados), com texto editável salvo no navegador.
- **Plantão:** quem está de plantão hoje, novos do dia, calendário de rodízio.
- **Mescláveis:** sugestões locais por similaridade (assunto + cliente + serviço) + fila local + confirmação pela API (`GET /tickets/merged`).
- **Satisfação:** intervalo de datas configurável; nota com enunciado oficial; CSAT % e NPS.
- **SLA e vencimentos:** resposta (1h útil), continuidade (ligar após 1h útil), encerramento (5 dias úteis auto-resolução), concluir (3 dias úteis), aguardando outra área; orientação por status/justificativa com responsável, próximo passo e regra citada.
- **Ações sugeridas:** follow-ups, mesclas, SLA, clientes insatisfeitos.
- **Dúvidas/Histórico:** busca em tickets resolvidos; indexação por período (mês a mês, em segundo plano); solução pronta (última resposta pública/nota interna); base de conhecimento oficial (`GET /kb/article`).
- **Horários:** mapa de calor dia da semana × hora.
- **Macros:** validar ticket, buscar trecho, indexar ações, macros aprendidas.
- **Encerrar 5d:** justificativa "Retorno do Cliente - Encerramento", prazo e chegando no limite.
- **Implantação:** clientes em implantação (tag ou `implantados.json`), reclamações em aberto por atendente.
- **Processos:** guia local pesquisável (playbooks por time/tag), checklist por serviço.
- **Equipe/Ranking:** comparativo por atendente (carga, críticos, tempo médio parado, FCR, reabertos, CSAT/NPS, tendência semanal, coluna Foco).
- **Comparativo:** KPIs 7/30 dias vs período anterior.
- **Relatórios:** Tempo e produtividade; Gargalos entre setores; Inteligência de problemas.
- **Terceiros:** tickets aguardando análise/dev/versão/fornecedor/implantação.
- **Fornecedor:** tickets com justificativa "Retorno de Fornecedor" ou tags `fornecedor_*`.

### 3.3 Cálculos principais (confirmados no código)

- `build_ticket_info`: `hours_idle = now - (lastActionDate or lastUpdate)`; `days_open = now - createdDate`; `level = classify_idle` (>48h CRITICO, >24h ATENCAO, senão OK).
- `nivel_risco`: ALTO se SLA vencido ou urgência "urgente"; MEDIO se SLA em risco, urgência "alta" ou parado >7 dias; senão BAIXO.
- `classify_sla`: usa `slaSolutionDate` (VENCIDO/RISCO/OK/SEM_SLA) ou `slaResponseDate`; se só acordo/tempo configurado → RISCO "SLA configurado sem data calculada".
- Mesclas: `subject_similarity = max(levenshtein normalizado, jaccard de tokens)`; candidato se similaridade ≥ 0.7 ou mesmo serviço+subs erviço; agrupado por cliente; ignora vínculos parent/children; dedup por par.
- SLA/vencimentos (horário comercial, seg–sex, sem feriados): resposta 1h útil após última ação do cliente/criação; continuidade: ligar após 1h útil do envio nosso; encerramento: 5 dias úteis; concluir: 3 dias úteis; pausa quando aguarda cliente.
- CSAT: `type=1` satisfeito/insatisfeito; `type=2` escala 1–5 (≥4 satisfeito, 1–2 insatisfeito); `type=3` NPS (≥9 promotor, ≤6 detrator); `type=4` sim/não. NPS = 100*(promotores-detratores)/total NPS.
- FCR = `resolvedInFirstCall` true; reopen = `reopenedIn` preenchido.
- Tempo por setor: sequência de `ownerHistories` (changedDate, ownerTeam), tempo entre transições.
- Inteligência: top assuntos (normalizados), categorias, serviços, clientes; erros recorrentes (cliente+serviço); rajada de tickets por cliente em 7 dias; picos por volume diário (média + 1.5σ).

### 3.4 Direto da API vs calculado localmente

- **Direto da API:** tickets e campos acima, survey responses/questions, merged, KB articles, serviços, atendentes, anexos.
- **Calculado localmente:** todas as métricas, classificações, similaridade de mescla, SLA derivado do playbook, horário comercial, feriados, heatmaps, rankings, recorrência, macros por casamento de texto, processos.

### 3.5 Persistido vs temporário

- **Persistido:** `cache/tickets.json` (ativos/resolvidos por atendente + survey + histórico + soluções + plantão + filas locais + mesclas + services + survey_questions + mesclas_confirmadas), `cache/acoes_index.json`, `painel/implantados.json`, `painel/macros_locais.json`, `painel/processos/*`, `feriados.txt`, relatórios em `relatorios/`.
- **Temporário:** cache em memória de artigos (10 min), status de jobs de indexação, payload montado por requisição, filtros/estado do frontend (parte em `localStorage` do navegador, ex.: texto da agenda).

### 3.6 Incompletas/provisórias/simuladas

- **Aba "Encerrar 5d": a lista de candidatos a mover para "Retorno do Cliente - Encerramento" agora é gerada a partir do cache** (`dados.pendencias.candidatos_encerramento`), com regra ligou + >1 dia útil OU Em atendimento/Novo com última ação da equipe. O bloco HTML estático anterior foi removido.
- UI para descrição HTML e anexos **não consome** as rotas/métodos disponíveis (as rotas HTTP foram removidas; métodos seguem em `api_client`).
- Mescla não pode ser executada pela API (só confirmada); ação continua no Movidesk web.
- Busca de artigos é por palavra-chave informada pelo agente; sem sugestão automática a partir do assunto do ticket.
- Alguns campos não disponíveis/limitados: `parentTickets`/`childrenTickets` não vêm no cache de relatório; módulo/versão e releases não existem no Movidesk (usam serviço/categoria/picos como proxy).
- Cache de atendentes (`agentes`) pode estar incompleto (lista da API limitada a 200).

## 4. Monitoramento de tickets

### 4.1 Status, responsáveis, solicitantes, equipes, prioridades, datas

- Status: `status` (texto livre) e `baseStatus` (Open/InAttendance/Stopped/Reopened/Waiting vs Resolved/Closed/Canceled). `ACTIVE_STATUSES_EXCLUDED = ("Resolved","Closed","Canceled")`.
- Responsável: `owner` (email/businessName); filtro por `owner/email`.
- Solicitante: `clients[]` (organization.businessName ou client.businessName).
- Equipes: `AGENTES_SUPORTE`, `AGENTES_DEV`, `AGENTES_IMPLANTACAO`, `AGENTES_COORDENACAO` (configuráveis); `papel_agente()` retorna suporte/dev/implantacao/coordenacao/outros.
- Prioridade: `urgency` (string, ex.: "urgente"/"alta") em minúsculas; usada em risco e listas.
- Datas: createdDate, lastUpdate, lastActionDate, resolvedIn, closedIn, reopenedIn, slaSolutionDate, slaResponseDate, slaRealResponseDate.
- Serviço: serviceFirstLevel/SecondLevel/ThirdLevel/Full/Id; categoria, origin (canal), tags, actionCount.

### 4.2 Histórico e ações

- `actions[]` com id, createdDate, createdBy (name/email), type (1 = nota interna), origin (canal), description.
- `ownerHistories[]` com changedDate, owner/ownerTeam, changedBy — usado para transições entre setores e tempo por setor.
- `parentTickets`/`childrenTickets` — usados na detecção de mescla e vínculo.
- Reaberturas: `reopenedIn` (data) e `resolvedInFirstCall` (FCR).
- Transferências: derivadas de `ownerHistories` (sequência de times).
- Tentativas de contato: detectadas por `origin` (13–16) ou palavras-chave em descriptions (LIGACAO_KEYWORDS); resumo por tipo.
- Notas internas: `type == 1`; o painel pode adicionar nota interna via `POST /tickets` (`main.py --nota`).

### 4.3 Identificação de tickets parados/atrasados/sem responsável/aguardando

- **Parados:** `hours_idle` com limiares 24h/48h (ATENCAO/CRITICO).
- **Atrasados (SLA Movidesk):** `classify_sla` (VENCIDO/RISCO) — mas `slaSolutionDate`/`slaResponseDate` vêm vazios nesta conta, então a maioria é SEM_SLA.
- **SLA derivado do playbook:** aba SLA com vencidos/próximos por regra (1h/3d/5d).
- **Sem responsável:** `owner` vazio ou "-" (chips no frontend; plantão marca `sem_responsavel`).
- **Aguardando cliente:** justification contém "retorno do cliente"; pausa relógio.
- **Aguardando setor interno:** justificativas com dev/análise/fornecedor/implantação/versão; fila "Aguardando terceiros".
- **Aguardando fornecedor:** justificativa "retorno de fornecedor" ou tags `fornecedor_*`.

## 5. Indicadores e relatórios

### 5.1 Já existentes

- Produtividade: resolvidos/criados/reabertos, FCR %, tempo médio de resolução, tempo aberto, tempo útil/parado, contatos, backlog inicial/final.
- Primeira resposta: `_rel_primeira_resposta_h` (tempo até 1ª ação da equipe para tickets criados pelo cliente), média/mediana/p95 etc. (`_rel_stats`).
- Resolução: tempo de resolução (created→resolved), tempo aberto.
- Backlog: aberto_em(ini) vs aberto_em(now).
- SLA: risco Movidesk (quando datas existem) + derivado do playbook.
- Satisfação: CSAT %, NPS, taxa de resposta, negativos listados.
- Avaliações: pesquisa com type/value/commentary, pergunta oficial.
- Reincidência: reabertos (reopen rate), recorrência por cliente+assunto (`recorrencia_de`), clientes em sequência (rajada 7d).
- Desempenho por atendente: ranking com carga, críticos, risco, antigos, tempo médio parado, FCR, reopen, CSAT/NPS, tendência, Foco (fila do dia, reclamações, implantação, fornecedor).
- Horários: heatmap de abertura.
- Gargalos: encaminhamentos por setor, tempo por setor, devoluções ao suporte, chamados recorrentes.
- Inteligência: top assuntos/categorias/serviços/clientes, erros recorrentes, incidentes, sugestões, picos.

### 5.2 Calculáveis com dados disponíveis (mas não implementados como indicador dedicado)

- Tempo médio de primeira resposta por atendente (dados de `primeira_acao_h` existem por ticket; ranking não agrega).
- CSAT por atendente já parcialmente (ranking agrega sat/insat por owner via survey).
- Taxa de reabertura por serviço/cliente (dados existem).
- Aging do backlog por faixa (dados de created/resolved existem).
- Tempo de espera do cliente (diferença entre ações) — actions existem no cache/índice.

### 5.3 Dependem de novas consultas/armazenamento histórico

- SLA oficial do Movidesk: `slaSolutionDate`/`slaResponseDate` vêm vazios nesta conta (confirmado); depende de configuração de SLA no Movidesk ou de outra rota não confirmada.
- Histórico de ações completo: índice de ações só tem 161 tickets; ampliar requer indexação (1 req/ticket, 10 req/min).
- `reopenedIn` no histórico longo: o índice de histórico leve não guarda esse campo (cobertura baixa).
- `ownerHistories` no histórico longo: só tickets sincronizados com expand trazem; cobertura limitada.
- Módulo/versão do produto e log de releases: não existem no Movidesk (confirmado como indisponível).
- Tickets internos vinculados (`parentTickets`/`childrenTickets`) não vêm no cache de relatório.

## 6. Endpoints/recursos ainda não utilizados

Diferenciando confirmado no código vs. documentação vs. não verificado:

- **Confirmados no código e já usados:** todos listados em 2.1.
- **Confirmados na documentação interna (CHECKPOINT) mas com UI parcial/sem consumo:**
  - `GET /tickets/htmldescription` (método `get_ticket_html_description` existe; rota HTTP removida; frontend não consome).
  - `GET /tickets/attachments` via expand (método `get_ticket_attachments`; rota HTTP removida).
  - `GET /storage/download` (método `download_attachment`; rota HTTP removida).
- **Não verificados / não confirmados (não presumir existência):**
  - Endpoint para **executar mescla** via API — não encontrado; mesclar continua no Movidesk web.
  - Rotas de SLA/prazo oficiais além dos campos do ticket — não confirmado (campos vazios nesta conta).
  - Endpoints de "time tracking"/worklog, releases/versões, módulos — não confirmados (documentação interna diz indisponíveis).
  - Paginação/contagem `$count` — confirmado como ignorado pela API.
  - Outros recursos do Movidesk (chat, telefonia, formulários, KB categorias, menus) — não auditados; podem existir mas não foram validados.
- **Potencialmente úteis (a validar antes de implementar):** filtros adicionais em `/kb/article` (title, datas, status, tags, category, menu), `GET /tickets/merged?id=...` para confirmação individual, parâmetros de data em survey, endpoints de persons (perfis), services hierarquia.

## 7. Problemas e oportunidades

### 7.1 Bugs/riscos

- **Dados simulados no frontend:** corrigido — bloco `candsBlk` hardcoded removido; candidatos agora vêm do cache (P0 resolvido).
- **Falta de UI para HTML/anexos** embora métodos existam (P2).
- **`build_plantao` falha de teste por data** (teste espera `real_hoje == "2026-10-19"` mas hoje é 20/10/2026) — teste dependente de data; não é bug de código, mas indica fragilidade.
- **Cache de atendentes** limitado a 200 e pode não incluir todos.
- **Risco de dados incompletos no relatório:** cobertura de ações/reopen/ownerHistories baixa; avisos já expõem.
- **Consultas redundantes:** `_consultar` carrega survey/questions/merged/services a cada consulta; aceitável mas consome cota (10 req/min). Artigos têm cache em memória.
- **Segurança/exposição:** token em query string (visível em logs/URLs); máscara em `mask_url` mitiga logs do cliente, mas o token pode aparecer em logs do servidor/proxy. `.env`/`.token` com 600. Painel sem auth (single-user, localhost).
- **Inconsistências de cálculo:** três definições diferentes de "novos hoje" (indicadores A, ranking B, plantão C) documentadas em `dados.py`; Fila do dia usa regra A, plantão usa createdDate hoje.
- **`_acao_sugerida` e guia:** dependem de `guia_playbook` (status/justificativa); OK.
- **Hardcoded times de suporte/dev** no `.env.example` (Jefferson/Lucas, Leandro/Matheus) — configurável, mas padrão fixo.

### 7.2 Oportunidades de valor para analista de suporte

- Alertas de tickets sem responsável e sem primeira resposta.
- SLA oficial (configurar no Movidesk ou validar rota).
- Histórico de ações completo para macros e soluções.
- Sugestão automática de artigos KB a partir do assunto do ticket.
- Exportação de relatórios (CSV/Excel) — hoje só texto.
- Notificações (desktop) para SLA vencido.
- Dashboard de tempo de espera do cliente e tempo de resolução por serviço.
- Detecção de tickets duplicados entre atendentes (mesclas hoje só por cliente).
- Comparativo de CSAT por serviço/categoria.
- Agenda automática com tickets resolvidos (já existe parcial).

## 8. Conclusão

### 8.1 Funcionalidades existentes (resumo)

Painel local com 22 abas; cache compartilhado CLI/painel; sync incremental; monitoramento de tickets (status, responsável, cliente, equipe, prioridade, datas); histórico de ações/mensagens/notas internas; transferências; reaberturas; detecção de parados/atrasados/sem responsável/aguardando cliente/setor; mesclas (sugestão local + confirmação API); pesquisa de satisfação com CSAT/NPS e pergunta oficial; SLA derivado do playbook em horário comercial; indicadores (SLA/fila/fornecedor/encerramento/implantação/reclamações); ranking por atendente; comparativo 7/30d; relatórios (tempo, gargalos, inteligência); macros (catálogo + aprendidas + índice); processos/playbooks; horários; plantão; agenda; base de conhecimento; fornecedor; encerrar 5d; implantação; terceiros.

### 8.2 Funcionalidades ausentes

- Execução de mescla via API (não confirmado).
- UI para HTML de ações e anexos.
- SLA oficial do Movidesk (campos vazios nesta conta).
- Histórico completo de ações (índice parcial).
- Dados de módulo/versão/releases (indisponíveis no Movidesk).
- Exportação CSV/Excel, notificações desktop, sugestão automática de KB.
- Indicadores dedicados: tempo de espera do cliente, aging do backlog, 1ª resposta por atendente.

### 8.3 Informações adicionais necessárias por melhoria

- Para SLA oficial: configuração de SLA no Movidesk ou documentação/endpoint validado.
- Para histórico completo: tempo de indexação (10 req/min) e storage.
- Para anexos/HTML: consumo no frontend (rotas removidas; reintroduzir se necessário).
- Para exportação: formato desejado e volume.
- Para notificações: preferências do usuário.

### 8.4 Priorização (P0 crítico, P1 alto impacto, P2 útil, P3 opcional)

- **P0:** (resolvido) bloco `candsBlk` hardcoded removido; restante: corrigir teste dependente de data (`test_build_plantao_expoe_resumo_hoje_e_proximos`) ou torná-lo determinístico.
- **P1:** ampliar índice de ações (macros/soluções) — alto valor para analista; expor cobertura; SLA oficial (validar/configurar).
- **P1:** unificar definição de "novos hoje" (documentada) para evitar divergência de métricas.
- **P2:** UI para HTML de ações e anexos; sugestão automática de artigos KB por assunto; exportação CSV.
- **P2:** indicadores dedicados (tempo de espera do cliente, aging do backlog, 1ª resposta por atendente).
- **P3:** notificações desktop, mescla via API (se existir), filtros avançados de KB, cache de atendentes >200.

---

> Marcações "não verificado": qualquer item da seção 6 que não esteja confirmado no código foi marcado explicitamente como não verificado/documentação interna. Credenciais, tokens e dados pessoais não foram incluídos.
