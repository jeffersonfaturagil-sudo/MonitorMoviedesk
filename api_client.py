import logging
import threading
import time
from collections import deque
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import requests

from config import (
    API_BASE_URL,
    MAX_RETRIES,
    MOVIDESK_TOKEN,
    RATE_LIMIT_REQUESTS,
    RATE_LIMIT_WINDOW_SECONDS,
    REQUEST_TIMEOUT,
)

log = logging.getLogger("movidesk")


def mask_url(url: str) -> str:
    if not url:
        return url
    parts = urlsplit(url)
    query = [(k, "***" if k.lower() == "token" else v) for k, v in parse_qsl(parts.query)]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


class RateLimiter:
    """Limita a 10 requisicoes por janela de 60s (janela deslizante)."""

    def __init__(self, max_requests: int = RATE_LIMIT_REQUESTS, window: int = RATE_LIMIT_WINDOW_SECONDS):
        self.max_requests = max_requests
        self.window = window
        self.timestamps = deque()
        self.lock = threading.Lock()

    def wait(self) -> None:
        while True:
            with self.lock:
                now = time.monotonic()
                while self.timestamps and now - self.timestamps[0] >= self.window:
                    self.timestamps.popleft()
                if len(self.timestamps) < self.max_requests:
                    self.timestamps.append(now)
                    return
                sleep_for = self.window - (now - self.timestamps[0]) + 0.2
            log.debug("Rate limit atingido, aguardando %.1fs", sleep_for)
            time.sleep(sleep_for)


class MovideskError(Exception):
    pass


