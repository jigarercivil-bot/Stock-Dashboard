import asx_dashboard as a, json
codes = ["AAPL", "MSFT", "JPM", "XOM", "KO", "NVDA", "MOV", "TSM", "HRTG", "WMT"]
cik = {t: c for c, t in a.sec_ticker_map().items()}
for c in codes:
    if c not in cik:
        print(c, "no CIK"); continue
    try:
        f = a._sec_get(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik[c]:010d}.json").json()
        q = a.sec_quarters(f)
        rows = (q or {}).get("rows", [])
        print(f"## {c}: {len(rows)} quarters")
        for r in rows[-6:]:
            print("   ", json.dumps(r))
    except Exception as e:
        print(c, "ERR", e)
