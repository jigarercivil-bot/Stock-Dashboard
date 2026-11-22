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
import os
import random
import re
import sys
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
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
FUND_VERSION = 3               # files from older versions are still used, and refreshed first
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
               "targetMeanPrice", "numberOfAnalystOpinions", "heldPercentInsiders", "sharesOutstanding", "beta",
               "exDividendDate", "lastDividendValue", "dividendRate", "earningsTimestampStart",
               "mostRecentQuarter", "netIncomeToCommon"]


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
    if d.get("v") not in (2, FUND_VERSION):
        return None, False
    if d.get("v") != FUND_VERSION:
        d["v"] = FUND_VERSION          # usable now; marked stale so it gets the new fields soon
        return d, False
    if "mostRecentQuarter" not in d and d.get("fetched") != "mock":
        return d, False                # missing the latest-period fields: refresh soon
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
        (inc, "gp_y", ["Gross Profit"], 5),
        (inc, "oi_y", ["Operating Income", "Total Operating Income As Reported"], 5),
        (bs, "shares_y", ["Ordinary Shares Number", "Share Issued"], 5),
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


def gather_fundamentals(m: pd.DataFrame, cfg: dict, watch: set[str], full: bool, gentle: dict | None = None,
                        force: dict | None = None) -> tuple[dict, dict]:
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
    # companies with new reports, unless their saved figures were fetched after the report came out
    fset = {c for c in (force or {}) if c in set(codes) and str((res.get(c) or {}).get("fetched", "")) < force[c]}
    need += [c for c in codes if c in fset and c not in need]
    need.sort(key=lambda c: (c not in fset, c in res, c not in mset))   # new results, missing, then today's movers
    budget = len(need) if full else min(len(need), max(cfg["fund_max_fetch"], sum(c in fset for c in need)))
    todo = need[:budget]
    if fset:
        log(f"  fundamentals: {sum(c in fset for c in need)} companies with new results go first")
    log(f"  fundamentals: {len(codes)} stocks in scope, {len(res)} cached, {len(need)} to fetch"
        + (f", trying up to {len(todo)} now" if budget < len(need) else ""))
    stats = {"scope": len(codes), "due": len(need), "tried": 0, "fetched": 0, "failed": 0, "stopped": None}
    if not todo:
        stats["covered"] = len(res)
        return res, stats

    if gentle:
        t_end = time.time() + gentle["minutes"] * 60
        delay, got, strikes, i, failed = gentle["delay"], 0, 0, 0, 0
        backoffs = [60, 180, 420]
        while i < len(todo):
            if time.time() > t_end:
                log(f"  time limit reached after {got} companies; the rest come on the next run")
                stats["stopped"] = "time"
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
                    stats["stopped"] = "yahoo"
                    break
                wait = backoffs[strikes]
                strikes += 1
                log(f"  Yahoo is rate-limiting; pausing {wait}s (attempt {strikes} of {len(backoffs)})")
                time.sleep(wait)
                _reset_yahoo_session()
                continue                      # retry the same company
            except Exception as e:  # noqa: BLE001
                log(f"  {c}: {e}")
                failed += 1
                i += 1
            if got and got % 50 == 0 and strikes == 0:
                log(f"  fundamentals {got} fetched, {len(todo) - i} to go")
            time.sleep(delay)
        log(f"  fundamentals: {got} fetched this run, {len(res)} of {len(codes)} now covered")
        stats.update(tried=i, fetched=got, failed=failed, covered=len(res))
        return res, stats

    errors, got, failed, tried = 0, 0, 0, 0
    for i in range(0, len(todo), 40):
        chunk = todo[i:i + 40]
        with ThreadPoolExecutor(FUND_THREADS) as ex:
            futs = {ex.submit(fetch_fundamentals, ymap[c]): c for c in chunk}
            for fu in as_completed(futs):
                c = futs[fu]
                tried += 1
                try:
                    res[c] = fu.result()
                    got += 1
                except RateLimited:
                    errors += 1
                    failed += 1
                except Exception as e:  # noqa: BLE001
                    log(f"  {c}: {e}")
                    failed += 1
        log(f"  fundamentals {min(i + 40, len(todo))}/{len(todo)}")
        if errors >= 15:
            log("  Yahoo is rate-limiting; stopping fundamentals here. The rest will come on the next run.")
            stats["stopped"] = "yahoo"
            break
        if errors:
            time.sleep(10)
            errors = max(0, errors - 2)
    stats.update(tried=tried, fetched=got, failed=failed, covered=len(res))
    return res, stats


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



# ------------------------------------------------- ASX announcements/halts ---
ASX_ANN_URL = "https://asx.api.markitdigital.com/asx-research/1.0/markets/announcements"
ANN_KINDS = [  # (tag, keywords in headline or announcement type), first match wins
    ("Trading halt", ["trading halt", "pause in trading"]),
    ("Suspension", ["suspension from official quotation", "suspension from quotation", "voluntary suspension"]),
    ("Reinstatement", ["reinstatement to official quotation", "reinstatement to quotation"]),
    ("Price query", ["price query", "aware letter", "response to asx query", "asx query"]),
    ("Takeover", ["takeover", "scheme ", "scheme implementation", "bidder", "target's statement", "merger", "acquisition"]),
    ("Capital raising", ["placement", "capital raising", "entitlement offer", "share purchase plan", "rights issue", "institutional offer"]),
    ("Exploration", ["drill", "assay", "intercept", "exploration", "mineral resource", "ore reserve", "mineralis", "mineraliz",
                     "high grade", "high-grade", "hits of", "metres at", "m at "]),
    ("AGM", ["agm", "annual general meeting", "chair's address", "chairman's address", "results of meeting"]),
    ("Results", ["results", "half year", "half-year", "full year", "annual report", "appendix 4d", "appendix 4e",
                 "quarterly activities", "quarterly cash", "appendix 4c", "appendix 5b", "investor presentation",
                 "preliminary final", "financial report"]),
    ("Guidance", ["guidance", "outlook", "trading update", "earnings update", "market update"]),
    ("Contract", ["contract", "agreement", "partnership", "award", "offtake", "purchase order"]),
    ("Dividend", ["dividend", "distribution"]),
]


def ann_kind(head: str, types: list) -> str:
    text = " " + re.sub(r"[^a-z0-9' ]", " ", (head + " " + " ".join(types or [])).lower()) + " "
    for tag, kws in ANN_KINDS:
        if any(re.search(r"\b" + re.escape(k), text) for k in kws):
            return tag
    return "Other"


FIN_REPORTS = [  # (label, keywords) for reports that bring new financial statements; first match wins
    ("Half-year report", ["appendix 4d", "half year report", "half-year report", "half yearly report", "half year accounts",
                          "half-year accounts", "half yearly accounts", "interim report", "half year financial report",
                          "half-year financial report", "half year results", "half-year results", "hy results", "1h results"]),
    ("Full-year results", ["appendix 4e", "preliminary final", "full year results", "full-year results", "fy results",
                           "full year report", "full-year report", "full year accounts", "full year statutory accounts"]),
    ("Annual report", ["annual report", "annual financial report"]),
]
FIN_SKIP = ["notice of", "proxy", "letter to shareholders", "online", "dispatch", "appendix 4g", "corporate governance",
            "date of", "teleconference", "webcast", "conference call", "briefing", "timing"]


