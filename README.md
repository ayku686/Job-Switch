# LinkedIn Job Outreach Kit

This kit searches public LinkedIn hiring posts in your logged-in Chrome session,
extracts contact details, lets you review the results, and can send your resume
using your own Gmail account. It does not automate LinkedIn login or actions
such as connecting or messaging.

## How it works

`scrape.py` runs the searches in `queries.txt` and uses `harvest.js` to collect
contact details into `leads.csv`. `report.py` creates a reviewable HTML report.
`build_send_list.py` filters and splits leads by sender account. `send.py` sends
your `email_template.txt` with your resume attached and records sent addresses
so they are not sent again.

## Requirements

- Windows, Google Chrome, and Python 3.9 or newer
- Your own LinkedIn account, logged in through the dedicated Chrome profile below
- One to three Gmail accounts with 2-Step Verification and Gmail App Passwords

The Python scripts use the standard library; no `pip install` is required.

## Setup

### 1. Start with your own local files

If this folder was copied from someone else, remove their local configuration,
resume, generated leads, and send history before adding yours. Run this from the
project folder in PowerShell:

```powershell
Remove-Item -ErrorAction SilentlyContinue config.json, .env, email_template.txt, *.pdf, `
  leads.csv, send_list.csv, send_a*.csv, sent_log.csv, failed_log.csv, `
  scrape_log.csv, lead_report.html, last_run.txt, scrape_cursor.txt, *.log
```

Create your local settings and email template:

```powershell
Copy-Item .env.example .env
Copy-Item email_template.example.txt email_template.txt
```

### 2. Fill in your details

Edit `.env` with your own values:

- `SENDER_NAME`: your name
- `RESUME_FILE`: your PDF filename, normally `resume.pdf`
- `SEED_ADDRESSES`: optional, comma-separated inboxes you own for delivery tests; leave empty if unused
- `GMAIL_ACCOUNT_1_EMAIL`, `GMAIL_ACCOUNT_1_APP_PASSWORD`, and `GMAIL_ACCOUNT_1_DAILY_CAP`: your first sending account
- Add the matching `_2` and `_3` variables for additional accounts. Remove example placeholders for accounts you will not use.

Use a Gmail App Password, not your normal Gmail password. Create one at
<https://myaccount.google.com/apppasswords> after enabling 2-Step Verification.
Keep the daily cap at 370. Enter credentials directly in your local `.env`; do
not share or commit that file. `.env.example` contains placeholders only.

Rewrite `email_template.txt` completely with your own experience and contact
details. The example template contains another person's details. Keep its first
line in the form `Subject: ...`.

Copy your resume PDF into this folder. Make its filename match `RESUME_FILE`.

### 3. Personalize searches and filters

The shipped searches target software engineering roles in India. Update
`queries.txt` with your target roles and locations.

If your field or country differs, update the role keywords and country-specific
filters in `harvest.js`, including `CORE`, `ADJ`, `HARD`, `phone`, and `USROLE`.
The phone pattern is currently for Indian numbers, and `USROLE` excludes posts
requiring US work authorization.

In both `build_send_list.py` and `send.py`, change `OWN_COMPANY = ("bottomline",)`
to a distinctive part of your employer's email domain. Use `()` if there is no
company to exclude.

### 4. Start Chrome and LinkedIn

In PowerShell, launch Chrome with a dedicated profile and remote debugging:

```powershell
& "C:\Program Files\Google\Chrome\Application\chrome.exe" `
  --remote-debugging-port=9222 `
  --user-data-dir="$env:LOCALAPPDATA\ChromeScrape" `
  --disable-backgrounding-occluded-windows `
  --disable-renderer-backgrounding `
  --disable-background-timer-throttling
```

If Chrome is installed elsewhere, change the executable path. In the new Chrome
window, log in to LinkedIn, open the feed or search page, set zoom to 50%, and
leave the LinkedIn tab open and active. Verify Chrome's debugging endpoint:

```powershell
curl.exe -s http://127.0.0.1:9222/json/version
```

### 5. Run and review

From the project folder, test two queries first:

```powershell
python scrape.py --limit 2
```

Check that `leads.csv` contains results. Then run the full search:

```powershell
python scrape.py
python report.py
Invoke-Item lead_report.html
```

Review the posts and recipients before sending. Build one send list per Gmail
account, replacing `2` with the number of accounts in `.env`:

```powershell
python build_send_list.py --accounts 2
```

Send a test message to an inbox you own and confirm the content and resume
attachment:

```powershell
python send.py --test your.address@gmail.com --account-index 0
```

When the test looks right, send each account's list separately. For two accounts:

```powershell
python send.py --list send_a0.csv --account-index 0
python send.py --list send_a1.csv --account-index 1
```

For three accounts, also send `send_a2.csv` with `--account-index 2`. With one
account, build with `--accounts 1` and send only `send_a0.csv`.

## Optional daily automation

`daily_run.ps1` runs the scrape, report, list building, and sending pipeline. If
you use it, edit its Chrome path and make its `--accounts` count and send
commands match the account blocks in `.env`. Run it manually once before adding
it to Windows Task Scheduler. The computer must be on and you must be signed in
when the task runs. From the project folder, register a daily 10 PM task with:

```powershell
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
  -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$PWD\daily_run.ps1`"" `
  -WorkingDirectory $PWD
$trigger = New-ScheduledTaskTrigger -Daily -At 22:00
Register-ScheduledTask -TaskName "LinkedInOutreachDaily" -Action $action -Trigger $trigger
```

Treat `.env`, resumes, lead CSVs, reports, and send logs as private. Review
recipients and send only relevant, appropriate outreach.
