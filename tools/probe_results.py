import asx_dashboard as a, pandas as pd, collections
d = pd.Timestamp("2026-11-20")
ev = a.sec_results(d, 7)
print("SEC events", len(ev), collections.Counter(e["kind"] for e in ev).most_common(), ev[:3])
anns = a.asx_announcements(d, days_back=7)
fin = [x for x in anns if x.get("fin")]
print("ASX fin", len(fin), collections.Counter(x["fin"] for x in fin).most_common())
for x in fin[:25]: print("  ", x["date"], x["code"], x["fin"], "|", x["head"])
