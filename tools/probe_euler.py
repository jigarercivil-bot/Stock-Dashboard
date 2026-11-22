import os, sys, json, time, re, requests
K = os.environ.get("EULERPOOL_API_KEY", "").strip()
S = requests.Session(); B = "https://api.eulerpool.com"
def get(path, params=None):
    p = dict(params or {}); p["token"] = K
    r = S.get(B + path, params=p, timeout=60); time.sleep(0.5)
    try: return r.status_code, r.json()
    except Exception: return r.status_code, r.text
st, y = get("/api/1/documentation/yaml")
text = y if isinstance(y, str) else json.dumps(y)
paths = sorted(set(re.findall(r"^\s{0,4}(/api/1/[^\s:]+):", text, re.M)))
print("spec status", st, "paths", len(paths))
for p in paths:
    if re.search(r"equity|search|isin|ticker|symbol|fundament|financ|quarter|interim|cash|balance", p, re.I):
        print("PATH", p)
st, j = get("/api/1/equity/incomestatement/AU000000BHP4")
print("BHP income last rows:", json.dumps(j[-3:])[:900] if isinstance(j, list) else j)
