#!/usr/bin/env python3
"""Calgary warehouse job watcher — cloud edition (GitHub Actions).

Monitors several sources for new jobs in the Calgary area and pushes a
phone alert via ntfy.sh the moment one appears:

  - Amazon  : hiring.amazon.ca GraphQL API (checked every run / 60s)
  - Sysco   : careers.sysco.ca (Radancy) Calgary/Rocky View (every 5th run)
  - YYC     : yyc.careers Calgary Airport Authority board (every 5th run)
  - Job Bank: jobbank.gc.ca "warehouse Calgary" + "airport Calgary"
              (every 5th run) — catches most other Calgary employers

Runs on a GitHub Actions schedule; each workflow run loops several checks
60 s apart so coverage stays near-minute despite the 5-minute cron floor.
state.json is persisted between runs with actions/cache.

ntfy topics are read from the environment (set as repo secrets — this repo
is public, so they must never be hardcoded here):
    NTFY_TOPIC_AMAZON, NTFY_TOPIC_OTHER

A run that starts with no memory (first run / lost cache) records every job
it sees WITHOUT alerting, so the phone is never spammed with old postings.

Manual use:
    python3 watcher.py               # one check
    python3 watcher.py --loop 5      # five checks, 60 s apart
    python3 watcher.py --force-all   # check every source now, ignore gating
"""

import datetime
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from html import unescape

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE_DIR, "state.json")
LOG_FILE = os.path.join(BASE_DIR, "watcher.log")
DASHBOARD = os.path.join(BASE_DIR, "jobs.html")

IS_MAC = sys.platform == "darwin" and not os.environ.get("GITHUB_ACTIONS")

USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

# Calgary downtown; 100 km covers Calgary, Balzac (YYC1), Airdrie, Okotoks
CALGARY_LAT = 51.0447
CALGARY_LNG = -114.0719
RADIUS_KM = 100

# Lowercase place names treated as "Calgary area" when filtering text locations
AREA_NAMES = ("calgary", "rocky view", "rocky-view", "balzac", "airdrie",
              "chestermere", "okotoks", "crossfield", "de winton", "dewinton")

AMAZON_URL = "https://hiring.amazon.ca/app#/jobSearch"

# Phone push via https://ntfy.sh — topic names come from repo secrets.
NTFY_ENABLED = True
NTFY_TOPIC_AMAZON = os.environ.get("NTFY_TOPIC_AMAZON", "")
NTFY_TOPIC_OTHER = os.environ.get("NTFY_TOPIC_OTHER", "")

FORGET_AFTER_HOURS = 72      # re-alert only if a job vanishes this long
FAIL_ALERT_THRESHOLD = 30    # warn once after this many consecutive failures

AMAZON_QUERY = (
    "query searchJobCardsByLocation($searchJobRequest: SearchJobRequest!) {"
    " searchJobCardsByLocation(searchJobRequest: $searchJobRequest) {"
    " nextToken jobCards { jobId jobTitle jobType employmentType city state"
    " postalCode locationName totalPayRateMin totalPayRateMax scheduleCount } } }"
)


def log(msg):
    line = f"{datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def http_get(url, headers=None, data=None, timeout=40):
    h = {"User-Agent": USER_AGENT}
    if headers:
        h.update(headers)
    req = urllib.request.Request(url, data=data, headers=h)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def strip_tags(s):
    return unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", s))).strip()


def in_area(text):
    t = (text or "").lower()
    return any(name in t for name in AREA_NAMES)


# ---------------------------------------------------------------- sources

