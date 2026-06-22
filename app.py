import os
import re
import json
import time
from decimal import Decimal
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests
import streamlit as st


# ============================================================
# PolyEdge Scanner
# Polymarket + bookmaker odds benchmark via The Odds API
# ============================================================

APP_VERSION = "PolyEdge Scanner v2.1 - quota saver does not alter results"
POLY_GAMMA = "https://gamma-api.polymarket.com"
POLY_CLOB = "https://clob.polymarket.com"
ODDS_API_BASE = "https://api.the-odds-api.com/v4"
ODDS_API_IO_BASE = "https://api.odds-api.io/v3"

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


def is_quota_expensive_or_outright_sport_key(sport_key: str) -> bool:
    k = str(sport_key or "").lower()
    bad_parts = [
        "_winner",
        "championship_winner",
        "super_bowl_winner",
        "world_series_winner",
        "world_cup_winner",
        "politics_",
        "golf_",
    ]
    return any(x in k for x in bad_parts)


def quota_remaining_low(quota_rows: List[Dict[str, Any]], threshold: int = 5) -> bool:
    if not quota_rows:
        return False
    last = quota_rows[-1].get("requests_remaining")
    try:
        return int(last) <= threshold
    except Exception:
        return False


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


def iso_date_et_from_event_time(iso_time: Any) -> str:
    """
    The Odds API usa timestamp UTC.
    Molti mercati Polymarket sportivi, specialmente World Cup/NFL/MLB, usano la data locale USA.
    Esempio: 2026-06-20 00:30 UTC = 2026-06-19 sera in US/Eastern.
    """
    try:
        dt = datetime.fromisoformat(str(iso_time).replace("Z", "+00:00"))
        return dt.astimezone(ZoneInfo("America/New_York")).date().isoformat()
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


def has_period_or_half_scope(question: str) -> bool:
    """
    Blocca mercati parziali quando The Odds API sta fornendo mercati full-game.
    Esempio da bloccare:
    - Both Teams to Score in Second Half
    se il bookmaker market e' solo btts full match.
    """
    q = canonical(question)
    period_terms = [
        "first half",
        "second half",
        "1st half",
        "2nd half",
        "first period",
        "second period",
        "third period",
        "1st period",
        "2nd period",
        "3rd period",
        "first quarter",
        "second quarter",
        "third quarter",
        "fourth quarter",
        "1st quarter",
        "2nd quarter",
        "3rd quarter",
        "4th quarter",
        "first inning",
        "second inning",
        "third inning",
        "first set",
        "second set",
        "third set",
        "in the second half",
        "in second half",
        "in the first half",
        "in first half",
    ]
    return any(t in q for t in period_terms)


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

    # In questa versione usiamo quote bookmaker full-game.
    # Quindi escludiamo championship/outright/stagionali e mercati parziali half/quarter/period.
    if is_outright_or_season_market(question):
        return False

    if has_period_or_half_scope(question):
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
def fetch_sports_odds_api_io(api_key: str) -> Tuple[List[str], str]:
    try:
        params = {"apiKey": api_key} if api_key else {}
        r = SESSION.get(f"{ODDS_API_IO_BASE}/sports", params=params, timeout=20)
        if r.status_code != 200:
            return [], f"HTTP {r.status_code}: {r.text[:300]}"
        data = r.json()
        raw = data
        if isinstance(data, dict):
            raw = data.get("sports") or data.get("data") or data.get("items") or []
        keys = []
        if isinstance(raw, list):
            for x in raw:
                if isinstance(x, str):
                    keys.append(x)
                elif isinstance(x, dict):
                    keys.append(str(x.get("key") or x.get("id") or x.get("slug") or x.get("name") or ""))
        return sorted([x for x in keys if x]), ""
    except Exception as e:
        return [], str(e)


@st.cache_data(ttl=120, show_spinner=False)
def fetch_events_odds_api_io(api_key: str, sport: str, limit: int = 5) -> Tuple[List[Dict[str, Any]], str]:
    if not api_key:
        return [], "Missing ODDS_API_IO_KEY"
    try:
        r = SESSION.get(
            f"{ODDS_API_IO_BASE}/events",
            params={"apiKey": api_key, "sport": sport, "limit": limit},
            timeout=25,
        )
        if r.status_code != 200:
            return [], f"HTTP {r.status_code}: {r.text[:300]}"
        data = r.json()
        raw = data if isinstance(data, list) else data.get("events") or data.get("data") or data.get("items") or []
        if not isinstance(raw, list):
            return [], "Unexpected events response"
        return [x for x in raw if isinstance(x, dict)], ""
    except Exception as e:
        return [], str(e)


@st.cache_data(ttl=90, show_spinner=False)
def fetch_event_odds_odds_api_io(api_key: str, event_id: str) -> Tuple[Any, str]:
    if not api_key:
        return None, "Missing ODDS_API_IO_KEY"
    try:
        r = SESSION.get(
            f"{ODDS_API_IO_BASE}/odds",
            params={"apiKey": api_key, "eventId": event_id},
            timeout=25,
        )
        if r.status_code != 200:
            return None, f"HTTP {r.status_code}: {r.text[:300]}"
        return r.json(), ""
    except Exception as e:
        return None, str(e)


def odds_api_io_event_label(ev: Dict[str, Any]) -> Tuple[str, str, str, str, str]:
    event_id = str(ev.get("id") or ev.get("eventId") or ev.get("event_id") or "")
    home = str(ev.get("home") or ev.get("homeTeam") or ev.get("home_team") or "")
    away = str(ev.get("away") or ev.get("awayTeam") or ev.get("away_team") or "")
    participants = ev.get("participants") or ev.get("teams") or []
    if (not home or not away) and isinstance(participants, list) and len(participants) >= 2:
        a, b = participants[0], participants[1]
        away = away or (str(a.get("name") or a.get("team") or "") if isinstance(a, dict) else str(a))
        home = home or (str(b.get("name") or b.get("team") or "") if isinstance(b, dict) else str(b))
    title = str(ev.get("name") or ev.get("title") or ev.get("eventName") or "")
    if not title and home and away:
        title = f"{away} @ {home}"
    start = str(ev.get("startTime") or ev.get("commence_time") or ev.get("start_time") or ev.get("date") or ev.get("start") or "")
    return event_id, title, home, away, start


def extract_decimal_odds_value(obj: Dict[str, Any]) -> Optional[Decimal]:
    for k in ["odds", "price", "decimalOdds", "decimal_odds", "value"]:
        if k in obj:
            v = dec(obj.get(k), "0")
            if v > 1:
                return v
    for k in ["americanOdds", "american_odds"]:
        if k in obj:
            a = dec(obj.get(k), "0")
            if a > 0:
                return Decimal("1") + (a / Decimal("100"))
            if a < 0:
                return Decimal("1") + (Decimal("100") / abs(a))
    return None


