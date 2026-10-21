"""Regressão da varredura única do cache (`dados.iter_cache_tickets`).

Antes cada consumidor repetia o laço `por_agente → chave` com a sua própria regra
de dedup e de prioridade: um ticket repetido em vários buckets podia aparecer duas
vezes, ou a cópia "errada" vencia (mudando resolvedIn e, com ele, o comparativo).
Estes testes fixam as duas ordens — por fonte e por bucket — e o que cada
consumidor faz com elas.

Fixture (`_cache`): o ticket 1 está no histórico E em a/ativos; o ticket 2 está em
a/ativos E b/ativos (cada cópia com uma quantidade diferente de ações); o ticket 3
está em a/resolvidos E b/resolvidos; os tickets 4 e 5 só em interagiu_hoje/fora_carga.
"""
from datetime import datetime

import dados
import macros


def _tk(i, **kw):
    t = {
        "id": i,
        "subject": f"ticket {i}",
        "baseStatus": "New",
        "status": "Novo",
        "createdDate": "2026-10-01T08:00:00-03:00",
        "lastActionDate": "2026-10-01T09:00:00-03:00",
        "resolvedIn": "2026-10-05T10:00:00-03:00",
        "clients": [{"businessName": "Cliente X"}],
        "owner": {"businessName": "Lucas", "email": "lucas@faturagil.com.br"},
    }
    t.update(kw)
    return t


def _acao(texto):
    return {"type": 2, "description": texto, "createdDate": "2026-10-05T10:00:00",
            "createdBy": {"businessName": "Lucas", "email": "lucas@faturagil.com.br"}}


def _cache(acoes_a=2, acoes_b=5):
    return {
        "schema": dados.CACHE_SCHEMA,
        "historico": [_tk(1, subject="copia historico")],
        "por_agente": {
            "a@faturagil.com.br": {
                "ativos": [_tk(1, subject="copia ativos A"),
                           _tk(2, subject="copia ativos A", actions=[_acao(f"a{i}") for i in range(acoes_a)])],
                "resolvidos": [_tk(3, subject="copia resolvidos A", actions=[_acao("r")])],
                "interagiu_hoje": [_tk(4)],
                "fora_carga": [_tk(5)],
            },
            "b@faturagil.com.br": {
                "ativos": [_tk(2, subject="copia ativos B", actions=[_acao(f"b{i}") for i in range(acoes_b)]),
                           _tk(6)],
                "resolvidos": [_tk(3, subject="copia resolvidos B")],
            },
        },
    }


def _fixar_cache(monkeypatch, raw):
    monkeypatch.setattr(dados, "load_cache_raw", lambda: raw)
    monkeypatch.setattr(dados, "_RECORRENTES", {"at": 0.0, "map": {}})


def _ids(pares):
    return [t["id"] for t, _origem in pares]


def test_iter_dedup_devolve_cada_id_uma_vez_com_a_origem(monkeypatch):
    pares = list(dados.iter_cache_tickets(_cache()))

    # ordem padrão: histórico, ativos (todos os buckets), resolvidos, interagiu, fora
    assert _ids(pares) == [1, 2, 6, 3, 4, 5]
    origens = {t["id"]: origem for t, origem in pares}
    assert origens[1] == "historico"      # o histórico vence a cópia do bucket
    assert origens[2] == "ativos"
    assert origens[6] == "ativos"
    assert origens[3] == "resolvidos"
    assert origens[4] == "interagiu_hoje"
    assert origens[5] == "fora_carga"
    assert [t["subject"] for t, _o in pares if t["id"] == 1] == ["copia historico"]


def test_iter_varre_todos_os_buckets_para_cada_fonte():
    assert _ids(dados.iter_cache_tickets(_cache(), ordem=("ativos",))) == [1, 2, 6]
    assert _ids(dados.iter_cache_tickets(_cache(), ordem=("resolvidos",))) == [3]


def test_iter_com_dedup_false_entrega_todas_as_copias():
    pares = list(dados.iter_cache_tickets(_cache(), ordem=("ativos",), dedup=False))
    assert _ids(pares) == [1, 2, 2, 6]
    copias_do_2 = [t["subject"] for t, _o in pares if t["id"] == 2]
    assert copias_do_2 == ["copia ativos A", "copia ativos B"]


def test_iter_por_bucket_preserva_a_precedencia_do_codigo_antigo():
    por_bucket = list(dados.iter_cache_tickets(
        _cache(), ordem=("ativos", "resolvidos"), por_bucket=True))
    assert _ids(por_bucket) == [1, 2, 3, 6]
    vencedores = {t["id"]: t["subject"] for t, _o in por_bucket}
    assert vencedores[2] == "copia ativos A"        # bucket "a" vem primeiro
    assert vencedores[3] == "copia resolvidos A"    # idem para resolvidos


def test_iter_ignora_ticket_sem_id_valido():
    raw = {"historico": [{"subject": "sem id"}, {"id": "abc"}], "por_agente": {}}
    assert list(dados.iter_cache_tickets(raw)) == []


