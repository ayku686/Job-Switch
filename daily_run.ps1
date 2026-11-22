# daily_run.ps1 — scheduled daily outreach pipeline (scrape -> report -> build -> send)
$ErrorActionPreference = "Continue"
Set-Location $PSScriptRoot
$log = Join-Path $PSScriptRoot "daily_run.log"
function Log($m) { "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')  $m" | Tee-Object -FilePath $log -Append }

# once per calendar day, no matter how many logons
$stamp = Join-Path $PSScriptRoot "last_run.txt"
$today = (Get-Date).ToString("yyyy-MM-dd")
if ((Test-Path $stamp) -and ((Get-Content $stamp) -eq $today)) { exit 0 }
# stamp BEFORE running: a failed run must not retry the same day (LinkedIn checkpoint rule)
$today | Set-Content $stamp

Log "=== daily run start ==="
$py = (Get-Command python -ErrorAction SilentlyContinue).Source
if (-not $py) { Log "ABORT: python not on PATH"; exit 1 }
$chrome = "C:\Program Files\Google\Chrome\Application\chrome.exe"

# ensure Chrome with the debug port + a LinkedIn tab
$alive = $false
try { Invoke-RestMethod "http://127.0.0.1:9222/json/version" -TimeoutSec 3 | Out-Null; $alive = $true } catch {}
if (-not $alive) {
    Log "starting Chrome (ChromeScrape profile)"
    Start-Process $chrome -ArgumentList @(
        "--remote-debugging-port=9222",
        "--user-data-dir=$env:LOCALAPPDATA\ChromeScrape",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-background-timer-throttling",
        "https://www.linkedin.com/feed/")
    Start-Sleep 40
} else {
    $tabs = Invoke-RestMethod "http://127.0.0.1:9222/json/list" -TimeoutSec 5
    if (-not ($tabs | Where-Object { $_.type -eq "page" -and $_.url -like "*linkedin.com*" })) {
        Log "opening LinkedIn tab in running Chrome"
        Start-Process $chrome -ArgumentList "--user-data-dir=$env:LOCALAPPDATA\ChromeScrape", "https://www.linkedin.com/feed/"
        Start-Sleep 25
    }
}

Log "scrape (6 queries, round-robin)"
& $py scrape.py --rotate 6 *>> $log
Log "report"
& $py report.py *>> $log
Log "build send lists"
& $py build_send_list.py --accounts 2 *>> $log
Log "send account 0"
& $py send.py --list send_a0.csv --account-index 0 *>> $log
Log "send account 1"
& $py send.py --list send_a1.csv --account-index 1 *>> $log
Log "=== daily run complete ==="
