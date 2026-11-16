#!/usr/bin/env python3
"""
Earth Anomaly Scout
-------------------
Pulls free Earth-science feeds, throws out the noise with hard rules,
asks an AI model (via OpenRouter) to score what's left, and sends you
a short daily shortlist of video-worthy stories.

Standard library only. State lives in one SQLite file.

Commands:
  python scout.py loop          run forever (scan every N minutes, daily digest)
  python scout.py scan          fetch + filter + score once
  python scout.py digest        send today's digest now (ignores the clock)
  python scout.py test-notify   send a test message to your phone
  python scout.py stats         print what's in the database
"""
import email.utils
import hashlib
import html
import html.entities
import json
import logging
import math
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

log = logging.getLogger("scout")
HERE = os.path.dirname(os.path.abspath(__file__))


# ----------------------------------------------------------------- config
def env(name, default=None, cast=str):
    val = os.environ.get(name)
    if val is None or val == "":
        return default
    try:
        return cast(val)
    except ValueError:
        log.warning("Bad value for %s=%r, using default %r", name, val, default)
        return default


class Cfg:
    api_key = env("OPENROUTER_API_KEY")
    model = env("SCOUT_MODEL")
    fallback_model = env("SCOUT_FALLBACK_MODEL")
    db_path = env("SCOUT_DB", os.path.join(HERE, "data", "scout.db"))
    feeds_path = env("SCOUT_FEEDS", os.path.join(HERE, "feeds.json"))
    tz = ZoneInfo(env("SCOUT_TZ", "America/Los_Angeles"))
    scan_every_min = env("SCAN_EVERY_MIN", 180, int)
    digest_hour = env("DIGEST_HOUR", 7, int)          # local hour, 0-23
    top_n = env("DIGEST_TOP_N", 5, int)
    min_score = env("DIGEST_MIN_SCORE", 6.0, float)
    breaking_score = env("BREAKING_SCORE", 8.5, float)
    batch_size = env("AI_BATCH_SIZE", 20, int)
    max_ai_items_per_run = env("AI_MAX_ITEMS_PER_RUN", 60, int)
    max_age_days = env("MAX_AGE_DAYS", 14, int)       # ignore stories older than this
    telegram_token = env("TELEGRAM_BOT_TOKEN")
    telegram_chat = env("TELEGRAM_CHAT_ID")
    ntfy_url = env("NTFY_URL")              # e.g. https://ntfy.sh/my-secret-topic
    ntfy_token = env("NTFY_TOKEN")
    user_agent = "EarthAnomalyScout/1.0 (personal research bot)"


# North America box (rough): Mexico to Arctic Canada, Alaska to Newfoundland.
NA_BOX = (14.0, 84.0, -170.0, -50.0)   # lat_min, lat_max, lon_min, lon_max

EARTH_TOPICS = re.compile(
    r"earthquake|quake|seismic|fault|tremor|volcan|magma|lava|eruption|caldera|"
    r"glacier|ice sheet|sea ice|ice shelf|permafrost|iceberg|arctic|antarctic|"
    r"ocean|seafloor|sea floor|sea level|current|gulf stream|tsunami|"
    r"tectonic|plate|crust|mantle|geolog|sediment|rock|mineral|fossil|"
    r"sinkhole|aquifer|groundwater|landslide|subsid|river|lake|canyon|cave|"
    r"hurricane|cyclone|el ni|la ni|polar vortex|wildfire|flood|drought|"
    r"magnetic field|geomagnetic|core",
    re.I,
)
HOOK_WORDS = re.compile(
    r"unexpected|surpris|first[- ]ever|first time|never (been )?(seen|before|observed)|"
    r"record|unpreceden|unusual|anomal|myster|puzzl|baffl|strange|hidden|beneath|"
    r"buried|discover|reveal|faster than|rapid|sudden|accelerat|collapse|shift|"
    r"rising|sinking|vanish|disappear|giant|massive|largest|oldest|deepest|"
    r"rethink|overturn|challenge|warning|signs of",
    re.I,
)
VOLCANO_ACTION = re.compile(
    r"new eruption|eruption began|erupt|increas|elevated|alert level|aviation colou?r|"
    r"ash plume|lava flow|unrest|inflation|swarm|explosion|dome",
    re.I,
)


