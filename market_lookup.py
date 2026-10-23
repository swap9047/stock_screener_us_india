"""One lookup for every live caller -- the research library's Q&A (api/market.py) and Claude chat's
connector (api/mcp.py): a company name or ticker in, the screener's numbers out.

  1. Find the ticker: a ticker the database knows (a bare symbol tried as .NS, .BO, then as typed);
     a name decision the library made (kg_aliases); a listing with the same name (the library's
     match_key); the only company whose name starts with it; the clearly closest listing name; else
     Yahoo's own symbol search -- so a company the library never discussed still resolves.
  2. Use the stored row (kg_market) while it is younger than STALE_HOURS; otherwise compute it now
     with market_api.compute_rows and the screener's saved calc settings (kg_market_meta, written
     by publish_market.py), so a live number matches the dashboard's, and store it.

The database is optional: without MARKET_DB_URL the lookup still answers, from Yahoo alone with the
default settings, and stores nothing. Every database call goes through MarketDB, so the checks can
replace it with an in-memory fake.
"""

import json
import os
import re
import unicodedata
from datetime import datetime, timezone

import market_api
import stock_data

# A stored row older than this is computed again. The screener republishes its own tickers every
# ~30 minutes in market hours and the library's twice a day, so in practice only rows nobody
# refreshed (an earlier live lookup, a weekend) are recomputed.
STALE_HOURS = 20
MAX_QUERIES = 5
# A misspelt name is matched to the closest listing (pg_trgm similarity of match_keys) only when it
# scores at least SIMILAR_MIN and leads every other company by SIMILAR_LEAD; otherwise Yahoo's
# search decides, and the answer says the company was matched by name.
SIMILAR_MIN = 0.7
SIMILAR_LEAD = 0.1
TICKER_RE = re.compile(r"^[A-Z0-9&\-]{1,15}(\.(NS|BO))?$")
_EXCHANGE_RANK = {"NSE": 0, "BSE": 1}
# Yahoo's exchange codes for the listings the screener computes (India, then the US venues).
_YAHOO_EXCHANGES = ("NSI", "BSE", "NMS", "NYQ", "NGM", "NCM", "ASE", "PCX", "BTS")


class MarketDB:
    """The few queries the lookup needs, as the market_writer role (it may read the listings and the
    name decisions, and read and write the kg_market tables -- nothing else)."""

    def __init__(self, conn):
        self.conn = conn

    @classmethod
    def from_env(cls):
        """(MarketDB or None, error): None without MARKET_DB_URL; error names a failed connection's
        class only, since the driver's message can quote the host."""
        url = os.environ.get("MARKET_DB_URL", "").strip()
        if not url:
            return None, ""
        try:
            import psycopg
            return cls(psycopg.connect(url, autocommit=True, prepare_threshold=None, connect_timeout=10)), ""
        except Exception as e:
            return None, f"database unavailable ({type(e).__name__})"

    def close(self):
        try:
            self.conn.close()
        except Exception:
            pass

    def _all(self, sql, params=()):
        with self.conn.cursor() as cur:
            cur.execute(sql, params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]

    def known_tickers(self, tickers):
        rows = self._all("SELECT ticker FROM kg_market WHERE ticker = ANY(%s) "
                         "UNION SELECT ticker FROM kg_companies WHERE ticker = ANY(%s)", (tickers, tickers))
        return {r["ticker"] for r in rows}

    def alias_ticker(self, key):
        """The ticker the library decided for a spoken name (kg_aliases, keyed by match_key)."""
        rows = self._all("SELECT ticker FROM kg_aliases WHERE alias_key = %s AND ticker IS NOT NULL "
                         "AND status IN ('approved', 'auto') LIMIT 1", (key,))
        return rows[0]["ticker"] if rows else None

    def listings_exact(self, key):
        """[{ticker, name, exchange, match_key}] whose match_key is the key ("Zed Labs" -> every Zed Laboratories listing)."""
        return self._all("SELECT ticker, name, exchange, match_key FROM kg_companies WHERE match_key = %s", (key,))

    def listings_starting(self, key):
        """[{ticker, name, exchange, match_key}] whose match_key starts with the key, at most 20 (a unique prefix:
        "Syncorex" -> Syncorex International)."""
        return self._all("SELECT ticker, name, exchange, match_key FROM kg_companies WHERE match_key LIKE %s LIMIT 20",
                         (_like_prefix(key),))

    def listings_similar(self, key):
        """[{ticker, name, exchange, match_key, score}] closest by trigram similarity (pg_trgm, as the library's own
        resolution scores names), best first."""
        return self._all("SELECT ticker, name, exchange, match_key, similarity(match_key, %s) AS score FROM kg_companies "
                         "WHERE match_key %% %s AND similarity(match_key, %s) >= 0.45 ORDER BY score DESC LIMIT 3",
                         (key, key, key))

    def rows(self, tickers):
        out = {}
        for r in self._all("SELECT * FROM kg_market WHERE ticker = ANY(%s)", (tickers,)):
            for k in ("metrics", "ai"):
                if isinstance(r.get(k), str):
                    r[k] = json.loads(r[k] or "{}")
            out[r["ticker"]] = r
        return out

    def calc_settings(self):
        rows = self._all("SELECT value FROM kg_market_meta WHERE key = 'calc_settings'")
        try:
            return json.loads(rows[0]["value"]) if rows else {}
        except (TypeError, ValueError):
            return {}

    def save(self, records):
        """Store live records. A row the screener or the library job owns keeps its source (they
        refresh it again) and its AI write-ups when the live record has none."""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        with self.conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO kg_market (ticker, company_name, exchange, currency, source, data_end, as_of, metrics, ai, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s) ON CONFLICT (ticker) DO UPDATE SET "
                "company_name = excluded.company_name, exchange = excluded.exchange, currency = excluded.currency, "
                "source = CASE WHEN kg_market.source IN ('screener', 'library') THEN kg_market.source ELSE excluded.source END, "
                "data_end = excluded.data_end, as_of = excluded.as_of, metrics = excluded.metrics, "
                "ai = CASE WHEN excluded.ai = '{}'::jsonb THEN kg_market.ai ELSE excluded.ai END, updated_at = excluded.updated_at",
                [(r["ticker"], r["company_name"], r["exchange"], r["currency"], r["source"], r["data_end"], r["as_of"],
                  json.dumps(r["metrics"]), json.dumps(r["ai"]), now) for r in records])


