import re
from dataclasses import dataclass, field
from datetime import datetime

from config import (
    CRITICAL_HOURS,
    SIMILARITY_THRESHOLD,
    SLA_RISK_HOURS,
    WARNING_HOURS,
)

STOPWORDS = {
    "de", "do", "da", "dos", "das", "em", "no", "na", "nos", "nas", "com", "por",
    "para", "por", "um", "uma", "que", "e", "ou", "the", "of", "to", "in", "on",
    "erro", "erro:", "favor", "ajuda", "ticket", "solicitacao", "solicitação",
}

STATUS_LABELS = {
    "CRITICO": "CRITICO",
    "ATENCAO": "ATENCAO",
    "OK": "OK",
}


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1]
    if "." in raw:
        head, _, frac = raw.partition(".")
        frac = re.sub(r"\D", "", frac)[:6].ljust(6, "0")
        raw = f"{head}.{frac}"
    try:
        d = datetime.fromisoformat(raw)
    except ValueError:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                d = datetime.strptime(raw[:19], fmt)
                break
            except ValueError:
                d = None
        if d is None:
            return None
    try:
        from datetime import timezone
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.astimezone()
    except Exception:
        return d


def client_name(ticket: dict) -> str:
    for client in ticket.get("clients") or []:
        org = client.get("organization") or {}
        name = org.get("businessName") or client.get("businessName")
        if name:
            return str(name).strip()
    return "N/D"


def hours_between(start: datetime | None, end: datetime) -> float | None:
    if not start:
        return None
    try:
        if start.tzinfo is None and end.tzinfo is not None:
            start = start.replace(tzinfo=end.tzinfo)
        elif end.tzinfo is None and start.tzinfo is not None:
            end = end.replace(tzinfo=start.tzinfo)
    except Exception:
        pass
    return (end - start).total_seconds() / 3600.0


def classify_idle(hours: float | None) -> str:
    if hours is None:
        return "CRITICO"
    if hours > CRITICAL_HOURS:
        return "CRITICO"
    if hours > WARNING_HOURS:
        return "ATENCAO"
    return "OK"


def classify_sla(ticket: dict, now: datetime) -> tuple[str, str]:
    """Retorna (nivel, descricao). Niveis: VENCIDO, RISCO, OK, SEM_SLA."""
    sla_date = parse_date(ticket.get("slaSolutionDate"))
    if sla_date:
        remaining = hours_between(now, sla_date)
        if remaining is None:
            return "SEM_SLA", "SLA sem data"
        if remaining < 0:
            return "VENCIDO", f"SLA vencido ha {abs(remaining):.1f}h"
        if remaining <= SLA_RISK_HOURS:
            return "RISCO", f"SLA vence em {remaining:.1f}h"
        return "OK", f"SLA vence em {remaining:.1f}h"

    response_date = parse_date(ticket.get("slaResponseDate"))
    if response_date:
        remaining = hours_between(now, response_date)
        if remaining is not None and remaining < 0:
            return "VENCIDO", f"SLA de resposta vencido ha {abs(remaining):.1f}h"
        if remaining is not None and remaining <= SLA_RISK_HOURS:
            return "RISCO", f"SLA de resposta vence em {remaining:.1f}h"
        return "OK", "SLA de resposta no prazo"

    if ticket.get("slaAgreement") or ticket.get("slaSolutionTime"):
        return "RISCO", "SLA configurado sem data calculada"

    return "SEM_SLA", "Sem SLA configurado"


@dataclass
class TicketInfo:
    id: int
    subject: str
    client: str
    status: str
    base_status: str
    service_first: str
    service_second: str
    created: datetime | None
    last_action: datetime | None
    last_update: datetime | None
    hours_idle: float | None
    days_open: float | None
    level: str
    sla_level: str
    sla_detail: str
    urgency: str
    raw: dict = field(repr=False, default_factory=dict)

    @property
    def idle_display(self) -> str:
        if self.hours_idle is None:
            return "sem registro"
        if self.hours_idle >= 48:
            days, hours = divmod(int(self.hours_idle), 24)
            return f"{days}d {hours}h"
        return f"{self.hours_idle:.1f}h"


def build_ticket_info(ticket: dict, now: datetime) -> TicketInfo:
    created = parse_date(ticket.get("createdDate"))
    last_action = parse_date(ticket.get("lastActionDate")) or parse_date(ticket.get("lastUpdate"))
    last_update = parse_date(ticket.get("lastUpdate"))
    hours_idle = hours_between(last_action, now)
    days_open = hours_between(created, now)
    sla_level, sla_detail = classify_sla(ticket, now)
    return TicketInfo(
        id=int(ticket.get("id")),
        subject=(ticket.get("subject") or "").strip(),
        client=client_name(ticket),
        status=ticket.get("status") or "",
        base_status=ticket.get("baseStatus") or "",
        service_first=ticket.get("serviceFirstLevel") or "",
        service_second=ticket.get("serviceSecondLevel") or "",
        created=created,
        last_action=last_action,
        last_update=last_update,
        hours_idle=hours_idle,
        days_open=(days_open / 24.0) if days_open is not None else None,
        level=classify_idle(hours_idle),
        sla_level=sla_level,
        sla_detail=sla_detail,
        urgency=ticket.get("urgency") or "",
        raw=ticket,
    )


def levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i]
        for j, cb in enumerate(b, 1):
            curr.append(min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = curr
    return prev[-1]


def normalize_subject(subject: str) -> str:
    text = subject.upper()
    parts = [p.strip() for p in text.split(" - ")]
    if len(parts) > 1:
        text = " - ".join(parts[:-1]) if len(parts[-1]) > 3 else text
    text = re.sub(r"[^A-Z0-9À-Ü\s]", " ", text)
    tokens = [t for t in text.split() if t not in STOPWORDS and len(t) > 2]
    return " ".join(tokens)


def subject_similarity(a: str, b: str) -> float:
    na, nb = normalize_subject(a), normalize_subject(b)
    if not na or not nb:
        return 0.0
    dist = levenshtein(na, nb)
    lev_sim = 1.0 - dist / max(len(na), len(nb))
    ta, tb = set(na.split()), set(nb.split())
    jaccard = len(ta & tb) / len(ta | tb) if ta | tb else 0.0
    return max(lev_sim, jaccard)


def find_merge_candidates(tickets: list[TicketInfo]) -> list[dict]:
    open_tickets = [
        t for t in tickets
        if t.base_status not in ("Resolved", "Closed", "Canceled") and t.id
    ]
    by_client: dict[str, list[TicketInfo]] = {}
    for t in open_tickets:
        by_client.setdefault(t.client, []).append(t)

    candidates = []
    for client, group in by_client.items():
        if client in ("N/D", "") or len(group) < 2:
            continue
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                if a.id == b.id:
                    continue
                similarity = subject_similarity(a.subject, b.subject)
                same_service = bool(
                    a.service_first and a.service_first == b.service_first
                )
                same_sub_service = same_service and a.service_second == b.service_second

                is_candidate = similarity >= SIMILARITY_THRESHOLD or (same_service and same_sub_service)
                if not is_candidate:
                    continue

                if similarity >= SIMILARITY_THRESHOLD and same_sub_service:
                    reason = (
                        f"Assunto similar ({similarity:.0%}) e mesmo serviço: "
                        f"{a.service_first} / {a.service_second}"
                    )
                    action = "Mesclar"
                elif similarity >= SIMILARITY_THRESHOLD:
                    reason = f"Assunto similar ({similarity:.0%}) — mesmo cliente"
                    action = "Verificar"
                elif same_sub_service:
                    reason = (
                        f"Mesmo cliente e mesmo serviço: {a.service_first} / {a.service_second}"
                    )
                    action = "Verificar"
                else:
                    reason = f"Mesmo primeiro nível de serviço: {a.service_first}"
                    action = "Verificar"

                older, newer = (a, b) if (a.created or now_min()) <= (b.created or now_min()) else (b, a)

                def _rel(t: TicketInfo) -> tuple[set, set]:
                    pais = {str(p.get("id")) for p in (t.raw.get("parentTickets") or []) if isinstance(p, dict)}
                    filhos = {str(c.get("id")) for c in (t.raw.get("childrenTickets") or []) if isinstance(c, dict)}
                    return pais, filhos

                pais_a, filhos_a = _rel(a)
                pais_b, filhos_b = _rel(b)
                if (str(b.id) in filhos_a or str(b.id) in pais_a
                        or str(a.id) in filhos_b or str(a.id) in pais_b):
                    continue
                ja_vinculado = bool(pais_a or filhos_a or pais_b or filhos_b)
                if ja_vinculado and action == "Mesclar":
                    action = "Verificar"
                candidates.append({
                    "ticket_a": older.id,
                    "ticket_b": newer.id,
                    "subject_a": older.subject,
                    "subject_b": newer.subject,
                    "client": client,
                    "similarity": similarity,
                    "reason": reason,
                    "action": action,
                    "ja_vinculado": ja_vinculado,
                    "parent": older.id,
                    "merge_into": newer.id,
                    "situation_a": older.raw.get("justification") or older.status,
                    "situation_b": newer.raw.get("justification") or newer.status,
                    "service_a": " / ".join(x for x in (older.service_first, older.service_second) if x),
                    "service_b": " / ".join(x for x in (newer.service_first, newer.service_second) if x),
                })

    candidates.sort(key=lambda c: (-c["similarity"], c["client"]))
    return candidates


def now_min() -> datetime:
    return datetime.now()


def sla_risks(tickets: list[TicketInfo]) -> list[TicketInfo]:
    return [t for t in tickets if t.sla_level in ("VENCIDO", "RISCO")]
