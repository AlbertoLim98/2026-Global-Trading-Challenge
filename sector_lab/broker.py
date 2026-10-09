"""Broker: Alpaca (SOLO conto paper) e un broker simulato per la modalità demo.

Non esiste alcun percorso verso il conto reale: `paper=True` è fisso e l'avvio fallisce se
l'endpoint non è quello paper.
"""

from __future__ import annotations

import threading
import uuid
from datetime import UTC, datetime
from typing import Any, Protocol

from net import with_retry

PAPER_HOST = "paper-api.alpaca.markets"


class Broker(Protocol):
    name: str

    def account(self) -> dict: ...
    def positions(self) -> list[dict]: ...
    def clock(self) -> dict: ...
    def tradable_symbols(self) -> set[str] | None: ...
    def latest_prices(self, symbols: list[str]) -> dict[str, float]: ...
    def submit_market(self, symbol: str, side: str, qty: float, client_id: str) -> dict: ...
    def submit_limit(
        self, symbol: str, side: str, qty: float, limit_price: float, client_id: str
    ) -> dict: ...
    def get_order(self, order_id: str) -> dict: ...
    def cancel_order(self, order_id: str) -> str: ...
    def portfolio_history(self, period: str = "1M") -> list[tuple[str, float]]: ...


def _num(v: Any) -> float | None:
    return None if v is None else float(v)


def _order(o: Any) -> dict:
    return {
        "order_id": str(o.id),
        "order_status": str(getattr(o.status, "value", o.status)),
        "filled_qty": _num(o.filled_qty) or 0.0,
        "filled_avg_price": _num(o.filled_avg_price),
    }


class AlpacaBroker:
    name = "alpaca-paper"

    def __init__(self, api_key: str, secret_key: str, feed: str = "iex") -> None:
        from alpaca.data.historical import StockHistoricalDataClient
        from alpaca.trading.client import TradingClient

        self._t = TradingClient(api_key, secret_key, paper=True)  # paper, sempre
        base = getattr(self._t, "_base_url", None)
        endpoint = str(getattr(base, "value", base) or "")
        if PAPER_HOST not in endpoint:
            raise RuntimeError(f"Avvio rifiutato: l'endpoint {endpoint!r} non è quello paper")
        self._d = StockHistoricalDataClient(api_key, secret_key)
        self._feed = feed

    def account(self) -> dict:
        a = with_retry(self._t.get_account)
        return {
            "account_number": str(getattr(a, "account_number", "") or ""),
            "equity": float(a.equity),
            "cash": float(a.cash),
            "buying_power": float(a.buying_power),
            "last_equity": float(a.last_equity),
            "status": str(getattr(a.status, "value", a.status)),
        }

    def positions(self) -> list[dict]:
        return [
            {
                "symbol": p.symbol,
                "qty": float(p.qty),
                "avg_entry": float(p.avg_entry_price),
                "price": float(p.current_price),
                "market_value": float(p.market_value),
                "pl": float(p.unrealized_pl),
                "pl_pct": float(p.unrealized_plpc),
                "day_pl": float(getattr(p, "unrealized_intraday_pl", None) or 0.0),
            }
            for p in with_retry(self._t.get_all_positions)
        ]

    def clock(self) -> dict:
        c = with_retry(self._t.get_clock)
        return {
            "is_open": bool(c.is_open),
            "next_open": str(c.next_open),
            "next_close": str(c.next_close),
        }

    def tradable_symbols(self) -> set[str] | None:
        """Azioni USA attive e negoziabili su Alpaca (esclude titoli sospesi, fusi o ritirati)."""
        from alpaca.trading.enums import AssetClass, AssetStatus
        from alpaca.trading.requests import GetAssetsRequest

        assets = with_retry(
            lambda: self._t.get_all_assets(
                GetAssetsRequest(status=AssetStatus.ACTIVE, asset_class=AssetClass.US_EQUITY)
            )
        )
        return {a.symbol for a in assets if a.tradable}

    def latest_prices(self, symbols: list[str]) -> dict[str, float]:
        from alpaca.data.enums import DataFeed
        from alpaca.data.requests import StockLatestTradeRequest

        out: dict[str, float] = {}
        for i in range(0, len(symbols), 100):
            req = StockLatestTradeRequest(
                symbol_or_symbols=symbols[i : i + 100], feed=DataFeed(self._feed)
            )
            trades = with_retry(lambda req=req: self._d.get_stock_latest_trade(req))
            out |= {s: float(t.price) for s, t in trades.items()}
        return out

    def submit_market(self, symbol: str, side: str, qty: float, client_id: str) -> dict:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import MarketOrderRequest

        return self._send(
            MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
                client_order_id=client_id,
            ),
            client_id,
        )

    def submit_limit(
        self, symbol: str, side: str, qty: float, limit_price: float, client_id: str
    ) -> dict:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest

        return self._send(
            LimitOrderRequest(
                symbol=symbol,
                qty=qty,
                side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                time_in_force=TimeInForce.DAY,
                limit_price=round(limit_price, 2),
                client_order_id=client_id,
            ),
            client_id,
        )

    def _send(self, req: Any, client_id: str) -> dict:
        from alpaca.common.exceptions import APIError

        try:
            return _order(with_retry(lambda: self._t.submit_order(req)))
        except APIError as err:
            # stesso client_order_id già usato (es. doppio clic): restituisci l'ordine esistente
            try:
                return _order(with_retry(lambda: self._t.get_order_by_client_id(client_id)))
            except APIError:
                raise err from None

    def get_order(self, order_id: str) -> dict:
        return _order(with_retry(lambda: self._t.get_order_by_id(order_id)))

    def portfolio_history(self, period: str = "1M") -> list[tuple[str, float]]:
        """Patrimonio di fine giornata del conto: lista di (data AAAA-MM-GG, patrimonio), senza i giorni a zero."""
        from alpaca.trading.requests import GetPortfolioHistoryRequest

        req = GetPortfolioHistoryRequest(period=period, timeframe="1D")
        h = with_retry(lambda: self._t.get_portfolio_history(req))
        return [
            (datetime.fromtimestamp(ts, tz=UTC).strftime("%Y-%m-%d"), float(eq))
            for ts, eq in zip(h.timestamp or [], h.equity or [], strict=False)
            if eq
        ]

    def cancel_order(self, order_id: str) -> str:
        """Annulla un ordine aperto. Esito: canceled | not_found (conto diverso o azzerato) | not_cancelable."""
        from alpaca.common.exceptions import APIError

        try:
            with_retry(lambda: self._t.cancel_order_by_id(order_id))
            return "canceled"
        except APIError as e:
            code = getattr(e, "status_code", None)
            if code == 404:
                return "not_found"
            if code == 422:
                return "not_cancelable"  # già eseguito, annullato o scaduto
            raise


