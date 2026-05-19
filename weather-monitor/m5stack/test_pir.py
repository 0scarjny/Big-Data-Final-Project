# Standalone PIR sanity-check.
#
# Run on the M5Core2 REPL with:
#     import test_pir
#     test_pir.run()
#
# Or copy/paste the body into the REPL. Doesn't import anything from the
# rest of the app — keep it dependency-free so a flaky sensor can be ruled
# in or out without main.py interfering.
#
# What it prints:
#   - One INIT line with the pin and a 30 s warm-up countdown (PIR units
#     output garbage for the first ~30 s after power-on while the pyroelectric
#     element stabilises — false positives in that window are NORMAL).
#   - One line on every state TRANSITION: "MOTION" on rising edge, "clear"
#     on falling edge, with a millisecond timestamp and the duration of
#     the previous state.
#   - One HEARTBEAT line every 5 s while idle so you can confirm the script
#     is still running and see the current pin level + how long it's held.
#   - A running total of detections so you can quantify false-positive rate.
#
# Stop with Ctrl+C.

from machine import Pin
import time

# Default wiring: M5Stack PIR Unit on Port B of an M5Core2.
#   - Red    = 5V
#   - Black  = GND
#   - Yellow = signal = GPIO 36  (input-only, no internal pull)
# If you moved the unit to a different port / hat, change PIN here.
PIN = 36

# How often to print the "I'm still here, current state is X" line while
# nothing transitions.
HEARTBEAT_S = 5

# How long to wait before believing any reading. Datasheet calls for ~30 s;
# in practice 10–15 s is usually enough. Lower to 0 if you want raw output
# immediately (you'll see noise).
WARMUP_S = 30


def run(pin=PIN, warmup_s=WARMUP_S, heartbeat_s=HEARTBEAT_S):
    p = Pin(pin, Pin.IN)
    print("=" * 60)
    print("[pir-test] Pin GPIO{} configured as input.".format(pin))
    print("[pir-test] Initial level: {}".format(p.value()))
    print("[pir-test] Warm-up: {} s (false positives in this window are normal)".format(warmup_s))
    print("=" * 60)

    # Countdown so you can see the script is alive even before transitions.
    for remaining in range(warmup_s, 0, -1):
        if remaining % 5 == 0 or remaining <= 3:
            print("[pir-test] warming up... {}s  (raw pin = {})".format(remaining, p.value()))
        time.sleep(1)
    print("[pir-test] warm-up done. Watching for motion.\n")

    last_value = p.value()
    last_change_ms = time.ticks_ms()
    last_heartbeat_ms = time.ticks_ms()
    rising_count = 0
    falling_count = 0

    try:
        while True:
            now_ms = time.ticks_ms()
            v = p.value()

            if v != last_value:
                dwell_ms = time.ticks_diff(now_ms, last_change_ms)
                if v == 1:
                    rising_count += 1
                    print("[pir-test] {:>10} ms |  MOTION  (was LOW for {} ms) | total motions = {}"
                          .format(now_ms, dwell_ms, rising_count))
                else:
                    falling_count += 1
                    print("[pir-test] {:>10} ms |  clear   (was HIGH for {} ms) | total clears  = {}"
                          .format(now_ms, dwell_ms, falling_count))
                last_value = v
                last_change_ms = now_ms
                last_heartbeat_ms = now_ms  # reset heartbeat so we don't double-log

            elif time.ticks_diff(now_ms, last_heartbeat_ms) >= heartbeat_s * 1000:
                state = "HIGH (motion)" if v == 1 else "LOW (idle)"
                held_s = time.ticks_diff(now_ms, last_change_ms) // 1000
                print("[pir-test] heartbeat — pin still {} for {}s   (rising={}, falling={})"
                      .format(state, held_s, rising_count, falling_count))
                last_heartbeat_ms = now_ms

            # Polling at 50 ms catches even short pulses without melting the CPU.
            time.sleep_ms(50)
    except KeyboardInterrupt:
        print("\n[pir-test] stopped. Final tally: {} rising edges, {} falling edges."
              .format(rising_count, falling_count))


if __name__ == "__main__":
    run()
