"""Regressão dos indicadores de SLA/vencimento (dados.build_sla_vencimentos).

O backend deriva o vencimento das regras do playbook (resposta 1h útil, encerrar
5 dias úteis, concluir 3 dias úteis) e conta tudo em horário comercial. Os testes
usam datas relativas ao "agora" real e bases folgadas, para não depender do
relógio nem do dia da semana.
"""
from datetime import datetime, timedelta

import dados

CLIENTE = {"email": "cliente@exemplo.com", "businessName": "Cliente"}
EQUIPE = {"email": "lucas@faturagil.com.br", "businessName": "Lucas"}


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S-03:00")


def _agora():
    return datetime.now().astimezone()


def _tk(ident, base, just="", quem=CLIENTE, status="Em Atendimento", **kw):
    actions = []
    if quem is not None:
        actions = [{"createdDate": _iso(base), "createdBy": quem}]
    t = {
        "id": ident,
        "subject": "Boleto nao registrado",
        "baseStatus": "New",
        "status": status,
        "justification": just,
        "clients": [{"businessName": "ACME"}],
        "createdDate": _iso(base - timedelta(days=2)),
        "lastUpdate": _iso(base),
        "owner": {"businessName": "Lucas", "email": "lucas@faturagil.com.br"},
        "actions": actions,
    }
    t.update(kw)
    return t


def _preparar(monkeypatch, ativos):
    raw = {"agentes": [], "por_agente": {"*": {"ativos": ativos, "resolvidos": []}}, "historico": []}
    monkeypatch.setattr(dados, "load_cache_raw", lambda *a, **k: raw)


def test_resposta_vencida_aguardando_cliente(monkeypatch):
    base = _agora() - timedelta(days=3)
    _preparar(monkeypatch, [_tk(1, base, quem=CLIENTE)])
    r = dados.build_sla_vencimentos("*")
    assert r["resposta"]["total"] == 1
    assert r["resposta"]["vencidos"] == 1
    assert r["resposta"]["itens"][0]["nivel"] == "VENCIDO"
    assert r["resposta"]["itens"][0]["restante_min"] < 0


def test_resposta_novo_sem_acao(monkeypatch):
    base = _agora() - timedelta(days=3)
    _preparar(monkeypatch, [_tk(2, base, quem=None, status="Novo", lastActionDate=None)])
    r = dados.build_sla_vencimentos("*")
    assert r["resposta"]["total"] == 1


def test_resposta_exclui_retorno_do_cliente(monkeypatch):
    base = _agora() - timedelta(days=3)
    _preparar(monkeypatch, [
        _tk(3, base, just="Retorno do Cliente - Continuidade", quem=EQUIPE),
        _tk(4, base, just="Retorno do Cliente - Encerramento", quem=EQUIPE),
    ])
    r = dados.build_sla_vencimentos("*")
    assert r["resposta"]["total"] == 0
    assert r["continuidade"]["total"] == 1
    assert r["encerramento"]["total"] == 1


def test_continuidade_precisa_ligar(monkeypatch):
    base = _agora() - timedelta(days=3)
    _preparar(monkeypatch, [_tk(5, base, just="Retorno do Cliente - Continuidade", quem=EQUIPE)])
    r = dados.build_sla_vencimentos("*")
    it = r["continuidade"]["itens"][0]
    assert it["precisa_ligar"] is True
    assert r["continuidade"]["ligar"] == 1


def test_continuidade_recente_nao_liga(monkeypatch):
    base = _agora() - timedelta(minutes=5)
    _preparar(monkeypatch, [_tk(6, base, just="Retorno do Cliente - Continuidade", quem=EQUIPE)])
    r = dados.build_sla_vencimentos("*")
    assert r["continuidade"]["itens"][0]["precisa_ligar"] is False


def test_encerramento_prazo_atingido_pode_encerrar(monkeypatch):
    # 30 dias para trás: mesmo com o feriado de 12/10 no meio, o prazo de 5
    # dias úteis cai com folga no passado.
    base = _agora() - timedelta(days=30)
    _preparar(monkeypatch, [_tk(7, base, just="Retorno do Cliente - Encerramento", quem=EQUIPE)])
    r = dados.build_sla_vencimentos("*")
    it = r["encerramento"]["itens"][0]
    # Passado o prazo de auto-resolução NÃO é VENCIDO (não é descumprimento
    # de SLA): é ATINGIDO e fica fora do card "Vencidos".
    assert it["nivel"] == "ATINGIDO"
    assert it["pode_encerrar"] is True
    assert it["restante_min"] < 0
    assert it["restante_txt"].startswith("prazo atingido")
    assert r["encerramento"]["vencidos"] == 0
    assert r["encerramento"]["atingidos"] == 1
    assert "playbook" in r["encerramento"]["texto_padrao"].lower()


