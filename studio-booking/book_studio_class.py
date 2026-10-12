#!/usr/bin/env python3
"""
Peloton Studios class booker (studio.onepeloton.com, Peloton Studios New York).

How the site works (reverse-engineered Oct 2026)
------------------------------------------------
* studio.onepeloton.com is a Next.js shell. Login is Auth0 (auth.onepeloton.com, Peloton account).
* The schedule and booking UI is a TeamUp iframe served from schedule.studio.onepeloton.com
  (TeamUp provider/business id 7248695). The frame talks to a REST API at
  https://schedule.studio.onepeloton.com/api/v2/ with headers
      Teamup-Provider-ID: 7248695, Teamup-Request-Mode: customer, Authorization: Token <session token>
* New classes appear in GET /api/v2/events when Peloton publishes them (Thursdays 12:00 PM ET,
  for the Thu-Mon that starts 35 days later). Booking a class is:
      POST /api/v2/events/{id}/initiate_purchase  {customer, return_to, select_drop_in: true}  -> next_url (checkout)
      POST /api/v2/events/{id}/register           {customer, customer_membership}             (membership/credit)
      POST /api/v2/events/{id}/join_waitlist      {customer}
* Instructor ids: Becs Gentry = 127606 (GET /api/v2/instructors?sort=name).

Strategy
--------
1. At T-prestart open the site in Chromium, log in (or reuse the saved browser profile), and
   capture the TeamUp session token + customer id from the frame's own API traffic.
2. From T-0 poll GET /events for the target instructor in the release window with plain HTTP
   (no browser in the loop). The moment a matching, non-full class appears, fire the booking
   call. Then let the browser finish the checkout page (payment method on file).
3. Fall back to the waitlist when the class is full (--waitlist), and to clicking inside the
   TeamUp frame if the API path is rejected.

Credentials: P2G_PELOTON__EMAIL / P2G_PELOTON__PASSWORD (same names as the sync workflow secrets)
or STUDIO_EMAIL / STUDIO_PASSWORD. With --profile-dir you can instead log in by hand once
(--login-only --headful) and the session is reused on later runs.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import threading
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import requests

try:
    from playwright.sync_api import (
        BrowserContext,
        Error as PlaywrightError,
        Frame,
        Page,
        TimeoutError as PlaywrightTimeout,
        sync_playwright,
    )
except ImportError:  # pragma: no cover
    sys.stderr.write("playwright is not installed: pip install -r requirements.txt && playwright install chromium\n")
    raise

ET = ZoneInfo("America/New_York")
HERE = Path(__file__).resolve().parent

SITE_URL = "https://studio.onepeloton.com/new-york/schedule"
TEAMUP_HOST = "https://schedule.studio.onepeloton.com"
API = f"{TEAMUP_HOST}/api/v2"
PROVIDER_ID = "7248695"
PROVIDER_SLUG = "7248695-peloton-studios-new-york"
FRAME_BASE = f"{TEAMUP_HOST}/w{PROVIDER_ID}/p/{PROVIDER_SLUG}/c/schedule"
BECS_GENTRY_ID = 127606

EXIT_BOOKED = 0
EXIT_ERROR = 1
EXIT_NOT_BOOKED = 2
EXIT_NO_RELEASE_SOON = 3

EVENT_FIELDS = ",".join([
    "id", "name", "starts_at", "ends_at", "is_full", "occupancy", "max_occupancy", "attending_count",
    "waiting_count", "registrations_open_at", "active_registration_status", "customer_url",
    "instructors", "offering_type.category.name", "offering_type.dropin_price", "venue.name",
])


# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
@dataclass
class Config:
    email: str
    password: str
    instructor: str
    instructor_id: Optional[int]
    class_name: Optional[str]
    class_time: Optional[str]
    class_day: Optional[str]
    window_start: Optional[dt.date]
    window_end: Optional[dt.date]
    release_weekday: int
    release_time: dt.time
    release_offset_days: int
    prestart_minutes: float
    max_wait_minutes: float
    poll_seconds: float
    poll_interval: float
    waitlist: bool
    membership: bool
    dry_run: bool
    discover: bool
    now: bool
    login_only: bool
    headful: bool
    profile_dir: Optional[Path]
    storage_state: Path
    out_dir: Path
    notify_url: Optional[str] = None
    chromium_path: Optional[str] = None


def parse_args(argv: Optional[list[str]] = None) -> Config:
    env = os.environ.get
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--instructor", default=env("STUDIO_INSTRUCTOR", "Becs Gentry"))
    p.add_argument("--instructor-id", type=int, default=int(env("STUDIO_INSTRUCTOR_ID", "0")) or None,
                   help="TeamUp instructor id (looked up by name when omitted; Becs Gentry = 127606)")
    p.add_argument("--class-name", default=env("STUDIO_CLASS_NAME") or None,
                   help="Optional text that must appear in the class name, e.g. 'Run' or '30 min'")
    p.add_argument("--class-time", default=env("STUDIO_CLASS_TIME") or None, help="Optional start time, e.g. '7:30 AM'")
    p.add_argument("--class-day", default=env("STUDIO_CLASS_DAY") or None, help="Optional weekday, e.g. 'Sat'")
    p.add_argument("--window-start", default=env("STUDIO_WINDOW_START") or None,
                   help="First class date to consider (YYYY-MM-DD). Default: release day + --release-offset-days")
    p.add_argument("--window-end", default=env("STUDIO_WINDOW_END") or None,
                   help="Last class date to consider (YYYY-MM-DD). Default: window start + 4 days (Thu-Mon)")
    p.add_argument("--release-weekday", default=env("STUDIO_RELEASE_WEEKDAY", "thu"))
    p.add_argument("--release-time", default=env("STUDIO_RELEASE_TIME", "12:00"), help="HH:MM Eastern")
    p.add_argument("--release-offset-days", type=int, default=int(env("STUDIO_RELEASE_OFFSET_DAYS", "35")),
                   help="Days between the release and the first class it unlocks (Peloton: 35)")
    p.add_argument("--prestart-minutes", type=float, default=float(env("STUDIO_PRESTART_MINUTES", "5")))
    p.add_argument("--max-wait-minutes", type=float, default=float(env("STUDIO_MAX_WAIT_MINUTES", "60")),
                   help="Exit immediately if the next release is further away (lets a cron fire at both DST offsets)")
    p.add_argument("--poll-seconds", type=float, default=float(env("STUDIO_POLL_SECONDS", "300")),
                   help="How long to keep polling after the release time")
    p.add_argument("--poll-interval", type=float, default=float(env("STUDIO_POLL_INTERVAL", "0.4")))
    p.add_argument("--waitlist", action="store_true", default=env("STUDIO_WAITLIST", "false").lower() == "true")
    p.add_argument("--membership", action="store_true", default=env("STUDIO_MEMBERSHIP", "false").lower() == "true",
                   help="Book with a class credit/membership on the account instead of a $35 drop-in purchase")
    p.add_argument("--dry-run", action="store_true", help="Find the class and stop before the booking/purchase call")
    p.add_argument("--discover", action="store_true", help="Log every TeamUp API request (token redacted) and page state")
    p.add_argument("--now", action="store_true", help="Skip waiting for the release; poll immediately")
    p.add_argument("--login-only", action="store_true", help="Log in, save the session, exit (use with --headful)")
    p.add_argument("--headful", action="store_true")
    p.add_argument("--profile-dir", default=env("STUDIO_PROFILE_DIR") or None,
                   help="Persistent Chromium profile dir; survives logins done by hand (recommended)")
    p.add_argument("--storage-state", default=env("STUDIO_STORAGE_STATE", str(HERE / "state" / "auth.json")))
    p.add_argument("--out-dir", default=env("STUDIO_OUT_DIR", str(HERE / "out")))
    a = p.parse_args(argv)

    weekdays = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
    wd = a.release_weekday.strip().lower()[:3]
    if wd not in weekdays:
        p.error(f"--release-weekday must be one of {list(weekdays)}")
    hh, mm = a.release_time.split(":")

    def d(s: Optional[str]) -> Optional[dt.date]:
        return dt.date.fromisoformat(s) if s else None

    return Config(
        email=env("P2G_PELOTON__EMAIL") or env("STUDIO_EMAIL") or "",
        password=env("P2G_PELOTON__PASSWORD") or env("STUDIO_PASSWORD") or "",
        instructor=a.instructor, instructor_id=a.instructor_id, class_name=a.class_name,
        class_time=a.class_time, class_day=a.class_day, window_start=d(a.window_start), window_end=d(a.window_end),
        release_weekday=weekdays[wd], release_time=dt.time(int(hh), int(mm)), release_offset_days=a.release_offset_days,
        prestart_minutes=a.prestart_minutes, max_wait_minutes=a.max_wait_minutes,
        poll_seconds=a.poll_seconds, poll_interval=a.poll_interval, waitlist=a.waitlist, membership=a.membership,
        dry_run=a.dry_run, discover=a.discover, now=a.now, login_only=a.login_only, headful=a.headful,
        profile_dir=Path(a.profile_dir) if a.profile_dir else None,
        storage_state=Path(a.storage_state), out_dir=Path(a.out_dir),
        notify_url=env("NOTIFY_URL") or None, chromium_path=env("PLAYWRIGHT_CHROMIUM_PATH") or None,
    )


# ----------------------------------------------------------------------------
# Timing
# ----------------------------------------------------------------------------
def log(msg: str) -> None:
    print(f"[{dt.datetime.now(ET).strftime('%H:%M:%S.%f')[:-3]} ET] {msg}", flush=True)


def next_release(cfg: Config, now: Optional[dt.datetime] = None, grace: dt.timedelta = dt.timedelta(minutes=10)) -> dt.datetime:
    """Next release instant in ET. A release < `grace` ago still counts so a late cron joins the window."""
    now = now or dt.datetime.now(ET)
    days_ahead = (cfg.release_weekday - now.date().weekday()) % 7
    candidate = dt.datetime.combine(now.date() + dt.timedelta(days=days_ahead), cfg.release_time, tzinfo=ET)
    if candidate + grace <= now:
        candidate = dt.datetime.combine(candidate.date() + dt.timedelta(days=7), cfg.release_time, tzinfo=ET)
    return candidate


def release_window(cfg: Config, release: dt.datetime) -> tuple[dt.date, dt.date]:
    start = cfg.window_start or (release.date() + dt.timedelta(days=cfg.release_offset_days))
    end = cfg.window_end or (start + dt.timedelta(days=4))
    return start, end


def sleep_until(target: dt.datetime, offset: float = 0.0) -> None:
    while True:
        remaining = (target - dt.datetime.now(ET)).total_seconds() - offset
        if remaining <= 0:
            return
        if remaining > 60:
            log(f"sleeping, {remaining/60:.1f} min until {target:%a %H:%M:%S} ET")
            time.sleep(min(remaining - 30, 300))
        elif remaining > 2:
            time.sleep(remaining - 1.5)
        else:
            time.sleep(0.005)


def measure_clock_offset(session: requests.Session) -> float:
    """server_time - local_time in seconds (1 s precision) from the API's Date header."""
    try:
        t0 = time.time()
        r = session.head(f"{API}/", timeout=10)
        t1 = time.time()
        if "date" not in r.headers:
            return 0.0
        return parsedate_to_datetime(r.headers["date"]).timestamp() - (t0 + t1) / 2
    except Exception as e:  # noqa: BLE001
        log(f"clock sync skipped: {e}")
        return 0.0


