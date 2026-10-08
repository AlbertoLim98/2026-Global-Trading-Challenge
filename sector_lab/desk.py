"""Orchestrazione del ribilanciamento: tabelle -> target -> proposte -> decisione -> ordine -> journal.

Nessun ordine parte senza una decisione esplicita dell'utente su quella singola proposta
(eseguire ora, rifiutare, rimandare di 1 ora).
"""

from __future__ import annotations

import math
import uuid
from datetime import timedelta
from typing import Any

import data
import metrics
import portfolio
from journal import OPEN_STATUSES, Journal, iso, now

SNOOZE = timedelta(hours=1)
FINAL_ORDER = {
    "filled": "filled",
    "canceled": "canceled",
    "expired": "canceled",
    "rejected": "canceled",
}


class DeskError(Exception):
    """Errore da mostrare all'utente (non è un bug)."""


class Desk:
    def __init__(self, state: Any, broker: Any, journal: Journal) -> None:
        self.state, self.broker, self.journal = state, broker, journal
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
        p = portfolio.Params(capital=capital, mode=mode)
        run_id = now().strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:4]

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
        if acct["equity"] < capital * 0.99:
            res["notes"].append(
                f"Il conto ha {acct['equity']:,.0f}$ di patrimonio, meno del capitale previsto "
                f"({capital:,.0f}$): gli acquisti sono limitati dalla liquidità disponibile"
            )

        superseded = self.journal.supersede_open(run_id)
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
        return {
            "run_id": run_id,
            "params": p.as_dict(),
            "account": acct,
            "sector_budgets": tg["sectors"],
            "n_targets": len(tg["targets"]),
            "invested_target": sum(t["value"] for t in tg["targets"].values()),
            "notes": res["notes"],
        }

    # --- controllo stop (senza rifare le tabelle) -----------------------------------------------------
    def stop_check(self) -> dict:
        positions = {s: x for s, x in self._positions().items() if s in self.managed}
        atrs = self._atrs(list(positions))
        p = portfolio.Params()
        open_stops = {
            x["symbol"] for x in self.journal.proposals(OPEN_STATUSES) if x["kind"] == "STOP"
        }
        run_id = "stop-" + now().strftime("%Y%m%d-%H%M%S")
        new = []
        for sym, pos in positions.items():
            loss = portfolio.stop_hit(pos, atrs.get(sym), p.atr_stop_mult)
            if loss is None or sym in open_stops:
                continue
            info = self.managed[sym]
            atr = atrs[sym]
            new.append(
                portfolio._proposal(
                    "STOP",
                    "sell",
                    sym,
                    info["name"],
                    info["sector"],
                    pos["qty"],
                    pos["price"],
                    f"Perdita {loss:.2f}$/azione ({loss / pos['avg_entry']:.1%}) > {p.atr_stop_mult:g} ATR "
                    f"({atr:.2f}$): vendita immediata consigliata",
                    atr=atr,
                    loss_per_share=loss,
                    avg_entry=pos["avg_entry"],
                )
            )
        self.journal.log("STOP_CHECK", {"n_positions": len(positions), "n_new": len(new)}, run_id)
        for pr in new:
            self.journal.add_proposal(run_id, pr)
        return {"n_positions": len(positions), "n_new": len(new)}

    # --- decisioni -----------------------------------------------------------------------------------
    def decide(self, pid: str, action: str) -> dict:
        pr = self.journal.get_proposal(pid)
        if pr is None:
            raise DeskError("proposta sconosciuta")
        if pr["status"] not in OPEN_STATUSES:
            raise DeskError(f"proposta già gestita (stato: {pr['status']})")
        base = {"action": action, "symbol": pr["symbol"], "kind": pr["kind"], "side": pr["side"]}
        if action == "reject":
            self.journal.set_status(pid, "rejected", "DECISION", base)
            return {"status": "rejected"}
        if action == "snooze":
            until = iso(now() + SNOOZE)
            self.journal.set_status(
                pid, "snoozed", "DECISION", {**base, "until": until}, snooze_until=until
            )
            return {"status": "snoozed", "snooze_until": until}
        if action != "execute":
            raise DeskError(f"azione sconosciuta: {action}")
        return self._execute(pr, base)

    def _execute(self, pr: dict, base: dict) -> dict:
        pid, sym = pr["id"], pr["symbol"]
        try:
            positions = self._positions()
            price = self.broker.latest_prices([sym]).get(sym) or pr["price"]
            if pr["side"] == "sell":
                held = positions.get(sym, {}).get("qty", 0.0)
                qty = min(pr["qty"], held)
                if qty <= 0:
                    self.journal.set_status(
                        pid, "void", "DECISION", {**base, "note": "posizione non più presente"}
                    )
                    raise DeskError("la posizione non è più presente: proposta annullata")
            else:
                bp = self.broker.account()["buying_power"]
                qty = min(pr["qty"], math.floor(bp / price))
                if qty < 1:
                    msg = f"potere d'acquisto insufficiente ({bp:,.0f}$) per {sym} a {price:.2f}$"
                    self.journal.log(
                        "ERROR",
                        {"message": msg, "symbol": sym, "action": "execute"},
                        pr["run_id"],
                        pid,
                    )
                    raise DeskError(msg)
            overdue = bool(pr.get("snooze_until") and pr["snooze_until"] <= iso(now()))
            self.journal.log(
                "DECISION",
                {
                    **base,
                    "qty": qty,
                    "proposed_qty": pr["qty"],
                    "price_at_decision": price,
                    "after_snooze": overdue,
                },
                pr["run_id"],
                pid,
            )
            order = self.broker.submit_market(sym, pr["side"], qty, f"sl-{pid}")
        except DeskError:
            raise
        except Exception as e:
            self.journal.log(
                "ERROR",
                {"message": f"{type(e).__name__}: {e}", "symbol": sym, "action": "execute"},
                pr["run_id"],
                pid,
            )
            raise DeskError(f"ordine non inviato: {e}") from e
        status = FINAL_ORDER.get(order["order_status"], "submitted")
        self.journal.update_data(pid, order_id=order["order_id"], exec_qty=qty)
        self.journal.log(
            "ORDER", {"symbol": sym, "side": pr["side"], "qty": qty, **order}, pr["run_id"], pid
        )
        self.journal.set_status(
            pid,
            status,
            "DECISION_DONE",
            {"order_id": order["order_id"], "order_status": order["order_status"]},
        )
        warn = None
        try:
            if not self.broker.clock()["is_open"]:
                warn = "Mercato chiuso: l'ordine resta in coda fino alla prossima apertura"
        except Exception:  # noqa: BLE001 - l'avviso è solo informativo
            warn = None
        return {"status": status, "order": order, "qty": qty, "warning": warn}

    def refresh_orders(self) -> int:
        """Aggiorna le proposte con ordine inviato ma non ancora concluso."""
        n = 0
        for pr in self.journal.proposals(("submitted",)):
            oid = pr.get("order_id")
            if not oid:
                continue
            try:
                o = self.broker.get_order(oid)
            except Exception as e:  # noqa: BLE001
                self.journal.log(
                    "ERROR",
                    {"message": f"{type(e).__name__}: {e}", "order_id": oid},
                    pr["run_id"],
                    pr["id"],
                )
                continue
            new = FINAL_ORDER.get(o["order_status"])
            if new:
                self.journal.log(
                    "ORDER_UPDATE", {"symbol": pr["symbol"], **o}, pr["run_id"], pr["id"]
                )
                self.journal.set_status(
                    pr["id"],
                    new,
                    "DECISION_DONE",
                    {"order_id": oid, "order_status": o["order_status"]},
                )
                n += 1
        return n

    def open_proposals(self) -> list[dict]:
        t = iso(now())
        out = []
        for pr in self.journal.proposals(OPEN_STATUSES):
            out.append(
                {
                    **pr,
                    "due": pr["status"] == "snoozed"
                    and bool(pr["snooze_until"])
                    and pr["snooze_until"] <= t,
                }
            )
        return out


def _slim_sector(r: dict) -> dict:
    keys = (
        "symbol",
        "name",
        "price",
        "trend_label",
        "ret_1m",
        "ret_3m",
        "ret_6m",
        "ret_12m",
        "rsi14",
        "rs_3m",
        "vol_ratio_20_90",
        "updown_volume",
        "vol_60d",
        "atr_pct",
        "drawdown_52w",
        "scores",
    )
    return {k: r.get(k) for k in keys}


def _slim_stock(r: dict) -> dict:
    keys = (
        "rank",
        "symbol",
        "name",
        "price",
        "trend_label",
        "twrr_w",
        "twrr_3m",
        "beta_1y",
        "te_60d",
        "rel_drawdown",
        "scores",
        "strategy_weight",
    )
    return {k: r.get(k) for k in keys}
