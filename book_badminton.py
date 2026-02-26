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

    # Login form appears (modal or new view) — wait for the email field
    email_input = page.locator('input[placeholder="Enter Your Email"]')
    email_input.wait_for(state="visible", timeout=15_000)
    email_input.fill(EMAIL)
    log.debug("Filled email field.")

    password_input = page.locator('input[placeholder="Enter Your Password"]')
    password_input.wait_for(state="visible", timeout=10_000)
    password_input.fill(PASSWORD)
    log.debug("Filled password field.")

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
    # Using "load" rather than "networkidle" because CourtReserve is a
    # React SPA that rarely reaches a true networkidle state.
    try:
        page.wait_for_load_state("load", timeout=15_000)
    except PlaywrightTimeout:
        pass  # proceed anyway if load takes too long

    # If login redirected away from the booking calendar, go back.
    # Use "domcontentloaded" to avoid ERR_ABORTED on SPA navigations.
    if ORG_URL not in page.url:
        log.info("Navigating back to booking calendar…")
        page.goto(ORG_URL, wait_until="domcontentloaded")
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


def find_and_book_slot(page) -> bool:
    """
    Scan the calendar/grid for available slots within preferred windows
    across the next DAYS_AHEAD days.  Returns True if a booking was made.
    """
    today = datetime.today().date()

    for day_offset in range(DAYS_AHEAD):
        target_date = today + timedelta(days=day_offset)
        log.info("Checking availability for %s…", target_date.strftime("%A %Y-%m-%d"))

        # Navigate to the target date if the page supports date navigation
        _navigate_to_date(page, target_date)

        # Find all available (not disabled/booked) time slots
        booked = _attempt_book_on_page(page, target_date)
        if booked:
            return True

    return False


def _navigate_to_date(page, target_date) -> None:
    """
    Attempt to set the date picker to target_date.
    CourtReserve uses a date input or clickable calendar.
    """
    date_str = target_date.strftime("%m/%d/%Y")

    # Try common date input selectors
    date_selectors = [
        'input[id*="date" i][type="text"]',
        'input[id*="date" i][type="date"]',
        'input[name*="date" i]',
        'input.datepicker',
    ]
    for sel in date_selectors:
        try:
            el = page.locator(sel).first
            if el.count() > 0 and el.is_visible(timeout=1_500):
                el.triple_click()
                el.fill(date_str)
                el.press("Enter")
                page.wait_for_load_state("networkidle")
                log.debug("Set date picker to %s", date_str)
                return
        except Exception:
            pass

    # Fallback: try clicking 'next day' arrows until we reach the target
    # (only useful when DAYS_AHEAD is small)


def _attempt_book_on_page(page, target_date) -> bool:
    """
    Find cells showing 'N Available' inside the Badminton column and book
    the first one that falls within a preferred time window.

    CourtReserve renders available courts as clickable cells (anchor tags or
    divs) whose visible text is exactly 'N Available' (e.g. '1 Available').
    Unavailable slots read 'UNAVAILABLE' or 'NONE AVAILABLE' — the regex
    below excludes those by requiring a leading digit.
    """
    import re

    # Match "1 Available", "2 Available", etc. — NOT "UNAVAILABLE" / "NONE AVAILABLE"
    available_slots = page.locator("a, td, div").filter(
        has_text=re.compile(r"^\d+\s+Available$", re.IGNORECASE)
    )

    count = available_slots.count()
    log.info(
        "Found %d 'N Available' cell(s) on %s.", count, target_date.strftime("%Y-%m-%d")
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

            slot_text = slot.inner_text(timeout=1_000).strip()
            log.info(
                "Attempting to book: '%s' on %s at %02d:00…",
                slot_text,
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
        browser = pw.chromium.launch(headless=HEADLESS)
        context = browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/122.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        try:
            login(page)
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