def fetch_amazon():
    payload = {
        "operationName": "searchJobCardsByLocation",
        "variables": {
            "searchJobRequest": {
                "locale": "en-CA",
                "country": "Canada",
                "keyWords": "",
                "equalFilters": [],
                "containFilters": [{"key": "isPrivateSchedule", "val": ["false"]}],
                "rangeFilters": [],
                "orFilters": [],
                "dateFilters": [],
                "sorters": [{"fieldName": "totalPayRateMax", "ascending": "false"}],
                "pageSize": 100,
                "geoQueryClause": {"lat": CALGARY_LAT, "lng": CALGARY_LNG,
                                   "unit": "km", "distance": RADIUS_KM},
            }
        },
        "query": AMAZON_QUERY,
    }
    body = http_get(
        "https://e5mquma77feepi2bdn4d6h3mpu.appsync-api.us-east-1.amazonaws.com/graphql",
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer token",
            "Country": "Canada",
            "iscanary": "false",
            "Origin": "https://hiring.amazon.ca",
            "Referer": "https://hiring.amazon.ca/",
        },
        data=json.dumps(payload).encode(),
    )
    parsed = json.loads(body)
    if "errors" in parsed:
        raise RuntimeError(f"Amazon API error: {parsed['errors']}")
    jobs = []
    for card in parsed["data"]["searchJobCardsByLocation"]["jobCards"] or []:
        pay = ""
        if card.get("totalPayRateMin"):
            pay = f"${card['totalPayRateMin']}"
            if card.get("totalPayRateMax") and card["totalPayRateMax"] != card["totalPayRateMin"]:
                pay += f"-${card['totalPayRateMax']}"
            pay += "/hr"
        jobs.append({
            "id": card["jobId"],
            "title": card.get("jobTitle", "Amazon job"),
            "company": "Amazon",
            "location": f"{card.get('city', '?')}, {card.get('state', 'AB')}",
            "pay": pay,
            "url": f"https://hiring.amazon.ca/app#/jobDetail?jobId={card['jobId']}&locale=en-CA",
        })
    return jobs


def fetch_sysco():
    qs = urllib.parse.urlencode({
        "ActiveFacetID": 0, "CurrentPage": 1, "RecordsPerPage": 500,
        "SearchResultsModuleName": "Search Results",
        "SearchFiltersModuleName": "Search Filters",
        "SortCriteria": 0, "SortDirection": 0, "SearchType": 5,
    })
    body = http_get(f"https://careers.sysco.ca/en/search-jobs/results?{qs}",
                    headers={"Accept": "application/json"})
    results = json.loads(body).get("results", "")
    jobs = []
    for url, title in re.findall(
            r'href="(/en/job/[^"]+)"[^>]*>\s*<h2>([^<]+)</h2>', results):
        city_slug = url.split("/")[3]
        if not in_area(city_slug.replace("-", " ")):
            continue
        jobs.append({
            "id": url.rstrip("/").split("/")[-1],
            "title": unescape(title).strip(),
            "company": "Sysco Canada",
            "location": city_slug.replace("-", " ").title() + ", AB",
            "pay": "",
            "url": "https://careers.sysco.ca" + url,
        })
    return jobs


def fetch_yyc():
    body = http_get("https://yyc.careers/Opportunities.aspx")
    jobs = []
    for jid, inner in re.findall(
            r'<a[^>]*Opportunity\.aspx\?JobId=(\d+)[^>]*>(.*?)</a>', body, re.S):
        title = strip_tags(inner)
        if not title:
            continue
        jobs.append({
            "id": jid,
            "title": title,
            "company": "Calgary Airport Authority (YYC)",
            "location": "Calgary Airport, AB",
            "pay": "",
            "url": f"https://yyc.careers/Opportunity.aspx?JobId={jid}",
        })
    # de-dupe repeated links to the same posting
    return list({j["id"]: j for j in jobs}.values())


# Job Bank keyword searches match loosely, so keep only jobs whose title
# looks warehouse- or airport-related
RELEVANT = ("warehouse", "picker", "forklift", "shipper", "receiver", "selector",
            "material handler", "loader", "packag", "packer", "distribution",
            "inventory", "freight", "shipping", "dock", "labour", "labor",
            "ramp", "cargo", "airport", "airline", "baggage", "ground handl",
            "logistics", "stock", "pallet", "lumper", "driver", "assembl",
            "production", "yard", "machine operator", "general help")


