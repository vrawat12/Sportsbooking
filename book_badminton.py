"""
Kotofit Badminton Booking Assistant
=====================================
Polls CourtReserve every 30 minutes for available Badminton courts between
6:00 PM and 9:30 PM.  When slots are found they are sent to a Claude AI
assistant which crafts a friendly Telegram message.  The user can reply
naturally; when they confirm a slot Claude emits BOOK:DATE:TIME and the
script books it automatically via Playwright.

Prerequisites:
    pip install -r requirements.txt
    playwright install chromium

Setup:
    cp .env.example .env   # fill in all credentials
    python book_badminton.py
"""

import asyncio
import os
import queue
import re
import threading
import time
import json
import random
import logging
import urllib.request
import urllib.error
from datetime import datetime, timedelta

import anthropic
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeout
from telegram import Update
from telegram.ext import Application, ContextTypes, MessageHandler, filters

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
DAYS_AHEAD          = int(os.getenv("DAYS_AHEAD", "7"))
CHECK_INTERVAL      = int(os.getenv("CHECK_INTERVAL_SECONDS", "1800"))
SPORT_TYPE          = os.getenv("SPORT_TYPE", "badminton").lower()
HEADLESS            = os.getenv("HEADLESS", "false").lower() == "true"
COOKIES_FILE        = os.getenv("COOKIES_FILE", "courtreserve_cookies.json")
TELEGRAM_BOT_TOKEN  = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID    = os.getenv("TELEGRAM_CHAT_ID", "")
ANTHROPIC_API_KEY   = os.getenv("ANTHROPIC_API_KEY", "")

# Evening window: start times in [18:00, 21:30]
_WINDOW_START_MINS = 18 * 60
_WINDOW_END_MINS   = 21 * 60 + 30

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
# Global state  (set at startup, read-only afterwards except _conversation)
# ---------------------------------------------------------------------------

_anthropic_client: anthropic.Anthropic | None = None

# Conversation history: list of {"role": "user"|"assistant", "content": str}
_conversation: list[dict] = []
_conversation_lock = threading.Lock()

# Set once the Telegram Application's event loop is running
_bot_app: Application | None = None
_bot_loop: asyncio.AbstractEventLoop | None = None
_bot_ready = threading.Event()   # scan thread waits on this before first notification

# ---------------------------------------------------------------------------
# Claude system prompt (provided by the user)
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You are a helpful badminton court booking assistant for Kotofit Jersey City. "
    "You have access to available court slots and help the user pick and confirm one. "
    "Be conversational and friendly but concise. "
    "When the user confirms a specific slot, respond with EXACTLY this format so the "
    "system can parse it: BOOK:2026-03-01:18:00 (BOOK:DATE:TIME in 24hr format). "
    "If no slots are available tell the user politely."
)

# ---------------------------------------------------------------------------
# Claude helpers
# ---------------------------------------------------------------------------

def _call_claude(user_message: str) -> str:
    """Thread-safe call to Claude, maintaining conversation history."""
    with _conversation_lock:
        _conversation.append({"role": "user", "content": user_message})
        response = _anthropic_client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1024,
            system=SYSTEM_PROMPT,
            messages=list(_conversation),
        )
        reply = response.content[0].text
        _conversation.append({"role": "assistant", "content": reply})
        log.debug("Claude reply: %s", reply[:200])
        return reply


def _parse_booking(text: str) -> tuple[str | None, str | None]:
    """Return (date_str, time_24) if text contains BOOK:DATE:TIME, else (None, None)."""
    m = re.search(r"BOOK:(\d{4}-\d{2}-\d{2}):(\d{2}:\d{2})", text)
    if m:
        return m.group(1), m.group(2)
    return None, None


def _reset_conversation() -> None:
    with _conversation_lock:
        _conversation.clear()

# ---------------------------------------------------------------------------
# Telegram send helper (used from non-async threads)
# ---------------------------------------------------------------------------

def _tg_send(chat_id: str | int, text: str) -> None:
    """Send a Telegram message from any thread via the bot's event loop."""
    if _bot_loop is None or _bot_app is None:
        log.warning("Bot not ready — cannot send: %s", text[:80])
        return
    future = asyncio.run_coroutine_threadsafe(
        _bot_app.bot.send_message(chat_id=int(chat_id), text=text),
        _bot_loop,
    )
    try:
        future.result(timeout=30)
    except Exception as exc:
        log.warning("_tg_send failed: %s", exc)