def financial_report_kind(head: str, types: list) -> str | None:
    """Label for announcements that carry new financial statements (not quarterly activity or cash reports)."""
    text = " " + re.sub(r"[^a-z0-9' ]", " ", (head + " " + " ".join(types or [])).lower()) + " "
    text = re.sub(r"\s+", " ", text)
    if any(k in text for k in FIN_SKIP) or "quarterly" in text or "appendix 4c" in text or "appendix 5b" in text:
        return None
    for label, kws in FIN_REPORTS:
        if any(k in text for k in kws):
            return label
    if "presentation" not in text:   # short headlines such as "1H FY27 Results" or "FY26 Results"
        if re.search(r"\b(1h|h1|hy)\s?(fy)?\s?\d{2,4} (results|result|financial report)", text):
            return "Half-year report"
        if re.search(r"\b(fy|2h|h2)\s?\d{2,4} (results|result|financial report)", text):
            return "Full-year results"
    return None


def asx_announcements(market_date, days_back: int = 0) -> list[dict]:
    """Announcements from the market date (Sydney time), newest first. With days_back, also earlier days
    (those carry a "day" field and are only used to rebuild the list of trading halts)."""
    import requests
    from zoneinfo import ZoneInfo

    syd = ZoneInfo("Australia/Sydney")
    day = pd.Timestamp(market_date).date()
    first = (pd.Timestamp(market_date) - pd.Timedelta(days=days_back)).date()
    out, seen = [], set()
    for page in range(40 + 15 * days_back):
        try:
            r = requests.get(ASX_ANN_URL, params={"itemsPerPage": 100, "page": page}, headers=UA, timeout=45)
            r.raise_for_status()
            items = (r.json().get("data") or {}).get("items") or []
        except Exception as e:  # noqa: BLE001
            log(f"  ASX announcements page {page} failed: {e}")
            break
        if not items:
            break
        older = False
        for it in items:
            try:
                t = datetime.fromisoformat(it["date"].replace("Z", "+00:00")).astimezone(syd)
            except Exception:  # noqa: BLE001
                continue
            if t.date() < first:
                older = True
                continue
            if t.date() > day:
                continue
            key = it.get("documentKey") or (it.get("symbol"), it.get("headline"), it.get("date"))
            if key in seen:
                continue
            seen.add(key)
            head = (it.get("headline") or "").strip()
            types = it.get("announcementTypes") or []
            a = {"t": t.strftime("%H:%M"), "code": (it.get("symbol") or "").upper(), "head": head,
                 "kind": ann_kind(head, types), "ps": bool(it.get("isPriceSensitive"))}
            fin = financial_report_kind(head, types)
            if fin:
                a["fin"], a["date"] = fin, t.strftime("%Y-%m-%d")
            if t.date() != day:
                if a["kind"] not in ("Trading halt", "Suspension", "Reinstatement") and not fin:
                    continue
                a["day"] = t.strftime("%Y-%m-%d")
            out.append(a)
        if older:
            break
        time.sleep(0.4)
    today = [a for a in out if "day" not in a]
    log(f"  ASX announcements: {len(today)} on {day}, {sum(a['ps'] for a in today)} price sensitive")
    return out


def update_halts(anns: list[dict], m: pd.DataFrame, market_date) -> list[dict]:
    """Keep a running list of halted/suspended ASX stocks between runs."""
    CACHE.mkdir(exist_ok=True)
    f = CACHE / "halts.json"
    try:
        halts = json.loads(f.read_text()) if f.exists() else {}
    except Exception:  # noqa: BLE001
        halts = {}
    day = pd.Timestamp(market_date).strftime("%Y-%m-%d")
    for a in sorted(anns, key=lambda a: (a.get("day", day), a["t"])):
        if a["kind"] in ("Trading halt", "Suspension"):
            halts[a["code"]] = {"code": a["code"], "since": a.get("day", day), "time": a["t"], "head": a["head"],
                                "kind": "suspension" if a["kind"] == "Suspension" else "halt"}
        elif a["kind"] == "Reinstatement":
            halts.pop(a["code"], None)
    traded = set(m.loc[m["traded"] & (m["vol"].fillna(0) > 0), "code"]) if len(m) else set()
    bdays = pd.bdate_range(day, periods=4)
    for code in list(halts):
        h = halts[code]
        # traded on a later day than the halt: it has resumed
        if code in traded and h["since"] < day:
            halts.pop(code)
            continue
        # a halt lasts at most two trading days; if still quiet it usually becomes a suspension
        if h["kind"] == "halt":
            start = pd.Timestamp(h["since"])
            h["resumeBy"] = pd.bdate_range(start, periods=3)[-1].strftime("%Y-%m-%d")
            if h["resumeBy"] < day:
                h["kind"] = "suspension"
                h.pop("resumeBy", None)
        if (pd.Timestamp(day) - pd.Timestamp(h["since"])).days > 120:
            halts.pop(code)
    f.write_text(json.dumps(halts))
    log(f"  halts: {sum(h['kind'] == 'halt' for h in halts.values())} trading halts, "
        f"{sum(h['kind'] == 'suspension' for h in halts.values())} suspensions")
    return sorted(halts.values(), key=lambda h: (h["kind"] != "halt", h["since"]), reverse=False)


# ---------------------------------------------------------- Berkshire 13F ---
BRK_CIK = "0001067983"
# The SEC asks every automated request to name a contact ("Name email"). Set SEC_CONTACT
# (a GitHub Actions variable, or an environment variable on your PC) to your own.
SEC_UA = {"User-Agent": os.environ.get("SEC_CONTACT", "").strip() or "Stock-Dashboard research tool",
          "Accept-Encoding": "gzip, deflate"}
BRK_TTL_DAYS = 7


def _sec_get(url: str):
    import requests

    r = requests.get(url, headers=SEC_UA, timeout=60)
    r.raise_for_status()
    time.sleep(0.25)                       # SEC asks for no more than 10 requests a second
    return r


def _parse_13f_table(xml_text: str) -> dict:
    """Aggregate a 13F information table by CUSIP -> {name, cls, cusip, value, shares}."""
    import xml.etree.ElementTree as ET

    root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    out: dict = {}
    for node in root.iter():
        if not node.tag.endswith("infoTable"):
            continue
        get = {c.tag.split("}")[-1]: c for c in node}
        def txt(k):
            return (get[k].text or "").strip() if k in get and get[k] is not None else ""
        cusip = txt("cusip").upper()
        shares = 0.0
        if "shrsOrPrnAmt" in get:
            for c in get["shrsOrPrnAmt"]:
                if c.tag.endswith("sshPrnamt"):
                    shares = float(c.text or 0)
        if txt("putCall"):
            continue                       # skip options
        val = float(txt("value") or 0)
        e = out.setdefault(cusip, {"name": txt("nameOfIssuer"), "cls": txt("titleOfClass"),
                                   "cusip": cusip, "value": 0.0, "shares": 0.0})
        e["value"] += val
        e["shares"] += shares
    return out


