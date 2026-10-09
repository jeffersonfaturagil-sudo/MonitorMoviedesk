"""Testes da configuração de times (Leandro migrou para dev: suporte = Jefferson + Lucas)."""
from config import AGENTES_DEV, AGENTES_SUPORTE, PLANTAO_ORDEM, papel_agente


def test_times_sao_disjuntos_e_nao_vazios():
    assert AGENTES_SUPORTE, "espera-se ao menos um atendente de suporte"
    assert AGENTES_DEV, "espera-se o time de dev configurado"
    assert not (AGENTES_SUPORTE & AGENTES_DEV), "um atendente não pode estar nos dois times"


def test_papel_agente_classifica_por_email():
    for email in AGENTES_SUPORTE:
        assert papel_agente(email) == "suporte"
    for email in AGENTES_DEV:
        assert papel_agente(email) == "dev"
    assert papel_agente("alguem.de.outra.area@faturagil.com.br") == "outros"
    assert papel_agente("") == "outros"
    assert papel_agente(None) == "outros"
    assert papel_agente("  Lucas@Faturagil.com.br  ".lower()) in ("suporte", "dev", "outros")


def test_plantao_so_escala_suporte():
    assert PLANTAO_ORDEM, "a ordem do plantão não pode ficar vazia"
    for email in PLANTAO_ORDEM:
        assert papel_agente(email) == "suporte", f"{email} não é do suporte"


def test_defaults_do_time_de_suporte():
    assert "jefferson@faturagil.com.br" in AGENTES_SUPORTE
    assert "lucas@faturagil.com.br" in AGENTES_SUPORTE
    assert "leandro@faturagil.com.br" in AGENTES_DEV


def test_papel_normaliza_maiusculas():
    for email in AGENTES_SUPORTE:
        assert papel_agente(email.upper()) == "suporte"
    for email in AGENTES_DEV:
        assert papel_agente(email.upper()) == "dev"
