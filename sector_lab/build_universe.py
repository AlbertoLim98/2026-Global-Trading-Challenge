"""Crea universe_us.csv dai titoli USA di un file di composizione iShares (.xls XML).

    uv run python sector_lab/build_universe.py percorso/file_ishares.xls

Il risultato (ticker, nome, settore ETF, peso) è l'elenco da cui Sector Lab sceglie
le aziende di ogni settore. Rilancialo con un file più recente per aggiornarlo.
"""

from __future__ import annotations

import csv
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

NS = {"s": "urn:schemas-microsoft-com:office:spreadsheet"}
HEAD = "Ticker dell'emittente"
# settore GICS (italiano, come nel file iShares) -> ETF SPDR
SECTOR_ETF = {
    "IT": "XLK",
    "Finanziari": "XLF",
    "Industriali": "XLI",
    "Consumi Discrezionali": "XLY",
    "Salute": "XLV",
    "Comunicazione": "XLC",
    "Generi di largo consumo": "XLP",
    "Energia": "XLE",
    "Materiali": "XLB",
    "Imprese di servizi di pubblica utilità": "XLU",
    "Immobili": "XLRE",
}
EXCLUDED_EXCHANGES = ("SIX Swiss Exchange", "NO MARKET")


def read_rows(path: Path) -> list[list[str]]:
    text = path.read_bytes().decode("utf-8-sig").lstrip("﻿")
    root = ET.fromstring(text)
    ws = root.find("s:Worksheet", NS)
    return [
        [c.findtext("s:Data", default="", namespaces=NS) for c in r.findall("s:Cell", NS)]
        for r in ws.iter("{" + NS["s"] + "}Row")
    ]


def main(src: str, out: str = "") -> None:
    rows = read_rows(Path(src))
    h = next(i for i, r in enumerate(rows) if HEAD in r)
    col = {name: i for i, name in enumerate(rows[h])}
    res = []
    for r in rows[h + 1 :]:
        if len(r) < len(col) or r[col["Asset Class"]] != "Azionario":
            continue
        if r[col["Area Geografica"]] != "Stati Uniti":
            continue
        if any(x in r[col["Cambio"]] for x in EXCLUDED_EXCHANGES):
            continue
        etf = SECTOR_ETF.get(r[col["Settore"]])
        if etf is None:
            continue
        res.append(
            (r[col[HEAD]].replace(" ", "."), r[col["Nome"]], etf, r[col["Ponderazione (%)"]])
        )
    dest = Path(out) if out else Path(__file__).with_name("universe_us.csv")
    with dest.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["ticker", "name", "sector_etf", "weight_pct"])
        w.writerows(res)
    print(f"{len(res)} titoli scritti in {dest}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    main(*sys.argv[1:3])
