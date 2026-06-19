import json
import re
import time
from decimal import Decimal, InvalidOperation
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote_plus

import pandas as pd
import requests
import streamlit as st

POLY_GAMMA = "https://gamma-api.polymarket.com"
POLY_CLOB = "https://clob.polymarket.com"
KALSHI_BASE = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_BASE_ALT = "https://external-api.kalshi.com/trade-api/v2"

st.set_page_config(page_title="Oddpool Lite Scanner", layout="wide")
st.title("Oddpool Lite - Scanner automatico opportunita'")
st.caption(
    "Scanner gratuito Polymarket/Kalshi: trova mercati simili, legge top-of-book e calcola edge teorico. "
    "Non e' consulenza finanziaria e non esegue trade automatici."
)

# -----------------------------
# Utility
# -----------------------------

def dec(value: Any, default: str = "0") -> Decimal:
    try:
        if value is None or value == "":
            return Decimal(default)
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return Decimal(default)


def to_float(value: Any) -> float:
    try:
        if value is None or value == "":
            return 0.0
        return float(value)
    except Exception:
        return 0.0


def price_to_decimal(value: Any) -> Optional[Decimal]:
    """Normalize prices returned as dollars (0.42), cents (42), or fixed point (4200)."""
    if value is None or value == "":
        return None
    x = dec(value)
    if x > 100:
        return x / Decimal("10000")
    if x > 1:
        return x / Decimal("100")
    return x


def parse_json_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(x) for x in value]
    if isinstance(value, str):
        s = value.strip()
        if not s:
            return []
        try:
            parsed = json.loads(s)
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
        except Exception:
            pass
        if "," in s:
            return [x.strip().strip('"') for x in s.split(",") if x.strip()]
        return [s.strip('"')]
    return []


def text_of_market(m: Dict[str, Any]) -> str:
    fields = [
        m.get("question"), m.get("title"), m.get("eventTitle"), m.get("subtitle"),
        m.get("slug"), m.get("description"), m.get("category"), m.get("ticker"),
        m.get("event_ticker"), m.get("series_ticker"),
    ]
    return " ".join(str(x or "") for x in fields)


def normalize_text(s: str) -> str:
    s = s.lower()
    repl = {
        "$": " dollars ", "%": " percent ", "&": " and ",
        "btc": " bitcoin ", "eth": " ethereum ", "sol": " solana ",
        "fed": " federal reserve ", "cpi": " inflation cpi ",
        "election": " election ", "trump": " trump ", "biden": " biden ",
    }
    for k, v in repl.items():
        s = s.replace(k, v)
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


STOPWORDS = set("""
will the a an and or of in on by before after at to for with from is are be this that which who what when where above below over under yes no market markets contract event outcome resolve resolves resolution
""".split())


def keywords(s: str) -> set:
    s = normalize_text(s)
    words = [w for w in s.split() if len(w) >= 3 and w not in STOPWORDS]
    return set(words)


def important_numbers(s: str) -> set:
    return set(re.findall(r"\b\d+(?:\.\d+)?\b", s.lower()))


def similarity(a: str, b: str) -> Tuple[float, Dict[str, Any]]:
    an = normalize_text(a)
    bn = normalize_text(b)
    seq = SequenceMatcher(None, an, bn).ratio()
    ka = keywords(an)
    kb = keywords(bn)
    overlap = len(ka & kb) / max(1, len(ka | kb))
    nums_a = important_numbers(an)
    nums_b = important_numbers(bn)
    nums_match = 1.0 if (not nums_a or not nums_b or bool(nums_a & nums_b)) else 0.0
    score = 0.50 * overlap + 0.35 * seq + 0.15 * nums_match
    detail = {"seq": round(seq, 3), "keyword_overlap": round(overlap, 3), "numbers_match": nums_match}
    return score, detail


def confidence(score: float, details: Dict[str, Any]) -> str:
    if score >= 0.62 and details.get("numbers_match", 1) == 1:
        return "Alta"
    if score >= 0.48:
        return "Media"
    return "Bassa"


