"""Print what Yahoo returns for interim (quarterly) statements for a sample of ASX tickers."""
import sys, time
import yfinance as yf
codes = sys.argv[1].split(",")
for c in codes:
    t = yf.Ticker(c + ".AX")
    try:
        q = t.quarterly_income_stmt
        cols = [str(x.date()) for x in q.columns] if q is not None and not q.empty else []
        rev = q.loc["Total Revenue"].tolist() if cols and "Total Revenue" in q.index else []
        ni = q.loc["Net Income"].tolist() if cols and "Net Income" in q.index else []
        qb = t.quarterly_balance_sheet; qc = t.quarterly_cashflow
        print(f"{c}: inc {cols} | rev {rev} | ni {ni} | bs {[str(x.date()) for x in qb.columns] if qb is not None and not qb.empty else []} | cf {[str(x.date()) for x in qc.columns] if qc is not None and not qc.empty else []}", flush=True)
        cal = t.calendar
        print(f"   calendar: {cal}", flush=True)
    except Exception as e:
        print(f"{c}: ERROR {e}", flush=True)
    time.sleep(2)