def _like_prefix(text):
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


# The research library's match_key (knowledge_store.match_key), which keys kg_companies.match_key and
# kg_aliases.alias_key: folded, legal words dropped, "labs" read as "laboratories", "&" as "and",
# letters and digits only. "Zed Labs Limited" == "Zed Labs" == "zed laboratories". Kept in
# step with that function, or a name the library resolves would miss here.
_LEGAL_WORDS = r"\b(?:the|ltd|limited|inc|incorporated|corp|corporation|plc|co|llc|lp|nv|sa|ag|se|holdings?)\b"


def match_key(name):
    t = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode()
    t = t.lower().replace("'", "").replace("\u2019", "").replace("&", " and ")
    t = re.sub(_LEGAL_WORDS, " ", t)
    t = re.sub(r"\blabs?\b", "laboratories", t)
    return re.sub(r"[^0-9a-z]", "", t)


def _best_listing(rows):
    """The ticker of one company's listings, NSE first, then BSE, then US."""
    return sorted(rows, key=lambda r: (_EXCHANGE_RANK.get(r.get("exchange"), 2), r["ticker"]))[0]["ticker"]


def yahoo_symbol(name):
    """The first equity Yahoo's search finds for a name, on an exchange the screener computes; Indian
    listings first (the library is mostly NSE/BSE). None when Yahoo finds none or fails."""
    try:
        import yfinance as yf
        quotes = yf.Search(name, max_results=8).quotes or []
    except Exception:
        return None
    equities = [q for q in quotes if q.get("quoteType") == "EQUITY" and q.get("exchange") in _YAHOO_EXCHANGES and q.get("symbol")]
    if not equities:
        return None
    return sorted(equities, key=lambda q: _YAHOO_EXCHANGES.index(q["exchange"]))[0]["symbol"]


def resolve(db, query, yahoo=yahoo_symbol):
    """(ticker or None, how): how one query was matched -- "ticker", "decision", "listing", "similar",
    "yahoo" -- so a caller can say how sure the match is."""
    text = " ".join(str(query or "").split())
    if not text:
        return None, ""
    up = text.upper()
    if db is not None:
        if TICKER_RE.match(up):
            # A bare symbol is read as NSE first, like the library's own resolution (a company with
            # an NSE listing and a US ADR under one symbol gets the NSE one).
            order = [up] if "." in up else [up + ".NS", up + ".BO", up]
            known = db.known_tickers(order)
            for t in order:
                if t in known:
                    return t, "ticker"
        key = match_key(text)
        if key:
            hit = db.alias_ticker(key)
            if hit:
                return hit, "decision"
            exact = db.listings_exact(key)
            if exact:
                return _best_listing(exact), "listing"
            starting = db.listings_starting(key) if len(key) >= 5 else []
            if len(key) >= 5 and len({r["match_key"] for r in starting}) == 1:
                return _best_listing(starting), "listing"
            try:
                similar = db.listings_similar(key)
            except Exception:
                similar = []
            # Taken only when close and clearly ahead of the next company: "Zeta Industries" scores 0.61
            # against "Zed Industries", too loose to call, so Yahoo decides.
            best = similar[0]["match_key"] if similar else None
            rivals = [r["score"] for r in similar if r["match_key"] != best]
            if best and similar[0]["score"] >= SIMILAR_MIN and similar[0]["score"] - max(rivals, default=0) >= SIMILAR_LEAD:
                return _best_listing([r for r in similar if r["match_key"] == best]), "similar"
    found = yahoo(text)
    if found:
        return found, "yahoo"
    if TICKER_RE.match(up) and db is None:
        return up, "ticker"
    return None, ""


