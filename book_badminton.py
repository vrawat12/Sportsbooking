"""
Kotofit Badminton Slot Booking Automation
==========================================
Polls the CourtReserve booking page for Kotofit Jersey City (Brunswick St)
and automatically books an available badminton court during your preferred
time windows.

Prerequisites:
    pip install -r requirements.txt
    playwright install chromium

Usage:
    cp .env.example .env        # fill in your credentials and org URL
    python book_badminton.py
"""

import os
import time
import random
import logging
from datetime import datetime, timedelta

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

load_dotenv()

EMAIL = os.getenv("KOTOFIT_EMAIL", "")
PASSWORD = os.getenv("KOTOFIT_PASSWORD", "")
ORG_URL = os.getenv(
    "COURTRESERVE_ORG_URL",
    "https://app.courtreserve.com/Online/Reservations/Bookings/8848?sId=21387",
)
DAYS_AHEAD = int(os.getenv("DAYS_AHEAD", "7"))
CHECK_INTERVAL = int(os.getenv("CHECK_INTERVAL_SECONDS", "1800"))
SPORT_TYPE = os.getenv("SPORT_TYPE", "badminton").lower()
HEADLESS = os.getenv("HEADLESS", "false").lower() == "true"
# Path where Playwright storage state (cookies + localStorage) is persisted
# between runs so we avoid a fresh login every 30-minute cycle.
COOKIES_FILE = os.getenv("COOKIES_FILE", "courtreserve_cookies.json")

# Parse preferred time windows: "6,12,17,22" → [(6,12),(17,22)]
_raw_windows = os.getenv("PREFERRED_TIME_WINDOWS", "6,12,17,22").split(",")
PREFERRED_WINDOWS: list[tuple[int, int]] = [
    (int(_raw_windows[i]), int(_raw_windows[i + 1]))
    for i in range(0, len(_raw_windows) - 1, 2)
]

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
    if missing:
        raise RuntimeError(
            f"Missing required environment variables: {', '.join(missing)}\n"
            "Copy .env.example to .env and fill in the values."
        )


def _slot_in_preferred_window(slot_hour: int) -> bool:
    """Return True if slot_hour falls within any preferred window."""
    return any(start <= slot_hour < end for start, end in PREFERRED_WINDOWS)


