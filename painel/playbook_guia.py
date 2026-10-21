"""Orientações por playbook: responsável e próximo passo por status/justificativa.

Fonte: ``painel/processos/playbook-movidesk.txt`` e ``playbook-suporte.txt``.

O objetivo é deixar explícito **o que é — e o que NÃO é — do Suporte**, para o
atendente agir conforme o playbook, **independente do assunto do ticket**, sempre
com a regra citada (defensável depois: "estava no playbook").

A decisão é tomada pelo *status/justificativa* (não pelo assunto), como manda o
fluxo do Movidesk. O catálogo abaixo é ordenado: a primeira regra que casar vence
(por isso as mais específicas vêm antes).
"""

_SEM_ACENTO = str.maketrans("áàâãäéèêëíìîïóòôõöúùûüç", "aaaaaeeeeiiiiooooouuuuc")


def _norm(s: str) -> str:
    return (s or "").strip().lower().translate(_SEM_ACENTO)


# Cada item: 'match' são trechos (normalizados) que precisam TODOS aparecer na
# justificativa/status. 'regra' cita o arquivo e as linhas do playbook.
ORIENTACOES: list[dict] = [
    {
        "id": "fornecedor",
        "titulo": "Aguardando » Retorno de Fornecedor",
        "match": ("retorno de fornecedor",),
        "responsavel": "Fornecedor",
        "do_suporte": True,
        "sugestao": (
            "Conferir na conta do cliente se houve avanço, enviar atualização ao cliente "
            "MESMO sem retorno do fornecedor e continuar o atendimento no chat do fornecedor "
            "(Tecnospeed/PJBank/etc.). A cada período (manhã/tarde), verificar se há retorno ao cliente."
        ),
        "regra": "playbook-movidesk.txt:173-174 e FLUXO DIÁRIO › Retorno do fornecedor (:252-256)",
        "nao": ("O Suporte não resolve a pendência do fornecedor; o que é do Suporte é "
                "não deixar o cliente sem status."),
    },
    {
        "id": "encerramento",
        "titulo": "Aguardando » Retorno do cliente - Para encerramento",
        "match": ("retorno do cliente", "encerramento"),
        "responsavel": "Cliente",
        "do_suporte": True,
        "sugestao": (
            "Ticket já resolvido, aguardando eventual consideração do cliente. Sem retorno em "
            "5 dias úteis ele é Resolvido automaticamente pelo sistema — encaminhar para encerramento."
        ),
        "regra": "playbook-movidesk.txt:179-181",
    },
    {
        "id": "continuidade",
        "titulo": "Aguardando » Retorno do cliente - Para continuidade",
        "match": ("retorno do cliente", "continuidade"),
        "responsavel": "Cliente",
        "do_suporte": True,
        "sugestao": (
            "Aguardando retorno do cliente para dar sequência. Se a última ação foi nossa e "
            "já passou 1h útil, LIGAR para coletar o feedback e registrar a tentativa (sem "
            "atender, tentar de novo no próximo período)."
        ),
        "regra": "playbook-movidesk.txt:175-178 e FLUXO DE RETORNO AO CLIENTE (playbook-suporte.txt:890-897)",
    },
    {
        "id": "retorno_cliente",
        "titulo": "Aguardando » Retorno do Cliente (geral)",
        "match": ("retorno do cliente",),
        "responsavel": "Cliente",
        "do_suporte": True,
        "sugestao": (
            "Sugerimos uma solução: perguntar ao cliente se ele conseguiu verificar. "
            "Se em 3 dias úteis ele não der o OK, concluir."
        ),
        "regra": "playbook-movidesk.txt:259-263",
    },
    {
        "id": "analise",
        "titulo": "Aguardando » Equipe de análise",
        "match": ("equipe de analise",),
        "responsavel": "Engenharia e Tecnologia",
        "do_suporte": False,
        "sugestao": (
            "Não é do Suporte decidir: a análise avalia se o desenvolvimento será feito. "
            "O que é do Suporte: manter o ticket no seu nome, conferir retorno ao cliente a cada "
            "período e cobrar a equipe no grupo. Para urgência, abrir tópico no Discord. "
            "Encaminhar à análise SÓ com evidências (prints/relatórios/extratos) e com os analistas em CC."
        ),
        "regra": "playbook-suporte.txt:119-136 (# ENVIAR PARA EQUIPE DE ANÁLISE) e playbook-movidesk.txt:167-168",
        "nao": "Não encaminhar para análise sem evidências; não aprovar/prometer desenvolvimento.",
        "evidencia": True,
    },
    {
        "id": "desenvolvimento",
        "titulo": "Aguardando » Equipe de desenvolvimento",
        "match": ("equipe de desenvolvimento",),
        "responsavel": "Engenharia e Tecnologia",
        "do_suporte": False,
        "sugestao": (
            "Já aprovado: encontra-se em desenvolvimento. Não é do Suporte implementar; "
            "acompanhar e atualizar o cliente."
        ),
        "regra": "playbook-movidesk.txt:169-170",
        "nao": "Não é do Suporte desenvolver nem estimar prazo de entrega.",
    },
    {
        "id": "liberacao_versao",
        "titulo": "Aguardando » Liberação de versão",
        "match": ("liberacao de versao",),
        "responsavel": "Engenharia e Tecnologia",
        "do_suporte": False,
        "sugestao": (
            "Já desenvolvido: aguardando a atualização de versão. Acompanhar e avisar o cliente "
            "quando a versão for liberada."
        ),
        "regra": "playbook-movidesk.txt:171-172",
        "nao": "Não é do Suporte liberar versão.",
    },
    {
        "id": "orcamento",
        "titulo": "Aguardando » Aprovação de orçamento",
        "match": ("aprovacao de orcamento", "orcamento"),
        "responsavel": "Comercial",
        "do_suporte": False,
        "sugestao": "Removido provisoriamente do fluxo (playbook).",
        "regra": "playbook-movidesk.txt:182-183",
    },
    {
        "id": "implantacao",
        "titulo": "Implantação (onboarding/treinamento)",
        "match": ("implantacao",),
        "responsavel": "Implantação",
        "do_suporte": True,
        "sugestao": (
            "Fluxo de Implantação (onboarding/treinamento). Recepção: receber o cliente e "
            "encaminhar ao implantador responsável (dar um retorno de que foi recebido). "
            "Exceção: configuração de E-mail/NFS-e/Meio de pagamento o Suporte PODE realizar "
            "para adiantar — depois comunicar o implantador."
        ),
        "regra": "playbook-movidesk.txt:186-203, :266-269 e playbook-suporte.txt:61-65, :881-887",
        "nao": "Não conduzir a implantação/onboarding nem validar liberação.",
    },
    {
        "id": "devolutiva_vendas",
        "titulo": "Implantação » Devolutiva Vendas",
        "match": ("devolutiva vendas",),
        "responsavel": "Comercial",
        "do_suporte": False,
        "sugestao": "Retornado à equipe de vendas para alinhamento de valores — não é do Suporte.",
        "regra": "playbook-movidesk.txt:200-201",
    },
    {
        "id": "churn",
        "titulo": "Implantação » Pedido de Churn",
        "match": ("pedido de churn",),
        "responsavel": "Comercial",
        "do_suporte": False,
        "sugestao": "Cliente cancelou o serviço durante a implantação — não é do Suporte.",
        "regra": "playbook-movidesk.txt:202-203",
    },
    {
        "id": "onboarding_assistido",
        "titulo": "Implantação » Onboarding Assistido",
        "match": ("onboarding assistido",),
        "responsavel": "Parceiro",
        "do_suporte": False,
        "sugestao": "A responsabilidade de implantação é do parceiro — não é do Suporte.",
        "regra": "playbook-movidesk.txt:198-199",
    },
]

