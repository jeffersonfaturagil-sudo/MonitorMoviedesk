"""Testes das orientações por playbook (playbook_guia).

A orientação é decidida pelo status/justificativa (não pelo assunto) e sempre
cita a regra, para o time saber o que fazer — e o que NÃO é do Suporte.
"""
import playbook_guia as pg


def test_continuidade_manda_ligar_apos_1h():
    g = pg.guia_playbook("Aguardando » Retorno do cliente - Para continuidade",
                         autor="equipe", espera_min=90)
    assert g["responsavel"] == "Cliente"
    assert g["do_suporte"] is True
    assert "JÁ PASSOU" in g["sugestao"]      # versão agressiva (ligar agora)
    assert "LIGAR" in g["sugestao"]
    assert "playbook-suporte" in g["regra"]


def test_continuidade_recente_nao_exige_ligar_ainda():
    g = pg.guia_playbook("Aguardando » Retorno do cliente - Para continuidade",
                         autor="cliente", espera_min=5)
    assert g["responsavel"] == "Cliente"
    assert g["do_suporte"] is True
    assert "JÁ PASSOU" not in g["sugestao"]  # ainda é só "aguardar"


def test_analise_nao_e_do_suporte():
    g = pg.guia_playbook("Aguardando » Equipe de análise")
    assert g["responsavel"] == "Engenharia e Tecnologia"
    assert g["do_suporte"] is False
    assert g["nao"]                          # explicita o que NÃO é do Suporte
    assert g["evidencia"] is True             # só encaminhar com evidências


def test_desenvolvimento_nao_e_do_suporte():
    g = pg.guia_playbook("Aguardando » Equipe de desenvolvimento")
    assert g["responsavel"] == "Engenharia e Tecnologia"
    assert g["do_suporte"] is False
    assert g["nao"]


def test_fornecedor_continua_com_acao_do_suporte():
    g = pg.guia_playbook("Aguardando » Retorno de Fornecedor")
    assert g["responsavel"] == "Fornecedor"
    assert g["do_suporte"] is True            # atualizar cliente é do Suporte
    assert g["nao"]


def test_implantacao_encaminha_ao_implantador():
    g = pg.guia_playbook("Implantação » Call 1 - Alinhamento")
    assert g["responsavel"] == "Implantação"
    assert "implantador" in g["sugestao"].lower()


def test_padrao_novo_atendimento():
    g = pg.guia_playbook("", novo=True)
    assert g["responsavel"] == "Suporte"
    assert g["do_suporte"] is True
    assert "iniciar" in g["sugestao"].lower()


def test_encerramento_e_mais_especifico_que_retorno_geral():
    assert pg.guia_playbook("Retorno do Cliente - Encerramento")["id"] == "encerramento"
    assert pg.guia_playbook("Retorno do Cliente - Continuidade")["id"] == "continuidade"
    assert pg.guia_playbook("Retorno do Cliente")["id"] == "retorno_cliente"


def test_referencia_lista_todas_as_orientacoes():
    refs = pg.orientacoes_referencia()
    ids = [o["id"] for o in refs]
    assert len(ids) == len(set(ids))
    assert {"fornecedor", "analise", "desenvolvimento", "implantacao"} <= set(ids)
    assert all(o.get("regra") and o.get("sugestao") for o in refs)
    assert all("match" not in o for o in refs)


def test_continuidade_marca_precisa_ligar_para_a_aba_ok():
    """O flag estruturado alimenta o pill da aba OK (sem recontar a espera)."""
    g = pg.guia_playbook("Retorno do Cliente - Continuidade", autor="equipe", espera_min=90)
    assert g["precisa_ligar"] is True
    # a bola ainda com o cliente, ou espera abaixo de 1h: não há ligação a fazer
    assert not pg.guia_playbook("Retorno do Cliente - Continuidade",
                                autor="cliente", espera_min=90).get("precisa_ligar")
    assert not pg.guia_playbook("Retorno do Cliente - Continuidade",
                                autor="equipe", espera_min=10).get("precisa_ligar")
