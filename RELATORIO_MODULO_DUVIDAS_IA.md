# Relatório — Módulo Dúvidas / Histórico e Assistente IA

## 1. Arquitetura

- **Frontend:** aba `duvidas` em `painel/index.html` (`telaDuvidas()`).
- **Backend:** rotas em `painel/server.py` (`/api/historico`, `/api/historico/completo`, `/api/historico/status`, `/api/artigos`, `/api/solucao`, `/api/ia`, `/api/ia/cota`, `/api/ia/feedback`).
- **Dados:** `painel/dados.py` (`buscar_historico`, `merge_historico`, `montar_solucao`, `build_pesquisa`, `historico_cobertura`), `painel/macros.py` (índice de ações), `painel/processos.py` (playbooks).
- **Cache:** `cache/tickets.json` (`historico`, `solucoes`), `cache/acoes_index.json`, `cache/ia_cache.json`, `cache/ia_feedback.json`.

## 2. Funcionalidades

- Busca local em tickets resolvidos (tokens + similaridade de assunto).
- Indexação por período (mês a mês, em segundo plano) via `/tickets` + `/tickets/past`.
- Solução pronta (última resposta pública/nota interna) via `/api/solucao`.
- Base de conhecimento oficial (`/kb/article`) com cache em memória (10 min).
- Assistente IA (OpenRouter): recebe texto/chat, monta contexto (playbooks + tickets similares + histórico), retorna resposta, intenção, artigos e macros sugeridos.
- Chat multi-turno (rebater), cache de respostas, feedback 👍/👎, cota exibida.
- Cobertura explícita (ações/reopen/setores).

## 3. Fluxo de dados

1. Usuário digita ou cola texto.
2. Frontend chama `/api/ia` com texto + histórico.
3. Backend monta contexto (até 4000 chars) e chama OpenRouter (com fallback de modelos).
4. Resposta cacheada em `ia_cache.json`.
5. Frontend exibe resposta + intenção + artigos + macros + botões de feedback.

## 4. Limitações

- Contexto limitado a 4000 chars (playbooks + 5 tickets + artigos).
- Busca de artigos KB pode retornar vazio dependendo do termo.
- Cota do OpenRouter depende da conta (limite pode ser null).
- Sem memória persistente entre sessões (histórico em memória do navegador).

## 5. Melhorias sugeridas

- Persistir histórico de chat por ticket (localStorage ou backend).
- Contexto automático do ticket selecionado.
- Detecção de intenção mais robusta (prompt ao modelo).
- Resumo automático da conversa.
- Cache de respostas por similaridade (não só hash exato).
- Modo "passo a passo" com checklist.
- Avaliação agregada para treinar prompts.
- Integração com Discord (enviar resposta ao canal).
EOF
echo "created"