# ----------------------------------------------------------------- db
SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id TEXT PRIMARY KEY,
    source TEXT, kind TEXT, title TEXT, summary TEXT, url TEXT,
    lat REAL, lon REAL, mag REAL, depth REAL,
    event_time TEXT, first_seen TEXT,
    passed INTEGER DEFAULT 0, reason TEXT,
    scored INTEGER DEFAULT 0, ai_fail INTEGER DEFAULT 0,
    hook INTEGER, explainable INTEGER, na INTEGER, score REAL,
    title_idea TEXT, why TEXT,
    notified INTEGER DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_items_pending ON items(passed, scored);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def db_connect(path):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    _normalize_old_dates(con)
    return con


def _normalize_old_dates(con):
    """v1 stored raw RSS dates; convert them to ISO so age checks work."""
    rows = con.execute(
        "SELECT id, event_time FROM items WHERE event_time IS NOT NULL AND event_time NOT LIKE '____-__-__T%'"
    ).fetchall()
    for r in rows:
        con.execute("UPDATE items SET event_time=? WHERE id=?", (parse_date(r["event_time"]), r["id"]))
    if rows:
        con.commit()


def fresh_cutoff():
    return iso(now_utc() - timedelta(days=Cfg.max_age_days))


def meta_get(con, k, default=None):
    row = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row["v"] if row else default


def meta_set(con, k, v):
    con.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
    con.commit()


def now_utc():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ----------------------------------------------------------------- fetching
def http_get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": Cfg.user_agent})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def clean_text(s, limit=600):
    if not s:
        return ""
    s = html.unescape(re.sub(r"<[^>]+>", " ", s))
    s = re.sub(r"\s+", " ", s).strip()
    return s[:limit]


def stable_id(source, *parts):
    h = hashlib.sha1("|".join(p or "" for p in parts).encode()).hexdigest()[:16]
    return f"{source}:{h}"


def parse_usgs(raw, feed):
    data = json.loads(raw)
    out = []
    for f in data.get("features", []):
        p = f.get("properties", {}) or {}
        coords = (f.get("geometry") or {}).get("coordinates") or [None, None, None]
        lon, lat, depth = (coords + [None, None, None])[:3]
        mag = p.get("mag")
        t = p.get("time")
        out.append({
            "id": f"usgs:{f.get('id')}",
            "source": feed["name"], "kind": "quake",
            "title": p.get("title") or f"M{mag} earthquake",
            "summary": f"Magnitude {mag} at {p.get('place')}, depth {depth} km. "
                       f"Tsunami flag: {p.get('tsunami')}. Alert: {p.get('alert')}. "
                       f"Felt reports: {p.get('felt')}.",
            "url": p.get("url"),
            "lat": lat, "lon": lon, "mag": mag, "depth": depth,
            "event_time": iso(datetime.fromtimestamp(t / 1000, timezone.utc)) if t else None,
        })
    return out


def _local(tag):
    return tag.rsplit("}", 1)[-1].lower() if isinstance(tag, str) else ""


def parse_date(s):
    """RFC 822 (RSS) or ISO 8601 (Atom) -> ISO UTC string, or None."""
    if not s:
        return None
    try:
        dt = email.utils.parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError):
        try:
            dt = datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return iso(dt)


_XML_ENTITIES = {"amp", "lt", "gt", "quot", "apos"}


def repair_xml(raw):
    """Fix the usual broken-feed sins: bare '&' and HTML-only entities like &nbsp;."""
    text = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw

    def fix(m):
        name = m.group(1)
        if name in _XML_ENTITIES:
            return m.group(0)
        cp = html.entities.name2codepoint.get(name)
        return f"&#{cp};" if cp else f"&amp;{name};"

    text = re.sub(r"&([A-Za-z][A-Za-z0-9]*);", fix, text)
    text = re.sub(r"&(?!(?:[A-Za-z][A-Za-z0-9]*|#\d+|#x[0-9A-Fa-f]+);)", "&amp;", text)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    return text.encode("utf-8")


