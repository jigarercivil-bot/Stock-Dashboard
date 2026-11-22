"""Summarise how recent the cached financial statements are, per market."""
import json, collections, pathlib, datetime
root = pathlib.Path("cache/fundamentals")
for mk in sorted(p.name for p in root.iterdir() if p.is_dir()) if root.exists() else []:
    pass
files = list(root.rglob("*.json"))
print("files", len(files))
by = collections.defaultdict(lambda: collections.Counter()); fetched = collections.defaultdict(collections.Counter)
mrq = collections.defaultdict(collections.Counter); fye = collections.defaultdict(collections.Counter)
samples = collections.defaultdict(list)
for f in files:
    try: d = json.loads(f.read_text())
    except Exception: continue
    mk = "AU" if f.stem.endswith(".AX") or f.parent.name == "AU" or ".AX" in f.name else "US"
    ry = d.get("rev_y") or d.get("ni_y") or []
    last = ry[-1]["p"] if ry else "none"
    by[mk][last] += 1; fetched[mk][str(d.get("fetched"))[:7]] += 1
    if len(samples[mk]) < 6 and last != "none": samples[mk].append((f.stem, [x["p"] for x in ry]))
for mk in by:
    print(mk, "latest annual period:", by[mk].most_common(12))
    print(mk, "fetched month:", fetched[mk].most_common(6))
    print(mk, "samples:", samples[mk])
