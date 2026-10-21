"""Testes do rastreio de macros (casamento por texto nas ações)."""
import macros


def _acao(desc, tipo=2, autor="Jefferson T.", data="2026-10-08T10:00:00"):
    return {
        "type": tipo,
        "description": desc,
        "createdDate": data,
        "createdBy": {"businessName": autor, "email": "jefferson@faturagil.com.br"},
    }


def _isolado(tmp_path, monkeypatch):
    import dados

    monkeypatch.setattr(macros, "ACOES_FILE", tmp_path / "acoes_index.json")
    monkeypatch.setattr(dados, "load_cache_raw", lambda: {})


def test_normalizar_remove_acentos_e_espacos():
    assert macros.normalizar("Solicitação   RECEBIDA") == "solicitacao recebida"
    assert macros.normalizar("Boleto Híbrido com API PIX SICOOB") == "boleto hibrido com api pix sicoob"


def test_catalogo_ids_unicos_e_com_assinaturas():
    ids = [m["id"] for m in macros.CATALOGO]
    assert len(ids) == len(set(ids))
    assert all(m.get("assinaturas") for m in macros.CATALOGO)
    nomes = {m["id"] for m in macros.CATALOGO}
    assert {"solicitacao_recebida", "credenciamento_pjbank", "sugestao_02"} <= nomes


def test_registrar_e_analisar_encontra_macro(tmp_path, monkeypatch):
    _isolado(tmp_path, monkeypatch)
    acts = [
        _acao("Bom dia! Solicitação Recebida, já estamos analisando o seu caso."),
        _acao("Equipe à disposição.", tipo=2, autor="Jefferson T."),
    ]
    macros.registrar_acoes(33124, acts, meta={"cliente": "ACME", "assunto": "Boleto"})

    fontes = macros.coletar_acoes()
    assert "33124" in fontes
    v = macros.analisar_entrada("33124", fontes["33124"], "teste")
    ids = [e["id"] for e in v["encontradas"]]
    assert "solicitacao_recebida" in ids
    achado = next(e for e in v["encontradas"] if e["id"] == "solicitacao_recebida")
    assert achado["ocorrencias"][0]["autor"]
    assert "Solicitação Recebida" in achado["ocorrencias"][0]["trecho"]


def test_analisar_nao_encontra_quando_texto_nao_bate(tmp_path, monkeypatch):
    _isolado(tmp_path, monkeypatch)
    macros.registrar_acoes(999, [_acao("Apenas uma resposta qualquer do suporte.")])
    fontes = macros.coletar_acoes()
    v = macros.analisar_entrada("999", fontes["999"], "teste")
    assert v["encontradas"] == []
    assert len(v["nao_encontradas"]) == len(macros.CATALOGO)


def test_analisar_pega_variacao_com_acento(tmp_path, monkeypatch):
    _isolado(tmp_path, monkeypatch)
    macros.registrar_acoes(500, [_acao("Encaminhamento Implantacao registrado no ticket.")])
    v = macros.analisar_entrada("500", macros.coletar_acoes()["500"], "teste")
    assert "encaminhamento_implantacao" in [e["id"] for e in v["encontradas"]]


def test_buscar_trecho_livre(tmp_path, monkeypatch):
    _isolado(tmp_path, monkeypatch)
    macros.registrar_acoes(1, [_acao("Boleto não registrado no banco Inter do cliente.")])
    macros.registrar_acoes(2, [_acao("Dúvida sobre relatório de comissão.")])

    r = macros.buscar("boleto nao registrado")
    tickets = [i["ticket"] for i in r["itens"]]
    assert tickets == [1]
    assert r["total"] == 1
    assert macros.buscar("")["erro"]


def test_estatisticas_conta_ocorrencias(tmp_path, monkeypatch):
    _isolado(tmp_path, monkeypatch)
    macros.registrar_acoes(10, [_acao("Solicitação Recebida")], meta={"cliente": "A"})
    macros.registrar_acoes(11, [_acao("Solicitação Recebida")], meta={"cliente": "B"})
    est = macros.estatisticas()
    assert est["indice"]["tickets_com_acoes"] == 2
    macro = next(c for c in est["catalogo"] if c["id"] == "solicitacao_recebida")
    assert macro["ocorrencias"] == 2
    assert set(macro["tickets"]) == {"10", "11"}


def test_meta_local_acha_no_cache(tmp_path, monkeypatch):
    import dados

    monkeypatch.setattr(macros, "ACOES_FILE", tmp_path / "acoes_index.json")
    monkeypatch.setattr(dados, "load_cache_raw", lambda: {
        "por_agente": {
            "lucas@faturagil.com.br": {
                "ativos": [{"id": 777, "subject": "NF rejeitada", "clients": [
                    {"businessName": "Cliente X"}
                ]}]
            }
        }
    })
    meta = macros.meta_local(777)
    assert meta["assunto"] == "NF rejeitada"
    assert meta["cliente"] == "Cliente X"