def _latest_13f_filings(n: int = 2) -> list[dict]:
    sub = _sec_get(f"https://data.sec.gov/submissions/CIK{BRK_CIK}.json").json()
    rec = sub["filings"]["recent"]
    rows = []
    for form, acc, filed, period in zip(rec["form"], rec["accessionNumber"], rec["filingDate"], rec["reportDate"]):
        if form in ("13F-HR", "13F-HR/A"):
            rows.append({"form": form, "acc": acc, "filed": filed, "period": period})
    # keep one filing per quarter, the latest (an amendment can restate a quarter)
    by_q = {}
    for r in sorted(rows, key=lambda r: r["filed"]):
        if r["form"] == "13F-HR" or r["period"] not in by_q:
            by_q[r["period"]] = r
    return [by_q[k] for k in sorted(by_q, reverse=True)[:n]]


def _filing_table(acc: str) -> str:
    cik = str(int(BRK_CIK))
    base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc.replace('-', '')}"
    idx = _sec_get(f"{base}/index.json").json()
    names = [i["name"] for i in idx["directory"]["item"] if i["name"].lower().endswith(".xml")]
    names = [n for n in names if "primary_doc" not in n.lower()] or names
    for n in names:
        txt = _sec_get(f"{base}/{n}").text
        if "infoTable" in txt:
            return txt
    raise RuntimeError("no information table in filing " + acc)


def _figi_tickers(cusips: list[str]) -> dict:
    import requests

    out = {}
    for i in range(0, len(cusips), 10):
        batch = cusips[i:i + 10]
        try:
            r = requests.post("https://api.openfigi.com/v3/mapping", timeout=60,
                              json=[{"idType": "ID_CUSIP", "idValue": c, "exchCode": "US"} for c in batch])
            if r.status_code == 429:
                time.sleep(30)
                r = requests.post("https://api.openfigi.com/v3/mapping", timeout=60,
                                  json=[{"idType": "ID_CUSIP", "idValue": c, "exchCode": "US"} for c in batch])
            r.raise_for_status()
            for c, res in zip(batch, r.json()):
                d = (res.get("data") or [{}])[0]
                if d.get("ticker"):
                    out[c] = d["ticker"].replace("/", ".").upper()
        except Exception as e:  # noqa: BLE001
            log(f"  OpenFIGI lookup failed: {e}")
        time.sleep(2.6)                    # 25 requests a minute without a key
    return out


def _name_key(s: str) -> str:
    s = re.sub(r"[^a-z0-9 ]", " ", str(s).lower())
    stop = {"inc", "corp", "corporation", "co", "company", "ltd", "limited", "plc", "the", "class", "com",
            "new", "hldgs", "holdings", "group", "del", "sa", "ag", "nv", "cl", "a", "b"}
    return " ".join(w for w in s.split() if w not in stop)


def berkshire_holdings(us_rows: pd.DataFrame | None) -> dict | None:
    """Berkshire Hathaway's latest 13F holdings, with change against the previous quarter."""
    CACHE.mkdir(exist_ok=True)
    cf = CACHE / "berkshire.json"
    if cf.exists() and time.time() - cf.stat().st_mtime < BRK_TTL_DAYS * 86400:
        try:
            return json.loads(cf.read_text())
        except Exception:  # noqa: BLE001
            pass
    try:
        filings = _latest_13f_filings(2)
        if not filings:
            raise RuntimeError("no 13F filings found")
        cur = _parse_13f_table(_filing_table(filings[0]["acc"]))
        prev = _parse_13f_table(_filing_table(filings[1]["acc"])) if len(filings) > 1 else {}
        tick = _figi_tickers(sorted(set(cur) | set(prev)))
        names = {}
        if us_rows is not None and len(us_rows):
            for code, nm in zip(us_rows["code"], us_rows["name"]):
                names.setdefault(_name_key(nm), code)
        rows = []
        tot = sum(h["value"] for h in cur.values()) or 1
        for cusip in sorted(set(cur) | set(prev), key=lambda c: -cur.get(c, {}).get("value", 0)):
            h, p = cur.get(cusip), prev.get(cusip)
            base = h or p
            t = tick.get(cusip) or names.get(_name_key(base["name"]))
            sh, psh = (h or {}).get("shares", 0.0), (p or {}).get("shares", 0.0)
            change = "sold" if not h else "new" if not p else ("added" if sh > psh * 1.001 else "reduced" if sh < psh * 0.999 else "unchanged")
            rows.append({"cusip": cusip, "ticker": t, "name": base["name"].title(), "cls": base["cls"],
                         "shares": round(sh), "prevShares": round(psh), "value": round((h or {}).get("value", 0)),
                         "weight": fnum((h or {}).get("value", 0) / tot * 100, 3), "change": change})
        data = {"period": filings[0]["period"], "filed": filings[0]["filed"],
                "prevPeriod": filings[1]["period"] if len(filings) > 1 else None,
                "accession": filings[0]["acc"], "fetched": datetime.now().strftime("%Y-%m-%d"),
                "total": round(tot), "rows": rows}
        cf.write_text(json.dumps(data))
        log(f"Berkshire 13F: {len([r for r in rows if r['change'] != 'sold'])} holdings for {data['period']}, filed {data['filed']}")
        return data
    except Exception as e:  # noqa: BLE001
        log(f"Berkshire 13F fetch failed: {e}")
        if cf.exists():
            try:
                return json.loads(cf.read_text())
            except Exception:  # noqa: BLE001
                return None
        return None


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
    day = pd.Timestamp(mdate).strftime("%Y-%m-%d")
    # New financial reports (ASX announcements / SEC filings) move those companies to the front of the refresh
    anns, events, rstate = None, [], load_results(mkt)
    first_res = not _results_path(mkt).exists()
    try:
        if mkt == "AU":
            first_time = not (CACHE / "halts.json").exists()
            anns = asx_announcements(mdate, days_back=7 if (first_time or first_res) else 0)
            events = [{"code": a["code"], "date": a["date"], "kind": a["fin"],
                       "url": f"https://www.asx.com.au/markets/trade-our-cash-market/announcements.{a['code'].lower()}"}
                      for a in anns if a.get("fin")]
            log(f"  financial reports lodged: {len(events)}")
        else:
            events = sec_results(mdate, days_back=7 if first_res else 3)
    except Exception as e:  # noqa: BLE001
        log(f"  results check skipped: {e}")
    rstate = merge_results(rstate, events, day)
    force = results_due(rstate, day)
    fund, fstats = ({}, None) if args.no_fundamentals else gather_fundamentals(m, cfg, watch, args.fundamentals_full, gentle, force)
    if fund and mkt == "AU" and not args.no_fundamentals:
        try:
            epm = float(os.environ.get("EP_MINUTES", "25"))
            for c, v in gather_eulerpool(list(fund), fund, set(force), epm).items():
                fund[c]["ep"] = v
        except Exception as e:  # noqa: BLE001
            log(f"  Eulerpool skipped: {e}")
    if fund and mkt == "US" and not args.no_fundamentals:
        try:
            for c, v in gather_sec_quarters(list(fund), force, float(os.environ.get("SECQ_MINUTES", "30"))).items():
                fund[c]["q"] = v
        except Exception as e:  # noqa: BLE001
            log(f"  SEC quarters skipped: {e}")
    if fund:
        rstate = check_results(rstate, fund, day)
        save_results(mkt, rstate)
    if fund:
        apply_fx(fund, cfg["ccy"])
    idx = index_moves(cfg["indices"])
    m.to_csv(HIST / f"{mkt}_{day}.csv", index=False)
    src = ("Prices from Yahoo Finance (end of day, delayed). Company list and market cap from "
           + ("the ASX." if mkt == "AU" else "Nasdaq's stock screener (all NYSE, Nasdaq and NYSE American listings)."))
    stats = {"listed": len(uni), "priced": len(m), "traded": int(m["traded"].sum()), "fund": fstats}
    payload = market_payload(mkt, m, hist, mdate, fund, idx, src, stats)
    payload["results"] = results_payload(rstate)
    if mkt == "US":
        payload["berkshire"] = berkshire_holdings(m)
    if mkt == "AU":
        try:
            if anns is None:
                raise RuntimeError("announcements unavailable")
            payload["announcements"] = [a for a in anns if "day" not in a]
            payload["halts"] = update_halts(anns, m, mdate)
        except Exception as e:  # noqa: BLE001
            log(f"  ASX announcements skipped: {e}")
    return payload


