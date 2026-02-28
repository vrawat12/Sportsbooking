"""
Kotofit Badminton Slot Notifier
================================
Polls the CourtReserve booking page for Kotofit Jersey City (Brunswick St)
every 30 minutes and sends a Telegram message when badminton courts are
available between 6:00 PM and 9:30 PM (1-hour slots).

No automatic booking is performed — the script only notifies you so you
can decide whether to book.

Prerequisites:
    pip install -r requirements.txt
    playwright install chromium

Setup:
    cp .env.example .env   # fill in credentials, org URL, and Telegram tokens
    python book_badminton.py
"""

import os
import time
import json
import random
import logging
import urllib.request
import urllib.error
from datetime import datetime, timedelta

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

load_dotenv()

EMAIL    = os.getenv("KOTOFIT_EMAIL", "")
PASSWORD = os.getenv("KOTOFIT_PASSWORD", "")
ORG_URL  = os.getenv(
    "COURTRESERVE_ORG_URL",
    "https://app.courtreserve.com/Online/Reservations/Bookings/8848?sId=21387",
)
DAYS_AHEAD     = int(os.getenv("DAYS_AHEAD", "7"))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_SECONDS", "1800"))
SPORT_TYPE     = os.getenv("SPORT_TYPE", "badminton").lower()
HEADLESS       = os.getenv("HEADLESS", "false").lower() == "true"
COOKIES_FILE   = os.getenv("COOKIES_FILE", "courtreserve_cookies.json")

# Telegram — get a bot token from @BotFather, your chat ID from @userinfobot
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID", "")

# Evening slot window: only report slots whose START time is in [18:00, 21:30]
_WINDOW_START_MINS = 18 * 60       # 6:00 PM
_WINDOW_END_MINS   = 21 * 60 + 30  # 9:30 PM

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _validate_config() -> None:
    """Abort early if required env vars are missing."""
    missing = []
    if not EMAIL:
        missing.append("KOTOFIT_EMAIL")
    if not PASSWORD:
        missing.append("KOTOFIT_PASSWORD")
    if "XXXX" in ORG_URL or not ORG_URL.startswith("http"):
        missing.append("COURTRESERVE_ORG_URL")
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_CHAT_ID:
        missing.append("TELEGRAM_CHAT_ID")
    if missing:
        raise RuntimeError(
            f"Missing required environment variables: {', '.join(missing)}\n"
            "Copy .env.example to .env and fill in the values."
        )


def _human_delay(min_s: float = 1.0, max_s: float = 3.0) -> None:
    """Sleep a random interval to mimic human interaction timing."""
    time.sleep(random.uniform(min_s, max_s))


def _send_telegram(text: str) -> None:
    """Send a plain-text message via the Telegram Bot API."""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    # ensure_ascii=False encodes emoji as UTF-8 bytes rather than broken
    # surrogate-pair escape sequences (\ud83d\udcc5) that cause HTTP 400.
    payload = json.dumps(
        {"chat_id": TELEGRAM_CHAT_ID, "text": text}, ensure_ascii=False
    ).encode("utf-8")
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    log.info("Calling Telegram API (chat_id=%s, %d chars)", TELEGRAM_CHAT_ID, len(text))
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                log.info("Telegram notification sent.")
            else:
                log.warning("Telegram API returned status %d", resp.status)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        log.warning("Telegram HTTP %d error: %s", exc.code, body[:500])
    except Exception as exc:
        log.warning("Failed to send Telegram notification: %s", exc)


def _format_slot(hour: int, minute: int) -> str:
    """Format a 1-hour slot starting at (hour, minute) in 12-hour time.

    Example: (18, 30) → '6:30 PM – 7:30 PM'
    """
    def _fmt(h: int, m: int) -> str:
        suffix = "AM" if h < 12 else "PM"
        h12 = h % 12 or 12
        return f"{h12}:{m:02d} {suffix}"

    return f"{_fmt(hour, minute)} – {_fmt(hour + 1, minute)}"


# ---------------------------------------------------------------------------
# Core automation
# ---------------------------------------------------------------------------

