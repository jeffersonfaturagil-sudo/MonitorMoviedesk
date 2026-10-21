import logging
import threading
import time
from collections import deque
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import requests

from config import (
    ACTIVE_STATUSES_EXCLUDED,
    API_BASE_URL,
    MAX_RETRIES,
    MOVIDESK_TOKEN,
    PAGE_SIZE,
    RATE_LIMIT_REQUESTS,
    RATE_LIMIT_WINDOW_SECONDS,
    REQUEST_TIMEOUT,
    RETRY_AFTER_MAX_SECONDS,
)

log = logging.getLogger("movidesk")

# Backoff fixo para falhas transitorias (rede, 5xx, JSON invalido).
BACKOFF_SECONDS = (5, 15, 30, 60)
# Bloqueio documentado do Movidesk na API de tickets: comeca em 60s e sobe para
# 120s e 300s. Usado como piso quando a resposta 429 nao traz ``Retry-After``.
RATE_LIMIT_BACKOFF_SECONDS = (60, 120, 300)
# Trava de seguranca da paginacao: evita loop infinito caso a API ignore ``$skip``
# e devolva sempre a mesma pagina cheia. Bem acima de qualquer volume real.
MAX_TICKETS_PAGINADOS = 2_000_000


def mask_url(url: str) -> str:
    if not url:
        return url
    parts = urlsplit(url)
    query = [(k, "***" if k.lower() == "token" else v) for k, v in parse_qsl(parts.query)]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def retry_after_seconds(resp) -> float | None:
    """Le o header ``Retry-After`` (segundos ou data HTTP). None quando ausente/invalido."""
    if resp is None:
        return None
    bruto = resp.headers.get("Retry-After") if getattr(resp, "headers", None) else None
    if not bruto:
        return None
    bruto = str(bruto).strip()
    try:
        return max(0.0, float(bruto))
    except ValueError:
        pass
    try:
        from datetime import datetime, timezone
        from email.utils import parsedate_to_datetime

        data = parsedate_to_datetime(bruto)
        if data is None:
            return None
        if data.tzinfo is None:
            data = data.replace(tzinfo=timezone.utc)
        return max(0.0, (data - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


class RateLimiter:
    """Limita a 10 requisicoes por janela de 60s (janela deslizante).

    Regras do Movidesk: no maximo 10 req/min por token -> janela deslizante de 60s
    com botoeira estrita. O :data:`now_min``/``_get`` nao controla a porta, so a taxa;
    se o limite for ultrapassado por uso concorrente ou cliente nao rateado, a API
    devolve HTTP 429 e o cliente repete com backoff.
    """

    _MIN_WINDOW_SECONDS = 60

    def __init__(self, max_requests: int = RATE_LIMIT_REQUESTS, window: int = RATE_LIMIT_WINDOW_SECONDS):
        if window < self._MIN_WINDOW_SECONDS:
            raise ValueError(
                "janela de rate limit muito curta para o limite do Movidesk "
                f"(minimo {_MIN_WINDOW_SECONDS}s, recebido {window}s)"
            )
        if max_requests <= 0:
            raise ValueError(f"limite de requests invalido: {max_requests!r}")
        self.max_requests = max_requests
        self.window = window
        self.timestamps: deque[float] = deque()
        self.lock = threading.Lock()

    def wait(self) -> None:
        """Bloqueia ate que exista uma vaga na janela deslizante, com log de depressao."""
        slept = 0.0
        while True:
            with self.lock:
                now = time.monotonic()
                while self.timestamps and now - self.timestamps[0] >= self.window:
                    self.timestamps.popleft()
                if len(self.timestamps) < self.max_requests:
                    self.timestamps.append(now)
                    return
                sleep_for = self.window - (now - self.timestamps[0]) + 0.2
            if slept > 0:
                log.info("Rate limit por pump: nova janela apos %.1fs (limite %d/min)", slept, self.max_requests)
            else:
                log.debug("Rate limit atingido, aguardando %.1fs", sleep_for)
            time.sleep(sleep_for)
            slept += sleep_for

    def status(self) -> dict:
        """Retorna os valores configurados para monitoramento/debug."""
        with self.lock:
            window = self.window
            now = time.monotonic()
            while self.timestamps and now - self.timestamps[0] >= window:
                self.timestamps.popleft()
            pending = len(self.timestamps)
        return {
            "max_requests": self.max_requests,
            "window_seconds": self.window,
            "pendencias": pending,
            "disponivel_agora": pending < self.max_requests,
            "minutos_ate_limpeza": round(max(0.0, (self.timestamps[0] + self.window - now) / 60.0), 2) if self.timestamps else 0.0,
        }


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
        self._base_url = API_BASE_URL

    @staticmethod
    def _espera_rate_limit(resp, attempt: int) -> float:
        """Quanto esperar depois de um 429.

        O Movidesk manda ``Retry-After`` quando bloqueia a API de tickets; quando o
        header nao vem, usa o piso documentado (60s, depois 120s e 300s). O teto
        evita que um valor absurdo trave o painel.
        """
        piso = RATE_LIMIT_BACKOFF_SECONDS[min(attempt, len(RATE_LIMIT_BACKOFF_SECONDS) - 1)]
        indicado = retry_after_seconds(resp)
        if indicado is None:
            return float(piso)
        return float(min(max(piso, indicado), RETRY_AFTER_MAX_SECONDS))

    def _get(self, endpoint: str, params: dict | None = None) -> object:
        params = dict(params or {})
        params["token"] = self.token
        url = f"{self._base_url}/{endpoint.lstrip('/')}"
        last_error = None
        espera: float | None = None

        for attempt in range(MAX_RETRIES):
            if attempt:
                sleep_for = espera if espera is not None else BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)]
                log.info("Retry %d em %.1fs (motivo: %s)", attempt + 1, sleep_for, last_error)
                time.sleep(sleep_for)
                espera = None
            self.limiter.wait()
            try:
                resp = self.session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            except requests.RequestException as exc:
                last_error = exc
                log.warning("Erro de rede em %s: %s", mask_url(url), exc)
                continue

            if resp.status_code == 200:
                try:
                    return resp.json()
                except ValueError as exc:
                    last_error = MovideskError(f"Resposta invalida de {mask_url(url)}: {exc}")
                    continue

            if resp.status_code == 429:
                last_error = MovideskError("429 Too Many Requests")
                espera = self._espera_rate_limit(resp, attempt)
                log.warning(
                    "HTTP 429 (rate limit do Movidesk) -> retry %d/%d em %.1fs (Retry-After=%s)",
                    attempt + 1, MAX_RETRIES, espera, resp.headers.get("Retry-After") or "ausente",
                )
                continue

            if resp.status_code in (401, 403):
                raise MovideskError(f"Autenticacao recusada ({resp.status_code}) para {mask_url(url)}")

            if resp.status_code >= 500:
                last_error = MovideskError(f"HTTP {resp.status_code}")
                log.warning("HTTP %s do servidor (tentativa %d/%d)", resp.status_code, attempt + 1, MAX_RETRIES)
                continue

            raise MovideskError(f"HTTP {resp.status_code} em {mask_url(url)}: {resp.text[:300]}")

        raise MovideskError(f"Falha apos {MAX_RETRIES} tentativas em {mask_url(url)}: {last_error}")

    def rate_limit_status(self) -> dict:
        """Retorna o estado atual do rate limiter (para log/debug)."""
        return self.limiter.status()

    def _internal_note_payload(self, description: str) -> dict:
        return {"actions": [{"type": 1, "description": description}]}

    def _post(self, endpoint: str, payload: dict, timeout: int | None = None) -> object:
        url = f"{self._base_url}/{endpoint.lstrip('/')}"
        last_error = None
        espera: float | None = None
        for attempt in range(MAX_RETRIES):
            if attempt:
                sleep_for = espera if espera is not None else BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)]
                log.info("Retry POST %d em %.1fs (motivo: %s)", attempt + 1, sleep_for, last_error)
                time.sleep(sleep_for)
                espera = None
            self.limiter.wait()
            try:
                resp = self.session.post(url, json=payload, timeout=timeout or REQUEST_TIMEOUT)
            except requests.RequestException as exc:
                last_error = exc
                log.warning("Erro de rede em POST %s: %s", mask_url(url), exc)
                continue
            if resp.status_code in (200, 201):
                try:
                    return resp.json()
                except ValueError as exc:
                    last_error = MovideskError(f"Resposta invalida de POST {mask_url(url)}: {exc}")
                    continue
            if resp.status_code == 429:
                last_error = MovideskError("429 Too Many Requests")
                espera = self._espera_rate_limit(resp, attempt)
                log.warning("HTTP 429 em POST %s -> retry %d/%d em %.1fs", mask_url(url), attempt + 1, MAX_RETRIES, espera)
                continue
            if resp.status_code in (401, 403):
                raise MovideskError(f"Autenticacao recusada ({resp.status_code}) para POST {mask_url(url)}")
            if resp.status_code >= 500:
                last_error = MovideskError(f"HTTP {resp.status_code}")
                log.warning("HTTP %s no servidor POST %s (tentativa %d/%d)", resp.status_code, mask_url(url), attempt + 1, MAX_RETRIES)
                continue
            raise MovideskError(f"HTTP {resp.status_code} em POST {mask_url(url)}: {resp.text[:300]}")
        raise MovideskError(f"Falha apos {MAX_RETRIES} tentativas em POST {mask_url(url)}: {last_error}")

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
        top = PAGE_SIZE
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
            if not data:
                break
            results.extend(data)
            # Avanca pelo que a API devolveu de fato, nao pelo $top pedido: se ela
            # capar a pagina abaixo do pedido (a doc fala em 100), nao pulamos
            # registros. E se vier menos que o pedido, aprendemos o teto efetivo
            # para nao confundir "pagina curta" com "fim" e parar cedo demais.
            skip += len(data)
            if len(data) < top:
                top = len(data)
            if skip > MAX_TICKETS_PAGINADOS:
                log.warning("Paginacao de %s interrompida em %d registros", route, skip)
                break
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
        excluded = " and ".join(f"baseStatus ne '{s}'" for s in ACTIVE_STATUSES_EXCLUDED)
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
        **incluindo os que fecharam** (baseStatus Resolved/Closed/Canceled).

        .. warning::
           Nao filtramos ``baseStatus`` aqui de proposito: quem mudou de ativo para
           resolvido chega com ``baseStatus ne 'Resolved'`` e seria descartado antes de
           ``_merge_delta`` vê-lo — o ticket ficaria eternamente preso em ``ativos`` ja
           resolvido (bug dos tickets de encerramento aparecendo como "vencido" mesmo
           depois de auto-resolvidos). O desempate entre ativo/resolvido e da fusao
           em ``_merge_delta``.

        .. note::
           O cache mantém ``ativos`` e ``resolvidos`` separados. Se o servidor devolver
           tickets que ja estavam resolvidos antes do ``since_iso``, a fusao em
           ``_merge_delta`` mescla por id e o resolvido prevalece sobre o ativo.
        """
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
        excluded = " and ".join(f"baseStatus ne '{s}'" for s in ACTIVE_STATUSES_EXCLUDED)
        flt = (
            f"actions/any(a: a/createdBy/email eq '{owner_email}') "
            f"and owner/email ne '{owner_email}' and {excluded}"
        )
        results = self._list_tickets(flt, "lastActionDate desc")
        log.info("%d tickets participados fora da carga", len(results))
        return results

    def get_ticket(self, ticket_id: int, expand: str = "clients,owner") -> dict:
        return self._get("tickets", {"id": ticket_id, "$expand": expand})

    # ------------------------------------------------------------------
    # GET /survey/questions — o texto real de cada pergunta
    #
    # /survey/responses so devolve ``questionId``; o enunciado ("Qual sua
    # avaliação para esse atendimento?") vem daqui, em ``languages[].description``.
    # Sem isso o painel tinha que inventar o rotulo por type/value.
    # ------------------------------------------------------------------
    SURVEY_QUESTIONS_ROUTE = "survey/questions"

    @staticmethod
    def _texto_pergunta(pergunta: dict) -> str:
        """Escolhe o texto pt-BR da pergunta; fallback: primeiro idioma com texto."""
        idiomas = pergunta.get("languages") if isinstance(pergunta, dict) else None
        if not isinstance(idiomas, list):
            return ""
        por_cultura = {
            str(item.get("cultureId") or "").strip(): (item.get("description") or "")
            for item in idiomas
            if isinstance(item, dict)
        }
        for cultura in ("pt-BR", "pt-br", "pt"):
            texto = por_cultura.get(cultura)
            if texto and texto.strip():
                return texto.strip()
        for item in idiomas:
            if isinstance(item, dict) and (item.get("description") or "").strip():
                return item["description"].strip()
        return ""

    def list_survey_questions(self) -> dict[str, dict]:
        """Catalogo de perguntas da pesquisa: ``{questionId: {pergunta, tipo, ativo}}``."""
        data = self._get(self.SURVEY_QUESTIONS_ROUTE)
        itens = data if isinstance(data, list) else []
        catalogo: dict[str, dict] = {}
        for p in itens:
            if not isinstance(p, dict):
                continue
            qid = str(p.get("id") or "").strip()
            if not qid:
                continue
            catalogo[qid] = {
                "pergunta": self._texto_pergunta(p),
                "tipo": p.get("type"),
                "ativo": bool(p.get("isActive")),
            }
        log.info("%d perguntas de pesquisa recebidas", len(catalogo))
        return catalogo

    def get_survey_question(self, question_id: str) -> dict:
        """Texto de uma pergunta especifica (GET /survey/questions/{id})."""
        qid = str(question_id or "").strip()
        if not qid:
            return {}
        data = self._get(f"{self.SURVEY_QUESTIONS_ROUTE}/{qid}")
        if not isinstance(data, dict):
            return {}
        return {
            "pergunta": self._texto_pergunta(data),
            "tipo": data.get("type"),
            "ativo": bool(data.get("isActive")),
        }

    # ------------------------------------------------------------------
    # GET /tickets/merged — mesclas confirmadas no Movidesk
    #
    # A API devolve tudo como string (count, pageNumber "1 of 92", mergedTicketsIds
    # separado por ';'), entao normalizamos aqui em vez de cada consumidor fazer isso.
    # ------------------------------------------------------------------
    MERGED_ROUTE = "tickets/merged"

    @staticmethod
    def _paginas_de(page_number) -> int | None:
        """Le ``'1 of 92'`` e devolve 92."""
        texto = str(page_number or "").lower()
        if " of " not in texto:
            return None
        try:
            return int(texto.rsplit(" of ", 1)[1].strip())
        except (ValueError, IndexError):
            return None

    @staticmethod
    def _ids_mesclados(bruto) -> list[int]:
        ids: list[int] = []
        for parte in str(bruto or "").split(";"):
            parte = parte.strip()
            if not parte:
                continue
            try:
                ids.append(int(parte))
            except ValueError:
                continue
        return ids

    @classmethod
    def _normaliza_mescla(cls, item) -> dict | None:
        if not isinstance(item, dict):
            return None
        try:
            ticket_id = int(str(item.get("ticketId") or "").strip())
        except ValueError:
            return None
        return {
            "ticket_id": ticket_id,
            "merged_ids": cls._ids_mesclados(item.get("mergedTicketsIds")),
            "last_update": str(item.get("lastUpdate") or ""),
        }

    def list_merged_tickets(self, start_date: str | None = None, end_date: str | None = None,
                            max_pages: int = 30) -> dict:
        """Mesclagens confirmadas (``AAAA-MM-DD`` em start/end; sem filtro = tudo).

        A API pagina em lotes de 50; ``max_pages`` limita quantas paginas buscamos.
        Devolve ``{total, paginas, itens: [{ticket_id, merged_ids, last_update}]}``,
        onde ``ticket_id`` e o pai que recebeu as mesclas.
        """
        itens: list[dict] = []
        total: int | None = None
        total_paginas = 1
        pagina = 1
        while pagina <= max(1, max_pages):
            params: dict = {"page": pagina}
            if start_date:
                params["startDate"] = start_date
            if end_date:
                params["endDate"] = end_date
            data = self._get(self.MERGED_ROUTE, params)
            if not isinstance(data, dict):
                raise MovideskError(f"Resposta inesperada de /{self.MERGED_ROUTE}: {str(data)[:300]}")
            if total is None:
                try:
                    total = int(str(data.get("count") or "").strip())
                except ValueError:
                    total = None
            if pagina == 1:
                total_paginas = self._paginas_de(data.get("pageNumber")) or 1
            for bruto in data.get("mergedTickets") or []:
                item = self._normaliza_mescla(bruto)
                if item:
                    itens.append(item)
            if pagina >= total_paginas:
                break
            pagina += 1
        log.info("%d tickets mesclados recebidos (%s..%s)", len(itens), start_date or "*", end_date or "*")
        return {"total": total, "paginas": total_paginas, "itens": itens}

    def get_merged_ticket(self, ticket_id) -> dict | None:
        """Mesclas de um ticket (aceita id numerico ou protocolo)."""
        data = self._get(self.MERGED_ROUTE, {"id": ticket_id})
        return self._normaliza_mescla(data)

    # ------------------------------------------------------------------
    # GET /tickets/htmldescription — HTML da abertura e das acoes
    # ------------------------------------------------------------------
    def get_ticket_html_description(self, ticket_id, action_id=None) -> list[dict]:
        """HTML das acoes do ticket; sem ``action_id`` devolve todas."""
        params: dict = {"id": ticket_id}
        if action_id:
            params["actionId"] = action_id
        data = self._get("tickets/htmldescription", params)
        if isinstance(data, dict):
            itens = [data]
        elif isinstance(data, list):
            itens = data
        else:
            itens = []
        return [
            {"id": i.get("id"), "description": i.get("description") or ""}
            for i in itens
            if isinstance(i, dict)
        ]

    # ------------------------------------------------------------------
    # Base de conhecimento: GET /kb/article (busca) e GET /article/{id}
    # ------------------------------------------------------------------
    def search_articles(self, q: str, page: int = 1, page_size: int = 30) -> dict:
        """Busca artigos por palavra-chave. Devolve ``{total, itens, page, page_size}``.

        A primeira pagina nao manda ``page``: verificado contra a API, enviar
        ``page=1`` devolve ``totalSize`` correto mas a lista de itens vazia.
        """
        params: dict = {"q": q, "pageSize": page_size}
        if page and page > 1:
            params["page"] = page
        data = self._get("kb/article", params)
        if not isinstance(data, dict):
            return {"total": None, "itens": [], "page": page, "page_size": page_size}
        itens = data.get("items") if isinstance(data.get("items"), list) else []
        return {
            "total": data.get("totalSize"),
            "itens": itens,
            "page": data.get("page") or page,
            "page_size": data.get("pageSize") or page_size,
        }

    def get_article(self, article_id) -> dict:
        """Artigo completo (titulo, contentText, contentHtml, categorias, anexos)."""
        data = self._get(f"article/{article_id}")
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------------------
    # Anexos: vêm no actions[].attachments e baixam em /storage/download
    # ------------------------------------------------------------------
    ATTACHMENTS_EXPAND = "actions($select=id,attachments;$expand=attachments)"

    def get_ticket_attachments(self, ticket_id: int) -> list[dict]:
        """Anexos do ticket, deduplicados por hash (o mesmo arquivo reenviado em
        varias acoes aparece repetido na API). Cada item guarda as acoes que o
        carregam, na ordem em que apareceram.
        """
        data = self._get(
            "tickets",
            {
                "$filter": f"id eq {ticket_id}",
                "$select": "id,actions",
                "$expand": self.ATTACHMENTS_EXPAND,
                "$top": 1,
            },
        )
        if not isinstance(data, list) or not data:
            return []
        anexos: dict[str, dict] = {}
        for acao in data[0].get("actions") or []:
            if not isinstance(acao, dict):
                continue
            for anexo in acao.get("attachments") or []:
                if not isinstance(anexo, dict):
                    continue
                nome = anexo.get("fileName") or anexo.get("name") or ""
                chave = anexo.get("path") or anexo.get("hash") or ""
                item = anexos.get(chave)
                if item is None:
                    anexos[chave] = {
                        "nome": nome,
                        "hash": chave,
                        "acao_ids": [acao.get("id")],
                        "raw": anexo,
                    }
                elif acao.get("id") not in item["acao_ids"]:
                    item["acao_ids"].append(acao.get("id"))
        return list(anexos.values())

    def download_attachment(self, file_hash: str) -> bytes:
        """Baixa o conteudo de um anexo pelo hash (``attachment.path``)."""
        if not file_hash:
            raise MovideskError("hash do anexo vazio")
        url = f"{self._base_url}/storage/download"
        params = {"token": self.token, "id": file_hash}
        last_error = None
        espera: float | None = None
        for attempt in range(MAX_RETRIES):
            if attempt:
                sleep_for = espera if espera is not None else BACKOFF_SECONDS[min(attempt - 1, len(BACKOFF_SECONDS) - 1)]
                log.info("Retry download %d em %.1fs (motivo: %s)", attempt + 1, sleep_for, last_error)
                time.sleep(sleep_for)
                espera = None
            self.limiter.wait()
            try:
                resp = self.session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            except requests.RequestException as exc:
                last_error = exc
                log.warning("Erro de rede no download do anexo: %s", exc)
                continue
            if resp.status_code == 200:
                return resp.content
            if resp.status_code == 429:
                last_error = MovideskError("429 Too Many Requests")
                espera = self._espera_rate_limit(resp, attempt)
                continue
            if resp.status_code in (401, 403):
                raise MovideskError(f"Anexo recusado ({resp.status_code})")
            raise MovideskError(f"HTTP {resp.status_code} ao baixar anexo")
        raise MovideskError(f"Falha apos {MAX_RETRIES} tentativas ao baixar anexo: {last_error}")

    def add_internal_note(self, ticket_id: int, description: str) -> dict:
        return self._post("tickets", self._internal_note_payload(description))