def flatten_odds_api_io_response(events: List[Dict[str, Any]], odds_payloads: Dict[str, Any], sport_key: str) -> pd.DataFrame:
    rows = []
    event_meta = {}
    for ev in events:
        event_id, title, home, away, start = odds_api_io_event_label(ev)
        if event_id:
            event_meta[event_id] = {
                "event": title,
                "home_team": home,
                "away_team": away,
                "commence_time": start,
                "event_date": iso_date_from_event_time(start),
                "event_date_et": iso_date_et_from_event_time(start),
                "start": short_time(start),
            }

    def normalize_market(m):
        mn = canonical(m)
        if "moneyline" in mn or "match winner" in mn or mn in {"h2h", "ml"}:
            return "h2h"
        if "total" in mn or "over under" in mn or "over/under" in mn:
            return "totals"
        if "both teams to score" in mn or "btts" in mn:
            return "btts"
        return str(m or "h2h")

    def walk(obj, ctx):
        if isinstance(obj, dict):
            c = dict(ctx)
            bookmaker = obj.get("bookmaker") or obj.get("bookmakerName") or obj.get("sportsbook") or obj.get("bookmakerTitle")
            if bookmaker:
                c["bookmaker"] = str(bookmaker)
            market = obj.get("market") or obj.get("marketName") or obj.get("marketType") or obj.get("market_key")
            if market:
                c["market_key"] = normalize_market(market)
            outcome = obj.get("outcome") or obj.get("selection") or obj.get("label") or obj.get("participantName") or obj.get("name")
            if outcome:
                c["outcome"] = str(outcome)
            if obj.get("point") is not None or obj.get("line") is not None or obj.get("handicap") is not None:
                c["point"] = obj.get("point") if obj.get("point") is not None else (obj.get("line") if obj.get("line") is not None else obj.get("handicap"))

            odds = extract_decimal_odds_value(obj)
            if odds and c.get("bookmaker") and c.get("outcome"):
                meta = event_meta.get(c.get("event_id"), {})
                implied = implied_prob_from_decimal_odds(odds)
                rows.append({
                    "event_id": c.get("event_id"),
                    "sport_key": sport_key,
                    "sport_title": sport_key,
                    "event": meta.get("event", c.get("event_id")),
                    "home_team": meta.get("home_team", ""),
                    "away_team": meta.get("away_team", ""),
                    "commence_time": meta.get("commence_time", ""),
                    "event_date": meta.get("event_date", ""),
                    "event_date_et": meta.get("event_date_et", ""),
                    "start": meta.get("start", ""),
                    "bookmaker": c.get("bookmaker"),
                    "bookmaker_key": c.get("bookmaker"),
                    "last_update": "",
                    "market_key": c.get("market_key", "h2h"),
                    "market": c.get("market_key", "h2h"),
                    "outcome": c.get("outcome"),
                    "point": c.get("point"),
                    "decimal_odds": float(odds),
                    "implied_probability": float(implied or Decimal("0")),
                    "provider": "Odds-API.io",
                })
            for v in obj.values():
                walk(v, c)
        elif isinstance(obj, list):
            for x in obj:
                walk(x, ctx)

    for event_id, payload in odds_payloads.items():
        walk(payload, {"event_id": event_id})

    return pd.DataFrame(rows)


def fetch_odds_api_io_multi_sport(api_key: str, sport_keys: List[str], max_events_per_sport: int) -> Tuple[pd.DataFrame, List[str]]:
    all_events = []
    payloads = {}
    errors = []
    by_sport = {}
    for sport in sport_keys:
        events, err = fetch_events_odds_api_io(api_key, sport, max_events_per_sport)
        if err:
            errors.append(f"{sport} events: {err[:160]}")
            continue
        by_sport[sport] = events
        for ev in events[:max_events_per_sport]:
            event_id, _, _, _, _ = odds_api_io_event_label(ev)
            if not event_id:
                continue
            payload, err = fetch_event_odds_odds_api_io(api_key, event_id)
            if err:
                errors.append(f"{sport} event {event_id} odds: {err[:160]}")
                continue
            payloads[event_id] = payload

    dfs = [flatten_odds_api_io_response(evs, payloads, sport) for sport, evs in by_sport.items()]
    dfs = [d for d in dfs if not d.empty]
    return (pd.concat(dfs, ignore_index=True) if dfs else pd.DataFrame()), errors


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


@st.cache_data(ttl=90, show_spinner=False)
def fetch_event_odds(
    api_key: str,
    sport_key: str,
    event_id: str,
    regions: str,
    markets: str,
    odds_format: str,
) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any], str]:
    if not api_key:
        return None, {}, "Missing API key"

    params = {
        "apiKey": api_key,
        "regions": regions,
        "markets": markets,
        "oddsFormat": odds_format,
        "dateFormat": "iso",
    }

    try:
        r = SESSION.get(
            f"{ODDS_API_BASE}/sports/{sport_key}/events/{event_id}/odds",
            params=params,
            timeout=30,
        )

        quota = {
            "requests_remaining": r.headers.get("x-requests-remaining"),
            "requests_used": r.headers.get("x-requests-used"),
            "requests_last": r.headers.get("x-requests-last"),
        }

        if r.status_code != 200:
            return None, quota, f"HTTP {r.status_code}: {r.text[:300]}"

        data = r.json()
        if not isinstance(data, dict):
            return None, quota, "Unexpected event odds response"

        return data, quota, ""

    except Exception as e:
        return None, {}, str(e)


