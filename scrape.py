#!/usr/bin/env python3
"""
Scrape LinkedIn hiring posts for recruiter contact details.

Drives YOUR already-logged-in Chrome via the DevTools Protocol (Windows/Linux/
macOS), so there is no login automation, no API key and no third-party service.
Nothing is filtered at this stage: every lead is kept and questionable ones are
FLAGGED, so you decide what to contact later.

Chrome must be running with --remote-debugging-port=9222 and a dedicated
--user-data-dir (see README / chrome_cdp.py), with LinkedIn logged in.

    python scrape.py                 # all queries in queries.txt
    python scrape.py --limit 2       # first 2 queries (always do this first)
    python scrape.py --start 40      # resume at query 40 after a crash
    python scrape.py --export        # re-export whatever is still in the browser

Output: leads.csv + scrape_log.csv
"""
import argparse, csv, json, random, sys, time
from datetime import datetime
from pathlib import Path

import chrome_cdp

HERE = Path(__file__).parent
QUERIES = HERE / "queries.txt"
HARVEST = HERE / "harvest.js"
LOG = HERE / "scrape_log.csv"
OUT = HERE / "leads.csv"
CURSOR = HERE / "scrape_cursor.txt"

LOAD_WAIT = 12          # seconds to wait for the search page to render
POLL_MAX = 18           # max 3s polls waiting for the harvester to finish
PACE_MIN, PACE_MAX = 45, 90   # randomized gap between queries. A fixed 26s
                              # tripped a checkpoint on a fresh session at
                              # query 3; jitter + slower holds ~1 search/min


def js_in_window(js: str, timeout=120) -> str:
    """Run JS in the logged-in LinkedIn tab via Chrome DevTools Protocol."""
    ws = chrome_cdp.linkedin_tab()
    if ws is None:
        return "NO_LINKEDIN_WINDOW"
    return chrome_cdp.evaluate(ws, js, timeout=timeout)


def navigate(url: str) -> str:
    ws = chrome_cdp.linkedin_tab()
    if ws is None:
        return "NO_LINKEDIN_WINDOW"
    chrome_cdp.navigate(ws, url)
    return "ok"


def pace():
    time.sleep(random.uniform(PACE_MIN, PACE_MAX))


def checkpointed() -> bool:
    """True if LinkedIn is showing a security interstitial instead of results.
    Continuing past a soft check is what turns it into a real restriction."""
    r = js_in_window(
        "(function(){var t=document.body?document.body.innerText:'';"
        "return (/unusual search traffic|verify your identity|security (check|verification)|"
        "activity on your account has been paused/i.test(t)"
        "||location.href.indexOf('/checkpoint')>-1)?'YES':'NO';})()")
    return r == "YES"


def goto_home() -> bool:
    """Click the nav Home button like a person would; fall back to navigating."""
    r = js_in_window(
        "(function(){var a=document.querySelector("
        "'a[data-test-global-nav-link=\"home\"],"
        "a[href^=\"https://www.linkedin.com/feed\"],a[href=\"/feed/\"]');"
        "if(a){a.click();return 'clicked';}return 'no';})()")
    if r == "clicked":
        return True
    return navigate("https://www.linkedin.com/feed/") == "ok"


def human_scroll(steps):
    """Irregular scrolls with pauses; occasionally scrolls back up a little."""
    for _ in range(steps):
        dy = random.randint(250, 900)
        if random.random() < 0.2:
            dy = -random.randint(150, 450)
        js_in_window(f"window.scrollBy({{top:{dy},behavior:'smooth'}});'ok'")
        time.sleep(random.uniform(1.5, 4.5))


def browse_feed(tag):
    """Bracket the search burst with normal-looking feed activity."""
    print(f"{tag}: home feed + scroll", flush=True)
    if goto_home():
        time.sleep(random.uniform(4, 8))
        human_scroll(random.randint(5, 9))


