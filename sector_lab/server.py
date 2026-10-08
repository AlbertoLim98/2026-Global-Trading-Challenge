"""Sector Lab: analisi dei 11 settori USA via ETF SPDR, con dati Alpaca (paper).

    uv run python sector_lab/server.py            # dati reali Alpaca
    uv run python sector_lab/server.py --demo     # dati sintetici, senza chiavi

Si apre su http://127.0.0.1:8770. Solo lettura: nessun ordine viene mai inviato.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import broker as broker_mod
import data
import desk as desk_mod
import export
import journal as journal_mod
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

    def fetch_bars(self, symbols: list[str]) -> dict:
        """Storico giornaliero di simboli arbitrari (dati demo o Alpaca)."""
        return data.demo_bars(symbols) if self.demo else data.fetch_bars(symbols, self.feed)

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


def make_handler(state: State, desk: desk_mod.Desk, port: int) -> type[BaseHTTPRequestHandler]:
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

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

        def _host_ok(self) -> bool:
            """Blocca le richieste con Host diverso da localhost (protezione da DNS rebinding)."""
            return self.headers.get("Host", "") in allowed_hosts

        def do_POST(self) -> None:
            # Un altro sito aperto nel browser non deve poter inviare ordini: serve un'intestazione
            # personalizzata (non inviabile cross-origin senza consenso) e un Host locale.
            if not self._host_ok() or self.headers.get("X-Sector-Lab") != "1":
                self._json({"error": "richiesta rifiutata"}, 403)
                return
            url = urlparse(self.path)
            try:
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                if url.path == "/api/rebalance/run":
                    mode = body.get("mode", "quality")
                    capital = float(body.get("capital", 1_000_000))
                    self._json(desk.run(mode, capital))
                elif url.path == "/api/rebalance/stops":
                    self._json(desk.stop_check())
                elif url.path == "/api/trade/reposition":
                    self._json(desk.reposition(str(body.get("id", ""))))
                else:
                    self._json({"error": "non trovato"}, 404)
            except desk_mod.DeskError as e:
                self._json({"error": str(e)}, 400)
            except Exception as e:  # noqa: BLE001
                desk.journal.log("ERROR", {"message": f"{type(e).__name__}: {e}", "path": url.path})
                self._json({"error": f"{type(e).__name__}: {e}"}, 500)

        def do_GET(self) -> None:
            if not self._host_ok():
                self._json({"error": "richiesta rifiutata"}, 403)
                return
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
                    acc = desk.broker.account()
                    pos = desk.broker.positions()
                    self._json({**acc, "positions": pos, "broker": desk.broker.name})
                elif url.path == "/api/rebalance/proposals":
                    desk.refresh_orders()
                    trades = desk.trades()
                    self._json(
                        {
                            "trades": trades,
                            "n_failed": sum(t["status"] == "failed" for t in trades),
                            "market": desk.broker.clock(),
                            "broker": desk.broker.name,
                        }
                    )
                elif url.path == "/api/proposals/history":
                    self._json({"proposals": desk.journal.proposals()[:300]})
                elif url.path == "/api/journal":
                    limit = int((q.get("limit") or ["300"])[0])
                    kind = (q.get("kind") or [None])[0]
                    ev = desk.journal.events(limit, kind)
                    for e in ev:
                        e["summary"] = journal_mod.summarize(e["kind"], e["payload"])
                        e["payload"] = (
                            {} if e["kind"] == "RUN" else e["payload"]
                        )  # il RUN è nel file Excel
                    self._json({"events": ev})
                elif url.path == "/api/journal.xlsx":
                    ev = desk.journal.events(100000)
                    self._xlsx(
                        export.build_journal(ev), f"journal_{time.strftime('%Y-%m-%d')}.xlsx"
                    )
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
    feed = a.feed or os.environ.get("ALPACA_DATA_FEED", "iex")
    state = State(a.demo, feed)
    if a.demo:
        syms = [*data.load_universe().ticker, data.STOCK_BENCHMARK, *data.SECTORS, data.BENCHMARK]
        prices = {k: float(v["close"].iloc[-1]) for k, v in data.demo_bars(syms).items()}
        broker = broker_mod.DemoBroker(1_000_000.0, prices)
        jpath = HERE / "journal_demo.db"
    else:
        creds = data.keys()
        broker = broker_mod.AlpacaBroker(*creds, feed=feed)
        jpath = Path(os.environ.get("SECTOR_LAB_JOURNAL", HERE / "journal.db"))
    desk = desk_mod.Desk(state, broker, journal_mod.Journal(jpath))
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(state, desk, a.port))
    url = f"http://127.0.0.1:{a.port}"
    print(f"Sector Lab su {url}  ({'DEMO' if a.demo else 'Alpaca paper, feed ' + feed})")
    if not a.no_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    with contextlib.suppress(KeyboardInterrupt):
        srv.serve_forever()


if __name__ == "__main__":
    main()
