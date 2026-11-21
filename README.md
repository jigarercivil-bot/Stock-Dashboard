# Stock Dashboard

A free end-of-day dashboard for the **ASX** and all **US-listed stocks** (NYSE, Nasdaq,
NYSE American). It rebuilds itself every weekday on GitHub Actions and is published with
GitHub Pages.

**Live dashboard:** https://jigarercivil-bot.github.io/Stock-Dashboard/

## What's in it

- **Today's moves** – market map (heatmap by sector), daily summary, top 10 gainers and
  losers overall, by sector and by company size, sector performance, new 52-week highs and
  lows, and a sortable table of every stock.
- **Value finder** – scores each company 0–100 on Value (P/E, EV/EBITDA, price to book,
  free cash flow yield, EV/sales), Growth, Quality and Balance sheet (debt, net cash,
  current ratio, Piotroski F-score), ranked against its own sector. Strategy presets,
  adjustable weights and filters, a "cheap versus strong" chart, red flags, and a scorecard
  for every company.
- **Company view** – 1-year price chart, 52-week range, key figures, and revenue, profit,
  cash flow, debt and cash by year.

Mega caps (ASX over A$100B, US over US$200B) are hidden by default and can be switched on
in the filters.

## How it runs

`.github/workflows/build.yml` runs twice each Sydney weekday (no weekend runs):
the ASX at 5:47pm Sydney time, after its 4pm close, and the US at 9:17am Sydney time, after
New York's close (Monday morning's run picks up Friday's US close). Each run rebuilds its own
market and keeps the other market's last build. Company
financials are cached between runs (each run fetches up to 600 ASX and 800 US companies),
so the Value finder's coverage fills in over the first one to two weeks. On GitHub the financials are fetched gently (one company at a time, at most 40 minutes per market) to stay under Yahoo's limits.

To run it now: **Actions → Build Stock Dashboard → Run workflow**.

## Running it on your own PC

Double-click `run_dashboard.bat` (Windows). The first run sets up Python packages. The
result is `output/asx_dashboard_latest.html`. Options:

```
run_dashboard.bat --markets AU          # ASX only
run_dashboard.bat --fundamentals-full   # fetch every missing fundamental now (slow)
python asx_dashboard.py --mock          # made-up data, for layout testing only
```

## Settings

At the top of `asx_dashboard.py`: market-cap bands, Value finder universe (size and
turnover floors), fundamentals per run, and refresh age. `watchlist.txt` lists codes that
always get fundamentals (`PLS.AX` for ASX only, `US:SOFI` for US only).

## Data and caveats

Prices and fundamentals come from Yahoo Finance via `yfinance` (free, unofficial, end of
day). Company lists and market caps come from the ASX and Nasdaq's stock screener. Yahoo
sometimes rate-limits; when it does, the run keeps the cached financials and finishes.
Figures can lag company reports or contain errors. Scores are a starting point for research,
not financial advice.