def parse_rss(raw, feed):
    """Handles RSS 2.0, Atom and GeoRSS/GDACS extras without knowing namespaces."""
    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        root = ET.fromstring(repair_xml(raw))
    out = []
    for el in root.iter():
        if _local(el.tag) not in ("item", "entry"):
            continue
        fields, link = {}, None
        for ch in el:
            name = _local(ch.tag)
            if name == "link" and ch.get("href") and not link:
                link = ch.get("href")
            if name not in fields and (ch.text or "").strip():
                fields[name] = ch.text.strip()
        link = link or fields.get("link") or fields.get("guid")
        title = clean_text(fields.get("title"), 300)
        summary = clean_text(fields.get("description") or fields.get("summary") or fields.get("content"))
        lat = lon = None
        point = fields.get("point")
        if point:
            try:
                lat, lon = [float(x) for x in point.replace(",", " ").split()[:2]]
            except ValueError:
                pass
        if lat is None and "lat" in fields and ("long" in fields or "lon" in fields):
            try:
                lat, lon = float(fields["lat"]), float(fields.get("long") or fields.get("lon"))
            except ValueError:
                pass
        extra = []
        for k in ("alertlevel", "eventtype", "severity", "country"):
            if k in fields:
                extra.append(f"{k}={fields[k]}")
        out.append({
            "id": stable_id(feed["name"], link, title),
            "source": feed["name"], "kind": feed["kind"],
            "title": title, "summary": (summary + (" [" + ", ".join(extra) + "]" if extra else "")).strip(),
            "url": link, "lat": lat, "lon": lon, "mag": None, "depth": None,
            "event_time": parse_date(fields.get("pubdate") or fields.get("published")
                                     or fields.get("updated") or fields.get("date")),
            "_alertlevel": (fields.get("alertlevel") or "").lower(),
            "_eventtype": (fields.get("eventtype") or "").upper(),
        })
    return out


PARSERS = {"usgs": parse_usgs, "rss": parse_rss}


def load_feeds(path):
    with open(path) as f:
        return [x for x in json.load(f) if x.get("enabled", True)]


# ----------------------------------------------------------------- hard filters
def in_na(lat, lon):
    if lat is None or lon is None:
        return False
    return NA_BOX[0] <= lat <= NA_BOX[1] and NA_BOX[2] <= lon <= NA_BOX[3]


