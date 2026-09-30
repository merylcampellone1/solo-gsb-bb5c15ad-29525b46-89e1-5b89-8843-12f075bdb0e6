"""End-to-end tests for reservation rescheduling (凭取消凭证改期)."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def create_point(client, tz="UTC"):
    r = client.post("/points", json={"name": "改期测试点", "timezone": tz})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def open_point(client, weekly=None, makeup=None, tz="UTC"):
    """Point with a rule covering ``weekly``; default 08:00-12:00 tomorrow."""
    pid = create_point(client, tz=tz)
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).date()
    r = client.post(
        f"/points/{pid}/rules",
        json={
            "expected_version": 0,
            "rules": {
                "weekly": weekly
                if weekly is not None
                else [
                    {
                        "weekday": tomorrow.isoweekday(),
                        "start_time": "08:00",
                        "end_time": "12:00",
                    }
                ],
                "closed_dates": [],
                "makeup": makeup or [],
            },
        },
    )
    assert r.status_code == 201, r.text
    return pid


def at(hour, minute=0, day_offset=1):
    """Fixed UTC wall-clock ``day_offset`` days from today."""
    day = (datetime.now(UTC) + timedelta(days=day_offset)).date()
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


def iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def reserve(client, pid, key, start, end):
    return client.post(
        f"/points/{pid}/reservations",
        json={"idempotency_key": key, "start": iso(start), "end": iso(end)},
    )


def reschedule(client, pid, rid, token, key, start, end, expected_revision):
    return client.post(
        f"/points/{pid}/reservations/{rid}/reschedule",
        json={
            "cancel_token": token,
            "idempotency_key": key,
            "expected_revision": expected_revision,
            "start": iso(start),
            "end": iso(end),
        },
    )


def cancel(client, pid, rid, token):
    return client.post(
        f"/points/{pid}/reservations/{rid}/cancel",
        json={"cancel_token": token},
    )


def book_one(client, key="r-1", start=None, end=None):
    pid = open_point(client)
    start = start or at(9)
    end = end or at(10)
    created = reserve(client, pid, key, start, end)
    assert created.status_code == 201, created.text
    return pid, created.json()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_reschedule_success_moves_window_and_bumps_revision(client):
    pid, resv = book_one(client)
    new_start, new_end = at(10), at(11)
    r = reschedule(
        client, pid, resv["id"], resv["cancel_token"], "move-1",
        new_start, new_end, expected_revision=1,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["id"] == resv["id"]
    assert body["revision"] == 2
    assert body["start"] == iso(new_start)
    assert body["end"] == iso(new_end)
    assert body["rules_version"] == 1
    assert body["status"] == "active"
    assert body["cancel_token"] == resv["cancel_token"]
    assert body["created_at"] == resv["created_at"]
    assert body["cancelled_at"] is None


def test_reschedule_keeps_reservation_id_and_token_through_two_moves(client):
    pid, resv = book_one(client)
    token = resv["cancel_token"]
    r1 = reschedule(client, pid, resv["id"], token, "m1", at(10), at(11), 1)
    assert r1.status_code == 200, r1.text
    assert r1.json()["revision"] == 2
    r2 = reschedule(client, pid, resv["id"], token, "m2", at(11), at(11, 30), 2)
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["id"] == resv["id"]
    assert body["cancel_token"] == token
    assert body["revision"] == 3
    assert body["start"] == iso(at(11))


def test_reschedule_frees_the_old_window(client):
    pid, resv = book_one(client)
    r = reschedule(
        client, pid, resv["id"], resv["cancel_token"], "move",
        at(11), at(11, 30), 1,
    )
    assert r.status_code == 200, r.text
    # The original 09:00-10:00 window is now bookable by someone else.
    again = reserve(client, pid, "new-tenant", at(9), at(10))
    assert again.status_code == 201, again.text


def test_reschedule_to_the_same_window_is_allowed(client):
    pid, resv = book_one(client)
    r = reschedule(
        client, pid, resv["id"], resv["cancel_token"], "same",
        at(9), at(10), 1,
    )
    assert r.status_code == 200, r.text
    assert r.json()["revision"] == 2
    assert r.json()["start"] == iso(at(9))


# ---------------------------------------------------------------------------
# Reschedule idempotency
# ---------------------------------------------------------------------------

def test_same_reschedule_key_same_content_replays_successful_result(client):
    pid, resv = book_one(client)
    first = reschedule(
        client, pid, resv["id"], resv["cancel_token"], "idem",
        at(10), at(11), 1,
    )
    assert first.status_code == 200
    replay = reschedule(
        client, pid, resv["id"], resv["cancel_token"], "idem",
        at(10), at(11), 1,
    )
    assert replay.status_code == 200
    assert replay.json() == first.json()
    # The replay must not bump the revision again.
    assert replay.json()["revision"] == 2


def test_reschedule_replay_returns_that_attempt_even_after_later_move(client):
    pid, resv = book_one(client)
    token = resv["cancel_token"]
    first = reschedule(client, pid, resv["id"], token, "m1", at(10), at(11), 1)
    assert first.status_code == 200
    reschedule(client, pid, resv["id"], token, "m2", at(11), at(11, 30), 2)
    # Replaying key "m1" returns the result of that successful attempt.
    replay = reschedule(client, pid, resv["id"], token, "m1", at(10), at(11), 1)
    assert replay.status_code == 200
    body = replay.json()
    assert body["revision"] == 2
    assert body["start"] == iso(at(10))
    assert body["end"] == iso(at(11))


def test_reschedule_replay_after_cancel_reports_cancelled_status(client):
    pid, resv = book_one(client)
    token = resv["cancel_token"]
    reschedule(client, pid, resv["id"], token, "m1", at(10), at(11), 1)
    cancel(client, pid, resv["id"], token)
    replay = reschedule(client, pid, resv["id"], token, "m1", at(10), at(11), 1)
    assert replay.status_code == 200
    body = replay.json()
    assert body["status"] == "cancelled"
    assert body["cancelled_at"] is not None
    assert body["start"] == iso(at(10))


def test_same_reschedule_key_different_content_conflicts_and_holds_revision(
    client,
):
    pid, resv = book_one(client)
    ok = reschedule(
        client, pid, resv["id"], resv["cancel_token"], "idem",
        at(10), at(11), 1,
    )
    assert ok.status_code == 200
    conflict = reschedule(
        client, pid, resv["id"], resv["cancel_token"], "idem",
        at(10, 30), at(11, 30), 2,
    )
    assert conflict.status_code == 409
    err = conflict.json()["error"]
    assert err["code"] == "idempotency_conflict"
    assert err["details"]["start"] == iso(at(10))
    # The conflicting attempt changed nothing.
    state = cancel(client, pid, resv["id"], resv["cancel_token"]).json()
    assert state["revision"] == 2
    assert state["start"] == iso(at(10))


def test_reschedule_key_is_independent_of_creation_key(client):
    pid, resv = book_one(client, key="create-key")
    # Reusing the creation key as a reschedule key is a separate namespace.
    r = reschedule(
        client, pid, resv["id"], resv["cancel_token"], "create-key",
        at(10), at(11), 1,
    )
    assert r.status_code == 200, r.text


def test_reschedule_keys_are_scoped_per_reservation(client):
    # Same key on two different reservations must not collide.
    p1, r1 = book_one(client, key="a", start=at(9), end=at(10))
    p2, r2 = book_one(client, key="b", start=at(9), end=at(10))
    assert p1 != p2
    resp1 = reschedule(
        client, p1, r1["id"], r1["cancel_token"], "shared", at(10), at(11), 1
    )
    resp2 = reschedule(
        client, p2, r2["id"], r2["cancel_token"], "shared", at(10), at(11), 1
    )
    assert resp1.status_code == 200, resp1.text
    assert resp2.status_code == 200, resp2.text


# ---------------------------------------------------------------------------
# Stale revision
# ---------------------------------------------------------------------------

def test_stale_expected_revision_conflicts_and_does_not_move(client):
    pid, resv = book_one(client)
    token = resv["cancel_token"]
    reschedule(client, pid, resv["id"], token, "m1", at(10), at(11), 1)
    stale = reschedule(client, pid, resv["id"], token, "m-stale", at(8), at(9), 1)
    assert stale.status_code == 409
    err = stale.json()["error"]
    assert err["code"] == "revision_conflict"
    assert err["details"]["expected_revision"] == 1
    assert err["details"]["current_revision"] == 2
    # Window untouched.
    state = cancel(client, pid, resv["id"], token).json()
    assert state["start"] == iso(at(10))
    assert state["revision"] == 2


# ---------------------------------------------------------------------------
# Not-reschedulable states
# ---------------------------------------------------------------------------

def test_cancelled_reservation_cannot_be_rescheduled(client):
    pid, resv = book_one(client)
    token = resv["cancel_token"]
    assert cancel(client, pid, resv["id"], token).status_code == 200
    r = reschedule(client, pid, resv["id"], token, "after-cancel", at(10), at(11), 1)
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "reservation_not_reschedulable"
    assert err["details"]["reason"] == "reservation_cancelled"


def test_started_reservation_cannot_be_rescheduled(client, monkeypatch):
    import app.main as main_module

    pid, resv = book_one(client, start=at(9), end=at(11))
    # Freeze "now" to shortly after the reservation started.
    frozen = at(9, 30)
    monkeypatch.setattr(main_module, "datetime", _FrozenDateTime(frozen))
    r = reschedule(
        client, pid, resv["id"], resv["cancel_token"], "too-late",
        at(9, 45), at(10, 45), 1,
    )
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "reservation_not_reschedulable"
    assert err["details"]["reason"] == "reservation_started"


class _FrozenDateTime:
    """datetime stand-in whose now(tz) returns one fixed instant."""

    def __init__(self, frozen):
        self._frozen = frozen

    def now(self, tz=None):
        if tz is None:
            return self._frozen.replace(tzinfo=None)
        return self._frozen.astimezone(tz)


# ---------------------------------------------------------------------------
# Token / existence semantics (mirror the cancel endpoint)
# ---------------------------------------------------------------------------

def test_reschedule_without_token_is_invalid_cancel_token(client):
    pid, resv = book_one(client)
    r = client.post(
        f"/points/{pid}/reservations/{resv['id']}/reschedule",
        json={
            "idempotency_key": "no-token",
            "expected_revision": 1,
            "start": iso(at(10)),
            "end": iso(at(11)),
        },
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "invalid_cancel_token"


def test_reschedule_with_wrong_token_is_invalid_cancel_token(client):
    pid, resv = book_one(client)
    r = reschedule(
        client, pid, resv["id"], "wrong-token", "bad", at(10), at(11), 1
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "invalid_cancel_token"


def test_reschedule_unknown_reservation_is_404(client):
    pid = open_point(client)
    r = client.post(
        f"/points/{pid}/reservations/999999/reschedule",
        json={
            "cancel_token": "anything",
            "idempotency_key": "ghost",
            "expected_revision": 1,
            "start": iso(at(10)),
            "end": iso(at(11)),
        },
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "reservation_not_found"


def test_reschedule_on_wrong_point_is_404(client):
    pid, resv = book_one(client)
    other = open_point(client)
    r = reschedule(
        client, other, resv["id"], resv["cancel_token"], "x", at(10), at(11), 1
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "reservation_not_found"


# ---------------------------------------------------------------------------
# Window validation against the current calendar
# ---------------------------------------------------------------------------

def test_reschedule_outside_open_hours_rejected_and_window_kept(client):
    pid, resv = book_one(client)
    token = resv["cancel_token"]
    r = reschedule(client, pid, resv["id"], token, "late", at(13), at(14), 1)
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "outside_open_hours"
    # Failure must not release / alter the original window: while still active
    # it keeps blocking a competing booking of the old window.
    clash = reserve(client, pid, "squatter", at(9), at(10))
    assert clash.status_code == 409
    assert clash.json()["error"]["code"] == "reservation_overlap"
    state = cancel(client, pid, resv["id"], token).json()
    assert state["revision"] == 1
    assert state["start"] == iso(at(9))


def test_reschedule_invalid_window_is_invalid_period(client):
    pid, resv = book_one(client)
    token = resv["cancel_token"]
    base = dict(
        pid=pid, rid=resv["id"], token=token, expected_revision=1,
    )
    # end not after start
    r = reschedule(client, base["pid"], base["rid"], token, "w1", at(10), at(10), 1)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_period"
    # longer than two hours
    r = reschedule(client, pid, resv["id"], token, "w2", at(8), at(10, 1), 1)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_period"
    # start in the past
    past = datetime.now(UTC) - timedelta(minutes=30)
    r = reschedule(
        client, pid, resv["id"], token, "w3", past, past + timedelta(hours=1), 1
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_period"
    # beyond the 30-day horizon
    far = datetime.now(UTC) + timedelta(days=31)
    r = reschedule(
        client, pid, resv["id"], token, "w4", far, far + timedelta(hours=1), 1
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_period"


def test_reschedule_naive_timestamps_are_invalid_request(client):
    pid, resv = book_one(client)
    r = client.post(
        f"/points/{pid}/reservations/{resv['id']}/reschedule",
        json={
            "cancel_token": resv["cancel_token"],
            "idempotency_key": "naive",
            "expected_revision": 1,
            "start": "2026-10-01T10:00:00",
            "end": "2026-10-01T11:00:00",
        },
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_request"


def test_reschedule_overlapping_other_reservation_rejected(client):
    pid = open_point(client)
    first = reserve(client, pid, "a", at(9), at(10)).json()
    second = reserve(client, pid, "b", at(10), at(11)).json()
    # Moving "second" onto "first" overlaps a different reservation.
    r = reschedule(
        client, pid, second["id"], second["cancel_token"], "onto-a",
        at(9, 30), at(10, 30), 1,
    )
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "reservation_overlap"
    assert err["details"]["conflicting_reservation_ids"] == [first["id"]]


def test_reschedule_ignores_its_own_old_window(client):
    pid, resv = book_one(client)
    # New window overlaps the reservation's own current window — allowed.
    r = reschedule(
        client, pid, resv["id"], resv["cancel_token"], "grow",
        at(9, 30), at(10, 30), 1,
    )
    assert r.status_code == 200, r.text


def test_reschedule_touching_another_reservation_is_allowed(client):
    pid = open_point(client)
    first = reserve(client, pid, "a", at(9), at(10)).json()
    second = reserve(client, pid, "b", at(10), at(11)).json()
    # End-to-end touch with the other reservation is fine.
    r = reschedule(
        client, pid, second["id"], second["cancel_token"], "touch",
        at(8), at(9), 1,
    )
    assert r.status_code == 200, r.text
    assert first["id"] != second["id"]


# ---------------------------------------------------------------------------
# Creation-key replay still returns the first-created window
# ---------------------------------------------------------------------------

def test_create_key_replay_after_reschedule_returns_original_window(client):
    pid, resv = book_one(client, key="orig")
    original_start, original_end = at(9), at(10)
    reschedule(
        client, pid, resv["id"], resv["cancel_token"], "move", at(10), at(11), 1
    )
    replay = reserve(client, pid, "orig", original_start, original_end)
    assert replay.status_code == 200
    body = replay.json()
    assert body["id"] == resv["id"]
    assert body["start"] == iso(original_start)
    assert body["end"] == iso(original_end)
    assert body["revision"] == 1
    assert body["cancel_token"] == resv["cancel_token"]


def test_create_key_different_content_after_reschedule_still_conflicts(client):
    pid, resv = book_one(client, key="orig")
    reschedule(
        client, pid, resv["id"], resv["cancel_token"], "move", at(10), at(11), 1
    )
    # Different content under the create key conflicts, reporting first window.
    conflict = reserve(client, pid, "orig", at(11), at(11, 30))
    assert conflict.status_code == 409
    err = conflict.json()["error"]
    assert err["code"] == "idempotency_conflict"
    assert err["details"]["start"] == iso(at(9))


# ---------------------------------------------------------------------------
# Rules version recorded on reschedule
# ---------------------------------------------------------------------------

def test_reschedule_uses_and_records_the_current_rules_version(client):
    pid, resv = book_one(client)
    token = resv["cancel_token"]
    weekday = at(0).isoweekday()
    pub = client.post(
        f"/points/{pid}/rules",
        json={
            "expected_version": 1,
            "rules": {
                "weekly": [
                    {"weekday": weekday, "start_time": "07:00", "end_time": "14:00"}
                ],
                "closed_dates": [],
                "makeup": [],
            },
        },
    )
    assert pub.status_code == 201, pub.text
    r = reschedule(client, pid, resv["id"], token, "v2move", at(8), at(9), 1)
    assert r.status_code == 200, r.text
    assert r.json()["rules_version"] == 2


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------

def test_concurrent_reschedules_exactly_one_wins(client):
    pid, resv = book_one(client)
    token = resv["cancel_token"]
    barrier = threading.Barrier(2)
    outcomes = []

    def attempt(key):
        barrier.wait()
        r = reschedule(
            client, pid, resv["id"], token, key, at(10), at(11), 1
        )
        outcomes.append((r.status_code, r.json()))

    threads = [
        threading.Thread(target=attempt, args=("c1",)),
        threading.Thread(target=attempt, args=("c2",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    statuses = sorted(s for s, _ in outcomes)
    assert statuses == [200, 409]
    loser = next(b for s, b in outcomes if s == 409)
    assert loser["error"]["code"] == "revision_conflict"
    # Exactly one reschedule landed: revision is 2.
    state = cancel(client, pid, resv["id"], token).json()
    assert state["revision"] == 2
    assert state["start"] == iso(at(10))


def test_concurrent_reschedule_and_cancel_leave_consistent_state(client):
    pid, resv = book_one(client)
    token = resv["cancel_token"]
    barrier = threading.Barrier(2)
    outcomes = {}

    def do_reschedule():
        barrier.wait()
        r = reschedule(
            client, pid, resv["id"], token, "vs-cancel", at(10), at(11), 1
        )
        outcomes["reschedule"] = (r.status_code, r.json())

    def do_cancel():
        barrier.wait()
        r = cancel(client, pid, resv["id"], token)
        outcomes["cancel"] = (r.status_code, r.json())

    threads = [
        threading.Thread(target=do_reschedule),
        threading.Thread(target=do_cancel),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    rs_status, rs_body = outcomes["reschedule"]
    cx_status, cx_body = outcomes["cancel"]
    if rs_status == 200:
        # Reschedule committed first; the subsequent cancel still succeeds.
        assert cx_status == 200
        assert cx_body["status"] == "cancelled"
        assert rs_body["revision"] == 2
    else:
        # Cancel committed first; rescheduling a cancelled reservation fails
        # with a distinguishable code (never a 500).
        assert rs_status == 409
        assert rs_body["error"]["code"] == "reservation_not_reschedulable"
        assert cx_status == 200
        # Original window was not moved by the failed attempt.
        assert cx_body["start"] == iso(at(9))
        assert cx_body["revision"] == 1


def test_concurrent_reschedule_and_breaking_publish_exactly_one_wins(client):
    pid, resv = book_one(client)
    token = resv["cancel_token"]
    barrier = threading.Barrier(2)
    outcomes = {}

    def do_reschedule():
        barrier.wait()
        r = reschedule(
            client, pid, resv["id"], token, "vs-publish", at(10), at(11), 1
        )
        outcomes["reschedule"] = (r.status_code, r.json())

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

    threads = [
        threading.Thread(target=do_reschedule),
        threading.Thread(target=do_publish),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    rs_status, rs_body = outcomes["reschedule"]
    pub_status, pub_body = outcomes["publish"]
    if rs_status == 200:
        # Reschedule committed first under v1; emptying the calendar would
        # then un-cover it, so the publish is refused.
        assert pub_status == 409
        assert pub_body["error"]["code"] == "reservation_change_conflict"
        assert client.get(f"/points/{pid}").json()["version"] == 1
    else:
        # Publish committed first; the new window no longer lies in open hours.
        assert rs_status == 409
        assert rs_body["error"]["code"] == "outside_open_hours"
        assert pub_status == 201
        assert client.get(f"/points/{pid}").json()["version"] == 2
        # Failed reschedule left the reservation on its original window,
        # which the publish is allowed to break only because... it committed
        # before the reservation was re-validated; the reservation still
        # exists untouched at revision 1.
        state = client.post(
            f"/points/{pid}/reservations/{resv['id']}/cancel",
            json={"cancel_token": token},
        ).json()
        assert state["revision"] == 1
        assert state["start"] == iso(at(9))