def fetch_odds_multi_sport(
    api_key: str,
    sport_keys: List[str],
    regions: str,
    core_markets: List[str],
    extra_markets: List[str],
    odds_format: str = "decimal",
    max_extra_events_per_sport: int = 4,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[str]]:
    """
    Scarica quote per piu' sport senza bruciare crediti inutilmente.
    Core markets: endpoint generale /odds.
    Extra markets/player props: endpoint evento, limitato.
    Stop immediato se The Odds API segnala OUT_OF_USAGE_CREDITS.
    """
    all_events = []
    quota_rows = []
    errors = []

    for sport_key in sport_keys:
        sport_key = str(sport_key).strip()
        if not sport_key:
            continue

        if quota_saver_mode and quota_remaining_low(quota_rows, threshold=3):
            errors.append("Stop automatico: crediti The Odds API quasi finiti.")
            break

        core_events = []

        if core_markets:
            events, quota, err = fetch_odds(
                api_key,
                sport_key,
                regions,
                ",".join(core_markets),
                odds_format,
            )

            if quota:
                quota_rows.append({"sport": sport_key, "market": "core", **quota})

            if err:
                if "OUT_OF_USAGE_CREDITS" in err:
                    errors.append("OUT_OF_USAGE_CREDITS: crediti The Odds API esauriti. Fermato lo scanner per non fare richieste inutili.")
                    break
                if "INVALID_MARKET_COMBO" not in err:
                    errors.append(f"{sport_key} core: {err[:160]}")
            else:
                core_events = events
                all_events.extend(events)

        if extra_markets and core_events and (not quota_saver_mode or not quota_remaining_low(quota_rows, threshold=10)):
            for ev in core_events[:max_extra_events_per_sport]:
                if quota_saver_mode and quota_remaining_low(quota_rows, threshold=3):
                    errors.append("Stop extra markets: crediti The Odds API quasi finiti.")
                    break

                event_id = ev.get("id")
                if not event_id:
                    continue

                event_extra, quota, err = fetch_event_odds(
                    api_key,
                    sport_key,
                    str(event_id),
                    regions,
                    ",".join(extra_markets),
                    odds_format,
                )

                if quota:
                    quota_rows.append({"sport": sport_key, "market": "extra_event", **quota})

                if err:
                    if "OUT_OF_USAGE_CREDITS" in err:
                        errors.append("OUT_OF_USAGE_CREDITS durante extra markets. Fermato lo scanner.")
                        break
                    if "INVALID_MARKET" not in err and "not supported" not in err.lower() and "INVALID_MARKET_COMBO" not in err:
                        errors.append(f"{sport_key} event {event_id} extra: {err[:160]}")
                elif event_extra:
                    all_events.append(event_extra)

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
                        "event_date_et": iso_date_et_from_event_time(commence),
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

    group_cols = ["event_id", "event", "home_team", "away_team", "event_date", "event_date_et", "start", "market_key", "outcome", "point"]
    for keys, g in df.groupby(group_cols, dropna=False):
        event_id, event, home, away, event_date, event_date_et, start, market_key, outcome, point = keys

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
            "event_date_et": event_date_et,
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

    odds_by_event = odds_best_df.groupby(["event_id", "event", "home_team", "away_team", "event_date", "event_date_et", "start"], dropna=False)

    for _, p in poly_df.head(max_candidates).iterrows():
        q = p["question"]
        q_match_text = p.get("match_text") or q
        poly_dates = set(str(p.get("poly_dates") or "").split(",")) if str(p.get("poly_dates") or "") else set()
        p_type = p["market_type"]

        if p_type in {"unknown", "outright"}:
            continue

        # The Odds API core markets here are full-game.
        # Do not compare partial Polymarket markets such as second-half BTTS to full-match BTTS.
        if has_period_or_half_scope(q_match_text):
            continue

        if not looks_like_single_game_market(q_match_text):
            continue

        possible_markets = odds_market_candidates_for_poly_type(p_type)
        if not possible_markets:
            continue

        for (event_id, event, home, away, event_date, event_date_et, start), eg in odds_by_event:
            event_text = f"{event} {home} {away}"
            sim = text_similarity(q_match_text, event_text)

            # Date must match if Polymarket contains ISO date.
            # Accept UTC date OR US/Eastern date because many sports markets are labelled by local date.
            if poly_dates and event_date not in poly_dates and event_date_et not in poly_dates:
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

                bookmaker_best_outcomes = build_best_outcomes_for_group(group_for_probs)

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
                    "event_date_et": event_date_et,
                    "poly_dates": ",".join(sorted(poly_dates)),
                    "event_start": start,
                    "odds_market": target_market,
                    "odds_outcome": best["outcome"],
                    "odds_point": best["point"],
                    "best_bookmaker": best["best_bookmaker"],
                    "best_odds": best["best_odds"],
                    "books_compared": best["books_compared"],
                    "fair_probability": float(fair_prob),
                    "bookmaker_best_outcomes": json.dumps(bookmaker_best_outcomes, ensure_ascii=False),
                    "event_id": event_id,
                })

    out = pd.DataFrame(rows)
    if not out.empty:
        conf_order = {"Alta": 0, "Media": 1, "Bassa": 2}
        out["_conf_order"] = out["confidence"].map(conf_order).fillna(9)
        out = out.sort_values(["_conf_order", "similarity", "poly_volume"], ascending=[True, False, False]).drop(columns=["_conf_order"])
    return out



def build_best_outcomes_for_group(group_for_probs: pd.DataFrame) -> List[Dict[str, Any]]:
    """
    Ritorna la migliore quota bookmaker per ogni outcome dello stesso evento/mercato/linea.
    Serve per costruire hedge Polymarket YES + bookmaker NOT-YES.
    """
    bests = []
    if group_for_probs is None or group_for_probs.empty:
        return bests

    for outcome, gg in group_for_probs.groupby("outcome", dropna=False):
        if gg.empty:
            continue

        idx = gg["best_odds"].idxmax()
        best = gg.loc[idx]
        odds = dec(best.get("best_odds"))
        if odds <= 1:
            continue

        bests.append({
            "outcome": str(best.get("outcome") or outcome),
            "point": "" if pd.isna(best.get("point")) else str(best.get("point")),
            "bookmaker": str(best.get("best_bookmaker") or ""),
            "odds": float(odds),
        })

    return bests


def best_outcomes_json_to_list(x: Any) -> List[Dict[str, Any]]:
    try:
        data = json.loads(str(x or "[]"))
        if isinstance(data, list):
            return data
        return []
    except Exception:
        return []


def split_target_and_not_target(best_outcomes: List[Dict[str, Any]], target_outcome: str) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    target = None
    others = []

    target_norm = canonical(target_outcome)

    for x in best_outcomes:
        out_norm = canonical(x.get("outcome") or "")
        if out_norm == target_norm and target is None:
            target = x
        else:
            others.append(x)

    return target, others


def hedge_plan_bookmaker_yes_poly_no(
    target: Dict[str, Any],
    poly_no_ask: Decimal,
    capital: Decimal,
) -> Dict[str, Any]:
    """
    Strategia:
    - Bookmaker: compra/bet YES sull'outcome del mercato Polymarket
    - Polymarket: compra NO

    Costo normalizzato per payout 1:
        1 / bookmaker_yes_odds + poly_no_ask

    Se costo < 1 = arbitraggio teorico.
    """
    odds = dec(target.get("odds"))
    if odds <= 1 or poly_no_ask <= 0:
        return {}

    book_cost_unit = Decimal("1") / odds
    combo_cost = book_cost_unit + poly_no_ask
    roi = (Decimal("1") / combo_cost) - Decimal("1") if combo_cost > 0 else None

    guaranteed_payout = capital / combo_cost if combo_cost > 0 else Decimal("0")
    bookmaker_stake = guaranteed_payout / odds
    polymarket_no_cost = guaranteed_payout * poly_no_ask
    profit = guaranteed_payout - capital

    return {
        "strategy": "HEDGE: Bookmaker YES + Polymarket NO",
        "combo_cost": combo_cost,
        "roi": roi,
        "profit": profit,
        "poly_leg": f"Compra NO Polymarket @ {fmt_price(poly_no_ask)}",
        "book_leg": f"Punta YES bookmaker: {target.get('outcome')} @ {float(odds):.3f} ({target.get('bookmaker')})",
        "stake_plan": json.dumps([
            {
                "site": "Bookmaker",
                "bookmaker": target.get("bookmaker"),
                "outcome": target.get("outcome"),
                "odds": float(odds),
                "stake_$": fmt_money(bookmaker_stake),
                "payout_if_wins_$": fmt_money(bookmaker_stake * odds),
            },
            {
                "site": "Polymarket",
                "outcome": "NO",
                "price": float(poly_no_ask),
                "cost_$": fmt_money(polymarket_no_cost),
                "payout_if_wins_$": fmt_money(guaranteed_payout),
            },
        ], ensure_ascii=False),
    }


