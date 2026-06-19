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

APP_VERSION = "PolyEdge Scanner v1.4 - auto multi-sport + totals/BTTS/scorer matching"
POLY_GAMMA = "https://gamma-api.polymarket.com"
POLY_CLOB = "https://clob.polymarket.com"
ODDS_API_BASE = "https://api.the-odds-api.com/v4"

DEFAULT_AUTO_SPORTS = [
    "soccer_epl",
    "soccer_italy_serie_a",
    "soccer_spain_la_liga",
    "soccer_germany_bundesliga",
    "soccer_france_ligue_one",
    "soccer_uefa_champs_league",
    "soccer_fifa_world_cup",
    "basketball_nba",
    "baseball_mlb",
    "americanfootball_nfl",
    "icehockey_nhl",
    "tennis_atp",
    "tennis_wta",
]

CORE_MARKETS = ["h2h", "spreads", "totals"]

# Mercati extra: la disponibilita' dipende da sport/bookmaker/piano API.
# Se una richiesta fallisce, l'app la salta senza bloccare lo scanner.
EXTRA_MARKETS = [
    "btts",
    "draw_no_bet",
    "player_goal_scorer_anytime",
    "player_goal_scorer_first",
    "player_anytime_td",
    "player_points",
    "player_rebounds",
    "player_assists",
]

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


def iso_date_from_event_time(iso_time: Any) -> str:
    try:
        dt = datetime.fromisoformat(str(iso_time).replace("Z", "+00:00"))
        return dt.date().isoformat()
    except Exception:
        return ""


def extract_iso_dates(text: Any) -> set:
    return set(re.findall(r"\b20\d{2}-\d{2}-\d{2}\b", str(text or "")))


def extract_poly_match_dates(row_or_text: Any) -> set:
    if isinstance(row_or_text, dict):
        text = " ".join([
            str(row_or_text.get("question") or ""),
            str(row_or_text.get("slug") or ""),
            str(row_or_text.get("poly_slug") or ""),
        ])
    else:
        text = str(row_or_text or "")
    return extract_iso_dates(text)


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


FIFA_CODE_MAP = {
    "arg": "argentina",
    "aus": "australia",
    "aut": "austria",
    "bel": "belgium",
    "bih": "bosnia and herzegovina",
    "bra": "brazil",
    "can": "canada",
    "che": "switzerland",
    "civ": "ivory coast",
    "col": "colombia",
    "cpv": "cape verde",
    "cro": "croatia",
    "cuw": "curacao",
    "cze": "czechia",
    "deu": "germany",
    "dza": "algeria",
    "ecu": "ecuador",
    "egy": "egypt",
    "eng": "england",
    "esp": "spain",
    "fra": "france",
    "ger": "germany",
    "gha": "ghana",
    "hai": "haiti",
    "hti": "haiti",
    "irn": "iran",
    "irq": "iraq",
    "jpn": "japan",
    "kor": "south korea",
    "kr": "south korea",
    "ksa": "saudi arabia",
    "mar": "morocco",
    "mex": "mexico",
    "nld": "netherlands",
    "nor": "norway",
    "nz": "new zealand",
    "pan": "panama",
    "par": "paraguay",
    "pol": "poland",
    "por": "portugal",
    "prt": "portugal",
    "qat": "qatar",
    "rsa": "south africa",
    "sco": "scotland",
    "sen": "senegal",
    "sui": "switzerland",
    "swe": "sweden",
    "tun": "tunisia",
    "tur": "turkey",
    "uru": "uruguay",
    "usa": "united states",
    "uzb": "uzbekistan",
}


def expand_codes_to_teams(text: Any) -> str:
    """
    Espande codici presenti negli slug Polymarket, esempio:
    fifwc-col-prt-2026-06-27-prt -> colombia portugal 2026-06-27 portugal
    """
    raw = str(text or "").lower()
    pieces = re.split(r"[^a-z0-9]+", raw)
    expanded = []
    for p in pieces:
        if p in FIFA_CODE_MAP:
            expanded.append(FIFA_CODE_MAP[p])
        else:
            expanded.append(p)
    return " ".join(expanded)


def canonical(s: Any) -> str:
    text = normalize_text(str(s or "") + " " + expand_codes_to_teams(s))
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

    # Goal / no goal, both teams to score.
    if (
        "both teams to score" in q
        or "btts" in q
        or "both score" in q
        or "goal no goal" in q
        or "no goal" in q
        or "both teams score" in q
    ):
        return "btts"

    # Scorer / player props.
    if (
        "score a goal" in q
        or "scores a goal" in q
        or "anytime scorer" in q
        or "first goalscorer" in q
        or "first goal scorer" in q
        or "to score" in q
        or "touchdown" in q
        or "td scorer" in q
    ):
        return "scorer"

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
        "both teams to score", "btts", "goal no goal", "no goal",
        "score a goal", "scores a goal", "anytime scorer", "first goalscorer",
        "touchdown", "td scorer",
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