def km_between(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def find_doublet(con, it):
    """Another M5.5+ quake within 300 km and 72 h -> the 'two quakes, second bigger' hook."""
    if (it.get("mag") or 0) < 5.5 or it.get("lat") is None or not it.get("event_time"):
        return None
    t = datetime.strptime(it["event_time"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    rows = con.execute(
        "SELECT id, mag, lat, lon, event_time FROM items WHERE kind='quake' AND id<>? AND mag>=5.5 "
        "AND event_time BETWEEN ? AND ?",
        (it["id"], iso(t - timedelta(hours=72)), iso(t + timedelta(hours=72))),
    ).fetchall()
    for r in rows:
        if r["lat"] is not None and km_between(it["lat"], it["lon"], r["lat"], r["lon"]) <= 300:
            return r
    return None


def hard_filter(con, it):
    """Return (passed, reason). Cheap rules that remove ~80-90% before any AI cost."""
    kind = it["kind"]
    text = f"{it.get('title', '')} {it.get('summary', '')}"

    if it.get("event_time"):
        try:
            age = now_utc() - datetime.strptime(it["event_time"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            if age > timedelta(days=Cfg.max_age_days):
                return False, f"older than {Cfg.max_age_days} days"
        except ValueError:
            pass

    if kind == "quake":
        mag, depth = it.get("mag") or 0, it.get("depth")
        dbl = find_doublet(con, it)
        if dbl:
            return True, f"doublet: another M{dbl['mag']} within 300 km / 72 h"
        if mag >= 6.0:
            return True, f"M{mag} (6.0+)"
        if in_na(it.get("lat"), it.get("lon")) and mag >= 5.0:
            return True, f"M{mag} in North America"
        if mag >= 5.5 and depth is not None and depth <= 15:
            return True, f"shallow M{mag} ({depth} km)"
        return False, f"M{mag} below thresholds"

    if kind == "gdacs":
        lvl, et = it.get("_alertlevel", ""), it.get("_eventtype", "")
        if lvl in ("orange", "red"):
            return True, f"GDACS {lvl} {et}"
        if et == "VO":
            return True, "GDACS volcano"
        if in_na(it.get("lat"), it.get("lon")):
            return True, f"GDACS {lvl or 'green'} {et} in North America"
        return False, f"GDACS {lvl or 'green'} {et}"

    if kind == "volcano":
        if VOLCANO_ACTION.search(text):
            return True, "volcano activity change"
        return False, "routine volcano update"

    if kind == "trusted":                        # e.g. NSIDC: few posts, always relevant
        return True, "trusted source"

    if kind == "science":
        topic = EARTH_TOPICS.search(text)
        hook = HOOK_WORDS.search(text)
        if topic and hook:
            return True, f"earth topic '{topic.group(0)}' + hook '{hook.group(0)}'"
        return False, "no earth topic + hook combo"

    return False, f"unknown kind {kind}"


# ----------------------------------------------------------------- AI scoring
SYSTEM_PROMPT = """You score news items for a faceless YouTube channel about Earth-science anomalies
(earthquakes, volcanoes, ice, oceans, hidden geology). The audience is general, mostly North American.
The channel explains real data calmly after a gripping hook. It never invents facts.

For EACH item give integers 1-10:
- hook: how surprising or gripping is it to a non-scientist? (10 = "wait, what?")
- explainable: is there enough real, citable data in the source to build an accurate 10-15 min video? (1 = vague/speculative)
- north_america: relevance to the US or Canada. Canada counts extra: give Canada-specific stories +2 (max 10).
Also give:
- title: one YouTube title, under 90 characters, curiosity-driven but not false.
- why: one short sentence on why it would work (or not).

Return ONLY JSON: {"items":[{"id":"...","hook":0,"explainable":0,"north_america":0,"title":"...","why":"..."}]}
Use the exact ids given. Include every item."""


def call_openrouter(model, items, timeout=90):
    payload = {
        "model": model,
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps({"items": items}, ensure_ascii=False)},
        ],
    }
    req = urllib.request.Request(
        "https://openrouter.ai/api/v1/chat/completions",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {Cfg.api_key}",
            "Content-Type": "application/json",
            "X-Title": "Earth Anomaly Scout",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = json.loads(r.read())
    content = body["choices"][0]["message"]["content"] or ""
    content = re.sub(r"^```(?:json)?|```$", "", content.strip(), flags=re.M).strip()
    return json.loads(content)


def validate_scores(result, wanted_ids):
    good = {}
    for row in (result or {}).get("items", []):
        try:
            rid = str(row["id"])
            if rid not in wanted_ids:
                continue
            vals = [int(row[k]) for k in ("hook", "explainable", "north_america")]
            if not all(1 <= v <= 10 for v in vals):
                continue
            good[rid] = {
                "hook": vals[0], "explainable": vals[1], "na": vals[2],
                "title": str(row.get("title", ""))[:120],
                "why": str(row.get("why", ""))[:240],
            }
        except (KeyError, TypeError, ValueError):
            continue
    return good


def composite(s):
    return round(0.45 * s["hook"] + 0.35 * s["explainable"] + 0.20 * s["na"], 2)


def score_batch(rows):
    items = [{
        "id": r["id"], "source": r["source"], "title": r["title"],
        "summary": r["summary"], "reason_flagged": r["reason"],
        "location": f"{r['lat']},{r['lon']}" if r["lat"] is not None else None,
    } for r in rows]
    wanted = {r["id"] for r in rows}
    models = [m for m in (Cfg.model, Cfg.model, Cfg.fallback_model) if m]   # main, retry, fallback
    for m in models:
        try:
            got = validate_scores(call_openrouter(m, items), wanted)
            if len(got) >= max(1, len(wanted) // 2):
                return got
            log.warning("Model %s returned %d/%d usable scores, retrying", m, len(got), len(wanted))
        except (urllib.error.URLError, json.JSONDecodeError, KeyError, IndexError, TimeoutError) as e:
            log.warning("Model %s failed: %s", m, e)
    return {}


def score_pending(con):
    if not Cfg.api_key or not Cfg.model:
        log.error("OPENROUTER_API_KEY or SCOUT_MODEL missing - skipping AI scoring this run.")
        return 0
    rows = con.execute(
        "SELECT * FROM items WHERE passed=1 AND scored=0 AND ai_fail<3 ORDER BY first_seen DESC LIMIT ?",
        (Cfg.max_ai_items_per_run,),
    ).fetchall()
    done = 0
    for i in range(0, len(rows), Cfg.batch_size):
        batch = rows[i:i + Cfg.batch_size]
        scores = score_batch(batch)
        for r in batch:
            s = scores.get(r["id"])
            if s:
                con.execute(
                    "UPDATE items SET scored=1, hook=?, explainable=?, na=?, score=?, title_idea=?, why=? WHERE id=?",
                    (s["hook"], s["explainable"], s["na"], composite(s), s["title"], s["why"], r["id"]),
                )
                done += 1
            else:
                con.execute("UPDATE items SET ai_fail=ai_fail+1 WHERE id=?", (r["id"],))
        con.commit()
    log.info("AI scored %d of %d pending items", done, len(rows))
    return done


# ----------------------------------------------------------------- notifications
def send(title, body):
    if Cfg.telegram_token and Cfg.telegram_chat:
        url = f"https://api.telegram.org/bot{Cfg.telegram_token}/sendMessage"
        data = json.dumps({
            "chat_id": Cfg.telegram_chat,
            "text": f"{title}\n\n{body}"[:4000],
            "disable_web_page_preview": True,
        }).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=30).read()
        return "telegram"
    if Cfg.ntfy_url:
        headers = {"Title": title.encode("ascii", "ignore").decode() or "Earth Anomaly Scout"}
        if Cfg.ntfy_token:
            headers["Authorization"] = f"Bearer {Cfg.ntfy_token}"
        req = urllib.request.Request(Cfg.ntfy_url, data=body.encode(), headers=headers)
        urllib.request.urlopen(req, timeout=30).read()
        return "ntfy"
    print(f"\n===== {title} =====\n{body}\n", flush=True)
    return "stdout"


def fmt_item(n, r):
    return (
        f"{n}) [{r['score']:.1f}] {r['title_idea'] or r['title']}\n"
        f"   Why: {r['why']}\n"
        f"   Hook {r['hook']} · Data {r['explainable']} · N.Am {r['na']} · {r['reason']}\n"
        f"   Source: {r['title']}\n   {r['url']}"
    )


def send_breaking(con):
    rows = con.execute(
        "SELECT * FROM items WHERE scored=1 AND notified=0 AND kind IN ('quake','gdacs','volcano') "
        # location doesn't matter for breaking: a mid-Atlantic doublet is still a 30X video
        "AND (0.55*hook + 0.45*explainable)>=? AND first_seen>=? "
        "AND (event_time IS NULL OR event_time>=?) ORDER BY score DESC",
        (Cfg.breaking_score, iso(now_utc() - timedelta(hours=12)), iso(now_utc() - timedelta(days=3))),
    ).fetchall()
    for r in rows:
        try:
            send("🚨 Breaking: video-worthy event", fmt_item(1, r) + "\n\nWindow: ~24-48 h before copycats.")
            con.execute("UPDATE items SET notified=1 WHERE id=?", (r["id"],))
            con.commit()
        except Exception as e:   # never let a notify failure kill the scan
            log.error("Breaking alert failed: %s", e)


def send_digest(con, force=False):
    local_now = now_utc().astimezone(Cfg.tz)
    today = local_now.strftime("%Y-%m-%d")
    if not force and (local_now.hour < Cfg.digest_hour or meta_get(con, "last_digest") == today):
        return False
    rows = con.execute(
        "SELECT * FROM items WHERE scored=1 AND notified=0 AND score>=? AND first_seen>=? "
        "AND (event_time IS NULL OR event_time>=?) ORDER BY score DESC LIMIT ?",
        (Cfg.min_score, iso(now_utc() - timedelta(hours=36)), fresh_cutoff(), Cfg.top_n),
    ).fetchall()
    failed = json.loads(meta_get(con, "failed_feeds", "[]"))
    if rows:
        body = "\n\n".join(fmt_item(i + 1, r) for i, r in enumerate(rows))
    else:
        body = "Nothing scored above the bar in the last 36 h. Quiet planet."
    if failed:
        body += "\n\n⚠️ Feeds failing (fix URL in feeds.json): " + ", ".join(failed)
    try:
        send(f"🌍 Earth Anomaly Scout · {local_now:%b %d}", body)
    except Exception as e:
        log.error("Digest send failed: %s", e)
        return False
    for r in rows:
        con.execute("UPDATE items SET notified=1 WHERE id=?", (r["id"],))
    meta_set(con, "last_digest", today)
    con.commit()
    return True


# ----------------------------------------------------------------- scan
def fetch_all(feeds, fetch=http_get):
    items, failed = [], []
    for feed in feeds:
        try:
            raw = fetch(feed["url"])
            got = PARSERS[feed["format"]](raw, feed)
            log.info("%-40s %3d items", feed["name"][:40], len(got))
            items.extend(got)
        except Exception as e:
            log.warning("Feed failed: %s (%s)", feed["name"], e)
            failed.append(feed["name"])
    return items, failed


def store_and_filter(con, items):
    new = passed = 0
    stamp = iso(now_utc())
    # quakes first so doublet detection can see earlier quakes from the same batch
    items = sorted(items, key=lambda x: (x["kind"] != "quake", x.get("event_time") or ""))
    for it in items:
        if con.execute("SELECT 1 FROM items WHERE id=?", (it["id"],)).fetchone():
            continue
        # same story carried by two feeds (e.g. two ScienceDaily sections) -> keep the first
        if it["kind"] != "quake" and it.get("title") and con.execute(
                "SELECT 1 FROM items WHERE lower(title)=lower(?)", (it["title"],)).fetchone():
            continue
        con.execute(
            "INSERT INTO items(id,source,kind,title,summary,url,lat,lon,mag,depth,event_time,first_seen) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (it["id"], it["source"], it["kind"], it["title"], it["summary"], it["url"],
             it["lat"], it["lon"], it["mag"], it["depth"], it["event_time"], stamp),
        )
        ok, reason = hard_filter(con, it)
        con.execute("UPDATE items SET passed=?, reason=? WHERE id=?", (int(ok), reason, it["id"]))
        new += 1
        passed += ok
    con.commit()
    log.info("New items: %d, passed hard filters: %d", new, passed)
    return new, passed


def cleanup(con, days=180):
    con.execute("DELETE FROM items WHERE first_seen < ?", (iso(now_utc() - timedelta(days=days)),))
    con.commit()


def run_scan(con, fetch=http_get):
    feeds = load_feeds(Cfg.feeds_path)
    items, failed = fetch_all(feeds, fetch)
    meta_set(con, "failed_feeds", json.dumps(failed))
    store_and_filter(con, items)
    score_pending(con)
    send_breaking(con)
    cleanup(con)


def stats(con):
    q = lambda sql: con.execute(sql).fetchone()[0]
    print(f"items: {q('SELECT COUNT(*) FROM items')}  passed: {q('SELECT COUNT(*) FROM items WHERE passed=1')}  "
          f"scored: {q('SELECT COUNT(*) FROM items WHERE scored=1')}  notified: {q('SELECT COUNT(*) FROM items WHERE notified=1')}")
    print("last digest:", meta_get(con, "last_digest"), " failing feeds:", meta_get(con, "failed_feeds"))
    for r in con.execute("SELECT score, title_idea, source FROM items WHERE scored=1 ORDER BY first_seen DESC, score DESC LIMIT 10"):
        print(f"  [{r['score']}] {r['title_idea']}  ({r['source']})")


def main(argv):
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                        format="%(asctime)s %(levelname)s %(message)s")
    cmd = argv[1] if len(argv) > 1 else "loop"
    con = db_connect(Cfg.db_path)
    if cmd == "scan":
        run_scan(con)
    elif cmd == "digest":
        send_digest(con, force=True)
    elif cmd == "test-notify":
        print("sent via", send("✅ Earth Anomaly Scout test", "Notifications are working."))
    elif cmd == "stats":
        stats(con)
    elif cmd == "loop":
        log.info("Scout running: scan every %d min, digest at %02d:00 %s",
                 Cfg.scan_every_min, Cfg.digest_hour, Cfg.tz.key)
        last_scan = 0.0
        while True:                       # tick every 5 min so the digest lands on time
            try:
                if time.time() - last_scan >= Cfg.scan_every_min * 60:
                    last_scan = time.time()
                    run_scan(con)
                send_digest(con)
            except Exception:
                log.exception("Cycle crashed - will retry next tick")
            time.sleep(300)
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
