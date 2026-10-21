"""Regressão: o relatório do CLI é só formatação do payload do painel.

Antes havia duas UIs para o mesmo dado — `report.py` recalculava críticos, mesclas,
SLA e ações por conta própria (e sem desescapar HTML). Agora o CLI monta o payload
com `dados.build_payload` (a mesma fonte da tela) e o formatador só desenha.
"""
import inspect
import json
import sys
from datetime import datetime

import dados
import main as cli
import report


def _tk(i, **kw):
    t = {
        "id": i,
        "subject": "Boleto nao registrado",
        "baseStatus": "New",
        "status": "Novo",
        "createdDate": "2026-09-01T08:00:00-03:00",
        "lastActionDate": "2026-09-01T09:00:00-03:00",
        "clients": [{"businessName": "Cliente X"}],
        "owner": {"businessName": "Lucas", "email": "lucas@faturagil.com.br"},
    }
    t.update(kw)
    return t


def _linhas_de_acao(texto: str) -> list[str]:
    """Ações sugeridas do relatório, sem o bullet (para comparar com o payload)."""
    secao = texto.split("📋 AÇÕES SUGERIDAS")[1]
    return [linha[2:] for linha in secao.splitlines() if linha.startswith("- ")]


def _payload_exemplo() -> dict:
    return {
        "gerado_em": "2026-10-13T10:00:00",
        "resumo": {"ativos": 2, "criticos": 1, "atencao": 1, "ok": 0, "mesclas": 1, "sla": 1},
        "criticos": [{
            "id": 42, "cliente": "Cliente & Cia", "assunto": "Boleto não registrado",
            "ultima_acao": "13/10/2026 09:00", "parado_display": "3d 1h", "status": "Novo",
        }],
        "atencao": [{
            "id": 43, "cliente": "Outro", "assunto": "NFS-e",
            "ultima_acao": "13/10/2026 10:00", "parado_display": "30.0h", "status": "Aguardando",
        }],
        "ok": [],
        "mesclas": [{
            "ticket_a": 42, "ticket_b": 43, "cliente": "Cliente & Cia",
            "motivo": "Assunto similar (90%)", "acao": "Mesclar", "pai": 42,
        }],
        "sla": [{
            "id": 42, "cliente": "Cliente & Cia", "sla_nivel": "VENCIDO",
            "sla_detalhe": "SLA vencido ha 2h", "assunto": "Boleto",
        }],
        "acoes": [
            "Follow-up no ticket #42 (Cliente & Cia) — sem interacao ha 3d 1h "
            "(status: Novo, segue aberto)",
            "Mesclar ticket #42 em #43 (Cliente & Cia) — assunto similar (90%)",
        ],
    }


def test_relatorio_reproduz_o_payload_sem_recalcular():
    payload = _payload_exemplo()

    texto = report.build_report(payload, datetime(2026, 10, 13, 10, 0))

    assert _linhas_de_acao(texto) == payload["acoes"], "relatorio refez as acoes"
    assert "Tickets ativos: 2" in texto
    assert "Tickets criticos (>48h parado): 1" in texto
    assert "Tickets em atencao (>24h parado): 1" in texto
    assert "Candidatos a mescla: 1" in texto
    assert "SLA em risco/vencido: 1" in texto
    assert "#42" in texto and "#43" in texto
    assert "Mesclar (pai #42)" in texto
    assert "SLA vencido ha 2h" in texto
    assert "13/10/2026 10:00" in texto  # header com a data recebida


def test_relatorio_usa_os_numeros_do_payload_e_nao_a_lista_local():
    """Resumo vem do payload: listas vazias não zeram os totais da tela."""
    payload = {"resumo": {"ativos": 7, "criticos": 2, "atencao": 3, "ok": 2,
                          "mesclas": 0, "sla": 0}, "acoes": []}

    texto = report.build_report(payload, datetime(2026, 10, 13, 8, 0))

    assert "Tickets ativos: 7" in texto
    assert "Tickets criticos (>48h parado): 2" in texto
    assert "Nenhuma acao urgente." in texto
    assert "(nenhum registro)" in texto