def fetch_jobbank():
    jobs = {}
    for keywords in ("warehouse Calgary", "airport Calgary"):
        qs = urllib.parse.urlencode({"searchstring": keywords, "sort": "D"})
        body = http_get(f"https://www.jobbank.gc.ca/jobsearch/jobsearch?{qs}")
        for article in re.split(r"<article", body)[1:]:
            m_id = re.search(r'href="/jobsearch/jobposting/(\d+)', article)
            m_title = re.search(r'<span class="noctitle">\s*(.*?)\s*</span>', article, re.S)
            m_biz = re.search(r'<li class="business">\s*(.*?)\s*</li>', article, re.S)
            m_loc = re.search(r'<li class="location">(.*?)</li>', article, re.S)
            m_sal = re.search(r'<li class="salary">(.*?)</li>', article, re.S)
            if not (m_id and m_title and m_loc):
                continue
            loc = strip_tags(m_loc.group(1)).replace("Location", "").strip()
            if not in_area(loc):
                continue
            title_l = strip_tags(m_title.group(1)).lower()
            if not any(k in title_l for k in RELEVANT):
                continue
            jid = m_id.group(1)
            sal = strip_tags(m_sal.group(1)).replace("Salary:", "").strip() if m_sal else ""
            jobs[jid] = {
                "id": jid,
                "title": strip_tags(m_title.group(1)),
                "company": strip_tags(m_biz.group(1)) if m_biz else "?",
                "location": loc,
                "pay": sal,
                "url": f"https://www.jobbank.gc.ca/jobsearch/jobposting/{jid}",
            }
    return list(jobs.values())


# name -> (fetcher, run every N runs). Amazon every run; boards every 5th.
SOURCES = {
    "amazon": (fetch_amazon, 1),
    "sysco": (fetch_sysco, 5),
    "yyc": (fetch_yyc, 5),
    "jobbank": (fetch_jobbank, 5),
}

SOURCE_LABELS = {
    "amazon": "Amazon (hiring.amazon.ca)",
    "sysco": "Sysco Canada (Calgary / Rocky View)",
    "yyc": "Calgary Airport Authority (yyc.careers)",
    "jobbank": "Job Bank Canada (warehouse + airport, Calgary)",
}


# ---------------------------------------------------------------- alerts

def osascript_notify(title, body):
    if not IS_MAC:
        return
    def esc(s):
        return s.replace("\\", "\\\\").replace('"', '\\"')
    subprocess.run(
        ["osascript", "-e",
         f'display notification "{esc(body)}" with title "{esc(title)}" sound name "Glass"'],
        check=False,
    )


def ntfy_push(title, body, topic, click_url=None):
    if not NTFY_ENABLED:
        return
    if not topic:
        log("ntfy push skipped: no topic configured (set NTFY_TOPIC_* secrets)")
        return
    try:
        # HTTP headers must be latin-1: drop emoji etc. from header values
        safe_title = title.encode("latin-1", errors="ignore").decode("latin-1").strip()
        headers = {"Title": safe_title, "Priority": "urgent", "Tags": "rotating_light"}
        if click_url:
            headers["Click"] = click_url
        req = urllib.request.Request(f"https://ntfy.sh/{topic}",
                                     data=body.encode(), headers=headers)
        urllib.request.urlopen(req, timeout=15)
    except Exception as e:
        log(f"ntfy push failed: {e}")


def describe(job):
    pay = f" | {job['pay']}" if job.get("pay") else ""
    return f"{job['title']} — {job['company']}, {job['location']}{pay}"


