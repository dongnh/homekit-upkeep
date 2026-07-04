"""HomeKit accessory definitions — one Contact Sensor + mark-done Switch +
countdown Battery per task.

Polarity follows light-programmer-homekit's convention (the standard one for
contact sensors): a task in good standing reads **Closed** (contact detected)
and an overdue one **Opened** — the alert state, so Apple Home's fixed
"<accessory name> Opened" notification means "time to do this".

The Battery is the countdown display: HomeKit has no free-text/number tile,
so each task carries a virtual battery that reads 100% right after mark-done
and drains linearly to 0% at the due moment. Below LOW_BATTERY_PCT the
low-battery badge appears on the tile — an early "coming due soon" warning
before the sensor opens.
"""
import asyncio
import logging
import zlib
from datetime import datetime

from pyhap.accessory import Accessory, Bridge
from pyhap.accessory_driver import AccessoryDriver
from pyhap.const import CATEGORY_SENSOR

from . import __version__, schedule, store

MANUFACTURER = "dongnh"
MODEL = "HomeUpkeepBridge"
DEFAULT_TICK = 30  # seconds between due-ness re-evaluations

# ContactSensorState: 1 = "not detected" → Apple Home renders "Opened";
# 0 = "detected" → "Closed". On schedule = closed, due = opened.
_OPEN = 1
_CLOSED = 0

# Battery "charge" (share of the cycle still remaining) at or below which
# StatusLowBattery flips on and Apple Home badges the tile. 20% of a weekly
# task ≈ 1.4 days' notice; of a monthly one ≈ 5.6 days.
LOW_BATTERY_PCT = 20
_NOT_CHARGEABLE = 2  # ChargingState — a virtual battery never charges

# HomeKit Accessory IDs (AIDs). pyhap assigns child AIDs by INSERTION ORDER
# and never persists them, so a changed task set would re-target Apple Home
# notifications/automations. Instead each task gets a DETERMINISTIC aid
# derived from its stable task id (same scheme as light-programmer-homekit).
# 1 = bridge (STANDALONE_AID); 7 is unusable in pyhap.
_BRIDGE_AID = 1
_AID_MIN = 8          # skips reserved 1 and the 2..7 band
_AID_MAX = 2 ** 31 - 1
_AID_SKIP = {7}


def _set_info(accessory: Accessory, serial: str, model: str) -> None:
    """HomeKit requires non-empty Manufacturer/Model/Serial/FirmwareRevision —
    Apple Home drops the session and shows 'No Response' otherwise."""
    info = accessory.get_service("AccessoryInformation")
    info.configure_char("Manufacturer", value=MANUFACTURER)
    info.configure_char("Model", value=model)
    info.configure_char("SerialNumber", value=serial)
    info.configure_char("FirmwareRevision", value=__version__)


def _task_aid(task_id: str) -> int:
    """Map a task id to a stable aid in [_AID_MIN, _AID_MAX] via crc32.
    Deterministic across restarts and independent of insertion order."""
    span = _AID_MAX - _AID_MIN + 1
    return _AID_MIN + (zlib.crc32(task_id.encode("utf-8")) % span)


def _assign_aids(task_ids) -> dict:
    """Return {task_id: aid} deterministically; collisions resolved by linear
    probing (+1, wrapping within the range and skipping reserved aids) since
    pyhap raises on a duplicate aid. Iterate ids in sorted order so probing is
    itself stable."""
    used = {_BRIDGE_AID} | _AID_SKIP
    out: dict = {}
    span = _AID_MAX - _AID_MIN + 1
    for tid in sorted(task_ids):
        aid = _task_aid(tid)
        while aid in used:
            aid = _AID_MIN + ((aid - _AID_MIN + 1) % span)
        used.add(aid)
        out[tid] = aid
    return out