def hedge_plan_poly_yes_bookmaker_not(
    target: Dict[str, Any],
    others: List[Dict[str, Any]],
    poly_yes_ask: Decimal,
    capital: Decimal,
) -> Dict[str, Any]:
    """
    Strategia:
    - Polymarket: compra YES
    - Bookmaker: copri NOT-YES puntando tutti gli altri outcome disponibili.

    Per h2h soccer a 3 esiti:
        Polymarket YES = Team A vince
        Bookmaker NOT-YES = Draw + Team B
    """
    if poly_yes_ask <= 0 or not others:
        return {}

    other_cost_unit = Decimal("0")
    clean_others = []

    for x in others:
        odds = dec(x.get("odds"))
        if odds <= 1:
            continue
        other_cost_unit += Decimal("1") / odds
        clean_others.append({**x, "odds_dec": odds})

    if not clean_others:
        return {}

    combo_cost = poly_yes_ask + other_cost_unit
    roi = (Decimal("1") / combo_cost) - Decimal("1") if combo_cost > 0 else None

    guaranteed_payout = capital / combo_cost if combo_cost > 0 else Decimal("0")
    polymarket_yes_cost = guaranteed_payout * poly_yes_ask
    profit = guaranteed_payout - capital

    stake_rows = [
        {
            "site": "Polymarket",
            "outcome": "YES",
            "price": float(poly_yes_ask),
            "cost_$": fmt_money(polymarket_yes_cost),
            "payout_if_wins_$": fmt_money(guaranteed_payout),
        }
    ]

    book_leg_parts = []
    for x in clean_others:
        odds = x["odds_dec"]
        stake = guaranteed_payout / odds
        book_leg_parts.append(f"{x.get('outcome')} @ {float(odds):.3f} ({x.get('bookmaker')})")
        stake_rows.append({
            "site": "Bookmaker",
            "bookmaker": x.get("bookmaker"),
            "outcome": x.get("outcome"),
            "odds": float(odds),
            "stake_$": fmt_money(stake),
            "payout_if_wins_$": fmt_money(stake * odds),
        })

    return {
        "strategy": "HEDGE: Polymarket YES + Bookmaker NOT-YES",
        "combo_cost": combo_cost,
        "roi": roi,
        "profit": profit,
        "poly_leg": f"Compra YES Polymarket @ {fmt_price(poly_yes_ask)}",
        "book_leg": "Punta NOT-YES bookmaker: " + " + ".join(book_leg_parts),
        "stake_plan": json.dumps(stake_rows, ensure_ascii=False),
    }


def pick_best_operational_strategy(
    simple_action: str,
    simple_edge: Optional[Decimal],
    simple_ev: Optional[Decimal],
    poly_yes_ask: Optional[Decimal],
    poly_yes_bid: Optional[Decimal],
    target_outcome: str,
    best_outcomes_json: Any,
    capital: Decimal,
    min_edge: Decimal,
) -> Dict[str, Any]:
    """
    Sceglie la migliore azione operativa tra:
    - solo BUY YES Polymarket
    - solo BUY NO Polymarket
    - hedge bookmaker YES + Polymarket NO
    - hedge Polymarket YES + bookmaker NOT-YES
    """
    strategies = []

    if poly_yes_ask is not None and poly_yes_ask > 0 and simple_edge is not None:
        strategies.append({
            "strategy": simple_action,
            "roi": simple_edge / poly_yes_ask if poly_yes_ask > 0 else None,
            "profit": simple_ev,
            "combo_cost": None,
            "poly_leg": f"Compra YES Polymarket @ {fmt_price(poly_yes_ask)}" if simple_action == "BUY YES Polymarket" else "",
            "book_leg": "",
            "stake_plan": "",
        })

    # BUY NO Polymarket semplice, quando YES e' caro rispetto al fair bookmaker.
    # fair_no = 1 - fair_yes
    # no_ask approx = 1 - yes_bid
    best_outcomes = best_outcomes_json_to_list(best_outcomes_json)
    target, others = split_target_and_not_target(best_outcomes, target_outcome)

    if poly_yes_bid is not None and poly_yes_bid > 0:
        poly_no_ask = Decimal("1") - poly_yes_bid

        # Hedging: bookmaker target YES + Polymarket NO
        if target:
            plan = hedge_plan_bookmaker_yes_poly_no(target, poly_no_ask, capital)
            if plan and plan.get("roi") is not None:
                strategies.append(plan)

        # Hedging: Polymarket YES + bookmaker NOT-YES
        if poly_yes_ask is not None and poly_yes_ask > 0 and others:
            plan = hedge_plan_poly_yes_bookmaker_not(target or {}, others, poly_yes_ask, capital)
            if plan and plan.get("roi") is not None:
                strategies.append(plan)

    valid_strategies = []
    for s in strategies:
        roi = s.get("roi")
        if roi is None:
            continue

        # Keep positive hedge/arbitrage. For simple value, require min_edge threshold handled outside.
        if roi > Decimal("0"):
            valid_strategies.append(s)

    if not valid_strategies:
        return {
            "strategy": "NO TRADE",
            "roi": None,
            "profit": None,
            "combo_cost": None,
            "poly_leg": "",
            "book_leg": "",
            "stake_plan": "",
        }

    best = sorted(valid_strategies, key=lambda x: x.get("roi") or Decimal("-999"), reverse=True)[0]
    return best


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
            book_status = book_status if book_status != "not read" else "missing price"

        fair_yes = dec(row.get("fair_probability"), "0")
        ask = poly_ask
        bid = poly_bid

        edge_yes = None
        ev_yes = None
        simple_action = "NO TRADE"

        if ask is not None and ask > 0 and fair_yes > 0:
            edge_yes = fair_yes - ask
            ev_yes = (capital / ask) * edge_yes if ask > 0 else None

            if edge_yes >= min_edge:
                simple_action = "BUY YES Polymarket"
            elif edge_yes <= -min_edge:
                simple_action = "BUY NO Polymarket candidate"
            else:
                simple_action = "NO TRADE"

        chosen = pick_best_operational_strategy(
            simple_action=simple_action,
            simple_edge=edge_yes,
            simple_ev=ev_yes,
            poly_yes_ask=ask,
            poly_yes_bid=bid,
            target_outcome=str(row.get("odds_outcome") or ""),
            best_outcomes_json=row.get("bookmaker_best_outcomes"),
            capital=capital,
            min_edge=min_edge,
        )

        combo_cost = chosen.get("combo_cost")
        combo_roi = chosen.get("roi")
        combo_profit = chosen.get("profit")

        # Add explicit opposite approximations.
        poly_no_ask = None
        if bid is not None:
            poly_no_ask = Decimal("1") - bid

        operational_action = chosen.get("strategy") or "NO TRADE"

        rows.append({
            **row.to_dict(),

            # Original Polymarket value columns.
            "poly_bid": fmt_price(poly_bid),
            "poly_ask": fmt_price(poly_ask),
            "poly_no_ask_stimato": fmt_price(poly_no_ask),
            "book_status": book_status,
            "fair_probability_%": fmt_pct(fair_yes),
            "edge_vs_poly_ask": float(edge_yes) if edge_yes is not None else None,
            "edge_vs_poly_ask_%": fmt_pct(edge_yes),
            "capital": fmt_money(capital),
            "estimated_ev_$": fmt_money(ev_yes),
            "suggested_action": simple_action,

            # New explicit operational columns.
            "azione_operativa": operational_action,
            "compra_polymarket": chosen.get("poly_leg") or "",
            "compra_bookmaker": chosen.get("book_leg") or "",
            "costo_combo": fmt_price(combo_cost) if combo_cost is not None else "",
            "roi_combo_%": fmt_pct(combo_roi) if combo_roi is not None else "",
            "profitto_teorico_$": fmt_money(combo_profit) if combo_profit is not None else "",
            "stake_plan": chosen.get("stake_plan") or "",
        })

        pct = i / total
        progress.progress(pct, text=f"Valuto candidati {i}/{total} - {pct*100:.1f}%")
        status.caption(f"Candidati valutati: {i}/{total}")

    progress.empty()
    status.empty()

    out = pd.DataFrame(rows)
    if not out.empty:
        # Put real hedge/arbitrage candidates first.
        out["_roi_sort"] = out["roi_combo_%"].apply(lambda x: float(str(x).replace("%", "")) if str(x).strip().endswith("%") else -999)
        out = out.sort_values(["azione_operativa", "_roi_sort", "edge_vs_poly_ask", "similarity"], ascending=[True, False, False, False], na_position="last")
        out = out.drop(columns=["_roi_sort"])
    return out


