"""Probe ROIC.ai for ASX interim statements. Prints status and a short summary; never prints the key."""
import os, sys, time, json, requests
KEY = os.environ.get("ROIC_API_KEY", "").strip()
if not KEY: sys.exit("ROIC_API_KEY is empty")
S = requests.Session()
def get(url, params):
    p = dict(params, apikey=KEY)
    r = S.get(url, params=p, timeout=40)
    time.sleep(13)   # free tier: 5 requests a minute
    try: j = r.json()
    except Exception: j = r.text[:300]
    return r.status_code, j
def rows(j):
    if isinstance(j, list): return j
    if isinstance(j, dict):
        for k in ("data", "results", "items"):
            if isinstance(j.get(k), list): return j[k]
    return []
def summ(rs):
    out = []
    for x in rs[:8]:
        d = x.get("date") or x.get("period_end_date"); per = x.get("period") or x.get("period_type"); lab = x.get("period_label") or x.get("fiscal_period")
        rev = next((x[k] for k in ("is_sales_revenue_turnover","revenue","is_revenue","total_revenue","sales") if k in x), None)
        ni = next((x[k] for k in ("is_net_income","net_income","is_ni") if k in x), None)
        out.append(f"{d}/{per}/{lab}: rev={rev} ni={ni} {x.get('currency','')}")
    return out
# 1) find a URL form that works for BHP
cands = [
  ("https://api.roic.ai/v2/fundamental/income-statement/BHP.AX", {"period":"quarterly","limit":8}),
  ("https://api.roic.ai/v2/fundamental/income-statement/ASX:BHP", {"period":"quarterly","limit":8}),
  ("https://api.roic.ai/v3.0.0/fundamental/income-statement/ASX:BHP", {"period_type":"quarterly","limit":8}),
  ("https://api.roic.ai/v2/fundamental/income-statement/BHP.AX", {"limit":3}),
]
form = None
for url, prm in cands:
    st, j = get(url, prm); rs = rows(j)
    keys = list(rs[0].keys())[:60] if rs else []
    print(f"TRY {url.split('roic.ai')[1]} {prm} -> {st}, {len(rs)} rows", flush=True)
    if rs: print("   fields:", keys, flush=True); print("   " + "\n   ".join(summ(rs)), flush=True)
    else: print("   body:", str(j)[:300], flush=True)
    if rs and form is None: form = (url, prm)
if not form: sys.exit("no working form")
url, prm = form
for c in sys.argv[1].split(","):
    u = url.replace("BHP", c)
    st, j = get(u, prm); rs = rows(j)
    print(f"{c}: {st}, {len(rs)} rows", flush=True)
    for line in summ(rs): print("   " + line, flush=True)
    if not rs: print("   body:", str(j)[:200], flush=True)
