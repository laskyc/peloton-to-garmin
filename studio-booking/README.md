# Peloton Studios class booker

Books an in-person class at [Peloton Studios New York](https://studio.onepeloton.com/new-york/schedule)
the moment the weekly release opens: **Thursdays at 12:00 PM ET**, for the Thursday-to-Monday
that starts 35 days later. Default target: any Becs Gentry class in that window.

## How the booking site really works (so you know what the script is doing)

* `studio.onepeloton.com` is only a shell. Login is Peloton's Auth0 (`auth.onepeloton.com`),
  the schedule and the Book button live in an embedded **TeamUp** frame served from
  `schedule.studio.onepeloton.com` (TeamUp business id `7248695`).
* That frame talks to a JSON API at `https://schedule.studio.onepeloton.com/api/v2/`.
  Listing classes needs no login, only the header `Teamup-Provider-ID: 7248695`.
  Booking needs the session token the frame gets after you log in.
* Classes for a release are simply *absent* from the API until noon Thursday, then appear
  already open. So the fastest possible booking is: poll `GET /events` for the instructor in the
  release window a few times per second and fire the booking call the instant the class exists.
  That is what this script does. The browser is only used for login and for the checkout page.
* Deeplinks you can use by hand:
  * Schedule, filtered to Becs, for a given week (open inside the booking page's frame):
    `https://schedule.studio.onepeloton.com/w7248695/p/7248695-peloton-studios-new-york/c/schedule?startdate=2026-11-19&enddate=2026-11-23&instructors=127606&date=2026-11-19`
  * Add `&e=<event id>` to open a class's booking modal directly.
  * Becs's live-class times (same classes you book in studio):
    `https://instructor-schedule.onepeloton.com/s/becs-gentry-schedule`
* Payment: pay-as-you-go. You are charged **at booking** ($35 + tax). Cancelling at least 24 hours
  before the class refunds it; later than that forfeits it. `--membership` books against a class
  credit/membership on the account instead.

## Setup (once)

```bash
cd studio-booking
pip install -r requirements.txt
python -m playwright install chromium
cp .env.example .env            # fill in P2G_PELOTON__EMAIL / P2G_PELOTON__PASSWORD
```

Then log in once by hand so the browser profile holds a real session (Peloton's login page runs
bot detection; a human login avoids it and the TeamUp session persists):

```bash
./run.sh --headful --login-only --profile-dir ./profile
```

Check it with a dry run that polls right now (it will find this week's already-published Becs
classes and stop before paying):

```bash
./run.sh --now --dry-run --profile-dir ./profile --window-start 2026-10-15 --window-end 2026-10-21
```

## Running it on Thursday

The script sleeps until T-5 min, logs in, captures the session, syncs its clock to the server,
and at 12:00:00 starts polling (default 0.4 s interval for 5 minutes).

```bash
./run.sh --profile-dir ./profile                       # waits for the next Thursday 12:00 ET
./run.sh --profile-dir ./profile --class-time "7:30 AM" --class-day Sat --waitlist
```

Schedule it on a machine that is awake at noon ET (laptop, home server, cheap VPS):

```cron
CRON_TZ=America/New_York
35 11 * * 4  /path/to/peloton-to-garmin/studio-booking/run.sh --profile-dir /path/to/peloton-to-garmin/studio-booking/profile >> /path/to/peloton-to-garmin/studio-booking/logs/cron.log 2>&1
```

macOS launchd equivalent: a `StartCalendarInterval` with `Weekday 4, Hour 11, Minute 35`.

### GitHub Actions (backup only)

`.github/workflows/book_studio_class.yml` runs the same script with the repo's existing
`P2G_PELOTON__EMAIL` / `P2G_PELOTON__PASSWORD` secrets. Two caveats make it a backup rather than
the primary: GitHub's cron often starts late (minutes, sometimes more) and a datacenter browser may
hit Peloton's login bot challenge. Use *Run workflow* with mode `now-dry-run` to test it.
Optional secret `STUDIO_NOTIFY_URL` (e.g. an ntfy.sh topic) gets a one-line result.

## Options

| Flag / env | Meaning |
|---|---|
| `--instructor`, `STUDIO_INSTRUCTOR` | Name as shown on the schedule (default Becs Gentry) |
| `--instructor-id`, `STUDIO_INSTRUCTOR_ID` | TeamUp id; skips the lookup (Becs = 127606) |
| `--class-name` / `--class-time` / `--class-day` | Narrow to one class; all given filters must match |
| `--window-start` / `--window-end` | Class dates to consider; default release day +35 .. +39 days |
| `--waitlist` | Join the waitlist when the class is full |
| `--membership` | Book with a credit/membership instead of a $35 drop-in purchase |
| `--prestart-minutes` | Login lead time (default 5) |
| `--poll-seconds` / `--poll-interval` | Polling window after noon (default 300 s at 0.4 s) |
| `--dry-run` | Stop before the purchase/waitlist call (still runs the eligibility check) |
| `--now` | Don't wait for Thursday; poll immediately |
| `--login-only --headful` | Open a browser, log in (by hand if no creds), save session, exit |
| `--profile-dir` | Persistent Chromium profile; recommended |
| `--discover` | Write every API request/response (token redacted) to `out/api_requests.jsonl` |
| `NOTIFY_URL` | POST the one-line result here |

Exit codes: 0 booked/waitlisted, 1 error, 2 polled but no spot, 3 no release within the wait window.

## Limits to know about

* **Two per day, four per Thursday-Monday period**, and waitlist spots count toward the four.
* The script books the **first open matching class** (earliest start). Use the filters to pin one.
* If the drop-in purchase returns a checkout page, the browser clicks through it using the card on
  file; a first-time account must have payment details and the waiver saved beforehand (book
  any cheap class by hand once).
* `out/` keeps screenshots of every step for post-mortems.