def alert(new_jobs, topic, kind):
    """kind is 'Amazon' or 'Calgary' — keeps the two alert channels distinct."""
    n = len(new_jobs)
    plural = "s" if n > 1 else ""
    if kind == "Amazon":
        title = f"🚨 AMAZON — {n} new job{plural} near Calgary!"
    else:
        by_src = {}
        for j in new_jobs:
            by_src.setdefault(j.get("source", "?"), []).append(j)
        src_summary = ", ".join(f"{s} ({len(js)})" for s, js in by_src.items())
        title = f"🔔 {n} new Calgary job{plural} — {src_summary}"
    body = "\n".join(describe(j) for j in new_jobs[:5])
    osascript_notify(title, "; ".join(describe(j) for j in new_jobs[:3]))
    ntfy_push(title, body, topic, click_url=new_jobs[0].get("url"))
    if IS_MAC:
        first = new_jobs[0]
        subprocess.run(
            ["say", f"{kind} job alert! {n} new job{plural}. "
                    f"{first['title']} at {first['company']}. Apply now!"],
            check=False,
        )


# ------------------------------------------------------------- dashboard

def write_dashboard(state):
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sections = []
    total = 0
    for src in SOURCES:
        jobs = state.get("current", {}).get(src, [])
        total += len(jobs)
        rows = ""
        for j in sorted(jobs, key=lambda x: x["title"]):
            first_seen = state.get("seen", {}).get(f"{src}:{j['id']}", {}).get("first_seen", "")
            first_seen = first_seen[:16].replace("T", " ") if first_seen else ""
            rows += (f"<tr><td><a href='{j['url']}' target='_blank'>{j['title']}</a></td>"
                     f"<td>{j['company']}</td><td>{j['location']}</td>"
                     f"<td>{j.get('pay', '')}</td><td>{first_seen}</td></tr>")
        if not rows:
            rows = "<tr><td colspan='5' class='empty'>No openings right now — watching…</td></tr>"
        checked = state.get("last_checked", {}).get(src, "never")[:19].replace("T", " ")
        sections.append(
            f"<h2>{SOURCE_LABELS[src]} <span class='count'>{len(jobs)}</span>"
            f"<span class='checked'>checked {checked}</span></h2>"
            f"<table><tr><th>Job</th><th>Company</th><th>Location</th>"
            f"<th>Pay</th><th>First seen</th></tr>{rows}</table>"
        )
    html_doc = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta http-equiv="refresh" content="60">