def login(page) -> None:
    """Log in to CourtReserve via the booking calendar page.

    The calendar loads directly (no automatic login redirect).  A 'LOG IN'
    button sits in the top-right nav bar.  Clicking it opens a modal/page
    with the email+password form.  After submitting, the modal closes and
    the LOG IN button disappears, confirming authentication.
    """
    log.info("Loading booking calendar: %s", ORG_URL)
    page.goto(ORG_URL, wait_until="domcontentloaded")
    page.wait_for_load_state("networkidle")

    login_btn = page.locator(
        'a:has-text("LOG IN"), button:has-text("LOG IN"), '
        'a:has-text("Log In"), button:has-text("Log In")'
    ).first

    try:
        login_btn.wait_for(state="visible", timeout=5_000)
    except PlaywrightTimeout:
        log.info("LOG IN button not found — already authenticated.")
        return

    log.info("Clicking LOG IN button…")
    login_btn.click()
    _human_delay()  # wait for modal to animate in

    email_input = page.locator('input[placeholder="Enter Your Email"]')
    email_input.wait_for(state="visible", timeout=15_000)
    email_input.fill(EMAIL)
    log.debug("Filled email field.")
    _human_delay()  # pause between fields, like a human tabbing over

    password_input = page.locator('input[placeholder="Enter Your Password"]')
    password_input.wait_for(state="visible", timeout=10_000)
    password_input.fill(PASSWORD)
    log.debug("Filled password field.")
    _human_delay()  # pause before submitting

    continue_btn = page.locator(
        'button[data-testid="Continue"][data-type="primary"]'
    )
    continue_btn.wait_for(state="visible", timeout=10_000)
    continue_btn.click()

    login_btn.wait_for(state="hidden", timeout=20_000)
    log.info("Logged in successfully.")

    # Let the post-login redirect finish before inspecting the URL.
    # CourtReserve is a React SPA that fires a JS redirect immediately after
    # the "load" event, so we wait for "load" first and then give the SPA an
    # extra 2 s to settle so we don't call goto() while a navigation is
    # already in flight (which causes ERR_ABORTED).
    try:
        page.wait_for_load_state("load", timeout=15_000)
    except PlaywrightTimeout:
        pass
    time.sleep(2)  # buffer for any post-load SPA redirect to complete

    # If login redirected away from the booking calendar, go back.
    # Retry once in case the first goto() is still aborted by a lingering
    # in-flight navigation.
    if ORG_URL not in page.url:
        log.info("Navigating back to booking calendar…")
        for _attempt in range(2):
            try:
                page.goto(ORG_URL, wait_until="domcontentloaded")
                break
            except Exception as exc:
                if _attempt == 0 and "ERR_ABORTED" in str(exc):
                    log.debug("goto aborted (SPA still navigating), retrying in 2 s…")
                    time.sleep(2)
                else:
                    raise
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except PlaywrightTimeout:
            pass  # SPA — networkidle may never fire; the page is usable anyway


def navigate_to_bookings(page) -> None:
    """Ensure we are on the booking calendar page."""
    if ORG_URL not in page.url:
        log.info("Navigating to booking calendar: %s", ORG_URL)
        page.goto(ORG_URL, wait_until="networkidle")


def select_sport(page) -> bool:
    """If there is a sport/court-type filter, select 'badminton'."""
    selectors = [
        'select[id*="sport" i]',
        'select[name*="sport" i]',
        'select[id*="court" i]',
        'select[name*="courtType" i]',
    ]
    for sel in selectors:
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible(timeout=2_000):
                el.select_option(label=SPORT_TYPE.capitalize())
                log.info("Selected sport filter: %s", SPORT_TYPE)
                page.wait_for_load_state("networkidle")
                return True
        except Exception:
            pass
    return False


def _get_calendar_frame(page):
    """
    Detect which frame (iframe or main page) contains the booking calendar.
    Returns the matching Frame, or the main page if no iframe matches.
    """
    try:
        page.wait_for_selector("iframe", timeout=10_000)
    except PlaywrightTimeout:
        pass

    all_frames = page.frames
    iframe_count = len(all_frames) - 1
    log.info("Found %d iframe(s) on the page.", iframe_count)
    for idx, frm in enumerate(all_frames[1:], start=1):
        log.debug("  iframe[%d] url=%s", idx, frm.url)

    for idx, frm in enumerate(all_frames[1:], start=1):
        try:
            frm.wait_for_selector(
                "text=/Reserve|NONE AVAILABLE/i",
                timeout=8_000,
            )
            log.info("Calendar grid detected in iframe[%d].", idx)
            return frm
        except PlaywrightTimeout:
            continue

    log.info("No calendar iframe matched; using main page frame.")
    return page


