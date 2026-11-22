#!/usr/bin/env python3
"""
Job-application mailer — Gmail SMTP, no browser required.

Reads send_list.csv (or --list FILE), sends email_template.txt with the resume attached,
throttled and resumable, across one or more Gmail accounts.

USAGE
    python3 send.py --dry-run          # show exactly what WOULD happen, send nothing
    python3 send.py --test you@x.com   # send ONE real email to yourself
    python3 send.py                    # real run, respects daily caps
    python3 send.py --limit 50         # cap this run at 50 sends total

SAFETY
  * Never sends to the same address twice  (sent_log.csv is the source of truth)
  * Hard per-account daily cap
  * Aborts the account immediately on a quota / rate-limit error
  * Randomised delay between sends, longer pause between batches
"""

import argparse, csv, os, random, smtplib, ssl, sys, time
from datetime import date, datetime
from email.message import EmailMessage
from pathlib import Path

HERE = Path(__file__).parent
LIST_FILE     = HERE / "send_list.csv"
TEMPLATE_FILE = HERE / "email_template.txt"
ENV_FILE = HERE / ".env"


def load_env_file():
    if not ENV_FILE.exists():
        return
    for raw_line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            os.environ.setdefault(key, value)


load_env_file()


# Resume: RESUME_FILE in .env, else the only PDF sitting next to this script
def _find_resume():
    name = os.environ.get("RESUME_FILE", "resume.pdf")
    if name and (HERE / name).exists():
        return HERE / name
    pdfs = sorted(HERE.glob("*.pdf"))
    return pdfs[0] if pdfs else HERE / "resume.pdf"
RESUME_FILE   = _find_resume()
SENT_LOG      = HERE / "sent_log.csv"
FAIL_LOG      = HERE / "failed_log.csv"

# pacing  — tuned so 350 emails ≈ 2.1 hours per account
BATCH_SIZE      = 50            # emails per batch
DELAY_MIN       = 8             # seconds between individual sends
DELAY_MAX       = 20            # avg ~14s -> 350 sends ≈ 82 min
BATCH_PAUSE_MIN = 300           # seconds between batches (5 min)
BATCH_PAUSE_MAX = 600           # (10 min)  -> 6 pauses ≈ 45 min

# SMTP errors that mean "stop using this account today"
QUOTA_MARKERS = ("quota", "rate limit", "5.4.5", "550-5.4.5", "too many", "exceeded")

# hard stop: never email your own company, even via --test or a hand-edited list
OWN_COMPANY = ("bottomline",)


def own_company(addr):
    return any(b in addr.split("@")[-1].lower() for b in OWN_COMPANY)


def preflight():
    """Fail with instructions, not a traceback — this is step one for a new user."""
    missing = []
    if not TEMPLATE_FILE.exists():
        missing.append(f"  {TEMPLATE_FILE.name}   ->  cp email_template.example.txt {TEMPLATE_FILE.name}")
    if not ENV_FILE.exists():
        missing.append(f"  {ENV_FILE.name}               ->  copy .env.example to .env and fill in your own values")
    if not RESUME_FILE.exists():
        missing.append("  your resume PDF     ->  put it in this folder (name it resume.pdf,"
                       " or set RESUME_FILE in .env)")
    if missing:
        sys.exit("\nSetup incomplete — missing:\n\n" + "\n".join(missing)
                 + "\n\nThen edit each one with your details. See README.md.\n")


def load_template():
    raw = TEMPLATE_FILE.read_text(encoding="utf-8")
    if raw.lower().startswith("subject:"):
        first, body = raw.split("\n", 1)
        return first.split(":", 1)[1].strip(), body.lstrip("\n")
    return "AI Engineer / Data Scientist — Exploring Roles", raw