def _human_delay(min_s: float = 1.0, max_s: float = 3.0) -> None:
    """Sleep a random interval to mimic human interaction timing."""
    time.sleep(random.uniform(min_s, max_s))


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

    # Detect the LOG IN nav button (case-insensitive text variants)
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

    # Login form appears (modal or new view) — wait for the email field
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

    # Submit — target the primary "Continue" button explicitly to avoid
    # matching the "Continue with Google" social-login button
    continue_btn = page.locator(
        'button[data-testid="Continue"][data-type="primary"]'
    )
    continue_btn.wait_for(state="visible", timeout=10_000)
    continue_btn.click()

    # Success: the LOG IN button disappears once authenticated
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
    """
    If there is a sport/court-type filter, select 'badminton'.
    Returns True if a filter was found and set; False if not present.
    """
    # CourtReserve often has a dropdown or radio buttons for court type
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
                # Try to select by label containing 'badminton'
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

    CourtReserve embeds the reservation grid inside an <iframe>.
    This function:
      - waits for any <iframe> tags to appear in the page
      - logs the total number of iframes found (for debugging)
      - probes each iframe for calendar content (Reserve / NONE AVAILABLE)
      - returns the matching Frame, falling back to the main page if none found

    Both Frame and Page expose identical .locator() / .wait_for_selector()
    APIs so callers can treat the return value uniformly.
    """
    # Give the page a moment to create any iframes before enumerating
    try:
        page.wait_for_selector("iframe", timeout=10_000)
    except PlaywrightTimeout:
        pass  # No iframes visible yet — will still enumerate page.frames

    all_frames = page.frames          # index 0 is always the main frame
    iframe_count = len(all_frames) - 1
    log.info("Found %d iframe(s) on the page.", iframe_count)
    for idx, frm in enumerate(all_frames[1:], start=1):
        log.debug("  iframe[%d] url=%s", idx, frm.url)

    # Return the first iframe that contains calendar slot labels
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
    """
    Block until the calendar grid has rendered at least one slot cell.
    ctx may be a Page or a Frame — both share the same selector API.

    Looks for slot-label text ('Reserve' / 'NONE AVAILABLE') first, then
    falls back to FullCalendar structural selectors.
    """
    # Most specific signal: a rendered slot label
    try:
        ctx.wait_for_selector(
            "text=/Reserve|NONE AVAILABLE/i",
            timeout=15_000,
        )
        return
    except PlaywrightTimeout:
        pass

    # Structural fallback: any FullCalendar grid cell
    for sel in (".fc-widget-content", ".fc-time-grid td", ".fc-day-grid td",
                "[class*='fc-slot']", "td.fc-agenda-slots"):
        try:
            ctx.wait_for_selector(sel, timeout=8_000)
            return
        except PlaywrightTimeout:
            continue

    log.debug("_wait_for_calendar: calendar may not have fully rendered")


# Selectors for FullCalendar's "go to next day/week" button
_NEXT_BTN_SELECTORS = [
    ".fc-next-button",
    "button.fc-button[title*='next' i]",
    "button[aria-label*='next' i]",
    ".fc-button-next",
    "a.fc-next",
]


def find_and_book_slot(page) -> bool:
    """
    Scan the calendar/grid for available slots within preferred windows
    across the next DAYS_AHEAD days.  Returns True if a booking was made.
    """
    today = datetime.today().date()

    # Discover which frame holds the calendar (also logs iframe count).
    # _get_calendar_frame waits for content to appear, so day_offset=0 needs
    # no additional wait inside _navigate_to_date.
    cal_frame = _get_calendar_frame(page)

    for day_offset in range(DAYS_AHEAD):
        target_date = today + timedelta(days=day_offset)
        log.info("Checking availability for %s…", target_date.strftime("%A %Y-%m-%d"))

        _navigate_to_date(page, cal_frame, day_offset)

        booked = _attempt_book_on_page(page, cal_frame, target_date)
        if booked:
            return True

    return False


def _navigate_to_date(page, cal_frame, day_offset: int) -> None:
    """
    Advance the CourtReserve calendar to the correct date.

    CourtReserve uses FullCalendar's next-arrow button (no free-form date
    input).  We advance one day at a time through the loop: day_offset==0
    means the calendar is already on today (no click needed); each subsequent
    call clicks next once and waits for the new day to render.

    The next-arrow may live inside the iframe or in the main page; we try
    the calendar frame first and fall back to the main page.
    """
    if day_offset == 0:
        # _get_calendar_frame already confirmed the calendar is rendered.
        return

    # Try to click the next-day arrow — search iframe first, then main page.
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

    # Wait for the new day's slots to render inside the calendar frame.
    _wait_for_calendar(cal_frame)


def _attempt_book_on_page(page, cal_frame, target_date) -> bool:
    """
    Find cells showing 'Reserve' inside the Badminton column and book
    the first one that falls within a preferred time window.

    CourtReserve renders available courts as clickable cells (anchor tags or
    divs) whose visible text is exactly 'Reserve'.
    Unavailable slots read 'UNAVAILABLE' or 'NONE AVAILABLE'.
    We anchor the regex (^Reserve$) so we don't accidentally match the
    'Reserve Now' confirmation button that appears later in the flow.

    cal_frame is the Frame (or Page) that contains the calendar grid.
    All slot searches are scoped to it so iframe content is reachable.
    """
    import re

    # Extra settling time: _wait_for_calendar confirms the calendar structure
    # is present, but Cloudflare's JS challenge may still be mutating the DOM.
    # A short fixed sleep lets all post-render JS finish before we scan.
    log.info("Waiting 4 s for page to fully settle before scanning…")
    time.sleep(4)

    # ── Diagnostic dump ──────────────────────────────────────────────────────
    # Runs every scan so we can see exactly what Playwright sees on the page.
    try:
        body_text = cal_frame.inner_text("body")
        has_reserve = "reserve" in body_text.lower()
        log.info("Diagnostic: 'reserve' in page text = %s", has_reserve)

        if has_reserve:
            # Show up to 300 chars of context around the first occurrence
            idx = body_text.lower().index("reserve")
            start = max(0, idx - 120)
            end   = min(len(body_text), idx + 180)
            log.info("Diagnostic: context around first 'reserve': …%s…",
                     body_text[start:end].replace("\n", " | "))
        else:
            # 'reserve' not present at all — show first 2 000 chars so we
            # can see what is actually on the page
            snippet = body_text[:2_000].replace("\n", " | ")
            log.info("Diagnostic: page inner_text (first 2000 chars): %s", snippet)
    except Exception as exc:
        log.debug("Diagnostic inner_text dump failed: %s", exc)

    try:
        html = page.content()
        has_reserve_html = "reserve" in html.lower()
        log.info("Diagnostic: 'reserve' in page HTML  = %s", has_reserve_html)
        if not has_reserve_html:
            log.info("Diagnostic: page HTML snippet (first 3000 chars): %s",
                     html[:3_000])
    except Exception as exc:
        log.debug("Diagnostic HTML dump failed: %s", exc)
    # ─────────────────────────────────────────────────────────────────────────

    # Match cells whose full text is exactly "Reserve" (case-insensitive).
    # Excludes "UNAVAILABLE", "NONE AVAILABLE", and "Reserve Now" buttons.
    # Scoped to cal_frame so iframe content is searched correctly.
    available_slots = cal_frame.locator("a, td, div").filter(
        has_text=re.compile(r"^Reserve$", re.IGNORECASE)
    )

    count = available_slots.count()
    log.info(
        "Found %d 'Reserve' cell(s) on %s.", count, target_date.strftime("%Y-%m-%d")
    )
    if count == 0:
        return False

    for i in range(count):
        slot = available_slots.nth(i)
        try:
            # Only act on slots inside a Badminton column (not Pickleball, etc.)
            in_badminton_col = slot.evaluate(
                """el => {
                    // Walk up to the nearest table cell, then find its column index
                    const cell = el.closest('td') || el.closest('[role="gridcell"]');
                    if (!cell) return true;  // can't determine — allow it

                    const row = cell.closest('tr') || cell.closest('[role="row"]');
                    if (!row) return true;

                    const colIndex = Array.from(row.children).indexOf(cell);

                    // Find the header row
                    const table = row.closest('table') || row.closest('[role="grid"]');
                    if (!table) return true;

                    const headerCells = table.querySelectorAll(
                        'thead th, thead td, [role="columnheader"]'
                    );
                    if (!headerCells.length) return true;

                    const header = headerCells[colIndex];
                    if (!header) return true;

                    const headerText = header.innerText.toLowerCase();
                    // Accept "badminton" columns; exclude the compact/discounted variant
                    return headerText.includes('badminton')
                        && !headerText.includes('compact');
                }"""
            )

            if not in_badminton_col:
                log.debug("Slot %d is not in a Badminton column, skipping.", i)
                continue

            slot_hour = _get_slot_hour(slot)
            if slot_hour is None:
                log.debug("Slot %d: could not determine time, skipping.", i)
                continue

            if not _slot_in_preferred_window(slot_hour):
                log.debug(
                    "Slot %d at %02d:00 is outside preferred windows, skipping.",
                    i,
                    slot_hour,
                )
                continue

            log.info(
                "Attempting to book 'Reserve' slot on %s at %02d:00…",
                target_date,
                slot_hour,
            )
            slot.click()
            page.wait_for_load_state("networkidle")

            confirmed = _confirm_booking(page)
            if confirmed:
                log.info(
                    "SUCCESS: Booked badminton court on %s at %02d:00",
                    target_date,
                    slot_hour,
                )
                return True

            log.warning("Booking confirmation failed; going back.")
            page.go_back()
            page.wait_for_load_state("networkidle")

        except Exception as exc:
            log.debug("Error processing slot %d: %s", i, exc)
            continue

    return False


def _get_slot_hour(slot_element) -> int | None:
    """
    Extract the starting hour for a slot by inspecting its DOM context.

    Tries, in order:
    1. data-time / data-start attribute on the element or its ancestors
    2. The first cell of the enclosing <tr> (the row time-label)
    3. Any ancestor element whose text contains a time pattern
    """
    try:
        # Method 1 — data attributes
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
            h = _parse_hour_from_text(time_text)
            if h is not None:
                return h

        # Method 2 — first cell of the enclosing table row
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
            h = _parse_hour_from_text(row_label)
            if h is not None:
                return h

        # Method 3 — nearest ancestor containing a time string
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
            return _parse_hour_from_text(ancestor_text)

    except Exception as exc:
        log.debug("_get_slot_hour error: %s", exc)

    return None


def _parse_hour_from_text(text: str):
    """
    Parse the starting hour (int, 0-23) from a slot label like
    '8:00 AM', '5:30 PM', '17:00', etc.
    Returns None if parsing fails.
    """
    import re

    # Match patterns: "8:00 AM", "5:30 PM", "17:00"
    m = re.search(r"(\d{1,2}):(\d{2})\s*(AM|PM)?", text, re.IGNORECASE)
    if not m:
        return None

    hour = int(m.group(1))
    meridiem = (m.group(3) or "").upper()

    if meridiem == "PM" and hour != 12:
        hour += 12
    elif meridiem == "AM" and hour == 12:
        hour = 0

    return hour


def _confirm_booking(page) -> bool:
    """
    Handle any confirmation dialog / form that CourtReserve shows after
    clicking a slot.  Returns True if booking was successfully submitted.
    """
    confirm_selectors = [
        'button:has-text("Confirm")',
        'button:has-text("Book")',
        'button:has-text("Reserve")',
        'input[value="Confirm"]',
        'input[value="Book"]',
        '#confirmReservation',
        '.btn-confirm',
    ]

    for sel in confirm_selectors:
        try:
            btn = page.locator(sel).first
            if btn.count() > 0 and btn.is_visible(timeout=3_000):
                btn.click()
                page.wait_for_load_state("networkidle")

                # Check for a success message
                success_keywords = ["confirmed", "success", "booked", "reservation"]
                page_text = page.content().lower()
                if any(kw in page_text for kw in success_keywords):
                    return True
        except Exception:
            pass

    # If no confirm button found, assume the click already booked it
    # and check for a success indicator
    try:
        page.wait_for_selector(
            'text=/confirmed|success|booked|reservation/i', timeout=5_000
        )
        return True
    except PlaywrightTimeout:
        pass

    return False


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_once() -> bool:
    """Run a single check-and-book cycle.  Returns True if a slot was booked."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=HEADLESS,
            args=[
                "--window-size=1280,900",
                # Removes the CDP "Automation" flag that Cloudflare detects.
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
        # Reuse saved cookies/localStorage from the previous run so we skip
        # the login flow entirely on most cycles.
        if os.path.exists(COOKIES_FILE):
            ctx_kwargs["storage_state"] = COOKIES_FILE
            log.info("Loaded saved session from %s", COOKIES_FILE)

        context = browser.new_context(**ctx_kwargs)

        # Mask navigator.webdriver — the primary JS property Cloudflare
        # checks to distinguish headless browsers from real users.
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )

        page = context.new_page()

        try:
            login(page)
            # Always persist the session after login (or when already
            # authenticated via saved cookies) so the next cycle is cookie-warm.
            page.context.storage_state(path=COOKIES_FILE)
            log.info("Saved session cookies to %s", COOKIES_FILE)

            navigate_to_bookings(page)
            select_sport(page)
            booked = find_and_book_slot(page)
            return booked
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

    log.info("=== Kotofit Badminton Auto-Booker ===")
    log.info("Location : Kotofit Jersey City – Brunswick St")
    log.info("Sport    : %s", SPORT_TYPE)
    log.info(
        "Windows  : %s",
        ", ".join(f"{s:02d}:00–{e:02d}:00" for s, e in PREFERRED_WINDOWS),
    )
    log.info("Interval : every %d minutes", CHECK_INTERVAL // 60)
    log.info("Headless : %s", HEADLESS)
    log.info("")

    attempt = 0
    while True:
        attempt += 1
        log.info("--- Check #%d at %s ---", attempt, datetime.now().strftime("%H:%M:%S"))

        booked = run_once()

        if booked:
            log.info("Slot booked! Exiting – check your email for the PIN code.")
            break

        log.info(
            "No suitable slot booked. Next check in %d minutes.",
            CHECK_INTERVAL // 60,
        )
        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