<title>Calgary Job Watch — {total} open</title>
<style>
body{{font-family:-apple-system,Helvetica,sans-serif;margin:24px auto;max-width:960px;
background:#11151c;color:#e8eaed;padding:0 16px}}
h1{{font-size:22px}} h2{{font-size:16px;margin:28px 0 8px;border-bottom:1px solid #2c3440;padding-bottom:6px}}
.count{{background:#2e7d32;border-radius:10px;padding:1px 9px;margin-left:8px;font-size:13px}}
.checked{{float:right;color:#8a93a3;font-size:12px;font-weight:normal}}
table{{width:100%;border-collapse:collapse;font-size:14px}}
td,th{{text-align:left;padding:7px 10px;border-bottom:1px solid #1f2630}}
th{{color:#8a93a3;font-weight:600;font-size:12px;text-transform:uppercase}}
a{{color:#7ab8ff;text-decoration:none}} a:hover{{text-decoration:underline}}
.empty{{color:#8a93a3;font-style:italic}}
.sub{{color:#8a93a3;font-size:13px}}
</style></head><body>
<h1>🏗️ Calgary Job Watch <span class='count'>{total} open</span></h1>
<p class="sub">Updated {now} · alerts fire the moment something new appears</p>
{''.join(sections)}
</body></html>"""
    with open(DASHBOARD, "w") as f:
        f.write(html_doc)


# ------------------------------------------------------------------ main

def run_once(force=False):
    state = load_state()
    state["run_count"] = state.get("run_count", 0) + 1
    now = datetime.datetime.now(datetime.timezone.utc)
    now_iso = now.isoformat()
    cutoff = now - datetime.timedelta(hours=FORGET_AFTER_HOURS)

    seen = state.setdefault("seen", {})
    # No memory at all means first run or lost cache: record everything
    # silently instead of spamming the phone with already-posted jobs.
    baseline = not seen
    current = state.setdefault("current", {})
    failures = state.setdefault("failures", {})
    last_checked = state.setdefault("last_checked", {})
    new_jobs = []
    counts = {}

    for src, (fetcher, interval) in SOURCES.items():
        if not force and (state["run_count"] - 1) % interval != 0:
            continue
        try:
            jobs = fetcher()
        except Exception as e:
            failures[src] = failures.get(src, 0) + 1
            log(f"[{src}] FETCH FAILED ({failures[src]} in a row): {e}")
            if failures[src] == FAIL_ALERT_THRESHOLD:
                ntfy_push("Job watcher problem",
                          f"{src} checks failing repeatedly — that source may be "
                          "blocking cloud runners. Check the Actions logs.",
                          NTFY_TOPIC_OTHER)
            continue
        failures[src] = 0
        last_checked[src] = now_iso
        current[src] = jobs
        counts[src] = len(jobs)
        for job in jobs:
            key = f"{src}:{job['id']}"
            prev = seen.get(key)
            is_new = prev is None
            if prev is not None:
                try:
                    is_new = datetime.datetime.fromisoformat(prev["last_seen"]) < cutoff
                except (KeyError, ValueError):
                    is_new = True
            if is_new:
                job["source"] = src
                new_jobs.append(job)
                seen[key] = {"first_seen": now_iso, "last_seen": now_iso,
                             "title": job["title"]}
            else:
                seen[key]["last_seen"] = now_iso

    state["seen"] = {
        k: v for k, v in seen.items()
        if datetime.datetime.fromisoformat(v["last_seen"]) >= cutoff
    }

    write_dashboard(state)

    if new_jobs and baseline:
        log(f"Baseline run — recorded {len(new_jobs)} existing jobs WITHOUT alerting.")
    elif new_jobs:
        for j in new_jobs:
            log(f"NEW [{j['source']}]: {describe(j)} ({j['url']})")
        amazon_new = [j for j in new_jobs if j["source"] == "amazon"]
        other_new = [j for j in new_jobs if j["source"] != "amazon"]
        if amazon_new:
            alert(amazon_new, NTFY_TOPIC_AMAZON, "Amazon")
        if other_new:
            alert(other_new, NTFY_TOPIC_OTHER, "Calgary")
        if IS_MAC:
            # open the most urgent group exactly once: Amazon first
            prio = amazon_new or other_new
            if len(prio) == 1:
                target = prio[0]["url"]
            else:
                target = AMAZON_URL if amazon_new else DASHBOARD
            subprocess.run(["open", target], check=False)
    else:
        summary = ", ".join(f"{s}:{n}" for s, n in counts.items()) or "no sources due"
        log(f"Checked ({summary}) — nothing new.")

    save_state(state)


def main():
    if "--test-notify" in sys.argv:
        alert([{"id": "test", "source": "amazon",
                "title": "TEST — Amazon Fulfillment Centre Associate",
                "company": "Amazon", "location": "Calgary, AB", "pay": "$22/hr",
                "url": "https://hiring.amazon.ca/app#/jobSearch"}],
              NTFY_TOPIC_AMAZON, "Amazon")
        log("Test notification fired.")
        return

    force = "--force-all" in sys.argv
    loops = 1
    if "--loop" in sys.argv:
        try:
            loops = int(sys.argv[sys.argv.index("--loop") + 1])
        except (IndexError, ValueError):
            loops = 5
    for i in range(loops):
        run_once(force=force)
        if i < loops - 1:
            time.sleep(60)


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


if __name__ == "__main__":
    main()
