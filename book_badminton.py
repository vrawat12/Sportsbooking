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
    "https://app.courtreserve.com/Online/Reservations/Bookings",
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
    """Log in to CourtReserve with email + password."""
    log.info("Navigating to login page…")
    page.goto("https://app.courtreserve.com/Account/Login", wait_until="networkidle")

    page.fill('input[name="Email"], input[id="Email"], input[type="email"]', EMAIL)
    page.fill('input[name="Password"], input[id="Password"], input[type="password"]', PASSWORD)
    page.click('button[type="submit"], input[type="submit"]')

    # Wait for redirect away from the login page
    page.wait_for_url(lambda url: "Login" not in url, timeout=15_000)
    log.info("Logged in successfully.")


def navigate_to_bookings(page) -> None:
    """Navigate to the Kotofit bookings page."""
    log.info("Opening bookings page: %s", ORG_URL)
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
    Look for clickable 'available' slot buttons on the current page view.
    Filter by preferred time windows and book the first matching one.
    """
    # CourtReserve renders available slots as buttons / anchor tags with
    # class names like 'available', 'open', etc.
    slot_selectors = [
        "td.reservationCell:not(.reserved):not(.disabled):not(.closed) a",
        "div.available-slot a",
        "a.available",
        "button.slot-available",
        ".k-scheduler-table td[data-slot-available='true']",
        # Generic: any cell that is not grayed out
        ".reservation-cell:not(.unavailable) a",
    ]

    for sel in slot_selectors:
        slots = page.locator(sel)
        count = slots.count()
        if count == 0:
            continue

        log.debug("Found %d potential slots with selector '%s'", count, sel)

        for i in range(count):
            slot = slots.nth(i)
            try:
                slot_text = slot.inner_text(timeout=1_000).strip()
                slot_hour = _parse_hour_from_text(slot_text)
                if slot_hour is None:
                    continue
                if not _slot_in_preferred_window(slot_hour):
                    log.debug(
                        "Slot at %02d:00 outside preferred windows, skipping.", slot_hour
                    )
                    continue

                log.info(
                    "Found matching slot on %s at %02d:00 — attempting to book…",
                    target_date,
                    slot_hour,
                )
                slot.click()
                page.wait_for_load_state("networkidle")

                # Confirm the booking dialog if one appears
                confirmed = _confirm_booking(page)
                if confirmed:
                    log.info(
                        "SUCCESS: Booked badminton court on %s at %02d:00",
                        target_date,
                        slot_hour,
                    )
                    return True
                else:
                    log.warning("Booking confirmation failed; going back.")
                    page.go_back()
                    page.wait_for_load_state("networkidle")

            except Exception as exc:
                log.debug("Error processing slot %d: %s", i, exc)
                continue

    return False


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
