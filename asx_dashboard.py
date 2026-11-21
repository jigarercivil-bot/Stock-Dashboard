#!/usr/bin/env python3
"""
Stock Dashboard — ASX + US
----------------------------------
Pulls end-of-day prices for every listed company on the ASX and the US exchanges
(NYSE, Nasdaq, NYSE American) for free via Yahoo Finance (yfinance), works out each
stock's daily move, 52-week range, liquidity and market-cap bucket, fetches
fundamentals for the day's movers, and writes one self-contained interactive HTML
dashboard with a market switch.

Usage
  python asx_dashboard.py                    # both markets
  python asx_dashboard.py --markets AU       # ASX only   (or --markets US)
  python asx_dashboard.py --open             # open the dashboard when done
  python asx_dashboard.py --limit 150        # quick test: first 150 tickers per market
  python asx_dashboard.py --no-fundamentals
  python asx_dashboard.py --mock             # synthetic data, no internet (layout testing)

Outputs (next to this script)
  output/asx_dashboard_latest.html
  output/asx_dashboard_YYYY-MM-DD.html
  output/history/<AU|US>_YYYY-MM-DD.csv      # daily snapshot of every stock's metrics
  cache/                                     # universe + fundamentals caches
"""
from __future__ import annotations

import argparse
import io
import json
import math
import random
import re
import sys
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
CACHE = HERE / "cache"
FUND_CACHE = CACHE / "fundamentals"
OUT = HERE / "output"
HIST = OUT / "history"
TEMPLATE = HERE / "dashboard_template.html"
WATCHLIST = HERE / "watchlist.txt"

# ---------------------------------------------------------------- settings ---
UNIVERSE_TTL_DAYS = 1          # re-download company lists daily
FUND_TTL_DAYS = 14             # refresh a stock's fundamentals after this many days
FUND_VERSION = 2               # bump to force a refetch of every cached fundamentals file
FUND_THREADS = 4               # parallel Yahoo requests (keep low to avoid rate limits)
FUND_TOP_N = 15                # fundamentals for top/bottom N per group
CHUNK = 150                    # tickers per yfinance batch
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
      "Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9"}

MARKETS = {
    "AU": {
        "name": "Australia", "exchange": "ASX", "currency": "A$", "suffix": ".AX",
        # Market-cap buckets in local currency (lower bound inclusive). Edit to taste.
        "caps": [("Mega", 100e9), ("Large", 10e9), ("Mid", 2e9), ("Small", 300e6), ("Micro", 0)],
        "indices": {"^AXJO": "S&P/ASX 200", "^AORD": "All Ordinaries", "^AXSO": "Small Ordinaries"},
        "fund_min_turnover": 50_000,    # fundamentals only for movers with this turnover today
        "hist_min_turnover": 10_000,    # keep a price chart for stocks averaging this turnover
        # Value finder universe: stocks at least this big and liquid get fundamentals
        "screen_min_mcap": 50e6, "screen_min_avgturn": 20_000,
        "fund_max_fetch": 600,          # new/refreshed fundamentals per run (rolling; rest come from cache)
        "ccy": "AUD",
        "classification": "GICS industry groups from the ASX company list",
    },
    "US": {
        "name": "United States", "exchange": "NYSE · Nasdaq", "currency": "US$", "suffix": "",
        "caps": [("Mega", 200e9), ("Large", 10e9), ("Mid", 2e9), ("Small", 300e6), ("Micro", 0)],
        "indices": {"^GSPC": "S&P 500", "^IXIC": "Nasdaq Composite", "^DJI": "Dow Jones", "^RUT": "Russell 2000"},
        "fund_min_turnover": 1_000_000,
        "hist_min_turnover": 100_000,
        "screen_min_mcap": 300e6, "screen_min_avgturn": 1_000_000,
        "fund_max_fetch": 800,
        "ccy": "USD",
        "classification": "Nasdaq sector and industry classification",
    },
}

AU_SOURCES = [
    "https://asx.api.markitdigital.com/asx-research/1.0/companies/directory/file",
    "https://www.asx.com.au/asx/research/ASXListedCompanies.csv",
]
US_SOURCE = "https://api.nasdaq.com/api/screener/stocks?tableonly=true&limit=25&offset=0&download=true"

# GICS industry group -> sector (ASX list)
GICS = {
    "energy": "Energy", "materials": "Materials",
    "capital goods": "Industrials", "commercial and professional services": "Industrials",
    "transportation": "Industrials",
    "automobiles and components": "Consumer Discretionary", "consumer durables and apparel": "Consumer Discretionary",
    "consumer services": "Consumer Discretionary", "retailing": "Consumer Discretionary",
    "consumer discretionary distribution and retail": "Consumer Discretionary",
    "food and staples retailing": "Consumer Staples", "consumer staples distribution and retail": "Consumer Staples",
    "food beverage and tobacco": "Consumer Staples", "household and personal products": "Consumer Staples",
    "health care equipment and services": "Health Care",
    "pharmaceuticals biotechnology and life sciences": "Health Care",
    "banks": "Financials", "diversified financials": "Financials", "financial services": "Financials",
    "insurance": "Financials",
    "software and services": "Information Technology", "technology hardware and equipment": "Information Technology",
    "semiconductors and semiconductor equipment": "Information Technology",
    "telecommunication services": "Communication Services", "media and entertainment": "Communication Services",
    "media": "Communication Services", "utilities": "Utilities", "real estate": "Real Estate",
    "equity real estate investment trusts reits": "Real Estate",
    "real estate management and development": "Real Estate",
}
# Nasdaq screener sector -> GICS-style sector name
US_SECTORS = {
    "technology": "Information Technology", "finance": "Financials", "health care": "Health Care",
    "consumer discretionary": "Consumer Discretionary", "consumer staples": "Consumer Staples",
    "industrials": "Industrials", "energy": "Energy", "real estate": "Real Estate", "utilities": "Utilities",
    "basic materials": "Materials", "telecommunications": "Communication Services",
}
OTHER = "Other / Not classified"
US_EXCLUDE = re.compile(r"warrant|\bunits?\b|\brights?\b|preferred|depositary shares,? each|% notes|notes due|"
                        r"debenture|subordinated|\bunit\b|acquisition corp", re.I)


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def norm(s: str) -> str:
    s = str(s or "").lower().replace("&", " and ")
    s = re.sub(r"[^a-z ]", " ", s)
    return " ".join(s.split())


