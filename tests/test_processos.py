import json
import os

import processos


PASTA = os.path.join(os.path.dirname(processos.__file__), "processos")


def _recarregar():
    processos._CACHE = []
    processos._CACHE_CHAVES = None
    return processos.carregar()


def test_index_json_aponta_para_arquivos_existentes():
    with open(os.path.join(PASTA, "index.json"), encoding="utf-8") as f:
        index = json.load(f)
    assert index, "index.json não pode estar vazio"
    for arquivo, meta in index.items():
        assert os.path.isfile(os.path.join(PASTA, arquivo)), f"falta {arquivo}"
        assert meta.get("titulo"), f"sem titulo em {arquivo}"
        assert meta.get("times"), f"sem times em {arquivo}"
        for t in meta.get("times", []):
            assert t in processos._TIMES, f"time invalido {t} em {arquivo}"


def test_carrega_todas_as_secoes_nao_vazias():
    docs = _recarregar()
    assert len(docs) == 11
    for d in docs:
        assert d["secoes"], f"sem secoes em {d['arquivo']}"
        for s in d["secoes"]:
            assert s["texto"].strip(), f"secao vazia em {d['arquivo']}"
            assert s["titulo"].strip()


def test_secoes_de_ignora_separadores_e_titulo_duplicado():
    texto = "Playbook de Suporte\n\n" + "-" * 80 + "\n# ORIENTAÇÕES\n\nFaça isso.\n"
    secoes = processos._secoes_de(texto, "Playbook de Suporte")
    assert [s["titulo"] for s in secoes] == ["ORIENTAÇÕES"]
    assert "----" not in secoes[0]["texto"]


def test_secoes_de_descarta_secao_sem_corpo():
    texto = "# Título sem corpo\n# Com corpo\n\nConteúdo real aqui.\n"
    secoes = processos._secoes_de(texto, "Doc")
    assert [s["titulo"] for s in secoes] == ["Com corpo"]


def test_secoes_de_preambulo_vira_secao_quando_tem_conteudo():
    texto = "Primeira linha de verdade\n\n# Outra seção\n\ncorpo\n"
    secoes = processos._secoes_de(texto, "Doc")
    assert [s["titulo"] for s in secoes] == ["Primeira linha de verdade", "Outra seção"]


def test_busca_erro_faturamento():
    docs, _, _ = processos.buscar("erro de faturamento")
    titulos = [d["titulo"] for d in docs]
    assert "Fluxo: Erro de Faturamento" in titulos
    primeiro = docs[0]
    assert primeiro["_score"] > 900
    assert all(s["titulo"] or s["texto"] for s in primeiro["secoes"])


def test_busca_nota_fiscal_com_erro_ignora_paradas():
    docs, _, termos = processos.buscar("nota fiscal com erro")
    assert "com" not in termos
    titulos = [d["titulo"] for d in docs]
    assert "Playbook de Suporte" in titulos
    topo = docs[0]
    assert any("NFSE" in s["titulo"].upper() or "NFS-E" in s["titulo"].upper()
               for s in topo["secoes"])


def test_busca_prefixo_plural():
    docs, _, _ = processos.buscar("notas fiscais")
    assert docs, "esperava casar 'notas'/'fiscais' com 'nota fiscal'"
    docs, _, _ = processos.buscar("erro de faturamento")
    assert docs[0]["titulo"] == "Fluxo: Erro de Faturamento"


def test_busca_sem_paradas_retorna_faturamento():
    docs, _, termos = processos.buscar("o que fazer para erro de faturamento")
    assert termos and "erro" in termos and "faturamento" in termos
    assert "Fluxo: Erro de Faturamento" in [d["titulo"] for d in docs]


def test_busca_por_time():
    docs, _, _ = processos.buscar("", time="implantacao")
    for d in docs:
        assert "implantacao" in d["times"]
    docs, _, _ = processos.buscar("", time="coordenacao")
    titulos = [d["titulo"] for d in docs]
    assert "KPI de Suporte" in titulos