# ------------------------------------------------ Eulerpool ASX half-years ---
# Yahoo has no half-year profit and loss for ASX companies. Eulerpool's free API does, but our
# tests found some companies wrong or months out of date, so every figure is cross-checked:
# halves must add up to the full year, and full years are compared with Yahoo's.
EP_BASE = "https://api.eulerpool.com/api/1/equity"
EP_DIR = CACHE / "eulerpool"
EP_TTL_DAYS = 14
EP_TOL_SUM = 0.03       # two halves within 3% of the full-year total
EP_TOL_SOURCES = 0.05   # Eulerpool and Yahoo full years within 5%


def _ep_key() -> str:
    return os.environ.get("EULERPOOL_API_KEY", "").strip()


def _ep_get(session, path: str, params: dict | None = None):
    p = dict(params or {})
    p["token"] = _ep_key()
    r = session.get(f"{EP_BASE}/{path}", params=p, timeout=45)
    if r.status_code == 429:
        raise RateLimited("Eulerpool rate limit")
    if r.status_code in (401, 402, 403):
        raise PermissionError(f"Eulerpool refused ({r.status_code})")
    r.raise_for_status()
    time.sleep(0.4)
    return r.json()


def _ep_isin(session, code: str, isins: dict) -> str | None:
    if code in isins:
        return isins[code]
    j = _ep_get(session, "search", {"q": f"{code}.AX"})
    rows = j.get("results") if isinstance(j, dict) else j
    isin = None
    for r in rows or []:
        if isinstance(r, dict) and str(r.get("ticker", "")).upper() == f"{code}.AX":
            isin = r.get("isin")
            break
    isins[code] = isin                      # remember misses too, so they aren't searched daily
    return isin


def _ep_rows(j) -> list[dict]:
    """Reported periods only: no analyst estimates (period ends in "e") and no empty placeholder rows."""
    out = []
    for r in j if isinstance(j, list) else []:
        p = str(r.get("period") or "")
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", p):
            continue
        out.append(r)
    return sorted(out, key=lambda r: r["period"])


def fetch_eulerpool(session, isin: str) -> dict:
    iq = _ep_rows(_ep_get(session, f"income-statement-quarterly/{isin}"))
    cq = {r["period"]: r for r in _ep_rows(_ep_get(session, f"cashflow-statement-quarterly/{isin}"))}
    bq = {r["period"]: r for r in _ep_rows(_ep_get(session, f"balance-sheet-quarterly/{isin}"))}
    ia = _ep_rows(_ep_get(session, f"incomestatement/{isin}"))
    prof = _ep_get(session, f"profile/{isin}")
    halves = []
    for r in iq[-10:]:
        rev, ni = fnum(r.get("revenue"), 3), fnum(r.get("netIncome"), 3)
        if not rev and not ni:
            continue                        # placeholder rows with zeros
        cf, bs = cq.get(r["period"], {}), bq.get(r["period"], {})
        halves.append({"p": r["period"], "rev": rev, "ni": ni, "ocf": fnum(cf.get("netOperatingCashFlow"), 3),
                       "cash": fnum(bs.get("cash"), 3), "debt": fnum(bs.get("totalDebt"), 3)})
    years = [{"p": r["period"], "rev": fnum(r.get("revenue"), 3), "ni": fnum(r.get("netIncome"), 3)} for r in ia[-4:]]
    return {"fetched": datetime.now().strftime("%Y-%m-%d"), "isin": isin,
            "ccy": (prof or {}).get("currency") if isinstance(prof, dict) else None, "halves": halves, "years": years}


def _months_before(p: str, n: int) -> str:
    return (pd.Period(p[:7], "M") - n).strftime("%Y-%m")


def check_eulerpool(ep: dict, f: dict | None) -> dict:
    """Decide which Eulerpool figures can be shown. Returns a compact record for the page."""
    halves = {h["p"][:7]: h for h in ep.get("halves") or []}
    years = ep.get("years") or []
    checks = []
    for y in years[-3:]:
        ym = y["p"][:7]
        h2, h1 = halves.get(ym), halves.get(_months_before(ym, 6))
        if not (h1 and h2):
            continue
        for key in ("rev", "ni"):
            tot = y.get(key)
            a, b = h1.get(key), h2.get(key)
            if tot is None or a is None or b is None or abs(tot) < 1e-9:
                continue
            checks.append(abs(a + b - tot) / abs(tot) <= EP_TOL_SUM)
            break
    ok = bool(checks) and checks[-1]       # the most recent full year must add up
    # Compare full years with Yahoo's
    disagree = None
    yf_rev = {p["p"]: p["v"] for p in (f or {}).get("rev_y") or [] if p.get("v") is not None}
    for y in reversed(years):
        ym, v = y["p"][:7], y.get("rev")
        if ym in yf_rev and v:
            yv = yf_rev[ym] / 1e6
            if yv and abs(v - yv) / max(abs(v), abs(yv)) > EP_TOL_SOURCES:
                disagree = {"p": ym, "ep": v, "yf": round(yv, 3)}
            break
    ep_last = max([h["p"] for h in ep.get("halves") or []] + [y["p"] for y in years] or [""])[:7]
    yf_last = (fund_period_end(f) or "")[:7] if f else ""
    rows = [h for h in sorted(halves.values(), key=lambda h: h["p"])][-5:] if ok else []
    return {"ok": ok, "checked": len(checks), "rows": rows, "ccy": ep.get("ccy"), "fetched": ep.get("fetched"),
            "disagree": disagree, "behind": bool(yf_last and ep_last and ep_last < yf_last),
            "newer": bool(ok and yf_last and rows and rows[-1]["p"][:7] > yf_last)}