def cap_bucket(mcap, buckets) -> str:
    if mcap is None or not np.isfinite(mcap) or mcap <= 0:
        return "Unknown"
    for name, lo in buckets:
        if mcap >= lo:
            return name
    return buckets[-1][0]


def fnum(x, nd=4):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(x):
        return None
    return round(x, nd)


def sig(x, digits=5):
    if x is None or not np.isfinite(x):
        return None
    return 0 if x == 0 else float(f"{x:.{digits}g}")


def to_num(series) -> pd.Series:
    return pd.to_numeric(series.astype(str).str.replace(r"[^0-9.eE\-]", "", regex=True), errors="coerce")


# ---------------------------------------------------------------- universe ---
def _parse_au(text: str) -> pd.DataFrame:
    lines = text.splitlines()
    start = next((i for i, ln in enumerate(lines[:10]) if "asx code" in ln.lower() or "company name" in ln.lower()), 0)
    df = pd.read_csv(io.StringIO("\n".join(lines[start:])))
    cols = {c.lower().strip(): c for c in df.columns}

    def pick(*names):
        for n in names:
            for k, orig in cols.items():
                if n in k:
                    return orig
        return None

    c_code, c_name, c_grp, c_cap = pick("asx code", "code"), pick("company name", "name"), pick("gics", "industry"), pick("market cap")
    out = pd.DataFrame({
        "code": df[c_code].astype(str).str.strip().str.upper(),
        "name": df[c_name].astype(str).str.strip().str.title() if c_name else "",
        "group": df[c_grp].astype(str).str.strip() if c_grp else "",
    })
    out["dir_mcap"] = to_num(df[c_cap]) if c_cap else np.nan
    out = out[out["code"].str.fullmatch(r"[A-Z0-9]{3,6}")].drop_duplicates("code")
    bad = out["group"].str.lower().isin(["nan", "not applic", "class pend", ""])
    out["sector"] = [OTHER if b else GICS.get(norm(g), OTHER) for g, b in zip(out["group"], bad)]
    out.loc[bad, "group"] = "Not classified"
    out["yahoo"] = out["code"] + ".AX"
    return out.reset_index(drop=True)


def _parse_us(payload: dict) -> pd.DataFrame:
    rows = (payload.get("data") or {}).get("rows") or []
    df = pd.DataFrame(rows)
    df["symbol"] = df["symbol"].astype(str).str.strip().str.upper()
    df = df[df["symbol"].str.fullmatch(r"[A-Z]{1,5}([/.][A-Z])?")]   # "^" = preferred share series, skipped
    df = df[~df["name"].astype(str).str.contains(US_EXCLUDE)]
    out = pd.DataFrame({
        "code": df["symbol"].str.replace("/", ".", regex=False),
        "name": df["name"].astype(str).str.replace(r"\s+(Common Stock|Class [A-C] Common Stock|Ordinary Shares|"
                                                   r"Class [A-C] Ordinary Shares|American Depositary Shares).*$",
                                                   "", regex=True).str.strip(),
        "group": df["industry"].astype(str).str.strip().replace({"": "Not classified", "nan": "Not classified"}),
        "sector": [US_SECTORS.get(norm(s), OTHER) for s in df["sector"]],
        "dir_mcap": to_num(df["marketCap"]),
    })
    out["yahoo"] = out["code"].str.replace(".", "-", regex=False)
    return out.drop_duplicates("code").reset_index(drop=True)


def load_universe(mkt: str) -> pd.DataFrame:
    import requests

    CACHE.mkdir(exist_ok=True)
    cache = CACHE / f"universe_{mkt}.csv"
    if cache.exists() and time.time() - cache.stat().st_mtime < UNIVERSE_TTL_DAYS * 86400:
        log(f"{mkt} universe: using today's cached company list")
        return pd.read_csv(cache, dtype={"code": str, "yahoo": str})
    try:
        if mkt == "AU":
            df = None
            for url in AU_SOURCES:
                try:
                    r = requests.get(url, headers=UA, timeout=60)
                    r.raise_for_status()
                    df = _parse_au(r.text)
                    if len(df) > 500:
                        break
                except Exception as e:  # noqa: BLE001
                    log(f"AU source failed ({url.split('/')[2]}): {e}")
        else:
            r = requests.get(US_SOURCE, headers=UA, timeout=90)
            r.raise_for_status()
            df = _parse_us(r.json())
        if df is not None and len(df) > 500:
            df.to_csv(cache, index=False)
            log(f"{mkt} universe: {len(df)} companies")
            return df
    except Exception as e:  # noqa: BLE001
        log(f"{mkt} universe download failed: {e}")
    if cache.exists():
        log(f"{mkt} universe: falling back to older cached list")
        return pd.read_csv(cache, dtype={"code": str, "yahoo": str})
    raise RuntimeError(f"Could not get the {mkt} company list")