# ============================================================
# Bookmaker vs Bookmaker arbitrage
# ============================================================

def bookmaker_outcome_group_key(row: pd.Series) -> Optional[Tuple[str, str, str, str, str, str]]:
    """
    Group per arbitraggio bookmaker.
    V1.6: gestiamo in modo sicuro h2h, totals e btts.
    Spread/player props sono esclusi dall'arbitraggio puro perché richiedono normalizzazione più delicata.
    """
    market_key = str(row.get("market_key") or "")

    if market_key not in {"h2h", "totals", "btts"}:
        return None

    point = row.get("point")
    if pd.isna(point) or point is None:
        point_key = ""
    else:
        point_key = str(point)

    return (
        str(row.get("event_id")),
        str(row.get("event")),
        str(row.get("event_date")),
        str(row.get("start")),
        market_key,
        point_key,
    )


def find_bookmaker_arbitrage(odds_df: pd.DataFrame, capital: Decimal, min_roi: Decimal) -> pd.DataFrame:
    """
    Cerca arbitraggio puro tra bookmaker.
    Formula:
        sum(1 / best_odds_per_outcome) < 1
    """
    if odds_df.empty:
        return pd.DataFrame()

    rows = []
    tmp = odds_df.copy()
    tmp["_arb_group"] = tmp.apply(bookmaker_outcome_group_key, axis=1)
    tmp = tmp[tmp["_arb_group"].notna()].copy()

    if tmp.empty:
        return pd.DataFrame()

    for group_key, g in tmp.groupby("_arb_group", dropna=False):
        event_id, event, event_date, start, market_key, point_key = group_key

        # At least 2 possible outcomes required.
        outcomes = sorted([x for x in g["outcome"].dropna().unique().tolist() if str(x).strip()])
        if len(outcomes) < 2:
            continue

        # For totals/btts we require exactly the natural opposite sides when possible.
        if market_key == "totals":
            norm_outcomes = set(canonical(x) for x in outcomes)
            if not {"over", "under"}.issubset(norm_outcomes):
                continue

        if market_key == "btts":
            norm_outcomes = set(canonical(x) for x in outcomes)
            if not {"yes", "no"}.issubset(norm_outcomes):
                continue

        bests = []
        for outcome in outcomes:
            gg = g[g["outcome"] == outcome].copy()
            if gg.empty:
                continue

            idx = gg["decimal_odds"].idxmax()
            best = gg.loc[idx]
            odds = dec(best["decimal_odds"])
            if odds <= 1:
                continue

            implied = implied_prob_from_decimal_odds(odds)
            if implied is None:
                continue

            bests.append({
                "outcome": str(outcome),
                "bookmaker": str(best.get("bookmaker") or ""),
                "bookmaker_key": str(best.get("bookmaker_key") or ""),
                "odds": odds,
                "implied": implied,
            })

        if len(bests) < 2:
            continue

        inv_sum = sum([x["implied"] for x in bests], Decimal("0"))
        if inv_sum <= 0:
            continue

        roi = (Decimal("1") / inv_sum) - Decimal("1")
        if roi < min_roi:
            continue

        guaranteed_payout = capital / inv_sum
        guaranteed_profit = guaranteed_payout - capital

        stake_plan = []
        for x in bests:
            stake = guaranteed_payout / x["odds"]
            stake_plan.append({
                "outcome": x["outcome"],
                "bookmaker": x["bookmaker"],
                "odds": float(x["odds"]),
                "stake": float(stake),
                "stake_$": fmt_money(stake),
                "payout_if_wins_$": fmt_money(stake * x["odds"]),
            })

        is_true_arb = inv_sum < Decimal("1")

        rows.append({
            "is_arbitrage": is_true_arb,
            "roi": float(roi),
            "roi_%": fmt_pct(roi),
            "guaranteed_profit_$": fmt_money(guaranteed_profit),
            "capital": fmt_money(capital),
            "event": event,
            "event_date": event_date,
            "start": start,
            "market_key": market_key,
            "point": point_key,
            "implied_sum": float(inv_sum),
            "best_prices": " | ".join([f"{x['outcome']} @ {float(x['odds']):.3f} ({x['bookmaker']})" for x in bests]),
            "stake_plan": json.dumps(stake_plan, ensure_ascii=False),
            "event_id": event_id,
        })

    out = pd.DataFrame(rows)
    if not out.empty:
        out = out.sort_values(["is_arbitrage", "roi"], ascending=[False, False])
    return out


def add_bookmaker_paper_trade(row: Dict[str, Any]):
    trade = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "type": "bookmaker_arbitrage",
        "event": row.get("event"),
        "market_key": row.get("market_key"),
        "point": row.get("point"),
        "roi_%": row.get("roi_%"),
        "guaranteed_profit_$": row.get("guaranteed_profit_$"),
        "capital": row.get("capital"),
        "best_prices": row.get("best_prices"),
        "stake_plan": row.get("stake_plan"),
    }
    st.session_state.paper_trades.append(trade)


# ============================================================
# Paper trade log
# ============================================================

def init_paper_log():
    if "paper_trades" not in st.session_state:
        st.session_state.paper_trades = []