def gather_eulerpool(codes: list[str], fund: dict, force: set[str], minutes: float) -> dict:
    """Fetch (time-limited) and cross-check Eulerpool half-years for ASX companies with financials."""
    if not _ep_key():
        return {}
    import requests

    EP_DIR.mkdir(parents=True, exist_ok=True)
    fi = EP_DIR / "_isin.json"
    try:
        isins = json.loads(fi.read_text())
    except Exception:  # noqa: BLE001
        isins = {}
    have, need = {}, []
    for c in codes:
        fp = EP_DIR / f"{c}.json"
        d = None
        if fp.exists():
            try:
                d = json.loads(fp.read_text())
            except Exception:  # noqa: BLE001
                d = None
        if d:
            have[c] = d
        age = (time.time() - fp.stat().st_mtime) / 86400 if fp.exists() else 1e9
        if not d or age > EP_TTL_DAYS or (c in force and age > 0.5):
            need.append(c)
    need.sort(key=lambda c: (c not in force, c in have))
    s, t_end, got, failed = requests.Session(), time.time() + minutes * 60, 0, 0
    for c in need:
        if time.time() > t_end:
            log(f"  Eulerpool: time limit reached after {got}; the rest come on the next runs")
            break
        try:
            isin = _ep_isin(s, c, isins)
            if not isin:
                continue
            d = fetch_eulerpool(s, isin)
            (EP_DIR / f"{c}.json").write_text(json.dumps(d))
            have[c] = d
            got += 1
        except (RateLimited, PermissionError) as e:
            log(f"  Eulerpool stopped: {e}")
            break
        except Exception as e:  # noqa: BLE001
            failed += 1
            if failed <= 5:
                log(f"  Eulerpool {c}: {e}")
    fi.write_text(json.dumps(isins))
    out = {c: check_eulerpool(d, fund.get(c)) for c, d in have.items() if c in fund}
    for c in os.environ.get("EP_LOG_CODES", "BHP,CBA,WTC,PLS,SFR,AIS,29M,A1M,CXO,ZIP").split(","):
        v = out.get(c.strip())
        if v:
            last = v["rows"][-1]["p"] if v["rows"] else "-"
            log(f"    {c}: {'pass' if v['ok'] else 'hidden'}, latest half {last}, behind={v['behind']}, newer={v['newer']}, disagree={v['disagree']}")
    ok = sum(v["ok"] for v in out.values())
    log(f"  Eulerpool: {got} fetched this run, {len(out)} on file, {ok} pass the half-year checks, "
        f"{sum(bool(v['disagree']) for v in out.values())} disagree with Yahoo")
    return out


# ------------------------------------------------ US quarterly results (SEC) ---
SECQ_DIR = CACHE / "sec_quarters"
SECQ_TTL_DAYS = 45
SECQ_TAGS = {
    "rev": ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax", "SalesRevenueNet",
            "RevenueFromContractWithCustomerIncludingAssessedTax", "RevenuesNetOfInterestExpense", "TotalRevenuesAndOtherIncome"],
    "ni": ["NetIncomeLoss", "ProfitLoss", "NetIncomeLossAvailableToCommonStockholdersBasic"],
    "ocf": ["NetCashProvidedByUsedInOperatingActivities", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
}
SECQ_INSTANT = {
    "cash": ["CashAndCashEquivalentsAtCarryingValue", "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents", "Cash"],
    "debt": ["LongTermDebt", "LongTermDebtNoncurrent", "DebtInstrumentCarryingAmount"],
}


def _days(a: str, b: str) -> int:
    return (pd.Timestamp(b) - pd.Timestamp(a)).days


def _secq_flow(entries: list[dict]) -> dict:
    """Quarter values (end date -> value) from duration facts: 3-month figures where filed,
    otherwise the difference between year-to-date figures that share a start date."""
    latest = {}
    for e in entries:
        if "start" not in e or e.get("val") is None:
            continue
        k = (e["start"], e["end"])
        if k not in latest or e.get("filed", "") > latest[k].get("filed", ""):
            latest[k] = e
    direct, by_start = {}, {}
    for (st, en), e in latest.items():
        d = _days(st, en)
        if 80 <= d <= 100:
            direct[en] = float(e["val"])
        if 80 <= d <= 380:
            by_start.setdefault(st, []).append((en, float(e["val"])))
    derived = {}
    for st, rows in by_start.items():
        rows.sort()
        prev_end, prev_val = None, 0.0
        for en, v in rows:
            span = _days(prev_end or st, en)
            if 80 <= span <= 100:
                derived[en] = v - prev_val
            prev_end, prev_val = en, v
    out = dict(derived)
    out.update(direct)
    return out


def _secq_instant(entries: list[dict]) -> dict:
    out = {}
    for e in entries:
        if "start" in e or e.get("val") is None:
            continue
        if e["end"] not in out or e.get("filed", "") > out[e["end"]][1]:
            out[e["end"]] = (float(e["val"]), e.get("filed", ""))
    return {k: v[0] for k, v in out.items()}


def sec_quarters(facts: dict) -> dict | None:
    gaap = (facts.get("facts") or {}).get("us-gaap") or {}
    def pick(tags, fn):
        """Merge the candidate tags (companies switch tags over the years); earlier tags win on overlap."""
        found = [fn(((gaap.get(t) or {}).get("units") or {}).get("USD") or []) for t in tags]
        found = [v for v in found if v]
        if not found:
            return {}
        primary = max(found, key=lambda v: max(v))      # the tag used in the latest filing leads
        merged = dict(primary)
        for v in found:                                  # older periods filed under other tags fill gaps
            for k, x in v.items():
                merged.setdefault(k, x)
        return merged
    series = {k: pick(v, _secq_flow) for k, v in SECQ_TAGS.items()}
    series.update({k: pick(v, _secq_instant) for k, v in SECQ_INSTANT.items()})
    ends = sorted(set(series["rev"]) | set(series["ni"]))
    if not ends:
        return None
    rows = []
    for en in ends[-9:]:
        r = {"p": en}
        for k in ("rev", "ni", "ocf", "cash", "debt"):
            v = series[k].get(en)
            r[k] = fnum(v / 1e6, 3) if v is not None else None
        rows.append(r)
    return {"rows": rows, "fetched": datetime.now().strftime("%Y-%m-%d")}