_PADRAO = {
    "id": "suporte",
    "titulo": "Novo / Em atendimento (Suporte)",
    "responsavel": "Suporte",
    "do_suporte": True,
    "sugestao": "Responder ao cliente em até 1h útil e manter o atendimento em dia. Inserir assunto claro, preencher Serviço/Origem/Categoria/Urgência e encerrar com ação pública.",
    "regra": "playbook-movidesk.txt:235-249 (FLUXO DIÁRIO DE ATENDIMENTO)",
}


def guia_playbook(justificativa: str = "", status: str = "", autor: str = "",
                  espera_min: float | None = None, novo: bool = False) -> dict:
    """Devolve quem é o responsável e o próximo passo do Suporte conforme o playbook.

    Decide pelo status/justificativa (não pelo assunto). ``autor`` é 'cliente'/
    'equipe'/'', ``espera_min`` o tempo útil desde a última ação da equipe (para o
    caso de continuidade) e ``novo`` indica ticket sem ação ainda.
    """
    alvo = _norm(justificativa) + " " + _norm(status)
    achado = None
    for o in ORIENTACOES:
        if all(termo in alvo for termo in o["match"]):
            achado = o
            break
    base = dict(achado) if achado else dict(_PADRAO)
    base.pop("match", None)
    base.setdefault("nao", "")
    base.setdefault("evidencia", bool(base.get("nao")))
    base["do_suporte"] = bool(base.get("do_suporte", True))

    if base["id"] == "suporte" and novo:
        base["sugestao"] = ("Ticket novo: iniciar o atendimento (verificar histórico, "
                            "inserir assunto claro e preencher os campos obrigatórios).")
    if base["id"] == "continuidade" and autor == "equipe" and (espera_min or 0) >= 60:
        base["precisa_ligar"] = True
        base["sugestao"] = ("JÁ PASSOU 1h útil desde a nossa ação: LIGAR agora para coletar o "
                            "feedback e registrar a tentativa. Sem atender, tentar de novo no "
                            "próximo período (manhã/tarde).")
    return base


def orientacoes_referencia() -> list[dict]:
    """Lista das orientações para a seção de referência 'o que é do Suporte'."""
    saida = []
    for o in ORIENTACOES:
        item = {k: v for k, v in o.items() if k != "match"}
        item.setdefault("nao", "")
        item.setdefault("do_suporte", True)
        item.setdefault("evidencia", False)
        saida.append(item)
    return saida