def load_config():
    sender_name = os.environ.get("SENDER_NAME", "").strip()
    if not sender_name or "YOUR" in sender_name.upper():
        sys.exit("ERROR: set your own SENDER_NAME in .env")

    accounts = []
    for number in range(1, 4):
        email = os.environ.get(f"GMAIL_ACCOUNT_{number}_EMAIL", "").strip()
        password = os.environ.get(f"GMAIL_ACCOUNT_{number}_APP_PASSWORD", "").strip()
        cap = os.environ.get(f"GMAIL_ACCOUNT_{number}_DAILY_CAP", "370").strip()
        if not email and not password:
            continue
        if not email or not password:
            sys.exit(f"ERROR: set both email and app password for Gmail account {number} in .env")
        if "xxxx" in password.lower() or "paste" in password.lower():
            sys.exit(f"ERROR: replace the placeholder App Password for Gmail account {number} in .env")
        try:
            daily_cap = int(cap)
        except ValueError:
            sys.exit(f"ERROR: GMAIL_ACCOUNT_{number}_DAILY_CAP must be a whole number")
        accounts.append({"email": email, "app_password": password, "daily_cap": daily_cap})

    if not accounts:
        sys.exit("ERROR: configure at least one Gmail account in .env")

    seeds = [address.strip() for address in os.environ.get("SEED_ADDRESSES", "").split(",")
             if address.strip()]
    positions = [position.strip() for position in
                 os.environ.get("SEED_POSITIONS", "1,20,40").split(",") if position.strip()]
    try:
        seed_positions = [int(position) for position in positions]
    except ValueError:
        sys.exit("ERROR: SEED_POSITIONS must be comma-separated whole numbers")

    return {
        "sender_name": sender_name,
        "resume_file": os.environ.get("RESUME_FILE", "resume.pdf").strip(),
        "seed_addresses": seeds,
        "seed_positions": seed_positions,
        "accounts": accounts,
    }


def already_sent():
    if not SENT_LOG.exists():
        return set(), {}
    done, per_day = set(), {}
    with open(SENT_LOG, encoding="utf-8") as f:
        for r in csv.DictReader(f):
            done.add(r["email"].lower())
            key = (r["from_account"], r["sent_at"][:10])
            per_day[key] = per_day.get(key, 0) + 1
    return done, per_day


