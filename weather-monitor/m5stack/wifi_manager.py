# wifi_manager.py
# NVS-backed Wi-Fi manager. No UI code in here.
#
# Storage layers, in priority order on autoconnect:
#   N — esp_wifi's nvs.net80211 cache (free auto-reconnect from prior connect)
#   M — our NVS namespace "mywifi" (primary, JSON blob under key "creds")
#   U — UIFlow firmware "uiflow" namespace (read-only fallback, never written)
#
# Debug prints are prefixed with "[wifi]" so you can grep the REPL.

import network
import json
import time
import sys
import os
import struct

from esp32 import NVS, Partition

DEBUG = True

_NVS_NAMESPACE = "mywifi"
_NVS_KEY = "creds"
_MAX_CREDS = 3
_LEGACY_FILE = "wifi_credentials.json"

_PAGE_SIZE = 4096
_ENTRY_SIZE = 32
_DATA_OFFSET = 64
_NUM_ENTRIES = 126


def _log(*args):
    if DEBUG:
        print("[wifi]", *args)


# --------------------------- M: own NVS store ----------------------------

_nvs = None


def _get_nvs():
    global _nvs
    if _nvs is None:
        _nvs = NVS(_NVS_NAMESPACE)
    return _nvs


def _load_my_list():
    """Layer M: list of {"s": ssid, "p": pwd}, most-recent first. [] if empty."""
    try:
        nvs = _get_nvs()
        buf = bytearray(2048)
        n = nvs.get_blob(_NVS_KEY, buf)
        raw = bytes(buf[:n]).decode("utf-8")
        lst = json.loads(raw)
        if isinstance(lst, list):
            return [e for e in lst
                    if isinstance(e, dict)
                    and isinstance(e.get("s"), str) and e.get("s")]
    except OSError:
        pass  # key absent — first run
    except (ValueError, UnicodeError) as e:
        _log("_load_my_list parse error:", e)
    return []


def _save_my_list(lst):
    try:
        nvs = _get_nvs()
        data = json.dumps(lst).encode("utf-8")
        nvs.set_blob(_NVS_KEY, data)
        nvs.commit()
        _log("M saved, ssids=", [e["s"] for e in lst])
        return True
    except OSError as e:
        _log("_save_my_list failed:", e)
        return False


# --------------------------- U: UIFlow read-only -------------------------
#
# We never write to the UIFlow NVS namespace — it's owned by the UIFlow
# firmware's setup screen. We only borrow its credentials as a one-shot
# fallback on first boot, then migrate them into M on first successful
# connect so subsequent boots don't pay the partition-scan cost.

def _read_uiflow_credentials():
    try:
        return _read_uiflow_inner()
    except Exception as e:
        _log("_read_uiflow_credentials raised:", type(e).__name__, e)
        return []


