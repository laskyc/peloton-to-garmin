#!/usr/bin/env python3
"""
Peloton Studios class booker.

Books an in-person class at studio.onepeloton.com (Peloton Studios NY / London)
the moment the weekly release opens (Thursdays 12:00 PM ET).

Strategy
--------
1. Wake up a few minutes before the release (``--prestart-minutes``), log in with
   the Peloton account, open the schedule and locate the target class card so
   that everything is warm.
2. Sync the local clock against the site's HTTP ``Date`` header.
3. At T-0 start polling: reload, find the card for the instructor, click Book,
   complete checkout (payment method on file), and verify the confirmation.
4. Optionally fall back to the waitlist when the class is full.

``--discover`` records every network request and every link/button on the page
so you can find deeplinks and the underlying booking API (Peloton Studios runs
on TeamUp). Feed what you find back via ``--schedule-url`` / ``--selectors``.

Credentials come from the same env vars the peloton-to-garmin workflow uses:
``P2G_PELOTON__EMAIL`` / ``P2G_PELOTON__PASSWORD`` (or ``STUDIO_EMAIL`` /
``STUDIO_PASSWORD``).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

try:
    from playwright.sync_api import (
        Browser,
        BrowserContext,
        Error as PlaywrightError,
        Locator,
        Page,
        TimeoutError as PlaywrightTimeout,
        sync_playwright,
    )
except ImportError:  # pragma: no cover
    sys.stderr.write("playwright is not installed: pip install -r requirements.txt && playwright install chromium\n")
    raise

ET = ZoneInfo("America/New_York")
HERE = Path(__file__).resolve().parent

EXIT_BOOKED = 0
EXIT_ERROR = 1
EXIT_NOT_BOOKED = 2
EXIT_NO_RELEASE_SOON = 3

# Each entry is a list of candidate selectors tried in order. Override any key
# with --selectors selectors.json once you have inspected the real page.
DEFAULT_SELECTORS: dict[str, list[str]] = {
    "cookie_accept": ["button:has-text('Accept')", "button:has-text('Agree')", "button:has-text('Got it')"],
    "login_trigger": [
        "a:has-text('Log in')", "button:has-text('Log in')", "a:has-text('Log In')", "button:has-text('Log In')",
        "a:has-text('Sign in')", "button:has-text('Sign in')", "a:has-text('Sign In')", "button:has-text('Sign In')",
        "a[href*='login']", "a[href*='signin']",
    ],
    "email": ["input[type='email']", "input[name='email']", "input[name='username']", "input[name='usernameOrEmail']", "#username", "#email"],
    "password": ["input[type='password']", "input[name='password']", "#password"],
    "login_submit": ["button[type='submit']", "button:has-text('Log in')", "button:has-text('Log In')", "button:has-text('Sign in')", "button:has-text('Continue')"],
    "logged_in_marker": [
        "text=/^\\s*(log ?out|sign ?out)\\s*$/i", "a[href*='logout']", "a[href*='signout']",
        "text=/my (bookings|classes|account|schedule)/i", "[aria-label*='account' i]", "[data-testid*='account' i]",
    ],
    "schedule_link": ["a:has-text('Schedule')", "a:has-text('Book a class')", "a:has-text('Book a Class')", "a:has-text('Classes')", "a[href*='schedule']", "a[href*='classes']"],
    "day_view": ["button:has-text('Day')", "a:has-text('Day')", "[role='tab']:has-text('Day')"],
    "book_button": [
        "button:has-text('Book')", "a:has-text('Book')", "button:has-text('Reserve')", "a:has-text('Reserve')",
        "button:has-text('Register')", "a:has-text('Register')", "button:has-text('Sign up')", "button:has-text('Sign Up')",
    ],
    "waitlist_button": ["button:has-text('Waitlist')", "a:has-text('Waitlist')", "button:has-text('Join waitlist')", "button:has-text('Join Waitlist')"],
    "confirm_button": [
        "button:has-text('Confirm')", "button:has-text('Complete')", "button:has-text('Pay')", "button:has-text('Book now')",
        "button:has-text('Book Now')", "button:has-text('Place order')", "button:has-text('Checkout')", "button:has-text('Check out')",
        "button:has-text('Agree')", "button:has-text('Accept')", "button:has-text('Continue')",
    ],
    "success_marker": ["text=/confirmed|you.?re (all )?(booked|set)|booking (is )?complete|see you (in|at)|thank you for (your )?(booking|purchase)|added to (the )?wait ?list|you.?re on the wait ?list/i"],
    "full_marker": ["text=/sold out|class (is )?full|fully booked|no (spots|spaces) (left|available)|wait ?list only/i"],
    "error_marker": ["text=/something went wrong|try again|limit reached|maximum|too many requests|rate limit/i"],
}

BOOK_WORDS = re.compile(r"\b(book|reserve|register|sign\s?up)\b", re.I)
WAITLIST_WORDS = re.compile(r"wait\s?list", re.I)


# ----------------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------------
@dataclass
class Config:
    email: str
    password: str
    instructor: str
    discipline: Optional[str]
    class_time: Optional[str]
    class_day: Optional[str]
    site_url: str
    schedule_url: Optional[str]
    release_weekday: int  # Monday=0 .. Sunday=6
    release_time: dt.time
    prestart_minutes: float
    max_wait_minutes: float
    poll_seconds: float
    poll_interval: float
    waitlist: bool
    dry_run: bool
    discover: bool
    now: bool
    headful: bool
    slow_mo: int
    storage_state: Path
    out_dir: Path
    selectors: dict[str, list[str]] = field(default_factory=lambda: dict(DEFAULT_SELECTORS))
    notify_url: Optional[str] = None
    chromium_path: Optional[str] = None


def parse_args(argv: Optional[list[str]] = None) -> Config:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--instructor", default=os.environ.get("STUDIO_INSTRUCTOR", "Becs Gentry"))
    p.add_argument("--discipline", default=os.environ.get("STUDIO_DISCIPLINE") or None,
                   help="Optional text that must appear on the card, e.g. 'Tread' or 'Run'")
    p.add_argument("--class-time", default=os.environ.get("STUDIO_CLASS_TIME") or None,
                   help="Optional time text that must appear on the card, e.g. '7:00 AM'")
    p.add_argument("--class-day", default=os.environ.get("STUDIO_CLASS_DAY") or None,
                   help="Optional day text that must appear on/near the card, e.g. 'Sat' or 'Saturday'")
    p.add_argument("--site-url", default=os.environ.get("STUDIO_SITE_URL", "https://studio.onepeloton.com/"))
    p.add_argument("--schedule-url", default=os.environ.get("STUDIO_SCHEDULE_URL") or None,
                   help="Deeplink to the schedule page/week to open after login (found via --discover)")
    p.add_argument("--release-weekday", default=os.environ.get("STUDIO_RELEASE_WEEKDAY", "thu"))
    p.add_argument("--release-time", default=os.environ.get("STUDIO_RELEASE_TIME", "12:00"), help="HH:MM, Eastern time")
    p.add_argument("--prestart-minutes", type=float, default=float(os.environ.get("STUDIO_PRESTART_MINUTES", "5")))
    p.add_argument("--max-wait-minutes", type=float, default=float(os.environ.get("STUDIO_MAX_WAIT_MINUTES", "60")),
                   help="Exit immediately if the next release is further away than this (lets a cron fire at both DST offsets)")
    p.add_argument("--poll-seconds", type=float, default=float(os.environ.get("STUDIO_POLL_SECONDS", "240")),
                   help="How long to keep trying after the release time")
    p.add_argument("--poll-interval", type=float, default=float(os.environ.get("STUDIO_POLL_INTERVAL", "0.75")))
    p.add_argument("--waitlist", action="store_true", default=os.environ.get("STUDIO_WAITLIST", "false").lower() == "true",
                   help="Join the waitlist if the class is already full")
    p.add_argument("--dry-run", action="store_true", help="Do everything except the final confirm/pay click")
    p.add_argument("--discover", action="store_true", help="Record network requests, links and buttons to the out dir")
    p.add_argument("--now", action="store_true", help="Don't wait for the release time, run immediately")
    p.add_argument("--headful", action="store_true", help="Show the browser window")
    p.add_argument("--slow-mo", type=int, default=0, help="Playwright slow_mo in ms (debugging)")
    p.add_argument("--storage-state", default=os.environ.get("STUDIO_STORAGE_STATE", str(HERE / "state" / "auth.json")))
    p.add_argument("--out-dir", default=os.environ.get("STUDIO_OUT_DIR", str(HERE / "out")))
    p.add_argument("--selectors", default=os.environ.get("STUDIO_SELECTORS") or None, help="JSON file overriding DEFAULT_SELECTORS keys")
    a = p.parse_args(argv)

    email = os.environ.get("P2G_PELOTON__EMAIL") or os.environ.get("STUDIO_EMAIL") or ""
    password = os.environ.get("P2G_PELOTON__PASSWORD") or os.environ.get("STUDIO_PASSWORD") or ""

    selectors = dict(DEFAULT_SELECTORS)
    if a.selectors:
        with open(a.selectors, encoding="utf-8") as fh:
            overrides = json.load(fh)
        for k, v in overrides.items():
            selectors[k] = [v] if isinstance(v, str) else list(v)

    hh, mm = a.release_time.split(":")
    weekdays = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
    wd = a.release_weekday.strip().lower()[:3]
    if wd not in weekdays:
        p.error(f"--release-weekday must be one of {list(weekdays)}")

    return Config(
        email=email, password=password, instructor=a.instructor, discipline=a.discipline,
        class_time=a.class_time, class_day=a.class_day, site_url=a.site_url, schedule_url=a.schedule_url,
        release_weekday=weekdays[wd], release_time=dt.time(int(hh), int(mm)),
        prestart_minutes=a.prestart_minutes, max_wait_minutes=a.max_wait_minutes,
        poll_seconds=a.poll_seconds, poll_interval=a.poll_interval, waitlist=a.waitlist,
        dry_run=a.dry_run, discover=a.discover, now=a.now, headful=a.headful, slow_mo=a.slow_mo,
        storage_state=Path(a.storage_state), out_dir=Path(a.out_dir), selectors=selectors,
        notify_url=os.environ.get("NOTIFY_URL") or None,
        chromium_path=os.environ.get("PLAYWRIGHT_CHROMIUM_PATH") or None,
    )


# ----------------------------------------------------------------------------
# Timing
# ----------------------------------------------------------------------------
def log(msg: str) -> None:
    now = dt.datetime.now(ET).strftime("%H:%M:%S.%f")[:-3]
    print(f"[{now} ET] {msg}", flush=True)


def next_release(cfg: Config, now: Optional[dt.datetime] = None, grace: dt.timedelta = dt.timedelta(minutes=10)) -> dt.datetime:
    """Next release instant in ET. A release that happened < `grace` ago still counts as 'next'
    so a late-firing scheduler still joins the polling window."""
    now = now or dt.datetime.now(ET)
    today = now.date()
    days_ahead = (cfg.release_weekday - today.weekday()) % 7
    candidate = dt.datetime.combine(today + dt.timedelta(days=days_ahead), cfg.release_time, tzinfo=ET)
    if candidate + grace <= now:
        candidate = dt.datetime.combine(candidate.date() + dt.timedelta(days=7), cfg.release_time, tzinfo=ET)
    return candidate


def sleep_until(target: dt.datetime, offset: float = 0.0) -> None:
    """Sleep until `target` (ET-aware), compensating for `offset` seconds (server - local)."""
    while True:
        remaining = (target - dt.datetime.now(ET)).total_seconds() - offset
        if remaining <= 0:
            return
        if remaining > 60:
            log(f"sleeping, {remaining/60:.1f} min until {target.strftime('%a %H:%M:%S')} ET")
            time.sleep(min(remaining - 30, 300))
        elif remaining > 2:
            time.sleep(remaining - 1.5)
        else:
            time.sleep(0.01)


def measure_clock_offset(context: BrowserContext, url: str) -> float:
    """Return server_time - local_time in seconds using the HTTP Date header (1s precision)."""
    try:
        t0 = time.time()
        resp = context.request.head(url, timeout=10_000)
        t1 = time.time()
        date_hdr = resp.headers.get("date")
        if not date_hdr:
            return 0.0
        server = parsedate_to_datetime(date_hdr).timestamp()
        local_mid = (t0 + t1) / 2
        return server - local_mid
    except Exception as e:  # noqa: BLE001
        log(f"clock sync skipped: {e}")
        return 0.0


# ----------------------------------------------------------------------------
# Browser helpers
# ----------------------------------------------------------------------------
class Booker:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.sel = cfg.selectors
        self.cfg.out_dir.mkdir(parents=True, exist_ok=True)
        self.cfg.storage_state.parent.mkdir(parents=True, exist_ok=True)
        self.requests_log = None
        self.shot_idx = 0
        self.offset = 0.0

    # -- utilities ---------------------------------------------------------
    def shot(self, page: Page, label: str) -> None:
        self.shot_idx += 1
        path = self.cfg.out_dir / f"{self.shot_idx:02d}_{label}.png"
        try:
            page.screenshot(path=str(path), full_page=False)
        except PlaywrightError:
            pass

    def first_visible(self, page: Page, key: str, timeout: float = 1500, scope: Optional[Locator] = None) -> Optional[Locator]:
        """First selector in `key` that resolves to a visible element.

        `timeout` (ms) is a total budget shared by all candidate selectors, not a
        per-selector wait, so a long candidate list stays cheap inside the poll loop."""
        root = scope if scope is not None else page
        deadline = time.time() + timeout / 1000.0
        while True:
            for s in self.sel.get(key, []):
                try:
                    loc = root.locator(s).first
                    if loc.is_visible():
                        return loc
                except PlaywrightError:
                    continue
            if time.time() >= deadline:
                return None
            time.sleep(0.1)

    def click_if_present(self, page: Page, key: str, timeout: float = 800) -> bool:
        loc = self.first_visible(page, key, timeout)
        if loc is None:
            return False
        try:
            loc.click(timeout=3000)
            return True
        except PlaywrightError:
            return False

    def is_logged_in(self, page: Page) -> bool:
        return self.first_visible(page, "logged_in_marker", timeout=2500) is not None

    # -- discovery ---------------------------------------------------------
    def attach_discovery(self, page: Page) -> None:
        if not self.cfg.discover:
            return
        self.requests_log = open(self.cfg.out_dir / "requests.jsonl", "a", encoding="utf-8")

        def on_response(resp):
            req = resp.request
            if req.resource_type in ("image", "font", "media", "stylesheet"):
                return
            row = {
                "ts": dt.datetime.now(ET).isoformat(timespec="milliseconds"),
                "method": req.method,
                "url": req.url,
                "status": resp.status,
                "type": req.resource_type,
            }
            if req.resource_type in ("xhr", "fetch") and req.method in ("POST", "PUT", "PATCH"):
                body = req.post_data or ""
                # never persist the password
                if self.cfg.password and self.cfg.password in body:
                    body = body.replace(self.cfg.password, "***")
                row["post_data"] = body[:2000]
            self.requests_log.write(json.dumps(row) + "\n")
            self.requests_log.flush()

        page.on("response", on_response)

    def dump_dom(self, page: Page, label: str) -> None:
        if not self.cfg.discover:
            return
        try:
            data = page.evaluate(
                """() => ({
                    url: location.href,
                    title: document.title,
                    links: [...document.querySelectorAll('a[href]')].map(a => ({text: a.innerText.trim().slice(0,120), href: a.href})),
                    buttons: [...document.querySelectorAll('button, [role=button], input[type=submit]')]
                        .map(b => ({text: (b.innerText || b.value || b.getAttribute('aria-label') || '').trim().slice(0,120),
                                    id: b.id || null, cls: (b.className || '').toString().slice(0,200),
                                    testid: b.getAttribute('data-testid')})),
                    scripts: [...document.scripts].map(s => s.src).filter(Boolean),
                    forms: [...document.forms].map(f => ({action: f.action, method: f.method,
                        fields: [...f.elements].map(e => ({name: e.name, type: e.type, id: e.id}))})),
                })"""
            )
            (self.cfg.out_dir / f"dom_{label}.json").write_text(json.dumps(data, indent=2), encoding="utf-8")
        except PlaywrightError as e:
            log(f"dom dump failed: {e}")

    # -- login -------------------------------------------------------------
    def ensure_logged_in(self, page: Page, context: BrowserContext) -> None:
        log(f"opening {self.cfg.site_url}")
        page.goto(self.cfg.site_url, wait_until="domcontentloaded", timeout=45_000)
        self.click_if_present(page, "cookie_accept")
        self.dump_dom(page, "landing")
        self.shot(page, "landing")

        if self.is_logged_in(page):
            log("already logged in (storage state reused)")
            return

        if not (self.cfg.email and self.cfg.password):
            raise SystemExit("Not logged in and no credentials in P2G_PELOTON__EMAIL / P2G_PELOTON__PASSWORD")

        log("logging in")
        if not self.click_if_present(page, "login_trigger", timeout=4000):
            log("no login button found; assuming login form is on the page")
        page.wait_for_load_state("domcontentloaded")
        self.dump_dom(page, "login")
        self.shot(page, "login")

        email_box = self.first_visible(page, "email", timeout=15_000)
        if email_box is None:
            raise SystemExit("could not find the email field; run with --discover --headful and adjust --selectors")
        email_box.fill(self.cfg.email)

        pw_box = self.first_visible(page, "password", timeout=3000)
        if pw_box is None:
            # two-step login: submit email first
            self.click_if_present(page, "login_submit", timeout=3000)
            pw_box = self.first_visible(page, "password", timeout=15_000)
        if pw_box is None:
            raise SystemExit("could not find the password field; adjust --selectors")
        pw_box.fill(self.cfg.password)
        if not self.click_if_present(page, "login_submit", timeout=3000):
            pw_box.press("Enter")

        try:
            page.wait_for_load_state("networkidle", timeout=20_000)
        except PlaywrightTimeout:
            pass
        # Login may bounce through members.onepeloton.com / auth0 and back.
        for _ in range(20):
            if self.is_logged_in(page):
                break
            time.sleep(0.5)
        self.shot(page, "after_login")
        self.dump_dom(page, "after_login")
        if not self.is_logged_in(page):
            if page.url.rstrip("/") != self.cfg.site_url.rstrip("/"):
                page.goto(self.cfg.site_url, wait_until="domcontentloaded")
            if not self.is_logged_in(page):
                log("WARNING: could not confirm logged-in state (marker not found). Continuing anyway; check screenshots.")
        context.storage_state(path=str(self.cfg.storage_state))
        log(f"saved session to {self.cfg.storage_state}")

    # -- schedule ----------------------------------------------------------
    def open_schedule(self, page: Page) -> None:
        if self.cfg.schedule_url:
            log(f"opening schedule deeplink {self.cfg.schedule_url}")
            page.goto(self.cfg.schedule_url, wait_until="domcontentloaded", timeout=45_000)
        else:
            self.click_if_present(page, "schedule_link", timeout=3000)
            page.wait_for_load_state("domcontentloaded")
        self.click_if_present(page, "day_view", timeout=1500)
        self.dump_dom(page, "schedule")
        self.shot(page, "schedule")

    def matching_cards(self, page: Page) -> list[Locator]:
        """Smallest ancestors of each instructor-name match that contain an action button."""
        cfg = self.cfg
        name_re = re.compile(re.escape(cfg.instructor), re.I)
        matches = page.get_by_text(name_re)
        cards: list[Locator] = []
        try:
            n = min(matches.count(), 25)
        except PlaywrightError:
            return cards
        for i in range(n):
            node = matches.nth(i)
            card = None
            for _ in range(8):
                try:
                    if node.locator("button, a[role='button'], a").count() > 0:
                        card = node
                        break
                    node = node.locator("xpath=..")
                except PlaywrightError:
                    break
            if card is None:
                continue
            try:
                text = card.inner_text(timeout=1500)
            except PlaywrightError:
                continue
            if cfg.discipline and cfg.discipline.lower() not in text.lower():
                continue
            if cfg.class_time and self._norm(cfg.class_time) not in self._norm(text):
                continue
            if cfg.class_day and cfg.class_day.lower() not in text.lower():
                continue
            cards.append(card)
        return cards

    @staticmethod
    def _norm(s: str) -> str:
        return re.sub(r"\s+", "", s).lower().replace(".", "")

    def action_button(self, card: Locator, allow_waitlist: bool) -> tuple[Optional[Locator], str]:
        buttons = card.locator("button, a")
        try:
            n = min(buttons.count(), 15)
        except PlaywrightError:
            return None, ""
        waitlist_btn = None
        for i in range(n):
            b = buttons.nth(i)
            try:
                if not b.is_visible() or not b.is_enabled():
                    continue
                label = (b.inner_text(timeout=500) or b.get_attribute("aria-label") or "").strip()
            except PlaywrightError:
                continue
            if WAITLIST_WORDS.search(label):
                waitlist_btn = waitlist_btn or b
                continue
            if BOOK_WORDS.search(label):
                return b, "book"
        if allow_waitlist and waitlist_btn is not None:
            return waitlist_btn, "waitlist"
        return None, ""

    # -- booking -----------------------------------------------------------
    def complete_checkout(self, page: Page, kind: str) -> bool:
        """After clicking Book: click through confirm/pay steps until a success marker appears."""
        deadline = time.time() + 45
        while time.time() < deadline:
            if self.first_visible(page, "success_marker", timeout=700) is not None:
                return True
            if self.first_visible(page, "full_marker", timeout=200) is not None:
                log("class reported full during checkout")
                return False
            if self.cfg.dry_run:
                confirm = self.first_visible(page, "confirm_button", timeout=700)
                if confirm is not None:
                    log(f"DRY RUN: would click '{confirm.inner_text()[:40]}' to finish the {kind}")
                    self.shot(page, "dry_run_confirm")
                    return True
            else:
                if self.click_if_present(page, "confirm_button", timeout=700):
                    log("clicked a confirm/pay step")
                    self.shot(page, "confirm_step")
                    try:
                        page.wait_for_load_state("domcontentloaded", timeout=5000)
                    except PlaywrightTimeout:
                        pass
            time.sleep(0.3)
        log("checkout did not reach a success marker within 45s")
        return False

    def try_once(self, page: Page) -> tuple[str, Optional[str]]:
        """One booking attempt. Returns (status, detail): status in booked|waitlisted|full|none|error."""
        cards = self.matching_cards(page)
        if not cards:
            return "none", "no card for instructor"
        full_seen = False
        for card in cards:
            btn, kind = self.action_button(card, allow_waitlist=self.cfg.waitlist)
            if btn is None:
                try:
                    txt = card.inner_text(timeout=500)
                except PlaywrightError:
                    txt = ""
                if re.search(r"sold out|full|no spots", txt, re.I):
                    full_seen = True
                continue
            try:
                label = btn.inner_text(timeout=500)
            except PlaywrightError:
                label = kind
            log(f"clicking '{label.strip()}' on card: {card.inner_text(timeout=500)[:90]!r}")
            self.shot(page, f"click_{kind}")
            try:
                btn.click(timeout=4000)
            except PlaywrightError as e:
                return "error", f"click failed: {e}"
            self.dump_dom(page, f"after_click_{kind}")
            if self.complete_checkout(page, kind):
                self.shot(page, "success")
                self.dump_dom(page, "success")
                return ("waitlisted" if kind == "waitlist" else "booked"), label.strip()
            self.shot(page, "checkout_failed")
            # back to the schedule for another go
            try:
                page.go_back(wait_until="domcontentloaded", timeout=8000)
            except PlaywrightError:
                self.open_schedule(page)
            return "error", "checkout did not confirm"
        return ("full", "class full, waitlist disabled") if full_seen else ("none", "card found but no book/waitlist button yet")

    def poll(self, page: Page, release: dt.datetime) -> str:
        end = max(release, dt.datetime.now(ET)) + dt.timedelta(seconds=self.cfg.poll_seconds)
        attempt = 0
        last_detail = None
        while dt.datetime.now(ET) < end:
            attempt += 1
            status, detail = self.try_once(page)
            if status in ("booked", "waitlisted"):
                log(f"SUCCESS ({status}) after {attempt} attempt(s): {detail}")
                return status
            if detail != last_detail:
                log(f"attempt {attempt}: {status} - {detail}")
                last_detail = detail
            if status == "full" and not self.cfg.waitlist:
                # keep polling: spots get released when payments fail / people cancel
                pass
            time.sleep(self.cfg.poll_interval)
            try:
                if self.cfg.schedule_url:
                    page.goto(self.cfg.schedule_url, wait_until="domcontentloaded", timeout=15_000)
                else:
                    page.reload(wait_until="domcontentloaded", timeout=15_000)
                self.click_if_present(page, "day_view", timeout=300)
            except PlaywrightError as e:
                log(f"reload failed: {e}")
                time.sleep(1)
        log(f"gave up after {attempt} attempts / {self.cfg.poll_seconds:.0f}s")
        return "not_booked"

    # -- orchestration -----------------------------------------------------
    def run(self) -> int:
        cfg = self.cfg
        release = next_release(cfg)
        now = dt.datetime.now(ET)
        if not cfg.now:
            until = (release - now).total_seconds() / 60
            if until > cfg.max_wait_minutes:
                log(f"next release is {release:%a %Y-%m-%d %H:%M} ET ({until:.0f} min away) > --max-wait-minutes {cfg.max_wait_minutes:.0f}; exiting")
                return EXIT_NO_RELEASE_SOON
            log(f"targeting release {release:%a %Y-%m-%d %H:%M} ET; pre-start at T-{cfg.prestart_minutes:g} min")
            sleep_until(release - dt.timedelta(minutes=cfg.prestart_minutes))
        else:
            release = dt.datetime.now(ET)
            log("--now: starting immediately")

        with sync_playwright() as pw:
            launch_kwargs = {"headless": not cfg.headful, "slow_mo": cfg.slow_mo}
            if cfg.chromium_path:
                launch_kwargs["executable_path"] = cfg.chromium_path
            browser: Browser = pw.chromium.launch(**launch_kwargs)
            ctx_kwargs = {
                "viewport": {"width": 1366, "height": 900},
                "timezone_id": "America/New_York",
                "locale": "en-US",
                "user_agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                               "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"),
            }
            if cfg.storage_state.exists():
                ctx_kwargs["storage_state"] = str(cfg.storage_state)
            context = browser.new_context(**ctx_kwargs)
            context.set_default_timeout(10_000)
            page = context.new_page()
            self.attach_discovery(page)

            try:
                self.ensure_logged_in(page, context)
                self.open_schedule(page)
                if cfg.site_url.startswith("http"):
                    self.offset = measure_clock_offset(context, cfg.site_url)
                    log(f"clock offset vs server: {self.offset:+.1f}s")

                cards = self.matching_cards(page)
                log(f"pre-release: found {len(cards)} card(s) matching {cfg.instructor!r}"
                    + (f" / {cfg.discipline}" if cfg.discipline else "")
                    + (f" / {cfg.class_time}" if cfg.class_time else ""))
                for c in cards[:5]:
                    try:
                        log("  - " + " | ".join(c.inner_text(timeout=500).split("\n"))[:140])
                    except PlaywrightError:
                        pass

                if not cfg.now:
                    # one more refresh at T-20s so the session/CSRF are fresh, then wait for T-0
                    sleep_until(release - dt.timedelta(seconds=20), self.offset)
                    try:
                        page.reload(wait_until="domcontentloaded", timeout=15_000)
                        self.click_if_present(page, "day_view", timeout=300)
                    except PlaywrightError:
                        pass
                    sleep_until(release, self.offset)
                    log("T-0: polling")

                status = self.poll(page, release)
            except SystemExit as e:
                log(f"FATAL: {e}")
                self.shot(page, "fatal")
                status = "error"
            except Exception as e:  # noqa: BLE001
                log(f"ERROR: {type(e).__name__}: {e}")
                self.shot(page, "error")
                status = "error"
            finally:
                try:
                    context.storage_state(path=str(cfg.storage_state))
                except PlaywrightError:
                    pass
                if self.requests_log:
                    self.requests_log.close()
                    log(f"discovery written to {cfg.out_dir}")
                browser.close()

        self.notify(f"Peloton Studios booker: {status} for {cfg.instructor} ({release:%a %m/%d %H:%M} ET)")
        if status in ("booked", "waitlisted"):
            return EXIT_BOOKED
        if status == "error":
            return EXIT_ERROR
        return EXIT_NOT_BOOKED

    def notify(self, text: str) -> None:
        log(text)
        if not self.cfg.notify_url:
            return
        try:
            import urllib.request
            req = urllib.request.Request(self.cfg.notify_url, data=text.encode(), method="POST",
                                         headers={"Content-Type": "text/plain"})
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as e:  # noqa: BLE001
            log(f"notify failed: {e}")


def main(argv: Optional[list[str]] = None) -> int:
    cfg = parse_args(argv)
    return Booker(cfg).run()


if __name__ == "__main__":
    sys.exit(main())