def is_stale(row, now=None):
    try:
        computed = datetime.fromisoformat(str(row.get("as_of")).replace("Z", "+00:00"))
        if computed.tzinfo is None:
            computed = computed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return True
    return ((now or datetime.now(timezone.utc)) - computed).total_seconds() > STALE_HOURS * 3600


def screener_settings(db):
    """The screener's saved calc settings over the defaults (the defaults alone without a database)."""
    settings = dict(stock_data.DEFAULT_SETTINGS)
    if db is not None:
        try:
            saved = db.calc_settings()
        except Exception:
            saved = {}
        settings.update({k: v for k, v in saved.items() if k in settings})
    return settings


def lookup(queries, refresh=False, db=None, compute=None, yahoo=yahoo_symbol):
    """[{query, found, ticker, how, record, live, note}] for up to MAX_QUERIES names or tickers, in the
    order asked; record is market_api.public_record's shape. Never raises for a bad name or a Yahoo
    miss: those come back found=False with a note."""
    compute = compute or market_api.compute_rows
    queries = [q for q in queries if str(q or "").strip()][:MAX_QUERIES]
    settings = screener_settings(db)
    resolved = [(q, *resolve(db, q, yahoo)) for q in queries]
    tickers = list(dict.fromkeys(t for _, t, _ in resolved if t))
    stored = db.rows(tickers) if db is not None and tickers else {}
    need = [t for t in tickers if refresh or t not in stored or is_stale(stored[t])]
    fresh = {}
    if need:
        rows = compute(need, settings=settings)
        for t, row in rows.items():
            old = stored.get(t) or {}
            sentiment = ((old.get("ai") or {}).get("sentiment") or {}).get("sentiment")
            if sentiment:
                # A screener ticker's guarded Sentiment, so Expert Take and its reasons match the dashboard's.
                row["sentiment"] = sentiment
                row["expert_take"] = market_api.columns_verdict(row)
            record = market_api.public_record(row, "live", settings=settings)
            if not record["ai"] and old.get("ai"):
                record["ai"] = old["ai"]
            fresh[t] = record
        if db is not None and fresh:
            db.save(list(fresh.values()))
    out = []
    for q, ticker, how in resolved:
        record = fresh.get(ticker) or stored.get(ticker)
        if record:
            out.append({"query": q, "found": True, "ticker": ticker, "how": how, "record": record,
                        "live": ticker in fresh, "note": ""})
        else:
            note = (f"{ticker}: Yahoo returned no price data" if ticker
                    else "no listed company matches this name; try the ticker (e.g. ABC.NS for NSE, ABC.BO for BSE)")
            out.append({"query": q, "found": False, "ticker": ticker, "how": how, "record": None, "live": False, "note": note})
    return out


def labelled(result, groups=None, settings=None, catalog=None):
    """One lookup result as a model reads it best: metrics by group under their screener labels, the
    screener's definition of each metric shown, the explain text and the AI write-ups -- the same shape
    as kg_api.market_metrics in the research library's database."""
    if not result["found"]:
        return {"query": result["query"], "found": False, "ticker": result["ticker"], "note": result["note"]}
    record = result["record"]
    catalog = catalog or market_api.catalog(settings)
    picked = market_api.select_groups(record, groups)
    by_field = {c["field"]: c for c in catalog}
    metrics, definitions = {}, {}
    for group, values in picked.items():
        if group == market_api.AI_GROUP:
            continue
        metrics[group] = {}
        for field, value in values.items():
            c = by_field.get(field, {"label": field, "description": ""})
            metrics[group][c["label"]] = value
            if group != "explain" and c.get("description"):
                definitions[c["label"]] = c["description"]
    out = {"query": result["query"], "found": True, "ticker": result["ticker"], "company": record.get("company_name"),
           "exchange": record.get("exchange"), "currency": record.get("currency"), "price_date": record.get("data_end"),
           "computed_at": record.get("as_of"), "computed_now": result["live"], "metrics": metrics, "definitions": definitions,
           "note": f"Computed by the user's stock screener from Yahoo Finance daily bars (not a live quote); prices in "
                   f"{record.get('currency') or '?'}. Cite figures as (screener, <price_date>)."}
    if market_api.AI_GROUP in picked:
        out["ai_views"] = picked[market_api.AI_GROUP]
    if result["how"] in ("similar", "yahoo"):
        out["matched_by"] = ("closest listed name" if result["how"] == "similar" else "Yahoo symbol search") \
            + f" -- check that {record.get('company_name')} is the company meant"
    return out
