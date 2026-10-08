# Sector Lab

Programma autonomo per analizzare gli 11 settori USA tramite gli ETF SPDR (XLK, XLF, XLI, XLY,
XLV, XLC, XLP, XLE, XLB, XLU, XLRE), con SPY come benchmark. Usa le API Alpaca (paper) e non
dipende dal resto del progetto `analystagents`.

Per ogni settore calcola:

| Pilastro | Indicatori |
|---|---|
| Trend | prezzo vs SMA50/SMA200, SMA50 vs SMA200, etichetta Rialzista / Misto / Ribassista |
| Momentum | rendimenti 1/3/6/12 mesi, RSI(14), forza relativa 3 e 6 mesi vs SPY |
| Volume | volume medio 20g / 90g, rapporto volume giorni su / giù, controvalore medio |
| Volatilità | volatilità annualizzata 20g e 60g, ATR(14)%, distanza dal massimo a 52 settimane |

Ogni pilastro ha un punteggio 0-100 (percentile del settore sugli altri 10; per la volatilità
un valore basso dà un punteggio alto) e c'è un totale. È una classifica relativa per orientarsi,
non un segnale operativo. La scheda "Portafoglio paper" mostra conto e posizioni in sola lettura.

## Avvio

```bash
uv sync
# in .env: ALPACA_API_KEY e ALPACA_SECRET_KEY (chiavi PAPER)
uv run python sector_lab/server.py          # dati reali, http://127.0.0.1:8770
uv run python sector_lab/server.py --demo   # dati sintetici, senza chiavi
uv run pytest sector_lab                    # test (offline)
```

Il feed predefinito è `iex` (gratuito): i volumi sono solo quelli della borsa IEX, quindi i
rapporti sono confrontabili ma i valori assoluti no. Con un abbonamento dati usa `--feed sip`
o `ALPACA_DATA_FEED=sip`.

## Prossimo passo

Dai settori migliori alle aziende: una vista che, per il settore scelto, scarica i componenti
principali dell'ETF e applica le stesse metriche.
