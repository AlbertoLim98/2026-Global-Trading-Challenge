"""Accesso ai dati: barre giornaliere e conto paper da Alpaca, più dati demo offline.

Sola lettura. Il client di trading è creato con paper=True e non esiste alcun
codice per inviare ordini.
"""

from __future__ import annotations

import os
import zlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from net import with_retry

SECTORS: dict[str, str] = {
    "XLK": "Tecnologia",
    "XLF": "Finanziari",
    "XLI": "Industriali",
    "XLY": "Consumi discrezionali",
    "XLV": "Salute",
    "XLC": "Comunicazione",
    "XLP": "Beni di largo consumo",
    "XLE": "Energia",
    "XLB": "Materiali",
    "XLU": "Servizi di pubblica utilità",
    "XLRE": "Immobiliare",
}
BENCHMARK = "SPY"
STOCK_BENCHMARK = "ACWI"  # riferimento per il rendimento relativo delle aziende
UNIVERSE_CSV = Path(__file__).with_name("universe_us.csv")
CHUNK = 50  # simboli per richiesta di barre
LOOKBACK_DAYS = 600  # ~410 sedute: 12 mesi di rendimenti + SMA200 sull'intero grafico


def load_env(path: Path) -> None:
    """Carica KEY=VALUE da .env senza sovrascrivere variabili già presenti."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip("\"'"))


def keys() -> tuple[str, str] | None:
    k, s = os.environ.get("ALPACA_API_KEY", ""), os.environ.get("ALPACA_SECRET_KEY", "")
    return (k, s) if k and s else None


def fetch_bars(
    symbols: list[str], feed: str = "iex", creds: tuple[str, str] | None = None
) -> dict[str, pd.DataFrame]:
    """Barre giornaliere rettificate (split e dividendi) per i simboli richiesti."""
    from alpaca.data.enums import Adjustment, DataFeed
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    creds = creds or keys()
    if creds is None:
        raise RuntimeError("ALPACA_API_KEY / ALPACA_SECRET_KEY mancanti (.env)")
    client = StockHistoricalDataClient(*creds)
    out: dict[str, pd.DataFrame] = {}
    for i in range(
        0, len(symbols), CHUNK
    ):  # richieste da pochi simboli: meno chiusure di connessione
        chunk = symbols[i : i + CHUNK]
        req = StockBarsRequest(
            symbol_or_symbols=chunk,
            timeframe=TimeFrame.Day,
            start=datetime.now(UTC) - timedelta(days=LOOKBACK_DAYS),
            adjustment=Adjustment.ALL,
            feed=DataFeed(feed.lower()),
        )
        raw = with_retry(lambda req=req: client.get_stock_bars(req).df)
        if raw.empty:
            continue
        present = set(raw.index.get_level_values(0))
        for sym in chunk:
            if sym not in present:
                continue
            d = raw.loc[sym][["open", "high", "low", "close", "volume"]].copy()
            idx = pd.DatetimeIndex(d.index).tz_convert("America/New_York")
            d.index = idx.normalize().tz_localize(None)
            out[sym] = d
    return out


def demo_bars(symbols: list[str]) -> dict[str, pd.DataFrame]:
    """Serie sintetiche (random walk con seed fisso) per provare l'interfaccia offline."""
    idx = pd.bdate_range(end=pd.Timestamp.today().normalize(), periods=420)
    out = {}
    for sym in symbols:
        rng = np.random.default_rng(zlib.crc32(sym.encode()))
        drift = rng.uniform(-0.0002, 0.0007)
        sigma = rng.uniform(0.007, 0.016)
        close = 100 * np.exp(np.cumsum(rng.normal(drift, sigma, len(idx))))
        spread = np.abs(rng.normal(0, sigma / 2, len(idx))) * close
        out[sym] = pd.DataFrame(
            {
                "open": close * (1 + rng.normal(0, sigma / 4, len(idx))),
                "high": close + spread,
                "low": close - spread,
                "close": close,
                "volume": rng.integers(5e5, 3e6, len(idx)).astype(float),
            },
            index=idx,
        )
    return out


def account_snapshot() -> dict:
    """Conto e posizioni del portafoglio paper (sola lettura)."""
    from alpaca.trading.client import TradingClient

    creds = keys()
    if creds is None:
        raise RuntimeError("ALPACA_API_KEY / ALPACA_SECRET_KEY mancanti (.env)")
    client = TradingClient(*creds, paper=True)
    acc = client.get_account()
    positions = [
        {
            "symbol": p.symbol,
            "qty": float(p.qty),
            "avg_entry": float(p.avg_entry_price),
            "price": float(p.current_price),
            "market_value": float(p.market_value),
            "pl": float(p.unrealized_pl),
            "pl_pct": float(p.unrealized_plpc),
        }
        for p in client.get_all_positions()
    ]
    return {
        "equity": float(acc.equity),
        "cash": float(acc.cash),
        "buying_power": float(acc.buying_power),
        "last_equity": float(acc.last_equity),
        "status": str(getattr(acc.status, "value", acc.status)),
        "positions": positions,
    }


def load_universe() -> pd.DataFrame:
    """Titoli USA per settore (ticker, name, sector_etf, weight_pct)."""
    return pd.read_csv(UNIVERSE_CSV)
