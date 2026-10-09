from datetime import datetime

from analyzer import TicketInfo
from config import AGENT_NAME, CRITICAL_HOURS, WARNING_HOURS

SEPARATOR = "=" * 40


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


def ticket_rows(tickets: list[TicketInfo]) -> list[list[str]]:
    rows = []
    for t in tickets:
        last = t.last_action.strftime("%d/%m %H:%M") if t.last_action else "sem registro"
        rows.append([
            f"#{t.id}",
            t.client[:32],
            t.subject[:60],
            last,
            t.idle_display,
            t.status,
        ])
    return rows


def merge_rows(candidates: list[dict]) -> list[list[str]]:
    return [
        [
            f"#{c['ticket_a']}",
            f"#{c['ticket_b']}",
            c["client"][:28],
            c["reason"],
            f"{c['action']} (pai #{c['parent']})",
        ]
        for c in candidates
    ]


def sla_rows(tickets: list[TicketInfo]) -> list[list[str]]:
    return [
        [f"#{t.id}", t.client[:28], t.sla_level, t.sla_detail, t.subject[:45]]
        for t in tickets
    ]


def build_report(
    critical: list[TicketInfo],
    warning: list[TicketInfo],
    ok: list[TicketInfo],
    candidates: list[dict],
    sla_in_risk: list[TicketInfo],
    total: int,
    now: datetime,
) -> str:
    actions = []
    for t in sorted(critical, key=lambda x: -(x.hours_idle or 0)):
        actions.append(
            f"Follow-up no ticket #{t.id} ({t.client}) — sem interacao ha {t.idle_display} "
            f"(status: {t.status}, segue aberto)"
        )
    for c in candidates:
        if c["action"] == "Mesclar":
            actions.append(
                f"Mesclar ticket #{c['ticket_a']} em #{c['ticket_b']} ({c['client']}) — {c['reason'].lower()}"
            )
    for t in sla_in_risk:
        actions.append(f"SLA {t.sla_level.lower()} no ticket #{t.id} ({t.client}) — {t.sla_detail}")

    lines = [
        SEPARATOR,
        f"RELATORIO DE MONITORAMENTO — {AGENT_NAME.upper()}",
        f"Gerado em: {now.strftime('%d/%m/%Y %H:%M:%S')}",
        SEPARATOR,
        "",
        "RESUMO",
        f"Tickets ativos: {total}",
        f"Tickets criticos (>{CRITICAL_HOURS}h parado): {len(critical)}",
        f"Tickets em atencao (>{WARNING_HOURS}h parado): {len(warning)}",
        f"Candidatos a mescla: {len(candidates)}",
        f"SLA em risco/vencido: {len(sla_in_risk)}",
        "",
        "🔴 TICKETS CRÍTICOS",
        format_table(
            ["Ticket", "Cliente", "Assunto", "Última Ação", "Horas Parado", "Status"],
            ticket_rows(critical),
        ),
        "",
        "🟡 TICKETS EM ATENÇÃO",
        format_table(
            ["Ticket", "Cliente", "Assunto", "Ultima Acao", "Horas Parado", "Status"],
            ticket_rows(warning),
        ),
        "",
        "🔗 CANDIDATOS A MESCLA",
        format_table(
            ["Ticket A", "Ticket B", "Cliente", "Motivo", "Acao Sugerida"],
            merge_rows(candidates),
        ),
        "",
        "SLA EM RISCO",
        format_table(
            ["Ticket", "Cliente", "Nivel", "Detalhe", "Assunto"],
            sla_rows(sla_in_risk),
        ),
        "",
        "📋 AÇÕES SUGERIDAS",
        "\n".join(f"- {a}" for a in actions) if actions else "- Nenhuma acao urgente.",
        "",
        f"Tickets OK (<{WARNING_HOURS}h): {len(ok)}",
    ]
    return "\n".join(lines)


def save_report(content: str, now: datetime, reports_dir) -> str:
    reports_dir.mkdir(parents=True, exist_ok=True)
    path = reports_dir / f"relatorio_{now.strftime('%Y%m%d_%H%M%S')}.txt"
    path.write_text(content, encoding="utf-8")
    return str(path)
