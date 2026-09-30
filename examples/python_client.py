#!/usr/bin/env python3
"""Python (stdlib-only) calling example for the recycling calendar API.

Run with the API already started::

    python examples/python_client.py
    BASE_URL=http://localhost:8000 python examples/python_client.py
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE_URL = os.getenv("BASE_URL", "http://localhost:8000")


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        BASE_URL + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def show(title: str, status: int, payload: dict) -> None:
    print(f"\n== {title} [{status}] ==")
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def main() -> None:
    status, point = call(
        "POST",
        "/points",
        {"name": "阳光社区回收点", "timezone": "Asia/Shanghai"},
    )
    show("create point", status, point)
    pid = point["id"]

    status, published = call(
        "POST",
        f"/points/{pid}/rules",
        {
            "expected_version": 0,
            "rules": {
                "weekly": [
                    {"weekday": 1, "start_time": "09:00", "end_time": "17:00"},
                    {"weekday": 3, "start_time": "09:00", "end_time": "17:00"},
                    {"weekday": 5, "start_time": "20:00", "end_time": "02:00"},
                ],
                "closed_dates": ["2026-10-01"],
                "makeup": [
                    {"day": "2026-10-01", "start_time": "13:00", "end_time": "16:00"}
                ],
            },
        },
    )
    show("publish v1", status, published)

    status, conflict = call(
        "POST",
        f"/points/{pid}/rules",
        {
            "expected_version": 0,  # stale
            "rules": {"weekly": [], "closed_dates": [], "makeup": []},
        },
    )
    show("stale publish -> distinguishable conflict", status, conflict)

    status, calendar = call(
        "GET", f"/points/{pid}/calendar?from=2026-09-28&to=2026-10-04"
    )
    show("calendar for an inclusive local-date range", status, calendar)

    # Compact rendering of the open intervals.
    if status == 200:
        print("\nOpen intervals (UTC):")
        for day in calendar["days"]:
            for interval in day["intervals"]:
                sources = ",".join(
                    s["kind"] + (f":{s.get('weekday') or s.get('day')}")
                    for s in interval["sources"]
                )
                print(
                    f"  {day['local_date']}  {interval['start']} -> {interval['end']}"
                    f"  [{sources}]"
                )

    # ------------------------------------------------------------------
    # Reservations: booked against the currently published calendar.
    # The window must start within the next 30 days, so compute it from now.
    # ------------------------------------------------------------------
    tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).date()
    weekday = tomorrow.isoweekday()

    status, utc_point = call(
        "POST", "/points", {"name": "预约演示点", "timezone": "UTC"}
    )
    show("create a UTC point for reservations", status, utc_point)
    rpid = utc_point["id"]

    status, published = call(
        "POST",
        f"/points/{rpid}/rules",
        {
            "expected_version": 0,
            "rules": {
                "weekly": [
                    {"weekday": weekday, "start_time": "08:00", "end_time": "12:00"}
                ],
                "closed_dates": [],
                "makeup": [],
            },
        },
    )
    show(f"publish weekly rule for weekday {weekday}", status, published)

    window = {
        "idempotency_key": "visit-demo-1",
        "start": f"{tomorrow}T09:00:00Z",
        "end": f"{tomorrow}T10:00:00Z",
    }
    status, reservation = call("POST", f"/points/{rpid}/reservations", window)
    show("book 09:00-10:00 UTC tomorrow", status, reservation)
    rid = reservation["id"]
    token = reservation["cancel_token"]

    status, replay = call("POST", f"/points/{rpid}/reservations", window)
    show("same key + same content replays (200)", status, replay)

    status, conflict = call(
        "POST",
        f"/points/{rpid}/reservations",
        {
            "idempotency_key": "visit-demo-1",
            "start": f"{tomorrow}T10:00:00Z",
            "end": f"{tomorrow}T11:00:00Z",
        },
    )
    show("same key + different content conflicts", status, conflict)

    status, overlap = call(
        "POST",
        f"/points/{rpid}/reservations",
        {
            "idempotency_key": "visit-demo-2",
            "start": f"{tomorrow}T09:30:00Z",
            "end": f"{tomorrow}T10:30:00Z",
        },
    )
    show("overlapping window rejected", status, overlap)

    # --- Reschedule with the cancel token: 09:00-10:00 -> 10:00-11:00 ----
    reschedule_body = {
        "cancel_token": token,
        "idempotency_key": "visit-demo-1-move",
        "expected_revision": 1,
        "start": f"{tomorrow}T10:00:00Z",
        "end": f"{tomorrow}T11:00:00Z",
    }
    status, moved = call(
        "POST",
        f"/points/{rpid}/reservations/{rid}/reschedule",
        reschedule_body,
    )
    show("reschedule with the cancel token (revision bumps to 2)", status, moved)

    status, replay = call(
        "POST",
        f"/points/{rpid}/reservations/{rid}/reschedule",
        reschedule_body,
    )
    show("same reschedule key + content replays the attempt (200)", status, replay)

    status, rs_conflict = call(
        "POST",
        f"/points/{rpid}/reservations/{rid}/reschedule",
        {
            "cancel_token": token,
            "idempotency_key": "visit-demo-1-move",
            "expected_revision": 2,
            "start": f"{tomorrow}T08:00:00Z",
            "end": f"{tomorrow}T09:00:00Z",
        },
    )
    show("same reschedule key, different content conflicts", status, rs_conflict)

    status, stale = call(
        "POST",
        f"/points/{rpid}/reservations/{rid}/reschedule",
        {
            "cancel_token": token,
            "idempotency_key": "visit-demo-1-move-again",
            "expected_revision": 1,  # stale: the reservation is now at revision 2
            "start": f"{tomorrow}T08:00:00Z",
            "end": f"{tomorrow}T09:00:00Z",
        },
    )
    show("stale expected_revision conflicts", status, stale)

    # The creation key is unaffected: it still replays the first-created window.
    status, create_replay = call(
        "POST", f"/points/{rpid}/reservations", window
    )
    show("creation key replay still returns the original window", status, create_replay)

    status, blocked = call(
        "POST",
        f"/points/{rpid}/rules",
        {
            "expected_version": 1,
            "rules": {"weekly": [], "closed_dates": [], "makeup": []},
        },
    )
    show("publish that un-covers the reservation is refused", status, blocked)

    status, cancelled = call(
        "POST",
        f"/points/{rpid}/reservations/{rid}/cancel",
        {"cancel_token": token},
    )
    show("cancel with the issued token", status, cancelled)

    status, again = call(
        "POST",
        f"/points/{rpid}/reservations/{rid}/cancel",
        {"cancel_token": token},
    )
    show("repeat cancel returns the cancelled state", status, again)

    # ------------------------------------------------------------------
    # Waitlist (候补): apply for a window already taken by someone else.
    # At this point demo-visit-3 holds 09:00-10:00 and demo-visit-5 holds
    # 10:00-11:00, so take a fresh 08:00-08:30 slot as the holder.
    # ------------------------------------------------------------------
    holder = call(
        "POST",
        f"/points/{rpid}/reservations",
        {
            "idempotency_key": "visit-demo-holder",
            "start": f"{tomorrow}T08:00:00Z",
            "end": f"{tomorrow}T08:30:00Z",
        },
    )[1]
    show("a new holder occupies 08:00-08:30", 201, holder)

    wait_body = {
        "idempotency_key": "visit-demo-wait",
        "start": f"{tomorrow}T08:00:00Z",
        "end": f"{tomorrow}T08:30:00Z",
    }
    status, waiting = call("POST", f"/points/{rpid}/waitlist", wait_body)
    show("join the waitlist for the occupied 08:00-08:30 window", status, waiting)
    wid = waiting["id"]
    wtoken = waiting["query_token"]

    status, w_replay = call("POST", f"/points/{rpid}/waitlist", wait_body)
    show("same key + same content replays the application (200)", status, w_replay)

    status, w_conflict = call(
        "POST",
        f"/points/{rpid}/waitlist",
        {
            "idempotency_key": "visit-demo-wait",
            "start": f"{tomorrow}T08:15:00Z",
            "end": f"{tomorrow}T08:45:00Z",
        },
    )
    show("same key + different content conflicts", status, w_conflict)

    status, free = call(
        "POST",
        f"/points/{rpid}/waitlist",
        {
            "idempotency_key": "visit-demo-free-window",
            "start": f"{tomorrow}T11:30:00Z",
            "end": f"{tomorrow}T12:00:00Z",
        },
    )
    show("a free window is not accepted onto the waitlist", status, free)

    status, w_state = call(
        "GET",
        f"/points/{rpid}/waitlist/{wid}?query_token={wtoken}",
    )
    show("query own waitlist state with the query key", status, w_state)

    # A second application, withdrawn while still queued.
    _, w2 = call(
        "POST",
        f"/points/{rpid}/waitlist",
        {
            "idempotency_key": "visit-demo-wait-2",
            "start": f"{tomorrow}T08:00:00Z",
            "end": f"{tomorrow}T08:15:00Z",
        },
    )
    status, withdrawn = call(
        "POST",
        f"/points/{rpid}/waitlist/{w2['id']}/withdraw",
        {"query_token": w2["query_token"]},
    )
    show("withdraw a queued application with the query key", status, withdrawn)

    status, promoted = call(
        "POST",
        f"/points/{rpid}/reservations/{holder['id']}/cancel",
        {"cancel_token": holder["cancel_token"]},
    )
    show("cancelling the holder runs the promotion sweep atomically", status, promoted)

    status, w_state = call(
        "GET",
        f"/points/{rpid}/waitlist/{wid}?query_token={wtoken}",
    )
    show("waitlist entry is now promoted and carries the reservation", status, w_state)


if __name__ == "__main__":
    main()
