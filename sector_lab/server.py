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
import export
import metrics
import stocks

CACHE_TTL = 600  # secondi


class State:
    def __init__(self, demo: bool, feed: str) -> None:
        self.demo, self.feed = demo, feed
        self._bars: dict = {}
        self._at = 0.0
        self._stocks: dict[str, tuple[float, dict]] = {}
        self._lock = threading.Lock()

    def bars(self, refresh: bool = False) -> dict:
        with self._lock:
            if refresh or not self._bars or time.time() - self._at > CACHE_TTL:
                syms = [*data.SECTORS, data.BENCHMARK]
                self._bars = data.demo_bars(syms) if self.demo else data.fetch_bars(syms, self.feed)
                self._at = time.time()
            return self._bars

    def stock_ranking(self, etf: str, refresh: bool = False, mode: str = "quality") -> dict:
        """Top 10 aziende del settore `etf`, valutate rispetto ad ACWI (cache per settore)."""
        with self._lock:
            hit = self._stocks.get(etf)
            if hit and not refresh and time.time() - hit[0] < CACHE_TTL:
                return hit[1][mode]
        uni = data.load_universe()
        uni = uni[uni.sector_etf == etf]
        syms = [*uni.ticker, data.STOCK_BENCHMARK]
        bars = data.demo_bars(syms) if self.demo else data.fetch_bars(syms, self.feed)
        if data.STOCK_BENCHMARK not in bars:
            raise RuntimeError(f"nessun dato per il benchmark {data.STOCK_BENCHMARK}")
        rows = stocks.analyze_sector(uni, bars, bars[data.STOCK_BENCHMARK])
        res = {
            "quality": stocks.top_quality(rows, len(uni)),
            "beta": stocks.top_beta(rows, len(uni)),
        }
        for r in res.values():
            r |= {
                "sector": etf,
                "sector_name": data.SECTORS[etf],
                "benchmark": data.STOCK_BENCHMARK,
            }
        with self._lock:
            self._stocks[etf] = (time.time(), res)
        return res[mode]

    def meta(self) -> dict:
        return {
            "demo": self.demo,
            "feed": "demo" if self.demo else self.feed,
            "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

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

        def _xlsx(self, body: bytes, name: str) -> None:
            self.send_response(200)
            self.send_header(
                "Content-Type", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
            )
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
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
                elif url.path == "/api/export.xlsx":
                    body = export.build(state.sectors())
                    self._xlsx(body, f"settori_{time.strftime('%Y-%m-%d')}.xlsx")
                elif url.path == "/api/stocks":
                    etf = (q.get("sector") or [""])[0].upper()
                    mode = (q.get("mode") or ["quality"])[0]
                    if mode not in ("quality", "beta"):
                        self._json({"error": f"modalità sconosciuta: {mode}"}, 400)
                    elif etf not in data.SECTORS:
                        self._json({"error": f"settore sconosciuto: {etf}"}, 404)
                    else:
                        self._json(
                            {**state.stock_ranking(etf, "refresh" in q, mode), **state.meta()}
                        )
                elif url.path == "/api/export_stocks.xlsx":
                    etf = (q.get("sector") or ["all"])[0].upper()
                    etfs = list(data.SECTORS) if etf == "ALL" else [etf]
                    mode = (q.get("mode") or ["quality"])[0]
                    if mode not in ("quality", "beta"):
                        self._json({"error": f"modalità sconosciuta: {mode}"}, 400)
                    elif any(e not in data.SECTORS for e in etfs):
                        self._json({"error": f"settore sconosciuto: {etf}"}, 404)
                    else:
                        res = {e: state.stock_ranking(e, mode=mode) for e in etfs}
                        body = export.build_stocks(res, data.SECTORS, state.meta(), mode)
                        tag = ("tutti" if etf == "ALL" else etf) + (
                            "_altobeta" if mode == "beta" else ""
                        )
                        self._xlsx(body, f"aziende_{tag}_{time.strftime('%Y-%m-%d')}.xlsx")
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
            except Exception as e:  # noqa: BLE001 - mostra l'errore nell'interfaccia invece di chiudere
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