# ---------------------------------------------------------------------------
# Telegram async handlers
# ---------------------------------------------------------------------------

async def _post_init(app: Application) -> None:
    """Called once the bot's event loop is running — capture the loop reference."""
    global _bot_loop
    _bot_loop = asyncio.get_running_loop()
    _bot_ready.set()
    log.info("Telegram bot ready. Listening for messages…")


async def _handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Receive a user Telegram message, pass to Claude, act on the reply."""
    user_text = update.message.text
    chat_id   = update.effective_chat.id
    log.info("Telegram ← %r", user_text[:120])

    # Run Claude synchronously in the default thread pool so the event loop
    # is not blocked during the Anthropic API call.
    loop  = asyncio.get_running_loop()
    reply = await loop.run_in_executor(None, _call_claude, user_text)
    log.info("Claude  → %r", reply[:120])

    date_str, time_24 = _parse_booking(reply)
    if date_str and time_24:
        await context.bot.send_message(
            chat_id=chat_id,
            text=f"Got it! Booking Badminton on {date_str} at {time_24} right now…",
        )
        # Spawn a daemon thread so booking doesn't block the event loop
        threading.Thread(
            target=_book_and_notify,
            args=(date_str, time_24, chat_id),
            daemon=True,
            name="booking",
        ).start()
    else:
        await context.bot.send_message(chat_id=chat_id, text=reply)

# ---------------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------------

def _validate_config() -> None:
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
    if not ANTHROPIC_API_KEY:
        missing.append("ANTHROPIC_API_KEY")
    if missing:
        raise RuntimeError(
            f"Missing required environment variables: {', '.join(missing)}\n"
            "Copy .env.example to .env and fill in the values."
        )


def _human_delay(min_s: float = 1.0, max_s: float = 3.0) -> None:
    time.sleep(random.uniform(min_s, max_s))


def _format_slot(hour: int, minute: int) -> str:
    """'6:30 PM – 7:30 PM' for a 1-hour slot starting at (hour, minute)."""
    def _fmt(h: int, m: int) -> str:
        suffix = "AM" if h < 12 else "PM"
        h12 = h % 12 or 12
        return f"{h12}:{m:02d} {suffix}"
    return f"{_fmt(hour, minute)} – {_fmt(hour + 1, minute)}"


def _time_to_data_time(hour: int, minute: int) -> str:
    """Convert 24-h (18, 0) to CourtReserve data-time format '6:00 PM'."""
    suffix = "AM" if hour < 12 else "PM"
    h12 = hour % 12 or 12
    return f"{h12}:{minute:02d} {suffix}"

# ---------------------------------------------------------------------------
# Playwright browser setup (shared by scan and booking sessions)
# ---------------------------------------------------------------------------

def _new_browser_context(pw):
    """Launch Chromium and return (browser, context, page)."""
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
    return browser, context, page

# ---------------------------------------------------------------------------
# Core Playwright automation helpers
# ---------------------------------------------------------------------------

def login(page) -> None:
    """Log in to CourtReserve, or return immediately if already authenticated."""
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
    _human_delay()

    email_input = page.locator('input[placeholder="Enter Your Email"]')
    email_input.wait_for(state="visible", timeout=15_000)
    email_input.fill(EMAIL)
    _human_delay()

    password_input = page.locator('input[placeholder="Enter Your Password"]')
    password_input.wait_for(state="visible", timeout=10_000)
    password_input.fill(PASSWORD)
    _human_delay()

    continue_btn = page.locator('button[data-testid="Continue"][data-type="primary"]')
    continue_btn.wait_for(state="visible", timeout=10_000)
    continue_btn.click()

    login_btn.wait_for(state="hidden", timeout=20_000)
    log.info("Logged in successfully.")

    try:
        page.wait_for_load_state("load", timeout=15_000)
    except PlaywrightTimeout:
        pass
    time.sleep(2)

    if ORG_URL not in page.url:
        log.info("Navigating back to booking calendar…")
        for _attempt in range(2):
            try:
                page.goto(ORG_URL, wait_until="domcontentloaded")
                break
            except Exception as exc:
                if _attempt == 0 and "ERR_ABORTED" in str(exc):
                    time.sleep(2)
                else:
                    raise
        try:
            page.wait_for_load_state("networkidle", timeout=15_000)
        except PlaywrightTimeout:
            pass


def navigate_to_bookings(page) -> None:
    if ORG_URL not in page.url:
        log.info("Navigating to booking calendar: %s", ORG_URL)
        page.goto(ORG_URL, wait_until="networkidle")


def select_sport(page) -> bool:
    for sel in ('select[id*="sport" i]', 'select[name*="sport" i]',
                'select[id*="court" i]', 'select[name*="courtType" i]'):
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
    try:
        page.wait_for_selector("iframe", timeout=10_000)
    except PlaywrightTimeout:
        pass
    all_frames = page.frames
    log.info("Found %d iframe(s) on the page.", len(all_frames) - 1)
    for idx, frm in enumerate(all_frames[1:], start=1):
        try:
            frm.wait_for_selector("text=/Reserve|NONE AVAILABLE/i", timeout=8_000)
            log.info("Calendar grid detected in iframe[%d].", idx)
            return frm
        except PlaywrightTimeout:
            continue
    log.info("No calendar iframe matched; using main page frame.")
    return page


def _wait_for_calendar(ctx) -> None:
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
    if day_offset == 0:
        return
    clicked = False
    for ctx in (cal_frame, page):
        for sel in _NEXT_BTN_SELECTORS:
            try:
                btn = ctx.locator(sel).first
                if btn.is_visible(timeout=2_000):
                    btn.click()
                    clicked = True
                    break
            except Exception:
                continue
        if clicked:
            break
    if not clicked:
        log.warning("Could not find next-day arrow (offset %d).", day_offset)
    _wait_for_calendar(cal_frame)

# ---------------------------------------------------------------------------
# Slot time extraction
# ---------------------------------------------------------------------------

def _parse_slot_time(text: str) -> tuple[int, int] | None:
    m = re.search(r"(\d{1,2}):(\d{2})\s*(AM|PM)?", text, re.IGNORECASE)
    if not m:
        return None
    hour, minute = int(m.group(1)), int(m.group(2))
    meridiem = (m.group(3) or "").upper()
    if meridiem == "PM" and hour != 12:
        hour += 12
    elif meridiem == "AM" and hour == 12:
        hour = 0
    return (hour, minute)


def _get_slot_time(slot_element) -> tuple[int, int] | None:
    try:
        val = slot_element.evaluate(
            """el => {
                for (const attr of ['data-time','data-start','data-slot-time',
                                    'data-begin','data-starttime']) {
                    let node = el;
                    while (node) {
                        const v = node.getAttribute && node.getAttribute(attr);
                        if (v) return v;
                        node = node.parentElement;
                    }
                }
                return null;
            }"""
        )
        if val:
            t = _parse_slot_time(val)
            if t is not None:
                return t

        row_label = slot_element.evaluate(
            """el => {
                const row = el.closest('tr') || el.closest('[role="row"]');
                if (!row) return null;
                const cell = row.querySelector(
                    'td:first-child, th:first-child, [role="rowheader"]'
                );
                return cell ? cell.innerText.trim() : null;
            }"""
        )
        if row_label:
            t = _parse_slot_time(row_label)
            if t is not None:
                return t

        anc = slot_element.evaluate(
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
        if anc:
            return _parse_slot_time(anc)
    except Exception as exc:
        log.debug("_get_slot_time error: %s", exc)
    return None

# ---------------------------------------------------------------------------
# Slot scanning
# ---------------------------------------------------------------------------

def _collect_slots_on_page(page, cal_frame, target_date) -> list[str]:
    """Return deduplicated, filtered slot strings for target_date."""
    log.info("Waiting 4 s for page to settle…")
    time.sleep(4)

    # Diagnostic ──────────────────────────────────────────────────────────────
    try:
        body_text = cal_frame.inner_text("body")
        has_reserve = "reserve" in body_text.lower()
        log.info("Diagnostic: 'reserve' in page text = %s", has_reserve)
        if has_reserve:
            idx   = body_text.lower().index("reserve")
            start = max(0, idx - 100)
            end   = min(len(body_text), idx + 160)
            log.info("Diagnostic context: …%s…",
                     body_text[start:end].replace("\n", " | "))
        else:
            log.info("Diagnostic: page text (first 1500): %s",
                     body_text[:1_500].replace("\n", " | "))
    except Exception as exc:
        log.debug("Diagnostic failed: %s", exc)
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
                continue

            h, m = slot_time
            if not (_WINDOW_START_MINS <= h * 60 + m <= _WINDOW_END_MINS):
                continue

            # Exact courttype match via data-courttype on the container div
            is_badminton = slot.evaluate(
                """el => {
                    const c = el.closest('[data-courttype]');
                    if (!c) return true;
                    return c.getAttribute('data-courttype') === 'Badminton';
                }"""
            )
            if not is_badminton:
                continue

            # Deduplicate by start time (multiple courts at same time)
            if (h, m) in seen_times:
                continue
            seen_times.add((h, m))

            found.append(_format_slot(h, m))
        except Exception as exc:
            log.debug("Slot %d error: %s", i, exc)

    log.info("Found %d evening Badminton slot(s) on %s.",
             len(found), target_date.strftime("%Y-%m-%d"))
    return found


def find_available_slots(page) -> dict:
    """Scan all DAYS_AHEAD days. Returns {date_label: [slot_strs]}."""
    today     = datetime.today().date()
    cal_frame = _get_calendar_frame(page)
    results   = {}

    for day_offset in range(DAYS_AHEAD):
        target_date = today + timedelta(days=day_offset)
        log.info("Checking %s…", target_date.strftime("%A %Y-%m-%d"))
        _navigate_to_date(page, cal_frame, day_offset)
        slots = _collect_slots_on_page(page, cal_frame, target_date)
        if slots:
            date_label = target_date.strftime("%A %b ") + str(target_date.day)
            results[date_label] = slots

    return results

# ---------------------------------------------------------------------------
# Slot booking
# ---------------------------------------------------------------------------

def _confirm_booking(page) -> bool:
    """Click the confirm/book button and verify a success indicator."""
    for sel in ('button:has-text("Confirm")', 'button:has-text("Book")',
                'button:has-text("Reserve Now")', 'input[value="Confirm"]',
                '#confirmReservation', '.btn-confirm'):
        try:
            btn = page.locator(sel).first
            if btn.count() > 0 and btn.is_visible(timeout=3_000):
                btn.click()
                page.wait_for_load_state("networkidle")
                if any(kw in page.content().lower()
                       for kw in ("confirmed", "success", "booked", "reservation")):
                    return True
        except Exception:
            pass
    try:
        page.wait_for_selector('text=/confirmed|success|booked/i', timeout=5_000)
        return True
    except PlaywrightTimeout:
        pass
    return False


def _book_specific_slot(date_str: str, time_24: str) -> bool:
    """
    Open a new Playwright session and book the Badminton slot at
    date_str (YYYY-MM-DD) and time_24 (HH:MM, 24-hour).
    """
    from datetime import date as _date
    target     = _date.fromisoformat(date_str)
    day_offset = (target - datetime.today().date()).days

    if day_offset < 0:
        log.warning("Cannot book past date %s", date_str)
        return False

    h, m         = map(int, time_24.split(":"))
    data_time    = _time_to_data_time(h, m)   # e.g. "6:00 PM"
    slot_selector = (
        f'[data-testid="reserveBtn"][data-courttype="Badminton"]'
        f'[data-time="{data_time}"] a.slot-btn'
    )
    log.info("Booking slot: %s at %s (data-time=%s)", date_str, time_24, data_time)

    with sync_playwright() as pw:
        browser, context, page = _new_browser_context(pw)
        try:
            login(page)
            page.context.storage_state(path=COOKIES_FILE)
            navigate_to_bookings(page)
            select_sport(page)

            cal_frame = _get_calendar_frame(page)
            _navigate_to_date(page, cal_frame, day_offset)
            _wait_for_calendar(cal_frame)
            time.sleep(2)

            slot = cal_frame.locator(slot_selector).first
            if not slot.is_visible(timeout=8_000):
                log.warning("Slot %s on %s not visible — may already be taken.",
                            data_time, date_str)
                return False

            slot.click()
            page.wait_for_load_state("networkidle")

            confirmed = _confirm_booking(page)
            if confirmed:
                log.info("SUCCESS: Booked Badminton on %s at %s", date_str, time_24)
            else:
                log.warning("Booking click succeeded but confirmation unclear.")
            return confirmed

        except Exception as exc:
            log.exception("Error during booking: %s", exc)
            return False
        finally:
            context.close()
            browser.close()


def _book_and_notify(date_str: str, time_24: str, chat_id: str | int) -> None:
    """Book the slot (in its own thread) and send the result to the user."""
    success = _book_specific_slot(date_str, time_24)
    _reset_conversation()   # fresh conversation after booking is done

    h, m = map(int, time_24.split(":"))
    slot_label = _format_slot(h, m)
    if success:
        msg = f"✅ Booked! {slot_label} on {date_str}. Check your email for the PIN code."
    else:
        msg = (
            f"❌ Booking failed for {slot_label} on {date_str}. "
            "The slot may have just been taken. Please try booking manually."
        )
    _tg_send(chat_id, msg)

# ---------------------------------------------------------------------------
# Scan session
# ---------------------------------------------------------------------------

def _run_scan_once() -> dict:
    """Run one Playwright session; return {date_label: [slot_strs]}."""
    with sync_playwright() as pw:
        browser, context, page = _new_browser_context(pw)
        try:
            login(page)
            page.context.storage_state(path=COOKIES_FILE)
            log.info("Saved session cookies to %s", COOKIES_FILE)
            navigate_to_bookings(page)
            select_sport(page)
            return find_available_slots(page)
        except PlaywrightTimeout as exc:
            log.error("Timeout during scan: %s", exc)
        except Exception as exc:
            log.exception("Scan error: %s", exc)
        finally:
            context.close()
            browser.close()
    return {}

# ---------------------------------------------------------------------------
# Slot notification via Claude
# ---------------------------------------------------------------------------

def _notify_slots_found(results: dict) -> None:
    """
    Send found slots to Claude; Claude crafts a friendly Telegram message
    which is forwarded to the user.
    """
    lines = ["Available Badminton courts just opened up at Kotofit Jersey City:"]
    for date_label, slots in results.items():
        lines.append(f"\n{date_label}:")
        for slot in slots:
            lines.append(f"  • {slot}")
    lines.append(
        "\nPlease let the user know about these slots in a friendly way "
        "and ask which one they'd like to book."
    )
    prompt = "\n".join(lines)

    log.info("Sending slot summary to Claude…")
    try:
        reply = _call_claude(prompt)
        log.info("Claude formatted notification, sending to Telegram…")
        _tg_send(TELEGRAM_CHAT_ID, reply)
    except Exception as exc:
        log.error("Failed to notify via Claude: %s", exc)

# ---------------------------------------------------------------------------
# Scan thread
# ---------------------------------------------------------------------------

def _scan_loop() -> None:
    log.info("Scan thread started — waiting for Telegram bot to be ready…")
    _bot_ready.wait()   # do not scan until the bot's event loop is running

    attempt = 0
    while True:
        attempt += 1
        log.info("--- Scan #%d at %s ---", attempt, datetime.now().strftime("%H:%M:%S"))
        try:
            results = _run_scan_once()
            if results:
                _notify_slots_found(results)
            else:
                log.info("No evening Badminton slots found.")
        except Exception as exc:
            log.exception("Unhandled error in scan loop: %s", exc)

        log.info("Next scan in %d minutes.", CHECK_INTERVAL // 60)
        time.sleep(CHECK_INTERVAL)

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    global _anthropic_client, _bot_app

    _validate_config()

    _anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    log.info("=== Kotofit Badminton Booking Assistant ===")
    log.info("Location : Kotofit Jersey City – Brunswick St")
    log.info("Window   : 6:00 PM – 9:30 PM (1-hour slots, Badminton only)")
    log.info("Interval : every %d minutes", CHECK_INTERVAL // 60)
    log.info("Model    : claude-sonnet-4-20250514")
    log.info("Headless : %s", HEADLESS)
    log.info("")

    # Start scan thread (daemon so it dies with the main thread)
    scan_thread = threading.Thread(target=_scan_loop, daemon=True, name="scanner")
    scan_thread.start()

    # Build the Telegram Application and register handlers
    _bot_app = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(_post_init)        # sets _bot_loop and signals _bot_ready
        .build()
    )
    _bot_app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, _handle_message)
    )

    log.info("Starting Telegram bot polling…")
    _bot_app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