def enc(q: str) -> str:
    return q.replace(" ", "%20").replace('"', "%22").replace("#", "%23").replace("/", "%2F")


def log_row(row):
    new = not LOG.exists()
    with open(LOG, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["n", "query", "posts", "emails_seen", "new_added",
                        "ticks", "secs", "stopped_by", "at"])
        w.writerow(row)


def scrub(s):
    """Strip lone surrogates (LinkedIn posts use styled unicode like 𝐛𝐨𝐥𝐝)
    which csv/utf-8 cannot encode."""
    if not isinstance(s, str):
        return s
    return "".join(c for c in s if not (0xD800 <= ord(c) <= 0xDFFF))


def drain():
    """Pull the store back and CLEAR it, so localStorage never approaches the
    5MB cap (setItem fails silently past it) and no single CDP return
    gets large enough to wedge the renderer."""
    raw = js_in_window(
        "(function(){var s=localStorage.getItem('__ctx2')||'{}';"
        "localStorage.removeItem('__ctx2');return s;})()", timeout=180)
    try:
        return json.loads(raw)
    except Exception:
        print(f"  WARN drain failed, got: {raw[:160]}")
        return {}


def export(d=None):
    if d is None:
        raw = js_in_window("JSON.stringify(JSON.parse(localStorage.getItem('__ctx2')||'{}'))", timeout=180)
        try:
            d = json.loads(raw)
        except Exception:
            print(f"export failed, got: {raw[:200]}")
            return 0
    rows = []
    for email, h in sorted(d.items()):
        rows.append({
            "email": scrub(email if not email.startswith("nomail:") else ""),
            "apply_method": h.get("method",""),
            "relevance": h.get("tier",""),
            "role_text": scrub((h.get("text","") or "")[:160]),
            "author": scrub(h.get("author","")),
            "author_url": h.get("authorUrl",""),
            "post_url": h.get("postUrl",""),
            "phone": scrub(h.get("phones","")),
            "whatsapp": scrub(h.get("whatsapp","")),
            "form_link": scrub(h.get("forms","")),
            "ats_link": scrub(h.get("ats","")),
            "lnkd_link": scrub(h.get("lnkd","")),
            "is_jobseeker": h.get("seeker",0),
            "is_us_role": h.get("usrole",0),
            "is_certmill": h.get("certmill",0),
            "is_nontech": h.get("nontech",0),
            "is_listicle": h.get("listicle",0),
            "no_post_context": h.get("orphan",0),
            "source_query": scrub(h.get("q","")),
            "post_text": scrub((h.get("text","") or "").replace("\n"," "))[:600],
        })
    with open(OUT, "w", newline="", encoding="utf-8", errors="replace") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else ["email"])
        w.writeheader()
        w.writerows(rows)
    print(f"exported {len(rows)} rows -> {OUT.name}")
    return len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--start", type=int, default=1,
                    help="resume at query N (1-based). Keeps existing __ctx2.")
    ap.add_argument("--rotate", type=int, default=0, metavar="N",
                    help="scrape only N queries this run, resuming round-robin "
                         "where the last run stopped (scrape_cursor.txt). "
                         "Overrides --start/--limit. Keeps daily volume low.")
    ap.add_argument("--export", action="store_true")
    args = ap.parse_args()

    if args.export:
        export()
        return

    queries = [q.strip() for q in QUERIES.read_text(encoding="utf-8").splitlines() if q.strip()]
    n_all, base = len(queries), 0
    if args.rotate:
        base = (int(CURSOR.read_text().strip() or 0) if CURSOR.exists() else 0) % n_all
        queries = [queries[(base + k) % n_all] for k in range(min(args.rotate, n_all))]
        print(f"rotation: {len(queries)} queries starting at #{base + 1} of {n_all}")
    elif args.limit:
        queries = queries[:args.limit]
    harvest_js = HARVEST.read_text(encoding="utf-8")

    print(f"PHASE 2 — {len(queries)} queries, past-24h, post-first extraction")
    print(f"est. {len(queries)*(LOAD_WAIT+40+(PACE_MIN+PACE_MAX)/2)/60:.0f} min\n", flush=True)

    ACC = {}                      # python-side accumulator; localStorage stays small
    DRAIN_EVERY = 15
    if args.start <= 1:
        js_in_window("localStorage.removeItem('__ctx2');'cleared'")
    else:
        print(f"resuming at query {args.start}, keeping existing __ctx2\n", flush=True)

    browse_feed("warm-up")
    hit_checkpoint = False

    for i, q in enumerate(queries, 1):
        if not args.rotate and i < args.start:
            continue
        url = ("https://www.linkedin.com/search/results/content/?keywords="
               + enc(q) + "&datePosted=%22past-24h%22&sortBy=%22date_posted%22")
        try:
            if navigate(url) == "NO_LINKEDIN_WINDOW":
                print("ABORT: no logged-in LinkedIn window found")
                break
            time.sleep(LOAD_WAIT)
            if checkpointed():
                print("CHECKPOINT DETECTED — stopping for today. Clear the challenge "
                      "by hand in the ChromeScrape window and DO NOT scrape again today.")
                log_row([i, q, 0, 0, "", 0, 0, "checkpoint",
                         datetime.now().isoformat(timespec="seconds")])
                hit_checkpoint = True
                break
            # A busy renderer can blow the CDP timeout. One query dying
            # must never kill the run -- 8 queries were lost that way.
            js_in_window(harvest_js, timeout=240)
        except Exception as e:
            print(f"[{i}/{len(queries)}] {q[:42]:<44} SKIPPED: {type(e).__name__}", flush=True)
            log_row([i, q, 0, 0, "", 0, 0, "error", datetime.now().isoformat(timespec="seconds")])
            pace()
            continue

        s, total, stopped = {}, "", "timeout"
        try:
            for _ in range(POLL_MAX):
                time.sleep(3)
                st = js_in_window("JSON.stringify({d:window.__H2?window.__H2.done:0,f:window.__H2?window.__H2.flat:0})")
                try:
                    if json.loads(st).get("d") == 1:
                        stopped = "plateau_or_cap"
                        break
                except Exception:
                    pass

            stats = js_in_window(
                "(function(){var S=window.__H2;if(!S)return '{}';"
                "return JSON.stringify({posts:S.maxPosts,em:Object.keys(S.recs).length,"
                "ticks:S.tick,secs:Math.round((Date.now()-S.t0)/1000)});})()")
            try:
                s = json.loads(stats)
            except Exception:
                s = {}
            total = js_in_window("String(Object.keys(JSON.parse(localStorage.getItem('__ctx2')||'{}')).length)")

            if i % DRAIN_EVERY == 0 or i == len(queries):
                ACC.update(drain())
                total = str(len(ACC))
        except Exception as e:
            # The harvester keeps running in the page and flushes to __ctx2 on
            # its own, so a stats-read failure costs telemetry, not data.
            stopped = f"stats_error:{type(e).__name__}"

        print(f"[{i}/{len(queries)}] {q[:42]:<44} posts={s.get('posts',0):>4} "
              f"leads={s.get('em',0):>3} store={total:<5} {s.get('secs',0)}s {stopped}",
              flush=True)
        log_row([i, q, s.get("posts", 0), s.get("em", 0), total, s.get("ticks", 0),
                 s.get("secs", 0), stopped, datetime.now().isoformat(timespec="seconds")])

        if args.rotate:
            CURSOR.write_text(str((base + i) % n_all))
        if i < len(queries):
            pace()

    print("\nharvest done, exporting...", flush=True)
    ACC.update(drain())
    # a checkpoint page has no feed to scroll; go quiet instead
    if not hit_checkpoint:
        browse_feed("cool-down")
    export(ACC)


if __name__ == "__main__":
    main()

