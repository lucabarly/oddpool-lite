
import json
import math
import re
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests
import streamlit as st


APP_VERSION = "PolyKalshi Edge Scanner v3.4 - fast candidate matching + orderbooks after match"

POLY_GAMMA = "https://gamma-api.polymarket.com"
POLY_CLOB = "https://clob.polymarket.com"
KALSHI_BASES = [
    "https://external-api.kalshi.com/trade-api/v2",
    "https://trading-api.kalshi.com/trade-api/v2",
]

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "PolyKalshiEdgeScanner/3.0"})


# ============================================================
# Utils
# ============================================================

def dec(x: Any, default: str = "0") -> Decimal:
    try:
        if x is None:
            return Decimal(default)
        if isinstance(x, Decimal):
            return x
        s = str(x).strip().replace("$", "").replace("%", "").replace(",", "")
        if s == "" or s.lower() in {"nan", "none", "null"}:
            return Decimal(default)
        return Decimal(s)
    except Exception:
        return Decimal(default)


def q4(x: Optional[Decimal]) -> str:
    if x is None:
        return ""
    try:
        return str(x.quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP))
    except Exception:
        return ""


def pct(x: Optional[Decimal]) -> str:
    if x is None:
        return ""
    try:
        return f"{(x * Decimal('100')).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}%"
    except Exception:
        return ""


def money(x: Optional[Decimal]) -> str:
    if x is None:
        return ""
    try:
        return f"${x.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}"
    except Exception:
        return ""


def safe_json_loads(x: Any) -> Any:
    if x is None:
        return None
    if isinstance(x, (list, dict)):
        return x
    try:
        return json.loads(str(x))
    except Exception:
        return None


