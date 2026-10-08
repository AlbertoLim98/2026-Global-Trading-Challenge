"""Sector Lab: analisi dei 11 settori USA via ETF SPDR, con dati Alpaca (paper).

    uv run python sector_lab/server.py            # dati reali Alpaca
    uv run python sector_lab/server.py --demo     # dati sintetici, senza chiavi

Si apre su http://127.0.0.1:8770. Solo lettura: nessun ordine viene mai inviato.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import data
import metrics

CACHE_TTL = 600  # secondi


class State:
    def __init__(self, demo: bool, feed: str) -> None:
        self.demo, self.feed = demo, feed
        self._bars: dict = {}
        self._at = 0.0
        self._lock = threading.Lock()

    def bars(self, refresh: bool = False) -> dict:
        with self._lock:
            if refresh or not self._bars or time.time() - self._at > CACHE_TTL:
                syms = [*data.SECTORS, data.BENCHMARK]
                self._bars = data.demo_bars(syms) if self.demo else data.fetch_bars(syms, self.feed)
                self._at = time.time()
            return self._bars

    def sectors(self, refresh: bool = False) -> dict:
        bars = self.bars(refresh)
        bench = bars[data.BENCHMARK]["close"] if data.BENCHMARK in bars else None
        rows = {s: metrics.compute(bars[s], bench) for s in data.SECTORS if s in bars}
        scores = metrics.score(rows)
        out = [{"symbol": s, "name": data.SECTORS[s], **rows[s], "scores": scores[s]} for s in rows]
        bench_row = metrics.compute(bars[data.BENCHMARK]) if data.BENCHMARK in bars else None
        return {
            "demo": self.demo,
            "feed": "demo" if self.demo else self.feed,
            "updated": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self._at)),
            "benchmark": bench_row and {"symbol": data.BENCHMARK, **bench_row},
            "sectors": out,
        }


def make_handler(state: State) -> type[BaseHTTPRequestHandler]:
    class H(BaseHTTPRequestHandler):
        def _json(self, obj: object, status: int = 200) -> None:
            body = json.dumps(obj, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            url = urlparse(self.path)
            q = parse_qs(url.query)
            try:
                if url.path in ("/", "/index.html"):
                    body = (HERE / "static" / "index.html").read_bytes()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif url.path == "/api/sectors":
                    self._json(state.sectors(refresh="refresh" in q))
                elif url.path == "/api/history":
                    sym = (q.get("symbol") or [""])[0].upper()
                    bars = state.bars()
                    if sym not in bars:
                        self._json({"error": f"simbolo sconosciuto: {sym}"}, 404)
                    else:
                        self._json(metrics.history(bars[sym]))
                elif url.path == "/api/account":
                    if state.demo:
                        self._json({"error": "modalità demo: nessun conto collegato"}, 503)
                    else:
                        self._json(data.account_snapshot())
                else:
                    self._json({"error": "non trovato"}, 404)
            except Exception as e:  # mostra l'errore nell'interfaccia invece di chiudere
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)

        def log_message(self, *a: object) -> None:
            pass

    return H


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--demo", action="store_true", help="dati sintetici, nessuna chiave richiesta")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--feed", default=None, help="iex (gratuito, default) o sip")
    a = ap.parse_args()

    data.load_env(HERE.parent / ".env")
    if not a.demo and data.keys() is None:
        sys.exit(
            "Chiavi Alpaca mancanti: compila ALPACA_API_KEY e ALPACA_SECRET_KEY in .env, "
            "oppure usa --demo."
        )
    import os

    feed = a.feed or os.environ.get("ALPACA_DATA_FEED", "iex")
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(State(a.demo, feed)))
    url = f"http://127.0.0.1:{a.port}"
    print(f"Sector Lab su {url}  ({'DEMO' if a.demo else 'Alpaca paper, feed ' + feed})")
    if not a.no_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    with contextlib.suppress(KeyboardInterrupt):
        srv.serve_forever()


if __name__ == "__main__":
    main()
