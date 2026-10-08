"""Esporta la tabella dei settori in un file Excel formattato."""

from __future__ import annotations

import io
from collections.abc import Callable
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

PCT, PCT2, NUM, INT = "0.0%", "0.00%", "0.00", "#,##0"

# (gruppo, intestazione, estrattore, formato)
Col = tuple[str, str, Callable[[dict], Any], str | None]
COLUMNS: list[Col] = [
    ("", "Settore", lambda r: r["name"], None),
    ("", "ETF", lambda r: r["symbol"], None),
    ("", "Prezzo", lambda r: r["price"], NUM),
    ("", "Ultimo dato", lambda r: r["last_date"], None),
    ("Trend", "Stato", lambda r: r["trend_label"], None),
    ("Trend", "vs SMA50", lambda r: r["px_vs_sma50"], PCT),
    ("Trend", "vs SMA200", lambda r: r["px_vs_sma200"], PCT),
    ("Trend", "SMA50 vs SMA200", lambda r: r["sma50_vs_sma200"], PCT),
    ("Trend", "Score", lambda r: r["scores"]["trend"], "score"),
    ("Momentum", "1m", lambda r: r["ret_1m"], PCT),
    ("Momentum", "3m", lambda r: r["ret_3m"], PCT),
    ("Momentum", "6m", lambda r: r["ret_6m"], PCT),
    ("Momentum", "12m", lambda r: r["ret_12m"], PCT),
    ("Momentum", "RSI 14", lambda r: r["rsi14"], "0"),
    ("Momentum", "RS 3m vs SPY", lambda r: r["rs_3m"], PCT),
    ("Momentum", "RS 6m vs SPY", lambda r: r["rs_6m"], PCT),
    ("Momentum", "Score", lambda r: r["scores"]["momentum"], "score"),
    ("Volume", "Vol 20g/90g", lambda r: r["vol_ratio_20_90"], NUM),
    ("Volume", "Su/Giù", lambda r: r["updown_volume"], NUM),
    ("Volume", "Controvalore medio 20g", lambda r: r["dollar_volume_20d"], INT),
    ("Volume", "Score", lambda r: r["scores"]["volume"], "score"),
    ("Volatilità", "Vol 20g", lambda r: r["vol_20d"], PCT),
    ("Volatilità", "Vol 60g", lambda r: r["vol_60d"], PCT),
    ("Volatilità", "ATR%", lambda r: r["atr_pct"], PCT2),
    ("Volatilità", "Da max 52w", lambda r: r["drawdown_52w"], PCT),
    ("Volatilità", "Score", lambda r: r["scores"]["volatility"], "score"),
    ("", "Score totale", lambda r: r["scores"]["total"], "score"),
]

NOTES = [
    "Punteggi 0-100: posizione del settore rispetto agli altri 10 (percentile). Non sono segnali operativi.",
    "Trend: prezzo vs medie a 50 e 200 giorni. Rialzista = prezzo > SMA50 > SMA200; Ribassista = il contrario.",
    "Momentum: rendimenti a 1/3/6/12 mesi, RSI(14) e forza relativa (RS) rispetto a SPY.",
    "Volume: volume medio 20g / 90g e rapporto tra volume nei giorni in rialzo e in ribasso (ultimi 20g).",
    "Volatilità: più bassa = punteggio più alto (vol 60g, ATR, distanza dal massimo a 52 settimane).",
    "Feed IEX (gratuito): i volumi sono solo quelli della borsa IEX; i rapporti sono confrontabili, i valori assoluti no.",
]


def _fill(score: float) -> PatternFill:
    """Rosso (0) -> giallo (50) -> verde (100), toni chiari."""
    t = max(0.0, min(100.0, score)) / 100
    r, g = (255, int(120 + 270 * t)) if t < 0.5 else (int(255 - 280 * (t - 0.5)), 205)
    return PatternFill("solid", fgColor=f"{r:02X}{min(g, 215):02X}7A")


def build(payload: dict) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Settori"
    thin = Side(style="thin", color="BBBBBB")
    rows = sorted(payload["sectors"], key=lambda r: r["scores"]["total"] or -1, reverse=True)

    # riga 1: gruppi uniti; riga 2: intestazioni
    start = 1
    for i, (grp, head, _, _) in enumerate(COLUMNS, 1):
        ws.cell(2, i, head)
        if grp and (i == len(COLUMNS) or COLUMNS[i][0] != grp):
            ws.merge_cells(start_row=1, start_column=start, end_row=1, end_column=i)
            ws.cell(1, start, grp)
        if i < len(COLUMNS) and COLUMNS[i][0] != grp:
            start = i + 1
    for c in range(1, len(COLUMNS) + 1):
        for r_ in (1, 2):
            cell = ws.cell(r_, c)
            cell.font = Font(bold=True)
            cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
            if r_ == 2:
                cell.border = Border(bottom=thin)
                cell.fill = PatternFill("solid", fgColor="E8ECF4")

    for ri, row in enumerate(rows, 3):
        for ci, (_, _, get, fmt) in enumerate(COLUMNS, 1):
            v = get(row)
            cell = ws.cell(ri, ci, v)
            if fmt == "score":
                cell.number_format = "0"
                if v is not None:
                    cell.fill = _fill(v)
                cell.font = Font(bold=True)
                cell.alignment = Alignment(horizontal="center")
            elif fmt:
                cell.number_format = fmt
            if row.get("trend_label") and COLUMNS[ci - 1][1] == "Stato":
                color = {"Rialzista": "12805C", "Ribassista": "C0392B"}.get(v, "444444")
                cell.font = Font(bold=True, color=color)
                cell.alignment = Alignment(horizontal="center")
            if fmt in (PCT, PCT2) and isinstance(v, (int, float)) and v < 0:
                cell.font = Font(color="C0392B")

    for i, (_, head, _, _) in enumerate(COLUMNS, 1):
        width = 24 if head == "Settore" else max(10, min(22, len(head) + 3))
        ws.column_dimensions[get_column_letter(i)].width = width
    ws.row_dimensions[2].height = 32
    ws.freeze_panes = "C3"

    info = wb.create_sheet("Note")
    info["A1"] = "Sector Lab - ETF settoriali SPDR"
    info["A1"].font = Font(bold=True, size=13)
    info["A2"] = f"Aggiornato: {payload['updated']}"
    info["A3"] = f"Feed dati: {payload['feed']}" + (" (DATI SINTETICI)" if payload["demo"] else "")
    for i, line in enumerate(NOTES, 5):
        info.cell(i, 1, line)
    info.column_dimensions["A"].width = 120

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()