def test_relatorio_aguenta_payload_vazio():
    texto = report.build_report({}, datetime(2026, 10, 13, 8, 0))
    assert "Tickets ativos: 0" in texto
    assert "Nenhuma acao urgente." in texto


def test_formatador_nao_calcula_regras_nem_le_o_relogio():
    """Guarda contra a volta da segunda UI: nada de analyzer/cálculo em report.py."""
    fonte = inspect.getsource(report)
    for proibido in ("analyzer", "find_merge_candidates", "sla_risks",
                     "build_ticket_info", "datetime.now()", "load_cache_raw"):
        assert proibido not in fonte, f"report.py voltou a calcular {proibido}"


def test_cli_monta_o_payload_do_painel_sem_bater_na_api(monkeypatch, tmp_path):
    caminho = tmp_path / "tickets.json"
    monkeypatch.setattr(dados, "CACHE_FILE", caminho)
    caminho.write_text(json.dumps({
        "schema": dados.CACHE_SCHEMA,
        "survey": [{"ticketId": 7, "type": 3, "value": 2,
                    "responseDate": "2026-10-12T10:00:00-03:00"}],
        "por_agente": {dados._chave_agente_padrao(): {
            "salvo_em": datetime.now().isoformat(timespec="seconds"),
            "ativos": [_tk(7), _tk(8)],
            "resolvidos": [],
        }},
    }, ensure_ascii=False), encoding="utf-8")

    class ClienteProibido:
        def __getattr__(self, nome):
            raise AssertionError(f"CLI chamou a API ({nome}) mesmo com cache valido")

    payload = cli.montar_payload(ClienteProibido(), use_cache=True, max_age_minutes=30)

    assert payload["resumo"]["ativos"] == 2
    assert payload["resumo"]["criticos"] == 2
    assert {t["id"] for t in payload["criticos"]} == {7, 8}
    # a pesquisa de satisfação do cache compartilhado entra nas ações do painel
    assert any("insatisfeito" in a for a in payload["acoes"]), payload["acoes"]

    texto = report.build_report(payload, datetime.now())
    assert _linhas_de_acao(texto) == payload["acoes"]
    assert texto.count("#7") >= 1 and texto.count("#8") >= 1

    resumo = cli.resumo_json(payload)
    assert resumo["tickets_ativos"] == 2
    assert sorted(resumo["criticos"]) == [7, 8]
    assert resumo["candidatos_mescla"] == payload["mesclas"]


def test_nota_interna_usa_os_criticos_do_payload(monkeypatch, tmp_path):
    import builtins

    monkeypatch.setattr(builtins, "print", lambda *a, **k: None)
    monkeypatch.setattr(cli, "REPORTS_DIR", tmp_path / "relatorios")
    monkeypatch.setattr(cli, "build_report", lambda *a, **k: "relatorio de teste")
    monkeypatch.setattr(cli, "confirm", lambda *a, **k: True)
    monkeypatch.setattr(cli, "montar_payload", lambda *a, **k: {
        "gerado_em": "2026-10-13T10:00:00",
        "resumo": {"ativos": 2, "criticos": 2, "atencao": 0, "ok": 0, "mesclas": 0, "sla": 0},
        "criticos": [{"id": 11, "parado_display": "3d 2h"}, {"id": 22, "parado_display": "5d"}],
        "atencao": [], "ok": [], "mesclas": [], "sla": [], "acoes": [],
    })

    class ClienteNotas:
        notas = []

        def add_internal_note(self, tid, desc):
            ClienteNotas.notas.append((tid, desc))
            return {"ok": True}

    monkeypatch.setattr(cli, "MovideskClient", ClienteNotas)
    monkeypatch.setattr(sys, "argv", ["main.py", "--nota"])

    assert cli.main() == 0
    assert [tid for tid, _ in ClienteNotas.notas] == [11, 22]
    assert "3d 2h" in ClienteNotas.notas[0][1]
