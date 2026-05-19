# PIR presence detection + rate-limit state for proactive announcements.
#
# Hardware: M5Stack PIR Unit on Port B (data line on GPIO 36, input-only).
# Active-high digital line — no debouncing needed beyond the per-tick poll
# already done by the asyncio task in main.py.

from unit import PIRUnit
import time

PIR_PORT = (36, 26)                # Port B on Core2 — (signal, second). PIRUnit takes the Grove pin tuple.
COOLDOWN_MS = 60 * 60 * 1000       # 1 hour between presence-triggered announcements

# Local-time window in which a "morning_check" announcement can fire.
# (start_hour, start_min, end_hour, end_min) — half-open: end excluded.
MORNING_WINDOW = (6, 30, 9, 30)

# Flip to True during development to get rising-edge motion logs + cooldown
# verdicts. Leave False in production — is_motion() is polled at 2 Hz and even
# rising-edge logs add up fast over a day.
DEBUG = False

_pir = None                        # PIRUnit instance (replaces raw Pin)
_last_announcement_ms = None       # ticks_ms() of last successful announcement
_morning_check_done_date = None    # "YYYY-MM-DD" the morning check last fired
_motion_pending = False            # latch set in IRQ_ACTIVE callback, consumed by is_motion()


def _log(level, *args):
    """Single log helper.
      level="info"  → always prints.
      level="debug" → only prints when the module-level DEBUG flag is True.
    Anything else falls through as info so a typo never silently swallows a
    message."""
    if level == "debug":
        if not DEBUG:
            return
        else:
            print("[presence][DEBUG]", *args)
    elif level == "info":
        print("[presence][INFO]", *args)
    else:
        raise AttributeError("invalid log level: {}".format(level))


def _on_active(pir):
    """IRQ_ACTIVE callback. Must be ultra-cheap: PIRUnit re-fires this every
    few tens of ms while the PIR line is high (the M5 driver polls on an
    internal timer, it's not a true edge IRQ), and MicroPython's soft-IRQ
    schedule queue only has 8 slots. A print() in here is enough to starve
    the m5ui render timer and crash it with `RuntimeError: schedule queue
    full`. So: flip the flag, return. All logging happens in is_motion()."""
    global _motion_pending
    _motion_pending = True


def init():
    """Set up PIRUnit on Port B and arm the active IRQ. Safe to call once at boot.

    Only IRQ_ACTIVE is registered. IRQ_NEGATIVE would double the callback
    traffic for no functional benefit (presence_task doesn't act on room-clear
    events) and would worsen the schedule-queue pressure described in
    _on_active's docstring."""
    global _pir
    _pir = PIRUnit(PIR_PORT)
    _pir.set_callback(_on_active, _pir.IRQ_ACTIVE)
    _pir.enable_irq()
    _log("info", "PIR initialised on Port B (DEBUG=" + str(DEBUG) + ")")


def is_motion():
    """Return True if an IRQ_ACTIVE event has fired since the last call, then
    clear the latch. Edge-triggered semantics: one wave → exactly one True
    read regardless of how long the PIR holds its line high.

    The debug log lives here (not in the IRQ callback) because this runs in
    presence_task's normal asyncio context, at most once per 500 ms — safe to
    print() without overflowing the soft-IRQ schedule queue."""
    global _motion_pending
    if _motion_pending:
        _motion_pending = False
        _log("debug", "motion detected (latch consumed)")
        return True
    return False


def _date_str(now_lt):
    return "{:04d}-{:02d}-{:02d}".format(now_lt[0], now_lt[1], now_lt[2])


def _in_morning_window(now_lt):
    sh, sm, eh, em = MORNING_WINDOW
    minutes = now_lt[3] * 60 + now_lt[4]
    return (sh * 60 + sm) <= minutes < (eh * 60 + em)


def should_announce(now_lt):
    """Decide whether the device should ask the server for an announcement
    *right now*. Returns the context string ("morning_check" or "presence")
    or None if we're still in cooldown / outside any trigger window.

    `now_lt` is a time.localtime() tuple.

    Rules:
      - If we're in the morning window and the morning check hasn't fired
        today yet → "morning_check" (takes priority over the cooldown).
      - Else if the 1-hour cooldown since the last announcement has elapsed
        → "presence".
      - Else → None.
    """
    today = _date_str(now_lt)
    if _in_morning_window(now_lt) and _morning_check_done_date != today:
        _log("debug", "should_announce -> morning_check")
        return "morning_check"

    if _last_announcement_ms is None:
        _log("debug", "should_announce -> presence (first ever)")
        return "presence"
    elapsed = time.ticks_diff(time.ticks_ms(), _last_announcement_ms)
    if elapsed >= COOLDOWN_MS:
        _log("debug", "should_announce -> presence (cooldown elapsed, {} ms)".format(elapsed))
        return "presence"
    remaining_s = (COOLDOWN_MS - elapsed) // 1000
    _log("debug", "should_announce -> None (cooldown, {}s remaining)".format(remaining_s))
    return None


def mark_announced(context, now_lt):
    """Update rate-limit state. Call only after a successful HTTP response so
    transient errors don't silence the device for an hour."""
    global _last_announcement_ms, _morning_check_done_date
    _last_announcement_ms = time.ticks_ms()
    if context == "morning_check":
        _morning_check_done_date = _date_str(now_lt)
    _log("info", "marked announced ({}) at {}".format(context, _date_str(now_lt)))
