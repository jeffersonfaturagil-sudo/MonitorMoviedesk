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
    assert len(docs) == 13
    for d in docs:
        assert d["secoes"], f"sem secoes em {d['arquivo']}"
        for s in d["secoes"]:
            assert s["texto"].strip(), f"secao vazia em {d['arquivo']}"
            assert s["titulo"].strip()


def test_busca_erro_faturamento():
    docs, _ = processos.buscar("erro de faturamento")
    titulos = [d["titulo"] for d in docs]
    assert "Fluxo: Erro de Faturamento" in titulos
    primeiro = docs[0]
    assert primeiro["_score"] > 900
    assert all(s["titulo"] or s["texto"] for s in primeiro["secoes"])


def test_busca_por_time():
    docs, _ = processos.buscar("", time="implantacao")
    for d in docs:
        assert "implantacao" in d["times"]
    docs, _ = processos.buscar("", time="coordenacao")
    titulos = [d["titulo"] for d in docs]
    assert "KPI de Suporte" in titulos


def test_busca_por_tag_e_sem_resultado():
    docs, _ = processos.buscar("", tag="faturamento")
    assert docs and any("Erro" in d["titulo"] for d in docs)
    docs, _ = processos.buscar("zzznadaexiste", tag="faturamento")
    assert not docs


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