def gather_sec_quarters(codes: list[str], force: dict, minutes: float) -> dict:
    """Quarterly figures for US companies from the SEC's company facts, refreshed when they file."""
    SECQ_DIR.mkdir(parents=True, exist_ok=True)
    try:
        cik_of = {t: c for c, t in sec_ticker_map().items()}
    except Exception as e:  # noqa: BLE001
        log(f"  SEC quarters skipped: {e}")
        return {}
    have, need = {}, []
    for c in codes:
        fp = SECQ_DIR / f"{c}.json"
        d = None
        if fp.exists():
            try:
                d = json.loads(fp.read_text())
            except Exception:  # noqa: BLE001
                d = None
        if d is not None:
            have[c] = d
        if c not in cik_of:
            continue
        stale = d is None or (time.time() - fp.stat().st_mtime) / 86400 > SECQ_TTL_DAYS
        if stale or (c in force and str((d or {}).get("fetched", "")) < force[c]):
            need.append(c)
    need.sort(key=lambda c: (c not in force, c in have))
    t_end, got, failed = time.time() + minutes * 60, 0, 0
    for c in need:
        if time.time() > t_end:
            log(f"  SEC quarters: time limit reached after {got}; the rest come on the next runs")
            break
        try:
            facts = _sec_get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik_of[c]:010d}.json").json()
            d = sec_quarters(facts) or {"rows": [], "fetched": datetime.now().strftime("%Y-%m-%d")}
            (SECQ_DIR / f"{c}.json").write_text(json.dumps(d))
            have[c] = d
            got += 1
        except Exception as e:  # noqa: BLE001
            failed += 1
            if "404" in str(e):          # no XBRL facts (e.g. some foreign filers): don't retry for a while
                (SECQ_DIR / f"{c}.json").write_text(json.dumps({"rows": [], "fetched": datetime.now().strftime("%Y-%m-%d")}))
            elif failed <= 5:
                log(f"  SEC quarters {c}: {e}")
    n = sum(1 for d in have.values() if d.get("rows"))
    log(f"  SEC quarters: {got} fetched this run, {n} companies with quarterly figures")
    for c in os.environ.get("SECQ_LOG_CODES", "AAPL,MSFT,JPM,XOM,KO,NVDA").split(","):
        d = have.get(c)
        if d and d.get("rows"):
            log("    " + c + ": " + "; ".join(f"{r['p']} rev {r['rev']} ni {r['ni']} ocf {r['ocf']}" for r in d["rows"][-5:]))
    return {c: d for c, d in have.items() if d.get("rows")}


# ------------------------------------------------ results-triggered refresh ---
RESULTS_KEEP_DAYS = 21      # how long a results event stays listed
RESULTS_RETRY_DAYS = 14     # keep re-checking a pending company for this long
SEC_FORMS = {"10-Q": "Quarterly report (10-Q)", "10-K": "Annual report (10-K)", "20-F": "Annual report (20-F)",
             "40-F": "Annual report (40-F)"}


def _results_path(mkt):
    return CACHE / f"results_{mkt}.json"


def load_results(mkt) -> dict:
    try:
        return json.loads(_results_path(mkt).read_text())
    except Exception:  # noqa: BLE001
        return {}


def merge_results(state: dict, events: list[dict], today: str) -> dict:
    """events: [{code, date, kind, url}]. A newer report for a company resets it to pending."""
    for e in events:
        old = state.get(e["code"])
        if old and old.get("lodged", "") >= e["date"]:
            continue
        state[e["code"]] = {"lodged": e["date"], "kind": e["kind"], "url": e.get("url"), "status": "pending", "tries": 0}
    cut = (pd.Timestamp(today) - pd.Timedelta(days=RESULTS_KEEP_DAYS)).strftime("%Y-%m-%d")
    return {c: v for c, v in state.items() if v.get("lodged", "") >= cut}


def results_due(state: dict, today: str) -> dict[str, str]:
    """Pending companies -> date their report was lodged (refetched unless already fetched today)."""
    cut = (pd.Timestamp(today) - pd.Timedelta(days=RESULTS_RETRY_DAYS)).strftime("%Y-%m-%d")
    return {c: v["lodged"] for c, v in state.items() if v.get("status") == "pending" and v.get("lodged", "") >= cut
            and v.get("checked") != today}


def fund_period_end(d: dict) -> str | None:
    """End date (YYYY-MM-DD) of the newest period in a company's saved financials."""
    ends = []
    ep = d.get("ep") or {}
    if ep.get("ok") and ep.get("rows"):
        ends.append(ep["rows"][-1]["p"])
    q = d.get("q") or {}
    if q.get("rows"):
        ends.append(q["rows"][-1]["p"])
    mrq = d.get("mostRecentQuarter")
    if isinstance(mrq, (int, float)) and mrq > 0:
        ends.append(datetime.fromtimestamp(mrq, timezone.utc).strftime("%Y-%m-%d"))
    for key in ("rev_y", "ni_y"):
        ser = d.get(key) or []
        if ser:
            ends.append((pd.Period(ser[-1]["p"], "M").end_time).strftime("%Y-%m-%d"))
    return max(ends) if ends else None


def check_results(state: dict, fund: dict, today: str) -> dict:
    """After fetching, mark each pending company updated if its figures now reach the reported period."""
    retry_cut = (pd.Timestamp(today) - pd.Timedelta(days=RESULTS_RETRY_DAYS)).strftime("%Y-%m-%d")
    for c, v in state.items():
        if v.get("status") != "pending":
            continue
        d = fund.get(c)
        if not d:
            v["status"] = "untracked"          # outside the financials collection
            continue
        fetched = str(d.get("fetched") or "")
        if fetched >= v["lodged"] or fetched == "mock":
            v["checked"] = today
            v["tries"] = v.get("tries", 0) + 1
            end = fund_period_end(d)
            v["periodEnd"] = end
            # Reports cover a period that ended at most about four months before they're lodged
            if end and end >= (pd.Timestamp(v["lodged"]) - pd.Timedelta(days=125)).strftime("%Y-%m-%d"):
                v["status"], v["updatedOn"] = "updated", today
                continue
        if v["lodged"] < retry_cut:
            v["status"] = "stale"
    return state


def save_results(mkt, state: dict):
    CACHE.mkdir(parents=True, exist_ok=True)
    _results_path(mkt).write_text(json.dumps(state, indent=1))


def results_payload(state: dict) -> list[dict]:
    rows = [dict(code=c, **v) for c, v in state.items() if v.get("status") != "untracked"]
    return sorted(rows, key=lambda r: (r["lodged"], r["code"]), reverse=True)


def sec_ticker_map() -> dict:
    """CIK (int) -> ticker, cached for a week."""
    f = CACHE / "sec_tickers.json"
    if f.exists() and time.time() - f.stat().st_mtime < 7 * 86400:
        return {int(k): v for k, v in json.loads(f.read_text()).items()}
    j = _sec_get("https://www.sec.gov/files/company_tickers.json").json()
    out = {}
    for row in j.values():
        out.setdefault(int(row["cik_str"]), str(row["ticker"]).upper().replace("-", "."))
    CACHE.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(out))
    return out