def log_row(path, header, row):
    new = not path.exists()
    with open(path, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(header)
        w.writerow(row)


def build_message(sender_email, sender_name, to_addr, subject, body, resume_bytes):
    m = EmailMessage()
    m["From"] = f"{sender_name} <{sender_email}>"
    m["To"] = to_addr
    m["Subject"] = subject
    m.set_content(body)
    if resume_bytes:
        m.add_attachment(resume_bytes, maintype="application", subtype="pdf",
                         filename=RESUME_FILE.name)
    return m


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="plan only, send nothing")
    ap.add_argument("--test", metavar="ADDR", help="send one real email to ADDR and exit")
    ap.add_argument("--limit", type=int, default=0, help="max sends this run (0 = use caps)")
    ap.add_argument("--account-index", type=int, default=None,
                    help="use ONLY this account (0-based). Lets you schedule each "
                         "account as a separate staggered job.")
    ap.add_argument("--spread", type=float, default=0,
                    help="spread this account's sends evenly over N hours. "
                         "Same volume, far lower requests/min. e.g. --spread 7")
    ap.add_argument("--bcc", type=int, default=0, metavar="N",
                    help="BCC MODE: send in batches of N recipients per message "
                         "(Gmail hard max = 100). Sender's own address goes in To: "
                         "so the message is not empty-To/BCC-only.")
    ap.add_argument("--bcc-gap", type=int, default=600, metavar="SEC",
                    help="seconds between BCC batches (default 600 = 10 min)")
    ap.add_argument("--list", metavar="CSV", default=None,
                    help="use this CSV instead of send_list.csv. Give each "
                         "account its own pre-split file -> physically impossible "
                         "for two accounts to share a recipient.")
    ap.add_argument("--subject", metavar="TEXT", default=None,
                    help="override the template's subject line, e.g. for a "
                         "targeted application to one specific posting")
    args = ap.parse_args()

    # --spread overrides the fixed pacing constants
    global DELAY_MIN, DELAY_MAX, BATCH_PAUSE_MIN, BATCH_PAUSE_MAX, BATCH_SIZE

    preflight()
    subject, body = load_template()
    if args.subject:
        subject = args.subject
    resume_bytes = RESUME_FILE.read_bytes() if RESUME_FILE.exists() else None
    cfg = load_config()

    # ---------- single test send ----------
    if args.test:
        if own_company(args.test):
            sys.exit(f"REFUSED: {args.test} is at your own company ({OWN_COMPANY})")
        # --account-index picks the sender; omit it to test EVERY account
        idxs = ([args.account_index] if args.account_index is not None
                else range(len(cfg["accounts"])))
        for i in idxs:
            acct = cfg["accounts"][i]
            try:
                msg = build_message(acct["email"], cfg["sender_name"],
                                    args.test, subject, body, resume_bytes)
                with smtplib.SMTP("smtp.gmail.com", 587, timeout=60) as s:
                    s.starttls(context=ssl.create_default_context())
                    s.login(acct["email"], acct["app_password"])
                    s.send_message(msg)
                print(f"  TEST SENT to {args.test}  FROM  {acct['email']}")
            except Exception as e:
                print(f"  TEST FAILED from {acct['email']}: {str(e)[:140]}")
            if len(list(idxs)) > 1:
                time.sleep(5)
        return

    # ---------- build the queue ----------
    list_path = Path(args.list) if args.list else LIST_FILE
    if not list_path.exists():
        sys.exit(f"ERROR: list file not found: {list_path}")
    print(f"list file: {list_path.name}")
    with open(list_path, encoding="utf-8") as f:
        all_targets = [r["email"].strip().lower() for r in csv.DictReader(f) if r["email"].strip()]
    done, per_day = already_sent()
    seeds = [s.lower() for s in cfg.get("seed_addresses", [])]

    # Preferred: give each account its OWN pre-split file via --list. Then no two
    # processes can ever share a recipient, by construction — nothing to compute,
    # nothing to get wrong.
    # Fallback (only when --list is omitted): partition the FULL, STABLE file by index.
    # Order matters — partition BEFORE subtracting already-sent, or each parallel
    # process sees a different-length list and `n % K` selects different addresses.
    if args.list or args.account_index is None:
        mine = all_targets
    else:
        K = len(cfg["accounts"])
        mine = [e for n, e in enumerate(all_targets) if n % K == args.account_index]
        print(f"PARTITION (fallback): account {args.account_index} of {K} -> "
              f"{len(mine)} of {len(all_targets)}")
    queue = [e for e in mine if e not in done and not own_company(e)]
    skipped_own = sum(1 for e in mine if own_company(e))
    if skipped_own:
        print(f"BLOCKED {skipped_own} own-company address(es) ({', '.join(OWN_COMPANY)})")

    today = date.today().isoformat()
    print(f"list={len(all_targets)}  already sent={len(done)}  my remaining={len(queue)}")

    if not queue:
        print("Nothing left to send.")
        return

    # ---------- per-account budgets ----------
    accounts = cfg["accounts"]
    if args.account_index is not None:
        if not (0 <= args.account_index < len(accounts)):
            sys.exit(f"ERROR: --account-index {args.account_index} out of range "
                     f"(have {len(accounts)} accounts)")
        accounts = [accounts[args.account_index]]
        print(f"restricted to account #{args.account_index}: {accounts[0]['email']}")

    plan = []
    for acct in accounts:
        cap = acct.get("daily_cap", 300)
        used = per_day.get((acct["email"], today), 0)
        budget = max(0, cap - used)
        plan.append((acct, budget, used, cap))
        print(f"  {acct['email']:<34} sent today {used:>3}/{cap:<4} -> budget {budget}")

    total_budget = sum(b for _, b, _, _ in plan)
    if args.limit:
        total_budget = min(total_budget, args.limit)
    print(f"can send {min(total_budget, len(queue))} this run")

    if args.spread > 0:
        per_acct = max(1, max(b for _, b, _, _ in plan))
        avg = (args.spread * 3600) / per_acct          # seconds per email
        DELAY_MIN = max(5, int(avg * 0.6))
        DELAY_MAX = max(DELAY_MIN + 5, int(avg * 1.4))
        BATCH_PAUSE_MIN = BATCH_PAUSE_MAX = 1          # long per-send delay replaces batch pauses
        BATCH_SIZE = 10 ** 9
        print(f"SPREAD MODE: {per_acct} emails over {args.spread}h "
              f"-> {DELAY_MIN}-{DELAY_MAX}s between sends "
              f"(~{60/avg:.2f} emails/min per account)")
    print()

    if args.dry_run:
        print("--- DRY RUN, nothing sent ---")
        for acct, budget, _, _ in plan:
            take = queue[:budget]
            queue = queue[budget:]
            print(f"{acct['email']} would send {len(take)}; first 3: {take[:3]}")
        return

    # ---------- BCC MODE ----------
    if args.bcc:
        size = min(args.bcc, 100)          # Gmail hard limit
        if args.bcc > 100:
            print(f"NOTE: --bcc {args.bcc} clamped to 100 (Gmail per-message limit)")
        sent_total = 0
        for acct, budget, _, _ in plan:
            if budget <= 0 or not queue:
                continue
            take = queue[:budget]
            batches = [take[i:i + size] for i in range(0, len(take), size)]
            print(f"=== {acct['email']}: {len(take)} recipients in {len(batches)} BCC batches of <={size} ===")
            ctx = ssl.create_default_context()
            for bi, group in enumerate(batches, 1):
                try:
                    m = EmailMessage()
                    m["From"] = f"{cfg['sender_name']} <{acct['email']}>"
                    m["To"] = acct["email"]          # avoids the empty-To / BCC-only signature
                    m["Bcc"] = ", ".join(group)
                    m["Subject"] = subject
                    m.set_content(body)
                    if resume_bytes:
                        m.add_attachment(resume_bytes, maintype="application",
                                         subtype="pdf", filename=RESUME_FILE.name)
                    with smtplib.SMTP("smtp.gmail.com", 587, timeout=120) as s:
                        s.starttls(context=ctx)
                        s.login(acct["email"], acct["app_password"])
                        s.send_message(m)
                    stamp = datetime.now().isoformat(timespec="seconds")
                    for addr in group:
                        log_row(SENT_LOG, ["email", "from_account", "sent_at"],
                                [addr, acct["email"], stamp])
                        if addr in queue:
                            queue.remove(addr)
                    sent_total += len(group)
                    print(f"  batch {bi}/{len(batches)}: {len(group)} recipients OK")
                except Exception as e:
                    err = str(e)
                    for addr in group:
                        log_row(FAIL_LOG, ["email", "from_account", "error", "at"],
                                [addr, acct["email"], err[:200],
                                 datetime.now().isoformat(timespec="seconds")])
                    print(f"  batch {bi}/{len(batches)} FAILED: {err[:140]}")
                    if any(mk in err.lower() for mk in QUOTA_MARKERS):
                        print(f"  !! quota hit on {acct['email']} — stopping this account")
                        break
                if bi < len(batches):
                    print(f"  --- waiting {args.bcc_gap}s before next batch ---")
                    time.sleep(args.bcc_gap)
            print()
        print(f"BCC RUN COMPLETE — {sent_total} recipients addressed. {len(queue)} remaining.")
        return

    sent_total = 0
    for acct, budget, _, _ in plan:
        if budget <= 0 or not queue:
            continue
        batch_targets = queue[:budget]
        # sprinkle seed addresses so you can check inbox-vs-spam afterwards
        for pos in sorted(cfg.get("seed_positions", [1, 90, 180, 270]), reverse=True):
            if seeds and pos < len(batch_targets):
                batch_targets.insert(pos, seeds[pos % len(seeds)])

        print(f"=== {acct['email']} -> {len(batch_targets)} emails ===")
        ctx = ssl.create_default_context()
        sent_this_acct, aborted = 0, False

        # ONE connection reused for the whole account: no repeated TLS handshake,
        # one login instead of hundreds. Faster and a far more normal pattern.
        conn = None
        def get_conn():
            nonlocal conn
            if conn is None:
                conn = smtplib.SMTP("smtp.gmail.com", 587, timeout=60)
                conn.starttls(context=ctx)
                conn.login(acct["email"], acct["app_password"])
            return conn

        for i, addr in enumerate(batch_targets, 1):
            if args.limit and sent_total >= args.limit:
                break
            try:
                msg = build_message(acct["email"], cfg["sender_name"],
                                    addr, subject, body, resume_bytes)
                try:
                    get_conn().send_message(msg)
                except (smtplib.SMTPServerDisconnected, smtplib.SMTPSenderRefused, OSError):
                    conn = None                      # reconnect once and retry
                    get_conn().send_message(msg)
                stamp = datetime.now().isoformat(timespec="seconds")
                is_seed = addr in seeds
                if not is_seed:
                    log_row(SENT_LOG, ["email", "from_account", "sent_at"],
                            [addr, acct["email"], stamp])
                    queue.remove(addr)
                    sent_total += 1
                sent_this_acct += 1
                tag = " [SEED]" if is_seed else ""
                print(f"  [{i}/{len(batch_targets)}] ok {addr}{tag}")
            except Exception as e:
                err = str(e)
                log_row(FAIL_LOG, ["email", "from_account", "error", "at"],
                        [addr, acct["email"], err[:200],
                         datetime.now().isoformat(timespec="seconds")])
                print(f"  [{i}] FAIL {addr}: {err[:120]}")
                if any(m in err.lower() for m in QUOTA_MARKERS):
                    print(f"  !! quota/rate limit hit on {acct['email']} — stopping this account")
                    aborted = True
                    break

            if i < len(batch_targets):
                if sent_this_acct % BATCH_SIZE == 0:
                    p = random.randint(BATCH_PAUSE_MIN, BATCH_PAUSE_MAX)
                    print(f"  --- batch of {BATCH_SIZE} done, pausing {p//60} min ---")
                    time.sleep(p)
                else:
                    time.sleep(random.randint(DELAY_MIN, DELAY_MAX))

        try:
            if conn is not None:
                conn.quit()
        except Exception:
            pass
        print(f"=== {acct['email']}: {sent_this_acct} sent"
              f"{' (ABORTED)' if aborted else ''} ===\n")

    print(f"RUN COMPLETE — {sent_total} new emails sent. "
          f"{len(queue)} remaining in queue.")


if __name__ == "__main__":
    main()
