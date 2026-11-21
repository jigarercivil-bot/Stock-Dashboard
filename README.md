# Stock Dashboard

A free end-of-day dashboard for the **ASX** and all **US-listed stocks** (NYSE, Nasdaq,
NYSE American). It rebuilds itself every weekday on GitHub Actions and is published with
GitHub Pages.

**Live dashboard:** https://jigarercivil-bot.github.io/Stock-Dashboard/

## What's in it

- **Market brief** (home) – the day in a paragraph; **Watch tomorrow** cards (trading halts
  in place, ex-dividend dates and results due this week, strong rises on heavy volume, quality
  stocks that fell without news, your watchlist); **biggest moves** with the reason when a
  company announced something; **announcements that moved prices**; **trading halts and
  suspensions**; sector moves. Announcements and halts come from the ASX (ASX view only).
- **Market movers** – market map (heatmap), top movers by sector and company size, new
  52-week highs and lows, and a sortable table of every stock.
- **Discover** – scores each company on Value, Growth, Quality and Balance sheet against its
  sector, with strategies (Balanced, Deep value, Growth at a fair price, Quality at a fair
  price, Net cash bargains, Quality on sale, Dividend income, Momentum with substance),
  adjustable weights and filters, a "cheap versus strong" chart and red flags.
- **Buffett-style** – eight checks drawn from Warren Buffett's shareholder letters and
  Berkshire's acquisition criteria, for both markets. Inspired by his published principles;
  not stocks he has picked.
- **Company page** – score dials, strengths and risks, "is the price reasonable?" in three
  views, price chart, key figures, financial health, Buffett-style checklist and peers.
- **Watchlist** and **Portfolio** – saved in your browser on that device (nothing uploaded).
  The portfolio shows value, today's change, gain on cost, scores and what needs attention.
- **Berkshire** – Berkshire Hathaway's latest 13F holdings with quarterly changes (US).

Search any stock with **Ctrl K**. Mega caps (ASX over A$100B, US over US$200B) are hidden
from the lists by default and can be switched on under Filters.

## How it runs

`.github/workflows/build.yml` runs twice each Sydney weekday (no weekend runs):
the ASX at 5pm Sydney time (all year, whatever the daylight-saving offset), after its 4pm close, and the US at 9:17am Sydney time, after
New York's close (Monday morning's run picks up Friday's US close). Each run rebuilds its own
market and keeps the other market's last build. Company
financials are cached between runs (each run fetches up to 600 ASX and 800 US companies),
so the Value finder's coverage fills in over the first one to two weeks. On GitHub the financials are fetched gently (one company at a time, at most 40 minutes per market) to stay under Yahoo's limits.

To run it now: **Actions → Build Stock Dashboard → Run workflow**. Enter `none` as the
market to rebuild only the page from the last saved data (about 2 minutes).

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

## SEC contact for Berkshire's holdings

The SEC requires every automated request to name a contact. Add a repository **secret**
named `SEC_CONTACT` with your name and email (for example `Jane Smith jane@example.com`).
On your PC, set an environment variable of the same name.

## Data and caveats

Prices and fundamentals come from Yahoo Finance via `yfinance` (free, unofficial, end of
day). Company lists and market caps come from the ASX and Nasdaq's stock screener. Yahoo
sometimes rate-limits; when it does, the run keeps the cached financials and finishes.
Figures can lag company reports or contain errors. Scores are a starting point for research,
not financial advice.