def sec_results(market_date, days_back: int) -> list[dict]:
    """Quarterly and annual reports filed with the SEC over the last few days (EDGAR daily form index)."""
    tick = sec_ticker_map()
    out, day = [], pd.Timestamp(market_date)
    for i in range(days_back + 1):
        d = day - pd.Timedelta(days=i)
        if d.weekday() >= 5:
            continue
        url = f"https://www.sec.gov/Archives/edgar/daily-index/{d.year}/QTR{(d.month - 1) // 3 + 1}/form.{d:%Y%m%d}.idx"
        try:
            text = _sec_get(url).text
        except Exception as e:  # noqa: BLE001
            if "404" not in str(e):
                log(f"  SEC index {d:%Y-%m-%d}: {e}")
            continue
        for line in text.splitlines():
            form = (line.split() or [""])[0]
            if form not in SEC_FORMS:
                continue
            mt = re.search(r"\s(\d{4,10})\s+(\d{8}|\d{4}-\d{2}-\d{2})\s+(edgar/\S+)\s*$", line)
            if not mt:
                continue
            code = tick.get(int(mt.group(1)))
            if not code:
                continue
            fn = mt.group(3)
            acc = fn.rsplit("/", 1)[-1].replace(".txt", "")
            link = f"https://www.sec.gov/Archives/{fn.rsplit('/', 1)[0]}/{acc.replace('-', '')}/{acc}-index.htm"
            out.append({"code": code, "date": f"{d:%Y-%m-%d}", "kind": SEC_FORMS[form], "url": link})
    log(f"  SEC filings: {len(out)} quarterly or annual reports over {days_back + 1} days")
    return out


