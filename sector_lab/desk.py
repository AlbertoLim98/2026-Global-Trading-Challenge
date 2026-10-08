"""Orchestrazione del ribilanciamento: tabelle -> target -> operazioni -> invio automatico -> journal.

Premendo "Avvia ribilanciamento" (o "Controlla stop") le operazioni calcolate vengono inviate da sole
al conto paper: prima le vendite, poi gli acquisti. Non c'è un passaggio di approvazione per singola
operazione. Ogni operazione non andata a buon fine (ordine rifiutato, annullato, scaduto, eseguito
solo in parte, errore del broker) viene segnalata e si può riposizionare a prezzo attuale.
"""

from __future__ import annotations

import math
import re
import time
import uuid
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import data
import metrics
import portfolio
from journal import Journal, now

NY = ZoneInfo("America/New_York")
SETTLE_SECONDS = 15  # attesa massima dell'esito delle vendite prima di inviare gli acquisti
FAILED_ORDER = {"canceled", "expired", "rejected", "suspended", "done_for_day"}
# campi dello stato dell'invio, da non copiare in una operazione riposizionata
_RUNTIME = {
    "status", "snooze_until", "updated", "run_id", "error", "remaining_qty", "order_id",
    "exec_qty", "filled_qty", "filled_avg_price", "order_type", "shortfall",
}  # fmt: skip


_REPO_PREFIX = re.compile(r"^(Riposizionata a prezzo attuale \([^)]*\): )+")


class DeskError(Exception):
    """Errore da mostrare all'utente (non è un bug)."""


