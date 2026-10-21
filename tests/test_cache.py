"""Regressão do cache compartilhado (painel e CLI no mesmo cache/tickets.json).

O CLI antigo gravava uma lista pura no arquivo do painel: `load_cache_raw()` só
aceitava dict, então o painel passava a ver tudo vazio e a próxima consulta
recriava o cache apagando pesquisa de satisfação, histórico e filas locais.
Estes testes garantem que (1) formatos antigos são migrados sem perder dados,
(2) a escrita preserva o resto do cache e (3) o painel enxerga o que o CLI gravou.
"""
import json
from datetime import datetime, timedelta

import dados


def _tk(i, **kw):
    t = {
        "id": i,
        "subject": "Boleto nao registrado",
        "baseStatus": "New",
        "status": "Novo",
        "createdDate": "2026-10-13T08:00:00-03:00",
        "lastActionDate": "2026-10-13T09:00:00-03:00",
        "clients": [{"businessName": "Cliente X"}],
        "owner": {"businessName": "Lucas", "email": "lucas@faturagil.com.br"},
    }
    t.update(kw)
    return t


def _cache_file(monkeypatch, tmp_path):
    caminho = tmp_path / "tickets.json"
    monkeypatch.setattr(dados, "CACHE_FILE", caminho)
    return caminho


def _bucket(raw):
    return raw["por_agente"][dados._chave_agente_padrao()]


def test_cache_em_lista_do_cli_antigo_e_migrado_sem_perder_tickets(monkeypatch, tmp_path):
    caminho = _cache_file(monkeypatch, tmp_path)
    caminho.write_text(json.dumps([_tk(1), _tk(2)]), encoding="utf-8")

    raw = dados.load_cache_raw()

    assert raw["schema"] == dados.CACHE_SCHEMA
    assert [t["id"] for t in _bucket(raw)["ativos"]] == [1, 2]
    # o arquivo passa a ficar no formato novo e o antigo vira backup
    assert json.loads(caminho.read_text(encoding="utf-8"))["schema"] == dados.CACHE_SCHEMA
    assert dados._arquivo_legado().exists()


def test_painel_enxerga_os_tickets_migrados(monkeypatch, tmp_path):
    caminho = _cache_file(monkeypatch, tmp_path)
    caminho.write_text(json.dumps([_tk(1)]), encoding="utf-8")

    payload = dados.load_cache_payload(dados._chave_agente_padrao())

    assert payload is not None
    assert payload["resumo"]["ativos"] == 1
    ids = [t["id"] for t in payload["criticos"] + payload["atencao"] + payload["ok"]]
    assert ids == [1]


def test_cache_antigo_com_ativos_na_raiz_vira_bucket_sem_perder_o_resto(monkeypatch, tmp_path):
    caminho = _cache_file(monkeypatch, tmp_path)
    caminho.write_text(json.dumps({
        "agentes": [{"nome": "Lucas", "email": "lucas@faturagil.com.br"}],
        "survey": [{"ticketId": 1, "type": 2, "value": 5}],
        "survey_salvo_em": "2026-10-01T10:00:00",
        "historico": [_tk(8, baseStatus="Resolved")],
        "ativos": [_tk(1)],
        "resolvidos": [_tk(2, baseStatus="Resolved")],
        "salvo_em": "2026-10-12T08:00:00",
    }, ensure_ascii=False), encoding="utf-8")

    raw = dados.load_cache_raw()

    assert raw["agentes"][0]["nome"] == "Lucas"
    assert raw["survey"][0]["ticketId"] == 1
    assert raw["historico"][0]["id"] == 8
    assert "ativos" not in raw and "resolvidos" not in raw
    assert [t["id"] for t in _bucket(raw)["ativos"]] == [1]
    assert [t["id"] for t in _bucket(raw)["resolvidos"]] == [2]
    assert _bucket(raw)["salvo_em"] == "2026-10-12T08:00:00"


