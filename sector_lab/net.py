"""Tentativi automatici per le chiamate di rete verso Alpaca.

Alpaca ogni tanto chiude la connessione senza rispondere ("Remote end closed connection without
response"), risponde 429 (troppe richieste) o 5xx. Sono errori transitori: si riprova con attesa crescente.
Gli errori veri (chiavi sbagliate, richiesta non valida) non vengono ripetuti.
"""

from __future__ import annotations

import time
from collections.abc import Callable

import requests

TRANSIENT_HTTP = {429, 500, 502, 503, 504}
TRANSIENT_EXC = (
    requests.exceptions.ConnectionError,
    requests.exceptions.Timeout,
    requests.exceptions.ChunkedEncodingError,
)


def is_transient(e: BaseException) -> bool:
    if isinstance(e, TRANSIENT_EXC):
        return True
    return getattr(e, "status_code", None) in TRANSIENT_HTTP  # APIError di alpaca-py


def with_retry[T](fn: Callable[[], T], tries: int = 4, base: float = 1.0) -> T:
    """Esegue `fn`; sugli errori transitori riprova (attese base, 2 base, 4 base...), poi rilancia l'errore."""
    for i in range(tries):
        try:
            return fn()
        except Exception as e:
            if not is_transient(e) or i == tries - 1:
                raise
            time.sleep(base * 2**i)
    raise AssertionError("irraggiungibile")
