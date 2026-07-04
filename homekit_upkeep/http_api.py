"""Tiny stdlib HTTP server for the task list and mark-done/force-due —
the same shape (and X-API-Key semantics) as light-programmer's mode_http.

GET  /tasks        → {"tasks": [{id, name, interval_days, time, last_done,
                                 due_at, due, battery}, …]}
POST /done {"id"}  → mark the task done now (sensor closes, cycle restarts)
POST /due  {"id"}  → force the task due now (backdates last_done past the
                     interval) — for testing or "nag me about this today"
"""
import json
import logging
import threading
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Optional

from . import store


def make_server(state_path: str, tasks: list, snapshot: Callable,
                host: str, port: int, on_change: Optional[Callable] = None,
                api_key: Optional[str] = None) -> ThreadingHTTPServer:
    """Build a ThreadingHTTPServer; caller is responsible for serve_forever()
    in a background thread.

    `snapshot` returns the current task list for GET /tasks. `on_change` is
    invoked (no args) after every successful POST so the HAP loop can refresh
    the sensors immediately instead of waiting for the next tick.

    `api_key` (optional): when set, every request must carry a matching
    `X-API-Key` header or it gets 401; unset = unauthenticated, fine for
    loopback.
    """
    by_id = {t["id"]: t for t in tasks}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # silence default stderr noise
            logging.debug("http_api " + fmt, *args)

        def _send(self, code: int, body: dict):
            payload = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def _read_json(self) -> dict:
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            raw = self.rfile.read(length)
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return {}

        def _authed(self) -> bool:
            """True if no key is configured, or the request carries a matching
            X-API-Key. On mismatch, emits 401 and returns False."""
            if not api_key:
                return True
            if self.headers.get("X-API-Key") == api_key:
                return True
            logging.warning("http_api: rejected unauthenticated %s %s from %s",
                            self.command, self.path, self.client_address[0])
            self._send(401, {"error": "unauthorized"})
            return False

        def do_GET(self):  # noqa: N802
            if not self._authed():
                return
            if self.path.rstrip("/") in ("", "/tasks"):
                self._send(200, {"tasks": snapshot()})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):  # noqa: N802
            if not self._authed():
                return
            path = self.path.rstrip("/")
            if path not in ("/done", "/due"):
                self._send(404, {"error": "not found"})
                return
            body = self._read_json()
            if "id" not in body:
                self._send(400, {"error": "expected id"})
                return
            task = by_id.get(body["id"])
            if task is None:
                self._send(404, {"error": f"unknown task id '{body['id']}'"})
                return
            if path == "/done":
                when = datetime.now()
            else:  # /due — backdate past the interval so the task is due NOW
                when = datetime.now() - timedelta(days=task["interval_days"] + 1)
            store.mark_done(state_path, task["id"],
                            when.isoformat(timespec="seconds"))
            if on_change:
                try:
                    on_change()
                except Exception as e:  # pragma: no cover
                    logging.warning("on_change hook failed: %s", e)
            entry = next(t for t in snapshot() if t["id"] == task["id"])
            self._send(200, entry)

    return ThreadingHTTPServer((host, port), Handler)


def start_in_thread(state_path: str, tasks: list, snapshot: Callable,
                    host: str, port: int, on_change: Optional[Callable] = None,
                    api_key: Optional[str] = None) -> ThreadingHTTPServer:
    if not api_key and host not in ("127.0.0.1", "localhost", "::1"):
        logging.warning("HTTP API bound to non-loopback host %s with NO "
                        "X-API-Key — /tasks, /done and /due are unauthenticated "
                        "and reachable on the LAN. Set http_api_key.", host)
    server = make_server(state_path, tasks, snapshot, host, port,
                         on_change=on_change, api_key=api_key)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    logging.info("HTTP API listening on %s:%s (state=%s, auth=%s)",
                 host, port, state_path, "on" if api_key else "off")
    return server