class OrderNotFound(Exception):
    """Ordine sconosciuto al conto (come l'errore 404 di Alpaca)."""

    status_code = 404


class DemoBroker:
    """Conto simulato in memoria: esegue subito al prezzo indicato. Solo per provare l'interfaccia."""

    name = "demo"

    def __init__(self, cash: float, prices: dict[str, float]) -> None:
        self.cash = cash
        self.prices = prices
        self.number = "DEMO"  # numero del conto simulato
        self.open = True  # mercato aperto?
        self.pos: dict[str, dict] = {}
        self.orders: dict[str, dict] = {}
        self._lock = threading.Lock()

    def account(self) -> dict:
        mv = sum(p["qty"] * self.prices.get(s, p["avg_entry"]) for s, p in self.pos.items())
        return {
            "account_number": self.number,
            "equity": self.cash + mv,
            "cash": self.cash,
            "buying_power": self.cash,
            "last_equity": self.cash + mv,
            "status": "DEMO",
        }

    def positions(self) -> list[dict]:
        out = []
        for s, p in self.pos.items():
            px = self.prices.get(s, p["avg_entry"])
            out.append(
                {
                    "symbol": s,
                    "qty": p["qty"],
                    "avg_entry": p["avg_entry"],
                    "price": px,
                    "market_value": p["qty"] * px,
                    "pl": p["qty"] * (px - p["avg_entry"]),
                    "pl_pct": px / p["avg_entry"] - 1,
                    "day_pl": 0.0,
                }
            )
        return out

    def clock(self) -> dict:
        return {"is_open": self.open, "next_open": "", "next_close": ""}

    def tradable_symbols(self) -> set[str] | None:
        return None  # demo: tutto è negoziabile

    def latest_prices(self, symbols: list[str]) -> dict[str, float]:
        return {s: self.prices[s] for s in symbols if s in self.prices}

    def submit_market(self, symbol: str, side: str, qty: float, client_id: str) -> dict:
        with self._lock:
            if client_id in self.orders:
                return self.orders[client_id]
            px = self.prices[symbol]
            if side == "buy":
                if qty * px > self.cash + 1e-6:
                    raise RuntimeError("liquidità insufficiente (demo)")
                p = self.pos.setdefault(symbol, {"qty": 0.0, "avg_entry": px})
                p["avg_entry"] = (p["avg_entry"] * p["qty"] + px * qty) / (p["qty"] + qty)
                p["qty"] += qty
                self.cash -= qty * px
            else:
                if symbol not in self.pos or qty > self.pos[symbol]["qty"] + 1e-9:
                    raise RuntimeError("posizione insufficiente (demo)")
                self.pos[symbol]["qty"] -= qty
                self.cash += qty * px
                if self.pos[symbol]["qty"] <= 1e-9:
                    del self.pos[symbol]
            o = {
                "order_id": uuid.uuid4().hex,
                "order_status": "filled",
                "filled_qty": qty,
                "filled_avg_price": px,
            }
            self.orders[client_id] = o
            return o

    def submit_limit(
        self, symbol: str, side: str, qty: float, limit_price: float, client_id: str
    ) -> dict:
        """Demo: l'ordine limite si esegue subito se il prezzo lo consente, altrimenti resta aperto."""
        px = self.prices[symbol]
        ok = limit_price >= px if side == "buy" else limit_price <= px
        if not ok:
            o = {
                "order_id": uuid.uuid4().hex,
                "order_status": "new",
                "filled_qty": 0.0,
                "filled_avg_price": None,
            }
            self.orders[client_id] = o
            return o
        return self.submit_market(symbol, side, qty, client_id)

    def get_order(self, order_id: str) -> dict:
        for o in self.orders.values():
            if o["order_id"] == order_id:
                return o
        raise OrderNotFound(f"order not found: {order_id}")

    def portfolio_history(self, period: str = "1M") -> list[tuple[str, float]]:
        return list(getattr(self, "history", []))  # serie di prova (data, patrimonio)

    def cancel_order(self, order_id: str) -> str:
        for o in self.orders.values():
            if o["order_id"] == order_id:
                if o["order_status"] in ("new", "accepted", "pending_new"):
                    o["order_status"] = "canceled"
                    return "canceled"
                return "not_cancelable"
        return "not_found"