def add_paper_trade(row: Dict[str, Any], stake: Decimal):
    trade = {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "action": row.get("azione_operativa") or row.get("suggested_action"),
        "stake": float(stake),
        "polymarket_question": row.get("polymarket_question"),
        "poly_ask": row.get("poly_ask"),
        "poly_no_ask_stimato": row.get("poly_no_ask_stimato"),
        "fair_probability_%": row.get("fair_probability_%"),
        "edge_vs_poly_ask_%": row.get("edge_vs_poly_ask_%"),
        "estimated_ev_$": row.get("estimated_ev_$"),
        "compra_polymarket": row.get("compra_polymarket"),
        "compra_bookmaker": row.get("compra_bookmaker"),
        "roi_combo_%": row.get("roi_combo_%"),
        "profitto_teorico_$": row.get("profitto_teorico_$"),
        "stake_plan": row.get("stake_plan"),
        "poly_link": row.get("poly_link"),
        "odds_event": row.get("odds_event"),
        "best_bookmaker": row.get("best_bookmaker"),
        "best_odds": row.get("best_odds"),
    }
    st.session_state.paper_trades.append(trade)



def fetch_provider_flat_odds_for_ui() -> Tuple[pd.DataFrame, List[Dict[str, Any]], List[str], Dict[str, Any]]:
    if odds_provider == "The Odds API":
        odds_events, quota_rows, errors = fetch_odds_multi_sport(
            odds_api_key,
            selected_sports,
            ",".join(regions),
            core_markets_selected,
            extra_markets_selected,
            "decimal",
            max_extra_events_per_sport,
        )
        odds_df = flatten_odds(odds_events)
        quota = quota_rows[-1] if quota_rows else {}
        return odds_df, quota_rows, errors, quota

    odds_df, errors = fetch_odds_api_io_multi_sport(
        odds_api_io_key,
        selected_sports,
        max_events_per_sport_provider,
    )
    return odds_df, [], errors, {}


# ============================================================
# UI
# ============================================================

init_paper_log()

st.title("PolyEdge Scanner")
st.caption(APP_VERSION)

st.info(
    "Obiettivo: usare Polymarket come mercato tradabile, le quote bookmaker come benchmark, "
    "e confrontare anche i bookmaker tra loro per arbitraggio teorico. Questa versione NON usa Kalshi."
)

st.warning(
    "Trade automatici real-money non sono abilitati in questa app. Per sicurezza questa versione genera segnali, "
    "trade plan e paper trade. L'esecuzione live su Polymarket richiede wallet/API key e va implementata solo in ambiente privato, non con private key su Streamlit Cloud."
)

with st.sidebar:
    st.header("Input")

    odds_provider = st.selectbox(
        "Provider quote",
        ["The Odds API", "Odds-API.io"],
        index=0,
        help="The Odds API = provider storico. Odds-API.io = alternativa gratuita/sperimentale."
    )

    odds_api_key = st.text_input(
        "The Odds API key",
        value=get_secret_or_env("ODDS_API_KEY", ""),
        type="password",
        help="Per usarla in cloud, aggiungi ODDS_API_KEY nei secrets di Streamlit.",
    )

    odds_api_io_key = st.text_input(
        "Odds-API.io key",
        value=get_secret_or_env("ODDS_API_IO_KEY", ""),
        type="password",
        help="Per usarla in cloud, aggiungi ODDS_API_IO_KEY nei secrets di Streamlit.",
    )

    active_odds_key = odds_api_key if odds_provider == "The Odds API" else odds_api_io_key

    if st.button("Svuota cache / aggiorna dati"):
        st.cache_data.clear()
        st.success("Cache svuotata. Rilancia lo scanner.")

    st.divider()

    quota_saver_mode = st.checkbox(
        "Protezione crediti API",
        value=True,
        help="Non cambia i risultati. Ferma solo lo scanner quando i crediti sono quasi finiti, evitando richieste inutili."
    )

    exclude_outright_sports = st.checkbox(
        "Escludi sport winner/outright",
        value=True,
        help="Filtro esplicito. Consigliato per questa app: gli sport *_winner non sono comparabili con mercati full-game h2h/totals."
    )

    auto_multi_sport = st.checkbox("Auto-scan multi-sport", value=True)

    available_sport_keys = []
    sports_err = ""

    if odds_provider == "The Odds API":
        sports, sports_err = fetch_sports(odds_api_key) if odds_api_key else ([], "")
        if sports:
            active = [s for s in sports if s.get("active", True)]
            available_sport_keys = [str(s.get("key")) for s in active if s.get("key")]
    else:
        available_sport_keys, sports_err = fetch_sports_odds_api_io(odds_api_io_key) if odds_api_io_key else ([], "")

    if not available_sport_keys:
        available_sport_keys = DEFAULT_AUTO_SPORTS
        if sports_err:
            st.caption(f"Sports API: {sports_err}")

    if exclude_outright_sports:
        available_sport_keys = [s for s in available_sport_keys if not is_quota_expensive_or_outright_sport_key(s)]

    preferred_safe_default = [
        "soccer_fifa_world_cup",
        "soccer_epl",
        "soccer_italy_serie_a",
        "baseball_mlb",
        "americanfootball_nfl",
    ]
    default_auto = [s for s in preferred_safe_default if s in available_sport_keys]
    if not default_auto:
        default_auto = [s for s in DEFAULT_AUTO_SPORTS if s in available_sport_keys][:5]
    if not default_auto:
        default_auto = available_sport_keys[:3]

    max_sports_per_run = st.slider(
        "Avviso se sport selezionati superano",
        1,
        50,
        5,
        step=1,
        help="Solo avviso: non taglia i risultati. Serve a ricordarti che piu' sport consumano piu' crediti."
    )

    max_events_per_sport_provider = st.slider(
        "Max eventi per sport provider alternativo",
        1,
        20,
        5,
        step=1,
        help="Usato soprattutto da Odds-API.io: limita chiamate per non sprecare quota."
    )

    if auto_multi_sport:
        selected_sports_raw = st.multiselect(
            "Sport da scansionare automaticamente",
            available_sport_keys,
            default=default_auto,
            help="Seleziona gli sport che vuoi davvero scansionare. La protezione crediti NON taglia questa lista."
        )
        selected_sports = selected_sports_raw
        if len(selected_sports_raw) > max_sports_per_run:
            st.warning(f"Hai selezionato {len(selected_sports_raw)} sport. Non verranno tagliati, ma consumerai piu' crediti API.")
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

    use_extra_markets = st.checkbox(
        "Prova mercati extra: goal/no goal, scorer, player props",
        value=False,
        help="Scelta esplicita: se attivo aumenta la copertura, ma consuma piu' richieste API."
    )
    extra_markets_selected = st.multiselect(
        "Mercati extra",
        EXTRA_MARKETS,
        default=["btts", "player_goal_scorer_anytime", "player_goal_scorer_first"],
        disabled=not use_extra_markets,
        help="Disponibilita' variabile per sport/bookmaker/piano API. Se non supportati, vengono saltati."
    ) if use_extra_markets else []

    max_extra_events_per_sport = st.slider(
        "Eventi per sport da provare per mercati extra",
        0,
        20,
        0,
        step=1,
        help="Scelta esplicita: piu' eventi extra = piu' copertura e piu' consumo API."
    )

    st.divider()

    st.caption("Nota: questa versione esclude mercati stagionali/outright Polymarket. Confronta solo singole partite/eventi.")
    theme = st.text_input("Filtro Polymarket", value="", placeholder="nba, mlb, nfl, team name, over 2.5...")
    poly_download = st.slider("Polymarket da scaricare", 100, 5000, 2500, step=100)
    top_poly = st.slider("Top Polymarket da analizzare", 50, 1500, 400, step=50)
    max_candidates = st.slider("Massimo candidati matching", 20, 1000, 250, step=10)

    st.divider()

    capital = dec(st.number_input("Capitale per trade ($)", min_value=1.0, max_value=100000.0, value=100.0, step=25.0))

    estimated_fx_roundtrip_pct = st.number_input(
        "Costo cambio/prelievo stimato round-trip (%)",
        min_value=0.0,
        max_value=20.0,
        value=1.5,
        step=0.10,
        help="Per Italia/EUR: costo totale stimato EUR->USD/USDC e USD/USDC->EUR. Cerca ROI sopra questo costo."
    )
    min_edge_pct = st.number_input("Edge minimo vs Polymarket ask (%)", min_value=-50.0, max_value=50.0, value=2.0, step=0.25)
    min_edge = dec(min_edge_pct) / Decimal("100")

    min_bookmaker_arb_roi_pct = st.number_input(
        "ROI minimo bookmaker arbitrage (%)",
        min_value=-10.0,
        max_value=50.0,
        value=0.0,
        step=0.10,
        help="0 mostra solo arbitraggio puro o quasi. Usa 0.2 / 0.5 per filtrare rumore."
    )
    min_bookmaker_arb_roi = dec(min_bookmaker_arb_roi_pct) / Decimal("100")

    read_orderbooks = st.checkbox("Leggi orderbook Polymarket live", value=True)
    auto_refresh = st.checkbox("Auto-refresh 60 secondi", value=False)

    st.divider()
    st.caption("Trade automatici real-money: disabilitati in questa versione. Usa Paper Trade.")


