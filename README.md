# Home Upkeep for HomeKit

Household maintenance on schedule, from the Home app — water the plants, clean the filters, descale the machine.

## What it is

One accessory per recurring chore, each carrying a Contact Sensor, a Switch and a countdown Battery — the entire interface. **Closed** means the task is on schedule; **Open** means it's due (the standard convention: Open is the alert state). Turn on notifications and Apple Home tells you the moment a chore comes due — "Water Office ZZ Plant Opened". Done it? Flip the task's switch: the sensor closes, the countdown restarts, the switch snaps back off.

The battery is the countdown: HomeKit has no free-text tile, so each task reads 100% right after you mark it done and drains linearly to 0% at the due moment. Below 20% the low-battery badge appears — a "coming due soon" warning before the sensor ever opens.

<img width="853" height="778" alt="image" src="https://github.com/user-attachments/assets/ff9ef072-753e-47fd-9cc6-484e51de12a3" />

## How it works

The daemon *is* the schedule — no external service to poll. A config JSON lists the tasks (`id`, `name`, `interval_days`, earliest notify `time`); an atomically-written state file remembers when each was last done; a tick re-evaluates due-ness and mirrors it onto the sensors. Reminders fire at the task's notify time on the due day, not at whatever hour you happened to finish last time — and they stay up until you mark the task done.

Each task's HomeKit id is derived from its stable `id`, so notifications and automations stay bound to the right chore across restarts. The accessory set is fixed at pairing time — add a task to the config and restart to surface its sensor.

Borrowed proven mechanics from its siblings: the config/state conventions of [light-programmer](https://github.com/dongnh/light_programmer), the Contact Sensor bridge mechanics (deterministic AIDs, stable MAC, pinned setup code) of [light-programmer-homekit](https://github.com/dongnh/light-programmer-homekit).

## Requirements

macOS or Linux on the same LAN as an Apple Home hub, Python 3.10+.

## Install & pair

Install into a virtualenv (`pip install -e .`) and run `homekit-upkeep --config config.json`. On first launch the bridge prints its HomeKit setup code; in the Home app choose Add Accessory and enter it. Set a fixed `pincode` so the code stays stable across restarts.

## Configure

A small JSON file — see [`examples/config.json`](examples/config.json). Fields you might set:

- `tasks` — the chores. Each needs a stable `id` (never rename it — it anchors the HomeKit identity), a `name` (the accessory name in Apple Home), `interval_days` (how often; fractions allowed for testing), and an optional `time` (`HH:MM`, default `09:00` — the earliest hour a reminder may fire).
- `bridge_name` — the name in Apple Home; it also seeds the stable MAC, so pick one you'll keep.
- `pincode` — fixed setup code (`DDD-DD-DDD`). Without it the bridge generates a new code on every restart.
- `mac` — explicit HomeKit device id; set one to rotate identity after a breaking accessory-set change.
- `port` — HAP port (default `51830`).
- `state_path` / `accessory_state` — where last-done timestamps and pairing state live.
- `http_host` / `http_port` / `http_api_key` — the HTTP API bind (default loopback `127.0.0.1:7872`; set a key if you open it to the LAN).
- `tick_seconds` — how often due-ness is re-evaluated (default 30).

On first run every task is seeded as "just done" — a fresh install shouldn't open every sensor at once. Force one due with `POST /due` if you want the reminder today.

## HTTP API

- `GET /tasks` — every task with `last_done`, `due_at`, current `due` and the countdown `battery` percentage.
- `POST /done {"id": "office_zz_plant"}` — mark done now (same as the switch).
- `POST /due {"id": "office_zz_plant"}` — force a task due now, for testing or "nag me about this today".

All endpoints honor `X-API-Key` when `http_api_key` is set.

## License

MIT
