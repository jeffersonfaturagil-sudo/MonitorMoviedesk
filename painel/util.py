"""Funções utilitárias comuns — fonte ÚNICA de normalização de texto.

Todo mundo que normaliza/tokeniza texto passa por aqui:

- ``norm_texto``: normalização leve (sem acento, minúsculas, só [a-z0-9 ]);
- ``norm_texto_ascii``: normalização forte (NFKD, ASCII, espaços colapsados);
- ``tokens_simples``: tokenização p/ busca, com ``STOPWORDS_COMUNS``.

``dados`` e ``processos`` importam estas funções como alias (``_norm``/``_tokens``)
``macros`` como ``normalizar`` — não redefina a sua própria cópia num módulo novo.
Exceção de propósito: ``analyzer.normalize_subject`` (mesclas) mantém caixa alta
e acento de forma distinta, porque compara assunto par a par — ver analyzer.py.
"""

import re
import unicodedata
from datetime import datetime

# Única lista de stopwords da busca. `tokens_simples` só enxerga tokens já
# normalizados por `norm_texto` (sempre ASCII minúsculo), então as entradas
# acentuadas nunca casavam e saíram: "não" "às" "até" "então" "já" "está"
# "você" "são". Os tokens ASCII seguem idênticos: "nao", "as" e "esta" já
# estavam na lista; "ate", "entao", "ja", "sao" e "voce" nunca foram filtrados
# e continuam não sendo.
STOPWORDS_COMUNS = frozenset((
    "a", "o", "e", "de", "do", "da", "em", "para", "com", "por", "que", "na", "no", "os", "as",
    "um", "uma", "ao", "dos", "das", "entre", "sobre", "sem", "como", "mais", "mas", "se",
    "ter", "tem", "pode", "isso", "essa", "esse", "eu", "ele", "ela",
    "nos", "vai", "ser", "seu", "sua", "meu", "minha", "qual", "quais", "quando", "este", "esta",
    "isto", "depois", "antes", "porque", "seja", "estiver", "sendo", "foi", "estar",
    "tiver", "nao", "sim", "the", "favor", "bom", "dia", "ola", "cliente", "ticket", "atendimento",
    "suporte", "preciso", "quero", "poderia", "ajuda",
))


def norm_texto(s: str | None) -> str:
    """Normalização leve: NFD, remove diacríticos, minúsculas, só letras/números/espaços."""
    if not s:
        return ""
    s = unicodedata.normalize("NFD", str(s))
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return re.sub(r"[^a-z0-9 ]", " ", s.lower())


def norm_texto_ascii(s: str | None) -> str:
    """Normalização mais forte: NFKD, ignora não-ASCII, minúsculas, espaços colapsados."""
    if not s:
        return ""
    txt = unicodedata.normalize("NFKD", str(s))
    txt = txt.encode("ascii", "ignore").decode("ascii").lower()
    return re.sub(r"\s+", " ", txt).strip()


def odata_dt(dt: datetime) -> str:
    """Formata datetime no padrão OData do Movidesk com o offset local.

    Ex.: ``2026-10-13T00:00:00-03:00``. Único lugar que formata data para a API
    (usado pelo painel e pelo CLI), para os dois nunca divergirem no filtro.
    """
    tz = dt.astimezone().strftime("%z")
    tz = f"{tz[:3]}:{tz[3:]}" if tz else "-03:00"
    return f"{dt.strftime('%Y-%m-%dT%H:%M:%S')}{tz}"


def tokens_simples(q: str) -> list[str]:
    raw = norm_texto(q)
    saida = []
    for t in raw.split():
        if len(t) >= 3 and t not in STOPWORDS_COMUNS:
            saida.append(t)
    return saida