def dedupe(items: List[Dict[str, Any]], key_fields: List[str]) -> List[Dict[str, Any]]:
    seen = set()
    out = []
    for item in items:
        key = None
        for f in key_fields:
            if item.get(f):
                key = str(item.get(f))
                break
        if not key:
            key = text_of_market(item)[:120]
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def market_link_poly(m: Dict[str, Any]) -> str:
    slug = m.get("slug") or m.get("eventSlug") or ""
    if slug:
        return f"https://polymarket.com/event/{slug}"
    q = quote_plus(str(m.get("question") or m.get("title") or ""))
    return f"https://polymarket.com/search?query={q}"


def market_link_kalshi(m: Dict[str, Any]) -> str:
    ticker = m.get("ticker") or ""
    if ticker:
        return f"https://kalshi.com/markets/{ticker}"
    q = quote_plus(str(m.get("title") or ""))
    return f"https://kalshi.com/search?query={q}"


# -----------------------------
# Data fetchers
# -----------------------------

@st.cache_data(ttl=60, show_spinner=False)
def get_polymarket_markets(query: str = "", max_download: int = 3000) -> List[Dict[str, Any]]:
    session = requests.Session()
    results: List[Dict[str, Any]] = []
    q = query.strip()

    if q:
        for key in ["search", "q", "query"]:
            for endpoint in ["markets", "events"]:
                try:
                    params = {key: q, "active": "true", "closed": "false", "limit": 500}
                    r = session.get(f"{POLY_GAMMA}/{endpoint}", params=params, timeout=15)
                    if not r.ok:
                        continue
                    data = r.json()
                    items = data if isinstance(data, list) else data.get(endpoint, [])
                    if endpoint == "markets":
                        results.extend([x for x in items if isinstance(x, dict)])
                    else:
                        for ev in items:
                            if not isinstance(ev, dict):
                                continue
                            for m in ev.get("markets", []) or []:
                                if isinstance(m, dict):
                                    m = dict(m)
                                    m.setdefault("eventTitle", ev.get("title") or ev.get("slug"))
                                    m.setdefault("eventSlug", ev.get("slug"))
                                    results.append(m)
                except Exception:
                    pass

    page_size = 500
    offset = 0
    while offset < max_download:
        try:
            params = {
                "limit": page_size,
                "offset": offset,
                "active": "true",
                "closed": "false",
                "order": "volume24hr",
                "ascending": "false",
            }
            r = session.get(f"{POLY_GAMMA}/markets", params=params, timeout=15)
            r.raise_for_status()
            data = r.json()
            batch = data if isinstance(data, list) else data.get("markets", [])
            batch = [x for x in batch if isinstance(x, dict)]
            if not batch:
                break
            results.extend(batch)
            offset += len(batch)
            if len(batch) < page_size:
                break
        except Exception:
            break

    return dedupe(results, ["id", "conditionId", "question", "slug"])


@st.cache_data(ttl=60, show_spinner=False)
def get_kalshi_markets(max_download: int = 1000, search: str = "") -> List[Dict[str, Any]]:
    session = requests.Session()
    base_urls = [KALSHI_BASE, KALSHI_BASE_ALT]

    def download_from_base(base_url: str, params_extra: Dict[str, Any] | None = None) -> List[Dict[str, Any]]:
        results: List[Dict[str, Any]] = []
        cursor = None

        while len(results) < max_download:
            params = {
                "limit": min(1000, max_download - len(results)),
                "status": "open",
            }

            if cursor:
                params["cursor"] = cursor

            if params_extra:
                params.update(params_extra)

            try:
                r = session.get(f"{base_url}/markets", params=params, timeout=20)
                if not r.ok:
                    break

                data = r.json()
                batch = data.get("markets", []) if isinstance(data, dict) else []

                if not batch:
                    break

                results.extend([x for x in batch if isinstance(x, dict)])

                cursor = data.get("cursor") or data.get("next_cursor")
                if not cursor:
                    break

            except Exception:
                break

        return dedupe(results, ["ticker", "title"])

    if search.strip():
        for base_url in base_urls:
            searched = download_from_base(base_url, {"search": search.strip()})
            if searched:
                return searched

    for base_url in base_urls:
        generic = download_from_base(base_url)
        if generic:
            return generic

    st.warning("Kalshi non disponibile: entrambi gli host API non hanno restituito mercati.")
    return []