def _wait_for_calendar(ctx) -> None:
    """Block until the calendar grid has rendered at least one slot cell."""
    try:
        ctx.wait_for_selector("text=/Reserve|NONE AVAILABLE/i", timeout=15_000)
        return
    except PlaywrightTimeout:
        pass

    for sel in (".fc-widget-content", ".fc-time-grid td", ".fc-day-grid td",
                "[class*='fc-slot']", "td.fc-agenda-slots"):
        try:
            ctx.wait_for_selector(sel, timeout=8_000)
            return
        except PlaywrightTimeout:
            continue

    log.debug("_wait_for_calendar: calendar may not have fully rendered")


_NEXT_BTN_SELECTORS = [
    ".fc-next-button",
    "button.fc-button[title*='next' i]",
    "button[aria-label*='next' i]",
    ".fc-button-next",
    "a.fc-next",
]


def _navigate_to_date(page, cal_frame, day_offset: int) -> None:
    """Advance the CourtReserve calendar to the correct date."""
    if day_offset == 0:
        return

    clicked = False
    for search_ctx in (cal_frame, page):
        for sel in _NEXT_BTN_SELECTORS:
            try:
                btn = search_ctx.locator(sel).first
                if btn.is_visible(timeout=2_000):
                    btn.click()
                    clicked = True
                    break
            except PlaywrightTimeout:
                continue
            except Exception:
                continue
        if clicked:
            break

    if not clicked:
        log.warning(
            "Could not find next-day arrow (offset %d); calendar may not advance.",
            day_offset,
        )

    _wait_for_calendar(cal_frame)


def _parse_slot_time(text: str) -> tuple[int, int] | None:
    """
    Parse (hour_24, minute) from a slot label like '8:00 AM', '5:30 PM', '17:30'.
    Returns None if parsing fails.
    """
    import re

    m = re.search(r"(\d{1,2}):(\d{2})\s*(AM|PM)?", text, re.IGNORECASE)
    if not m:
        return None

    hour     = int(m.group(1))
    minute   = int(m.group(2))
    meridiem = (m.group(3) or "").upper()

    if meridiem == "PM" and hour != 12:
        hour += 12
    elif meridiem == "AM" and hour == 12:
        hour = 0

    return (hour, minute)


def _get_slot_time(slot_element) -> tuple[int, int] | None:
    """
    Extract the (hour, minute) for a slot by inspecting its DOM context.

    Tries, in order:
    1. data-time / data-start attribute on the element or its ancestors
    2. The first cell of the enclosing <tr> (the row time-label)
    3. Any ancestor element whose text contains a time pattern
    """
    try:
        time_text = slot_element.evaluate(
            """el => {
                for (const attr of ['data-time', 'data-start', 'data-slot-time',
                                    'data-begin', 'data-starttime']) {
                    let node = el;
                    while (node) {
                        const val = node.getAttribute && node.getAttribute(attr);
                        if (val) return val;
                        node = node.parentElement;
                    }
                }
                return null;
            }"""
        )
        if time_text:
            t = _parse_slot_time(time_text)
            if t is not None:
                return t

        row_label = slot_element.evaluate(
            """el => {
                const row = el.closest('tr') || el.closest('[role="row"]');
                if (!row) return null;
                const firstCell = row.querySelector(
                    'td:first-child, th:first-child, [role="rowheader"]'
                );
                return firstCell ? firstCell.innerText.trim() : null;
            }"""
        )
        if row_label:
            t = _parse_slot_time(row_label)
            if t is not None:
                return t

        ancestor_text = slot_element.evaluate(
            r"""el => {
                let node = el.parentElement;
                for (let i = 0; i < 6; i++) {
                    if (!node) break;
                    if (/\d{1,2}:\d{2}\s*(AM|PM)/i.test(node.innerText || ''))
                        return node.innerText;
                    node = node.parentElement;
                }
                return null;
            }"""
        )
        if ancestor_text:
            return _parse_slot_time(ancestor_text)

    except Exception as exc:
        log.debug("_get_slot_time error: %s", exc)

    return None