def fetch_odds_multi_sport(
    api_key: str,
    sport_keys: List[str],
    regions: str,
    core_markets: List[str],
    extra_markets: List[str],
    odds_format: str = "decimal",
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
    """
    Scarica quote per piu' sport.
    I mercati core vengono richiesti insieme.
    I mercati extra vengono richiesti separatamente, e se falliscono non bloccano lo scanner.
    """
    all_events = []
    quota_rows = []
    errors = []

    for sport_key in sport_keys:
        sport_key = str(sport_key).strip()
        if not sport_key:
            continue

        # Core markets.
        if core_markets:
            events, quota, err = fetch_odds(
                api_key,
                sport_key,
                regions,
                ",".join(core_markets),
                odds_format,
            )
            if err:
                errors.append(f"{sport_key} core: {err}")
            else:
                all_events.extend(events)
            if quota:
                quota_rows.append({"sport": sport_key, **quota})

        # Extra markets: try one by one, skip unsupported.
        for mkt in extra_markets:
            events, quota, err = fetch_odds(
                api_key,
                sport_key,
                regions,
                mkt,
                odds_format,
            )
            if not err and events:
                all_events.extend(events)
            elif err:
                # Keep only concise diagnostics.
                errors.append(f"{sport_key} {mkt}: {err[:120]}")
            if quota:
                quota_rows.append({"sport": sport_key, "market": mkt, **quota})

    # Deduplicate events while preserving bookmaker market content is hard because the same event
    # can be returned with different markets. We keep all and flatten later; duplicate rows are harmless.
    return all_events, quota_rows, errors


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
                        "event_date": iso_date_from_event_time(commence),
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

    group_cols = ["event_id", "event", "home_team", "away_team", "event_date", "start", "market_key", "outcome", "point"]
    for keys, g in df.groupby(group_cols, dropna=False):
        event_id, event, home, away, event_date, start, market_key, outcome, point = keys

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
            "event_date": event_date,
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

        slug = m.get("slug") or ""
        match_text = f"{question} {slug} {expand_codes_to_teams(slug)}"

        rows.append({
            "poly_id": m.get("id") or m.get("conditionId"),
            "question": question,
            "match_text": match_text,
            "poly_dates": ",".join(sorted(extract_poly_match_dates({"question": question, "slug": slug}))),
            "market_type": poly_market_type(match_text),
            "volume": volume,
            "liquidity": liquidity,
            "slug": slug,
            "token_yes": polymarket_yes_token(m),
            "raw": m,
        })

    df = pd.DataFrame(rows)
    if not df.empty:
        df = df.sort_values(["volume", "liquidity"], ascending=[False, False])
    return df


def target_outcome_for_market(q_match_text: str, target_market: str, home: str, away: str) -> Tuple[Optional[str], Optional[Decimal], str]:
    qn = canonical(q_match_text)

    if target_market == "h2h":
        side = outcome_side_from_poly_question(q_match_text, home, away)
        if side:
            return side, None, "team match"
        return None, None, "missing team side"

    if target_market == "totals":
        line = extract_total_line(q_match_text)
        if line is None:
            return None, None, "missing total line"
        if "over" in qn:
            return "Over", line, "total over line match"
        if "under" in qn:
            return "Under", line, "total under line match"
        return None, None, "missing over/under side"

    if target_market == "btts":
        if "no goal" in qn or "both teams not" in qn or "not both" in qn:
            return "No", None, "btts no"
        if "both teams to score" in qn or "btts" in qn or "both score" in qn or "goal no goal" in qn:
            return "Yes", None, "btts yes"
        return None, None, "missing btts side"

    if target_market == "scorer":
        # For player props we do fuzzy player-name matching later.
        return "PLAYER", None, "player scorer/prop"

    return None, None, "unsupported market"


def odds_market_candidates_for_poly_type(p_type: str) -> List[str]:
    if p_type == "h2h":
        return ["h2h"]
    if p_type == "totals":
        return ["totals"]
    if p_type == "spreads":
        return ["spreads"]
    if p_type == "btts":
        return ["btts"]
    if p_type == "scorer":
        return [
            "player_goal_scorer_anytime",
            "player_goal_scorer_first",
            "player_anytime_td",
            "player_points",
            "player_rebounds",
            "player_assists",
        ]
    return []


def match_poly_to_odds(poly_df: pd.DataFrame, odds_best_df: pd.DataFrame, max_candidates: int) -> pd.DataFrame:
    if poly_df.empty or odds_best_df.empty:
        return pd.DataFrame()

    rows = []

    odds_by_event = odds_best_df.groupby(["event_id", "event", "home_team", "away_team", "event_date", "start"], dropna=False)

    for _, p in poly_df.head(max_candidates).iterrows():
        q = p["question"]
        q_match_text = p.get("match_text") or q
        poly_dates = set(str(p.get("poly_dates") or "").split(",")) if str(p.get("poly_dates") or "") else set()
        p_type = p["market_type"]

        if p_type in {"unknown", "outright"}:
            continue

        if not looks_like_single_game_market(q_match_text):
            continue

        possible_markets = odds_market_candidates_for_poly_type(p_type)
        if not possible_markets:
            continue

        for (event_id, event, home, away, event_date, start), eg in odds_by_event:
            event_text = f"{event} {home} {away}"
            sim = text_similarity(q_match_text, event_text)

            # Date must match if Polymarket contains ISO date.
            if poly_dates and event_date not in poly_dates:
                continue

            if sim < Decimal("0.16"):
                continue

            # For h2h require one of the two teams.
            if p_type == "h2h":
                side_check = outcome_side_from_poly_question(q_match_text, home, away)
                if not side_check:
                    continue

            for target_market in possible_markets:
                mg = eg[eg["market_key"] == target_market].copy()
                if mg.empty:
                    continue

                target_outcome, target_point, reason = target_outcome_for_market(q_match_text, p_type, home, away)
                if target_outcome is None:
                    continue

                if p_type in {"h2h", "totals", "btts"}:
                    candidates = mg[mg["outcome"].apply(lambda x: canonical(x) == canonical(target_outcome))].copy()

                    if target_point is not None:
                        candidates = candidates[candidates["point"].apply(lambda x: dec(x, "-999") == target_point)]

                    if candidates.empty:
                        continue

                elif p_type == "scorer":
                    cand_rows = []
                    for _, rr in mg.iterrows():
                        out_text = str(rr.get("outcome") or "")
                        player_sim = text_similarity(q_match_text, out_text)
                        if player_sim >= Decimal("0.25"):
                            rrd = rr.to_dict()
                            rrd["_player_sim"] = float(player_sim)
                            cand_rows.append(rrd)

                    if not cand_rows:
                        continue

                    candidates = pd.DataFrame(cand_rows).sort_values("_player_sim", ascending=False)

                else:
                    continue

                # Compute no-vig fair probability using all outcomes for same event/market/point.
                if target_point is None:
                    group_for_probs = mg
                else:
                    group_for_probs = mg[mg["point"].apply(lambda x: dec(x, "-999") == target_point)]

                no_vig = no_vig_probabilities(group_for_probs)

                best = candidates.sort_values("best_odds", ascending=False).iloc[0]
                key = f"{best['outcome']}|{best['point']}"
                fair_prob = no_vig.get(key)

                if fair_prob is None:
                    fair_prob = Decimal(str(best["avg_market_implied_probability"]))

                confidence = "Media"
                if int(best["books_compared"]) >= 3 and sim >= Decimal("0.18"):
                    confidence = "Alta"

                # For extra markets, be stricter.
                if p_type in {"btts", "scorer"} and int(best["books_compared"]) < 2:
                    confidence = "Media"

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
                    "event_date": event_date,
                    "poly_dates": ",".join(sorted(poly_dates)),
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

    out = pd.DataFrame(rows)
    if not out.empty:
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

    auto_multi_sport = st.checkbox("Auto-scan multi-sport", value=True)

    sports, sports_err = fetch_sports(odds_api_key) if odds_api_key else ([], "")

    available_sport_keys = []
    if sports:
        active = [s for s in sports if s.get("active", True)]
        available_sport_keys = [str(s.get("key")) for s in active if s.get("key")]
    else:
        available_sport_keys = DEFAULT_AUTO_SPORTS
        if sports_err:
            st.caption(f"Sports API: {sports_err}")

    default_auto = [s for s in DEFAULT_AUTO_SPORTS if s in available_sport_keys]
    if not default_auto:
        default_auto = available_sport_keys[:8]

    if auto_multi_sport:
        selected_sports = st.multiselect(
            "Sport da scansionare automaticamente",
            available_sport_keys,
            default=default_auto[:8],
            help="Puoi lasciarlo automatico. Meno sport = piu' veloce; piu' sport = piu' copertura."
        )
    else:
        selected_sport = st.selectbox("Sport The Odds API", available_sport_keys, index=0)
        selected_sports = [selected_sport]

    regions = st.multiselect("Bookmaker regions", ["us", "uk", "eu", "au"], default=["us", "uk", "eu"])
    core_markets_selected = st.multiselect(
        "Mercati core bookmaker",
        CORE_MARKETS,
        default=["h2h", "totals"],
        help="h2h = risultato, totals = over/under, spreads = handicap/spread"
    )

    use_extra_markets = st.checkbox("Prova mercati extra: goal/no goal, scorer, player props", value=True)
    extra_markets_selected = st.multiselect(
        "Mercati extra",
        EXTRA_MARKETS,
        default=["btts", "player_goal_scorer_anytime", "player_goal_scorer_first"],
        disabled=not use_extra_markets,
        help="Disponibilita' variabile per sport/bookmaker/piano API. Se non supportati, vengono saltati."
    ) if use_extra_markets else []

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
    elif not regions or not core_markets_selected:
        st.error("Seleziona almeno una regione e almeno un mercato core.")
    elif not selected_sports:
        st.error("Seleziona almeno uno sport.")
    else:
        if st.button("Avvia scanner", type="primary"):
            with st.spinner("Scarico dati..."):
                poly_markets, poly_err = fetch_polymarket_markets(poly_download, theme)
                odds_events, quota_rows, odds_errors = fetch_odds_multi_sport(
                    odds_api_key,
                    selected_sports,
                    ",".join(regions),
                    core_markets_selected,
                    extra_markets_selected,
                    "decimal",
                )
                quota = quota_rows[-1] if quota_rows else {}
                odds_err = ""

            if poly_err:
                st.warning(f"Polymarket warning: {poly_err}")
            if odds_errors:
                with st.expander("The Odds API: mercati/sport saltati"):
                    st.write("Alcuni mercati extra o sport possono non essere disponibili con il piano/API corrente.")
                    st.write(odds_errors[:50])

            poly_filtered = [m for m in poly_markets if is_relevant_poly_market(m, theme)]
            poly_df = candidate_poly_questions(poly_filtered).head(top_poly)
            odds_df = flatten_odds(odds_events)
            odds_best = best_odds_for_event(odds_df)

            st.info(
                f"Dataset: {len(poly_df)} Polymarket analizzati su {len(poly_markets)} scaricati; "
                f"{len(odds_events)} eventi odds da {len(selected_sports)} sport; {len(odds_df)} quote bookmaker."
            )

            if quota_rows:
                last_quota = quota_rows[-1]
                st.caption(
                    f"Odds API quota ultima richiesta: remaining={last_quota.get('requests_remaining')} "
                    f"used={last_quota.get('requests_used')} last={last_quota.get('requests_last')}"
                )

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
                        "confidence", "similarity", "poly_market_type", "polymarket_question", "odds_event", "poly_dates", "event_date", "event_start",
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
                            "suggested_action", "confidence", "poly_market_type", "edge_vs_poly_ask_%", "estimated_ev_$",
                            "capital", "poly_ask", "poly_bid", "fair_probability_%",
                            "polymarket_question", "odds_event", "poly_dates", "event_date", "event_start",
                            "odds_market", "odds_outcome", "best_bookmaker", "best_odds",
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
        odds_events, quota_rows, odds_errors = fetch_odds_multi_sport(
            odds_api_key,
            selected_sports,
            ",".join(regions),
            core_markets_selected,
            extra_markets_selected,
            "decimal",
        )
        quota = quota_rows[-1] if quota_rows else {}
        odds_err = ""

        if odds_errors:
            with st.expander("The Odds API: mercati/sport saltati"):
                st.write(odds_errors[:50])
        if True:
            odds_df = flatten_odds(odds_events)
            odds_best = best_odds_for_event(odds_df)

            st.caption(f"{len(odds_events)} eventi; {len(odds_df)} quote; {len(odds_best)} best prices.")
            if quota:
                st.caption(f"Quota: remaining={quota.get('requests_remaining')} used={quota.get('requests_used')} last={quota.get('requests_last')}")

            if not odds_best.empty:
                show_cols = ["event", "event_date", "start", "market_key", "outcome", "point", "best_bookmaker", "best_odds", "books_compared"]
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

Da v1.4 non devi scegliere uno sport singolo: puoi usare Auto-scan multi-sport. L'app prova più sport e più mercati, inclusi risultato partita, over/under, spread, goal/no goal e scorer/player props quando disponibili.

Il segnale principale è:

```text
edge = fair_probability_bookmaker_no_vig - polymarket_yes_ask
```

Importante: questa versione confronta solo mercati Polymarket che sembrano riferiti a singole partite/eventi.
Mercati stagionali/outright come Championship, Super Bowl, division winner, MVP, World Cup winner vengono esclusi perché non sono confrontabili con quote h2h di una singola partita.

Inoltre, se Polymarket contiene una data nel formato `YYYY-MM-DD`, l'evento bookmaker deve avere esattamente la stessa data. Questo evita confronti tipo "Portugal win on 2026-06-27" contro una partita del 2026-06-23.

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