@st.cache_data(ttl=8, show_spinner=False)
def get_poly_book(token_id: str) -> Dict[str, Any]:
    r = requests.get(f"{POLY_CLOB}/book", params={"token_id": token_id}, timeout=12)
    r.raise_for_status()
    return r.json()


@st.cache_data(ttl=8, show_spinner=False)
def get_kalshi_orderbook(ticker: str) -> Dict[str, Any]:
    last_error = None
    for base in [KALSHI_BASE, KALSHI_BASE_ALT]:
        try:
            r = requests.get(f"{base}/markets/{ticker}/orderbook", timeout=12)
            if r.ok:
                return r.json()
            last_error = f"{r.status_code}: {r.text[:120]}"
        except Exception as e:
            last_error = str(e)
    raise RuntimeError(last_error or "Kalshi orderbook non disponibile")


# -----------------------------
# Price extraction
# -----------------------------

def polymarket_yes_token(m: Dict[str, Any]) -> Optional[str]:
    outcomes = [x.lower() for x in parse_json_list(m.get("outcomes"))]
    tokens = parse_json_list(m.get("clobTokenIds"))
    if not tokens:
        return None
    if outcomes and "yes" in outcomes:
        idx = outcomes.index("yes")
        if idx < len(tokens):
            return tokens[idx]
    return tokens[0]


def best_poly_prices(book: Dict[str, Any]) -> Tuple[Optional[Decimal], Optional[Decimal], Optional[Decimal], Optional[Decimal]]:
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    bid_rows = [(price_to_decimal(x.get("price")), dec(x.get("size"))) for x in bids if isinstance(x, dict)]
    ask_rows = [(price_to_decimal(x.get("price")), dec(x.get("size"))) for x in asks if isinstance(x, dict)]
    bid_rows = [(p, s) for p, s in bid_rows if p is not None]
    ask_rows = [(p, s) for p, s in ask_rows if p is not None]
    best_bid = max([p for p, _ in bid_rows], default=None)
    best_ask = min([p for p, _ in ask_rows], default=None)
    bid_size = next((s for p, s in bid_rows if p == best_bid), None) if best_bid is not None else None
    ask_size = next((s for p, s in ask_rows if p == best_ask), None) if best_ask is not None else None
    return best_bid, best_ask, bid_size, ask_size


def best_kalshi_prices_from_market(m: Dict[str, Any]) -> Tuple[Optional[Decimal], Optional[Decimal]]:
    bid = price_to_decimal(m.get("yes_bid") or m.get("yes_bid_dollars"))
    ask = price_to_decimal(m.get("yes_ask") or m.get("yes_ask_dollars"))
    return bid, ask


def best_kalshi_prices_from_book(ob: Dict[str, Any]) -> Tuple[Optional[Decimal], Optional[Decimal], Optional[Decimal], Optional[Decimal]]:
    book = ob.get("orderbook_fp") or ob.get("orderbook") or {}
    yes = book.get("yes_dollars") or book.get("yes") or []
    no = book.get("no_dollars") or book.get("no") or []

    yes_rows = []
    for row in yes:
        if isinstance(row, (list, tuple)) and len(row) >= 2:
            p = price_to_decimal(row[0])
            if p is not None:
                yes_rows.append((p, dec(row[1])))
    no_rows = []
    for row in no:
        if isinstance(row, (list, tuple)) and len(row) >= 2:
            p = price_to_decimal(row[0])
            if p is not None:
                no_rows.append((p, dec(row[1])))

    best_yes_bid = max([p for p, _ in yes_rows], default=None)
    best_no_bid = max([p for p, _ in no_rows], default=None)
    best_yes_ask = (Decimal("1") - best_no_bid) if best_no_bid is not None else None
    best_no_ask = (Decimal("1") - best_yes_bid) if best_yes_bid is not None else None
    return best_yes_bid, best_yes_ask, best_no_bid, best_no_ask