if auto_refresh:
    time.sleep(60)
    st.rerun()


tab_scan, tab_poly, tab_odds, tab_arb, tab_paper, tab_setup = st.tabs([
    "Scanner",
    "Polymarket",
    "Bookmaker Odds",
    "Bookmaker Arbitrage",
    "Paper Trade",
    "Setup",
])


with tab_scan:
    st.subheader("Scanner Polymarket vs bookmaker benchmark")

    if quota_saver_mode:
        st.caption("Protezione crediti API attiva: non modifica i risultati, ferma solo richieste inutili se i crediti sono quasi finiti.")
    if exclude_outright_sports:
        st.caption("Filtro esplicito winner/outright attivo.")
    st.caption(f"Provider quote: {odds_provider}. Costo cambio/prelievo round-trip stimato: {estimated_fx_roundtrip_pct:.2f}%. Cerca ROI superiore a questo costo + margine sicurezza.")

    if not active_odds_key:
        st.error("Inserisci la API key del provider selezionato nella sidebar o nei secrets Streamlit.")
    elif not regions or not core_markets_selected:
        st.error("Seleziona almeno una regione e almeno un mercato core.")
    elif not selected_sports:
        st.error("Seleziona almeno uno sport.")
    else:
        if st.button("Avvia scanner", type="primary"):
            with st.spinner("Scarico dati..."):
                poly_markets, poly_err = fetch_polymarket_markets(poly_download, theme)
                odds_df, quota_rows, odds_errors, quota = fetch_provider_flat_odds_for_ui()
                odds_events = []
                odds_err = ""

            if poly_err:
                st.warning(f"Polymarket warning: {poly_err}")
            if odds_errors:
                with st.expander("The Odds API: note tecniche"):
                    st.write("Alcuni sport/mercati extra possono non essere disponibili con il piano/API corrente. Gli errori non bloccano lo scanner.")
                    st.write(odds_errors[:30])

            poly_filtered = [m for m in poly_markets if is_relevant_poly_market(m, theme)]
            poly_df = candidate_poly_questions(poly_filtered).head(top_poly)
            odds_best = best_odds_for_event(odds_df)

            st.info(
                f"Dataset: {len(poly_df)} Polymarket analizzati su {len(poly_markets)} scaricati; "
                f"provider {odds_provider}; {len(selected_sports)} sport; {len(odds_df)} quote bookmaker normalizzate."
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
                        "confidence", "similarity", "poly_market_type", "polymarket_question", "odds_event", "poly_dates", "event_date", "event_date_et", "event_start",
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
                            "azione_operativa", "roi_combo_%", "profitto_teorico_$",
                            "compra_polymarket", "compra_bookmaker", "costo_combo",
                            "confidence", "poly_market_type", "edge_vs_poly_ask_%", "estimated_ev_$",
                            "capital", "poly_ask", "poly_bid", "poly_no_ask_stimato", "fair_probability_%",
                            "polymarket_question", "odds_event", "poly_dates", "event_date", "event_date_et", "event_start",
                            "odds_market", "odds_outcome", "best_bookmaker", "best_odds",
                            "books_compared", "poly_link", "book_status", "stake_plan"
                        ]

                        st.dataframe(evaluated[main_cols].head(300), width="stretch", hide_index=True)

                        st.download_button(
                            "Scarica CSV segnali",
                            evaluated.to_csv(index=False).encode("utf-8"),
                            file_name="polyedge_signals.csv",
                            mime="text/csv",
                        )

                        action_rows = evaluated[evaluated["azione_operativa"] != "NO TRADE"].copy()
                        if not action_rows.empty:
                            st.success(f"Trovati {len(action_rows)} segnali operativi teorici.")
                            selected_idx = st.selectbox(
                                "Aggiungi un segnale al paper trade",
                                action_rows.index.tolist(),
                                format_func=lambda i: f"{action_rows.loc[i, 'azione_operativa']} | {action_rows.loc[i, 'roi_combo_%']} | {action_rows.loc[i, 'polymarket_question'][:80]}"
                            )

                            with st.expander("Stake plan / istruzioni selezionate"):
                                sel = action_rows.loc[selected_idx]
                                st.write("Polymarket:", sel.get("compra_polymarket"))
                                st.write("Bookmaker:", sel.get("compra_bookmaker"))
                                try:
                                    plan_df = pd.DataFrame(json.loads(sel.get("stake_plan") or "[]"))
                                    if not plan_df.empty:
                                        st.dataframe(plan_df, width="stretch", hide_index=True)
                                except Exception:
                                    st.write(sel.get("stake_plan"))

                            paper_stake = dec(st.number_input("Stake paper trade ($)", min_value=1.0, max_value=100000.0, value=float(capital), step=25.0))
                            if st.button("Aggiungi Paper Trade"):
                                add_paper_trade(action_rows.loc[selected_idx].to_dict(), paper_stake)
                                st.success("Paper trade aggiunto.")
                        else:
                            st.info("Nessun segnale operativo sopra soglia. Puoi abbassare edge minimo o cambiare sport/filtro.")


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

    if not active_odds_key:
        st.error("Inserisci la API key del provider selezionato.")
    elif st.button("Carica bookmaker odds", key="load_odds"):
        odds_df, quota_rows, odds_errors, quota = fetch_provider_flat_odds_for_ui()
        odds_events = []
        odds_err = ""

        if odds_errors:
            with st.expander("The Odds API: note tecniche"):
                st.write("Alcuni sport/mercati extra possono non essere disponibili con il piano/API corrente. Gli errori non bloccano lo scanner.")
                st.write(odds_errors[:30])
        if True:
            odds_best = best_odds_for_event(odds_df)

            st.caption(f"Provider {odds_provider}; {len(selected_sports)} sport; {len(odds_df)} quote; {len(odds_best)} best prices.")
            if quota:
                st.caption(f"Quota: remaining={quota.get('requests_remaining')} used={quota.get('requests_used')} last={quota.get('requests_last')}")

            if not odds_best.empty:
                show_cols = ["event", "event_date", "start", "market_key", "outcome", "point", "best_bookmaker", "best_odds", "books_compared"]
                st.dataframe(odds_best[show_cols].head(1000), width="stretch", hide_index=True)