# ------------------------------------------------------------------ prices ---
def download_prices(tickers: list[str]) -> dict[str, pd.DataFrame]:
    import yfinance as yf

    frames: dict[str, pd.DataFrame] = {}
    total = len(tickers)
    for i in range(0, total, CHUNK):
        batch = tickers[i:i + CHUNK]
        data = None
        for attempt in range(3):
            try:
                data = yf.download(batch, period="1y", interval="1d", auto_adjust=False,
                                   group_by="ticker", threads=True, progress=False)
                break
            except Exception as e:  # noqa: BLE001
                log(f"  batch retry {attempt + 1}: {e}")
                time.sleep(5 * (attempt + 1))
        if data is None or data.empty:
            continue
        if isinstance(data.columns, pd.MultiIndex):
            have = set(data.columns.get_level_values(0))
            for t in batch:
                if t in have:
                    df = data[t].dropna(subset=["Close"])
                    if len(df):
                        frames[t] = df
        elif len(batch) == 1:
            frames[batch[0]] = data.dropna(subset=["Close"])
        log(f"  prices {min(i + CHUNK, total)}/{total} (have {len(frames)})")
        time.sleep(1)
    for df in frames.values():
        if getattr(df.index, "tz", None) is not None:
            df.index = df.index.tz_localize(None)
    return frames


def compute_metrics(uni: pd.DataFrame, frames: dict[str, pd.DataFrame], cfg: dict):
    if not frames:
        return pd.DataFrame(), {"dates": [], "s": {}}, pd.Timestamp.today()
    last_dates = pd.Series({t: df.index[-1] for t, df in frames.items()})
    market_date = last_dates.mode().iloc[0]
    # shared calendar = longest series that ends on the market date
    cal = max((df.index for t, df in frames.items() if df.index[-1] == market_date), key=len)
    cal = cal[cal <= market_date]
    year_start = pd.Timestamp(year=market_date.year, month=1, day=1)
    rows, series = [], {}
    for _, u in uni.iterrows():
        df = frames.get(u["yahoo"])
        if df is None:
            continue
        df = df[df.index <= market_date]
        if len(df) < 2:
            continue
        c = df["Close"].astype(float).values
        h = df["High"].astype(float).values
        lo = df["Low"].astype(float).values
        v = df["Volume"].astype(float).fillna(0).values
        traded = df.index[-1] == market_date
        prev, last = c[-2], c[-1]
        chg = (last / prev - 1) * 100 if traded and prev > 0 else np.nan
        hi52, lo52 = np.nanmax(h[-252:]), np.nanmin(lo[-252:])
        prior_hi = np.nanmax(h[-252:-1]) if len(h) > 2 else np.nan
        prior_lo = np.nanmin(lo[-252:-1]) if len(lo) > 2 else np.nan
        vol = v[-1] if traded else 0.0
        avg_vol20 = np.nanmean(v[-21:-1]) if len(v) > 5 else np.nan
        avg_turn20 = np.nanmean((v * c)[-21:-1]) if len(v) > 5 else 0

        def back(n):
            return (last / c[-1 - n] - 1) * 100 if len(c) > n and c[-1 - n] > 0 else np.nan

        ytd_base = df[df.index < year_start]["Close"]
        ytd = (last / float(ytd_base.iloc[-1]) - 1) * 100 if len(ytd_base) else np.nan
        mcap = float(u["dir_mcap"]) if pd.notna(u.get("dir_mcap")) else np.nan
        rows.append({
            "code": u["code"], "yahoo": u["yahoo"], "name": u["name"], "group": u["group"], "sector": u["sector"],
            "price": fnum(last, 4), "prev": fnum(prev, 4), "chg": fnum(chg, 2), "traded": bool(traded),
            "vol": fnum(vol, 0), "turnover": fnum(vol * last, 0),
            "relvol": fnum(vol / avg_vol20, 2) if avg_vol20 and avg_vol20 > 0 else None,
            "hi52": fnum(hi52, 4), "lo52": fnum(lo52, 4),
            "offhi": fnum((last / hi52 - 1) * 100, 2) if hi52 > 0 else None,
            "offlo": fnum((last / lo52 - 1) * 100, 2) if lo52 > 0 else None,
            "newhi": bool(traded and np.isfinite(prior_hi) and h[-1] > prior_hi),
            "newlo": bool(traded and np.isfinite(prior_lo) and lo[-1] < prior_lo),
            "d5": fnum(back(5), 2), "m1": fnum(back(21), 2), "m3": fnum(back(63), 2), "ytd": fnum(ytd, 2),
            "mcap": fnum(mcap, 0), "cap": cap_bucket(mcap, cfg["caps"]),
            "avgturn": fnum(avg_turn20, 0),
        })
        if (avg_turn20 or 0) >= cfg["hist_min_turnover"]:
            aligned = df["Close"].reindex(cal)
            first = int(np.argmax(aligned.notna().values)) if aligned.notna().any() else len(cal)
            series[u["code"]] = [first] + [sig(x) for x in aligned.values[first:]]
    hist = {"dates": [d.strftime("%y%m%d") for d in cal], "s": series}
    return pd.DataFrame(rows), hist, market_date


# ------------------------------------------------------------ fundamentals ---
def pick_fund_targets(m: pd.DataFrame, cfg: dict, watch: set[str]) -> list[str]:
    liq = m[(m["traded"]) & (m["turnover"].fillna(0) >= cfg["fund_min_turnover"]) & m["chg"].notna()]
    liq = liq[liq["cap"] != "Mega"]
    codes: set[str] = set()

    def add(df):
        s = df.sort_values("chg")
        codes.update(s.head(FUND_TOP_N)["code"])
        codes.update(s.tail(FUND_TOP_N)["code"])

    add(liq)
    for _, g in liq.groupby("sector"):
        add(g)
    for _, g in liq.groupby("cap"):
        add(g)
    codes.update(liq[liq["newhi"] | liq["newlo"]]["code"])
    codes.update(c for c in watch if c in set(m["code"]))
    return sorted(codes)