def net_edge(buy_price: Optional[Decimal], sell_price: Optional[Decimal], buffer_bps: Decimal) -> Optional[Decimal]:
    if buy_price is None or sell_price is None:
        return None
    buffer_cost = (buy_price + sell_price) * buffer_bps / Decimal("10000")
    return sell_price - buy_price - buffer_cost


def fmt_price(x: Optional[Decimal]) -> str:
    if x is None:
        return ""
    return f"{float(x):.4f}"


def fmt_pct(x: Optional[Decimal]) -> str:
    if x is None:
        return ""
    return f"{float(x * Decimal('100')):.2f}%"


# -----------------------------
# Scanner
# -----------------------------

def quick_filter(query: str, m: Dict[str, Any]) -> bool:
    if not query.strip():
        return True
    q_terms = keywords(query)
    if not q_terms:
        return True
    return bool(q_terms & keywords(text_of_market(m)))


def candidate_pairs(poly_markets: List[Dict[str, Any]], kalshi_markets: List[Dict[str, Any]], min_similarity: float, max_pairs: int) -> List[Dict[str, Any]]:
    """
    Trova pair candidati in modo piu' permissivo e conserva anche candidati sotto soglia per debug.
    """
    candidates = []

    k_index: Dict[str, List[int]] = {}
    for i, km in enumerate(kalshi_markets):
        for kw in list(keywords(text_of_market(km)))[:30]:
            k_index.setdefault(kw, []).append(i)

    for pm in poly_markets:
        pm_text = text_of_market(pm)
        pm_keys = keywords(pm_text)
        possible_idx = set()

        for kw in pm_keys:
            possible_idx.update(k_index.get(kw, []))

        if not possible_idx:
            possible_idx = set(range(min(len(kalshi_markets), 800)))

        local = []
        for idx in possible_idx:
            km = kalshi_markets[idx]
            score, details = similarity(pm_text, text_of_market(km))
            local.append((score, details, km))

        local.sort(key=lambda x: x[0], reverse=True)

        for score, details, km in local[:5]:
            candidates.append({
                "poly": pm,
                "kalshi": km,
                "similarity": score,
                "details": details,
                "passes_similarity": score >= min_similarity,
            })

    candidates.sort(key=lambda x: x["similarity"], reverse=True)
    passed = [c for c in candidates if c["passes_similarity"]]
    debug = [c for c in candidates if not c["passes_similarity"]]
    return (passed + debug)[:max_pairs]