def canonical(s: str) -> str:
    s = str(s or "").lower()
    s = s.replace("&", " and ")
    s = re.sub(r"https?://\S+", " ", s)
    s = re.sub(r"[^a-z0-9\s\.\-]", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


STOPWORDS = {
    "will", "the", "a", "an", "be", "to", "of", "on", "in", "for", "by", "at", "and",
    "or", "market", "prediction", "event", "contract", "this", "that", "with", "from",
    "vs", "v", "game", "match", "during", "before", "after", "end", "ends", "any",
    "which", "who", "what", "when", "where", "do", "does", "is", "are", "have", "has",
    "win", "wins", "winner", "yes", "no"
}


SYNONYMS = {
    "usa": "united states",
    "us": "united states",
    "u.s.": "united states",
    "u.s": "united states",
    "uk": "united kingdom",
    "btc": "bitcoin",
    "eth": "ethereum",
    "trump": "donald trump",
    "biden": "joe biden",
    "fomc": "fed",
    "cpi": "inflation",
    "btts": "both teams score",
    "both teams to score": "both teams score",
}


def normalize_text_for_match(s: str) -> str:
    s = canonical(s)
    for k, v in SYNONYMS.items():
        s = re.sub(rf"\b{re.escape(k)}\b", v, s)
    return re.sub(r"\s+", " ", s).strip()


def tokens(s: str) -> List[str]:
    s = normalize_text_for_match(s)
    out = []
    for t in s.split():
        if t in STOPWORDS:
            continue
        if len(t) <= 1:
            continue
        out.append(t)
    return out


def text_similarity(a: str, b: str) -> float:
    ta = set(tokens(a))
    tb = set(tokens(b))
    if not ta or not tb:
        return SequenceMatcher(None, normalize_text_for_match(a), normalize_text_for_match(b)).ratio()
    jacc = len(ta & tb) / max(1, len(ta | tb))
    seq = SequenceMatcher(None, normalize_text_for_match(a), normalize_text_for_match(b)).ratio()
    containment = len(ta & tb) / max(1, min(len(ta), len(tb)))
    return 0.50 * jacc + 0.30 * seq + 0.20 * containment


def extract_dates(s: str) -> List[str]:
    s = str(s or "")
    found = set()

    for m in re.findall(r"\b(20\d{2})[-_/](\d{1,2})[-_/](\d{1,2})\b", s):
        y, mo, d = m
        found.add(f"{int(y):04d}-{int(mo):02d}-{int(d):02d}")

    months = {
        "jan": 1, "january": 1,
        "feb": 2, "february": 2,
        "mar": 3, "march": 3,
        "apr": 4, "april": 4,
        "may": 5,
        "jun": 6, "june": 6,
        "jul": 7, "july": 7,
        "aug": 8, "august": 8,
        "sep": 9, "sept": 9, "september": 9,
        "oct": 10, "october": 10,
        "nov": 11, "november": 11,
        "dec": 12, "december": 12,
    }
    lower = s.lower()
    for name, mo in months.items():
        # June 22 2026 / Jun 22, 2026
        for m in re.finditer(rf"\b{name}\s+(\d{{1,2}})(?:st|nd|rd|th)?[,]?\s+(20\d{{2}})\b", lower):
            d = int(m.group(1))
            y = int(m.group(2))
            found.add(f"{y:04d}-{mo:02d}-{d:02d}")

    return sorted(found)


def extract_numbers_set(s: str) -> set:
    """
    Extracts relevant numeric thresholds: dates, prices, totals, percentages, years.
    We keep strings to avoid float precision issues.
    """
    return set(re.findall(r"\b\d+(?:\.\d+)?\b", str(s or "").lower()))


def extract_years_set(s: str) -> set:
    return set(re.findall(r"\b20\d{2}\b", str(s or "")))


def compact_entity_tokens(s: str) -> set:
    """
    Tokens used for entity overlap. Removes common market words.
    """
    bad = STOPWORDS | {
        "above", "below", "over", "under", "yes", "no", "before", "after",
        "january", "february", "march", "april", "june", "july", "august",
        "september", "october", "november", "december", "jan", "feb", "mar",
        "apr", "jun", "jul", "aug", "sep", "oct", "nov", "dec",
    }
    return {t for t in tokens(s) if t not in bad and not re.fullmatch(r"\d+(?:\.\d+)?", t)}


def matching_text_from_market_row(row: Dict[str, Any]) -> str:
    fields = [
        row.get("title", ""),
        row.get("ticker", ""),
        row.get("category", ""),
        row.get("event_date", ""),
        row.get("dates_in_title", ""),
    ]
    return " ".join(str(x or "") for x in fields)


def structured_score(poly_row: Dict[str, Any], kalshi_row: Dict[str, Any]) -> Tuple[float, str]:
    """
    Combines lexical score with entity/date/number checks.
    This does not try to be perfect; it is designed to explain why a match is high/low.
    """
    ptxt = matching_text_from_market_row(poly_row)
    ktxt = matching_text_from_market_row(kalshi_row)

    base = text_similarity(ptxt, ktxt)

    p_entities = compact_entity_tokens(ptxt)
    k_entities = compact_entity_tokens(ktxt)
    shared_entities = p_entities & k_entities
    entity_overlap = len(shared_entities) / max(1, min(len(p_entities), len(k_entities)))

    score = (0.70 * base) + (0.30 * entity_overlap)
    reasons = [f"base={base:.3f}", f"entity_overlap={entity_overlap:.3f}"]

    # Category boost/penalty.
    if poly_row.get("category") and kalshi_row.get("category"):
        if poly_row.get("category") == kalshi_row.get("category"):
            score += 0.04
            reasons.append("category_match=+0.04")
        else:
            score -= 0.04
            reasons.append("category_mismatch=-0.04")

    # Outcome type boost/penalty.
    po = poly_row.get("outcome_type") or "binary"
    ko = kalshi_row.get("outcome_type") or "binary"
    if po != "binary" and ko != "binary":
        if po == ko:
            score += 0.04
            reasons.append("outcome_type_match=+0.04")
        else:
            score *= 0.65
            reasons.append("outcome_type_mismatch=*0.65")

    # Date handling.
    p_dates = set([x for x in [poly_row.get("event_date", "")] + str(poly_row.get("dates_in_title", "")).split(",") if x])
    k_dates = set([x for x in [kalshi_row.get("event_date", "")] + str(kalshi_row.get("dates_in_title", "")).split(",") if x])
    if p_dates and k_dates:
        if p_dates & k_dates:
            score += 0.05
            reasons.append("date_match=+0.05")
        else:
            score *= 0.55
            reasons.append("date_mismatch=*0.55")

    # Numbers: if both have numbers and none overlap, strongly penalize.
    p_nums = extract_numbers_set(ptxt)
    k_nums = extract_numbers_set(ktxt)
    if p_nums and k_nums:
        if p_nums & k_nums:
            score += 0.04
            reasons.append("number_overlap=+0.04")
        else:
            # If only years differ this can be decisive.
            score *= 0.60
            reasons.append("number_mismatch=*0.60")

    # Years: stronger penalty.
    p_years = extract_years_set(ptxt)
    k_years = extract_years_set(ktxt)
    if p_years and k_years and not (p_years & k_years):
        score *= 0.50
        reasons.append("year_mismatch=*0.50")

    # Need at least one meaningful shared entity unless lexical score is very high.
    if not shared_entities and base < 0.72:
        score *= 0.50
        reasons.append("no_shared_entity=*0.50")

    score = max(0.0, min(1.0, score))
    if shared_entities:
        reasons.append("shared=" + ",".join(sorted(list(shared_entities))[:10]))

    return score, " | ".join(reasons)


def parse_time_date(x: Any) -> str:
    if not x:
        return ""
    s = str(x)
    if re.match(r"^\d{4}-\d{2}-\d{2}", s):
        return s[:10]
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return dt.date().isoformat()
    except Exception:
        return ""


def category_from_text(s: str) -> str:
    t = canonical(s)
    if any(k in t for k in ["bitcoin", "ethereum", "crypto", "btc", "eth", "solana", "xrp", "doge"]):
        return "crypto"
    if any(k in t for k in ["trump", "biden", "election", "senate", "president", "mayor", "congress", "politics"]):
        return "politics"
    if any(k in t for k in ["nba", "nfl", "mlb", "nhl", "soccer", "football", "world cup", "ufc", "tennis", "fifa", "team", "score", "game"]):
        return "sport"
    if any(k in t for k in ["fed", "cpi", "inflation", "rate", "recession", "gdp", "jobless", "unemployment"]):
        return "economy"
    if any(k in t for k in ["temperature", "weather", "rain", "snow", "hurricane"]):
        return "weather"
    return "other"


def outcome_direction(title: str) -> str:
    t = canonical(title)
    if any(k in t for k in ["both teams", "btts"]):
        return "btts"
    if "over" in t:
        return "over"
    if "under" in t:
        return "under"
    return "binary"


# ============================================================
# Price normalization
# ============================================================

def normalize_price_unit(x: Any) -> Optional[Decimal]:
    """
    Normalizes price to 0..1.
    Kalshi often uses cents 0..100.
    Polymarket uses dollars 0..1.
    """
    if x is None:
        return None
    v = dec(x, "-1")
    if v < 0:
        return None
    if v > 1:
        v = v / Decimal("100")
    if v < 0 or v > 1:
        return None
    return v


def infer_no_ask_from_yes_bid(yes_bid: Optional[Decimal]) -> Optional[Decimal]:
    if yes_bid is None:
        return None
    return max(Decimal("0"), min(Decimal("1"), Decimal("1") - yes_bid))


def infer_yes_ask_from_no_bid(no_bid: Optional[Decimal]) -> Optional[Decimal]:
    if no_bid is None:
        return None
    return max(Decimal("0"), min(Decimal("1"), Decimal("1") - no_bid))


def mid_from_bid_ask(bid: Optional[Decimal], ask: Optional[Decimal]) -> Optional[Decimal]:
    if bid is not None and ask is not None and ask >= bid:
        return (bid + ask) / Decimal("2")
    if bid is not None:
        return bid
    if ask is not None:
        return ask
    return None


# ============================================================
# Polymarket
# ============================================================

@st.cache_data(ttl=90, show_spinner=False)
def fetch_polymarket_markets(limit_total: int, search: str = "") -> Tuple[List[Dict[str, Any]], str]:
    """
    Polymarket Gamma pagination fix.

    Gamma often returns max ~100 markets per page even if a larger limit is requested.
    Previous versions requested 500 and stopped when 100 came back, incorrectly assuming
    there were no more pages. This version requests 100 per page and keeps paging until:
    - requested limit_total is reached
    - an empty page is returned
    - a repeated page/offset situation is detected
    - Gamma returns 422 after we already have data
    """
    out = []
    offset = 0
    batch = 100
    seen_ids = set()
    pages = 0

    try:
        while len(out) < limit_total:
            params = {
                "limit": min(batch, limit_total - len(out)),
                "offset": offset,
                "active": "true",
                "closed": "false",
                "order": "volume24hr",
                "ascending": "false",
            }
            if search:
                params["search"] = search

            r = SESSION.get(f"{POLY_GAMMA}/markets", params=params, timeout=25)

            if r.status_code == 422 and out:
                return out, f"Pagination stopped after {len(out)} markets: Gamma returned 422 at offset {offset}."
            if r.status_code != 200:
                return out, f"HTTP {r.status_code}: {r.text[:300]}"

            data = r.json()
            if not isinstance(data, list) or not data:
                break

            pages += 1
            added_this_page = 0

            for m in data:
                mid = str(m.get("id") or m.get("conditionId") or m.get("slug") or "")
                if mid and mid in seen_ids:
                    continue
                if mid:
                    seen_ids.add(mid)
                out.append(m)
                added_this_page += 1
                if len(out) >= limit_total:
                    break

            # If Gamma repeats a page or all records were duplicates, avoid infinite loop.
            if added_this_page == 0:
                return out, f"Pagination stopped after {len(out)} markets: repeated/duplicate page at offset {offset}."

            # Advance by the actual number returned by the API page, not by requested limit.
            offset += len(data)

            time.sleep(0.03)

        return out, f"Downloaded {len(out)} Polymarket markets in {pages} pages."
    except Exception as e:
        return out, str(e)


def get_poly_tokens(m: Dict[str, Any]) -> Tuple[Optional[str], Optional[str], List[str]]:
    outcomes = safe_json_loads(m.get("outcomes")) or []
    token_ids = safe_json_loads(m.get("clobTokenIds")) or safe_json_loads(m.get("clob_token_ids")) or []

    outcomes = [str(x) for x in outcomes] if isinstance(outcomes, list) else []
    token_ids = [str(x) for x in token_ids] if isinstance(token_ids, list) else []

    yes_token = None
    no_token = None

    for i, out in enumerate(outcomes):
        if i >= len(token_ids):
            continue
        o = canonical(out)
        if o == "yes":
            yes_token = token_ids[i]
        elif o == "no":
            no_token = token_ids[i]

    if not yes_token and token_ids:
        yes_token = token_ids[0]
    if not no_token and len(token_ids) > 1:
        no_token = token_ids[1]

    return yes_token, no_token, outcomes


@st.cache_data(ttl=10, show_spinner=False)
def get_poly_book(token_id: str) -> Tuple[Optional[Decimal], Optional[Decimal], str]:
    """
    Returns best bid, best ask in 0..1.
    """
    if not token_id:
        return None, None, "missing token"

    try:
        r = SESSION.get(f"{POLY_CLOB}/book", params={"token_id": token_id}, timeout=12)
        if r.status_code != 200:
            return None, None, f"HTTP {r.status_code}"

        data = r.json()
        bids = data.get("bids") or []
        asks = data.get("asks") or []

        bid = None
        ask = None
        if bids:
            bid = max([normalize_price_unit(x.get("price")) for x in bids if normalize_price_unit(x.get("price")) is not None] or [None])
        if asks:
            ask = min([normalize_price_unit(x.get("price")) for x in asks if normalize_price_unit(x.get("price")) is not None] or [None])

        return bid, ask, "ok"
    except Exception as e:
        return None, None, str(e)


def polymarket_to_rows(markets: List[Dict[str, Any]], read_books: bool, max_books: int) -> pd.DataFrame:
    rows = []
    books_read = 0

    for m in markets:
        q = str(m.get("question") or m.get("title") or "")
        if not q:
            continue

        yes_token, no_token, outcomes = get_poly_tokens(m)
        yes_bid = yes_ask = no_bid = no_ask = None
        book_status = "not read"

        if read_books and yes_token and books_read < max_books:
            yes_bid, yes_ask, book_status = get_poly_book(yes_token)
            books_read += 1

        if read_books and no_token and books_read < max_books:
            no_bid_real, no_ask_real, no_status = get_poly_book(no_token)
            no_bid = no_bid_real
            no_ask = no_ask_real
            books_read += 1

        # If direct NO book not available, infer from YES market.
        if no_ask is None:
            no_ask = infer_no_ask_from_yes_bid(yes_bid)
        if no_bid is None:
            no_bid = infer_yes_ask_from_no_bid(yes_ask)

        end = m.get("endDate") or m.get("end_date") or m.get("closedTime") or ""
        date = parse_time_date(end)
        title_full = q
        slug = m.get("slug") or ""

        rows.append({
            "source": "Polymarket",
            "id": str(m.get("id") or m.get("conditionId") or slug),
            "ticker": str(slug),
            "yes_token": yes_token,
            "no_token": no_token,
            "title": title_full,
            "matching_text": " ".join([title_full, str(slug), str(m.get("description") or ""), str(m.get("category") or "")]),
            "category": category_from_text(title_full + " " + str(m.get("category", ""))),
            "event_date": date,
            "dates_in_title": ",".join(extract_dates(title_full + " " + slug)),
            "outcome_type": outcome_direction(title_full),
            "yes_bid": yes_bid,
            "yes_ask": yes_ask,
            "no_bid": no_bid,
            "no_ask": no_ask,
            "yes_mid": mid_from_bid_ask(yes_bid, yes_ask),
            "spread": (yes_ask - yes_bid) if yes_bid is not None and yes_ask is not None else None,
            "volume": dec(m.get("volume") or m.get("volumeNum") or m.get("volume24hr") or "0"),
            "liquidity": dec(m.get("liquidity") or "0"),
            "url": f"https://polymarket.com/market/{slug}" if slug else "",
            "book_status": book_status,
            "raw": json.dumps({
                "question": q,
                "outcomes": outcomes,
                "yes_token": yes_token,
                "no_token": no_token,
            }, ensure_ascii=False),
        })

    return pd.DataFrame(rows)


# ============================================================
# Kalshi
# ============================================================

@st.cache_data(ttl=90, show_spinner=False)
def fetch_kalshi_markets(limit_total: int, search: str = "", status: str = "open") -> Tuple[List[Dict[str, Any]], str, str]:
    """
    Public market data fetch. Some environments use external-api, some trading-api.
    This is read-only. No order placement.
    """
    last_err = ""
    for base in KALSHI_BASES:
        out = []
        cursor = None
        try:
            while len(out) < limit_total:
                params = {
                    "limit": min(1000, limit_total - len(out)),
                    "status": status,
                }
                if cursor:
                    params["cursor"] = cursor
                if search:
                    params["search"] = search

                r = SESSION.get(f"{base}/markets", params=params, timeout=25)
                if r.status_code != 200:
                    last_err = f"{base}: HTTP {r.status_code}: {r.text[:300]}"
                    break

                data = r.json()
                markets = data.get("markets") or data.get("data") or []
                if not isinstance(markets, list) or not markets:
                    break
                out.extend(markets)
                cursor = data.get("cursor") or data.get("next_cursor")
                if not cursor:
                    break
                time.sleep(0.03)

            if out:
                return out, "", base
        except Exception as e:
            last_err = f"{base}: {e}"

    return [], last_err, ""


def kalshi_url(m: Dict[str, Any]) -> str:
    event = m.get("event_ticker") or m.get("eventTicker") or ""
    ticker = m.get("ticker") or ""
    if event:
        return f"https://kalshi.com/markets/{event}"
    if ticker:
        return f"https://kalshi.com/markets/{ticker}"
    return ""


def kalshi_to_rows(markets: List[Dict[str, Any]]) -> pd.DataFrame:
    rows = []

    for m in markets:
        ticker = str(m.get("ticker") or "")
        title = str(m.get("title") or m.get("yes_sub_title") or m.get("subtitle") or m.get("name") or ticker)
        subtitle = str(m.get("subtitle") or m.get("sub_title") or "")
        event_title = str(m.get("event_title") or m.get("eventTitle") or m.get("event_ticker") or "")

        series_title = str(m.get("series_title") or m.get("seriesTitle") or "")
        rules_primary = str(m.get("rules_primary") or m.get("rulesPrimary") or "")
        full_title = " ".join([x for x in [event_title, title, subtitle] if x]).strip()
        matching_text = " ".join([x for x in [event_title, series_title, title, subtitle, ticker, rules_primary] if x]).strip()

        yes_bid = normalize_price_unit(m.get("yes_bid") or m.get("yesBid"))
        yes_ask = normalize_price_unit(m.get("yes_ask") or m.get("yesAsk"))
        no_bid = normalize_price_unit(m.get("no_bid") or m.get("noBid"))
        no_ask = normalize_price_unit(m.get("no_ask") or m.get("noAsk"))

        # Kalshi orderbooks are binary; if one side missing, infer.
        if yes_ask is None:
            yes_ask = infer_yes_ask_from_no_bid(no_bid)
        if no_ask is None:
            no_ask = infer_no_ask_from_yes_bid(yes_bid)
        if yes_bid is None:
            yes_bid = infer_no_ask_from_yes_bid(no_ask)
        if no_bid is None:
            no_bid = infer_yes_ask_from_no_bid(yes_ask)

        close_time = m.get("close_time") or m.get("closeTime") or m.get("expiration_time") or m.get("expirationTime") or ""
        date = parse_time_date(close_time)

        rows.append({
            "source": "Kalshi",
            "id": ticker,
            "ticker": ticker,
            "title": full_title,
            "matching_text": matching_text,
            "category": category_from_text(matching_text + " " + str(m.get("category", ""))),
            "event_date": date,
            "dates_in_title": ",".join(extract_dates(full_title + " " + ticker)),
            "outcome_type": outcome_direction(full_title),
            "yes_bid": yes_bid,
            "yes_ask": yes_ask,
            "no_bid": no_bid,
            "no_ask": no_ask,
            "yes_mid": mid_from_bid_ask(yes_bid, yes_ask),
            "spread": (yes_ask - yes_bid) if yes_bid is not None and yes_ask is not None else None,
            "volume": dec(m.get("volume") or m.get("volume_24h") or m.get("volume24h") or "0"),
            "liquidity": dec(m.get("open_interest") or m.get("openInterest") or "0"),
            "url": kalshi_url(m),
            "book_status": "market fields",
            "raw": json.dumps({
                "ticker": ticker,
                "event_ticker": m.get("event_ticker") or m.get("eventTicker"),
                "title": title,
                "subtitle": subtitle,
            }, ensure_ascii=False),
        })

    return pd.DataFrame(rows)


# ============================================================
# Matching and arbitrage
# ============================================================

def date_match_ok(a: pd.Series, b: pd.Series) -> bool:
    da = set([x for x in [a.get("event_date", "")] + str(a.get("dates_in_title", "")).split(",") if x])
    db = set([x for x in [b.get("event_date", "")] + str(b.get("dates_in_title", "")).split(",") if x])

    # If both have explicit dates, require overlap.
    if da and db:
        return bool(da & db)
    return True


def outcome_type_ok(a: pd.Series, b: pd.Series) -> bool:
    oa = a.get("outcome_type") or "binary"
    ob = b.get("outcome_type") or "binary"
    if oa == "binary" or ob == "binary":
        return True
    return oa == ob


def rough_candidate_score(p_meta: Dict[str, Any], k_meta: Dict[str, Any], same_category: bool, require_date_match: bool) -> float:
    """
    Very fast pre-score used only to choose candidate pairs before the expensive structured_score.
    """
    p_entities = p_meta["entities"]
    k_entities = k_meta["entities"]

    shared = p_entities & k_entities
    entity_score = len(shared) / max(1, min(len(p_entities), len(k_entities)))

    score = entity_score

    if p_meta["category"] and p_meta["category"] == k_meta["category"]:
        score += 0.15
    elif same_category:
        return -1.0

    if p_meta["outcome_type"] != "binary" and k_meta["outcome_type"] != "binary":
        if p_meta["outcome_type"] == k_meta["outcome_type"]:
            score += 0.08
        else:
            score -= 0.25

    p_dates = p_meta["dates"]
    k_dates = k_meta["dates"]
    if p_dates and k_dates:
        if p_dates & k_dates:
            score += 0.15
        elif require_date_match:
            return -1.0
        else:
            score -= 0.15

    p_nums = p_meta["numbers"]
    k_nums = k_meta["numbers"]
    if p_nums and k_nums:
        if p_nums & k_nums:
            score += 0.10
        else:
            score -= 0.25

    if not shared:
        score -= 0.20

    return score


def build_market_meta(row: Dict[str, Any]) -> Dict[str, Any]:
    txt = matching_text_from_market_row(row)
    date_values = set([x for x in [row.get("event_date", "")] + str(row.get("dates_in_title", "")).split(",") if x])
    return {
        "text": txt,
        "entities": compact_entity_tokens(txt),
        "numbers": extract_numbers_set(txt),
        "years": extract_years_set(txt),
        "dates": date_values,
        "category": row.get("category"),
        "outcome_type": row.get("outcome_type") or "binary",
    }


def hydrate_matches_with_poly_books(matches: pd.DataFrame, max_books: int) -> pd.DataFrame:
    """
    Performance fix:
    - Matching does not need Polymarket orderbooks.
    - Read orderbooks only after candidate matches have been found.
    - max_books counts token-book reads, so one market can consume 1 or 2 reads.
    """
    if matches.empty or max_books <= 0:
        return matches

    out = matches.copy()
    book_cache: Dict[str, Tuple[Optional[Decimal], Optional[Decimal], str]] = {}
    reads = 0
    progress = st.progress(0, text="Leggo orderbook Polymarket solo sui match...")
    total = max(1, len(out))

    for n, idx in enumerate(out.index, start=1):
        yes_token = str(out.at[idx, "poly_yes_token"] or "")
        no_token = str(out.at[idx, "poly_no_token"] or "")

        yes_bid = out.at[idx, "poly_yes_bid"]
        yes_ask = out.at[idx, "poly_yes_ask"]
        no_bid = out.at[idx, "poly_no_bid"]
        no_ask = out.at[idx, "poly_no_ask"]

        if yes_token and reads < max_books:
            if yes_token not in book_cache:
                book_cache[yes_token] = get_poly_book(yes_token)
                reads += 1
            yes_bid, yes_ask, _ = book_cache[yes_token]

        if no_token and reads < max_books:
            if no_token not in book_cache:
                book_cache[no_token] = get_poly_book(no_token)
                reads += 1
            no_bid, no_ask, _ = book_cache[no_token]

        if no_ask is None:
            no_ask = infer_no_ask_from_yes_bid(yes_bid)
        if no_bid is None:
            no_bid = infer_yes_ask_from_no_bid(yes_ask)

        out.at[idx, "poly_yes_bid"] = yes_bid
        out.at[idx, "poly_yes_ask"] = yes_ask
        out.at[idx, "poly_no_bid"] = no_bid
        out.at[idx, "poly_no_ask"] = no_ask

        if n % 10 == 0 or n == total:
            progress.progress(n / total, text=f"Orderbook match {n}/{total} - token letti {reads}/{max_books}")

        if reads >= max_books:
            # Continue loop only to keep inferred values for already-read rows unnecessary; stop early for speed.
            break

    progress.empty()
    st.caption(f"Orderbook Polymarket letti dopo matching: {reads} token.")
    return out


def build_matches(
    poly_df: pd.DataFrame,
    kalshi_df: pd.DataFrame,
    min_similarity: float,
    max_matches: int,
    same_category: bool,
    require_date_match: bool = False,
    diagnostic_limit: int = 150,
    max_candidates_per_poly: int = 80,
    fallback_candidates: int = 120,
    exhaustive_matching: bool = False,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """
    v3.4 performance matcher:
    - precomputes metadata once;
    - uses an inverted index on entity tokens;
    - rough-scores candidates cheaply;
    - runs expensive structured_score only on top N candidates;
    - optional exhaustive mode for validation/debug.
    """
    rows = []
    diagnostic_rows = []

    if poly_df.empty or kalshi_df.empty:
        return pd.DataFrame(), pd.DataFrame()

    poly_records = list(poly_df.to_dict("records"))
    kalshi_records = list(kalshi_df.to_dict("records"))

    kalshi_meta = [build_market_meta(k) for k in kalshi_records]
    kalshi_by_volume = sorted(
        range(len(kalshi_records)),
        key=lambda ix: float(kalshi_records[ix].get("volume") or 0),
        reverse=True,
    )

    token_index: Dict[str, List[int]] = {}
    for idx, meta in enumerate(kalshi_meta):
        for tok in list(meta["entities"])[:50]:
            token_index.setdefault(tok, []).append(idx)

    progress = st.progress(0, text="Matching veloce Polymarket ↔ Kalshi...")
    total = len(poly_records)
    comparisons = 0
    expensive_scores = 0

    for i, p in enumerate(poly_records, start=1):
        pser = pd.Series(p)
        p_meta = build_market_meta(p)

        if exhaustive_matching:
            candidate_indices = set(range(len(kalshi_records)))
        else:
            candidate_indices = set()
            for tok in p_meta["entities"]:
                candidate_indices.update(token_index.get(tok, []))

            # Fallback: do NOT scan all Kalshi. Use top liquid/active candidates only.
            if not candidate_indices:
                candidate_indices = set(kalshi_by_volume[:fallback_candidates])

        rough = []
        for idx in candidate_indices:
            k = kalshi_records[idx]
            kser = pd.Series(k)

            if same_category and k.get("category") != p.get("category"):
                continue

            if require_date_match and not date_match_ok(pser, kser):
                continue

            if not outcome_type_ok(pser, kser):
                continue

            rs = rough_candidate_score(p_meta, kalshi_meta[idx], same_category, require_date_match)
            if rs < -0.10:
                continue

            rough.append((rs, idx))

        rough.sort(key=lambda x: x[0], reverse=True)

        # Cap expensive scoring.
        if not exhaustive_matching:
            rough = rough[:max_candidates_per_poly]

        scored = []
        for _, idx in rough:
            k = kalshi_records[idx]
            sim, reason = structured_score(p, k)
            scored.append((sim, k, reason))
            expensive_scores += 1

        comparisons += len(rough)
        scored.sort(key=lambda x: x[0], reverse=True)

        if scored:
            sim, k, reason = scored[0]
            diagnostic_rows.append({
                "similarity": sim,
                "score_reason": reason,
                "category_poly": p.get("category"),
                "category_kalshi": k.get("category"),
                "poly_title": p.get("title"),
                "kalshi_title": k.get("title"),
                "poly_date": p.get("event_date"),
                "kalshi_date": k.get("event_date"),
                "poly_url": p.get("url"),
                "kalshi_url": k.get("url"),
            })

        for sim, k, reason in scored[:5]:
            if sim < min_similarity:
                continue

            rows.append({
                "similarity": sim,
                "confidence": "Alta" if sim >= 0.80 else ("Media" if sim >= 0.65 else "Bassa"),
                "score_reason": reason,
                "category": p.get("category"),
                "kalshi_category": k.get("category"),
                "outcome_type": p.get("outcome_type"),
                "poly_title": p.get("title"),
                "kalshi_title": k.get("title"),
                "poly_ticker": p.get("ticker"),
                "poly_yes_token": p.get("yes_token"),
                "poly_no_token": p.get("no_token"),
                "kalshi_ticker": k.get("ticker"),
                "poly_date": p.get("event_date"),
                "kalshi_date": k.get("event_date"),
                "poly_yes_bid": p.get("yes_bid"),
                "poly_yes_ask": p.get("yes_ask"),
                "poly_no_bid": p.get("no_bid"),
                "poly_no_ask": p.get("no_ask"),
                "kalshi_yes_bid": k.get("yes_bid"),
                "kalshi_yes_ask": k.get("yes_ask"),
                "kalshi_no_bid": k.get("no_bid"),
                "kalshi_no_ask": k.get("no_ask"),
                "poly_url": p.get("url"),
                "kalshi_url": k.get("url"),
                "poly_volume": p.get("volume"),
                "kalshi_volume": k.get("volume"),
                "poly_liquidity": p.get("liquidity"),
                "kalshi_liquidity": k.get("liquidity"),
            })

            if len(rows) >= max_matches:
                progress.empty()
                st.caption(f"Matching performance: {comparisons:,} candidati valutati; {expensive_scores:,} score completi.")
                diag = pd.DataFrame(diagnostic_rows)
                if not diag.empty:
                    diag = diag.sort_values("similarity", ascending=False).head(diagnostic_limit)
                return pd.DataFrame(rows), diag

        if i % 20 == 0 or i == total:
            progress.progress(
                i / total,
                text=f"Matching veloce {i}/{total} | candidati {comparisons:,} | score completi {expensive_scores:,}"
            )

    progress.empty()
    st.caption(f"Matching performance: {comparisons:,} candidati valutati; {expensive_scores:,} score completi.")
    match_df = pd.DataFrame(rows)
    diag_df = pd.DataFrame(diagnostic_rows)
    if not diag_df.empty:
        diag_df = diag_df.sort_values("similarity", ascending=False).head(diagnostic_limit)
    return match_df, diag_df


def arbitrage_plan(price_a: Decimal, price_b: Decimal, capital: Decimal, leg_a: str, leg_b: str) -> Dict[str, Any]:
    combo = price_a + price_b
    if combo <= 0:
        return {}
    payout = capital / combo
    cost_a = payout * price_a
    cost_b = payout * price_b
    profit = payout - capital
    roi = profit / capital if capital > 0 else Decimal("0")

    return {
        "combo_cost": combo,
        "payout": payout,
        "profit": profit,
        "roi": roi,
        "stake_plan": json.dumps([
            {"leg": leg_a, "price": float(price_a), "cost_$": money(cost_a), "payout_if_wins_$": money(payout)},
            {"leg": leg_b, "price": float(price_b), "cost_$": money(cost_b), "payout_if_wins_$": money(payout)},
        ], ensure_ascii=False),
    }


def evaluate_cross_market(matches: pd.DataFrame, capital: Decimal, min_roi: Decimal) -> pd.DataFrame:
    if matches.empty:
        return pd.DataFrame()

    rows = []
    for _, r in matches.iterrows():
        py = r.get("poly_yes_ask")
        pn = r.get("poly_no_ask")
        ky = r.get("kalshi_yes_ask")
        kn = r.get("kalshi_no_ask")

        strategies = []

        if py is not None and kn is not None:
            plan = arbitrage_plan(dec(py), dec(kn), capital, "Compra YES Polymarket", "Compra NO Kalshi")
            if plan:
                strategies.append(("HEDGE: Polymarket YES + Kalshi NO", plan))

        if ky is not None and pn is not None:
            plan = arbitrage_plan(dec(ky), dec(pn), capital, "Compra YES Kalshi", "Compra NO Polymarket")
            if plan:
                strategies.append(("HEDGE: Kalshi YES + Polymarket NO", plan))

        # Directional best-price spread, not guaranteed arbitrage.
        best_yes_platform = ""
        best_yes_price = None
        if py is not None and ky is not None:
            if dec(py) < dec(ky):
                best_yes_platform = "Polymarket"
                best_yes_price = dec(py)
            else:
                best_yes_platform = "Kalshi"
                best_yes_price = dec(ky)

        best_no_platform = ""
        best_no_price = None
        if pn is not None and kn is not None:
            if dec(pn) < dec(kn):
                best_no_platform = "Polymarket"
                best_no_price = dec(pn)
            else:
                best_no_platform = "Kalshi"
                best_no_price = dec(kn)

        if strategies:
            strategies.sort(key=lambda x: x[1]["roi"], reverse=True)
            name, best = strategies[0]
            action = name if best["roi"] >= min_roi else "NO TRADE - ROI sotto soglia"
            rows.append({
                **r.to_dict(),
                "azione_operativa": action,
                "roi": best["roi"],
                "roi_%": pct(best["roi"]),
                "profitto_teorico_$": money(best["profit"]),
                "costo_combo": q4(best["combo_cost"]),
                "payout_garantito_$": money(best["payout"]),
                "stake_plan": best["stake_plan"],
                "best_yes_platform": best_yes_platform,
                "best_yes_price": q4(best_yes_price),
                "best_no_platform": best_no_platform,
                "best_no_price": q4(best_no_price),
            })
        else:
            rows.append({
                **r.to_dict(),
                "azione_operativa": "NO TRADE - prezzi mancanti",
                "roi": None,
                "roi_%": "",
                "profitto_teorico_$": "",
                "costo_combo": "",
                "payout_garantito_$": "",
                "stake_plan": "",
                "best_yes_platform": best_yes_platform,
                "best_yes_price": q4(best_yes_price),
                "best_no_platform": best_no_platform,
                "best_no_price": q4(best_no_price),
            })

    out = pd.DataFrame(rows)
    if not out.empty:
        out["_roi_sort"] = out["roi"].apply(lambda x: float(x) if x is not None and str(x) != "nan" else -999)
        out = out.sort_values(["_roi_sort", "similarity"], ascending=[False, False]).drop(columns=["_roi_sort"])
    return out


def pretty_prices(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    for c in [
        "poly_yes_bid", "poly_yes_ask", "poly_no_bid", "poly_no_ask",
        "kalshi_yes_bid", "kalshi_yes_ask", "kalshi_no_bid", "kalshi_no_ask",
    ]:
        if c in out.columns:
            out[c] = out[c].apply(lambda x: q4(dec(x)) if x is not None and str(x) != "nan" else "")
    if "similarity" in out.columns:
        out["similarity"] = out["similarity"].apply(lambda x: round(float(x), 4))
    return out


# ============================================================
# UI
# ============================================================

st.set_page_config(page_title="PolyKalshi Edge Scanner", layout="wide")

st.title("PolyKalshi Edge Scanner")
st.caption(APP_VERSION)

st.warning(
    "Trade automatici real-money: disabilitati. Questa app genera segnali, normalizzazione prezzi e paper trade plan. "
    "Non inserire private key o credenziali trading in Streamlit Cloud."
)

with st.sidebar:
    st.header("Configurazione")

    search = st.text_input(
        "Filtro testo globale",
        value="",
        help="Esempi: trump, bitcoin, argentina, fed, cpi, world cup. Vuoto = mercati top per volume."
    )

    category_filter = st.multiselect(
        "Categorie da includere",
        ["politics", "sport", "crypto", "economy", "weather", "other"],
        default=["politics", "sport", "crypto", "economy", "weather", "other"],
    )

    poly_limit = st.slider("Polymarket da scaricare", 100, 5000, 1500, step=100)
    kalshi_limit = st.slider("Kalshi da scaricare", 100, 5000, 1500, step=100)

    read_poly_books = st.checkbox(
        "Leggi orderbook Polymarket live dopo il matching",
        value=True,
        help="v3.4: gli orderbook vengono letti solo sui match trovati, non su tutti i mercati. Molto più veloce."
    )
    max_poly_books = st.slider("Max token Polymarket da leggere dopo matching", 20, 3000, 400, step=20)

    st.markdown("### Performance matching")
    exhaustive_matching = st.checkbox(
        "Confronto esaustivo Polymarket x Kalshi",
        value=False,
        help="Molto lento. Usalo solo per debug su dataset piccoli."
    )
    max_candidates_per_poly = st.slider(
        "Max candidati Kalshi per mercato Polymarket",
        20,
        1000,
        80,
        step=20,
        help="Più alto = più recall ma più lento. 60-120 è un buon range."
    )
    fallback_candidates = st.slider(
        "Fallback candidati se non ci sono parole condivise",
        20,
        1000,
        120,
        step=20,
        help="Evita di confrontare ogni Polymarket con tutti i Kalshi."
    )

    min_similarity = st.slider("Similarità minima matching", 0.40, 0.95, 0.68, step=0.01)
    same_category = st.checkbox(
        "Richiedi stessa categoria stimata",
        value=False,
        help="Disattivato di default: le categorie sono stimate e possono essere diverse tra Kalshi e Polymarket."
    )

    require_date_match = st.checkbox(
        "Richiedi stessa data",
        value=False,
        help="Disattivato di default: le date di chiusura/settlement possono differire anche per eventi simili."
    )

    max_matches = st.slider("Massimo match da mostrare", 20, 3000, 500, step=20)
    capital = dec(st.number_input("Capitale per strategia ($)", min_value=1.0, max_value=100000.0, value=100.0, step=25.0))
    min_roi = dec(st.number_input("ROI minimo arbitraggio (%)", min_value=0.0, max_value=50.0, value=1.0, step=0.10)) / Decimal("100")

    fx_cost = dec(st.number_input(
        "Costo cambio/prelievo stimato round-trip (%)",
        min_value=0.0,
        max_value=20.0,
        value=2.0,
        step=0.10,
        help="Serve come riferimento: ROI sotto questo costo non è operativo per chi parte da EUR."
    )) / Decimal("100")

    st.markdown("---")
    st.caption("Kalshi: se il primo endpoint non risponde, l'app prova automaticamente l'altro base URL pubblico.")


tab_scan, tab_poly, tab_kalshi, tab_setup = st.tabs(["Scanner", "Polymarket", "Kalshi", "Setup"])


with tab_scan:
    st.subheader("Polymarket ↔ Kalshi normalized scanner")

    st.caption(
        f"Filtro costo Italia/EUR: ROI minimo impostato {pct(min_roi)}; costo cambio stimato {pct(fx_cost)}. "
        "Operativamente ha senso cercare ROI > costo cambio + margine sicurezza."
    )

    if st.button("Avvia scanner Polymarket ↔ Kalshi", type="primary"):
        with st.spinner("Scarico Polymarket..."):
            poly_markets, poly_err = fetch_polymarket_markets(poly_limit, search)

        with st.spinner("Scarico Kalshi..."):
            kalshi_markets, kalshi_err, kalshi_base = fetch_kalshi_markets(kalshi_limit, search)

        if poly_err:
            if str(poly_err).startswith("Downloaded"):
                st.caption(f"Polymarket: {poly_err}")
            else:
                st.warning(f"Polymarket warning: {poly_err}")
        if kalshi_err:
            st.warning(f"Kalshi warning: {kalshi_err}")
        if kalshi_base:
            st.caption(f"Kalshi base URL usato: {kalshi_base}")

        with st.spinner("Normalizzo Polymarket senza orderbook..."):
            # Performance fix v3.4:
            # orderbook is not needed for matching. We read it only after candidates are found.
            poly_df = polymarket_to_rows(poly_markets, False, 0)

        with st.spinner("Normalizzo Kalshi..."):
            kalshi_df = kalshi_to_rows(kalshi_markets)

        if category_filter:
            poly_df = poly_df[poly_df["category"].isin(category_filter)].copy()
            kalshi_df = kalshi_df[kalshi_df["category"].isin(category_filter)].copy()

        st.info(
            f"Dataset normalizzato: {len(poly_df)} Polymarket su {len(poly_markets)} scaricati; {len(kalshi_df)} Kalshi su {len(kalshi_markets)} scaricati. "
            f"Prezzi in scala 0..1."
        )

        if poly_df.empty:
            st.error("Nessun mercato Polymarket dopo filtri.")
        elif kalshi_df.empty:
            st.error("Nessun mercato Kalshi dopo filtri.")
        else:
            with st.spinner("Cerco match cross-platform..."):
                matches, diagnostics = build_matches(
                    poly_df,
                    kalshi_df,
                    min_similarity,
                    max_matches,
                    same_category,
                    require_date_match,
                    diagnostic_limit=100,
                    max_candidates_per_poly=max_candidates_per_poly,
                    fallback_candidates=fallback_candidates,
                    exhaustive_matching=exhaustive_matching,
                )

            if matches.empty:
                st.warning("Nessun match sopra la soglia. Qui sotto trovi i migliori quasi-match per capire se la soglia è troppo alta o se i dataset sono davvero diversi.")
                if not diagnostics.empty:
                    diag_cols = [
                        "similarity", "category_poly", "category_kalshi", "score_reason",
                        "poly_title", "kalshi_title", "poly_date", "kalshi_date", "poly_url", "kalshi_url"
                    ]
                    st.markdown("### Diagnostica migliori quasi-match")
                    st.dataframe(diagnostics[diag_cols].head(100), width="stretch", hide_index=True)
            else:
                if read_poly_books:
                    with st.spinner("Leggo orderbook Polymarket solo sui match trovati..."):
                        matches = hydrate_matches_with_poly_books(matches, max_poly_books)
                else:
                    st.info("Orderbook Polymarket live disattivato: il matching è più veloce, ma arbitraggio può avere prezzi Polymarket mancanti.")

                if 'diagnostics' in locals() and not diagnostics.empty:
                    with st.expander("Diagnostica migliori quasi-match"):
                        diag_cols = [
                            "similarity", "category_poly", "category_kalshi", "score_reason",
                            "poly_title", "kalshi_title", "poly_date", "kalshi_date", "poly_url", "kalshi_url"
                        ]
                        st.dataframe(diagnostics[diag_cols].head(100), width="stretch", hide_index=True)

                st.markdown("### Match trovati")
                match_cols = [
                    "confidence", "similarity", "category", "kalshi_category", "score_reason", "outcome_type",
                    "poly_title", "kalshi_title", "poly_date", "kalshi_date",
                    "poly_yes_ask", "poly_no_ask", "kalshi_yes_ask", "kalshi_no_ask",
                    "poly_url", "kalshi_url"
                ]
                st.dataframe(pretty_prices(matches)[match_cols].head(300), width="stretch", hide_index=True)

                st.markdown("### Arbitraggio / hedge cross-market")
                evaluated = evaluate_cross_market(matches, capital, min_roi)

                show_cols = [
                    "azione_operativa", "roi_%", "profitto_teorico_$", "costo_combo", "payout_garantito_$",
                    "confidence", "similarity", "category", "kalshi_category", "score_reason", "outcome_type",
                    "poly_title", "kalshi_title",
                    "poly_yes_ask", "poly_no_ask", "kalshi_yes_ask", "kalshi_no_ask",
                    "best_yes_platform", "best_yes_price", "best_no_platform", "best_no_price",
                    "poly_url", "kalshi_url", "stake_plan"
                ]
                st.dataframe(pretty_prices(evaluated)[show_cols].head(500), width="stretch", hide_index=True)

                good = evaluated[evaluated["azione_operativa"].astype(str).str.startswith("HEDGE")].copy()
                if good.empty:
                    st.info("Nessun arbitraggio sopra soglia ROI. Puoi abbassare soglia o cercare eventi specifici.")
                else:
                    st.success(f"Trovati {len(good)} hedge sopra soglia.")

                    idx = st.selectbox(
                        "Vedi stake plan",
                        good.index.tolist(),
                        format_func=lambda i: f"{good.loc[i, 'roi_%']} | {good.loc[i, 'poly_title'][:60]} ↔ {good.loc[i, 'kalshi_title'][:60]}"
                    )
                    try:
                        st.dataframe(pd.DataFrame(json.loads(good.loc[idx, "stake_plan"])), width="stretch", hide_index=True)
                    except Exception:
                        st.write(good.loc[idx, "stake_plan"])

                st.download_button(
                    "Scarica CSV risultati",
                    pretty_prices(evaluated).to_csv(index=False).encode("utf-8"),
                    "polykalshi_results.csv",
                    "text/csv",
                )


with tab_poly:
    st.subheader("Polymarket normalizzato")
    if st.button("Carica Polymarket", key="load_poly"):
        markets, err = fetch_polymarket_markets(poly_limit, search)
        if err:
            if str(err).startswith("Downloaded"):
                st.caption(f"Polymarket: {err}")
            else:
                st.warning(err)
        df = polymarket_to_rows(markets, read_poly_books, max_poly_books)
        if category_filter:
            df = df[df["category"].isin(category_filter)].copy()
        cols = ["category", "title", "event_date", "yes_bid", "yes_ask", "no_bid", "no_ask", "volume", "liquidity", "url", "book_status"]
        st.dataframe(pretty_prices(df)[cols].head(1000), width="stretch", hide_index=True)


with tab_kalshi:
    st.subheader("Kalshi normalizzato")
    if st.button("Carica Kalshi", key="load_kalshi"):
        markets, err, base = fetch_kalshi_markets(kalshi_limit, search)
        if err:
            st.warning(err)
        if base:
            st.caption(f"Base URL: {base}")
        df = kalshi_to_rows(markets)
        if category_filter:
            df = df[df["category"].isin(category_filter)].copy()
        cols = ["category", "title", "event_date", "yes_bid", "yes_ask", "no_bid", "no_ask", "volume", "liquidity", "url", "book_status"]
        st.dataframe(pretty_prices(df)[cols].head(1000), width="stretch", hide_index=True)


with tab_setup:
    st.subheader("Setup e logica")

    st.markdown(
        """
### Fix v3.4

Performance:
- non legge più gli orderbook Polymarket prima del matching;
- legge gli orderbook solo sui match trovati;
- usa inverted index su entità/parole importanti;
- limita i candidati Kalshi per ogni mercato Polymarket;
- confronto esaustivo disponibile solo come opzione debug.

### Fix v3.3

Il matching ora usa uno score spiegabile:
- similarità testuale;
- overlap entità;
- controllo numeri/soglie;
- controllo anni/date;
- penalità se non ci sono entità condivise;
- spiegazione `score_reason` per ogni match.

### Fix v3.2

Il matching è meno rigido:
- stessa categoria disattivata di default;
- stessa data disattivata di default;
- nessuno scarto automatico se non ci sono parole identiche;
- tabella diagnostica dei migliori quasi-match anche sotto soglia.

### Fix v3.1

La paginazione Polymarket ora usa batch da 100 e continua fino al limite impostato nello slider. Prima chiedeva 500 mercati, Gamma ne restituiva 100, e l'app si fermava erroneamente a 100.

### Logica normalizzata

Ogni mercato viene trasformato in questo schema comune:

```text
source
title
category
YES bid
YES ask
NO bid
NO ask
event_date
volume/liquidity
url
```

Poi lo scanner cerca match testuali tra Polymarket e Kalshi, con controllo opzionale di categoria e date.

### Strategie calcolate

```text
Polymarket YES + Kalshi NO
Kalshi YES + Polymarket NO
```

Se:

```text
YES ask piattaforma A + NO ask piattaforma B < 1
```

allora esiste arbitraggio teorico.

### Nota importante

Questa versione è read-only. Non piazza ordini reali. Prima di fare qualsiasi operazione reale devi verificare manualmente:

```text
evento identico
risoluzione identica
data identica
mercato non ambiguo
liquidità sufficiente
fee
cambio EUR/USD/USDC
regole di settlement
```

### Perché questa versione è diversa

Qui non usiamo bookmaker. Confrontiamo due mercati binari veri:

```text
Polymarket
Kalshi
```

Quindi la normalizzazione è molto più pulita rispetto a bookmaker odds.
"""
    )
