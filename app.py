import os
import re
import json
import time
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests
import streamlit as st


# ============================================================
# PolyEdge Scanner
# Polymarket + bookmaker odds benchmark via The Odds API
# ============================================================

APP_VERSION = "PolyEdge Scanner v1.2 - block outright vs single-game mismatch"
POLY_GAMMA = "https://gamma-api.polymarket.com"
POLY_CLOB = "https://clob.polymarket.com"
ODDS_API_BASE = "https://api.the-odds-api.com/v4"

st.set_page_config(
    page_title="PolyEdge Scanner",
    page_icon="📊",
    layout="wide",
)

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "PolyEdgeScanner/1.0"})


# ============================================================
# Generic helpers
# ============================================================

def get_secret_or_env(name: str, default: str = "") -> str:
    try:
        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:
        pass
    return os.environ.get(name, default)


def normalize_text(s: Any) -> str:
    s = str(s or "").lower()
    s = s.replace("&", " and ")
    s = re.sub(r"[^a-z0-9\.\-\s]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def dec(x: Any, default: str = "0") -> Decimal:
    try:
        if x is None or x == "":
            return Decimal(default)
        return Decimal(str(x))
    except Exception:
        return Decimal(default)


def fmt_pct(x: Optional[Decimal]) -> str:
    if x is None:
        return ""
    return f"{float(x) * 100:.2f}%"


def fmt_money(x: Optional[Decimal]) -> str:
    if x is None:
        return ""
    return f"${float(x):,.2f}"


def fmt_price(x: Optional[Decimal]) -> str:
    if x is None:
        return ""
    return f"{float(x):.4f}"


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def short_time(iso_time: str) -> str:
    try:
        dt = datetime.fromisoformat(str(iso_time).replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except Exception:
        return str(iso_time or "")


def implied_prob_from_decimal_odds(odds: Decimal) -> Optional[Decimal]:
    if odds <= 0:
        return None
    return Decimal("1") / odds


def safe_float(x: Any) -> Optional[float]:
    try:
        if x is None or x == "":
            return None
        return float(x)
    except Exception:
        return None


def round_down_money(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.01"))


def market_link_poly(m: Dict[str, Any]) -> str:
    slug = m.get("slug") or m.get("marketSlug")
    if slug:
        return f"https://polymarket.com/market/{slug}"
    return "https://polymarket.com"


# ============================================================
# Team normalization
# ============================================================

TEAM_ALIASES = {
    "man utd": "manchester united",
    "man united": "manchester united",
    "man city": "manchester city",
    "spurs": "tottenham",
    "tottenham hotspur": "tottenham",
    "inter milan": "inter",
    "internazionale": "inter",
    "ac milan": "milan",
    "ny yankees": "new york yankees",
    "new york y": "new york yankees",
    "la dodgers": "los angeles dodgers",
    "los angeles d": "los angeles dodgers",
    "sf giants": "san francisco giants",
    "kc chiefs": "kansas city chiefs",
    "tb buccaneers": "tampa bay buccaneers",
    "gb packers": "green bay packers",
    "usa": "united states",
    "us": "united states",
    "turkiye": "turkey",
    "korea republic": "south korea",
    "republic of korea": "south korea",
}


def canonical(s: Any) -> str:
    text = normalize_text(s)
    for k, v in TEAM_ALIASES.items():
        text = re.sub(r"\b" + re.escape(k) + r"\b", v, text)
    return text


def words_set(s: Any) -> set:
    return set([w for w in canonical(s).split() if len(w) > 2])


def text_similarity(a: str, b: str) -> Decimal:
    aw = words_set(a)
    bw = words_set(b)
    if not aw or not bw:
        return Decimal("0")
    inter = aw & bw
    union = aw | bw
    return Decimal(len(inter)) / Decimal(len(union))


def contains_phrase(text: str, phrase: str) -> bool:
    return re.search(r"\b" + re.escape(canonical(phrase)) + r"\b", canonical(text)) is not None


def outcome_side_from_poly_question(question: str, home: str, away: str) -> Optional[str]:
    q = canonical(question)
    h = canonical(home)
    a = canonical(away)

    if contains_phrase(q, h):
        return home
    if contains_phrase(q, a):
        return away

    return None


def extract_total_line(question: str) -> Optional[Decimal]:
    q = canonical(question)
    m = re.search(r"\b(?:over|under|o/u|total)\s*([0-9]+(?:\.[0-9]+)?)\b", q)
    if m:
        return dec(m.group(1), "0")
    m = re.search(r"\b([0-9]+(?:\.[0-9]+)?)\s*(?:goals|runs|points)\b", q)
    if m:
        return dec(m.group(1), "0")
    return None


def is_outright_or_season_market(question: str) -> bool:
    """
    Blocca mercati stagionali/outright.
    Esempio NON confrontabile:
    - Will Cincinnati Bengals win the 2027 NFL AFC Championship?
    con:
    - Bengals @ Dolphins singola partita
    """
    q = canonical(question)

    outright_terms = [
        "championship",
        "afc championship",
        "nfc championship",
        "conference championship",
        "division",
        "super bowl",
        "world cup",
        "nba finals",
        "stanley cup",
        "world series",
        "tournament",
        "league winner",
        "win the 20",
        "win 20",
        "regular season",
        "mvp",
        "rookie of the year",
        "cy young",
        "ballon d or",
    ]

    return any(term in q for term in outright_terms)


def looks_like_single_game_market(question: str) -> bool:
    """
    True solo se la domanda Polymarket sembra riferirsi a una singola partita/evento.
    """
    q = canonical(question)

    single_game_patterns = [
        " vs ",
        " v ",
        " at ",
        " @ ",
        "beat",
        "defeat",
        "win on",
        "win against",
        "end in a draw",
        "end in draw",
        "over",
        "under",
        "spread",
    ]

    if any(p in q for p in single_game_patterns):
        return True

    # Date ISO o date testuali aiutano a distinguere singola partita.
    if re.search(r"\b20\d{2}-\d{2}-\d{2}\b", q):
        return True
    if re.search(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2}\b", q):
        return True

    return False


def poly_market_type(question: str) -> str:
    q = canonical(question)

    if is_outright_or_season_market(question):
        return "outright"

    if "over" in q or "under" in q or "o u" in q or "total" in q:
        return "totals"

    if "spread" in q or "handicap" in q:
        return "spreads"

    if ("win" in q or "winner" in q or "beat" in q) and looks_like_single_game_market(question):
        return "h2h"

    return "unknown"


# ============================================================
# Polymarket APIs
# ============================================================

@st.cache_data(ttl=180, show_spinner=False)
def fetch_polymarket_markets(max_download: int, query: str = "") -> Tuple[List[Dict[str, Any]], str]:
    results = []
    page_size = 100
    offset = 0

    while len(results) < max_download:
        params = {
            "limit": min(page_size, max_download - len(results)),
            "offset": offset,
            "active": "true",
            "closed": "false",
            "order": "volume24hr",
            "ascending": "false",
        }

        if query.strip():
            params["q"] = query.strip()

        try:
            r = SESSION.get(f"{POLY_GAMMA}/markets", params=params, timeout=20)

            # Gamma API puo' restituire 422 quando l'offset supera il range consentito.
            # Se abbiamo gia' raccolto dati, non e' un errore operativo: interrompiamo la paginazione.
            if r.status_code in (400, 404, 422) and results:
                break

            if r.status_code != 200:
                return results, f"HTTP {r.status_code}: {r.text[:300]}"

            data = r.json()
            batch = data if isinstance(data, list) else data.get("markets", [])
            batch = [x for x in batch if isinstance(x, dict)]

            if not batch:
                break

            results.extend(batch)
            offset += len(batch)

            if len(batch) < page_size:
                break

        except Exception as e:
            # Se abbiamo gia' scaricato risultati, usiamoli senza bloccare lo scanner.
            if results:
                break
            return results, str(e)

    return results, ""


def parse_jsonish(x: Any) -> Any:
    if isinstance(x, (list, dict)):
        return x
    if x is None:
        return None
    try:
        return json.loads(x)
    except Exception:
        return None


def polymarket_yes_token(m: Dict[str, Any]) -> Optional[str]:
    clob_ids = parse_jsonish(m.get("clobTokenIds"))
    outcomes = parse_jsonish(m.get("outcomes"))

    if isinstance(clob_ids, list) and len(clob_ids) > 0:
        if isinstance(outcomes, list):
            for i, out in enumerate(outcomes):
                if str(out).lower() == "yes" and i < len(clob_ids):
                    return str(clob_ids[i])
        return str(clob_ids[0])

    token = m.get("yesTokenId") or m.get("token_id") or m.get("conditionId")
    return str(token) if token else None


@st.cache_data(ttl=25, show_spinner=False)
def get_poly_orderbook(token_id: str) -> Tuple[Optional[Decimal], Optional[Decimal], str]:
    if not token_id:
        return None, None, "missing token"

    try:
        r = SESSION.get(f"{POLY_CLOB}/book", params={"token_id": token_id}, timeout=10)
        r.raise_for_status()
        data = r.json()

        bids = data.get("bids") or []
        asks = data.get("asks") or []

        best_bid = None
        best_ask = None

        if bids:
            bid_prices = [dec(x.get("price")) for x in bids if isinstance(x, dict)]
            bid_prices = [x for x in bid_prices if x > 0]
            best_bid = max(bid_prices) if bid_prices else None

        if asks:
            ask_prices = [dec(x.get("price")) for x in asks if isinstance(x, dict)]
            ask_prices = [x for x in ask_prices if x > 0]
            best_ask = min(ask_prices) if ask_prices else None

        return best_bid, best_ask, "ok"

    except Exception as e:
        return None, None, str(e)


def fallback_poly_prices(m: Dict[str, Any]) -> Tuple[Optional[Decimal], Optional[Decimal]]:
    bid = None
    ask = None

    for k in ["bestBid", "best_bid", "bid", "yesBid"]:
        if m.get(k) is not None:
            bid = dec(m.get(k))
            break

    for k in ["bestAsk", "best_ask", "ask", "yesAsk"]:
        if m.get(k) is not None:
            ask = dec(m.get(k))
            break

    if bid is not None and bid > 1:
        bid = bid / Decimal("100")
    if ask is not None and ask > 1:
        ask = ask / Decimal("100")

    return bid, ask


def is_relevant_poly_market(m: Dict[str, Any], theme: str) -> bool:
    text = " ".join([
        str(m.get("question") or ""),
        str(m.get("title") or ""),
        str(m.get("description") or ""),
        str(m.get("category") or ""),
        str(m.get("slug") or ""),
    ])

    question = str(m.get("question") or m.get("title") or "")
    q = canonical(text)

    if theme.strip():
        for term in canonical(theme).split():
            if term and term not in q:
                return False

    # In questa versione usiamo quote bookmaker di singole partite.
    # Quindi escludiamo championship/outright/stagionali.
    if is_outright_or_season_market(question):
        return False

    # Sports-focused single-game first version.
    sport_terms = [
        " vs ", " v ", " @ ", " at ", " beat", " win on", "win against",
        "spread", "over", "under", "goals", "runs", "points",
        "nba", "mlb", "nfl", "soccer", "football", "tennis",
        "premier league", "serie a", "la liga", "champions league",
    ]

    return any(t in q for t in sport_terms) and looks_like_single_game_market(question)


# ============================================================
# Odds API
# ============================================================

@st.cache_data(ttl=900, show_spinner=False)
def fetch_sports(api_key: str) -> Tuple[List[Dict[str, Any]], str]:
    if not api_key:
        return [], "Missing API key"

    try:
        r = SESSION.get(f"{ODDS_API_BASE}/sports", params={"apiKey": api_key}, timeout=20)
        if r.status_code != 200:
            return [], f"HTTP {r.status_code}: {r.text[:300]}"
        data = r.json()
        if not isinstance(data, list):
            return [], "Unexpected sports response"
        return data, ""
    except Exception as e:
        return [], str(e)


@st.cache_data(ttl=90, show_spinner=False)
def fetch_odds(
    api_key: str,
    sport_key: str,
    regions: str,
    markets: str,
    odds_format: str,
) -> Tuple[List[Dict[str, Any]], Dict[str, Any], str]:
    if not api_key:
        return [], {}, "Missing API key"

    params = {
        "apiKey": api_key,
        "regions": regions,
        "markets": markets,
        "oddsFormat": odds_format,
        "dateFormat": "iso",
    }

    try:
        r = SESSION.get(f"{ODDS_API_BASE}/sports/{sport_key}/odds", params=params, timeout=30)

        quota = {
            "requests_remaining": r.headers.get("x-requests-remaining"),
            "requests_used": r.headers.get("x-requests-used"),
            "requests_last": r.headers.get("x-requests-last"),
        }

        if r.status_code != 200:
            return [], quota, f"HTTP {r.status_code}: {r.text[:500]}"

        data = r.json()
        if not isinstance(data, list):
            return [], quota, "Unexpected odds response"

        return data, quota, ""

    except Exception as e:
        return [], {}, str(e)


def flatten_odds(events: List[Dict[str, Any]]) -> pd.DataFrame:
    rows = []

    for ev in events:
        event_id = ev.get("id")
        sport_key = ev.get("sport_key")
        sport_title = ev.get("sport_title")
        home = ev.get("home_team") or ""
        away = ev.get("away_team") or ""
        commence = ev.get("commence_time")
        event_label = f"{away} @ {home}" if home and away else str(event_id)

        for bm in ev.get("bookmakers", []) or []:
            bookmaker = bm.get("title") or bm.get("key")
            bookmaker_key = bm.get("key")
            last_update = bm.get("last_update")

            for market in bm.get("markets", []) or []:
                market_key = market.get("key")

                for outcome in market.get("outcomes", []) or []:
                    name = outcome.get("name")
                    point = outcome.get("point")
                    price = dec(outcome.get("price"))

                    if price <= 1:
                        continue

                    implied = implied_prob_from_decimal_odds(price)
                    rows.append({
                        "event_id": event_id,
                        "sport_key": sport_key,
                        "sport_title": sport_title,
                        "event": event_label,
                        "home_team": home,
                        "away_team": away,
                        "commence_time": commence,
                        "start": short_time(commence),
                        "bookmaker": bookmaker,
                        "bookmaker_key": bookmaker_key,
                        "last_update": last_update,
                        "market_key": market_key,
                        "market": market_key,
                        "outcome": name,
                        "point": point,
                        "decimal_odds": float(price),
                        "implied_probability": float(implied or Decimal("0")),
                    })

    return pd.DataFrame(rows)


def best_odds_for_event(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()

    rows = []

    group_cols = ["event_id", "event", "home_team", "away_team", "start", "market_key", "outcome", "point"]
    for keys, g in df.groupby(group_cols, dropna=False):
        event_id, event, home, away, start, market_key, outcome, point = keys

        best_idx = g["decimal_odds"].idxmax()
        best = g.loc[best_idx]

        odds = dec(best["decimal_odds"])
        implied = implied_prob_from_decimal_odds(odds)

        avg_implied = Decimal(str(g["implied_probability"].mean())) if len(g) else None

        rows.append({
            "event_id": event_id,
            "event": event,
            "home_team": home,
            "away_team": away,
            "start": start,
            "market_key": market_key,
            "outcome": outcome,
            "point": "" if pd.isna(point) else point,
            "best_bookmaker": best["bookmaker"],
            "best_odds": float(odds),
            "best_implied_probability": float(implied or Decimal("0")),
            "avg_market_implied_probability": float(avg_implied or Decimal("0")),
            "books_compared": int(len(g)),
        })

    return pd.DataFrame(rows)


def no_vig_probabilities(g: pd.DataFrame) -> Dict[str, Decimal]:
    """
    Converts best odds for outcomes in one event/market group into no-vig probabilities.
    """
    probs = {}
    total = Decimal("0")

    for _, row in g.iterrows():
        odds = dec(row.get("best_odds"))
        p = implied_prob_from_decimal_odds(odds)
        if p is None:
            continue
        key = f"{row.get('outcome')}|{row.get('point')}"
        probs[key] = p
        total += p

    if total <= 0:
        return {}

    return {k: v / total for k, v in probs.items()}


# ============================================================
# Matching Polymarket vs Odds
# ============================================================

def candidate_poly_questions(poly_markets: List[Dict[str, Any]]) -> pd.DataFrame:
    rows = []
    for m in poly_markets:
        question = m.get("question") or m.get("title") or ""
        if not question:
            continue

        volume = safe_float(m.get("volume24hr") or m.get("volume24hrClob") or m.get("volume") or 0) or 0
        liquidity = safe_float(m.get("liquidity") or m.get("liquidityNum") or 0) or 0

        rows.append({
            "poly_id": m.get("id") or m.get("conditionId"),
            "question": question,
            "market_type": poly_market_type(question),
            "volume": volume,
            "liquidity": liquidity,
            "slug": m.get("slug"),
            "token_yes": polymarket_yes_token(m),
            "raw": m,
        })

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(["volume", "liquidity"], ascending=[False, False])
    return df


def match_poly_to_odds(poly_df: pd.DataFrame, odds_best_df: pd.DataFrame, max_candidates: int) -> pd.DataFrame:
    if poly_df.empty or odds_best_df.empty:
        return pd.DataFrame()

    rows = []

    odds_by_event = odds_best_df.groupby(["event_id", "event", "home_team", "away_team", "start"], dropna=False)

    count = 0
    for _, p in poly_df.head(max_candidates).iterrows():
        q = p["question"]
        p_type = p["market_type"]

        if p_type in {"unknown", "outright"}:
            continue

        if not looks_like_single_game_market(q):
            continue

        for (event_id, event, home, away, start), eg in odds_by_event:
            event_text = f"{event} {home} {away}"
            sim = text_similarity(q, event_text)

            # Evita confronti assurdi tra mercato stagionale e singola partita.
            # Ora richiediamo una similarita' minima piu' alta.
            if sim < Decimal("0.16"):
                continue

            # Per h2h deve comparire almeno una delle due squadre dell'evento nella domanda Polymarket.
            if p_type == "h2h":
                side_check = outcome_side_from_poly_question(q, home, away)
                if not side_check:
                    continue

            target_market = p_type
            mg = eg[eg["market_key"] == target_market].copy()

            if mg.empty:
                continue

            target_outcome = None
            target_point = None
            confidence = "Bassa"
            reason = ""

            if target_market == "h2h":
                side = outcome_side_from_poly_question(q, home, away)
                if side:
                    target_outcome = side
                    confidence = "Media" if sim >= Decimal("0.12") else "Bassa"
                    reason = "team match"
                else:
                    continue

            elif target_market == "totals":
                line = extract_total_line(q)
                qn = canonical(q)
                if line is None:
                    continue

                if "over" in qn:
                    target_outcome = "Over"
                elif "under" in qn:
                    target_outcome = "Under"
                else:
                    continue

                target_point = line
                confidence = "Media" if sim >= Decimal("0.10") else "Bassa"
                reason = "total line match"

            elif target_market == "spreads":
                # V1: keep only debug candidates, because spread wording is more dangerous.
                continue

            if target_outcome is None:
                continue

            # Find exact outcome in bookmaker best odds.
            candidates = mg[mg["outcome"].apply(lambda x: canonical(x) == canonical(target_outcome))].copy()

            if target_point is not None:
                candidates = candidates[candidates["point"].apply(lambda x: dec(x, "-999") == target_point)]

            if candidates.empty:
                continue

            # Compute no-vig fair probability using all outcomes for same event/market/point.
            point_filter = "" if target_point is None else str(float(target_point)).rstrip("0").rstrip(".")
            if target_point is None:
                group_for_probs = mg
            else:
                group_for_probs = mg[mg["point"].apply(lambda x: dec(x, "-999") == target_point)]

            no_vig = no_vig_probabilities(group_for_probs)

            best = candidates.sort_values("best_odds", ascending=False).iloc[0]
            key = f"{best['outcome']}|{best['point']}"
            fair_prob = no_vig.get(key)

            if fair_prob is None:
                # fallback: average implied, less good
                fair_prob = Decimal(str(best["avg_market_implied_probability"]))

            # Upgrade confidence for exact team/event plus enough books.
            if confidence == "Media" and int(best["books_compared"]) >= 3 and sim >= Decimal("0.16"):
                confidence = "Alta"

            rows.append({
                "confidence": confidence,
                "reason": reason,
                "similarity": float(sim),
                "polymarket_question": q,
                "poly_market_type": p_type,
                "poly_token_yes": p["token_yes"],
                "poly_volume": p["volume"],
                "poly_liquidity": p["liquidity"],
                "poly_link": market_link_poly(p["raw"]),
                "odds_event": event,
                "event_start": start,
                "odds_market": target_market,
                "odds_outcome": best["outcome"],
                "odds_point": best["point"],
                "best_bookmaker": best["best_bookmaker"],
                "best_odds": best["best_odds"],
                "books_compared": best["books_compared"],
                "fair_probability": float(fair_prob),
                "event_id": event_id,
            })

            count += 1

    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.sort_values(["confidence", "similarity", "poly_volume"], ascending=[True, False, False])
        conf_order = {"Alta": 0, "Media": 1, "Bassa": 2}
        out["_conf_order"] = out["confidence"].map(conf_order).fillna(9)
        out = out.sort_values(["_conf_order", "similarity", "poly_volume"], ascending=[True, False, False]).drop(columns=["_conf_order"])
    return out


def evaluate_candidates(candidates: pd.DataFrame, capital: Decimal, read_orderbooks: bool, min_edge: Decimal) -> pd.DataFrame:
    if candidates.empty:
        return pd.DataFrame()

    rows = []
    total = len(candidates)
    progress = st.progress(0, text="Valuto candidati Polymarket... 0%")
    status = st.empty()

    for i, (_, row) in enumerate(candidates.iterrows(), start=1):
        token = row.get("poly_token_yes")
        poly_bid = poly_ask = None
        book_status = "not read"

        if read_orderbooks and token:
            poly_bid, poly_ask, book_status = get_poly_orderbook(str(token))

        if poly_bid is None or poly_ask is None:
            raw = None
            # p raw is not in candidates, fallback unavailable here.
            book_status = book_status if book_status != "not read" else "missing price"

        fair = dec(row.get("fair_probability"), "0")
        ask = poly_ask

        edge = None
        ev_on_capital = None
        suggested_action = "No trade"

        if ask is not None and ask > 0 and fair > 0:
            edge = fair - ask
            ev_on_capital = (capital / ask) * edge if ask > 0 else None

            if edge >= min_edge:
                suggested_action = "BUY YES Polymarket"
            elif edge <= -min_edge:
                suggested_action = "NO BUY - bookmaker fair lower"

        rows.append({
            **row.to_dict(),
            "poly_bid": fmt_price(poly_bid),
            "poly_ask": fmt_price(poly_ask),
            "book_status": book_status,
            "fair_probability_%": fmt_pct(fair),
            "edge_vs_poly_ask": float(edge) if edge is not None else None,
            "edge_vs_poly_ask_%": fmt_pct(edge),
            "capital": fmt_money(capital),
            "estimated_ev_$": fmt_money(ev_on_capital),
            "suggested_action": suggested_action,
        })

        pct = i / total
        progress.progress(pct, text=f"Valuto candidati {i}/{total} - {pct*100:.1f}%")
        status.caption(f"Candidati valutati: {i}/{total}")

    progress.empty()
    status.empty()

    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.sort_values(["suggested_action", "edge_vs_poly_ask", "similarity"], ascending=[True, False, False], na_position="last")
    return out


# ============================================================
# Paper trade log
# ============================================================

def init_paper_log():
    if "paper_trades" not in st.session_state:
        st.session_state.paper_trades = []


def add_paper_trade(row: Dict[str, Any], stake: Decimal):
    trade = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "action": row.get("suggested_action"),
        "stake": float(stake),
        "polymarket_question": row.get("polymarket_question"),
        "poly_ask": row.get("poly_ask"),
        "fair_probability_%": row.get("fair_probability_%"),
        "edge_vs_poly_ask_%": row.get("edge_vs_poly_ask_%"),
        "estimated_ev_$": row.get("estimated_ev_$"),
        "poly_link": row.get("poly_link"),
        "odds_event": row.get("odds_event"),
        "best_bookmaker": row.get("best_bookmaker"),
        "best_odds": row.get("best_odds"),
    }
    st.session_state.paper_trades.append(trade)


# ============================================================
# UI
# ============================================================

init_paper_log()

st.title("PolyEdge Scanner")
st.caption(APP_VERSION)

st.info(
    "Obiettivo: usare Polymarket come mercato tradabile e le quote bookmaker come benchmark di probabilita. "
    "Questa versione NON usa Kalshi."
)

st.warning(
    "Trade automatici real-money non sono abilitati in questa app. Per sicurezza questa versione genera segnali, "
    "trade plan e paper trade. L'esecuzione live su Polymarket richiede wallet/API key e va implementata solo in ambiente privato, non con private key su Streamlit Cloud."
)

with st.sidebar:
    st.header("Input")

    odds_api_key = st.text_input(
        "The Odds API key",
        value=get_secret_or_env("ODDS_API_KEY", ""),
        type="password",
        help="Per usarla in cloud, aggiungi ODDS_API_KEY nei secrets di Streamlit.",
    )

    if st.button("Svuota cache / aggiorna dati"):
        st.cache_data.clear()
        st.success("Cache svuotata. Rilancia lo scanner.")

    st.divider()

    sport_default_options = [
        "soccer_epl",
        "soccer_italy_serie_a",
        "soccer_spain_la_liga",
        "soccer_uefa_champs_league",
        "basketball_nba",
        "baseball_mlb",
        "americanfootball_nfl",
        "tennis_atp",
        "tennis_wta",
    ]

    sports, sports_err = fetch_sports(odds_api_key) if odds_api_key else ([], "")

    if sports:
        active = [s for s in sports if s.get("active", True)]
        labels = [f"{s.get('title')} ({s.get('key')})" for s in active]
        selected_label = st.selectbox("Sport The Odds API", labels, index=0)
        selected_sport = active[labels.index(selected_label)].get("key")
    else:
        selected_sport = st.selectbox("Sport The Odds API", sport_default_options, index=0)
        if sports_err:
            st.caption(f"Sports API: {sports_err}")

    regions = st.multiselect("Bookmaker regions", ["us", "uk", "eu", "au"], default=["us", "uk", "eu"])
    markets = st.multiselect("Mercati bookmaker", ["h2h", "totals", "spreads"], default=["h2h", "totals"])

    st.divider()

    st.caption("Nota: questa versione esclude mercati stagionali/outright Polymarket. Confronta solo singole partite/eventi.")
    theme = st.text_input("Filtro Polymarket", value="", placeholder="nba, mlb, nfl, team name, over 2.5...")
    poly_download = st.slider("Polymarket da scaricare", 100, 5000, 2500, step=100)
    top_poly = st.slider("Top Polymarket da analizzare", 50, 1500, 400, step=50)
    max_candidates = st.slider("Massimo candidati matching", 20, 1000, 250, step=10)

    st.divider()

    capital = dec(st.number_input("Capitale per trade ($)", min_value=1.0, max_value=100000.0, value=100.0, step=25.0))
    min_edge_pct = st.number_input("Edge minimo vs Polymarket ask (%)", min_value=-50.0, max_value=50.0, value=2.0, step=0.25)
    min_edge = dec(min_edge_pct) / Decimal("100")
    read_orderbooks = st.checkbox("Leggi orderbook Polymarket live", value=True)
    auto_refresh = st.checkbox("Auto-refresh 60 secondi", value=False)

    st.divider()
    st.caption("Trade automatici real-money: disabilitati in questa versione. Usa Paper Trade.")


if auto_refresh:
    time.sleep(60)
    st.rerun()


tab_scan, tab_poly, tab_odds, tab_paper, tab_setup = st.tabs([
    "Scanner",
    "Polymarket",
    "Bookmaker Odds",
    "Paper Trade",
    "Setup",
])


with tab_scan:
    st.subheader("Scanner Polymarket vs bookmaker benchmark")

    if not odds_api_key:
        st.error("Inserisci una The Odds API key nella sidebar o nei secrets Streamlit come ODDS_API_KEY.")
    elif not regions or not markets:
        st.error("Seleziona almeno una regione e almeno un mercato.")
    else:
        if st.button("Avvia scanner", type="primary"):
            with st.spinner("Scarico dati..."):
                poly_markets, poly_err = fetch_polymarket_markets(poly_download, theme)
                odds_events, quota, odds_err = fetch_odds(
                    odds_api_key,
                    selected_sport,
                    ",".join(regions),
                    ",".join(markets),
                    "decimal",
                )

            if poly_err:
                st.warning(f"Polymarket warning: {poly_err}")
            if odds_err:
                st.error(f"The Odds API error: {odds_err}")
                st.stop()

            poly_filtered = [m for m in poly_markets if is_relevant_poly_market(m, theme)]
            poly_df = candidate_poly_questions(poly_filtered).head(top_poly)
            odds_df = flatten_odds(odds_events)
            odds_best = best_odds_for_event(odds_df)

            st.info(
                f"Dataset: {len(poly_df)} Polymarket analizzati su {len(poly_markets)} scaricati; "
                f"{len(odds_events)} eventi odds; {len(odds_df)} quote bookmaker."
            )

            if quota:
                st.caption(f"Odds API quota: remaining={quota.get('requests_remaining')} used={quota.get('requests_used')} last={quota.get('requests_last')}")

            if poly_df.empty:
                st.warning("Nessun mercato Polymarket rilevante trovato. Prova un filtro diverso o vuoto.")
            elif odds_best.empty:
                st.warning("Nessuna quota bookmaker trovata per sport/mercati/regioni scelti.")
            else:
                with st.spinner("Matching strutturato Polymarket ↔ bookmaker odds..."):
                    candidates = match_poly_to_odds(poly_df, odds_best, max_candidates)

                if candidates.empty:
                    st.warning("Nessun candidato trovato. Prova un altro sport, filtro più specifico, o includi h2h/totals.")
                else:
                    st.markdown("### Candidati matching")
                    cand_cols = [
                        "confidence", "similarity", "polymarket_question", "odds_event", "event_start",
                        "odds_market", "odds_outcome", "odds_point", "best_bookmaker", "best_odds",
                        "books_compared", "fair_probability", "poly_link"
                    ]
                    st.dataframe(candidates[cand_cols].head(200), width="stretch", hide_index=True)

                    st.markdown("### Valutazione edge")
                    evaluated = evaluate_candidates(candidates, capital, read_orderbooks, min_edge)

                    if evaluated.empty:
                        st.info("Nessun candidato valutato.")
                    else:
                        main_cols = [
                            "suggested_action", "confidence", "edge_vs_poly_ask_%", "estimated_ev_$",
                            "capital", "poly_ask", "poly_bid", "fair_probability_%",
                            "polymarket_question", "odds_event", "event_start",
                            "odds_outcome", "best_bookmaker", "best_odds",
                            "books_compared", "poly_link", "book_status"
                        ]

                        st.dataframe(evaluated[main_cols].head(300), width="stretch", hide_index=True)

                        st.download_button(
                            "Scarica CSV segnali",
                            evaluated.to_csv(index=False).encode("utf-8"),
                            file_name="polyedge_signals.csv",
                            mime="text/csv",
                        )

                        buy_rows = evaluated[evaluated["suggested_action"] == "BUY YES Polymarket"].copy()
                        if not buy_rows.empty:
                            st.success(f"Trovati {len(buy_rows)} segnali BUY YES teorici sopra soglia.")
                            selected_idx = st.selectbox(
                                "Aggiungi un segnale al paper trade",
                                buy_rows.index.tolist(),
                                format_func=lambda i: f"{buy_rows.loc[i, 'edge_vs_poly_ask_%']} | {buy_rows.loc[i, 'polymarket_question'][:90]}"
                            )
                            paper_stake = dec(st.number_input("Stake paper trade ($)", min_value=1.0, max_value=100000.0, value=float(capital), step=25.0))
                            if st.button("Aggiungi Paper Trade"):
                                add_paper_trade(buy_rows.loc[selected_idx].to_dict(), paper_stake)
                                st.success("Paper trade aggiunto.")
                        else:
                            st.info("Nessun BUY YES sopra soglia. Puoi abbassare edge minimo o cambiare sport/filtro.")


with tab_poly:
    st.subheader("Mercati Polymarket")

    if st.button("Carica Polymarket", key="load_poly"):
        poly_markets, poly_err = fetch_polymarket_markets(poly_download, theme)
        if poly_err:
            st.warning(poly_err)

        poly_filtered = [m for m in poly_markets if is_relevant_poly_market(m, theme)]
        poly_df = candidate_poly_questions(poly_filtered)

        st.caption(f"{len(poly_df)} mercati rilevanti su {len(poly_markets)} scaricati.")

        if not poly_df.empty:
            show_cols = ["question", "market_type", "volume", "liquidity", "token_yes", "slug"]
            st.dataframe(poly_df[show_cols].head(500), width="stretch", hide_index=True)


with tab_odds:
    st.subheader("Bookmaker odds via The Odds API")

    if not odds_api_key:
        st.error("Inserisci The Odds API key.")
    elif st.button("Carica bookmaker odds", key="load_odds"):
        odds_events, quota, odds_err = fetch_odds(
            odds_api_key,
            selected_sport,
            ",".join(regions),
            ",".join(markets),
            "decimal",
        )

        if odds_err:
            st.error(odds_err)
        else:
            odds_df = flatten_odds(odds_events)
            odds_best = best_odds_for_event(odds_df)

            st.caption(f"{len(odds_events)} eventi; {len(odds_df)} quote; {len(odds_best)} best prices.")
            if quota:
                st.caption(f"Quota: remaining={quota.get('requests_remaining')} used={quota.get('requests_used')} last={quota.get('requests_last')}")

            if not odds_best.empty:
                show_cols = ["event", "start", "market_key", "outcome", "point", "best_bookmaker", "best_odds", "books_compared"]
                st.dataframe(odds_best[show_cols].head(1000), width="stretch", hide_index=True)


with tab_paper:
    st.subheader("Paper Trade Log")

    if not st.session_state.paper_trades:
        st.info("Nessun paper trade ancora.")
    else:
        paper_df = pd.DataFrame(st.session_state.paper_trades)
        st.dataframe(paper_df, width="stretch", hide_index=True)
        st.download_button(
            "Scarica paper trades CSV",
            paper_df.to_csv(index=False).encode("utf-8"),
            file_name="polyedge_paper_trades.csv",
            mime="text/csv",
        )

        if st.button("Svuota paper log"):
            st.session_state.paper_trades = []
            st.rerun()


with tab_setup:
    st.subheader("Setup")

    st.markdown(
        """
### Streamlit secrets

Aggiungi questa variabile nei secrets di Streamlit:

```toml
ODDS_API_KEY = "la_tua_api_key"
```

### Metodo

Questa app non cerca più Kalshi. Usa:

```text
Polymarket = mercato tradabile
The Odds API = benchmark quote bookmaker
```

Il segnale principale è:

```text
edge = fair_probability_bookmaker_no_vig - polymarket_yes_ask
```

Importante: questa versione confronta solo mercati Polymarket che sembrano riferiti a singole partite/eventi.
Mercati stagionali/outright come Championship, Super Bowl, division winner, MVP, World Cup winner vengono esclusi perché non sono confrontabili con quote h2h di una singola partita.

Se l'edge è positivo, Polymarket YES sembra più economico rispetto al benchmark bookmaker.

### Trade automatici

La versione attuale non invia ordini real-money. Genera:

```text
- segnale
- link Polymarket
- paper trade
- CSV operativo
```

L'esecuzione automatica live va fatta solo con API ufficiali, chiavi custodite in ambiente privato, limiti di rischio e conferma esplicita.
        """
    )