def test_entries_do_cache_historico_primeiro_e_ativos_opcionais(monkeypatch):
    _fixar_cache(monkeypatch, _cache())

    sem_ativos = {t["id"]: t["subject"] for t in dados._entries_do_cache()}
    com_ativos = {t["id"]: t["subject"] for t in dados._entries_do_cache(incluir_ativos=True)}

    assert set(sem_ativos) == {1, 3}                 # histórico + resolvidos
    assert set(com_ativos) == {1, 2, 3, 6}           # ativos entram, interagiu/fora não
    assert com_ativos[1] == "copia historico"        # histórico vence a cópia do bucket
    assert com_ativos[3] == "copia resolvidos A"


def test_all_active_entries_so_ativos_sem_repetir(monkeypatch):
    _fixar_cache(monkeypatch, _cache())
    entradas = dados._all_active_entries()
    assert [t["id"] for t in entradas] == [1, 2, 6]
    assert entradas[1]["subject"] == "copia ativos A"


def test_mapa_horarios_conta_cada_ticket_uma_vez(monkeypatch):
    _fixar_cache(monkeypatch, _cache())
    mapa = dados.build_mapa_horarios()
    assert mapa["total"] == 4        # {1, 2, 3, 6} — o 2 está em dois buckets
    assert sum(mapa["por_dia"]) == 4


def test_macros_meta_local_acha_em_qualquer_fonte(monkeypatch):
    _fixar_cache(monkeypatch, _cache())

    assert macros.meta_local(4)["assunto"] == "ticket 4"          # só em interagiu_hoje
    assert macros.meta_local(5)["assunto"] == "ticket 5"          # só em fora_carga
    assert macros.meta_local(1)["assunto"] == "copia ativos A"    # buckets antes do histórico
    assert macros.meta_local("lixo") == {}
    assert macros.meta_local(999999) == {}


def test_macros_candidatos_respeita_a_prioridade_das_fontes(monkeypatch, tmp_path):
    monkeypatch.setattr(macros, "ACOES_FILE", tmp_path / "acoes_index.json")
    raw = _cache()
    raw["historico"].append(_tk(9, subject="antigo"))

    candidatos = {c["id"]: c["prio"] for c in macros.candidatos_para_indexar(raw)}

    assert candidatos[1] == 0    # ativos vence a cópia do histórico
    assert candidatos[6] == 0
    assert candidatos[4] == 1    # interagiu_hoje
    assert candidatos[5] == 2    # fora_carga
    assert candidatos[9] == 4    # histórico por último
    assert 2 not in candidatos, "as duas cópias do ticket 2 têm ações no cache"
    # O ticket 3 tem ações só na cópia de a/resolvidos; a cópia de b/resolvidos não
    # tem — e o código antigo (preservado aqui) ainda o coloca na fila.
    assert candidatos[3] == 3


def test_macros_candidatos_ignora_o_que_ja_esta_no_indice(monkeypatch, tmp_path):
    arquivo = tmp_path / "acoes_index.json"
    arquivo.write_text('{"salvo_em": "2026-10-13T10:00:00", "tickets": {"6": {"acoes": []}}}',
                       encoding="utf-8")
    monkeypatch.setattr(macros, "ACOES_FILE", arquivo)

    candidatos = {c["id"] for c in macros.candidatos_para_indexar(_cache())}
    assert 6 not in candidatos
    assert {1, 4, 5} <= candidatos


def test_macros_coletar_acoes_mantem_a_ultima_copia_varrida(monkeypatch, tmp_path):
    monkeypatch.setattr(macros, "ACOES_FILE", tmp_path / "acoes_index.json")

    fontes = macros.coletar_acoes(_cache(acoes_a=2, acoes_b=5))

    assert set(fontes) == {"2", "3"}                 # 4 e 5 não têm ações no cache
    assert len(fontes["2"]["acoes"]) == 5, "a última cópia varrida (bucket b) vence"
    assert len(fontes["3"]["acoes"]) == 1            # b/resolvidos não tem ações
    assert fontes["2"]["cliente"] == "Cliente X"


def test_estatisticas_de_macros_nao_duplicam_tickets(monkeypatch, tmp_path):
    monkeypatch.setattr(macros, "ACOES_FILE", tmp_path / "acoes_index.json")

    est = macros.estatisticas(_cache(acoes_a=2, acoes_b=5))

    assert est["indice"]["tickets_com_acoes"] == 2   # tickets 2 e 3, uma vez cada
    assert est["indice"]["acoes"] == 5 + 1


def test_comparativo_nao_conta_o_mesmo_ticket_duas_vezes(monkeypatch):
    """O comparativo lê _entries_do_cache: o ticket 3 não pode entrar duas vezes."""
    _fixar_cache(monkeypatch, _cache())
    entradas = dados._entries_do_cache(incluir_ativos=True)
    ids = [t["id"] for t in entradas]
    assert len(ids) == len(set(ids))
    assert sorted(ids) == [1, 2, 3, 6]

    anterior = dados.agente_atual()
    dados.definir_agente("lucas@faturagil.com.br")  # dono dos tickets da fixture
    try:
        comp = dados.build_comparativo(datetime(2026, 10, 13, 12, 0))
    finally:
        dados.definir_agente(anterior)
    assert comp["total_indice"] == len({1, 2, 3, 6})