def test_gravacao_de_um_atendente_preserva_survey_historico_e_filas(monkeypatch, tmp_path):
    caminho = _cache_file(monkeypatch, tmp_path)
    caminho.write_text(json.dumps({
        "schema": dados.CACHE_SCHEMA,
        "agentes": [{"nome": "Lucas", "email": "lucas@faturagil.com.br"}],
        "survey": [{"ticketId": 1, "type": 2, "value": 5}],
        "survey_salvo_em": "2026-10-01T10:00:00",
        "historico": [_tk(9, baseStatus="Resolved")],
        "solucoes": {"9": {"resposta": {"texto": "solucao"}}},
        "mesclas": {"1:2": {"status": "mesclado"}},
        "plantao": {"por_data": {"2026-10-13": "lucas@faturagil.com.br"}},
        "por_agente": {},
    }, ensure_ascii=False), encoding="utf-8")

    dados.salvar_bucket_agente(dados.AGENT_EMAIL, [_tk(1), _tk(2)])

    raw = json.loads(caminho.read_text(encoding="utf-8"))
    assert [t["id"] for t in _bucket(raw)["ativos"]] == [1, 2]
    for chave in ("agentes", "survey", "survey_salvo_em", "historico", "solucoes",
                  "mesclas", "plantao"):
        assert raw.get(chave), f"{chave} foi perdido na gravacao"
    assert raw["historico"][0]["id"] == 9
    assert raw["mesclas"] == {"1:2": {"status": "mesclado"}}


def test_cli_grava_no_formato_do_painel_e_preserva_o_cache(monkeypatch, tmp_path):
    import main as cli

    caminho = _cache_file(monkeypatch, tmp_path)
    caminho.write_text(json.dumps({
        "schema": dados.CACHE_SCHEMA,
        "survey": [{"ticketId": 1, "type": 2, "value": 5}],
        "historico": [_tk(7, baseStatus="Resolved")],
        "por_agente": {},
    }, ensure_ascii=False), encoding="utf-8")

    class ClienteFalso:
        chamadas = 0

        def list_active_tickets(self, owner=None):
            ClienteFalso.chamadas += 1
            return [_tk(1), _tk(2)]

    cli.fetch_tickets(ClienteFalso(), use_cache=False, max_age_minutes=30)

    raw = json.loads(caminho.read_text(encoding="utf-8"))
    assert raw["survey"][0]["ticketId"] == 1, "CLI apagou a pesquisa de satisfacao"
    assert raw["historico"][0]["id"] == 7, "CLI apagou o historico"
    bucket = _bucket(raw)
    assert [t["id"] for t in bucket["ativos"]] == [1, 2]
    assert bucket["origem"] == "cli"
    assert bucket["last_sync_odata"][-6] in "+-"  # OData com offset, igual ao painel

    # o painel enxerga os tickets que o CLI gravou
    payload = dados.load_cache_payload(dados._chave_agente_padrao())
    assert payload is not None and payload["resumo"]["ativos"] == 2

    # cache fresco: a segunda chamada do CLI nao bate na API de novo
    ticks = cli.fetch_tickets(ClienteFalso(), use_cache=True, max_age_minutes=30)
    assert [t["id"] for t in ticks] == [1, 2]
    assert ClienteFalso.chamadas == 1


def test_cache_velho_nao_e_usado(monkeypatch, tmp_path):
    caminho = _cache_file(monkeypatch, tmp_path)
    velho = (datetime.now() - timedelta(hours=2)).isoformat(timespec="seconds")
    caminho.write_text(json.dumps({
        "por_agente": {dados._chave_agente_padrao(): {"ativos": [_tk(1)], "salvo_em": velho}},
    }), encoding="utf-8")

    assert dados.tickets_do_cache(max_age_minutes=30) is None
    antigos = dados.tickets_do_cache(max_age_minutes=180)
    assert antigos is not None and [t["id"] for t in antigos] == [1]


def test_save_grava_schema_e_nao_deixa_temporario(monkeypatch, tmp_path):
    caminho = _cache_file(monkeypatch, tmp_path)

    dados.save_cache_raw({"por_agente": {}})

    raw = json.loads(caminho.read_text(encoding="utf-8"))
    assert raw["schema"] == dados.CACHE_SCHEMA
    assert not (tmp_path / "tickets.json.tmp").exists()