def _collect_slots_on_page(page, cal_frame, target_date) -> list[str]:
    """
    Find all available (Reserve) slots on the current calendar day whose
    start time falls between 6:00 PM and 9:30 PM.

    Returns a list of formatted strings like "6:00 PM – 7:00 PM".
    No clicking or booking is performed.
    """
    log.info("Waiting 4 s for page to fully settle before scanning…")
    time.sleep(4)

    # ── Diagnostic dump ──────────────────────────────────────────────────────
    try:
        body_text = cal_frame.inner_text("body")
        has_reserve = "reserve" in body_text.lower()
        log.info("Diagnostic: 'reserve' in page text = %s", has_reserve)
        if has_reserve:
            idx = body_text.lower().index("reserve")
            start = max(0, idx - 120)
            end   = min(len(body_text), idx + 180)
            log.info("Diagnostic: context around first 'reserve': …%s…",
                     body_text[start:end].replace("\n", " | "))
        else:
            snippet = body_text[:2_000].replace("\n", " | ")
            log.info("Diagnostic: page inner_text (first 2000 chars): %s", snippet)
    except Exception as exc:
        log.debug("Diagnostic inner_text dump failed: %s", exc)

    try:
        html = page.content()
        has_reserve_html = "reserve" in html.lower()
        log.info("Diagnostic: 'reserve' in page HTML  = %s", has_reserve_html)
        if not has_reserve_html:
            log.info("Diagnostic: page HTML snippet (first 3000 chars): %s", html[:3_000])
    except Exception as exc:
        log.debug("Diagnostic HTML dump failed: %s", exc)

    try:
        reserve_nodes = cal_frame.evaluate(
            """() => {
                const out = [];
                const walker = document.createTreeWalker(
                    document.body, NodeFilter.SHOW_TEXT
                );
                let node;
                while ((node = walker.nextNode()) && out.length < 3) {
                    if (node.textContent.trim().toLowerCase() === 'reserve') {
                        const el = node.parentElement;
                        out.push({
                            tag:        el ? el.tagName : 'N/A',
                            className:  el ? el.className : '',
                            outerHTML:  el ? el.outerHTML.substring(0, 400) : '',
                            parentTag:  el && el.parentElement ? el.parentElement.tagName : '',
                            parentHTML: el && el.parentElement
                                            ? el.parentElement.outerHTML.substring(0, 600) : '',
                        });
                    }
                }
                return out;
            }"""
        )
        for i, n in enumerate(reserve_nodes or []):
            log.info("Diagnostic: Reserve node[%d] tag=<%s> class=%r outerHTML=%s",
                     i, n["tag"], n["className"], n["outerHTML"])
            log.info("Diagnostic: Reserve node[%d] parent=<%s> parentHTML=%s",
                     i, n["parentTag"], n["parentHTML"])
    except Exception as exc:
        log.debug("Diagnostic Reserve DOM dump failed: %s", exc)
    # ─────────────────────────────────────────────────────────────────────────

    available_slots = cal_frame.get_by_text("Reserve", exact=True)
    count = available_slots.count()
    log.info("Found %d 'Reserve' cell(s) on %s.", count, target_date.strftime("%Y-%m-%d"))
    if count == 0:
        return []

    seen_times: set[tuple[int, int]] = set()
    found: list[str] = []
    for i in range(count):
        slot = available_slots.nth(i)
        try:
            slot_time = _get_slot_time(slot)
            if slot_time is None:
                log.debug("Slot %d: could not determine time, skipping.", i)
                continue

            h, m = slot_time
            if not (_WINDOW_START_MINS <= h * 60 + m <= _WINDOW_END_MINS):
                log.debug(
                    "Slot %d at %02d:%02d is outside 6–9:30 PM window, skipping.",
                    i, h, m,
                )
                continue

            # Filter by data-courttype="Badminton" on the container div.
            # The CourtReserve DOM structure is:
            #   <div data-courttype="Badminton" data-time="6:00 PM">
            #     <a class="btn slot-btn ...">Reserve</a>
            #   </div>
            # Compact courts have data-courttype="Badminton (Compact - 20% Off)".
            is_badminton = slot.evaluate(
                """el => {
                    const c = el.closest('[data-courttype]');
                    if (!c) return true;  // can't determine — allow it
                    return c.getAttribute('data-courttype') === 'Badminton';
                }"""
            )
            if not is_badminton:
                log.debug("Slot %d: courttype is not 'Badminton', skipping.", i)
                continue

            # Deduplicate by start time — multiple physical courts can be
            # open at the same time; report each time slot only once.
            if (h, m) in seen_times:
                log.debug("Slot %d at %02d:%02d already listed, skipping.", i, h, m)
                continue
            seen_times.add((h, m))

            found.append(_format_slot(h, m))
        except Exception as exc:
            log.debug("Error processing slot %d: %s", i, exc)

    log.info(
        "Found %d evening slot(s) on %s.", len(found), target_date.strftime("%Y-%m-%d")
    )
    return found


