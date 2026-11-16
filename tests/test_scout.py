"""Offline test: fake feeds + fake AI, checks filters, doublets, scoring, digest, breaking."""
import json, os, sys, tempfile, time
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.update({"OPENROUTER_API_KEY": "x", "SCOUT_MODEL": "fake/model", "SCOUT_FALLBACK_MODEL": "fake/backup"})
import scout

now_ms = int(time.time() * 1000)
USGS = json.dumps({"features": [
    {"id": "a1", "properties": {"mag": 6.7, "place": "Central Mid-Atlantic Ridge", "time": now_ms - 6 * 3600e3, "url": "https://u/a1", "title": "M 6.7 - Mid-Atlantic Ridge"}, "geometry": {"coordinates": [-25.0, 0.5, 10]}},
    {"id": "a2", "properties": {"mag": 5.6, "place": "Central Mid-Atlantic Ridge", "time": now_ms, "url": "https://u/a2", "title": "M 5.6 - Mid-Atlantic Ridge"}, "geometry": {"coordinates": [-25.5, 0.8, 20]}},
    {"id": "b1", "properties": {"mag": 5.1, "place": "Vancouver Island, Canada", "time": now_ms, "url": "https://u/b1", "title": "M 5.1 - Vancouver Island"}, "geometry": {"coordinates": [-126.5, 49.5, 25]}},
    {"id": "c1", "properties": {"mag": 4.7, "place": "Tonga", "time": now_ms, "url": "https://u/c1", "title": "M 4.7 - Tonga"}, "geometry": {"coordinates": [-175, -20, 30]}},
]}).encode()
GDACS = b"""<?xml version="1.0"?><rss xmlns:gdacs="http://www.gdacs.org" xmlns:georss="http://www.georss.org/georss"><channel>
<item><title>Red alert flood in X</title><link>https://g/1</link><description>Floods</description><gdacs:alertlevel>Red</gdacs:alertlevel><gdacs:eventtype>FL</gdacs:eventtype><georss:point>10 100</georss:point></item>
<item><title>Green cyclone far away</title><link>https://g/2</link><description>Storm</description><gdacs:alertlevel>Green</gdacs:alertlevel><gdacs:eventtype>TC</gdacs:eventtype><georss:point>-30 60</georss:point></item>
</channel></rss>"""
SCI = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Unexpected magma body found beneath Hudson Bay</title><link href="https://s/1"/><summary>Researchers report a surprising structure.</summary></entry>
<entry><title>New smartphone battery chemistry</title><link href="https://s/2"/><summary>Unexpected gains.</summary></entry>
<entry><title>Glacier study updates model</title><link href="https://s/3"/><summary>Routine refinement.</summary></entry>
</feed>"""

FEEDS = [
    {"name": "USGS", "kind": "quake", "format": "usgs", "url": "usgs"},
    {"name": "GDACS", "kind": "gdacs", "format": "rss", "url": "gdacs"},
    {"name": "Sci", "kind": "science", "format": "rss", "url": "sci"},
    {"name": "Dead", "kind": "science", "format": "rss", "url": "dead"},
]

def fake_fetch(url):
    if url == "dead":
        raise OSError("404")
    return {"usgs": USGS, "gdacs": GDACS, "sci": SCI}[url]

calls = []
def fake_ai(model, items, timeout=90):
    calls.append(model)
    if model == "fake/model":
        raise json.JSONDecodeError("bad", "x", 0)     # main model fails -> retry -> fallback
    return {"items": [{"id": i["id"], "hook": 9 if "Atlantic" in i["title"] or "Hudson" in i["title"] else 6,
                       "explainable": 9, "north_america": 10 if "Hudson" in i["title"] or "Vancouver" in i["title"] else 3,
                       "title": "T:" + i["title"][:40], "why": "test"} for i in items]}

sent = []
scout.call_openrouter = fake_ai
scout.send = lambda t, b: sent.append((t, b)) or "test"

tmp = tempfile.mkdtemp()
fp = os.path.join(tmp, "feeds.json"); json.dump(FEEDS, open(fp, "w"))
scout.Cfg.feeds_path = fp
con = scout.db_connect(os.path.join(tmp, "t.db"))

scout.run_scan(con, fetch=fake_fetch)
rows = {r["id"]: dict(r) for r in con.execute("SELECT * FROM items")}

def check(cond, msg):
    print(("PASS " if cond else "FAIL ") + msg); assert cond, msg

check(rows["usgs:a1"]["passed"] == 1, "M6.7 passes")
check(rows["usgs:a2"]["passed"] == 1 and "doublet" in rows["usgs:a2"]["reason"], "M5.6 near M6.7 flagged as doublet")
check(rows["usgs:b1"]["passed"] == 1 and "North America" in rows["usgs:b1"]["reason"], "M5.1 Vancouver Island passes (N. America)")
check(rows["usgs:c1"]["passed"] == 0, "M4.7 Tonga dropped")
gd = [r for r in rows.values() if r["source"] == "GDACS"]
check(sorted(r["passed"] for r in gd) == [0, 1], "GDACS red kept, far green dropped")
sci = {r["title"]: r for r in rows.values() if r["source"] == "Sci"}
check(sci["Unexpected magma body found beneath Hudson Bay"]["passed"] == 1, "Hudson Bay science story passes")
check(sci["New smartphone battery chemistry"]["passed"] == 0, "off-topic story dropped")
check(sci["Glacier study updates model"]["passed"] == 0, "no-hook story dropped")
check(calls[:3] == ["fake/model", "fake/model", "fake/backup"], "main, retry, then fallback model used")
check(all(r["scored"] for r in rows.values() if r["passed"]), "all passed items scored")
check(json.loads(scout.meta_get(con, "failed_feeds")) == ["Dead"], "dead feed recorded")
check(any("Breaking" in t for t, _ in sent), "breaking alert sent for high-scoring quake")

# second scan: nothing new, no extra AI calls
n = len(calls); scout.run_scan(con, fetch=fake_fetch)
check(len(calls) == n, "re-scan skips already-seen items (no new AI cost)")

check(scout.send_digest(con, force=True), "digest sent")
title, body = sent[-1]
check("Hudson" in body and "Dead" in body, "digest includes top story and dead-feed warning")
print("\n--- sample digest ---\n" + title + "\n" + body)