def test_encerramento_no_prazo(monkeypatch):
    base = _agora() - timedelta(hours=1)
    _preparar(monkeypatch, [_tk(8, base, just="Retorno do Cliente - Encerramento", quem=EQUIPE)])
    r = dados.build_sla_vencimentos("*")
    it = r["encerramento"]["itens"][0]
    assert it["nivel"] in ("OK", "PROXIMO")
    assert it["pode_encerrar"] is False
    assert r["encerramento"]["vencidos"] == 0
    assert r["encerramento"]["atingidos"] == 0


def test_concluir_generico(monkeypatch):
    base = _agora() - timedelta(days=10)
    _preparar(monkeypatch, [_tk(9, base, just="Retorno do Cliente", quem=EQUIPE)])
    r = dados.build_sla_vencimentos("*")
    assert r["concluir"]["total"] == 1
    assert r["concluir"]["vencidos"] == 1


def test_escopo_por_agente(monkeypatch):
    base = _agora() - timedelta(days=3)
    t1 = _tk(10, base, quem=CLIENTE, owner={"businessName": "Lucas", "email": "lucas@faturagil.com.br"})
    t2 = _tk(11, base, quem=CLIENTE, owner={"businessName": "Jeff", "email": "jeff@faturagil.com.br"})
    raw = {"agentes": [], "por_agente": {
        "*": {"ativos": [t1, t2], "resolvidos": []},
        "lucas@faturagil.com.br": {"ativos": [t1], "resolvidos": []},
    }, "historico": []}
    monkeypatch.setattr(dados, "load_cache_raw", lambda *a, **k: raw)
    r = dados.build_sla_vencimentos("lucas@faturagil.com.br")
    assert r["escopo"] == "carga"
    assert r["resposta"]["total"] == 1


def test_shape_e_avisos(monkeypatch):
    base = _agora() - timedelta(days=3)
    _preparar(monkeypatch, [_tk(12, base, quem=CLIENTE)])
    r = dados.build_sla_vencimentos("*")
    assert set(("gerado_em", "escopo", "janela", "regras", "resposta",
                "continuidade", "encerramento", "concluir", "avisos")) <= set(r)
    assert r["janela"] == "08:00-12:00 e 13:00-18:00"
    assert len(r["avisos"]) == 2


# --- Orientações do playbook (responsável + próximo passo, por status) ---


def test_itens_carregam_orientacao_do_playbook(monkeypatch):
    base = _agora() - timedelta(days=3)
    _preparar(monkeypatch, [_tk(20, base, just="Retorno do Cliente - Continuidade", quem=EQUIPE)])
    r = dados.build_sla_vencimentos("*")
    it = r["continuidade"]["itens"][0]
    assert it["responsavel"] == "Cliente"
    assert it["do_suporte"] is True
    assert "LIGAR" in it["sugestao"].upper()
    assert "playbook" in it["regra"].lower()
    assert "responsabilidades" in r
    assert r["areas"]["total"] == 0


def test_areas_reune_o_que_nao_e_do_suporte(monkeypatch):
    base = _agora() - timedelta(days=3)
    _preparar(monkeypatch, [
        _tk(21, base, just="Aguardando » Equipe de análise", quem=EQUIPE),
        _tk(22, base, just="Aguardando » Retorno de Fornecedor", quem=EQUIPE),
        _tk(23, base, quem=CLIENTE),  # do Suporte: entra na resposta
    ])
    r = dados.build_sla_vencimentos("*")
    assert r["resposta"]["total"] == 1
    assert r["areas"]["total"] == 2
    resp = {it["id"]: it for it in r["areas"]["itens"]}
    assert resp[21]["responsavel"] == "Engenharia e Tecnologia"
    assert resp[21]["do_suporte"] is False
    assert resp[22]["responsavel"] == "Fornecedor"
    assert resp[22]["do_suporte"] is True


def test_referencia_de_responsabilidades_presente(monkeypatch):
    base = _agora() - timedelta(days=1)
    _preparar(monkeypatch, [_tk(24, base, quem=CLIENTE)])
    r = dados.build_sla_vencimentos("*")
    refs = r["responsabilidades"]
    assert len(refs) >= 5
    assert all(o.get("titulo") and o.get("responsavel") and o.get("regra") for o in refs)
    assert "playbook" in r["escopo_suporte"].lower()