def test_busca_por_tag_e_sem_resultado():
    docs, _, _ = processos.buscar("", tag="faturamento")
    assert docs and any("Erro" in d["titulo"] for d in docs)
    docs, _, _ = processos.buscar("zzznadaexiste", tag="faturamento")
    assert not docs


def test_busca_fuzzy_tolera_typo():
    docs, _, _ = processos.buscar("erro de faturamentoo")
    titulos = [d["titulo"] for d in docs]
    assert "Fluxo: Erro de Faturamento" in titulos
    assert all(d.get("_fuzzy") for d in docs)


def test_busca_fuzzy_nao_casa_gibberish():
    docs, _, _ = processos.buscar("zzzqwmplk")
    assert not docs


def test_checklist_extrai_passos_do_playbook():
    chk = processos.checklist("erro de faturamento")
    assert chk["consulta"] == "erro de faturamento"
    assert chk["passos"], "esperava extrair passos acionaveis"
    assert "Fluxo: Erro de Faturamento" in chk["fontes"]
    for p in chk["passos"]:
        assert 15 <= len(p) <= 220


def test_checklist_sem_consulta_fica_vazio():
    chk = processos.checklist("")
    assert chk["passos"] == []
    assert chk["fontes"] == []


def test_checklist_respeita_limite():
    chk = processos.checklist("ticket", limite=3)
    assert len(chk["passos"]) <= 3


# --- Leitura do chat: nao basta uma palavra -------------------------------


def test_termos_informativos_descarta_o_que_nao_existe_no_corpus():
    """Ruido do chat (nome de cliente, protocolo) não pode ocupar vaga."""
    docs = processos.carregar()
    ruido = [f"zzq{m:02d}xx" for m in range(50)]
    tokens = ruido + ["faturamento"]
    assert len(tokens) > processos._MAX_TERMS_BUSCA, "precisa ser consulta longa"
    contagens = processos._contagens_termos(docs, tokens)
    assert all(contagens[t] == 0 for t in ruido), "o ruido nao pode existir no corpus"
    uteis, ignorados = processos._termos_informativos(tokens, contagens)
    assert uteis == ["faturamento"]
    assert len(ignorados) == 50
    assert len(uteis) + len(ignorados) == len(tokens)
    assert len(uteis) <= processos._MAX_TERMS_BUSCA


def test_consulta_longa_preserva_a_ordem_do_chat():
    """Não se ordena por raridade: a ordem da conversa é a ordem que o agente leu."""
    docs = processos.carregar()
    tokens = [f"zzq{m:02d}xx" for m in range(45)] + ["erro", "faturamento", "nota"]
    contagens = processos._contagens_termos(docs, tokens)
    uteis, _ = processos._termos_informativos(tokens, contagens)
    assert uteis == ["erro", "faturamento", "nota"]


def test_consulta_curta_passa_inteira_sem_reducao():
    docs = processos.carregar()
    tokens = ["erro", "faturamento"]
    uteis, ignorados = processos._termos_informativos(
        tokens, processos._contagens_termos(docs, tokens))
    assert uteis == tokens
    assert ignorados == []


def test_palavra_repetida_no_chat_nao_vira_evidencia_forte():
    """Repetir a mesma palavra não pode simular dois termos do caso."""
    docs, _, termos = processos.buscar("boleto boleto boleto")
    assert termos == ["boleto"]
    assert docs
    for d in docs:
        assert d["_casados"] == ["boleto"]
        assert d["_confianca"] == "fraco"


def test_termo_generico_sozinho_nunca_e_evidencia_forte():
    """"boleto" esta em quase todo playbook: so ele nao identifica o processo."""
    docs, _, termos = processos.buscar("boleto")
    assert termos == ["boleto"]
    assert docs, "a busca ainda devolve candidatos"
    assert all(d["_confianca"] == "fraco" for d in docs)


def test_termo_de_assunto_vem_do_index_json_e_dos_cabecalhos():
    """Assunto é o que o time rotulou (tags/títulos/cabeçalhos), não a frequência."""
    vocab = processos._vocabulario()
    for t in ("faturamento", "erro", "boleto", "retorno", "fornecedor", "nota", "fiscal"):
        assert processos._marca("", vocab, {t}) > 0, f"{t} deveria ser assunto"
    # Palavra de conversa não virou assunto só por aparecer no texto de algum playbook.
    for t in ("ainda", "tambem", "relata", "ontem"):
        assert processos._marca("", vocab, {t}) == 0, f"{t} não é assunto"


