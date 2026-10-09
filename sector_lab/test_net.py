import sys
import time
from pathlib import Path

import pytest
import requests

sys.path.insert(0, str(Path(__file__).parent))
import net
import server


def test_retries_a_dropped_connection_then_succeeds():
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise requests.exceptions.ConnectionError(
                "Remote end closed connection without response"
            )
        return "ok"

    assert net.with_retry(flaky, tries=4, base=0) == "ok" and len(calls) == 3


def test_gives_up_after_the_last_try_and_raises_the_original_error():
    calls = []

    def down():
        calls.append(1)
        raise requests.exceptions.ConnectionError("giù")

    with pytest.raises(requests.exceptions.ConnectionError, match="giù"):
        net.with_retry(down, tries=3, base=0)
    assert len(calls) == 3


def test_real_errors_are_not_retried():
    calls = []

    class Unauthorized(Exception):
        status_code = 401

    def bad():
        calls.append(1)
        raise Unauthorized("chiavi sbagliate")

    with pytest.raises(Unauthorized):
        net.with_retry(bad, tries=4, base=0)
    assert len(calls) == 1
    with pytest.raises(ValueError):
        net.with_retry(lambda: (_ for _ in ()).throw(ValueError("x")), base=0)


def test_rate_limit_and_server_errors_count_as_transient():
    class Api(Exception):
        def __init__(self, code):
            self.status_code = code

    assert all(net.is_transient(Api(c)) for c in (429, 500, 502, 503, 504))
    assert not any(net.is_transient(Api(c)) for c in (400, 401, 403, 404))
    assert net.is_transient(requests.exceptions.Timeout()) and not net.is_transient(KeyError("k"))


def test_validation_reuses_downloaded_data_instead_of_fetching_again(monkeypatch):
    state = server.State(True, "iex")
    state.short_view()  # primo calcolo (dati demo)
    _, view = state._short
    state._short = (time.time() - 1200, view)  # più vecchio di 10 minuti: la vista si rifarebbe

    def no_network(symbols):
        raise AssertionError("la validazione non deve riscaricare i dati")

    monkeypatch.setattr(state, "fetch_bars", no_network)
    assert state.short_ic()["rows"]  # usa i dati già in memoria
    with pytest.raises(AssertionError):
        state.short_view()  # la vista normale invece li aggiorna