class Desk:
    def __init__(
        self, state: Any, broker: Any, journal: Journal, settle_seconds: float = SETTLE_SECONDS
    ) -> None:
        self.state, self.broker, self.journal = state, broker, journal
        self.settle_seconds = settle_seconds
        self.universe = data.load_universe()
        self.managed = {
            r.ticker: {"name": r.name, "sector": r.sector_etf} for r in self.universe.itertuples()
        }

    # --- dati di mercato -------------------------------------------------------------------------
    def _positions(self) -> dict[str, dict]:
        return {p["symbol"]: p for p in self.broker.positions() if p["qty"] > 0}

    def _atrs(self, symbols: list[str]) -> dict[str, float]:
        if not symbols:
            return {}
        bars = self.state.fetch_bars(symbols)
        out = {}
        for s, df in bars.items():
            v = metrics.atr(df).iloc[-1]
            if not math.isnan(v):
                out[s] = float(v)
        return out

    # --- ribilanciamento giornaliero ---------------------------------------------------------------
    def run(self, mode: str = "quality", capital: float = 1_000_000.0) -> dict:
        if mode not in ("quality", "beta"):
            raise DeskError(f"modalità sconosciuta: {mode}")
        if capital < 10_000:
            raise DeskError("capitale troppo basso")
        self.refresh_orders()
        busy = self.journal.proposals(("submitted",))
        if busy:
            names = ", ".join(sorted({b["symbol"] for b in busy})[:8])
            raise DeskError(
                f"Ci sono {len(busy)} ordini ancora in corso ({names}): attendi il loro esito "
                "prima di rilanciare, per non duplicare gli ordini"
            )
        p = portfolio.Params(capital=capital, mode=mode)
        run_id = now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]

        tradable = self.state.tradable(refresh=True)
        sectors = self.state.sectors(refresh=True)
        by_etf = {
            etf: self.state.stock_ranking(etf, refresh=True, mode=mode) for etf in data.SECTORS
        }
        positions = self._positions()
        held_managed = [s for s in positions if s in self.managed]
        atrs = self._atrs(held_managed)
        tg = portfolio.build_targets(sectors["sectors"], by_etf, p)
        need = sorted(set(tg["targets"]) | set(held_managed))
        prices = self.broker.latest_prices(need) if need else {}
        acct = self.broker.account()
        res = portfolio.build_proposals(
            tg["targets"], positions, prices, atrs, self.managed, acct["cash"], p
        )
        res["notes"] = [*tg["notes"], *res["notes"]]
        if tradable is not None:
            gone = sorted(set(self.managed) - tradable)
            if gone:
                shown = ", ".join(gone[:12]) + ("…" if len(gone) > 12 else "")
                res["notes"].append(
                    f"{len(gone)} titoli dell'elenco non sono negoziabili su Alpaca (non attivi, fusi o "
                    f"ritirati) e sono stati esclusi: {shown}"
                )
        if acct["equity"] < capital * 0.99:
            res["notes"].append(
                f"Il conto ha {acct['equity']:,.0f}$ di patrimonio, meno del capitale previsto "
                f"({capital:,.0f}$): gli acquisti sono limitati dalla liquidità disponibile"
            )

        superseded = self.journal.supersede_failed(run_id)
        self.journal.log(
            "RUN",
            {
                "params": p.as_dict(),
                "broker": self.broker.name,
                "account": acct,
                "sector_table": [_slim_sector(r) for r in sectors["sectors"]],
                "stock_tables": {k: [_slim_stock(r) for r in v["top"]] for k, v in by_etf.items()},
                "sector_budgets": tg["sectors"],
                "targets": list(tg["targets"].values()),
                "n_proposals": len(res["proposals"]),
                "notes": res["notes"],
                "superseded": superseded,
                "data_feed": sectors["feed"],
            },
            run_id,
        )
        for pr in res["proposals"]:
            self.journal.add_proposal(run_id, pr)
        outcome = self._execute_all([pr["id"] for pr in res["proposals"]])
        return {
            "run_id": run_id,
            "params": p.as_dict(),
            "account": acct,
            "sector_budgets": tg["sectors"],
            "n_targets": len(tg["targets"]),
            "invested_target": sum(t["value"] for t in tg["targets"].values()),
            "notes": res["notes"],
            "outcome": outcome,
        }

    # --- controllo stop (senza rifare le tabelle) -----------------------------------------------------
    def stop_check(self) -> dict:
        self.refresh_orders()
        positions = {s: x for s, x in self._positions().items() if s in self.managed}
        atrs = self._atrs(list(positions))
        p = portfolio.Params()
        active = {
            x["symbol"] for x in self.journal.proposals(("submitted",)) if x["kind"] == "STOP"
        }
        run_id = now().strftime("%Y%m%d-%H%M%S-stop")
        new = []
        for sym, pos in positions.items():
            loss = portfolio.stop_hit(pos, atrs.get(sym), p.atr_stop_mult)
            if loss is None or sym in active:
                continue
            info = self.managed[sym]
            atr = atrs[sym]
            new.append(
                portfolio._proposal(
                    "STOP", "sell", sym, info["name"], info["sector"], pos["qty"], pos["price"],
                    f"Perdita {loss:.2f}$/azione ({loss / pos['avg_entry']:.1%}) > "
                    f"{p.atr_stop_mult:g} ATR ({atr:.2f}$): vendita per stop",
                    atr=atr, loss_per_share=loss, avg_entry=pos["avg_entry"],
                )
            )  # fmt: skip
        self.journal.log("STOP_CHECK", {"n_positions": len(positions), "n_new": len(new)}, run_id)
        for pr in new:
            self.journal.add_proposal(run_id, pr)
        outcome = self._execute_all([pr["id"] for pr in new])
        return {"n_positions": len(positions), "n_new": len(new), "outcome": outcome}

    # --- invio degli ordini --------------------------------------------------------------------------
    def _execute_all(self, ids: list[str]) -> dict:
        """Invia tutte le operazioni: prima stop e vendite, poi (a vendite concluse) gli acquisti."""
        props = [self.journal.get_proposal(i) for i in ids]
        sells = [x["id"] for x in props if x["side"] == "sell"]
        buys = [x["id"] for x in props if x["side"] == "buy"]
        for pid in sells:
            self._send(pid)
        self._settle(sells)
        for pid in buys:
            self._send(pid)
        status = [self.journal.get_proposal(i)["status"] for i in ids]
        return {
            "total": len(ids),
            "filled": status.count("filled"),
            "in_progress": status.count("submitted"),
            "failed": status.count("failed"),
            "void": status.count("void"),
        }

    def _settle(self, sell_ids: list[str]) -> None:
        """Attende (al massimo `settle_seconds`) l'esito delle vendite: liberano potere d'acquisto."""
        deadline = time.monotonic() + self.settle_seconds
        while time.monotonic() < deadline:
            self.refresh_orders()
            if not any(self.journal.get_proposal(i)["status"] == "submitted" for i in sell_ids):
                return
            time.sleep(1)

    def _send(self, pid: str, limit: bool = False) -> str:
        """Invia l'ordine di una operazione. Non solleva eccezioni: un errore la segna come fallita."""
        pr = self.journal.get_proposal(pid)
        sym, side = pr["symbol"], pr["side"]
        try:
            tradable = self.state.tradable()
            if side == "buy" and tradable is not None and sym not in tradable:
                return self._fail(
                    pid,
                    f"{sym} non è negoziabile su Alpaca (non attivo): sarà escluso dal prossimo ribilanciamento",
                )
            price = self.broker.latest_prices([sym]).get(sym) or pr["price"]
            qty = pr["qty"]
            if side == "sell":
                held = self._positions().get(sym, {}).get("qty", 0.0)
                qty = min(qty, held)
                if qty <= 0:
                    self.journal.set_status(
                        pid, "void", "DECISION_DONE", {"note": "posizione non più presente"}
                    )
                    return "void"
            else:
                bp = self.broker.account()["buying_power"]
                max_qty = math.floor(bp / price)
                if max_qty < 1:
                    return self._fail(
                        pid,
                        f"potere d'acquisto insufficiente ({bp:,.0f}$) per {sym} a {price:.2f}$",
                    )
                qty = min(qty, max_qty)
            order_type = "limit" if limit else "market"
            self.journal.log(
                "DECISION",
                {
                    "action": "auto", "symbol": sym, "kind": pr["kind"], "side": side, "qty": qty,
                    "proposed_qty": pr["qty"], "price_at_decision": price, "order_type": order_type,
                },
                pr["run_id"], pid,
            )  # fmt: skip
            cid = f"sl-{pid}"
            order = (
                self.broker.submit_limit(sym, side, qty, price, cid)
                if limit
                else self.broker.submit_market(sym, side, qty, cid)
            )
        except Exception as e:  # noqa: BLE001 - qualsiasi errore del broker: segnala e prosegui
            msg = f"{type(e).__name__}: {e}"
            if "not active" in msg or "not tradable" in msg:
                msg += f" — {sym} non è negoziabile: sarà escluso dal prossimo ribilanciamento"
            return self._fail(pid, msg)
        self.journal.update_data(
            pid, order_id=order["order_id"], exec_qty=qty, order_type=order_type,
            filled_qty=order["filled_qty"], filled_avg_price=order["filled_avg_price"],
            shortfall=pr["qty"] - qty if qty < pr["qty"] else 0,
        )  # fmt: skip
        self.journal.log(
            "ORDER", {"symbol": sym, "side": side, "qty": qty, "order_type": order_type, **order},
            pr["run_id"], pid,
        )  # fmt: skip
        return self._apply_order(pid, order, qty)

    def _apply_order(self, pid: str, order: dict, qty: float) -> str:
        """Traduce lo stato dell'ordine in esito dell'operazione (eseguita / in corso / fallita)."""
        st = order["order_status"]
        filled = order["filled_qty"] or 0.0
        if st == "filled":
            self.journal.set_status(
                pid, "filled", "DECISION_DONE",
                {"order_id": order["order_id"], "order_status": st},
            )  # fmt: skip
            return "filled"
        if st in FAILED_ORDER:
            reason = f"ordine {st}"
            if filled > 0:
                reason += f": eseguite {filled:g} azioni su {qty:g}"
            return self._fail(pid, reason, remaining=max(qty - filled, 0.0))
        self.journal.set_status(
            pid, "submitted", "DECISION_DONE", {"order_id": order["order_id"], "order_status": st}
        )
        return "submitted"

    def _fail(self, pid: str, reason: str, remaining: float | None = None) -> str:
        pr = self.journal.get_proposal(pid)
        rem = pr["qty"] if remaining is None else remaining
        self.journal.update_data(pid, error=reason, remaining_qty=rem)
        self.journal.set_status(
            pid, "failed", "TRADE_FAILED",
            {"symbol": pr["symbol"], "side": pr["side"], "reason": reason, "remaining_qty": rem},
        )  # fmt: skip
        return "failed"

    def refresh_orders(self) -> int:
        """Aggiorna le operazioni con ordine inviato ma non concluso; segnala quelle fallite."""
        n = 0
        for pr in self.journal.proposals(("submitted",)):
            oid = pr.get("order_id")
            if not oid:
                continue
            try:
                o = self.broker.get_order(oid)
            except Exception as e:  # noqa: BLE001
                msg = f"{type(e).__name__}: {e}"
                self.journal.log("ERROR", {"message": msg, "order_id": oid}, pr["run_id"], pr["id"])
                continue
            if o["order_status"] == "filled" or o["order_status"] in FAILED_ORDER:
                self.journal.log(
                    "ORDER_UPDATE", {"symbol": pr["symbol"], **o}, pr["run_id"], pr["id"]
                )
                self.journal.update_data(
                    pr["id"], filled_qty=o["filled_qty"], filled_avg_price=o["filled_avg_price"]
                )
                self._apply_order(pr["id"], o, pr.get("exec_qty") or pr["qty"])
                n += 1
        return n

    # --- riposizionamento a prezzo attuale ---------------------------------------------------------
    def reposition(self, pid: str) -> dict:
        """Rimanda la quantità non eseguita di un'operazione fallita con un ordine limite al prezzo attuale."""
        pr = self.journal.get_proposal(pid)
        if pr is None:
            raise DeskError("operazione sconosciuta")
        if pr["status"] != "failed":
            raise DeskError(
                f"si possono riposizionare solo le operazioni fallite (stato: {pr['status']})"
            )
        remaining = pr.get("remaining_qty") or pr["qty"]
        try:
            price = self.broker.latest_prices([pr["symbol"]]).get(pr["symbol"])
        except Exception as e:
            raise DeskError(f"prezzo attuale non disponibile: {e}") from e
        if not price:
            raise DeskError(f"prezzo attuale non disponibile per {pr['symbol']}")
        new = {k: v for k, v in pr.items() if k not in _RUNTIME}
        new |= {
            "id": uuid.uuid4().hex[:12],
            "qty": remaining,
            "price": price,
            "value": remaining * price,
            "parent_id": pid,
            "reason": f"Riposizionata a prezzo attuale ({price:.2f}$, da {pid}): {_REPO_PREFIX.sub('', pr['reason'])}",
        }
        self.journal.add_proposal(pr["run_id"], new)
        self.journal.set_status(
            pid, "repositioned", "REPOSITION",
            {"symbol": pr["symbol"], "side": pr["side"], "qty": remaining, "limit_price": price,
             "new_id": new["id"]},
        )  # fmt: skip
        status = self._send(new["id"], limit=True)
        return {"status": status, "new_id": new["id"], "limit_price": price, "qty": remaining}

    def trades(self) -> list[dict]:
        return self.journal.trades()

    # --- portafoglio -----------------------------------------------------------------------------------
    def movements_today(self) -> list[dict]:
        """Movimenti eseguiti oggi (giorno di borsa, fuso di New York), dal più recente."""
        today = datetime.now(NY).date()
        out = []
        for pr in self.journal.proposals(("filled",)):
            when = datetime.fromisoformat(pr["updated"])
            if when.astimezone(NY).date() != today:
                continue
            qty = pr.get("filled_qty") or pr.get("exec_qty") or pr["qty"]
            price = pr.get("filled_avg_price") or pr["price"]
            out.append(
                {
                    "time": pr["updated"],
                    "side": pr["side"],
                    "kind": pr["kind"],
                    "symbol": pr["symbol"],
                    "name": pr["name"],
                    "qty": qty,
                    "price": price,
                    "value": qty * price,
                    "order_type": pr.get("order_type", "market"),
                    "reason": pr["reason"],
                }
            )
        return sorted(out, key=lambda m: m["time"], reverse=True)

    def portfolio(self) -> dict:
        """Composizione, P/L giornaliero e da apertura, movimenti di oggi."""
        acct = self.broker.account()
        equity = acct["equity"]
        rows = []
        for p in self.broker.positions():
            info = self.managed.get(p["symbol"])
            rows.append(
                {
                    **p,
                    "name": info["name"] if info else p["symbol"],
                    "sector": info["sector"] if info else None,
                    "sector_name": data.SECTORS.get(info["sector"], "")
                    if info
                    else "Fuori strategia",
                    "weight": p["market_value"] / equity if equity else 0.0,
                }
            )
        rows.sort(key=lambda r: -r["market_value"])
        by_sector: dict[str, dict] = {}
        for r in rows:
            d = by_sector.setdefault(
                r["sector_name"], {"sector_name": r["sector_name"], "value": 0.0, "n": 0, "pl": 0.0}
            )
            d["value"] += r["market_value"]
            d["pl"] += r["pl"]
            d["n"] += 1
        for d in by_sector.values():
            d["weight"] = d["value"] / equity if equity else 0.0
        day_pl = equity - acct["last_equity"]
        opening = self.journal.opening()
        base = opening["equity"] if opening else None
        return {
            "broker": self.broker.name,
            "account": acct,
            "cash_weight": acct["cash"] / equity if equity else 0.0,
            "day_pl": day_pl,
            "day_pl_pct": day_pl / acct["last_equity"] if acct["last_equity"] else None,
            "opening": opening,
            "since_open_pl": equity - base if base else None,
            "since_open_pct": (equity / base - 1) if base else None,
            "unrealized_pl": sum(r["pl"] for r in rows),
            "positions": rows,
            "sectors": sorted(by_sector.values(), key=lambda d: -d["value"]),
            "movements": self.movements_today(),
        }


def _slim_sector(r: dict) -> dict:
    keys = (
        "symbol", "name", "price", "trend_label", "ret_1m", "ret_3m", "ret_6m", "ret_12m", "rsi14",
        "rs_3m", "vol_ratio_20_90", "updown_volume", "vol_60d", "atr_pct", "drawdown_52w", "scores",
    )  # fmt: skip
    return {k: r.get(k) for k in keys}


def _slim_stock(r: dict) -> dict:
    keys = (
        "rank", "symbol", "name", "price", "trend_label", "twrr_w", "twrr_3m", "beta_1y",
        "te_60d", "rel_drawdown", "scores", "weight_in_sector",
    )  # fmt: skip
    return {k: r.get(k) for k in keys}