def market_payload(mkt, m, hist, mdate, fund, idx, source, stats=None) -> dict:
    cfg = MARKETS[mkt]
    clean = m.astype(object).where(pd.notna(m), None)
    return {
        "id": mkt, "name": cfg["name"], "exchange": cfg["exchange"], "currency": cfg["currency"],
        "marketDate": pd.Timestamp(mdate).strftime("%Y-%m-%d"), "source": source,
        "classification": cfg["classification"],
        "capBuckets": [{"name": n, "min": lo} for n, lo in cfg["caps"]],
        "cols": list(m.columns), "rows": clean.values.tolist(),
        "hist": hist, "fund": fund, "indices": idx, "stats": stats or {},
        "builtUtc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
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
        gm_ = min(0.9, om + rng.uniform(0.1, 0.45))
        fund[r["code"]]["gp_y"] = [{"p": p_["p"], "v": round(p_["v"] * gm_ * rng.uniform(0.95, 1.05))} for p_ in fund[r["code"]]["rev_y"]]
        fund[r["code"]]["oi_y"] = [{"p": p_["p"], "v": round(p_["v"] * om * rng.uniform(0.9, 1.1))} for p_ in fund[r["code"]]["rev_y"]]
        drift = rng.choice([-0.02, -0.01, 0, 0.01, 0.04])
        fund[r["code"]]["shares_y"] = [{"p": f"{y}-06", "v": round(shares * (1 + drift) ** (y - 2025))} for y in range(2021, 2026)]
    base = {"AU": 8000, "US": 5800}[mkt]
    idx = []
    for k, (s, nm) in enumerate(cfg["indices"].items()):
        b = base * (1 + 0.3 * k)
        sp = [fnum(b * (1 + rng.gauss(0, 0.004) + 0.0008 * j), 1) for j in range(22)]
        idx.append({"sym": s, "name": nm, "last": sp[-1], "chg": fnum((sp[-1] / sp[-2] - 1) * 100, 2), "spark": sp})
    hist = {"dates": [d.strftime("%y%m%d") for d in days], "s": series}
    stats = {"listed": len(m) + 6, "priced": len(m), "traded": len(m),
             "fund": {"scope": int(len(m) * 0.9), "due": 300, "tried": 260, "fetched": 240, "failed": 20,
                      "stopped": "yahoo", "covered": len(fund)}}
    payload = market_payload(mkt, m, hist, days[-1], fund, idx, "Synthetic test data — not real prices.", stats)
    if mkt == "AU":
        heads = {"Results": "Half Year Results and Investor Presentation", "Capital raising": "A$45M placement to fund expansion",
                 "Takeover": "Scheme Implementation Deed signed", "Guidance": "FY27 guidance upgrade", "Exploration": "High-grade drill results at main project",
                 "Contract": "Award of 7-year services contract", "Price query": "Response to ASX price query", "Other": "Change of Director's Interest Notice",
                 "AGM": "AGM Chair's Address", "Dividend": "Dividend/Distribution announcement"}
        anns = []
        movers_ = m.reindex(m["chg"].abs().sort_values(ascending=False).index)["code"].tolist()
        for i, code in enumerate(movers_[:60] + m.sample(120, random_state=seed)["code"].tolist()):
            kind = rng.choice(list(heads)) if i % 3 else "Other"
            anns.append({"t": f"{rng.randint(8, 18):02d}:{rng.randint(0, 59):02d}", "code": code, "head": heads[kind], "kind": kind,
                         "ps": kind not in ("Other", "AGM") and rng.random() < 0.8})
        payload["announcements"] = anns
        hc = m.sample(5, random_state=seed + 1)["code"].tolist()
        nxt = pd.bdate_range(days[-1], periods=4)
        payload["halts"] = [{"code": hc[0], "since": days[-1].strftime("%Y-%m-%d"), "time": "09:58", "head": "Trading Halt", "kind": "halt", "resumeBy": nxt[2].strftime("%Y-%m-%d")},
                            {"code": hc[1], "since": days[-1].strftime("%Y-%m-%d"), "time": "12:20", "head": "Trading Halt", "kind": "halt", "resumeBy": nxt[2].strftime("%Y-%m-%d")},
                            {"code": hc[2], "since": days[-2].strftime("%Y-%m-%d"), "time": "09:01", "head": "Trading Halt", "kind": "halt", "resumeBy": nxt[1].strftime("%Y-%m-%d")},
                            {"code": hc[3], "since": days[-6].strftime("%Y-%m-%d"), "time": "09:30", "head": "Suspension from Quotation", "kind": "suspension"}]
    ts = int((days[-1] + pd.Timedelta(days=3)).timestamp())
    for i, (code, f) in enumerate(list(fund.items())[:400]):
        if i % 23 == 0:
            f["exDividendDate"], f["lastDividendValue"] = ts + 86400 * (i % 5), round(rng.uniform(0.05, 1.2), 3)
        if i % 37 == 0:
            f["earningsTimestampStart"] = ts + 86400 * (i % 4)
        f["mostRecentQuarter"] = int(pd.Timestamp("2026-09-30" if mkt == "US" else "2025-12-31").timestamp())
        f["netIncomeToCommon"] = fnum((f.get("ni_y") or [{}])[-1].get("v", 0) * rng.uniform(0.85, 1.25), 0)
    if mkt == "US":
        picks = m.sort_values("mcap", ascending=False).head(45)
        w = [rng.uniform(0.2, 1) ** 3 for _ in range(len(picks))]
        tot = sum(w)
        rows = []
        for (_, r), wi in zip(picks.iterrows(), w):
            ch = rng.choice(["unchanged"] * 5 + ["added", "reduced", "new"])
            val = 270e9 * wi / tot
            sh = val / r["price"]
            psh = 0 if ch == "new" else sh * (0.8 if ch == "added" else 1.25 if ch == "reduced" else 1)
            rows.append({"cusip": "000000000", "ticker": r["code"], "name": r["name"], "cls": "COM", "shares": round(sh),
                         "prevShares": round(psh), "value": round(val), "weight": fnum(wi / tot * 100, 3), "change": ch})
        rows.append({"cusip": "999999999", "ticker": None, "name": "Example Sold Holding", "cls": "COM", "shares": 0,
                     "prevShares": 1000000, "value": 0, "weight": 0, "change": "sold"})
        payload["berkshire"] = {"period": "2026-09-30", "filed": "2026-11-14", "prevPeriod": "2026-06-30",
                                "accession": "mock", "fetched": "mock", "total": 270e9, "rows": rows}
    fcodes = list(fund)[:12]
    today = days[-1].strftime("%Y-%m-%d")
    ev = []
    for i, c in enumerate(fcodes[:8]):
        lod = (days[-1] - pd.Timedelta(days=i % 6)).strftime("%Y-%m-%d")
        kind = ("Half-year report" if i % 2 else "Annual report") if mkt == "AU" else ("Quarterly report (10-Q)" if i % 3 else "Annual report (10-K)")
        st = "pending" if i % 3 == 1 else "updated"
        e = {"code": c, "lodged": lod, "kind": kind, "status": st, "tries": 1 + i % 3, "checked": today,
             "url": f"https://www.asx.com.au/markets/trade-our-cash-market/announcements.{c.lower()}" if mkt == "AU" else "https://www.sec.gov/"}
        if st == "updated":
            e["updatedOn"] = today
        ev.append(e)
    payload["results"] = ev
    if mkt == "AU":   # Eulerpool half-years: one good, one that fails the check, one with a source disagreement
        for i, c in enumerate(list(fund)[:30]):
            f = fund[c]; ry = f.get("rev_y") or []
            if len(ry) < 2:
                continue
            last = ry[-1]["v"] / 1e6
            rows = []
            for j, p in enumerate(["2024-12-31", "2025-06-30", "2025-12-31", "2026-06-30"]):
                v = last * (0.45 + 0.04 * j) * (1.6 if (i % 3 == 1 and j == 1) else 1)
                rows.append({"p": p, "rev": round(v, 1), "ni": round(v * 0.12, 1), "ocf": round(v * 0.18, 1), "cash": None, "debt": None})
            f["ep"] = {"ok": i % 3 != 1, "checked": 2, "rows": rows if i % 3 != 1 else [], "ccy": "AUD", "fetched": today,
                       "disagree": {"p": ry[-1]["p"], "ep": round(last * 1.26, 1), "yf": round(last, 1)} if i % 3 == 2 else None,
                       "behind": False, "newer": i % 3 == 0}
    return payload


# ------------------------------------------------------------------ output ---
def save_market(payload: dict) -> None:
    CACHE.mkdir(exist_ok=True)
    (CACHE / f"market_{payload['id']}.json").write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")


def load_market(mkt: str):
    f = CACHE / f"market_{mkt}.json"
    try:
        return json.loads(f.read_text(encoding="utf-8")) if f.exists() else None
    except Exception:  # noqa: BLE001
        return None


# GoatCounter site code for the public view counter (https://<code>.goatcounter.com).
# Leave empty to turn the counter off. GOATCOUNTER_SITE in the environment overrides it.
GOATCOUNTER_SITE = "jigarkumar"


def build_html(markets: list[dict], scheduled: bool = False) -> str:
    payload = {"generated": datetime.now().strftime("%Y-%m-%d %H:%M"),
               "generatedUtc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "scheduled": scheduled, "markets": markets,
               "counter": re.sub(r"[^a-z0-9-]", "", (os.environ.get("GOATCOUNTER_SITE") or GOATCOUNTER_SITE).strip().lower())}
    data = json.dumps(payload, separators=(",", ":"), allow_nan=False).replace("</", "<\\/")
    return TEMPLATE.read_text(encoding="utf-8").replace("/*__DATA__*/null", data)


def main():
    ap = argparse.ArgumentParser(description="Build the Stock Dashboard (ASX + US)")
    ap.add_argument("--markets", default="AU,US", help="comma list of AU, US (default both); 'none' rebuilds the page from saved data")
    ap.add_argument("--limit", type=int, help="only the first N tickers per market (testing)")
    ap.add_argument("--no-fundamentals", action="store_true")
    ap.add_argument("--fundamentals-full", action="store_true",
                    help="fetch every missing/stale fundamental now instead of a rolling batch (slow)")
    ap.add_argument("--mock", action="store_true", help="synthetic data, no internet")
    ap.add_argument("--open", action="store_true", help="open the dashboard when done")
    ap.add_argument("--scheduled", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--only", action="store_true",
                    help="show only the markets built this run (don't add the other market's last saved build)")
    ap.add_argument("--fund-minutes", type=float, default=0,
                    help="gentle mode: fetch fundamentals one at a time for at most this many minutes per market")
    ap.add_argument("--fund-delay", type=float, default=1.5, help="gentle mode: seconds between companies")
    args = ap.parse_args()

    OUT.mkdir(exist_ok=True)
    HIST.mkdir(parents=True, exist_ok=True)
    t0 = time.time()
    page_only = args.markets.strip().lower() == "none"
    wanted = [] if page_only else [x.strip().upper() for x in args.markets.split(",") if x.strip().upper() in MARKETS]
    if not wanted and not page_only:
        sys.exit("--markets must include AU and/or US (or 'none' to rebuild the page from saved data)")

    markets = []
    if args.mock:
        markets = [mock_market(mk, 900 if mk == "AU" else 1400, 7 + i) for i, mk in enumerate(wanted)]
    else:
        watch = read_watchlist()
        built = {}
        for mk in wanted:
            try:
                built[mk] = build_market(mk, args, watch[mk])
                save_market(built[mk])
            except Exception as e:  # noqa: BLE001
                log(f"{mk} skipped: {e}")
        # Markets not rebuilt this run (or that failed) keep their last saved version,
        # so the ASX and US runs at different times both show both markets.
        for mk in MARKETS:
            if mk not in built and not args.only:
                prev = load_market(mk)
                if prev:
                    log(f"{mk}: reusing the last saved build from {prev.get('builtUtc', 'earlier')}")
                    built[mk] = prev
        markets = [built[mk] for mk in MARKETS if mk in built]
        if not markets:
            sys.exit("No market could be built. Check your internet connection and try again in a few minutes.")

    html = build_html(markets, scheduled=args.scheduled)
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