def test_palavra_de_conversa_nao_conta_como_assunto():
    """"ainda"/"tambem" caem em 1-3 playbooks por acaso — não são assunto."""
    docs = processos.carregar()
    cont = processos._contagens_termos(docs, ["ainda", "tambem", "faturamento"])
    assert cont["ainda"] >= 1, "ainda existe no corpus"
    assert not processos._distintivo("ainda", cont, len(docs))
    assert not processos._distintivo("tambem", cont, len(docs))
    assert processos._distintivo("faturamento", cont, len(docs))


def test_secao_do_assunto_abre_antes_da_secao_longa():
    """O cabeçalho nomeia o assunto; o corpo de seção longa engole conversa."""
    docs, _, _ = processos.buscar("erro de faturamento")
    sup = next(d for d in docs if d["titulo"] == "Playbook de Suporte")
    assert any("ERRO DE FATURAMENTO" in s["titulo"].upper() for s in sup["secoes"]), \
        "a seção com o assunto no título precisa estar entre as que casaram"


def test_termo_especifico_sozinho_e_no_maximo_indireto():
    docs, _, _ = processos.buscar("faturamento")
    assert docs
    for d in docs:
        assert d["_confianca"] in ("media", "fraco")
        assert d["_confianca"] != "forte"


def test_dois_termos_do_caso_viram_evidencia_forte():
    docs, _, _ = processos.buscar("erro de faturamento")
    assert docs[0]["titulo"] == "Fluxo: Erro de Faturamento"
    assert docs[0]["_confianca"] == "forte"
    assert set(docs[0]["_casados"]) == {"erro", "faturamento"}


def test_evidencia_e_reportada_para_o_usuario():
    """O painel precisa dizer o que leu, não só listar documentos parecidos."""
    docs, _, _ = processos.buscar("erro de faturamento")
    d = docs[0]
    assert d["_confianca"] in ("forte", "media", "fraco")
    assert d["_casados"] and d["_distintivos"]
    assert isinstance(d["_ignorados"], dict)
    assert set(d["_ignorados"]) == {"lista", "total"}


def test_checklist_nao_imposta_passos_com_somente_termo_generico():
    chk = processos.checklist("boleto")
    assert chk["passos"] == []
    assert chk["fontes"] == []
    assert chk["confianca"] == ""
    assert "genéricos" in chk["aviso"]


def test_checklist_confirma_evidencia_forte():
    chk = processos.checklist("erro de faturamento")
    assert chk["passos"]
    assert chk["confianca"] == "forte"
    assert chk["aviso"] == ""


def test_texto_do_chat_e_cortado_no_limite():
    """Chat colado nao pode estourar o custo da busca."""
    grande = "faturamento " * (processos._MAX_CHARS_CHAT // 14 + 50)
    assert len(grande) > processos._MAX_CHARS_CHAT
    docs, _, termos = processos.buscar(grande)
    assert docs
    assert termos


def test_arquivo_sem_cabecalho_vira_secao_unica(tmp_path, monkeypatch):
    base = tmp_path / "processos"
    base.mkdir()
    (base / "index.json").write_text(
        json.dumps({"unico.txt": {"titulo": "Processo Único", "times": ["suporte"], "tags": []}}),
        encoding="utf-8",
    )
    (base / "unico.txt").write_text("Passo 1\nLigar para o cliente.\nPasso 2\nAguardar retorno.",
                                    encoding="utf-8")
    original = processos._BASE
    monkeypatch.setattr(processos, "_BASE", str(base))
    processos._CACHE = []
    processos._CACHE_CHAVES = None
    try:
        docs = processos.carregar()
        assert len(docs) == 1
        assert len(docs[0]["secoes"]) == 1
        assert "Ligar para o cliente" in docs[0]["secoes"][0]["texto"]
    finally:
        processos._BASE = original
        processos._CACHE = []
        processos._CACHE_CHAVES = None