# Oddpool Lite

MVP gratuito ispirato a Oddpool: dashboard Streamlit per esplorare mercati Polymarket, leggere orderbook Polymarket/Kalshi e calcolare possibili inefficienze cross-platform su pair manuali.

## Installazione

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
streamlit run app.py
```

## Funzioni incluse

- Ricerca mercati attivi Polymarket tramite Gamma API pubblica.
- Estrazione `clobTokenIds` per usare gli orderbook CLOB.
- Lettura orderbook Polymarket tramite endpoint pubblico `/book`.
- Lettura orderbook Kalshi tramite endpoint REST pubblico, quando disponibile senza autenticazione.
- Calcolo edge teorico tra buy/sell YES con buffer bps per fee/slippage.

## Limiti importanti

- Il matching tra Polymarket e Kalshi e' manuale: devi assicurarti che evento, outcome e regole di settlement siano identici.
- Non esegue ordini e non gestisce wallet/API key.
- Non include storico, websocket, database, alert Telegram/email o backtesting.
- Un edge positivo non e' profitto garantito: possono esserci fee, slippage, latenza, limiti geografici, regolamentazione, cancellazioni o settlement diverso.

## Roadmap consigliata

1. Database SQLite/Postgres per snapshot storici.
2. Matcher semantico tra eventi Polymarket/Kalshi.
3. WebSocket per orderbook realtime.
4. Alert quando edge netto > soglia.
5. Paper trading per 30 giorni.
6. Solo dopo: moduli di trading autenticato, con limiti di rischio rigidi.
