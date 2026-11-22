import os, sys, json, time, requests
import yfinance as yf
K = os.environ.get("EULERPOOL_API_KEY", "").strip()
S = requests.Session(); B = "https://api.eulerpool.com"
ISIN = dict(BHP="AU000000BHP4", CBA="AU000000CBA7", WTC="AU000000WTC3", PLS="AU000000PLS0", SFR="AU000000SFR8", AIS="AU000000AIS8",
            **{"29M": "AU0000157067"}, A1M="AU0000049033", CXO="AU000000CXO2", ZIP="AU0000218307")
def get(path):
    r = S.get(B + path, params={"token": K}, timeout=60); time.sleep(0.5)
    try: return r.json()
    except Exception: return []
def actual(rows):
    return sorted([r for r in rows if isinstance(r, dict) and not str(r.get("period", "")).endswith("e")], key=lambda r: r["period"])
def m(x): return None if x is None else round(float(x), 1)
for c, isin in ISIN.items():
    iq = actual(get(f"/api/1/equity/income-statement-quarterly/{isin}"))
    cq = {r["period"]: r for r in actual(get(f"/api/1/equity/cashflow-statement-quarterly/{isin}"))}
    bq = {r["period"]: r for r in actual(get(f"/api/1/equity/balance-sheet-quarterly/{isin}"))}
    ia = actual(get(f"/api/1/equity/incomestatement/{isin}"))
    print(f"\n#### {c}  EP annual last: " + json.dumps([(r['period'], m(r.get('revenue')), m(r.get('netIncome'))) for r in ia[-2:]]))
    for r in iq[-4:]:
        p = r["period"]; cf = cq.get(p, {}); bs = bq.get(p, {})
        print(f"  EP {p} {r.get('quarterPeriod','')}: rev {m(r.get('revenue'))} ni {m(r.get('netIncome'))} opCF {m(cf.get('netOperatingCashFlow'))} cash {m(bs.get('cash'))} debt {m(bs.get('totalDebt') or bs.get('longTermDebt'))}")
    try:
        t = yf.Ticker(c + ".AX"); inc = t.income_stmt
        cols = list(inc.columns)[:2]
        print("  YF annual: " + json.dumps([(str(d.date()), m(inc.loc['Total Revenue', d]/1e6) if 'Total Revenue' in inc.index else None, m(inc.loc['Net Income', d]/1e6) if 'Net Income' in inc.index else None) for d in cols]))
    except Exception as e:
        print("  YF err", e)
    time.sleep(1)