def _row(df, keys):
    if df is None or getattr(df, "empty", True):
        return None
    for k in keys:
        if k in df.index:
            return df.loc[k].sort_index(ascending=False)  # newest first
    return None


def _stmt_series(df, keys, n):
    s = _row(df, keys)
    if s is None:
        return []
    s = s.dropna().sort_index()
    return [{"p": pd.Timestamp(d).strftime("%Y-%m"), "v": fnum(val, 0)} for d, val in s.tail(n).items()]


def _at(df, keys, i):
    s = _row(df, keys)
    if s is None or len(s) <= i:
        return None
    v = s.iloc[i]
    return float(v) if pd.notna(v) else None


def piotroski(inc, bs, cf):
    """Piotroski F-score from the two latest annual statements. Returns [[label, 1|0|None], ...]."""
    NI, REV, GP = ["Net Income", "Net Income Common Stockholders"], ["Total Revenue", "Operating Revenue"], ["Gross Profit"]
    TA, LTD = ["Total Assets"], ["Long Term Debt", "Long Term Debt And Capital Lease Obligation"]
    CA, CL = ["Current Assets"], ["Current Liabilities"]
    SH, OCF = ["Ordinary Shares Number", "Share Issued"], ["Operating Cash Flow", "Cash Flow From Continuing Operating Activities"]
    g = lambda df, k, i: _at(df, k, i)
    ni0, ni1, ta0, ta1 = g(inc, NI, 0), g(inc, NI, 1), g(bs, TA, 0), g(bs, TA, 1)
    ocf0 = g(cf, OCF, 0)
    roa0 = ni0 / ta0 if ni0 is not None and ta0 else None
    roa1 = ni1 / ta1 if ni1 is not None and ta1 else None

    def b(cond):
        return None if cond is None else int(bool(cond))

    def ratio(a, c):
        return a / c if a is not None and c else None

    ltd0, ltd1 = g(bs, LTD, 0), g(bs, LTD, 1)
    lev0 = ratio(ltd0 or 0.0, ta0) if ta0 else None   # no long-term debt line = no debt
    lev1 = ratio(ltd1 or 0.0, ta1) if ta1 else None
    cr0, cr1 = ratio(g(bs, CA, 0), g(bs, CL, 0)), ratio(g(bs, CA, 1), g(bs, CL, 1))
    sh0, sh1 = g(bs, SH, 0), g(bs, SH, 1)
    gm0, gm1 = ratio(g(inc, GP, 0), g(inc, REV, 0)), ratio(g(inc, GP, 1), g(inc, REV, 1))
    at0, at1 = ratio(g(inc, REV, 0), ta0), ratio(g(inc, REV, 1), ta1)
    return [
        ["Profitable (positive return on assets)", b(None if roa0 is None else roa0 > 0)],
        ["Positive operating cash flow", b(None if ocf0 is None else ocf0 > 0)],
        ["Return on assets improved", b(None if roa0 is None or roa1 is None else roa0 > roa1)],
        ["Cash flow higher than profit (earnings quality)", b(None if ocf0 is None or ni0 is None else ocf0 > ni0)],
        ["Long-term debt fell relative to assets", b(None if lev0 is None or lev1 is None else lev0 <= lev1)],
        ["Current ratio improved", b(None if cr0 is None or cr1 is None else cr0 > cr1)],
        ["No new shares issued", b(None if sh0 is None or sh1 is None else sh0 <= sh1 * 1.005)],
        ["Gross margin improved", b(None if gm0 is None or gm1 is None else gm0 > gm1)],
        ["Asset turnover improved", b(None if at0 is None or at1 is None else at0 > at1)],
    ]


INFO_FIELDS = ["trailingPE", "forwardPE", "priceToBook", "trailingEps", "forwardEps", "bookValue",
               "totalRevenue", "ebitda", "freeCashflow", "operatingCashflow", "totalDebt", "totalCash",
               "revenueGrowth", "earningsGrowth", "grossMargins", "operatingMargins", "profitMargins",
               "returnOnEquity", "returnOnAssets", "currentRatio", "quickRatio", "debtToEquity",
               "trailingAnnualDividendYield", "dividendYield", "payoutRatio", "trailingPegRatio",
               "targetMeanPrice", "numberOfAnalystOpinions", "heldPercentInsiders", "sharesOutstanding", "beta"]


class RateLimited(Exception):
    pass


def _fund_path(yahoo: str) -> Path:
    return FUND_CACHE / f"{yahoo.replace('^', '_')}.json"


def load_cached_fund(yahoo: str):
    """-> (data or None, fresh: bool)"""
    f = _fund_path(yahoo)
    if not f.exists():
        return None, False
    try:
        d = json.loads(f.read_text())
    except Exception:  # noqa: BLE001
        return None, False
    if d.get("v") != FUND_VERSION:
        return None, False
    return d, time.time() - f.stat().st_mtime < FUND_TTL_DAYS * 86400