# ----------------------------------------------------------------------------
# TeamUp API client
# ----------------------------------------------------------------------------
class TeamUp:
    def __init__(self, token: Optional[str] = None, cookies: Optional[dict] = None, discover_log=None):
        self.s = requests.Session()
        self.s.headers.update({
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Teamup-Provider-ID": PROVIDER_ID,
            "Teamup-Request-Mode": "customer",
            "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"),
            "Origin": TEAMUP_HOST,
            "Referer": f"{FRAME_BASE}",
        })
        if token:
            self.s.headers["Authorization"] = f"Token {token}"
        if cookies:
            for k, v in cookies.items():
                self.s.cookies.set(k, v, domain="schedule.studio.onepeloton.com")
        self.discover_log = discover_log

    def _log(self, method: str, url: str, status: int, body: Any, payload: Any = None) -> None:
        if self.discover_log is None:
            return
        row = {"ts": dt.datetime.now(ET).isoformat(timespec="milliseconds"), "method": method, "url": url, "status": status}
        if payload is not None:
            row["payload"] = payload
        if method != "GET":
            row["response"] = body if isinstance(body, (dict, list)) else str(body)[:500]
        self.discover_log.write(json.dumps(row, default=str) + "\n")
        self.discover_log.flush()

    def get(self, path: str, params: Optional[dict] = None, timeout: float = 8) -> Any:
        r = self.s.get(f"{API}/{path}", params=params, timeout=timeout)
        body = _json(r)
        self._log("GET", r.url, r.status_code, body)
        r.raise_for_status()
        return body

    def post(self, path: str, payload: dict, timeout: float = 10) -> tuple[int, Any]:
        r = self.s.post(f"{API}/{path}", json=payload, timeout=timeout)
        body = _json(r)
        self._log("POST", r.url, r.status_code, body, payload)
        return r.status_code, body

    # -- public lookups ------------------------------------------------------
    def instructor_id(self, name: str) -> Optional[int]:
        data = self.get("instructors", {"sort": "name", "page_size": 200})
        for i in data.get("results", data if isinstance(data, list) else []):
            if i.get("name", "").strip().lower() == name.strip().lower():
                return int(i["id"])
        return None

    def events(self, instructor_id: int, start: dt.date, end: dt.date) -> list[dict]:
        data = self.get("events", {
            "instructors": instructor_id,
            "local_starts_at_gte": f"{start:%Y-%m-%d}T00:00",
            "local_starts_at_lte": f"{end:%Y-%m-%d}T23:59",
            "status": "active", "sort": "start", "page_size": 100,
            "expand": "instructors,active_registration_status,offering_type,offering_type.category,venue",
            "fields": EVENT_FIELDS,
        })
        return data.get("results", [])

    def bootstrap(self) -> dict:
        return self.get("internal/frontends/customer/bootstrap_data", {"provider": PROVIDER_ID})

    # -- booking -------------------------------------------------------------
    def eligibility(self, customer: int, event_id: int) -> tuple[int, Any]:
        return self.post("registration_eligibility/check", {"customer": customer, "event": event_id, "target_type": "class"})

    def initiate_purchase(self, customer: int, event_id: int, return_to: str) -> tuple[int, Any]:
        return self.post(f"events/{event_id}/initiate_purchase", {"customer": customer, "return_to": return_to, "select_drop_in": True})

    def register(self, customer: int, event_id: int, customer_membership: Optional[int] = None) -> tuple[int, Any]:
        payload: dict[str, Any] = {"customer": customer}
        if customer_membership:
            payload["customer_membership"] = customer_membership
        return self.post(f"events/{event_id}/register", payload)

    def join_waitlist(self, customer: int, event_id: int) -> tuple[int, Any]:
        return self.post(f"events/{event_id}/join_waitlist", {"customer": customer})


