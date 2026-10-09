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
import net
import portfolio
import shortterm
import stocks

CACHE_TTL = 600  # secondi


class State:
    def __init__(self, demo: bool, feed: str) -> None:
        self.demo, self.feed = demo, feed
        self._bars: dict = {}
        self._at = 0.0
        self._stocks: dict[str, tuple[float, dict]] = {}
        self._short: tuple[float, dict] | None = None
        self._short_ic: tuple[float, dict] | None = None
        self.tradable_fn = None  # broker.tradable_symbols (None in demo: nessun filtro)
        self._tradable: tuple[float, set[str] | None] | None = None
        self._lock = threading.Lock()

    def fetch_bars(self, symbols: list[str]) -> dict:
        """Storico giornaliero di simboli arbitrari (dati demo o Alpaca)."""
        return data.demo_bars(symbols) if self.demo else data.fetch_bars(symbols, self.feed)

    def tradable(self, refresh: bool = False) -> set[str] | None:
        """Simboli negoziabili sul broker (cache 6 ore). None = nessun filtro (demo o errore)."""
        if self.tradable_fn is None:
            return None
        with self._lock:
            if self._tradable and not refresh and time.time() - self._tradable[0] < 6 * 3600:
                return self._tradable[1]
        try:
            syms = self.tradable_fn()
        except Exception:  # noqa: BLE001 - senza elenco non si filtra; il broker rifiuterà l'ordine
            syms = None
        with self._lock:
            self._tradable = (time.time(), syms)
        return syms

    def short_view(self, refresh: bool = False, max_age: float = CACHE_TTL) -> dict:
        """Score di breve periodo (1 giorno) di tutto l'universo negoziabile, con il contesto di mercato."""
        with self._lock:
            if self._short and not refresh and time.time() - self._short[0] < max_age:
                return self._short[1]
        uni = data.load_universe()
        tradable = self.tradable()
        if tradable is not None:
            uni = uni[uni.ticker.isin(tradable)]
        syms = list(uni.ticker)
        bars = self.fetch_bars([*syms, data.BENCHMARK, *data.SECTORS])
        bars = {
            k: shortterm.drop_incomplete(v) for k, v in bars.items()
        }  # niente barra di oggi incompleta
        if data.BENCHMARK not in bars:
            raise RuntimeError(f"nessun dato per {data.BENCHMARK}")
        sector_of = dict(zip(uni.ticker, uni.sector_etf, strict=True))
        panel = shortterm.build_panel(
            {s: bars[s] for s in syms if s in bars},
            bars[data.BENCHMARK],
            sector_of,
            {e: bars[e] for e in data.SECTORS if e in bars},
        )
        rows = shortterm.latest_table(
            panel, dict(zip(uni.ticker, uni.name, strict=True)), sector_of
        )
        view = {
            "context": shortterm.market_context(panel),
            "rows": rows,
            "n_analyzed": len(rows),
            "n_universe": len(syms),
            "feed": "demo" if self.demo else self.feed,
            "pillars": shortterm.PILLAR_LABEL,
            "notes": shortterm.COMPONENT_NOTES,
            "_panel": panel,  # per la validazione storica (non serializzato)
        }
        with self._lock:
            self._short = (time.time(), view)
        return view

    def short_ic(self, refresh: bool = False) -> dict:
        """Validazione storica degli indicatori di breve periodo (correlazione col rendimento del giorno dopo)."""
        with self._lock:
            if self._short_ic and not refresh and time.time() - self._short_ic[0] < 3600:
                return self._short_ic[1]
        rep = shortterm.ic_report(
            self.short_view(max_age=3600)["_panel"]
        )  # niente nuovo scaricamento
        with self._lock:
            self._short_ic = (time.time(), rep)
        return rep

    def portfolio_weights(self, mode: str, capital: float = 1_000_000.0) -> dict:
        """Pesi reali dell'algoritmo di ribilanciamento: il titolo è valutato insieme agli altri settori."""
        by_etf = {etf: self.stock_ranking(etf, mode=mode) for etf in data.SECTORS}
        p = portfolio.Params(capital=capital, mode=mode)
        tg = portfolio.build_targets(self.sectors()["sectors"], by_etf, p)
        return {
            "targets": {k: t["value"] / capital for k, t in tg["targets"].items()},
            "sectors": {s["symbol"]: s["budget"] / capital for s in tg["sectors"]},
            "notes": tg["notes"],
        }

    def stock_view(self, etf: str, mode: str = "quality", refresh: bool = False) -> dict:
        """Top 10 del settore con il peso nel portafoglio calcolato sull'insieme dei settori."""
        res = self.stock_ranking(etf, refresh, mode)
        w = self.portfolio_weights(mode)
        sec = next(x for x in self.sectors()["sectors"] if x["symbol"] == etf)
        sector_score = (sec.get("scores") or {}).get("total")
        top = [
            {
                **r,
                "weight_portfolio": w["targets"].get(r["symbol"], 0.0),
                "sector_score": sector_score,
            }
            for r in res["top"]
        ]
        return {
            **res,
            "top": top,
            "sector_portfolio_weight": w["sectors"].get(etf, 0.0),
            "notes": w["notes"],
        }

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
        tradable = self.tradable()
        if (
            tradable is not None
        ):  # fuori i titoli non attivi o non negoziabili (fusi, ritirati, sospesi)
            uni = uni[uni.ticker.isin(tradable)]
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