def fetch_fundamentals(yahoo: str) -> dict:
    import yfinance as yf

    t = yf.Ticker(yahoo)
    try:
        info = t.info or {}
    except Exception as e:  # noqa: BLE001
        if "too many" in str(e).lower() or "rate" in str(e).lower():
            raise RateLimited(str(e))
        info = {}
    if len(info) < 8:
        raise RateLimited("empty response from Yahoo")
    out: dict = {"v": FUND_VERSION, "fetched": datetime.now().strftime("%Y-%m-%d")}
    for k in INFO_FIELDS:
        out[k] = fnum(info.get(k), 6)
    out["industry"] = info.get("industry")
    out["website"] = info.get("website")
    out["rec"] = info.get("recommendationKey")
    out["fin_ccy"] = info.get("financialCurrency")
    out["px_ccy"] = info.get("currency")
    summ = info.get("longBusinessSummary") or ""
    out["summary"] = (summ[:400] + "…") if len(summ) > 400 else summ
    px = fnum(info.get("currentPrice")) or fnum(info.get("regularMarketPrice")) or fnum(info.get("previousClose"))

    def per_share(ratio_key):
        # per-share value in the trading currency (works when the company reports in another currency)
        ratio = fnum(info.get(ratio_key), 8)   # Yahoo sometimes sends text such as "Infinity"
        return fnum(px / ratio, 6) if px and ratio and ratio > 0 else None

    out["eps_t"], out["feps_t"], out["bvps_t"] = per_share("trailingPE"), per_share("forwardPE"), per_share("priceToBook")
    stm = {}
    for attr in ["income_stmt", "quarterly_income_stmt", "balance_sheet", "cashflow"]:
        try:
            stm[attr] = getattr(t, attr)
        except Exception:  # noqa: BLE001
            stm[attr] = None
    inc, qinc, bs, cf = stm["income_stmt"], stm["quarterly_income_stmt"], stm["balance_sheet"], stm["cashflow"]
    for df, key, keys, n in [
        (inc, "rev_y", ["Total Revenue", "Operating Revenue"], 5),
        (inc, "ni_y", ["Net Income", "Net Income Common Stockholders"], 5),
        (qinc, "rev_q", ["Total Revenue", "Operating Revenue"], 8),
        (bs, "debt_y", ["Total Debt"], 5),
        (bs, "cash_y", ["Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments"], 5),
        (bs, "equity_y", ["Stockholders Equity", "Common Stock Equity"], 5),
        (cf, "fcf_y", ["Free Cash Flow"], 5),
    ]:
        try:
            out[key] = _stmt_series(df, keys, n)
        except Exception:  # noqa: BLE001
            out[key] = []
    try:
        fs = piotroski(inc, bs, cf)
        out["fs"] = fs
        known = [x[1] for x in fs if x[1] is not None]
        out["fscore"], out["fmax"] = sum(known), len(known)
    except Exception:  # noqa: BLE001
        out["fs"], out["fscore"], out["fmax"] = [], None, 0
    FUND_CACHE.mkdir(parents=True, exist_ok=True)
    _fund_path(yahoo).write_text(json.dumps(out))
    return out


def screen_universe(m: pd.DataFrame, cfg: dict) -> list[str]:
    u = m[(m["mcap"].fillna(0) >= cfg["screen_min_mcap"]) & (m["avgturn"].fillna(0) >= cfg["screen_min_avgturn"])]
    return u.sort_values("mcap", ascending=False)["code"].tolist()


def _reset_yahoo_session() -> None:
    """After a block, drop yfinance's saved cookie/crumb so the next request signs in afresh."""
    try:
        from yfinance.data import YfData
        d = YfData()
        for attr in ("_crumb", "_cookie"):
            if hasattr(d, attr):
                setattr(d, attr, None)
    except Exception:  # noqa: BLE001
        pass


def gather_fundamentals(m: pd.DataFrame, cfg: dict, watch: set[str], full: bool, gentle: dict | None = None) -> dict:
    """Load cached fundamentals and fetch missing/stale ones.

    gentle = {"delay": s, "minutes": m} fetches one company at a time with a pause between,
    backs off when Yahoo rate-limits, and stops after `minutes` (used on GitHub Actions).
    """
    ymap = dict(zip(m["code"], m["yahoo"]))
    movers = pick_fund_targets(m, cfg, watch)
    codes = list(dict.fromkeys(movers + screen_universe(m, cfg)))
    res, need = {}, []
    for c in codes:
        d, fresh = load_cached_fund(ymap[c])
        if d:
            res[c] = d
        if not fresh:
            need.append(c)
    mset = set(movers)
    need.sort(key=lambda c: (c in res, c not in mset))      # missing first, then today's movers
    budget = len(need) if full else min(len(need), cfg["fund_max_fetch"])
    todo = need[:budget]
    log(f"  fundamentals: {len(codes)} stocks in scope, {len(res)} cached, {len(need)} to fetch"
        + (f", trying up to {len(todo)} now" if budget < len(need) else ""))
    if not todo:
        return res

    if gentle:
        t_end = time.time() + gentle["minutes"] * 60
        delay, got, strikes, i = gentle["delay"], 0, 0, 0
        backoffs = [60, 180, 420]
        while i < len(todo):
            if time.time() > t_end:
                log(f"  time limit reached after {got} companies; the rest come on the next run")
                break
            c = todo[i]
            try:
                res[c] = fetch_fundamentals(ymap[c])
                got += 1
                strikes = 0
                i += 1
            except RateLimited:
                if strikes >= len(backoffs):
                    log(f"  Yahoo is still blocking after {got} companies; stopping until the next run")
                    break
                wait = backoffs[strikes]
                strikes += 1
                log(f"  Yahoo is rate-limiting; pausing {wait}s (attempt {strikes} of {len(backoffs)})")
                time.sleep(wait)
                _reset_yahoo_session()
                continue                      # retry the same company
            except Exception as e:  # noqa: BLE001
                log(f"  {c}: {e}")
                i += 1
            if got and got % 50 == 0 and strikes == 0:
                log(f"  fundamentals {got} fetched, {len(todo) - i} to go")
            time.sleep(delay)
        log(f"  fundamentals: {got} fetched this run, {len(res)} of {len(codes)} now covered")
        return res

    errors = 0
    for i in range(0, len(todo), 40):
        chunk = todo[i:i + 40]
        with ThreadPoolExecutor(FUND_THREADS) as ex:
            futs = {ex.submit(fetch_fundamentals, ymap[c]): c for c in chunk}
            for fu in as_completed(futs):
                c = futs[fu]
                try:
                    res[c] = fu.result()
                except RateLimited:
                    errors += 1
                except Exception as e:  # noqa: BLE001
                    log(f"  {c}: {e}")
        log(f"  fundamentals {min(i + 40, len(todo))}/{len(todo)}")
        if errors >= 15:
            log("  Yahoo is rate-limiting; stopping fundamentals here. The rest will come on the next run.")
            break
        if errors:
            time.sleep(10)
            errors = max(0, errors - 2)
    return res