def _read_uiflow_inner():
    nvs_part = None
    for p in Partition.find(Partition.TYPE_DATA):
        if p.info()[4] == "nvs":
            nvs_part = p
            break
    if nvs_part is None:
        _log("no 'nvs' partition")
        return []

    part_size = nvs_part.info()[3]
    num_pages = part_size // _PAGE_SIZE

    uiflow_ns = None
    ssid_map = {}
    pswd_map = {}

    for page_idx in range(num_pages):
        buf = bytearray(_PAGE_SIZE)
        try:
            nvs_part.readblocks(page_idx, buf)
        except Exception as e:
            _log("readblocks page", page_idx, "raised:", e)
            continue
        page = bytes(buf)

        # 0xFFFFFFFF in the page-state word means the page has never been
        # written to. Skip it.
        page_state = struct.unpack("<I", page[0:4])[0]
        if page_state == 0xFFFFFFFF:
            continue

        entry_idx = 0
        while entry_idx < _NUM_ENTRIES:
            # Entry-state bitmap: 2 bits per entry, WRITTEN = 0b10. NOR-flash
            # 1->0 ratchet means EMPTY=0b11 -> WRITTEN=0b10 -> ERASED=0b00.
            bm_byte = page[32 + entry_idx // 4]
            bm_state = (bm_byte >> ((entry_idx % 4) * 2)) & 0b11
            if bm_state != 0b10:
                entry_idx += 1
                continue

            off = _DATA_OFFSET + entry_idx * _ENTRY_SIZE
            ns = page[off]
            typ = page[off + 1]
            span = page[off + 2] or 1
            if span > _NUM_ENTRIES - entry_idx:
                span = 1
            key = page[off + 8:off + 24].rstrip(b"\x00").decode("utf-8", "replace")

            # Namespace declarations live in ns=0 with type u8 (0x01); the
            # u8 value is the assigned namespace index for that name.
            if ns == 0 and typ == 0x01 and key == "uiflow":
                uiflow_ns = page[off + 24]
                _log("uiflow ns_idx =", uiflow_ns)
                entry_idx += span
                continue

            if uiflow_ns is None or ns != uiflow_ns or typ != 0x21:
                entry_idx += span
                continue

            # String entry: u16 length at off+24, payload starts in the next
            # 32-byte slot at off+32.
            try:
                length = struct.unpack("<H", page[off + 24:off + 26])[0]
                if length > _PAGE_SIZE:
                    entry_idx += span
                    continue
                payload = page[off + 32:off + 32 + length]
                val = payload.rstrip(b"\x00").decode("utf-8", "replace")
            except Exception:
                entry_idx += span
                continue

            if key.startswith("ssid") and len(key) > 4:
                try:
                    ssid_map[int(key[4:])] = val
                except ValueError:
                    pass
            elif key.startswith("pswd") and len(key) > 4:
                try:
                    pswd_map[int(key[4:])] = val
                except ValueError:
                    pass

            entry_idx += span

    out = []
    for n in sorted(ssid_map.keys()):
        ssid = ssid_map[n]
        if not ssid:
            continue
        out.append((ssid, pswd_map.get(n, "")))
    if out:
        _log("U creds:", [s for s, _ in out])
    return out


# --------------------------- legacy file migration ------------------------

_migration_done = False


def _maybe_migrate():
    global _migration_done
    if _migration_done:
        return
    _migration_done = True
    try:
        try:
            os.stat(_LEGACY_FILE)
        except OSError:
            return
        try:
            with open(_LEGACY_FILE, "r") as f:
                data = json.load(f)
        except (OSError, ValueError) as e:
            _log("legacy file invalid:", e)
            try:
                os.remove(_LEGACY_FILE)
            except OSError:
                pass
            return
        if not isinstance(data, dict) or not data:
            try:
                os.remove(_LEGACY_FILE)
            except OSError:
                pass
            return

        existing = _load_my_list()
        existing_ssids = {e.get("s") for e in existing}
        new_list = list(existing)
        for ssid, pwd in data.items():
            if (isinstance(ssid, str) and ssid
                    and isinstance(pwd, str)
                    and ssid not in existing_ssids):
                new_list.append({"s": ssid, "p": pwd})
        new_list = new_list[:_MAX_CREDS]
        if _save_my_list(new_list):
            try:
                os.remove(_LEGACY_FILE)
                _log("migrated", len(data), "creds, removed", _LEGACY_FILE)
            except OSError as e:
                _log("could not remove legacy file:", e)
    except Exception as e:
        _log("_maybe_migrate failed:", type(e).__name__, e)


_maybe_migrate()


# ------------------------- public credential API --------------------------

def load_credentials():
    """All known creds as dict. M overrides U on conflict."""
    out = {}
    for ssid, pwd in _read_uiflow_credentials():
        out[ssid] = pwd
    for entry in _load_my_list():
        out[entry["s"]] = entry.get("p", "")
    _log("load_credentials ->", list(out.keys()))
    return out


def save_credentials(creds):
    """Replace M with the given dict. Preserves order of pre-existing entries."""
    if not isinstance(creds, dict):
        return False
    existing = _load_my_list()
    new_list = []
    seen = set()
    for entry in existing:
        ssid = entry.get("s")
        if ssid in creds and ssid not in seen:
            pwd = creds[ssid]
            new_list.append({"s": ssid, "p": pwd if isinstance(pwd, str) else ""})
            seen.add(ssid)
    for ssid, pwd in creds.items():
        if not isinstance(ssid, str) or not ssid or ssid in seen:
            continue
        new_list.append({"s": ssid, "p": pwd if isinstance(pwd, str) else ""})
        seen.add(ssid)
    new_list = new_list[:_MAX_CREDS]
    return _save_my_list(new_list)


def add_credential(ssid, password):
    """Insert SSID at the front of M (most-recent-first). Caps at _MAX_CREDS."""
    if not ssid:
        _log("add_credential: empty ssid")
        return False
    pwd = password or ""
    current = [e for e in _load_my_list() if e.get("s") != ssid]
    current.insert(0, {"s": ssid, "p": pwd})
    current = current[:_MAX_CREDS]
    return _save_my_list(current)


def forget_credential(ssid):
    """Remove SSID from M. UIFlow creds (Layer U) are never touched — manage
    those via the UIFlow IDE / M5Burner."""
    if not ssid:
        return False
    current = _load_my_list()
    new = [e for e in current if e.get("s") != ssid]
    if len(new) == len(current):
        _log("forget_credential: not in M:", ssid)
        return False
    return _save_my_list(new)


def known_ssids():
    """SSIDs from M ∪ U, M first."""
    seen = set()
    out = []
    for entry in _load_my_list():
        s = entry.get("s")
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    for ssid, _ in _read_uiflow_credentials():
        if ssid and ssid not in seen:
            seen.add(ssid)
            out.append(ssid)
    return out


# --------------------------------- scan -----------------------------------

def _decode_ssid(ssid_bytes):
    try:
        return ssid_bytes.decode("utf-8")
    except (UnicodeError, AttributeError):
        return None


def _do_scan(wlan):
    t0 = time.ticks_ms()
    try:
        raw = wlan.scan()
    except OSError as e:
        _log("  scan OSError:", e)
        return []
    except Exception as e:
        _log("  scan unexpected:", type(e).__name__, e)
        try:
            sys.print_exception(e)
        except Exception:
            pass
        return []
    _log("  scan got", len(raw), "in", time.ticks_diff(time.ticks_ms(), t0), "ms")
    return raw


def scan_networks(wlan):
    """[(ssid, rssi, secure)] sorted by RSSI desc.

    Never disconnects an active connection. ESP-IDF supports in-place scan
    while STA is associated; the only reason the previous version did a
    pre-scan disconnect was to clear a stuck failed-connect state, and that
    state can only exist while we're NOT connected. So we gate the recovery
    disconnect on `not isconnected()`.
    """
    _log("scan_networks: start")

    if not wlan.active():
        try:
            wlan.active(True)
            time.sleep_ms(500)
        except Exception as e:
            _log("  active(True) raised:", e)
            return []

    raw = _do_scan(wlan)

    if len(raw) == 0 and not wlan.isconnected():
        _log("  empty + disconnected, clearing driver state")
        try:
            wlan.disconnect()
            time.sleep_ms(300)
        except Exception as e:
            _log("  disconnect raised:", e)
        raw = _do_scan(wlan)

    if len(raw) == 0 and not wlan.isconnected():
        _log("  still empty, cycling radio")
        try:
            wlan.active(False)
            time.sleep_ms(500)
            wlan.active(True)
            time.sleep_ms(800)
        except Exception as e:
            _log("  radio cycle raised:", e)
        raw = _do_scan(wlan)

    if len(raw) == 0:
        _log("  giving up — zero entries")
        return []

    best = {}
    for item in raw:
        ssid = _decode_ssid(item[0])
        if not ssid:
            continue
        rssi = item[3]
        is_secure = item[4] != 0
        if ssid not in best or rssi > best[ssid][0]:
            best[ssid] = (rssi, is_secure)

    out = [(s, r, sec) for s, (r, sec) in best.items()]
    out.sort(key=lambda t: t[1], reverse=True)
    _log("scan_networks ->", len(out), "unique SSIDs")
    return out


# ------------------------------- connect ----------------------------------

def current_ssid(wlan):
    if not wlan.isconnected():
        return None
    try:
        return wlan.config("essid")
    except Exception as e:
        _log("current_ssid: config('essid') raised:", e)
        return None


def connect_to(wlan, ssid, password="", timeout_ms=15000):
    if not ssid:
        return False
    _log("connect_to({!r}, pwd_len={}, timeout={}ms)".format(
        ssid, len(password or ""), timeout_ms))

    if not wlan.active():
        wlan.active(True)
        time.sleep_ms(300)

    if wlan.isconnected() and current_ssid(wlan) == ssid:
        _log("  already on", ssid)
        return True

    try:
        if wlan.isconnected():
            _log("  releasing previous association")
            wlan.disconnect()
            time.sleep_ms(200)
    except Exception as e:
        _log("  disconnect raised:", e)

    try:
        if password:
            wlan.connect(ssid, password)
        else:
            wlan.connect(ssid)
    except Exception as e:
        _log("  wlan.connect() raised:", e)
        return False

    start = time.ticks_ms()
    last_status = None
    while time.ticks_diff(time.ticks_ms(), start) < timeout_ms:
        if wlan.isconnected():
            _log("  connected in", time.ticks_diff(time.ticks_ms(), start), "ms")
            try:
                _log("  ifconfig:", wlan.ifconfig())
            except Exception:
                pass
            return True
        try:
            st = wlan.status()
            if st != last_status:
                _log("  status:", st)
                last_status = st
        except Exception:
            pass
        time.sleep_ms(250)
    _log("  timeout, status:", last_status)
    return False


# ----------------------------- autoconnect --------------------------------

def autoconnect(wlan, timeout_ms=10000):
    """Boot autoconnect. Tries Layer N (free), then M, then U as fallback."""
    if not wlan.active():
        wlan.active(True)

    if wlan.isconnected():
        s = current_ssid(wlan) or "connected"
        _log("autoconnect: already on", s)
        return s

    # Layer N: ESP-IDF may auto-reconnect from nvs.net80211 once the radio
    # comes up. Give it 3 s before falling through.
    deadline = time.ticks_add(time.ticks_ms(), 3000)
    while time.ticks_diff(deadline, time.ticks_ms()) > 0:
        if wlan.isconnected():
            s = current_ssid(wlan) or "connected"
            _log("autoconnect: ESP-IDF auto-reconnected to", s)
            return s
        time.sleep_ms(200)

    # Layer M: our store, most-recent-first.
    for entry in _load_my_list():
        ssid = entry.get("s", "")
        pwd = entry.get("p", "")
        if not ssid:
            continue
        _log("autoconnect: trying M:", ssid)
        if connect_to(wlan, ssid, pwd, timeout_ms):
            add_credential(ssid, pwd)  # bump to front
            return ssid

    # Layer U: UIFlow fallback. On success, copy into M so next boot is fast.
    for ssid, pwd in _read_uiflow_credentials():
        _log("autoconnect: trying U:", ssid)
        if connect_to(wlan, ssid, pwd, timeout_ms):
            add_credential(ssid, pwd)
            _log("autoconnect: U->M migrated", ssid)
            return ssid

    _log("autoconnect: exhausted all sources")
    return None