def test_estatisticas_recalcula_apos_novo_ticket(tmp_path, monkeypatch):
    _isolado(tmp_path, monkeypatch)
    macros.registrar_acoes(20, [_acao("Solicitação Recebida")])
    assert macros.estatisticas()["indice"]["tickets_com_acoes"] == 1
    macros.registrar_acoes(21, [_acao("Solicitação Recebida")])
    assert macros.estatisticas()["indice"]["tickets_com_acoes"] == 2


def test_listar_e_remover_macro(tmp_path, monkeypatch):
    _isolado(tmp_path, monkeypatch)
    monkeypatch.setattr(macros, "EXTRA_FILE", tmp_path / "macros_locais.json")
    frase = "Texto unico de encerramento padrao do time"
    for i in range(5):
        macros.registrar_acoes(100 + i, [_acao(frase)])
    r = macros.definir_macro("Encerrar", frase)
    assert r.get("ok"), r
    assert macros.listar_definidas()["total"] == 1
    assert macros.remover_macro(r["id"]).get("ok")
    assert macros.listar_definidas()["total"] == 0
    assert macros.remover_macro("nao_existe").get("erro")


def _acao_email(desc, email, tipo=2):
    return {"type": tipo, "description": desc, "createdDate": "2026-10-08T10:00:00",
            "createdBy": {"businessName": "Fulano", "email": email}}


def test_equipe_flag_por_dominio():
    assert macros._limpar_acao(_acao_email("x", "jefferson@faturagil.com.br"))["equipe"] is True
    assert macros._limpar_acao(_acao_email("x", "cliente@empresa.com"))["equipe"] is False
    assert macros._limpar_acao({"description": "x"})["equipe"] is True


def test_hit_de_macro_ignora_acao_do_cliente(tmp_path, monkeypatch):
    _isolado(tmp_path, monkeypatch)
    acts = [
        _acao_email("Solicitação Recebida", "cliente@empresa.com"),   # cliente: não conta
        _acao_email("Solicitação Recebida", "lucas@faturagil.com.br"),  # equipe: conta
    ]
    macros.registrar_acoes(300, acts, meta={"cliente": "ACME"})
    v = macros.analisar_entrada("300", macros.coletar_acoes()["300"], "teste")
    achado = next(e for e in v["encontradas"] if e["id"] == "solicitacao_recebida")
    assert achado["quantos"] == 1


def test_trecho_respeita_fronteira_de_palavra():
    # "sugestão 01" não pode casar dentro de "sugestão 012".
    assert macros._trecho("Referente à sugestão 012 já tratada", ["sugestão 01"]) is None
    assert macros._trecho("Referente à sugestão 01 já tratada", ["sugestão 01"]) is not None


def test_trecho_casa_sem_acento_e_com_fronteira():
    achado = macros._trecho("Encaminhamento Implantacao registrado.", ["encaminhamento implantação"])
    assert achado and achado[1] == "encaminhamento implantação"
    # não casa como pedaço de palavra maior
    assert macros._trecho("Encaminhamento Implantacaoxyz", ["encaminhamento implantacao"]) is None


def test_aprendida_detectada_em_textos_repetidos(tmp_path, monkeypatch):
    _isolado(tmp_path, monkeypatch)
    frase = "Favor aguardar a conclusao da analise interna da equipe"
    for i in range(macros.LIMIAR_MACRO):
        macros.registrar_acoes(400 + i, [_acao(frase)])
    aprendidas = [c for c in macros.estatisticas()["catalogo"] if c.get("auto")]
    assert len(aprendidas) == 1
    assert aprendidas[0]["ocorrencias"] == macros.LIMIAR_MACRO
    assert "repetida em" in aprendidas[0]["fonte"]
    # o texto aprendido passa a casar como macro em um ticket novo
    macros.registrar_acoes(499, [_acao(frase)])
    v = macros.analisar_entrada("499", macros.coletar_acoes()["499"], "teste")
    ids = [e["id"] for e in v["encontradas"]]
    assert aprendidas[0]["id"] in ids
    assert next(e for e in v["encontradas"] if e["id"] == aprendidas[0]["id"])["auto"] is True


def test_aprendida_ignora_assinatura_de_email(tmp_path, monkeypatch):
    _isolado(tmp_path, monkeypatch)
    frase = "Atenciosamente Fulano de Tal contato@faturagil.com.br ramal 1234"
    for i in range(macros.LIMIAR_MACRO):
        macros.registrar_acoes(500 + i, [_acao(frase)])
    assert [c for c in macros.estatisticas()["catalogo"] if c.get("auto")] == []


def test_aprendida_ignora_acao_do_cliente(tmp_path, monkeypatch):
    _isolado(tmp_path, monkeypatch)
    frase = "Favor aguardar a conclusao da analise interna da equipe"
    for i in range(macros.LIMIAR_MACRO):
        macros.registrar_acoes(600 + i, [_acao_email(frase, "cliente@empresa.com")])
    assert [c for c in macros.estatisticas()["catalogo"] if c.get("auto")] == []