class MovideskClient:
    def __init__(self, token: str = MOVIDESK_TOKEN):
        if not token:
            raise MovideskError(
                "Token nao configurado. Defina MOVIDESK_TOKEN ou crie o arquivo .token no projeto."
            )
        self.token = token
        self.session = requests.Session()
        self.limiter = RateLimiter()

    def _get(self, endpoint: str, params: dict | None = None) -> object:
        params = dict(params or {})
        params["token"] = self.token
        url = f"{API_BASE_URL}/{endpoint.lstrip('/')}"
        backoffs = (5, 15, 30, 60)
        last_error = None

        for attempt in range(MAX_RETRIES):
            self.limiter.wait()
            try:
                resp = self.session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            except requests.RequestException as exc:
                last_error = exc
                log.warning("Erro de rede em %s: %s", mask_url(url), exc)
                time.sleep(backoffs[min(attempt, len(backoffs) - 1)])
                continue

            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError as exc:
                    raise MovideskError(f"Resposta invalida de {mask_url(url)}: {exc}") from exc

            if resp.status_code == 429:
                wait = backoffs[min(attempt, len(backoffs) - 1)]
                log.warning("HTTP 429 (rate limit), aguardandos %ss", wait)
                time.sleep(wait)
                last_error = MovideskError("429 Too Many Requests")
                continue

            if resp.status_code in (401, 403):
                raise MovideskError(f"Autenticacao recusada ({resp.status_code}) para {mask_url(url)}")

            if resp.status_code >= 500:
                wait = backoffs[min(attempt, len(backoffs) - 1)]
                log.warning("HTTP %s do servidor, retry em %ss", resp.status_code, wait)
                time.sleep(wait)
                last_error = MovideskError(f"HTTP {resp.status_code}")
                continue

            raise MovideskError(f"HTTP {resp.status_code} em {mask_url(url)}: {resp.text[:300]}")

        raise MovideskError(f"Falha apos {MAX_RETRIES} tentativas em {mask_url(url)}: {last_error}")

    TICKET_SELECT = (
        "id,protocol,subject,createdDate,lastUpdate,lastActionDate,status,baseStatus,"
        "serviceFirstLevel,serviceSecondLevel,serviceFirstLevelId,serviceThirdLevel,serviceFull,"
        "origin,tags,resolvedInFirstCall,reopenedIn,"
        "lifeTimeWorkingTime,stoppedTime,stoppedTimeWorkingTime,"
        "parentTickets,childrenTickets,"
        "slaSolutionDate,slaResponseDate,slaRealResponseDate,"
        "slaSolutionTime,slaResponseTime,slaAgreement,urgency,category,actionCount,"
        "justification,resolvedIn,closedIn"
    )

    def _list_tickets(self, flt: str, orderby: str, select: str | None = None,
                      expand: str = "clients", route: str = "tickets") -> list[dict]:
        results: list[dict] = []
        skip = 0
        top = 100
        while True:
            data = self._get(
                route,
                {
                    "$filter": flt,
                    "$select": select or self.TICKET_SELECT,
                    "$expand": expand,
                    "$orderby": orderby,
                    "$top": top,
                    "$skip": skip,
                },
            )
            if not isinstance(data, list):
                raise MovideskError(f"Resposta inesperada da API: {str(data)[:300]}")
            results.extend(data)
            if len(data) < top:
                break
            skip += top
        return results

    ACTIONS_EXPAND = (
        "actions($select=id,createdDate,createdBy,type,origin,description;$expand=createdBy)"
    )

    def list_agents(self) -> list[dict]:
        """Atendentes ativos (profileType 3)."""
        data = self._get(
            "persons",
            {
                "$filter": "profileType eq 3 and isActive eq true",
                "$select": "id,businessName,userName,accountEmail,isActive",
                "$top": 200,
            },
        )
        agents = []
        if isinstance(data, list):
            for p in data:
                email = (p.get("accountEmail") or p.get("userName") or "").strip().lower()
                name = (p.get("businessName") or email).strip()
                if email:
                    agents.append({"nome": name, "email": email})
        agents.sort(key=lambda a: a["nome"].lower())
        log.info("%d atendentes ativos", len(agents))
        return agents

    @staticmethod
    def _owner_clause(owner_email: str | None) -> str:
        if owner_email and owner_email != "*":
            return f"owner/email eq '{owner_email}'"
        return ""

    def list_active_tickets(self, owner_email: str | None = None) -> list[dict]:
        excluded = " and ".join(f"baseStatus ne '{s}'" for s in ("Resolved", "Closed", "Canceled"))
        own = self._owner_clause(owner_email)
        flt = f"{own} and {excluded}" if own else excluded
        results = self._list_tickets(
            flt,
            "lastUpdate asc",
            select=f"{self.TICKET_SELECT},owner,createdBy,actions,ownerHistories",
            expand=(
                f"clients,owner,createdBy,{self.ACTIONS_EXPAND},"
                "ownerHistories($expand=owner,changedBy)"
            ),
        )
        log.info("%d tickets ativos recebidos (owner=%s)", len(results), owner_email or "*")
        return results

    def list_tickets_changed_since(self, owner_email: str | None, since_iso: str) -> list[dict]:
        """Delta por lastUpdate: pega tickets criados/alterados desde a ultima sincronizacao,
        incluindo os que mudaram de status (para mover entre ativos/resolvidos)."""
        own = self._owner_clause(owner_email)
        flt = f"{own} and lastUpdate gt {since_iso}" if own else f"lastUpdate gt {since_iso}"
        results = self._list_tickets(
            flt,
            "lastUpdate asc",
            select=f"{self.TICKET_SELECT},owner,createdBy,actions,ownerHistories",
            expand=(
                f"clients,owner,createdBy,{self.ACTIONS_EXPAND},"
                "ownerHistories($expand=owner,changedBy)"
            ),
        )
        log.info("%d tickets alterados desde %s (owner=%s)", len(results), since_iso, owner_email or "*")
        return results

    def list_services(self) -> list[dict]:
        """Catalogo de servicos (hierarquia, categoria/urgencia padrao)."""
        data = self._get(
            "services",
            {
                "$select": "id,name,parentServiceId,defaultCategory,defaultUrgency,isActive,serviceForTicketType",
                "$top": 1000,
            },
        )
        servicos = data if isinstance(data, list) else []
        log.info("%d servicos recebidos", len(servicos))
        return servicos

    def list_resolved_tickets_since(self, owner_email: str | None, since_iso: str) -> list[dict]:
        own = self._owner_clause(owner_email)
        flt = f"{own} and resolvedIn gt {since_iso}" if own else f"resolvedIn gt {since_iso}"
        results = self._list_tickets(
            flt, "resolvedIn desc",
            select=f"{self.TICKET_SELECT},owner,createdBy",
            expand="clients,owner,createdBy",
        )
        log.info("%d tickets resolvidos recentes recebidos", len(results))
        return results

    def get_ticket_actions(self, ticket_id: int) -> list[dict]:
        """Ações de um ticket (para histórico/dúvidas e tentativas de contato)."""
        data = self._get(
            "tickets",
            {
                "$filter": f"id eq {ticket_id}",
                "$select": "id,actions",
                "$expand": f"{self.ACTIONS_EXPAND}",
                "$top": 1,
            },
        )
        if isinstance(data, list) and data:
            return data[0].get("actions") or []
        return []

    # Campos leves suficientes para o indice de duvidas.
    INDEX_SELECT = (
        "id,protocol,subject,createdDate,lastUpdate,resolvedIn,closedIn,"
        "baseStatus,status,justification,origin,"
        "serviceFirstLevel,serviceSecondLevel,serviceFirstLevelId,"
        "category,urgency,tags,actionCount"
    )

    def list_tickets_range(self, start_odata: str, end_odata: str,
                           campo: str = "resolvedIn", status: tuple[str, ...] | None = None,
                           owner_email: str | None = None) -> list[dict]:
        """Busca tickets cujo `campo` (resolvedIn/createdDate/lastUpdate) esta em [start,end).
        Consulta AMBAS as rotas (/tickets e /tickets/past) e deduplica por id, pois a
        rota /tickets so cobre lastUpdate dos ultimos 90 dias."""
        faixa = f"{campo} ge {start_odata} and {campo} lt {end_odata}"
        partes = [faixa]
        own = self._owner_clause(owner_email)
        if own:
            partes.insert(0, own)
        if status:
            partes.append("(" + " or ".join(f"baseStatus eq '{s}'" for s in status) + ")")
        flt = " and ".join(partes)
        encontrados: dict[int, dict] = {}
        for route in ("tickets", "tickets/past"):
            try:
                for t in self._list_tickets(
                    flt, f"{campo} asc",
                    select=f"{self.INDEX_SELECT},owner,createdBy",
                    expand="clients,owner,createdBy",
                    route=route,
                ):
                    try:
                        encontrados[int(t["id"])] = t
                    except (KeyError, TypeError, ValueError):
                        continue
            except MovideskError as exc:
                log.warning("Falha em /%s (%s..%s): %s", route, start_odata, end_odata, exc)
        log.info("%d tickets no periodo %s..%s campo=%s", len(encontrados), start_odata, end_odata, campo)
        return list(encontrados.values())

    def list_survey_responses(self, since_iso: str, until_iso: str | None = None,
                              max_items: int = 100000) -> list[dict]:
        items: list[dict] = []
        after: str | None = None
        for _ in range(max_items // 100 + 1):
            params: dict = {"limit": 100, "responseDateGreaterThan": since_iso}
            if until_iso:
                params["responseDateLessThan"] = until_iso
            if after:
                params["startingAfter"] = after
            data = self._get("survey/responses", params)
            if not isinstance(data, dict):
                raise MovideskError(f"Resposta inesperada de /survey/responses: {str(data)[:300]}")
            batch = data.get("items") or []
            items.extend(batch)
            if not data.get("hasMore") or not batch:
                break
            after = batch[-1].get("id")
            if not after:
                break
        log.info("%d respostas de pesquisa recebidas", len(items))
        return items


    def list_tickets_with_my_actions_since(self, owner_email: str, since_iso: str) -> list[dict]:
        flt = (
            f"actions/any(a: a/createdBy/email eq '{owner_email}' "
            f"and a/createdDate ge {since_iso})"
        )
        results = self._list_tickets(flt, "lastActionDate desc", expand="clients,owner")
        log.info("%d tickets com minhas acoes desde %s", len(results), since_iso)
        return results

    def list_participation_outside_load(self, owner_email: str) -> list[dict]:
        excluded = " and ".join(f"baseStatus ne '{s}'" for s in ("Resolved", "Closed", "Canceled"))
        flt = (
            f"actions/any(a: a/createdBy/email eq '{owner_email}') "
            f"and owner/email ne '{owner_email}' and {excluded}"
        )
        results = self._list_tickets(flt, "lastActionDate desc")
        log.info("%d tickets participados fora da carga", len(results))
        return results

    def get_ticket(self, ticket_id: int, expand: str = "clients,owner") -> dict:
        return self._get("tickets", {"id": ticket_id, "$expand": expand})

    def add_internal_note(self, ticket_id: int, description: str) -> dict:
        payload = {"actions": [{"type": 1, "description": description}]}
        params = {"id": ticket_id, "token": self.token}
        url = f"{API_BASE_URL}/tickets"
        backoffs = (5, 15, 30, 60)
        for attempt in range(MAX_RETRIES):
            self.limiter.wait()
            try:
                resp = self.session.post(url, params=params, json=payload, timeout=REQUEST_TIMEOUT)
            except requests.RequestException as exc:
                time.sleep(backoffs[min(attempt, len(backoffs) - 1)])
                last_error = exc
                continue
            if resp.status_code in (200, 201):
                try:
                    return resp.json()
                except ValueError:
                    return {"ok": True}
            if resp.status_code == 429 or resp.status_code >= 500:
                time.sleep(backoffs[min(attempt, len(backoffs) - 1)])
                last_error = MovideskError(f"HTTP {resp.status_code}")
                continue
            raise MovideskError(f"HTTP {resp.status_code} ao adicionar nota: {resp.text[:300]}")
        raise MovideskError(f"Falha ao adicionar nota no ticket {ticket_id}: {last_error}")