with tab_arb:
    st.subheader("Bookmaker vs Bookmaker Arbitrage")

    st.info(
        "Questa tab confronta i bookmaker tra loro sullo stesso evento/mercato. "
        "Per ora l'arbitraggio puro viene calcolato in modo sicuro su h2h, totals e btts quando disponibili."
    )

    if not active_odds_key:
        st.error("Inserisci la API key del provider selezionato.")
    elif not regions or not core_markets_selected:
        st.error("Seleziona almeno una regione e almeno un mercato core.")
    elif not selected_sports:
        st.error("Seleziona almeno uno sport.")
    elif st.button("Avvia bookmaker arbitrage scanner", type="primary"):
        with st.spinner("Scarico quote bookmaker multi-sport..."):
            odds_events, quota_rows, odds_errors = fetch_odds_multi_sport(
                odds_api_key,
                selected_sports,
                ",".join(regions),
                core_markets_selected,
                extra_markets_selected,
                "decimal",
                max_extra_events_per_sport,
            )

        if odds_errors:
            with st.expander("The Odds API: note tecniche"):
                st.write("Alcuni sport/mercati extra possono non essere disponibili con il piano/API corrente. Gli errori non bloccano lo scanner.")
                st.write(odds_errors[:30])

        st.caption(
            f"Dataset bookmaker: provider {odds_provider}; {len(selected_sports)} sport; "
            f"{len(odds_df)} quote bookmaker normalizzate."
        )

        if quota_rows:
            last_quota = quota_rows[-1]
            st.caption(
                f"Odds API quota ultima richiesta: remaining={last_quota.get('requests_remaining')} "
                f"used={last_quota.get('requests_used')} last={last_quota.get('requests_last')}"
            )

        if odds_df.empty:
            st.warning("Nessuna quota bookmaker trovata.")
        else:
            arb_df = find_bookmaker_arbitrage(odds_df, capital, min_bookmaker_arb_roi)

            if arb_df.empty:
                st.info("Nessun arbitraggio bookmaker sopra la soglia impostata.")
            else:
                show_cols = [
                    "is_arbitrage", "roi_%", "guaranteed_profit_$", "capital",
                    "event", "event_date", "start", "market_key", "point",
                    "best_prices", "implied_sum"
                ]
                st.dataframe(arb_df[show_cols].head(300), width="stretch", hide_index=True)

                st.download_button(
                    "Scarica CSV bookmaker arbitrage",
                    arb_df.to_csv(index=False).encode("utf-8"),
                    file_name="bookmaker_arbitrage.csv",
                    mime="text/csv",
                )

                real_arbs = arb_df[arb_df["is_arbitrage"] == True].copy()
                if not real_arbs.empty:
                    st.success(f"Trovati {len(real_arbs)} arbitrage teorici bookmaker.")
                    selected_arb_idx = st.selectbox(
                        "Aggiungi arbitraggio al paper trade",
                        real_arbs.index.tolist(),
                        format_func=lambda i: f"{real_arbs.loc[i, 'roi_%']} | {real_arbs.loc[i, 'event']} | {real_arbs.loc[i, 'market_key']}"
                    )

                    selected_row = real_arbs.loc[selected_arb_idx].to_dict()

                    with st.expander("Stake plan selezionato"):
                        try:
                            stake_df = pd.DataFrame(json.loads(selected_row.get("stake_plan") or "[]"))
                            st.dataframe(stake_df, width="stretch", hide_index=True)
                        except Exception:
                            st.write(selected_row.get("stake_plan"))

                    if st.button("Aggiungi Arbitrage Paper Trade"):
                        add_bookmaker_paper_trade(selected_row)
                        st.success("Arbitrage paper trade aggiunto.")
                else:
                    st.info("Ci sono righe sopra soglia, ma nessun arbitraggio puro con implied_sum < 1.")


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
ODDS_API_KEY = "la_tua_api_key_the_odds_api"
ODDS_API_IO_KEY = "la_tua_api_key_odds_api_io"
```

### Metodo

Questa app non cerca più Kalshi. Usa:

```text
Polymarket = mercato tradabile
The Odds API = benchmark quote bookmaker
```

Da v1.5 non devi scegliere uno sport singolo: puoi usare Auto-scan multi-sport. L'app prova più sport e più mercati.

I mercati core (`h2h`, `spreads`, `totals`) vengono richiesti dall'endpoint generale. I mercati extra/player props vengono provati tramite endpoint singolo evento, con un limite configurabile per non consumare troppa quota.

I segnali principali sono:

```text
1. Polymarket value:
   edge = fair_probability_bookmaker_no_vig - polymarket_yes_ask

2. Bookmaker arbitrage:
   sum(1 / best_odds_per_outcome) < 1
```

Nel secondo caso l'app calcola anche lo stake plan teorico per distribuire il capitale tra i bookmaker.

Da v1.7 l'app esclude i mercati Polymarket parziali come `first half`, `second half`, `quarter`, `period`, `inning`, quando li confronterebbe contro quote bookmaker full-game. Questo evita falsi positivi tipo `Both Teams to Score in Second Half` vs `BTTS full match`.

Da v2.0 l'app supporta piu' provider quote:
- The Odds API
- Odds-API.io, adapter sperimentale

Da v2.1 la protezione crediti API non modifica piu' i risultati: non taglia sport, non disattiva mercati e non cambia il matching. Ferma solo lo scanner quando i crediti sono quasi finiti. L'esclusione winner/outright e' ora un filtro separato ed esplicito.

Da v1.9 e' stata introdotta la protezione anti-spreco quota.

Da v1.8 la tabella mostra istruzioni operative esplicite:
- cosa comprare su Polymarket;
- quale esito puntare sul bookmaker;
- prezzo/quota;
- costo combinato;
- ROI teorico;
- stake plan.

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