def apply_fx(fund: dict, trade_ccy: str) -> None:
    """Attach fx = rate from each company's reporting currency to the trading currency."""
    import yfinance as yf

    def quote(pair):
        try:
            df = yf.download(f"{pair}=X", period="5d", progress=False, auto_adjust=False)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            c = df["Close"].dropna()
            return float(c.iloc[-1]) if len(c) else None
        except Exception:  # noqa: BLE001
            return None

    pairs = {d.get("fin_ccy") for d in fund.values() if d.get("fin_ccy") and d.get("fin_ccy") != trade_ccy}
    rates = {}
    usd_to_trade = 1.0 if trade_ccy == "USD" else None
    for ccy in pairs:
        r = quote(f"{ccy}{trade_ccy}")
        if r is None and ccy != "USD":          # no direct pair: go via US dollars
            to_usd = quote(f"{ccy}USD")
            if to_usd is None:
                usd_per = quote(f"USD{ccy}")
                to_usd = 1 / usd_per if usd_per else None
            if usd_to_trade is None:
                usd_to_trade = quote(f"USD{trade_ccy}")
            if to_usd and usd_to_trade:
                r = to_usd * usd_to_trade
        if r is None:
            log(f"  FX {ccy}->{trade_ccy} unavailable; those companies' ratios are left blank")
        else:
            rates[ccy] = r
    for d in fund.values():
        c = d.get("fin_ccy")
        d["fx"] = 1.0 if not c or c == trade_ccy else fnum(rates.get(c), 6)


def index_moves(indices: dict) -> list[dict]:
    import yfinance as yf

    out = []
    for sym, name in indices.items():
        try:
            df = yf.download(sym, period="1mo", interval="1d", progress=False, auto_adjust=False)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            c = df["Close"].dropna().astype(float)
            if len(c) >= 2:
                out.append({"sym": sym, "name": name, "last": fnum(c.iloc[-1], 2),
                            "chg": fnum((c.iloc[-1] / c.iloc[-2] - 1) * 100, 2),
                            "spark": [fnum(x, 1) for x in c.tail(22)]})
        except Exception as e:  # noqa: BLE001
            log(f"Index {sym} failed: {e}")
    return out


def read_watchlist() -> dict[str, set[str]]:
    w = {"AU": set(), "US": set()}
    if WATCHLIST.exists():
        for ln in WATCHLIST.read_text().splitlines():
            ln = ln.split("#")[0].strip().upper()
            if not ln:
                continue
            if ln.endswith(".AX"):
                w["AU"].add(ln[:-3])
            elif ln.startswith("US:"):
                w["US"].add(ln[3:].strip())
            else:
                w["AU"].add(ln)
                w["US"].add(ln)
    return w


def build_market(mkt: str, args, watch: set[str]) -> dict:
    cfg = MARKETS[mkt]
    log(f"=== {cfg['name']} ===")
    uni = load_universe(mkt)
    if args.limit:
        uni = uni.head(args.limit)
    log(f"Downloading 1 year of daily prices for {len(uni)} tickers…")
    frames = download_prices(uni["yahoo"].tolist())
    m, hist, mdate = compute_metrics(uni, frames, cfg)
    if m.empty:
        raise RuntimeError("no price data came back from Yahoo Finance")
    log(f"{mkt}: {len(m)} stocks, {int(m['traded'].sum())} traded on {mdate:%Y-%m-%d}")
    gentle = {"delay": args.fund_delay, "minutes": args.fund_minutes} if args.fund_minutes else None
    fund = {} if args.no_fundamentals else gather_fundamentals(m, cfg, watch, args.fundamentals_full, gentle)
    if fund:
        apply_fx(fund, cfg["ccy"])
    idx = index_moves(cfg["indices"])
    day = pd.Timestamp(mdate).strftime("%Y-%m-%d")
    m.to_csv(HIST / f"{mkt}_{day}.csv", index=False)
    src = ("Prices from Yahoo Finance (end of day, delayed). Company list and market cap from "
           + ("the ASX." if mkt == "AU" else "Nasdaq's stock screener (all NYSE, Nasdaq and NYSE American listings)."))
    return market_payload(mkt, m, hist, mdate, fund, idx, src)


