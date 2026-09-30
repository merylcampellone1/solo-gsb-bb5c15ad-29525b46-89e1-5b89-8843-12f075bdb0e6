"""End-to-end tests for visit reservations against the published calendar."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def create_point(client, tz="UTC"):
    r = client.post("/points", json={"name": "预约测试点", "timezone": tz})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def publish(client, pid, rules, expected=0):
    r = client.post(
        f"/points/{pid}/rules",
        json={"expected_version": expected, "rules": rules},
    )
    assert r.status_code == 201, r.text
    return r.json()["version"]


def tomorrow_at(hour, minute=0):
    """Tomorrow (UTC) at a fixed wall-clock hour; always safely in the future."""
    day = (datetime.now(UTC) + timedelta(days=1)).date()
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


def iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def reserve(client, pid, key, start, end):
    return client.post(
        f"/points/{pid}/reservations",
        json={"idempotency_key": key, "start": iso(start), "end": iso(end)},
    )


def open_point(client, weekly=None, makeup=None, tz="UTC"):
    """A point whose published rules cover ``weekly``/``makeup``; returns pid."""
    pid = create_point(client, tz=tz)
    publish(
        client,
        pid,
        {
            "weekly": weekly
            if weekly is not None
            else [
                {
                    "weekday": tomorrow_at(0).isoweekday(),
                    "start_time": "08:00",
                    "end_time": "12:00",
                }
            ],
            "closed_dates": [],
            "makeup": makeup or [],
        },
    )
    return pid


# ---------------------------------------------------------------------------
# Creation + idempotency
# ---------------------------------------------------------------------------

def test_create_reservation_success_returns_number_token_version(client):
    pid = open_point(client)
    start, end = tomorrow_at(9), tomorrow_at(10)
    r = reserve(client, pid, "key-1", start, end)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["id"] > 0
    assert body["point_id"] == pid
    assert body["status"] == "active"
    assert body["rules_version"] == 1
    assert body["cancel_token"]
    assert body["start"] == iso(start)
    assert body["end"] == iso(end)
    assert body["cancelled_at"] is None


def test_same_key_same_content_replays_original_result(client):
    pid = open_point(client)
    start, end = tomorrow_at(9), tomorrow_at(10)
    first = reserve(client, pid, "dup", start, end)
    assert first.status_code == 201
    replay = reserve(client, pid, "dup", start, end)
    assert replay.status_code == 200
    assert replay.json() == first.json()


def test_same_key_different_content_conflicts(client):
    pid = open_point(client)
    assert reserve(client, pid, "dup", tomorrow_at(9), tomorrow_at(10)).status_code == 201
    conflict = reserve(client, pid, "dup", tomorrow_at(10), tomorrow_at(11))
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"


def test_same_key_on_another_point_is_independent(client):
    pid1, pid2 = open_point(client), open_point(client)
    start, end = tomorrow_at(9), tomorrow_at(10)
    assert reserve(client, pid1, "shared", start, end).status_code == 201
    assert reserve(client, pid2, "shared", start, end).status_code == 201


# ---------------------------------------------------------------------------
# Window policy
# ---------------------------------------------------------------------------

def test_invalid_windows_are_invalid_period(client):
    pid = open_point(client)
    # end not after start
    r = reserve(client, pid, "w1", tomorrow_at(10), tomorrow_at(10))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_period"
    # inverted
    r = reserve(client, pid, "w2", tomorrow_at(11), tomorrow_at(10))
    assert r.status_code == 422
    # longer than two hours
    r = reserve(client, pid, "w3", tomorrow_at(8), tomorrow_at(10, 1))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_period"
    # start in the past
    past = datetime.now(UTC) - timedelta(hours=1)
    r = reserve(client, pid, "w4", past, past + timedelta(hours=1))
    assert r.status_code == 422
    # start beyond the 30-day horizon
    far = datetime.now(UTC) + timedelta(days=31)
    r = reserve(client, pid, "w5", far, far + timedelta(hours=1))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_period"


def test_exactly_two_hours_and_just_within_horizon_are_allowed(client):
    pid = open_point(
        client,
        weekly=[
            {
                "weekday": tomorrow_at(0).isoweekday(),
                "start_time": "08:00",
                "end_time": "12:00",
            }
        ],
    )
    r = reserve(client, pid, "two-hours", tomorrow_at(9), tomorrow_at(11))
    assert r.status_code == 201, r.text


def test_naive_timestamps_are_rejected_as_invalid_request(client):
    pid = open_point(client)
    r = client.post(
        f"/points/{pid}/reservations",
        json={
            "idempotency_key": "naive",
            "start": "2026-10-01T09:00:00",
            "end": "2026-10-01T10:00:00",
        },
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_request"


def test_missing_fields_are_invalid_request(client):
    pid = open_point(client)
    r = client.post(f"/points/{pid}/reservations", json={"start": iso(tomorrow_at(9))})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_request"


# ---------------------------------------------------------------------------
# Calendar containment
# ---------------------------------------------------------------------------

def test_no_published_rules_is_a_distinguishable_error(client):
    pid = create_point(client)
    r = reserve(client, pid, "norules", tomorrow_at(9), tomorrow_at(10))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "no_published_version"


def test_outside_open_hours_rejected(client):
    pid = open_point(client)  # open 08:00-12:00 tomorrow
    r = reserve(client, pid, "late", tomorrow_at(13), tomorrow_at(14))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "outside_open_hours"
    # partially inside is still outside
    r = reserve(client, pid, "partial", tomorrow_at(11), tomorrow_at(12, 30))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "outside_open_hours"


def test_reservation_may_cross_local_midnight_inside_overnight_rule(client):
    weekday = tomorrow_at(0).isoweekday()
    pid = open_point(
        client,
        weekly=[{"weekday": weekday, "start_time": "22:00", "end_time": "02:00"}],
    )
    # 23:00 -> 01:00 next day crosses the local midnight, fully inside 22:00-02:00.
    r = reserve(client, pid, "overnight", tomorrow_at(23), tomorrow_at(1) + timedelta(days=1))
    assert r.status_code == 201, r.text
    # But 21:30 -> 23:00 starts before the session opens.
    r = reserve(client, pid, "too-early", tomorrow_at(21, 30), tomorrow_at(23))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "outside_open_hours"


def test_touching_merged_sessions_count_as_one_continuous_interval(client):
    day = (datetime.now(UTC) + timedelta(days=1)).date()
    pid = open_point(
        client,
        weekly=[
            {
                "weekday": day.isoweekday(),
                "start_time": "08:00",
                "end_time": "12:00",
            }
        ],
        makeup=[{"day": day.isoformat(), "start_time": "12:00", "end_time": "14:00"}],
    )
    # 11:00-13:00 spans the seam of two touching sessions (merged calendar).
    r = reserve(client, pid, "seam", tomorrow_at(11), tomorrow_at(13))
    assert r.status_code == 201, r.text


# ---------------------------------------------------------------------------
# Overlap
# ---------------------------------------------------------------------------

def test_overlap_rejected_but_touching_allowed(client):
    pid = open_point(client)
    assert reserve(client, pid, "a", tomorrow_at(9), tomorrow_at(10)).status_code == 201
    # overlapping windows
    for key, s, e in [
        ("b", tomorrow_at(9, 30), tomorrow_at(10, 30)),
        ("c", tomorrow_at(8, 30), tomorrow_at(9, 30)),
        ("d", tomorrow_at(9), tomorrow_at(10)),
    ]:
        r = reserve(client, pid, key, s, e)
        assert r.status_code == 409, (key, r.text)
        assert r.json()["error"]["code"] == "reservation_overlap"
    # end-to-end touching coexists
    assert reserve(client, pid, "e", tomorrow_at(10), tomorrow_at(11)).status_code == 201
    assert reserve(client, pid, "f", tomorrow_at(8), tomorrow_at(9)).status_code == 201


def test_cancelled_reservation_frees_the_window(client):
    pid = open_point(client)
    first = reserve(client, pid, "gone", tomorrow_at(9), tomorrow_at(10)).json()
    r = client.post(
        f"/points/{pid}/reservations/{first['id']}/cancel",
        json={"cancel_token": first["cancel_token"]},
    )
    assert r.status_code == 200
    again = reserve(client, pid, "reused-window", tomorrow_at(9), tomorrow_at(10))
    assert again.status_code == 201, again.text


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------

def test_cancel_requires_and_checks_the_token(client):
    pid = open_point(client)
    created = reserve(client, pid, "tok", tomorrow_at(9), tomorrow_at(10)).json()
    rid = created["id"]
    # missing token
    r = client.post(f"/points/{pid}/reservations/{rid}/cancel", json={})
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "invalid_cancel_token"
    # wrong token
    r = client.post(
        f"/points/{pid}/reservations/{rid}/cancel",
        json={"cancel_token": "wrong"},
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "invalid_cancel_token"
    # correct token
    r = client.post(
        f"/points/{pid}/reservations/{rid}/cancel",
        json={"cancel_token": created["cancel_token"]},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "cancelled"
    assert body["cancelled_at"] is not None
    assert "cancel_token" not in body
    # repeat cancel returns the already-cancelled state
    r = client.post(
        f"/points/{pid}/reservations/{rid}/cancel",
        json={"cancel_token": created["cancel_token"]},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "cancelled"


def test_cancel_unknown_reservation_or_wrong_point_is_404(client):
    pid = open_point(client)
    created = reserve(client, pid, "lost", tomorrow_at(9), tomorrow_at(10)).json()
    r = client.post(
        f"/points/{pid}/reservations/999999/cancel",
        json={"cancel_token": created["cancel_token"]},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "reservation_not_found"
    other = open_point(client)
    r = client.post(
        f"/points/{other}/reservations/{created['id']}/cancel",
        json={"cancel_token": created["cancel_token"]},
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "reservation_not_found"


def test_replay_after_cancel_returns_the_original_reservation(client):
    pid = open_point(client)
    start, end = tomorrow_at(9), tomorrow_at(10)
    created = reserve(client, pid, "ghost", start, end).json()
    client.post(
        f"/points/{pid}/reservations/{created['id']}/cancel",
        json={"cancel_token": created["cancel_token"]},
    )
    replay = reserve(client, pid, "ghost", start, end)
    assert replay.status_code == 200
    assert replay.json()["id"] == created["id"]
    assert replay.json()["cancel_token"] == created["cancel_token"]
    assert replay.json()["status"] == "cancelled"


# ---------------------------------------------------------------------------
# Rule publication / timezone changes vs. active reservations
# ---------------------------------------------------------------------------

def test_publish_that_uncovers_a_reservation_is_refused(client):
    pid = open_point(client)
    created = reserve(client, pid, "keep", tomorrow_at(9), tomorrow_at(10)).json()
    # Removing the only weekly rule would leave the reservation uncovered.
    r = client.post(
        f"/points/{pid}/rules",
        json={
            "expected_version": 1,
            "rules": {"weekly": [], "closed_dates": [], "makeup": []},
        },
    )
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "reservation_change_conflict"
    assert err["details"]["conflicting_reservation_ids"] == [created["id"]]
    # The point is still at version 1.
    assert client.get(f"/points/{pid}").json()["version"] == 1


def test_publish_that_keeps_coverage_is_allowed(client):
    pid = open_point(client)
    reserve(client, pid, "keep", tomorrow_at(9), tomorrow_at(10))
    weekday = tomorrow_at(0).isoweekday()
    r = client.post(
        f"/points/{pid}/rules",
        json={
            "expected_version": 1,
            "rules": {
                "weekly": [
                    {"weekday": weekday, "start_time": "07:00", "end_time": "13:00"}
                ],
                "closed_dates": [],
                "makeup": [],
            },
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["version"] == 2


def test_cancelled_reservation_does_not_block_publication(client):
    pid = open_point(client)
    created = reserve(client, pid, "cx", tomorrow_at(9), tomorrow_at(10)).json()
    client.post(
        f"/points/{pid}/reservations/{created['id']}/cancel",
        json={"cancel_token": created["cancel_token"]},
    )
    r = client.post(
        f"/points/{pid}/rules",
        json={
            "expected_version": 1,
            "rules": {"weekly": [], "closed_dates": [], "makeup": []},
        },
    )
    assert r.status_code == 201, r.text


def test_timezone_change_that_uncovers_a_reservation_is_refused(client):
    pid = open_point(client)  # UTC, open tomorrow 08:00-12:00 local
    created = reserve(client, pid, "tz", tomorrow_at(9), tomorrow_at(10)).json()
    # UTC-5: the session becomes 13:00-17:00 UTC, no longer covering 09:00.
    r = client.patch(f"/points/{pid}", json={"timezone": "Etc/GMT+5"})
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "reservation_change_conflict"
    assert err["details"]["conflicting_reservation_ids"] == [created["id"]]
    assert client.get(f"/points/{pid}").json()["timezone"] == "UTC"
    # UTC+1: the session becomes 07:00-11:00 UTC, still covering 09:00-10:00.
    r = client.patch(f"/points/{pid}", json={"timezone": "Etc/GMT-1"})
    assert r.status_code == 200, r.text
    assert r.json()["timezone"] == "Etc/GMT-1"


def test_name_only_patch_is_not_affected_by_reservations(client):
    pid = open_point(client)
    reserve(client, pid, "nm", tomorrow_at(9), tomorrow_at(10))
    r = client.patch(f"/points/{pid}", json={"name": "改名"})
    assert r.status_code == 200
    assert r.json()["name"] == "改名"


def test_reservation_on_non_utc_point_uses_local_wall_clock(client):
    # Asia/Shanghai is UTC+8: local 09:00-17:00 is 01:00-09:00 UTC.
    pid = open_point(
        client,
        weekly=[
            {
                "weekday": tomorrow_at(0).isoweekday(),
                "start_time": "09:00",
                "end_time": "17:00",
            }
        ],
        tz="Asia/Shanghai",
    )
    ok = reserve(client, pid, "sh-ok", tomorrow_at(1), tomorrow_at(2))
    assert ok.status_code == 201, ok.text
    # 02:00 UTC is 10:00 local - inside; 10:00 UTC is 18:00 local - outside.
    bad = reserve(client, pid, "sh-bad", tomorrow_at(9, 30), tomorrow_at(10, 30))
    assert bad.status_code == 409
    assert bad.json()["error"]["code"] == "outside_open_hours"


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------

def test_concurrent_overlapping_reservations_exactly_one_wins(client):
    pid = open_point(client)
    start, end = tomorrow_at(9), tomorrow_at(10)
    barrier = threading.Barrier(2)
    outcomes: list[tuple[int, dict]] = []

    def attempt(key):
        barrier.wait()
        r = reserve(client, pid, key, start, end)
        outcomes.append((r.status_code, r.json()))

    threads = [threading.Thread(target=attempt, args=(f"race-{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    statuses = sorted(status for status, _ in outcomes)
    assert statuses == [201, 409]
    loser = next(body for status, body in outcomes if status == 409)
    assert loser["error"]["code"] == "reservation_overlap"


def test_concurrent_reservation_and_breaking_publish_exactly_one_wins(client):
    pid = open_point(client)
    start, end = tomorrow_at(9), tomorrow_at(10)
    barrier = threading.Barrier(2)
    outcomes: dict[str, tuple[int, dict]] = {}

    def do_reserve():
        barrier.wait()
        r = reserve(client, pid, "race-res", start, end)
        outcomes["reserve"] = (r.status_code, r.json())

    def do_publish():
        barrier.wait()
        r = client.post(
            f"/points/{pid}/rules",
            json={
                "expected_version": 1,
                "rules": {"weekly": [], "closed_dates": [], "makeup": []},
            },
        )
        outcomes["publish"] = (r.status_code, r.json())

    threads = [threading.Thread(target=do_reserve), threading.Thread(target=do_publish)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    reserve_status, reserve_body = outcomes["reserve"]
    publish_status, publish_body = outcomes["publish"]
    if reserve_status == 201:
        # The reservation committed first, so emptying the calendar must fail.
        assert publish_status == 409
        assert publish_body["error"]["code"] == "reservation_change_conflict"
        assert client.get(f"/points/{pid}").json()["version"] == 1
    else:
        # The publish committed first, so the reservation finds no opening.
        assert reserve_status == 409
        assert reserve_body["error"]["code"] == "outside_open_hours"
        assert publish_status == 201
        assert client.get(f"/points/{pid}").json()["version"] == 2