def find_available_slots(page) -> dict:
    """
    Scan the calendar across the next DAYS_AHEAD days for available evening
    badminton slots (6:00 PM – 9:30 PM start times).

    Returns a dict mapping date labels to lists of formatted slot strings:
        {"Thursday Feb 26": ["6:00 PM – 7:00 PM", "7:30 PM – 8:30 PM"], ...}
    An empty dict means no slots were found across all scanned days.
    """
    today = datetime.today().date()
    cal_frame = _get_calendar_frame(page)
    results = {}

    for day_offset in range(DAYS_AHEAD):
        target_date = today + timedelta(days=day_offset)
        log.info("Checking availability for %s…", target_date.strftime("%A %Y-%m-%d"))

        _navigate_to_date(page, cal_frame, day_offset)
        slots = _collect_slots_on_page(page, cal_frame, target_date)

        if slots:
            # Cross-platform label without leading zero: "Thursday Feb 26"
            date_label = target_date.strftime("%A %b ") + str(target_date.day)
            results[date_label] = slots

    return results


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_once() -> bool:
    """
    Run a single scan cycle.
    Sends a Telegram message if evening slots are found.
    Returns True if any slots were found and notified.
    """
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=HEADLESS,
            args=[
                "--window-size=1280,900",
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
        )

        ctx_kwargs = dict(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
            locale="en-US",
            timezone_id="America/New_York",
        )
        if os.path.exists(COOKIES_FILE):
            ctx_kwargs["storage_state"] = COOKIES_FILE
            log.info("Loaded saved session from %s", COOKIES_FILE)

        context = browser.new_context(**ctx_kwargs)
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = context.new_page()

        try:
            login(page)
            page.context.storage_state(path=COOKIES_FILE)
            log.info("Saved session cookies to %s", COOKIES_FILE)

            navigate_to_bookings(page)
            select_sport(page)
            results = find_available_slots(page)

            if results:
                # Send one short message per day to stay under Telegram's
                # 4096-character limit and keep notifications readable.
                for date_label, slots in results.items():
                    message = (
                        f"📅 {date_label} — Available Badminton slots:\n"
                        + "\n".join(slots)
                    )
                    log.info("Sending Telegram notification:\n%s", message)
                    _send_telegram(message)
                return True

            return False

        except PlaywrightTimeout as exc:
            log.error("Timed out during automation: %s", exc)
        except Exception as exc:
            log.exception("Unexpected error: %s", exc)
        finally:
            context.close()
            browser.close()

    return False


def main() -> None:
    _validate_config()

    log.info("=== Kotofit Badminton Slot Notifier ===")
    log.info("Location : Kotofit Jersey City – Brunswick St")
    log.info("Sport    : %s", SPORT_TYPE)
    log.info("Window   : 6:00 PM – 9:30 PM start (1-hour slots)")
    log.info("Interval : every %d minutes", CHECK_INTERVAL // 60)
    log.info("Headless : %s", HEADLESS)
    log.info("")

    attempt = 0
    while True:
        attempt += 1
        log.info("--- Check #%d at %s ---", attempt, datetime.now().strftime("%H:%M:%S"))

        found = run_once()

        if found:
            log.info("Telegram notification sent for available slots.")
        else:
            log.info("No available evening slots found.")

        log.info("Next check in %d minutes.", CHECK_INTERVAL // 60)
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