def _explain(e: Exception) -> str:
    """Messaggio per l'interfaccia: gli errori di rete transitori vengono spiegati, il resto resta com'è."""
    if net.is_transient(e):
        return (
            "Alpaca ha interrotto la connessione o è momentaneamente non raggiungibile "
            f"(dopo vari tentativi automatici). Riprova tra qualche secondo. [{type(e).__name__}]"
        )
    return f"{type(e).__name__}: {e}"


def choose_journal_path(
    here: Path,
    portfolio: str | None,
    explicit: str | None,
    env_path: str | None,
    account: str | None,
) -> tuple[Path, str | None]:
    """Journal da usare e, se serve, una nota per l'utente.

    Un journal appartiene a un conto Alpaca. Con `--journal` o SECTOR_LAB_JOURNAL si usa quel file. Con un nome di
    portafoglio si usa `journal_<nome>.db`. Senza nome si usa `journal.db`, ma solo se è dello stesso conto (o senza
    conto noto): altrimenti `journal_<numero del conto>.db`, così cambiare chiavi non mescola due portafogli.
    """
    if explicit or env_path:
        return Path(explicit or env_path), None
    slug = "".join(c if c.isalnum() else "_" for c in (portfolio or "").lower()).strip("_")
    if slug:
        return here / f"journal_{slug}.db", None
    generic = here / "journal.db"
    known = journal_mod.peek_account(generic)
    if known and account and known != account:
        alt = here / f"journal_{account}.db"
        return (
            alt,
            f"{generic.name} appartiene al conto {known}: per il conto {account} uso {alt.name}",
        )
    return generic, None


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
                elif url.path == "/api/orders/cancel":
                    self._json(desk.cancel_open())
                elif url.path == "/api/trade/reposition":
                    self._json(desk.reposition(str(body.get("id", ""))))
                else:
                    self._json({"error": "non trovato"}, 404)
            except desk_mod.DeskError as e:
                self._json({"error": str(e)}, 400)
            except Exception as e:  # noqa: BLE001
                desk.journal.log("ERROR", {"message": f"{type(e).__name__}: {e}", "path": url.path})
                self._json({"error": _explain(e)}, 502 if net.is_transient(e) else 500)

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
                        self._json({**state.stock_view(etf, mode, "refresh" in q), **state.meta()})
                elif url.path == "/api/export_stocks.xlsx":
                    etf = (q.get("sector") or ["all"])[0].upper()
                    etfs = list(data.SECTORS) if etf == "ALL" else [etf]
                    mode = (q.get("mode") or ["quality"])[0]
                    if mode not in ("quality", "beta"):
                        self._json({"error": f"modalità sconosciuta: {mode}"}, 400)
                    elif any(e not in data.SECTORS for e in etfs):
                        self._json({"error": f"settore sconosciuto: {etf}"}, 404)
                    else:
                        res = {e: state.stock_view(e, mode) for e in etfs}
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
                elif url.path == "/api/short":
                    v = state.short_view("refresh" in q)
                    self._json(
                        {k: x for k, x in v.items() if k not in ("_panel", "rows")}
                        | {"rows": v["rows"][:60]}
                        | state.meta()
                    )
                elif url.path == "/api/short/ic":
                    self._json(state.short_ic("refresh" in q))
                elif url.path == "/api/short.xlsx":
                    v = state.short_view()
                    ic = state.short_ic() if "ic" in q else None
                    self._xlsx(
                        export.build_short(v, ic), f"breve_termine_{time.strftime('%Y-%m-%d')}.xlsx"
                    )
                elif url.path == "/api/info":
                    self._json(desk.info())
                elif url.path == "/api/portfolio":
                    desk.refresh_orders()
                    self._json(desk.portfolio())
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
                        export.build_journal(ev, desk.journal.indicators()),
                        f"journal_{time.strftime('%Y-%m-%d')}.xlsx",
                    )
                else:
                    self._json({"error": "non trovato"}, 404)
            except Exception as e:  # noqa: BLE001 - mostra l'errore nell'interfaccia invece di chiudere
                self._json({"error": _explain(e)}, 502 if net.is_transient(e) else 500)

        def log_message(self, *a: object) -> None:
            pass

    return H


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--demo", action="store_true", help="dati sintetici, nessuna chiave richiesta")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--feed", default=None, help="iex (gratuito, default) o sip")
    ap.add_argument("--portfolio", default=None, help="nome del portafoglio (compare nel journal)")
    ap.add_argument(
        "--strategy",
        choices=sorted(journal_mod.STRATEGY_LABEL),
        default=None,
        help="strategia fissa del portafoglio: beta (alto beta) o quality (qualità)",
    )
    ap.add_argument("--env", default=None, help="file con le chiavi Alpaca (default: .env)")
    ap.add_argument(
        "--sample-minutes",
        type=float,
        default=5,
        help="ogni quanti minuti registrare nel journal il patrimonio del conto (0 = mai). Serve a confrontare\n"
        "i portafogli con una curva affidabile, indipendente dallo storico di Alpaca",
    )
    ap.add_argument(
        "--journal", default=None, help="percorso del journal (default dedotto dal nome)"
    )
    a = ap.parse_args()

    data.load_env(Path(a.env) if a.env else HERE.parent / ".env")
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
        jpath = Path(a.journal) if a.journal else HERE / "journal_demo.db"
    else:
        creds = data.keys()
        broker = broker_mod.AlpacaBroker(*creds, feed=feed)
        account = None
        with contextlib.suppress(
            Exception
        ):  # senza rete non si può riconoscere il conto: nessun controllo
            account = broker.account().get("account_number")
        jpath, note = choose_journal_path(
            HERE, a.portfolio, a.journal, os.environ.get("SECTOR_LAB_JOURNAL"), account
        )
        if note:
            print("Nota:", note)
    state.tradable_fn = broker.tradable_symbols
    journal = journal_mod.Journal(jpath, a.portfolio, a.strategy)
    if not a.demo:
        try:
            journal.bind_account(account)
        except journal_mod.JournalAccountError as e:
            sys.exit(f"{e}\nJournal: {jpath}")
    desk = desk_mod.Desk(state, broker, journal, portfolio=a.portfolio, strategy=a.strategy)
    if a.sample_minutes > 0:
        stop = threading.Event()

        def sampler() -> None:
            desk.snapshot_equity()
            while not stop.wait(a.sample_minutes * 60):
                desk.snapshot_equity()

        threading.Thread(target=sampler, daemon=True).start()
    srv = ThreadingHTTPServer(("127.0.0.1", a.port), make_handler(state, desk, a.port))
    url = f"http://127.0.0.1:{a.port}"
    label = f" · {a.portfolio}" if a.portfolio else ""
    label += f" · strategia {journal_mod.STRATEGY_LABEL[a.strategy]}" if a.strategy else ""
    print(f"Sector Lab su {url}  ({'DEMO' if a.demo else 'Alpaca paper, feed ' + feed}){label}")
    print(f"Journal: {jpath}")
    if not a.no_browser:
        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    with contextlib.suppress(KeyboardInterrupt):
        srv.serve_forever()


if __name__ == "__main__":
    main()
