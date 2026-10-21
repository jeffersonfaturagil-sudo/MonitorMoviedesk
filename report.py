"""Formatação do relatório em texto do painel Movidesk.

O relatório **não recalcula nada**: recebe o payload que o painel já monta
(``painel/dados.build_payload`` — a mesma fonte da tela) e só formata em tabelas.
Era aqui que existia a segunda UI para o mesmo dado: este módulo refazia
críticos/mesclas/SLA/ações por conta própria e podia divergir da tela (além de
não desescapar HTML dos assuntos).
"""
from datetime import datetime

from config import AGENT_NAME, CRITICAL_HOURS, WARNING_HOURS

SEPARATOR = "=" * 40

CABECALHO_TICKETS = ["Ticket", "Cliente", "Assunto", "Última Ação", "Parado há", "Status"]
CABECALHO_MESCLAS = ["Ticket A", "Ticket B", "Cliente", "Motivo", "Ação Sugerida"]
CABECALHO_SLA = ["Ticket", "Cliente", "Nível", "Detalhe", "Assunto"]


def format_table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "(nenhum registro)"
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(str(cell)))

    def line(cells):
        return "| " + " | ".join(str(c).ljust(widths[i]) for i, c in enumerate(cells)) + " |"

    out = [line(headers)]
    out.append("|" + "|".join("-" * (w + 2) for w in widths) + "|")
    out.extend(line(r) for r in rows)
    return "\n".join(out)


def _txt(valor, limite: int | None = None) -> str:
    texto = " ".join(str(valor if valor is not None else "").split())
    return texto[:limite] if limite else texto


def _linhas_tickets(itens: list[dict]) -> list[list[str]]:
    return [
        [
            f"#{t.get('id')}",
            _txt(t.get("cliente"), 32),
            _txt(t.get("assunto"), 60),
            _txt(t.get("ultima_acao")) or "sem registro",
            _txt(t.get("parado_display")),
            _txt(t.get("status")),
        ]
        for t in itens or []
    ]


def _linhas_mesclas(itens: list[dict]) -> list[list[str]]:
    return [
        [
            f"#{m.get('ticket_a')}",
            f"#{m.get('ticket_b')}",
            _txt(m.get("cliente"), 28),
            _txt(m.get("motivo")),
            f"{_txt(m.get('acao'))} (pai #{m.get('pai')})",
        ]
        for m in itens or []
    ]


def _linhas_sla(itens: list[dict]) -> list[list[str]]:
    return [
        [
            f"#{t.get('id')}",
            _txt(t.get("cliente"), 28),
            _txt(t.get("sla_nivel")),
            _txt(t.get("sla_detalhe")),
            _txt(t.get("assunto"), 45),
        ]
        for t in itens or []
    ]


def build_report(payload: dict, now: datetime) -> str:
    """Monta o texto do relatório a partir do payload do painel (fonte única)."""
    payload = payload or {}
    resumo = payload.get("resumo") or {}
    acoes = payload.get("acoes") or []
    criticos = payload.get("criticos") or []
    atencao = payload.get("atencao") or []
    mesclas = payload.get("mesclas") or []
    sla = payload.get("sla") or []

    lines = [
        SEPARATOR,
        f"RELATORIO DE MONITORAMENTO — {(AGENT_NAME or '').upper()}",
        f"Gerado em: {now.strftime('%d/%m/%Y %H:%M:%S')}",
        SEPARATOR,
        "",
        "RESUMO",
        f"Tickets ativos: {resumo.get('ativos', 0)}",
        f"Tickets criticos (>{CRITICAL_HOURS}h parado): {resumo.get('criticos', len(criticos))}",
        f"Tickets em atencao (>{WARNING_HOURS}h parado): {resumo.get('atencao', len(atencao))}",
        f"Candidatos a mescla: {resumo.get('mesclas', len(mesclas))}",
        f"SLA em risco/vencido: {resumo.get('sla', len(sla))}",
        "",
        "🔴 TICKETS CRÍTICOS",
        format_table(CABECALHO_TICKETS, _linhas_tickets(criticos)),
        "",
        "🟡 TICKETS EM ATENÇÃO",
        format_table(CABECALHO_TICKETS, _linhas_tickets(atencao)),
        "",
        "🔗 CANDIDATOS A MESCLA",
        format_table(CABECALHO_MESCLAS, _linhas_mesclas(mesclas)),
        "",
        "SLA EM RISCO",
        format_table(CABECALHO_SLA, _linhas_sla(sla)),
        "",
        "📋 AÇÕES SUGERIDAS",
        "\n".join(f"- {a}" for a in acoes) if acoes else "- Nenhuma acao urgente.",
        "",
        f"Tickets OK (<{WARNING_HOURS}h): {resumo.get('ok', len(payload.get('ok') or []))}",
        f"Tickets sem risco (aba OK, risco baixo): {resumo.get('ok_risco', len(payload.get('ok_risco') or []))}",
        f"Aguardando terceiros (cliente/dev/fornecedor): "
        f"{resumo.get('aguardando_terceiros', len(payload.get('aguardando_terceiros') or []))}",
    ]
    return "\n".join(lines)


def save_report(content: str, now: datetime, reports_dir) -> str:
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"relatorio_{now.strftime('%Y%m%d_%H%M%S')}.txt"
    path.write_text(content, encoding="utf-8")
    return str(path)
