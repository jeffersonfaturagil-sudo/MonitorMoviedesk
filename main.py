import argparse
import json
import logging
import sys
from datetime import datetime

from analyzer import build_ticket_info, find_merge_candidates, sla_risks
from api_client import MovideskClient, MovideskError
from config import AGENT_EMAIL, CACHE_FILE, REPORTS_DIR
from report import build_report, save_report

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("movidesk")


def fetch_tickets(client: MovideskClient, use_cache: bool, max_age_minutes: int) -> list[dict]:
    if use_cache and CACHE_FILE.exists():
        age = datetime.now().timestamp() - CACHE_FILE.stat().st_mtime
        if age < max_age_minutes * 60:
            log.info("Usando cache local (%.0f min)", age / 60)
            data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data.get("ativos") or []
            return data
    tickets = client.list_active_tickets(AGENT_EMAIL)
    CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    CACHE_FILE.write_text(json.dumps(tickets, ensure_ascii=False, indent=1), encoding="utf-8")
    return tickets


def confirm(prompt: str) -> bool:
    try:
        return input(f"{prompt} [s/N]: ").strip().lower() in ("s", "sim", "y", "yes")
    except EOFError:
        return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Monitor de tickets Movidesk")
    parser.add_argument("--cache", action="store_true", help="Forca uso do cache local")
    parser.add_argument("--max-age", type=int, default=30, help="Idade maxima do cache (min)")
    parser.add_argument("--nota", action="store_true", help="Adiciona nota interna em tickets criticos (Tarefa 4)")
    parser.add_argument("--json", action="store_true", help="Emite tambem saida JSON")
    args = parser.parse_args()

    now = datetime.now()
    try:
        client = MovideskClient()
        raw = fetch_tickets(client, args.cache, args.max_age)
    except MovideskError as exc:
        log.error("%s", exc)
        return 1

    infos = [build_ticket_info(t, now) for t in raw]
    infos.sort(key=lambda t: -(t.hours_idle or 0))

    critical = [t for t in infos if t.level == "CRITICO"]
    warning = [t for t in infos if t.level == "ATENCAO"]
    ok = [t for t in infos if t.level == "OK"]
    candidates = find_merge_candidates(infos)
    sla_in_risk = sla_risks(infos)

    report = build_report(critical, warning, ok, candidates, sla_in_risk, len(infos), now)
    path = save_report(report, now, REPORTS_DIR)
    print(report)
    print(f"\nRelatorio salvo em: {path}")

    if args.json:
        payload = {
            "gerado_em": now.isoformat(timespec="seconds"),
            "tickets_ativos": len(infos),
            "criticos": [t.id for t in critical],
            "atencao": [t.id for t in warning],
            "candidatos_mescla": candidates,
            "sla_risco": [t.id for t in sla_in_risk],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=1))

    if args.nota:
        if not critical:
            log.info("Nenhum ticket critico para anotar.")
            return 0
        if not confirm(f"\nAdicionar nota interna nos {len(critical)} tickets criticos?"):
            log.info("Acao automatica cancelada pelo usuario.")
            return 0
        for t in critical:
            desc = f"Ticket parado ha {t.idle_display}. Necessario follow-up urgente."
            try:
                client.add_internal_note(t.id, desc)
                log.info("Nota adicionada ao ticket #%s", t.id)
            except MovideskError as exc:
                log.error("Falha ao anotar #%s: %s", t.id, exc)

    return 0


if __name__ == "__main__":
    sys.exit(main())
