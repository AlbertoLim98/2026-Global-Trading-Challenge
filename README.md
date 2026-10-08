# 2026 Global Trading Challenge

Strumenti di analisi per il paper trading su Alpaca. Contiene **Sector Lab**
(`sector_lab/`): trend, momentum, volume e volatilità degli 11 settori USA tramite ETF SPDR.
Istruzioni complete in [sector_lab/README.md](sector_lab/README.md).

```bash
cp .env.example .env     # inserisci le chiavi paper
uv sync
uv run python sector_lab/server.py          # http://127.0.0.1:8770
uv run python sector_lab/server.py --demo   # senza chiavi, dati sintetici
```
