# 1070 Park Avenue Listing Monitor

Watches sale listings at **1070 Park Avenue, New York, NY 10128** and emails
you the moment a new listing appears or an active listing changes price /
status.

All listing fetches go through [Firecrawl](https://docs.firecrawl.dev) —
regular HTTP gets blocked by StreetEasy, Compass, Zillow, Redfin, et al.

## Two modes

| mode | frequency | coverage | firecrawl calls | credits |
|---|---|---|---|---|
| **minimal** (default, daily) | 8 AM NYT | StreetEasy building page only | 1 × `scrape(markdown)` | ~1 |
| **full** (manual, monthly) | on demand | StreetEasy + Compass + Corcoran + Elliman + BHS + Sotheby's + CityRealty + Zillow + Realtor + Redfin | ~8 × `search` + ~5 × `extract` | ~250–300 |

The daily minimal run catches ~95% of what you'd see — StreetEasy is where
every listing worth caring about posts first. The monthly full sweep catches
the occasional "whisper" listing that shows up on Compass or BHS before it
hits StreetEasy (or never does).

Rentals are filtered out of both modes. Cross-source duplicates of the same
unit are collapsed in full mode (StreetEasy wins as canonical).

## Install

```bash
cd "C:\Users\acarr\OneDrive\Documents\Claude\Projects\1070-park-monitor"
py -3 -m venv venv
venv\Scripts\pip install -r requirements.txt
copy .env.example .env            # then fill in API keys
```

Required env vars (in `.env`):

| var | what |
|---|---|
| `FIRECRAWL_API_KEY` | from https://firecrawl.dev |
| `GMAIL_USER` | sending Gmail address |
| `GMAIL_APP_PASSWORD` | 16-char [Gmail app password](https://myaccount.google.com/apppasswords) |
| `ALERT_TO` | recipient |
| `ALERT_BCC` | backup copy — `you+tag@gmail.com` lets Gmail deliver a second distinct copy |

## Run manually

```bash
venv\Scripts\python src\run.py                  # minimal daily sweep (StreetEasy only, ~1 credit)
venv\Scripts\python src\run.py --mode full      # full monthly sweep (~250-300 credits)
venv\Scripts\python src\run.py --test           # smoke test: sends a test email, does NOT write state
venv\Scripts\python src\run.py --dry            # scrape + diff but skip email and state
```

Logs land in `logs/YYYY-MM-DD.log`.

## State & baseline

`state/seen.json` is the canonical memory of what's been seen. Format:

```json
{
  "<listing_url>": {
    "first_seen": "...",
    "last_seen":  "...",
    "last_price": 2350000,
    "last_status": "in_contract",
    "unit": "4E",
    "source": "streeteasy"
  }
}
```

- **First real run is a baseline.** When `seen.json` is empty, the first run
  populates it with every currently-active listing and suppresses the alert
  email — so you don't get a flood of "NEW!" alerts for units that were
  already listed. You'll start getting alerts on the *next* run, for anything
  that's genuinely new.
- To force a full re-notify, delete `state/seen.json` (or reset it to `{}`)
  — the very next run becomes a new baseline.

## Scheduled runs (Windows Task Scheduler)

Three tasks are registered:

```
1070Park_Morning              8:00 AM daily          enabled      minimal mode
1070Park_Afternoon            4:00 PM daily          DISABLED     (kept for future use)
1070Park_MonthlyFullSweep     9:00 AM on the 1st     enabled      full mode (via run-full.cmd)
```

All run under your user account (Interactive logon type) and will catch up
when the PC wakes if they missed the trigger time. The daily task retries
3× with 5-min gaps on failure; the monthly task uses default schtasks
retry behavior (no retry — if it misses day 1, it runs the next time the
PC wakes that day via `StartWhenAvailable`).

The monthly sweep invokes `run-full.cmd` in the project root, which cd's
to the project directory and runs `venv\Scripts\python.exe src\run.py --mode full`.
Edit that `.cmd` if you need to change what the monthly run does.

To trigger a full sweep on demand:

```powershell
Start-ScheduledTask -TaskName '1070Park_MonthlyFullSweep'
# or
cd 'C:\Users\acarr\OneDrive\Documents\Claude\Projects\1070-park-monitor'
.\venv\Scripts\python.exe src\run.py --mode full
```

Verify:

```powershell
schtasks /query /tn '1070Park_Morning'
schtasks /query /tn '1070Park_Afternoon'
```

Run one on demand:

```powershell
Start-ScheduledTask -TaskName '1070Park_Morning'
```

### Disable / remove

```powershell
# temporarily disable
Disable-ScheduledTask -TaskName '1070Park_Morning'
Disable-ScheduledTask -TaskName '1070Park_Afternoon'

# permanently remove
Unregister-ScheduledTask -TaskName '1070Park_Morning' -Confirm:$false
Unregister-ScheduledTask -TaskName '1070Park_Afternoon' -Confirm:$false
```

### Run whether user is logged on or not

The tasks are registered with `LogonType Interactive`, which requires the user
to be logged on. On a personal Windows machine this is usually fine. To have
them fire while signed out, re-register with a password:

```powershell
$cred = Get-Credential  # enter your Windows password when prompted
Set-ScheduledTask -TaskName '1070Park_Morning'  -User $cred.UserName -Password $cred.GetNetworkCredential().Password
Set-ScheduledTask -TaskName '1070Park_Afternoon' -User $cred.UserName -Password $cred.GetNetworkCredential().Password
```

## Credit budget

| activity | credits | monthly cost |
|---|---|---|
| daily minimal run (1 scrape) | ~1 | ~30 |
| monthly full run (searches + extracts) | ~250–300 | ~250–300 |
| **total** | | **~280–330 / month** |

Well within the 3,000-credit Hobby plan. Even running the full sweep weekly
instead of monthly (~1,200/month) fits.

If you need to cut further, `config.json > full_mode > search_domains` is
the knob — remove whichever portals are least useful.

## How it works end to end

### Minimal mode (daily)

1. **Scrape** (`scraper.scrape_minimal`) — one `fc.scrape(url, formats=['markdown'])`
   call against the StreetEasy building page.
2. **Parse** (`scraper.parse_streeteasy_markdown`) — locate the
   `## Available units` section and regex out each listing card's unit,
   URL, price, beds, baths, sqft, broker, image, and status. Uses the
   page's own "N units for sale" header as an expected-count sanity check.
3. **Fallback** (safety net only) — if the page claims N>0 listings but
   local parsing returned 0 (site redesign, markdown shape drift), call
   `fc.extract()` once to pull the data structurally. Logged prominently —
   this is the signal that the regex needs an update. Not the default path.
4. **Diff** → **Notify** → **Save state** (shared below).

### Full mode (monthly)

1. **Discovery** — for each portal in `config.json > full_mode > search_domains`,
   `fc.search("1070 Park Avenue … site:<domain>")` finds candidate URLs.
   StreetEasy and CityRealty building pages are always included directly.
2. **Extract** — candidate URLs are batched (10 per call) into
   `fc.extract()` with a structured JSON schema. The LLM handles site-specific
   layout variance and filters rentals via an `is_rental` boolean.
3. **Dedupe** — collapses duplicates of the same unit across portals,
   keyed by `(unit, price)`. StreetEasy wins, then CityRealty, then others.

### Shared (both modes)

4. **Diff** (`diff.diff_listings`) — compares fresh results to
   `state/seen.json`. Emits three change types:
   - `new`: listing_url not in state
   - `price`: `abs(new_price - last_price) >= $1`
   - `status`: status changed (active → in_contract, etc.)
5. **Notify** (`notifier.send_change_alert`) — one batched HTML email,
   High-Priority headers set. Subject encodes change type + unit + price.
   BCC to `acarras92+1070park@gmail.com` for a distinct second copy (Gmail
   routes plus-addresses through filters). If email send fails, state is
   still saved so the next run doesn't re-alert the same change.
6. **Save state** — `seen.json` written atomically.

## Adding another building later

All building-specific config lives in `config.json`. To monitor a second
building, you have two options:

1. **Duplicate the whole project** — copy the folder, change `config.json`,
   update the scheduled task names. Cheapest path.
2. **Parameterize** — extend `config.json` to `{ "buildings": [ {...}, {...} ] }`,
   wrap the main loop in `run.py` to iterate buildings, and namespace the
   state file per building (e.g. `state/seen.<slug>.json`). About an hour of
   work; do this once you have 3+ targets.

## Files

```
1070-park-monitor/
├── .env                    # API keys — NEVER commit
├── .env.example            # template
├── config.json             # building metadata, site list, search terms
├── requirements.txt        # python deps
├── src/
│   ├── run.py              # entrypoint
│   ├── scraper.py          # Firecrawl wrappers
│   ├── diff.py             # state diff
│   ├── notifier.py         # Gmail SMTP
│   └── schema.py           # Pydantic Listing model
├── state/
│   └── seen.json           # persistent state
└── logs/
    └── YYYY-MM-DD.log      # daily logs (appended by every run that day)
```
