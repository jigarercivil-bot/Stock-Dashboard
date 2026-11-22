"""Find which Eulerpool endpoints and ticker forms work for ASX companies. Never prints the key."""
import os, sys, json, time, requests
K = os.environ.get("EULERPOOL_API_KEY", "").strip()
if not K: sys.exit("EULERPOOL_API_KEY is empty")
S = requests.Session()
def get(url, params=None, auth="bearer"):
    h = {"Authorization": f"Bearer {K}"} if auth == "bearer" else {}
    p = dict(params or {})
    if auth == "token": p["token"] = K
    try:
        r = S.get(url, headers=h, params=p, timeout=40)
    except Exception as e:
        return "ERR", str(e)[:200]
    time.sleep(0.6)
    try: return r.status_code, r.json()
    except Exception: return r.status_code, r.text[:300]
def short(j, n=700):
    s = json.dumps(j)[:n]
    return s.replace(K, "***")
B = "https://api.eulerpool.com"
tries = [
  ("search v1", f"{B}/v1/search", {"q": "BHP"}, "bearer"),
  ("search v1 companies", f"{B}/v1/companies/search", {"q": "BHP Group"}, "bearer"),
  ("fin income BHP", f"{B}/v1/financials/BHP/income", {}, "bearer"),
  ("fin income BHP.AX", f"{B}/v1/financials/BHP.AX/income", {}, "bearer"),
  ("fin income ASX:BHP", f"{B}/v1/financials/ASX:BHP/income", {}, "bearer"),
  ("fin income ISIN", f"{B}/v1/financials/AU000000BHP4/income", {}, "bearer"),
  ("companies ISIN income", f"{B}/v1/companies/AU000000BHP4/income-statement", {}, "bearer"),
  ("companies BHP.AX income", f"{B}/v1/companies/BHP.AX/income-statement", {}, "bearer"),
  ("legacy isin token", f"{B}/api/1/equity/incomestatement/AU000000BHP4", {}, "token"),
  ("legacy isin bearer", f"{B}/api/1/equity/incomestatement/AU000000BHP4", {}, "bearer"),
  ("legacy quarterly", f"{B}/api/1/equity/incomestatement-quarterly/AU000000BHP4", {}, "token"),
  ("legacy profile", f"{B}/api/1/equity/profile/AU000000BHP4", {}, "token"),
]
for name, url, prm, auth in tries:
    st, j = get(url, prm, auth)
    print(f"== {name}: {st}\n   {short(j)}", flush=True)