def _json(r: requests.Response) -> Any:
    try:
        return r.json()
    except ValueError:
        return r.text


# ----------------------------------------------------------------------------
# Event matching
# ----------------------------------------------------------------------------
def _norm(s: str) -> str:
    return re.sub(r"[\s.]", "", s).lower()


def event_matches(cfg: Config, ev: dict) -> bool:
    starts = dt.datetime.fromisoformat(ev["starts_at"]).astimezone(ET)
    if cfg.class_name and cfg.class_name.lower() not in ev.get("name", "").lower():
        return False
    if cfg.class_day and not starts.strftime("%A").lower().startswith(cfg.class_day.lower()[:3]):
        return False
    if cfg.class_time:
        want = _norm(cfg.class_time)
        have = {_norm(starts.strftime("%I:%M %p")).lstrip("0"), _norm(starts.strftime("%H:%M")), _norm(starts.strftime("%-I:%M%p"))}
        if want.lstrip("0") not in have and want not in have:
            return False
    return True


def describe(ev: dict) -> str:
    starts = dt.datetime.fromisoformat(ev["starts_at"]).astimezone(ET)
    full = ev.get("is_full")
    return (f"{ev['id']} {starts:%a %m/%d %-I:%M %p} {ev.get('name','?')} "
            f"[{'FULL' if full else 'open'} {ev.get('occupancy', '?')}/{ev.get('max_occupancy', '?')}, waitlist {ev.get('waiting_count', 0)}]")


