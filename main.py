"""Relatório de tickets do Movidesk em texto (CLI).

Não existe regra de negócio aqui: o CLI monta o MESMO payload que o painel
(``dados.build_payload``) usando o cache compartilhado e só manda formatar em
``report.build_report``. Assim relatório e tela nunca divergem.
"""
import argparse
import json
import logging
import sys
from datetime import datetime, timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
for _caminho in (BASE_DIR, BASE_DIR / "painel"):
    _texto = str(_caminho)
    if _texto not in sys.path:
        sys.path.insert(0, _texto)

from api_client import MovideskClient, MovideskError  # noqa: E402
from config import AGENT_EMAIL, REPORTS_DIR  # noqa: E402
from dados import (  # noqa: E402
    build_payload,
    bucket_do_agente,
    load_cache_raw,
    salvar_bucket_agente,
    tickets_do_cache,
)
from report import build_report, save_report  # noqa: E402
from util import odata_dt  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("movidesk")


def fetch_tickets(client: MovideskClient, use_cache: bool, max_age_minutes: int) -> list[dict]:
    """Busca os tickets ativos gravando no MESMO cache do painel (schema ``por_agente``).

    A escrita é read-modify-write: preserva pesquisa de satisfação, histórico,
    soluções, plantão e filas locais do painel (antes o CLI sobrescrevia tudo com
    uma lista pura, e o painel passava a ver o cache vazio).
    """
    if use_cache:
        ativos = tickets_do_cache(AGENT_EMAIL, max_age_minutes=max_age_minutes)
        if ativos is not None:
            log.info("Usando cache local (%d tickets, ate %d min)", len(ativos), max_age_minutes)
            return ativos
    tickets = client.list_active_tickets(AGENT_EMAIL)
    salvar_bucket_agente(
        AGENT_EMAIL,
        tickets,
        extras={
            # Mesma margem de 1 min do painel: o próximo delta por lastUpdate não
            # deixa nenhum ticket alterado durante esta consulta de fora.
            "last_sync_odata": odata_dt(datetime.now() - timedelta(minutes=1)),
            "origem": "cli",
        },
    )
    return tickets


def montar_payload(client: MovideskClient, use_cache: bool, max_age_minutes: int) -> dict:
    """Busca os tickets e devolve o MESMO payload que o painel monta (fonte única).

    Resolvidos, pesquisa de satisfação e interações do dia vêm do cache
    compartilhado: nenhum indicador é recalculado aqui.
    """
    tickets = fetch_tickets(client, use_cache, max_age_minutes)
    cache = load_cache_raw()
    bucket = bucket_do_agente(AGENT_EMAIL, raw=cache)
    survey = cache.get("survey") if isinstance(cache.get("survey"), list) else []
    return build_payload(
        tickets,
        datetime.now(),
        resolved_raw=bucket.get("resolvidos") or [],
        survey_raw=survey,
        interagiu_raw=bucket.get("interagiu_hoje") or [],
        fora_raw=bucket.get("fora_carga") or [],
        agente_email=AGENT_EMAIL,
    )


def resumo_json(payload: dict) -> dict:
    """Resumo em JSON para scripts (``--json``), derivado do mesmo payload."""
    return {
        "gerado_em": payload.get("gerado_em"),
        "tickets_ativos": (payload.get("resumo") or {}).get("ativos", 0),
        "criticos": [t.get("id") for t in payload.get("criticos") or []],
        "atencao": [t.get("id") for t in payload.get("atencao") or []],
        "candidatos_mescla": payload.get("mesclas") or [],
        "sla_risco": [t.get("id") for t in payload.get("sla") or []],
    }


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
        payload = montar_payload(client, args.cache, args.max_age)
    except MovideskError as exc:
        log.error("%s", exc)
        return 1

    texto = build_report(payload, now)
    path = save_report(texto, now, REPORTS_DIR)
    print(texto)
    print(f"\nRelatorio salvo em: {path}")

    if args.json:
        print(json.dumps(resumo_json(payload), ensure_ascii=False, indent=1))

    if args.nota:
        criticos = payload.get("criticos") or []
        if not criticos:
            log.info("Nenhum ticket critico para anotar.")
            return 0
        if not confirm(f"\nAdicionar nota interna nos {len(criticos)} tickets criticos?"):
            log.info("Acao automatica cancelada pelo usuario.")
            return 0
        for t in criticos:
            tid = t.get("id")
            desc = (f"Ticket parado ha {t.get('parado_display') or '?'}."
                    " Necessario follow-up urgente.")
            try:
                client.add_internal_note(tid, desc)
                log.info("Nota adicionada ao ticket #%s", tid)
            except MovideskError as exc:
                log.error("Falha ao anotar #%s: %s", tid, exc)

    return 0


if __name__ == "__main__":
    sys.exit(main())