def market_payload(mkt, m, hist, mdate, fund, idx, source) -> dict:
    cfg = MARKETS[mkt]
    clean = m.astype(object).where(pd.notna(m), None)
    return {
        "id": mkt, "name": cfg["name"], "exchange": cfg["exchange"], "currency": cfg["currency"],
        "marketDate": pd.Timestamp(mdate).strftime("%Y-%m-%d"), "source": source,
        "classification": cfg["classification"],
        "capBuckets": [{"name": n, "min": lo} for n, lo in cfg["caps"]],
        "cols": list(m.columns), "rows": clean.values.tolist(),
        "hist": hist, "fund": fund, "indices": idx,
    }


# -------------------------------------------------------------------- mock ---
def mock_market(mkt: str, n: int, seed: int) -> dict:
    cfg = MARKETS[mkt]
    rng = random.Random(seed)
    sectors = list(dict.fromkeys(GICS.values()))
    groups: dict[str, list[str]] = {}
    for g, s in GICS.items():
        groups.setdefault(s, []).append(g.title())
    days = pd.bdate_range(end=pd.Timestamp("2026-11-13"), periods=252)
    rows, series, seen = [], {}, set()
    top = cfg["caps"][0][1] * 3
    for i in range(n):
        code = "".join(rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(3 if mkt == "AU" else rng.choice([2, 3, 4])))
        if code in seen:
            continue
        seen.add(code)
        sec = rng.choice(sectors)
        mcap = math.exp(rng.uniform(math.log(5e6), math.log(top)))
        p0 = math.exp(rng.uniform(math.log(0.02 if mkt == "AU" else 1), math.log(90 if mkt == "AU" else 400)))
        vol = rng.uniform(0.01, 0.045)
        c = [p0]
        for _ in range(251):
            c.append(max(0.001, c[-1] * math.exp(rng.gauss(0.0003, vol))))
        c = np.array(c)
        h = c * (1 + np.abs(np.random.default_rng(i + seed).normal(0, vol / 2, 252)))
        lo = c * (1 - np.abs(np.random.default_rng(i + seed + 1).normal(0, vol / 2, 252)))
        last, prev = c[-1], c[-2]
        shares_traded = mcap / last * rng.uniform(0.0002, 0.012)
        rows.append({
            "code": code, "yahoo": code + cfg["suffix"],
            "name": f"{code.title()} {rng.choice(['Resources', 'Group', 'Holdings', 'Limited', 'Minerals', 'Health', 'Technologies', 'Energy', 'Bancorp'])}",
            "group": rng.choice(groups[sec]), "sector": sec,
            "price": fnum(last), "prev": fnum(prev), "chg": fnum((last / prev - 1) * 100, 2), "traded": True,
            "vol": fnum(shares_traded, 0), "turnover": fnum(shares_traded * last, 0), "relvol": fnum(rng.uniform(0.2, 6), 2),
            "hi52": fnum(h.max()), "lo52": fnum(lo.min()),
            "offhi": fnum((last / h.max() - 1) * 100, 2), "offlo": fnum((last / lo.min() - 1) * 100, 2),
            "newhi": bool(h[-1] > h[:-1].max()), "newlo": bool(lo[-1] < lo[:-1].min()),
            "d5": fnum((last / c[-6] - 1) * 100, 2), "m1": fnum((last / c[-22] - 1) * 100, 2),
            "m3": fnum((last / c[-64] - 1) * 100, 2), "ytd": fnum((last / c[-220] - 1) * 100, 2),
            "mcap": fnum(mcap, 0), "cap": cap_bucket(mcap, cfg["caps"]),
            "avgturn": fnum(shares_traded * last * rng.uniform(0.5, 1.5), 0),
        })
        series[code] = [0] + [sig(x) for x in c]
    m = pd.DataFrame(rows)
    fund = {}
    labels = [x[0] for x in piotroski(None, None, None)]
    for _, r in m.iterrows():
        if rng.random() < 0.18:
            continue
        px, mcap = r["price"], r["mcap"]
        shares = mcap / px
        rev = mcap * math.exp(rng.uniform(math.log(0.08), math.log(3)))
        om = rng.gauss(0.10, 0.13)
        pm = om - rng.uniform(0.0, 0.06)
        ni = rev * pm
        equity = mcap * rng.uniform(0.15, 1.3)
        debt = rev * rng.uniform(0, 1.6) * (0 if rng.random() < 0.15 else 1)
        cash = rev * rng.uniform(0.02, 0.6)
        eps = ni / shares
        fcf = ni * rng.uniform(0.2, 1.5) if ni > 0 else ni * rng.uniform(0.5, 1.5)
        g0 = rng.gauss(0.06, 0.15)
        fs = [[lab, int(rng.random() < (0.75 if pm > 0.05 else 0.4))] for lab in labels]
        fund[r["code"]] = {
            "v": FUND_VERSION, "fetched": "mock", "fx": 1.0, "fin_ccy": cfg["ccy"], "px_ccy": cfg["ccy"],
            "trailingPE": fnum(px / eps) if eps > 0 else None, "forwardPE": None, "priceToBook": fnum(mcap / equity),
            "eps_t": fnum(eps, 6) if eps > 0 else None, "trailingEps": fnum(eps, 6),
            "feps_t": fnum(eps * (1 + rng.gauss(0.1, 0.15)), 6) if eps > 0 else None,
            "bvps_t": fnum(equity / shares, 6), "totalRevenue": fnum(rev, 0), "ebitda": fnum(rev * (om + 0.06), 0),
            "freeCashflow": fnum(fcf, 0), "operatingCashflow": fnum(fcf * 1.3, 0),
            "totalDebt": fnum(debt, 0), "totalCash": fnum(cash, 0),
            "revenueGrowth": fnum(g0, 4), "earningsGrowth": fnum(g0 * 1.5 + rng.gauss(0, 0.15), 4),
            "grossMargins": fnum(min(0.95, om + rng.uniform(0.1, 0.45)), 4), "operatingMargins": fnum(om, 4),
            "profitMargins": fnum(pm, 4), "returnOnEquity": fnum(ni / equity, 4), "returnOnAssets": fnum(ni / (equity + debt + 1), 4),
            "currentRatio": fnum(rng.uniform(0.6, 3.6), 2), "quickRatio": None, "debtToEquity": fnum(debt / equity * 100, 2),
            "trailingAnnualDividendYield": fnum(rng.uniform(0, 0.07) if pm > 0 else 0, 4), "payoutRatio": None,
            "trailingPegRatio": None, "targetMeanPrice": fnum(px * rng.uniform(0.8, 1.5), 4) if rng.random() < 0.6 else None,
            "numberOfAnalystOpinions": rng.randint(1, 15), "heldPercentInsiders": fnum(rng.uniform(0, 0.4), 3),
            "sharesOutstanding": fnum(shares, 0), "beta": fnum(rng.uniform(0.4, 1.8), 2), "rec": None,
            "industry": "Mock industry", "website": None, "summary": "Synthetic company used to test the dashboard layout.",
            "rev_y": [{"p": f"{y}-06", "v": round(rev / (1 + g0) ** (2025 - y))} for y in range(2021, 2026)],
            "ni_y": [{"p": f"{y}-06", "v": round(ni * rng.uniform(0.6, 1.2))} for y in range(2021, 2026)],
            "rev_q": [{"p": f"2025-{mth:02d}", "v": round(rev / 4 * rng.uniform(0.85, 1.15))} for mth in (3, 6, 9, 12)],
            "debt_y": [{"p": f"{y}-06", "v": round(debt * rng.uniform(0.7, 1.3))} for y in range(2021, 2026)],
            "cash_y": [{"p": f"{y}-06", "v": round(cash * rng.uniform(0.7, 1.3))} for y in range(2021, 2026)],
            "equity_y": [{"p": f"{y}-06", "v": round(equity * rng.uniform(0.8, 1.1))} for y in range(2021, 2026)],
            "fcf_y": [{"p": f"{y}-06", "v": round(fcf * rng.uniform(0.6, 1.3))} for y in range(2021, 2026)],
            "fs": fs, "fscore": sum(x[1] for x in fs), "fmax": 9,
        }
    base = {"AU": 8000, "US": 5800}[mkt]
    idx = []
    for k, (s, nm) in enumerate(cfg["indices"].items()):
        b = base * (1 + 0.3 * k)
        sp = [fnum(b * (1 + rng.gauss(0, 0.004) + 0.0008 * j), 1) for j in range(22)]
        idx.append({"sym": s, "name": nm, "last": sp[-1], "chg": fnum((sp[-1] / sp[-2] - 1) * 100, 2), "spark": sp})
    hist = {"dates": [d.strftime("%y%m%d") for d in days], "s": series}
    return market_payload(mkt, m, hist, days[-1], fund, idx, "Synthetic test data — not real prices.")