# ----------------------------------------------------------------------------
# Booker
# ----------------------------------------------------------------------------
class Booker:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        cfg.out_dir.mkdir(parents=True, exist_ok=True)
        cfg.storage_state.parent.mkdir(parents=True, exist_ok=True)
        self.discover_log = open(cfg.out_dir / "api_requests.jsonl", "a", encoding="utf-8") if cfg.discover else None
        self.shot_idx = 0
        self.token: Optional[str] = None
        self.customer: Optional[int] = None
        self.lock = threading.Lock()

    # -- helpers --------------------------------------------------------------
    def shot(self, page: Page, label: str) -> None:
        self.shot_idx += 1
        try:
            page.screenshot(path=str(self.cfg.out_dir / f"{self.shot_idx:02d}_{label}.png"))
        except PlaywrightError:
            pass

    def teamup_frame(self, page: Page, timeout: float = 30) -> Optional[Frame]:
        deadline = time.time() + timeout
        while time.time() < deadline:
            for f in page.frames:
                if "schedule.studio.onepeloton.com" in f.url and "/c/" in f.url:
                    return f
            time.sleep(0.25)
        return None

    def visible(self, root, selectors: list[str], timeout_ms: int = 1500):
        deadline = time.time() + timeout_ms / 1000
        while True:
            for s in selectors:
                try:
                    loc = root.locator(s).first
                    if loc.is_visible():
                        return loc
                except PlaywrightError:
                    pass
            if time.time() >= deadline:
                return None
            time.sleep(0.1)

    # -- session capture -------------------------------------------------------
    def attach_capture(self, page: Page) -> None:
        """Harvest the TeamUp Authorization token from the frame's own API calls."""
        def on_request(req):
            if "/api/v2/" not in req.url or "schedule.studio.onepeloton.com" not in req.url:
                return
            auth = req.headers.get("authorization", "")
            if auth.startswith("Token "):
                with self.lock:
                    self.token = auth.split(" ", 1)[1]
            if self.discover_log and req.method != "GET":
                self.discover_log.write(json.dumps({
                    "ts": dt.datetime.now(ET).isoformat(timespec="milliseconds"), "source": "browser",
                    "method": req.method, "url": req.url, "post_data": (req.post_data or "")[:2000],
                }) + "\n")
        page.on("request", on_request)
        page.on("requestfailed", lambda r: log(f"request failed: {r.failure} {r.url[:120]}")
                if "onepeloton.com" in r.url and r.resource_type in ("document", "xhr", "fetch", "script") else None)
        page.on("console", lambda m: log(f"console {m.type}: {m.text[:200]}") if m.type == "error" and "datadog" not in m.text.lower() else None)

    def describe_page(self, page: Page, label: str) -> None:
        """Log what the page looks like (URL with secrets masked, fields, buttons, text) for remote debugging."""
        try:
            url = re.sub(r"(code|state|nonce|id_token|access_token|token)=[^&#]+", r"\1=<masked>", page.url)
            info = page.evaluate("""() => ({
                title: document.title,
                inputs: [...document.querySelectorAll('input')].filter(i => i.type !== 'hidden')
                    .map(i => `${i.type}|name=${i.name}|id=${i.id}|ph=${i.placeholder}|ac=${i.autocomplete}`),
                buttons: [...document.querySelectorAll('button,[role=button],input[type=submit]')]
                    .map(b => (b.innerText || b.value || b.getAttribute('aria-label') || '').trim().slice(0, 40)).filter(Boolean),
                text: (document.body ? document.body.innerText : '').replace(/\\s+/g, ' ').slice(0, 400),
            })""")
            log(f"[{label}] url={url}")
            log(f"[{label}] title={info['title']!r} inputs={info['inputs']} buttons={info['buttons'][:15]}")
            log(f"[{label}] text={info['text']!r}")
        except PlaywrightError as e:
            log(f"[{label}] describe failed: {e}")

    def dismiss_cookie_banner(self, page: Page) -> None:
        for sel in ("#fides-accept-all-button", "button:has-text('Accept all')", "button:has-text('Dismiss')",
                    ".fides-close-button", "button[aria-label='Close']"):
            try:
                loc = page.locator(sel).first
                if loc.is_visible():
                    loc.click(timeout=1500)
                    log("dismissed cookie banner")
                    return
            except PlaywrightError:
                continue

    def shell_logged_in(self, page: Page) -> bool:
        """The Next.js header shows 'Account' when the Auth0 session exists, 'Log In' otherwise."""
        try:
            return page.get_by_role("button", name=re.compile(r"^account$", re.I)).first.is_visible()
        except PlaywrightError:
            return False

    def frame_logged_in(self, frame: Frame) -> bool:
        try:
            txt = frame.evaluate("() => document.body ? document.body.innerText : ''")
        except PlaywrightError:
            return False
        return "LOG IN / REGISTER" not in txt.upper()

    # -- login ----------------------------------------------------------------
    def login(self, page: Page, context: BrowserContext) -> None:
        cfg = self.cfg
        log(f"opening {SITE_URL}")
        page.goto(SITE_URL, wait_until="domcontentloaded", timeout=60_000)
        time.sleep(1.5)
        self.dismiss_cookie_banner(page)
        frame = self.teamup_frame(page)
        if frame is None:
            raise SystemExit("TeamUp schedule frame did not load")
        try:
            frame.wait_for_load_state("networkidle", timeout=20_000)
        except PlaywrightTimeout:
            pass
        self.shot(page, "landing")

        if self.frame_logged_in(frame):
            log("already logged in (saved session reused)")
        else:
            if not (cfg.email and cfg.password):
                if cfg.login_only and cfg.headful:
                    log("no credentials in env; log in by hand in the browser window (you have 5 minutes)")
                    for _ in range(600):
                        time.sleep(0.5)
                        f = self.teamup_frame(page, timeout=1)
                        if f and self.frame_logged_in(f):
                            break
                else:
                    raise SystemExit("not logged in and no credentials in P2G_PELOTON__EMAIL / P2G_PELOTON__PASSWORD")
            else:
                log("logging in through Peloton (Auth0)")
                page.get_by_role("button", name=re.compile(r"log ?in", re.I)).first.click(timeout=10_000)
                page.wait_for_url(re.compile(r"auth\.onepeloton\.com"), timeout=30_000)
                try:
                    page.wait_for_load_state("networkidle", timeout=15_000)
                except PlaywrightTimeout:
                    pass
                self.describe_page(page, "auth0 login page")
                email = self.visible(page, ["input[type='email']", "input[name='username']", "input[name='email']",
                                            "input[autocomplete='username']", "input[id*='email' i]"], 30_000)
                if email is None:
                    self.shot(page, "login_no_email_field")
                    raise SystemExit("could not find the email field on auth.onepeloton.com (bot challenge?). "
                                     "Run once with --headful --login-only --profile-dir and log in by hand.")
                email.fill(cfg.email)
                pw = self.visible(page, ["input[type='password']"], 3000)
                if pw is None:
                    submit = self.visible(page, ["button[type='submit']", "button:has-text('Continue')", "button:has-text('Next')"], 3000)
                    if submit:
                        submit.click()
                    pw = self.visible(page, ["input[type='password']"], 20_000)
                if pw is None:
                    self.shot(page, "login_no_password_field")
                    raise SystemExit("could not find the password field; adjust the login step")
                pw.fill(cfg.password)
                submit = self.visible(page, ["button[type='submit']", "button:has-text('Log in')", "button:has-text('Log In')",
                                             "button:has-text('Continue')"], 3000)
                if submit:
                    submit.click()
                else:
                    pw.press("Enter")
                time.sleep(2.0)
                self.describe_page(page, "after submitting credentials")
                page.wait_for_url(re.compile(r"studio\.onepeloton\.com"), timeout=60_000)
                try:
                    page.wait_for_load_state("networkidle", timeout=15_000)
                except PlaywrightTimeout:
                    pass
                self.describe_page(page, "after redirect")
                log(f"shell shows logged in: {self.shell_logged_in(page)}")
                # the callback lands on the home page; go back to the schedule so the frame gets the token
                log("returning to the schedule page")
                page.goto(SITE_URL, wait_until="domcontentloaded", timeout=60_000)
                time.sleep(1.5)
                self.dismiss_cookie_banner(page)
                frame = self.teamup_frame(page)
                for i in range(60):
                    if frame and self.frame_logged_in(frame):
                        break
                    if i == 20:  # the widget sometimes needs a reload to pick up the identity-provider token
                        page.reload(wait_until="domcontentloaded")
                        self.dismiss_cookie_banner(page)
                    time.sleep(0.5)
                    frame = self.teamup_frame(page, timeout=1)
            self.shot(page, "after_login")

        frame = self.teamup_frame(page)
        if frame is None or not self.frame_logged_in(frame):
            self.shot(page, "login_unconfirmed")
            self.describe_page(page, "login unconfirmed")
            if frame:
                try:
                    log("frame text: " + re.sub(r"\s+", " ", frame.evaluate("() => document.body.innerText"))[:300])
                except PlaywrightError:
                    pass
            raise SystemExit(f"login did not stick (shell logged in: {self.shell_logged_in(page)}, "
                             "frame still shows LOG IN / REGISTER); see screenshots")

        # The frame's API calls carry the customer session token; make it issue one.
        self.goto_frame(frame, self.release_dates_for_frame())
        for _ in range(40):
            if self.token:
                break
            time.sleep(0.25)
        if not self.token:
            # Fallback: ask bootstrap_data with the browser's cookies; it returns the session's access_token.
            try:
                r = context.request.get(f"{API}/internal/frontends/customer/bootstrap_data?provider={PROVIDER_ID}",
                                        headers={"Teamup-Provider-ID": PROVIDER_ID, "Teamup-Request-Mode": "customer",
                                                 "Accept": "application/json"})
                self.token = (r.json() or {}).get("access_token") if r.ok else None
            except PlaywrightError:
                pass
        if not self.token:
            raise SystemExit("could not capture the TeamUp session token from the frame's API calls")
        api = self.api()
        boot = api.bootstrap()
        cust = boot.get("current_customer")
        self.customer = int(cust["id"] if isinstance(cust, dict) else cust) if cust else None
        if not self.customer:
            raise SystemExit(f"TeamUp session has no customer (bootstrap_data: {json.dumps(boot)[:300]})")
        log(f"session ready: customer {self.customer}, token captured")
        try:
            context.storage_state(path=str(cfg.storage_state))
        except PlaywrightError:
            pass

    def release_dates_for_frame(self) -> tuple[dt.date, dt.date]:
        start, end = release_window(self.cfg, next_release(self.cfg) if not self.cfg.now else dt.datetime.now(ET))
        return start, end

    def goto_frame(self, frame: Frame, dates: tuple[dt.date, dt.date], event_id: Optional[int] = None) -> None:
        start, end = dates
        q = {"startdate": f"{start:%Y-%m-%d}", "enddate": f"{end:%Y-%m-%d}", "venues": "", "category": "",
             "instructors": self.cfg.instructor_id or "", "offering_types": "", "age_restrictions": "", "date": f"{start:%Y-%m-%d}"}
        if event_id:
            q["e"] = event_id
        url = f"{FRAME_BASE}?{urlencode(q)}"
        try:
            frame.evaluate("u => { location.href = u }", url)
            frame.wait_for_load_state("domcontentloaded", timeout=15_000)
        except PlaywrightError as e:
            log(f"frame navigation failed: {e}")

    def api(self) -> TeamUp:
        return TeamUp(self.token, discover_log=self.discover_log)

    # -- booking ----------------------------------------------------------------
    def book_event(self, api: TeamUp, ev: dict, page: Page) -> str:
        """Returns booked | waitlisted | full | rejected."""
        cfg = self.cfg
        eid = int(ev["id"])
        if ev.get("is_full"):
            if not cfg.waitlist:
                return "full"
            if cfg.dry_run:
                log(f"DRY RUN: would join the waitlist for {describe(ev)}")
                return "waitlisted"
            st, body = api.join_waitlist(self.customer, eid)
            log(f"join_waitlist -> {st} {json.dumps(body)[:200]}")
            return "waitlisted" if st < 300 else "rejected"

        if cfg.dry_run:
            log(f"DRY RUN: would book {describe(ev)}")
            st, body = api.eligibility(self.customer, eid)
            log(f"eligibility check -> {st} {json.dumps(body)[:400]}")
            return "booked"

        if cfg.membership:
            st, body = api.register(self.customer, eid, self._membership_id(api, eid))
            log(f"register -> {st} {json.dumps(body)[:300]}")
            if st < 300:
                return "booked"
            log("register rejected, falling back to drop-in purchase")

        return_to = f"{FRAME_BASE}?e={eid}"
        st, body = api.initiate_purchase(self.customer, eid, return_to)
        log(f"initiate_purchase -> {st} {json.dumps(body)[:300]}")
        if st >= 300:
            code = body.get("code") if isinstance(body, dict) else None
            if code in ("event_full", "registrations_full"):
                return "full"
            return "rejected"
        next_url = body.get("next_url") if isinstance(body, dict) else None
        if next_url:
            return "booked" if self.finish_checkout(page, next_url) else "rejected"
        return "booked"

    def _membership_id(self, api: TeamUp, eid: int) -> Optional[int]:
        st, body = api.eligibility(self.customer, eid)
        if st >= 300 or not isinstance(body, dict):
            return None
        for opt in body.get("registration_options", []) or []:
            cm = opt.get("customer_membership")
            if cm:
                return int(cm["id"] if isinstance(cm, dict) else cm)
        return None

    def finish_checkout(self, page: Page, next_url: str) -> bool:
        """Open TeamUp's checkout page and confirm payment with the card on file."""
        if next_url.startswith("/"):
            next_url = TEAMUP_HOST + next_url
        log(f"opening checkout {next_url}")
        try:
            page.goto(next_url, wait_until="domcontentloaded", timeout=30_000)
        except PlaywrightError as e:
            log(f"checkout navigation failed: {e}")
            return False
        self.shot(page, "checkout")
        deadline = time.time() + 90
        confirm_sel = ["button:has-text('Confirm and pay')", "button:has-text('Pay now')", "button:has-text('Pay')",
                       "button:has-text('Complete purchase')", "button:has-text('Complete')", "button:has-text('Confirm')",
                       "button:has-text('Book')", "button:has-text('Agree')", "button:has-text('Accept')", "button[type='submit']"]
        success_re = re.compile(r"booked successfully|you.?re booked|booking confirmed|thank you|confirmed|see you", re.I)
        while time.time() < deadline:
            try:
                txt = page.evaluate("() => document.body.innerText")
            except PlaywrightError:
                txt = ""
            if success_re.search(txt):
                self.shot(page, "checkout_success")
                return True
            if re.search(r"class is full|sold out|no longer available", txt, re.I):
                log("checkout says the class is no longer available")
                return False
            btn = self.visible(page, confirm_sel, 700)
            if btn is not None:
                try:
                    label = btn.inner_text(timeout=500).strip()
                    btn.click(timeout=3000)
                    log(f"clicked '{label}' on checkout")
                    self.shot(page, "checkout_click")
                    time.sleep(1.0)
                except PlaywrightError:
                    pass
            time.sleep(0.3)
        log("checkout did not confirm within 90s; check screenshots and your email")
        return False

    def click_fallback(self, page: Page, ev: dict) -> str:
        """Open the booking modal inside the TeamUp frame and click Book / Join waitlist."""
        frame = self.teamup_frame(page)
        if frame is None:
            return "rejected"
        start, end = self.release_dates_for_frame()
        self.goto_frame(frame, (start, end), event_id=int(ev["id"]))
        time.sleep(1.0)
        self.shot(page, "modal")
        btn = self.visible(frame, ["button:has-text('Book')", "button:has-text('Register')", "button:has-text('Reserve')"], 4000)
        kind = "booked"
        if btn is None and self.cfg.waitlist:
            btn = self.visible(frame, ["button:has-text('Join waitlist')", "button:has-text('Join Waitlist')", "button:has-text('Waitlist')"], 2000)
            kind = "waitlisted"
        if btn is None:
            return "rejected"
        if self.cfg.dry_run:
            log(f"DRY RUN: would click '{btn.inner_text()}' in the booking modal")
            return kind
        btn.click()
        self.shot(page, "modal_clicked")
        # drop-in flow leaves the frame for a checkout page; follow it on the main page
        time.sleep(2.0)
        for f in page.frames:
            if "checkout" in f.url or "/purchase" in f.url:
                return "booked" if self.finish_checkout(page, f.url) else "rejected"
        try:
            txt = frame.evaluate("() => document.body.innerText")
        except PlaywrightError:
            txt = ""
        if re.search(r"booked successfully|joined waitlist|you.?re on the waitlist", txt, re.I):
            return kind
        return "rejected"

    # -- polling -----------------------------------------------------------------
    def poll(self, api: TeamUp, page: Page, release: dt.datetime) -> str:
        cfg = self.cfg
        start, end = release_window(cfg, release)
        end_time = max(release, dt.datetime.now(ET)) + dt.timedelta(seconds=cfg.poll_seconds)
        log(f"polling {cfg.instructor} (id {cfg.instructor_id}) for classes {start} .. {end}")
        attempt = 0
        tried: set[int] = set()
        last_msg = None
        while dt.datetime.now(ET) < end_time:
            attempt += 1
            try:
                events = api.events(cfg.instructor_id, start, end)
            except requests.RequestException as e:
                msg = f"events request failed: {e}"
                if msg != last_msg:
                    log(msg)
                    last_msg = msg
                time.sleep(cfg.poll_interval)
                continue
            candidates = [e for e in events if event_matches(cfg, e) and int(e["id"]) not in tried]
            if not candidates:
                msg = f"no matching class yet ({len(events)} event(s) for instructor in window)"
                if msg != last_msg:
                    log(f"attempt {attempt}: {msg}")
                    last_msg = msg
                time.sleep(cfg.poll_interval)
                continue
            # prefer open classes, earliest first
            candidates.sort(key=lambda e: (bool(e.get("is_full")), e["starts_at"]))
            for ev in candidates:
                log(f"attempt {attempt}: trying {describe(ev)}")
                result = self.book_event(api, ev, page)
                if result == "rejected":
                    log("API path rejected; trying the click fallback")
                    result = self.click_fallback(page, ev)
                if result in ("booked", "waitlisted"):
                    log(f"SUCCESS ({result}): {describe(ev)}")
                    return result
                if result == "full":
                    log("class is full" + ("" if cfg.waitlist else " (waitlist disabled, keep polling for a released spot)"))
                tried.add(int(ev["id"])) if result == "rejected" else None
            time.sleep(cfg.poll_interval)
        log(f"gave up after {attempt} polls / {cfg.poll_seconds:.0f}s")
        return "not_booked"

    # -- orchestration --------------------------------------------------------------
    def run(self) -> int:
        cfg = self.cfg
        release = next_release(cfg)
        if cfg.now or cfg.login_only:
            release = dt.datetime.now(ET)
            log("starting immediately")
        else:
            until = (release - dt.datetime.now(ET)).total_seconds() / 60
            if until > cfg.max_wait_minutes:
                log(f"next release {release:%a %Y-%m-%d %H:%M} ET is {until:.0f} min away (> {cfg.max_wait_minutes:.0f}); exiting")
                return EXIT_NO_RELEASE_SOON
            log(f"targeting release {release:%a %Y-%m-%d %H:%M} ET (classes {release_window(cfg, release)[0]} .. {release_window(cfg, release)[1]}); "
                f"pre-start at T-{cfg.prestart_minutes:g} min")
            sleep_until(release - dt.timedelta(minutes=cfg.prestart_minutes))

        public = TeamUp(discover_log=self.discover_log)
        if not cfg.instructor_id:
            cfg.instructor_id = public.instructor_id(cfg.instructor)
            if not cfg.instructor_id:
                log(f"instructor {cfg.instructor!r} not found in TeamUp instructor list")
                return EXIT_ERROR
            log(f"instructor {cfg.instructor!r} -> id {cfg.instructor_id}")
        offset = measure_clock_offset(public.s)
        log(f"clock offset vs server: {offset:+.1f}s")

        status = "error"
        with sync_playwright() as pw:
            ctx_kwargs: dict[str, Any] = {
                "viewport": {"width": 1366, "height": 1000}, "timezone_id": "America/New_York", "locale": "en-US",
                "user_agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"),
            }
            launch_kwargs: dict[str, Any] = {"headless": not cfg.headful}
            if cfg.chromium_path:
                launch_kwargs["executable_path"] = cfg.chromium_path
            if cfg.profile_dir:
                cfg.profile_dir.mkdir(parents=True, exist_ok=True)
                context = pw.chromium.launch_persistent_context(str(cfg.profile_dir), **launch_kwargs, **ctx_kwargs)
                browser = None
            else:
                browser = pw.chromium.launch(**launch_kwargs)
                if cfg.storage_state.exists():
                    ctx_kwargs["storage_state"] = str(cfg.storage_state)
                context = browser.new_context(**ctx_kwargs)
            context.set_default_timeout(10_000)
            page = context.pages[0] if context.pages else context.new_page()
            self.attach_capture(page)
            try:
                self.login(page, context)
                if cfg.login_only:
                    log("login saved; done")
                    status = "login_only"
                else:
                    api = self.api()
                    if not cfg.now:
                        sleep_until(release - dt.timedelta(seconds=15), offset)
                        # keep the browser session warm and re-arm the token right before T-0
                        frame = self.teamup_frame(page, timeout=2)
                        if frame:
                            self.goto_frame(frame, release_window(cfg, release))
                        time.sleep(0.5)
                        api = self.api()
                        sleep_until(release, offset)
                        log("T-0")
                    status = self.poll(api, page, release)
            except SystemExit as e:
                log(f"FATAL: {e}")
                self.shot(page, "fatal")
            except Exception as e:  # noqa: BLE001
                log(f"ERROR: {type(e).__name__}: {e}")
                self.shot(page, "error")
            finally:
                try:
                    context.storage_state(path=str(cfg.storage_state))
                except PlaywrightError:
                    pass
                if self.discover_log:
                    self.discover_log.close()
                    log(f"discovery written to {cfg.out_dir}")
                context.close()
                if browser:
                    browser.close()

        if status == "login_only":
            return EXIT_BOOKED
        self.notify(f"Peloton Studios booker: {status} for {cfg.instructor} (release {release:%a %m/%d %H:%M} ET)")
        if status in ("booked", "waitlisted"):
            return EXIT_BOOKED
        return EXIT_ERROR if status == "error" else EXIT_NOT_BOOKED

    def notify(self, text: str) -> None:
        log(text)
        if not self.cfg.notify_url:
            return
        try:
            requests.post(self.cfg.notify_url, data=text.encode(), headers={"Content-Type": "text/plain"}, timeout=10)
        except Exception as e:  # noqa: BLE001
            log(f"notify failed: {e}")


def main(argv: Optional[list[str]] = None) -> int:
    return Booker(parse_args(argv)).run()


if __name__ == "__main__":
    sys.exit(main())
