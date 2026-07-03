"""Entry point — config load/validation + HAP driver wiring."""
import argparse
import hashlib
import json
import logging
import signal
from datetime import datetime

from pyhap.accessory_driver import AccessoryDriver

from . import schedule, store
from .accessory import DEFAULT_TICK, build_bridge
from .http_api import start_in_thread


def _stable_mac(seed: str) -> str:
    """Derive a deterministic locally-administered MAC from `seed` so that
    factory-resetting accessory.state does not generate a new HomeKit device id
    (which leaves stale mDNS records behind and confuses Apple Home)."""
    h = hashlib.sha256(seed.encode()).digest()
    octets = list(h[:6])
    octets[0] = (octets[0] & 0xFC) | 0x02  # locally administered, unicast
    return ":".join(f"{b:02X}" for b in octets)


def _load_tasks(cfg: dict) -> list:
    """Validate config `tasks` into [{id, name, interval_days, time}, …].
    Config has no formal schema (light-programmer convention), but the few
    fields that exist are checked hard — a typo'd task should fail loudly at
    startup, not silently never remind."""
    tasks, seen = [], set()
    for i, entry in enumerate(cfg.get("tasks") or []):
        tid = entry.get("id")
        if not tid or not isinstance(tid, str):
            raise SystemExit(f"config: tasks[{i}] is missing 'id'")
        if tid in seen:
            raise SystemExit(f"config: duplicate task id '{tid}'")
        seen.add(tid)
        try:
            interval = float(entry["interval_days"])
        except (KeyError, TypeError, ValueError):
            raise SystemExit(
                f"config: task '{tid}' needs a numeric 'interval_days'")
        if interval <= 0:
            raise SystemExit(f"config: task '{tid}' interval_days must be > 0")
        try:
            notify = schedule.parse_hhmm(entry.get("time", schedule.DEFAULT_NOTIFY))
        except (AttributeError, ValueError):
            raise SystemExit(f"config: task '{tid}' has a bad 'time' (want HH:MM)")
        tasks.append({"id": tid, "name": entry.get("name") or tid,
                      "interval_days": interval, "time": notify})
    if not tasks:
        raise SystemExit("config: no tasks defined")
    return tasks


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--config", required=True, help="Path to config JSON")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()

    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s | %(levelname)s | %(message)s",
        datefmt="%H:%M:%S",
    )

    with open(args.config) as f:
        cfg = json.load(f)

    tasks = _load_tasks(cfg)
    state_path = cfg.get("state_path", "./upkeep_state.json")
    # First run (or newly added tasks): seed last_done = now so a fresh install
    # doesn't open every sensor at once. POST /due forces one due immediately.
    now_iso = datetime.now().isoformat(timespec="seconds")
    store.seed_missing(state_path, {t["id"]: now_iso for t in tasks})

    bridge_name = cfg.get("bridge_name", "Home Upkeep")
    logging.info("Building bridge '%s' with %d task(s)", bridge_name, len(tasks))

    mac = cfg.get("mac") or _stable_mac(f"homekit-upkeep:{bridge_name}")
    logging.info("Using stable MAC %s for bridge '%s'", mac, bridge_name)
    # HAP-python regenerates a RANDOM setup code on every startup unless one is
    # provided (it is not persisted in accessory.state). Pin it via config
    # `pincode` ("DDD-DD-DDD") so the code is stable across restarts.
    pincode = cfg.get("pincode")
    if pincode:
        logging.info("Using fixed HomeKit setup code from config")
    driver = AccessoryDriver(
        port=cfg.get("port", 51830),
        persist_file=cfg.get("accessory_state", "./accessory.state"),
        address=cfg.get("address"),
        mac=mac,
        pincode=pincode.encode() if pincode else None,
    )
    bridge = build_bridge(driver, bridge_name, tasks, state_path,
                          tick=cfg.get("tick_seconds", DEFAULT_TICK))
    driver.add_accessory(accessory=bridge)

    # HTTP POSTs mutate the store from the server thread; the sensors are only
    # touched in the HAP loop, so hop threads for the immediate refresh.
    start_in_thread(
        state_path, tasks, bridge.snapshot,
        cfg.get("http_host", "127.0.0.1"), int(cfg.get("http_port", 7872)),
        on_change=lambda: driver.loop.call_soon_threadsafe(bridge.refresh),
        api_key=cfg.get("http_api_key"),
    )

    signal.signal(signal.SIGTERM, lambda *_: driver.stop())
    driver.start()


if __name__ == "__main__":
    main()
