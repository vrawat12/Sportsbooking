# Sportsbooking
Book Sports Appointments

---

## Kotofit Badminton Auto-Booker

Automatically polls the [Kotofit](https://www.kotofit.com) CourtReserve booking
page and books a badminton court at **Jersey City – Brunswick St** whenever a
slot opens up in your preferred time windows.

### How it works

1. Launches a Chromium browser (via Playwright)
2. Logs in to your CourtReserve / Kotofit account
3. Scans available slots across the next N days
4. Books the first slot that falls in your preferred morning / evening windows
5. Repeats every 30 minutes until a booking is confirmed
6. Exits after a successful booking — check your email for the entry PIN code

---

### Setup

**1. Install Python dependencies**

```bash
pip install -r requirements.txt
playwright install chromium
```

**2. Configure credentials**

```bash
cp .env.example .env
```

Edit `.env` and fill in:

| Variable | Description |
|---|---|
| `KOTOFIT_EMAIL` | Your Kotofit / CourtReserve login email |
| `KOTOFIT_PASSWORD` | Your account password |
| `COURTRESERVE_ORG_URL` | The booking URL — open kotofit.com, click **Book → Jersey City**, and copy the full `app.courtreserve.com/…` URL from your browser |
| `PREFERRED_TIME_WINDOWS` | Comma-separated pairs: `6,12,17,22` = 6–12 AM and 5–10 PM |
| `DAYS_AHEAD` | How many days forward to scan (max 14 free / 21 paid) |
| `CHECK_INTERVAL_SECONDS` | Seconds between checks (default 1800 = 30 min) |
| `HEADLESS` | `false` to see the browser window; `true` to run silently |

**3. Run**

```bash
python book_badminton.py
```

The script logs every action. When a booking succeeds you will see:

```
SUCCESS: Booked badminton court on 2026-02-26 at 08:00
Slot booked! Exiting – check your email for the PIN code.
```

---

### Notes

- **First run**: set `HEADLESS=false` so you can watch the browser and verify the
  selectors work against the live CourtReserve layout for Kotofit.
- **CourtReserve layout changes**: if CourtReserve updates their UI, the slot
  selectors in `book_badminton.py` may need to be updated to match the new HTML.
- **Cancellation**: Kotofit allows free cancellations up to 48 hours before the
  session; cancellations 12–48 h before incur a $5 fee; no refund within 12 h.
- Keep your `.env` file out of version control — it is listed in `.gitignore`.
