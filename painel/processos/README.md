# playbooks-com-secoes-repetidas.md
# README para a pasta `painel/processos/`

**O que está aqui**
- `playbook-suporte.txt`
- `playbook-implantacao.txt`
- `playbook-movidesk.txt`
- `roteiro-onboarding.txt`
- `guia-videochamada-implantacao.txt`
- `playbook-cs-helpdesk.txt`
- `ajustes-fluxo-implantacao-suporte.txt`
- `fluxo-erro-faturamento.txt`
- `ocorrencia-nfse-emails.txt`
- `kpi-suporte.txt`
- `jornada-cliente-contador.txt`
- `suporte-embradev.txt`
- `suporte-fornecedores-embradev.txt`

**Por que um README agora**
Esses arquivos são playbooks/roteiros operacionais da equipe. A análise que eu fiz mostrou repetição estrutural forte entre eles, e não apenas conteúdo similar. Quando voltar alguém nisso, fica mais fácil entender o que é canônico, o que é duplicado e o que deve ser unificado ou removido.

**Repetições que eu detectei**
- Cabecalhos idênticos aparecendo em vários arquivos, principalmente:
    - `# HELP DESK`
    - `# BASE DE CONHECIMENTO`
    - `# PASTAS NO DRIVE`
    - `# RELATÓRIO DIÁRIO` (ou variações)
- Em `playbook-suporte.txt`, `playbook-implantacao.txt` e `guia-videochamada-implantacao.txt` há o mesmo bloco de referências (`Pasta do Suporte: ...`, `Pasta dos Clientes: ...`, etc.) com diferenças mínimas.
- Várias seções são, na prática, plantões comuns de onboarding/implantação/agendamento/returno ao cliente.
- Alguns arquivos são claramente derivados ou atalhos de outros (ex.: playbook de suporte que cita o playbook de implantação).

**Observações de estrutura**
- `playbook-suporte.txt` é o maior e mais fragmentado: muitas seções, muita repetição conceitual, e várias linhas divisoras `---` que funcionam como separadores visuais fracos.
- `playbook-implantacao.txt` e `guia-videochamada-implantacao.txt` têm bastante sobreposição de roteiro de implantação/videochamada.
- `roteiro-onboarding.txt` é um roteiro de integração para novos suportes.
- `playbook-movidesk.txt` é focado na própria ferramenta/helpdesk.
- Arquivos menores como `suporte-embradev.txt` e `suporte-fornecedores-embradev.txt` parecem mais referências puntuais.

**O que eu não vou fazer agora**
- Não estou unificando nada automaticamente.
- Não estou renomeando ou movendo arquivos.
- Não estou apagando conteúdo.

**Critério que eu usaria se fosse consolidar**
- Um arquivo mestre por tema (ex.: suporte, implantação, ferramenta Movidesk, onboarding de novos colaboradores), com seções nomeadas.
- O que é igual viria de um lugar só, e o que é específico de um papel viria de um playbook dedicado.
- Blocos idênticos, como os de `PASTAS NO DRIVE`, teriam só uma versão canônica.
- Divisas puras `---` e cabeçalhos mágicos soltos teriam um padrão mais claro se a equipe quiser manter o texto legível no futuro.

**Limitações dessa leitura**
- Baseei no conteúdo dos `.txt` e nas repetições que aparecem nele; não testei como esses textos são usados no dia a dia pela equipe.
- Alguns arquivos são longos e eu olhei os pontos mais repetidos, não linha por linha em todos.
- Se houver versão “canônica” em outro lugar (planilhas, drive, outro repositório), isso muda a interpretação do que é duplicação real.