def scan_opportunities(
    poly_markets: List[Dict[str, Any]],
    kalshi_markets: List[Dict[str, Any]],
    min_similarity: float,
    max_pairs: int,
    buffer_bps: Decimal,
    min_edge: Decimal,
    read_orderbooks: bool,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    Ritorna due tabelle:
    - opportunita': solo pair con edge >= filtro e similarita' sopra soglia;
    - debug: tutti i pair analizzati, inclusi quelli scartati, con motivo.
    """
    pairs = candidate_pairs(poly_markets, kalshi_markets, min_similarity, max_pairs)
    opportunity_rows = []
    debug_rows = []

    if not pairs:
        return pd.DataFrame(), pd.DataFrame()

    progress = st.progress(0, text="Analisi pair candidati...")
    total = max(1, len(pairs))

    for n, pair in enumerate(pairs, start=1):
        pm = pair["poly"]
        km = pair["kalshi"]
        token = polymarket_yes_token(pm)
        ticker = km.get("ticker")

        p_bid = p_ask = p_bid_size = p_ask_size = None
        k_bid = k_ask = None
        status = "ok"
        reject_reason = ""

        try:
            if token and read_orderbooks:
                pb = get_poly_book(token)
                p_bid, p_ask, p_bid_size, p_ask_size = best_poly_prices(pb)
            elif not token:
                reject_reason = "token Polymarket YES mancante"
        except Exception as e:
            status = f"Polymarket err: {str(e)[:80]}"
            reject_reason = "errore orderbook Polymarket"

        try:
            k_bid, k_ask = best_kalshi_prices_from_market(km)
            if ticker and read_orderbooks and (k_bid is None or k_ask is None):
                kb = get_kalshi_orderbook(ticker)
                k_bid, k_ask, _, _ = best_kalshi_prices_from_book(kb)
            elif not ticker:
                reject_reason = reject_reason or "ticker Kalshi mancante"
        except Exception as e:
            status = f"Kalshi err: {str(e)[:80]}" if status == "ok" else status + " | Kalshi err"
            reject_reason = reject_reason or "errore orderbook Kalshi"

        e1 = net_edge(p_ask, k_bid, buffer_bps)
        e2 = net_edge(k_ask, p_bid, buffer_bps)

        best_edge = None
        best_trade = ""

        if e1 is not None:
            best_edge = e1
            best_trade = "Compra YES Polymarket / vendi YES Kalshi"

        if e2 is not None and (best_edge is None or e2 > best_edge):
            best_edge = e2
            best_trade = "Compra YES Kalshi / vendi YES Polymarket"

        if best_edge is None:
            reject_reason = reject_reason or "prezzi bid/ask insufficienti"
        elif best_edge < min_edge:
            reject_reason = f"edge sotto filtro ({fmt_pct(best_edge)} < {fmt_pct(min_edge)})"
        elif pair["similarity"] < min_similarity:
            reject_reason = f"similarita sotto filtro ({pair['similarity']:.3f} < {min_similarity:.3f})"

        base_row = {
            "edge_netto": float(best_edge) if best_edge is not None else None,
            "edge_netto_%": fmt_pct(best_edge),
            "trade": best_trade,
            "confidence": confidence(pair["similarity"], pair["details"]),
            "similarity": round(pair["similarity"], 3),
            "passes_similarity": pair.get("passes_similarity", False),
            "polymarket": pm.get("question") or pm.get("title") or pm.get("eventTitle"),
            "kalshi": km.get("title") or km.get("subtitle") or km.get("ticker"),
            "poly_bid": fmt_price(p_bid),
            "poly_ask": fmt_price(p_ask),
            "kalshi_bid": fmt_price(k_bid),
            "kalshi_ask": fmt_price(k_ask),
            "poly_liquidity": to_float(pm.get("liquidity") or pm.get("liquidityNum")),
            "poly_volume24h": to_float(pm.get("volume24hr") or pm.get("volume24hrClob")),
            "kalshi_volume": to_float(km.get("volume")),
            "kalshi_liquidity": to_float(km.get("liquidity")),
            "kalshi_ticker": ticker,
            "poly_token_yes": token,
            "poly_link": market_link_poly(pm),
            "kalshi_link": market_link_kalshi(km),
            "matching_detail": json.dumps(pair["details"]),
            "status": status,
            "motivo_scarto": reject_reason,
        }

        debug_rows.append(base_row)

        if best_edge is not None and best_edge >= min_edge and pair["similarity"] >= min_similarity:
            opportunity_rows.append(base_row)

        progress.progress(n / total, text=f"Analisi pair {n}/{total}")
        time.sleep(0.01)

    progress.empty()

    opp_df = pd.DataFrame(opportunity_rows)
    debug_df = pd.DataFrame(debug_rows)

    if not opp_df.empty:
        opp_df = opp_df.sort_values(["edge_netto", "similarity"], ascending=[False, False], na_position="last")

    if not debug_df.empty:
        debug_df = debug_df.sort_values(["edge_netto", "similarity"], ascending=[False, False], na_position="last")

    return opp_df, debug_df


# -----------------------------
# Sidebar controls
# -----------------------------
with st.sidebar:
    st.header("Impostazioni scanner")
    search = st.text_input("Filtro tema", value="", placeholder="bitcoin, btc, fed, trump, inflation...")
    poly_download = st.slider("Mercati Polymarket da scaricare", 500, 8000, 3000, step=500)
    kalshi_download = st.slider("Mercati Kalshi da scaricare", 100, 3000, 1000, step=100)
    top_poly = st.slider("Top Polymarket usati nello scanner", 20, 1500, 500, step=10)
    top_kalshi = st.slider("Top Kalshi usati nello scanner", 20, 3000, 1000, step=20)
    max_pairs = st.slider("Pair candidati da analizzare", 10, 1000, 300, step=10)
    min_similarity = st.slider("Similarita' minima matching", 0.25, 0.85, 0.45, step=0.01)
    min_edge_pct = st.number_input("Mostra solo edge netto >= %", min_value=-20.0, max_value=20.0, value=0.0, step=0.1)
    buffer_bps = Decimal(str(st.number_input("Buffer fee/slippage, bps", min_value=0, max_value=1500, value=0, step=10)))
    read_orderbooks = st.checkbox("Leggi orderbook live", value=True)
    auto_refresh = st.checkbox("Auto-refresh 60 secondi", value=False)
    if st.button("Svuota cache / aggiorna dati"):
        st.cache_data.clear()
        st.rerun()
    st.divider()
    st.subheader("Pair manuale")
    poly_token = st.text_input("Polymarket token_id / asset_id")
    kalshi_ticker = st.text_input("Kalshi market ticker")

if auto_refresh:
    st.write("Auto-refresh attivo: aggiorna manualmente con Svuota cache se vuoi forzare subito.")

# -----------------------------
# Tabs
# -----------------------------
tab_scan, tab_poly, tab_kalshi, tab_manual, tab_help = st.tabs([
    "Scanner Opportunita'", "Mercati Polymarket", "Mercati Kalshi", "Pair manuale", "Note importanti"
])

with tab_scan:
    st.subheader("Scanner automatico Polymarket/Kalshi")
    st.write(
        "Lo scanner prova a trovare mercati simili tra le due piattaforme e calcola un edge teorico. "
        "Le opportunita' con confidence Media/Bassa vanno verificate manualmente: wording e regole di settlement possono essere diversi."
    )

    if st.button("Avvia scanner", type="primary"):
        with st.spinner("Scarico mercati Polymarket e Kalshi..."):
            poly_all = get_polymarket_markets(search, poly_download)
            kalshi_all = get_kalshi_markets(kalshi_download, "")

            poly_filtered = [m for m in poly_all if quick_filter(search, m)] if search.strip() else poly_all
            kalshi_filtered = [m for m in kalshi_all if quick_filter(search, m)] if search.strip() else kalshi_all

            poly_sorted = sorted(poly_filtered, key=lambda m: to_float(m.get("volume24hr") or m.get("liquidity") or 0), reverse=True)[:top_poly]
            kalshi_sorted = sorted(kalshi_filtered, key=lambda m: to_float(m.get("volume") or m.get("liquidity") or 0), reverse=True)[:top_kalshi]

        st.info(f"Dataset: {len(poly_sorted)} Polymarket usati su {len(poly_all)} scaricati; {len(kalshi_sorted)} Kalshi usati su {len(kalshi_all)} scaricati.")
        if not poly_sorted or not kalshi_sorted:
            st.warning("Pochi dati trovati. Prova filtro vuoto oppure una keyword piu' ampia: crypto, election, fed, inflation.")
        else:
            min_edge = Decimal(str(min_edge_pct)) / Decimal("100")
            df, debug_df = scan_opportunities(poly_sorted, kalshi_sorted, min_similarity, max_pairs, buffer_bps, min_edge, read_orderbooks)

            view_cols = [
                "edge_netto_%", "trade", "confidence", "similarity", "polymarket", "kalshi",
                "poly_bid", "poly_ask", "kalshi_bid", "kalshi_ask", "kalshi_ticker", "status"
            ]

            debug_cols = [
                "edge_netto_%", "confidence", "similarity", "motivo_scarto", "polymarket", "kalshi",
                "poly_bid", "poly_ask", "kalshi_bid", "kalshi_ask", "kalshi_ticker", "status", "matching_detail"
            ]

            if df.empty:
                st.warning("Nessuna opportunita' sopra i filtri impostati. Sotto trovi comunque i migliori pair candidati/debug.")
            else:
                st.success(f"Trovate {len(df)} opportunita' candidate. Ordinate per edge netto teorico.")
                st.dataframe(df[view_cols], width="stretch", hide_index=True)
                st.download_button(
                    "Scarica CSV opportunita'",
                    df.to_csv(index=False).encode("utf-8"),
                    file_name="oddpool_lite_opportunita.csv",
                    mime="text/csv",
                )
                st.markdown("#### Link e dettagli")
                for _, row in df.head(10).iterrows():
                    st.markdown(
                        f"- **{row['edge_netto_%']}** | {row['confidence']} | "
                        f"[Polymarket]({row['poly_link']}) | [Kalshi]({row['kalshi_link']}) | "
                        f"Ticker Kalshi: `{row['kalshi_ticker']}` | Token PM: `{row['poly_token_yes']}`"
                    )

            st.markdown("### Debug matching - migliori pair analizzati")
            st.caption(
                "Questa tabella mostra anche i pair scartati. Serve per capire se il problema e' matching, orderbook, prezzi o edge sotto filtro."
            )
            if debug_df.empty:
                st.info("Nessun pair candidato generato. Abbassa la similarita' minima o aumenta i mercati analizzati.")
            else:
                st.dataframe(debug_df[debug_cols], width="stretch", hide_index=True)
                st.download_button(
                    "Scarica CSV debug matching",
                    debug_df.to_csv(index=False).encode("utf-8"),
                    file_name="oddpool_lite_debug_matching.csv",
                    mime="text/csv",
                )

with tab_poly:
    st.subheader("Mercati Polymarket")
    with st.spinner("Carico Polymarket..."):
        poly_all = get_polymarket_markets(search, poly_download)
    poly_filtered = [m for m in poly_all if quick_filter(search, m)] if search.strip() else poly_all
    st.caption(f"Mercati mostrati: {len(poly_filtered)} su {len(poly_all)} scaricati. Filtro: {search or 'nessuno'}")
    rows = []
    for m in poly_filtered[:500]:
        outcomes = parse_json_list(m.get("outcomes"))
        token_ids = parse_json_list(m.get("clobTokenIds"))
        rows.append({
            "question": m.get("question") or m.get("title") or m.get("eventTitle"),
            "volume24hr": m.get("volume24hr"),
            "liquidity": m.get("liquidity"),
            "outcomes": ", ".join(outcomes),
            "YES token": polymarket_yes_token(m),
            "clobTokenIds": ", ".join(token_ids),
            "endDate": m.get("endDate"),
            "link": market_link_poly(m),
        })
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

with tab_kalshi:
    st.subheader("Mercati Kalshi")
    with st.spinner("Carico Kalshi..."):
        kalshi_all = get_kalshi_markets(kalshi_download, search)
    kalshi_filtered = [m for m in kalshi_all if quick_filter(search, m)] if search.strip() else kalshi_all
    st.caption(f"Mercati mostrati: {len(kalshi_filtered)} su {len(kalshi_all)} scaricati. Filtro: {search or 'nessuno'}")
    rows = []
    for m in kalshi_filtered[:500]:
        bid, ask = best_kalshi_prices_from_market(m)
        rows.append({
            "ticker": m.get("ticker"),
            "title": m.get("title"),
            "subtitle": m.get("subtitle"),
            "yes_bid": fmt_price(bid),
            "yes_ask": fmt_price(ask),
            "volume": m.get("volume"),
            "liquidity": m.get("liquidity"),
            "close_time": m.get("close_time"),
            "link": market_link_kalshi(m),
        })
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)

with tab_manual:
    st.subheader("Scanner pair manuale")
    st.write("Inserisci un token_id Polymarket e, opzionalmente, il ticker Kalshi dello stesso evento/outcome.")
    if st.button("Analizza pair manuale", type="primary"):
        if not poly_token:
            st.warning("Serve almeno il Polymarket token_id. Il ticker Kalshi e' opzionale.")
        else:
            col1, col2 = st.columns(2)
            poly_prices = kalshi_prices = None
            with col1:
                st.markdown("### Polymarket")
                try:
                    pb = get_poly_book(poly_token.strip())
                    p_bid, p_ask, p_bid_size, p_ask_size = best_poly_prices(pb)
                    poly_prices = (p_bid, p_ask)
                    st.metric("Best YES bid", fmt_price(p_bid) or "n/a")
                    st.metric("Best YES ask", fmt_price(p_ask) or "n/a")
                    st.write({"bid_size": str(p_bid_size), "ask_size": str(p_ask_size)})
                except Exception as e:
                    st.error(f"Errore Polymarket: {e}")
            with col2:
                st.markdown("### Kalshi")
                if kalshi_ticker.strip():
                    try:
                        kb = get_kalshi_orderbook(kalshi_ticker.strip())
                        k_bid, k_ask, k_no_bid, k_no_ask = best_kalshi_prices_from_book(kb)
                        kalshi_prices = (k_bid, k_ask)
                        st.metric("Best YES bid", fmt_price(k_bid) or "n/a")
                        st.metric("Best YES ask", fmt_price(k_ask) or "n/a")
                        st.write({"best_no_bid": fmt_price(k_no_bid), "best_no_ask": fmt_price(k_no_ask)})
                    except Exception as e:
                        st.error(f"Errore Kalshi: {e}")
                else:
                    st.info("Ticker Kalshi non inserito: mostro solo Polymarket.")

            if poly_prices and kalshi_prices:
                p_bid, p_ask = poly_prices
                k_bid, k_ask = kalshi_prices
                e1 = net_edge(p_ask, k_bid, buffer_bps)
                e2 = net_edge(k_ask, p_bid, buffer_bps)
                st.markdown("### Edge teorico al netto del buffer")
                st.dataframe(pd.DataFrame([
                    {"trade": "Buy YES Polymarket / Sell YES Kalshi", "edge": fmt_pct(e1)},
                    {"trade": "Buy YES Kalshi / Sell YES Polymarket", "edge": fmt_pct(e2)},
                ]), width="stretch", hide_index=True)
                if any(x is not None and x > 0 for x in [e1, e2]):
                    st.success("Possibile inefficienza. Verifica matching esatto, liquidita', fee, regole di settlement e reale esecuzione.")
                else:
                    st.info("Nessun edge positivo dopo il buffer impostato.")

with tab_help:
    st.subheader("Come leggere i risultati")
    st.markdown(
        """
**Edge netto** = differenza tra prezzo di vendita e prezzo di acquisto, meno il buffer fee/slippage impostato.

**Confidence** non garantisce che il mercato sia identico. Indica solo quanto i testi sembrano simili:
- **Alta**: candidato buono, ma da verificare comunque.
- **Media**: possibile, attenzione al wording.
- **Bassa**: spesso falso positivo.

**Regola pratica:** prima di mettere soldi, apri entrambi i link e controlla manualmente:
1. stesso evento;
2. stessa data/ora di settlement;
3. stessa soglia numerica;
4. stesso outcome YES/NO;
5. liquidita' sufficiente per entrare e uscire;
6. costi, spread e limiti account.

Questa app non piazza ordini e non promette profitto. Serve per trovare candidati da verificare.
        """
    )