# ------------------------------------------------------------------ output ---
def build_html(markets: list[dict]) -> str:
    payload = {"generated": datetime.now().strftime("%Y-%m-%d %H:%M"), "markets": markets}
    data = json.dumps(payload, separators=(",", ":"), allow_nan=False).replace("</", "<\\/")
    return TEMPLATE.read_text(encoding="utf-8").replace("/*__DATA__*/null", data)


def main():
    ap = argparse.ArgumentParser(description="Build the Stock Dashboard (ASX + US)")
    ap.add_argument("--markets", default="AU,US", help="comma list of AU, US (default both)")
    ap.add_argument("--limit", type=int, help="only the first N tickers per market (testing)")
    ap.add_argument("--no-fundamentals", action="store_true")
    ap.add_argument("--fundamentals-full", action="store_true",
                    help="fetch every missing/stale fundamental now instead of a rolling batch (slow)")
    ap.add_argument("--mock", action="store_true", help="synthetic data, no internet")
    ap.add_argument("--open", action="store_true", help="open the dashboard when done")
    ap.add_argument("--scheduled", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--fund-minutes", type=float, default=0,
                    help="gentle mode: fetch fundamentals one at a time for at most this many minutes per market")
    ap.add_argument("--fund-delay", type=float, default=1.5, help="gentle mode: seconds between companies")
    args = ap.parse_args()

    OUT.mkdir(exist_ok=True)
    HIST.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    wanted = [x.strip().upper() for x in args.markets.split(",") if x.strip().upper() in MARKETS]
    if not wanted:
        sys.exit("--markets must include AU and/or US")

    markets = []
    if args.mock:
        markets = [mock_market(mk, 900 if mk == "AU" else 1400, 7 + i) for i, mk in enumerate(wanted)]
    else:
        watch = read_watchlist()
        for mk in wanted:
            try:
                markets.append(build_market(mk, args, watch[mk]))
            except Exception as e:  # noqa: BLE001
                log(f"{mk} skipped: {e}")
        if not markets:
            sys.exit("No market could be built. Check your internet connection and try again in a few minutes.")

    html = build_html(markets)
    latest = OUT / "asx_dashboard_latest.html"
    latest.write_text(html, encoding="utf-8")
    if not args.mock:
        day = max(mk["marketDate"] for mk in markets)
        (OUT / f"asx_dashboard_{day}.html").write_text(html, encoding="utf-8")
    log(f"Done in {time.time() - t0:.0f}s -> {latest}  ({len(html) / 1e6:.1f} MB)")
    if args.open:
        webbrowser.open(latest.as_uri())


if __name__ == "__main__":
    main()
