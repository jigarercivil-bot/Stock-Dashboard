import os, sys, json, time, re, requests
K = os.environ.get("EULERPOOL_API_KEY", "").strip()
S = requests.Session(); B = "https://api.eulerpool.com"
def get(path, params=None):
    p = dict(params or {}); p["token"] = K
    r = S.get(B + path, params=p, timeout=60); time.sleep(0.5)
    try: return r.status_code, r.json()
    except Exception: return r.status_code, r.text[:200]
def pick(row, keys):
    for k in keys:
        if k in row and row[k] not in (None, ""): return row[k]
    return None
st, j = get("/api/1/equity/search", {"q": "BHP"}); print("search", st, json.dumps(j)[:500])
codes = sys.argv[1].split(",")
for c in codes:
    st, j = get("/api/1/equity/search", {"q": c + ".AX"})
    isin = None
    rows = j if isinstance(j, list) else (j.get("results") or j.get("data") or []) if isinstance(j, dict) else []
    for r in rows:
        if isinstance(r, dict) and str(r.get("ticker", "")).upper() in (c + ".AX", c):
            isin = r.get("isin"); break
    if not isin and rows and isinstance(rows[0], dict): isin = rows[0].get("isin")
    print(f"\n#### {c}: search {st} -> {isin}  {json.dumps(rows[:2])[:300]}")
    if not isin: continue
    for path, kind in [("income-statement-quarterly", "IQ"), ("cashflow-statement-quarterly", "CQ"), ("balance-sheet-quarterly", "BQ"), ("incomestatement", "IA"), ("financials-ltm", "LTM")]:
        st, j = get(f"/api/1/equity/{path}/{isin}")
        if isinstance(j, list) and j:
            last = sorted(j, key=lambda r: str(r.get("period") or r.get("date") or ""))[-3:]
            if kind == "IQ" or kind == "IA":
                out = [(r.get("period") or r.get("date"), pick(r, ["revenue", "totalRevenue"]), pick(r, ["netIncome"])) for r in last]
            elif kind == "CQ":
                out = [(r.get("period") or r.get("date"), pick(r, ["netOperatingCashFlow", "operatingCashFlow", "cashFromOperatingActivities", "netCashFromOperatingActivities"]), list(r.keys())[:12] if r is last[-1] else "") for r in last]
            else:
                out = [(r.get("period") or r.get("date"), pick(r, ["cash", "cashAndEquivalents", "cashAndShortTermInvestments", "cashEquivalents"]), pick(r, ["totalDebt", "longTermDebt"]), list(r.keys())[:14] if r is last[-1] else "") for r in last]
            print(f"  {kind} {st} n={len(j)}: {json.dumps(out)[:600]}")
        else:
            print(f"  {kind} {st}: {json.dumps(j)[:300]}")