class UpkeepTask(Accessory):
    """One maintenance task: a Contact Sensor carrying the due state (drives
    notifications), a Switch that marks the task done — flip it on when the
    chore is finished; the cycle restarts and the switch snaps back off —
    and a virtual Battery counting down the cycle (100% = just done,
    0% = due, low-battery badge = coming due soon)."""
    category = CATEGORY_SENSOR

    def __init__(self, driver, display_name: str, task_id: str, on_done,
                 due: bool = False, level: int = 100, aid: int = None):
        super().__init__(driver, display_name, aid=aid)
        _set_info(self, serial=f"upkeep-{task_id}", model="HomeUpkeepTask")
        self.task_id = task_id
        self._on_done = on_done
        serv = self.add_preload_service("ContactSensor")
        self.char_contact = serv.configure_char(
            "ContactSensorState", value=_OPEN if due else _CLOSED,
        )
        switch = self.add_preload_service("Switch")
        self.char_on = switch.configure_char(
            "On", value=False, setter_callback=self._switch_set,
        )
        batt = self.add_preload_service("BatteryService")
        self.char_level = batt.configure_char("BatteryLevel", value=level)
        self.char_low = batt.configure_char(
            "StatusLowBattery", value=1 if level <= LOW_BATTERY_PCT else 0,
        )
        batt.configure_char("ChargingState", value=_NOT_CHARGEABLE)

    def _switch_set(self, value) -> None:
        if not value:
            return  # the snap-back / a manual off — nothing to record
        logging.info("task '%s' marked done from HomeKit", self.task_id)
        self._on_done(self.task_id)
        # Momentary behaviour: acknowledge the tap, then snap back off.
        # (set_value does not re-enter this callback — only client writes do.)
        self.driver.loop.call_later(1.0, self.char_on.set_value, False)

    def set_status(self, due: bool, level: int) -> None:
        # set_value only notifies Apple Home when the value actually changes,
        # so re-applying the same state every tick is cheap and spam-free.
        self.char_contact.set_value(_OPEN if due else _CLOSED)
        self.char_level.set_value(level)
        self.char_low.set_value(1 if level <= LOW_BATTERY_PCT else 0)


class UpkeepBridge(Bridge):
    """Bridge that re-evaluates every task's due-ness on a fixed tick and
    mirrors it onto the Contact Sensors."""

    def __init__(self, driver, name: str, tasks: list, state_path: str,
                 tick: int = DEFAULT_TICK):
        super().__init__(driver, name)
        self.tasks = tasks  # validated config entries (id/name/interval_days/time)
        self.state_path = state_path
        self.tick = max(5, int(tick))
        self.task_accessories: dict = {}  # task id -> UpkeepTask

    def _status_map(self, now: datetime) -> dict:
        """{task id: (due, battery level)} for every task."""
        last = store.load(self.state_path)["last_done"]
        out = {}
        for t in self.tasks:
            ld = schedule.parse_last_done(last.get(t["id"]))
            if ld is None:
                # Missing/corrupt timestamp — read as "just done" rather than
                # opening the sensor on bad data; the next mark-done repairs it.
                out[t["id"]] = (False, 100)
                continue
            d_at = schedule.due_at(ld, t["interval_days"], t["time"])
            out[t["id"]] = (now >= d_at, schedule.battery_pct(ld, d_at, now))
        return out

    def refresh(self) -> None:
        """Recompute due-ness and push it onto the sensors. Runs in the HAP
        event loop (tick, switch callback, or call_soon_threadsafe from HTTP)."""
        status = self._status_map(datetime.now())
        for task_id, acc in self.task_accessories.items():
            acc.set_status(*status[task_id])

    def mark_done(self, task_id: str) -> None:
        store.mark_done(self.state_path, task_id,
                        datetime.now().isoformat(timespec="seconds"))
        self.refresh()

    def snapshot(self) -> list:
        """Current task list for GET /tasks. Thread-safe: pure reads over the
        immutable config list and the lock-guarded store."""
        now = datetime.now()
        last = store.load(self.state_path)["last_done"]
        out = []
        for t in self.tasks:
            ld = schedule.parse_last_done(last.get(t["id"]))
            d_at = schedule.due_at(ld, t["interval_days"], t["time"]) if ld else None
            out.append({
                "id": t["id"],
                "name": t["name"],
                "interval_days": t["interval_days"],
                "time": t["time"].strftime("%H:%M"),
                "last_done": ld.isoformat(timespec="seconds") if ld else None,
                "due_at": d_at.isoformat(timespec="seconds") if d_at else None,
                "due": bool(d_at is not None and now >= d_at),
                "battery": schedule.battery_pct(ld, d_at, now) if d_at else 100,
            })
        return out

    async def run(self) -> None:
        # The driver schedules this coroutine as a background task.
        while True:
            try:
                self.refresh()
            except Exception as e:  # noqa: BLE001 - the tick must never die
                logging.warning("refresh failed: %s", e)
            await asyncio.sleep(self.tick)


def build_bridge(driver: AccessoryDriver, name: str, tasks: list,
                 state_path: str, tick: int = DEFAULT_TICK) -> UpkeepBridge:
    """Construct the bridge with one task accessory per config entry, seeded
    with the current due-ness so paired hubs see correct state immediately."""
    bridge = UpkeepBridge(driver, name, tasks, state_path, tick)
    _set_info(bridge, serial="upkeep-bridge", model=MODEL)
    aids = _assign_aids([t["id"] for t in tasks])
    status = bridge._status_map(datetime.now())
    for t in tasks:
        due, level = status[t["id"]]
        acc = UpkeepTask(driver, t["name"], t["id"], on_done=bridge.mark_done,
                         due=due, level=level, aid=aids[t["id"]])
        bridge.task_accessories[t["id"]] = acc
        bridge.add_accessory(acc)
    return bridge
