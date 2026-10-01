"""End-to-end tests for cross-point reassignment (改派到另一回收点)."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def create_point(client, tz="UTC"):
    r = client.post("/points", json={"name": "改派测试点", "timezone": tz})
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


def reassign(client, pid, rid, token, key, target_pid, start, end, expected_revision):
    return client.post(
        f"/points/{pid}/reservations/{rid}/reassign",
        json={
            "cancel_token": token,
            "idempotency_key": key,
            "expected_revision": expected_revision,
            "target_point_id": target_pid,
            "start": iso(start),
            "end": iso(end),
        },
    )


def cancel(client, pid, rid, token):
    return client.post(
        f"/points/{pid}/reservations/{rid}/cancel",
        json={"cancel_token": token},
    )


def waitlist(client, pid, key, start, end):
    return client.post(
        f"/points/{pid}/waitlist",
        json={"idempotency_key": key, "start": iso(start), "end": iso(end)},
    )


def book_one(client, key="r-1", start=None, end=None):
    pid = open_point(client)
    start = start or at(9)
    end = end or at(10)
    created = reserve(client, pid, key, start, end)
    assert created.status_code == 201, created.text
    return pid, created.json()


class _FrozenDateTime:
    """datetime stand-in whose now(tz) returns one fixed instant."""

    def __init__(self, frozen):
        self._frozen = frozen

    def now(self, tz=None):
        if tz is None:
            return self._frozen.replace(tzinfo=None)
        return self._frozen.astimezone(tz)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_reassign_success_cancels_source_and_books_target(client):
    src_pid, resv = book_one(client)
    tgt_pid = open_point(client)
    r = reassign(
        client, src_pid, resv["id"], resv["cancel_token"], "mv-1",
        tgt_pid, at(10), at(11), expected_revision=1,
    )
    assert r.status_code == 200, r.text
    body = r.json()

    source = body["source_reservation"]
    assert source["id"] == resv["id"]
    assert source["point_id"] == src_pid
    assert source["status"] == "cancelled"
    assert source["cancelled_at"] is not None
    assert source["start"] == iso(at(9))  # original window, unchanged
    assert "cancel_token" not in source

    target = body["target_reservation"]
    assert target["id"] != resv["id"]  # a brand-new reservation number
    assert target["point_id"] == tgt_pid
    assert target["status"] == "active"
    assert target["revision"] == 1
    assert target["start"] == iso(at(10))
    assert target["end"] == iso(at(11))
    assert target["cancel_token"]  # fresh cancel token issued
    assert target["cancel_token"] != resv["cancel_token"]
    assert body["target_rules_version"] == target["rules_version"] == 1


def test_reassign_releases_the_source_window(client):
    src_pid, resv = book_one(client)
    tgt_pid = open_point(client)
    r = reassign(
        client, src_pid, resv["id"], resv["cancel_token"], "mv",
        tgt_pid, at(10), at(11), 1,
    )
    assert r.status_code == 200, r.text
    # The original 09:00-10:00 window at the source point is bookable again.
    again = reserve(client, src_pid, "new-tenant", at(9), at(10))
    assert again.status_code == 201, again.text


def test_reassign_occupies_the_target_window(client):
    src_pid, resv = book_one(client)
    tgt_pid = open_point(client)
    r = reassign(
        client, src_pid, resv["id"], resv["cancel_token"], "mv",
        tgt_pid, at(10), at(11), 1,
    )
    assert r.status_code == 200, r.text
    # The new window at the target point is now taken.
    clash = reserve(client, tgt_pid, "squatter", at(10, 30), at(11, 30))
    assert clash.status_code == 409
    assert clash.json()["error"]["code"] == "reservation_overlap"


def test_reassign_records_the_target_rules_version(client):
    src_pid, resv = book_one(client)
    tgt_pid = open_point(client)
    # Publish a second version at the target; the reassign must validate
    # against (and report) the current version.
    weekday = at(0).isoweekday()
    pub = client.post(
        f"/points/{tgt_pid}/rules",
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
    r = reassign(
        client, src_pid, resv["id"], resv["cancel_token"], "mv",
        tgt_pid, at(8), at(9), 1,
    )
    assert r.status_code == 200, r.text
    assert r.json()["target_rules_version"] == 2
    assert r.json()["target_reservation"]["rules_version"] == 2


def test_reassign_target_reservation_is_cancelable_with_its_own_token(client):
    src_pid, resv = book_one(client)
    tgt_pid = open_point(client)
    body = reassign(
        client, src_pid, resv["id"], resv["cancel_token"], "mv",
        tgt_pid, at(10), at(11), 1,
    ).json()
    target = body["target_reservation"]
    r = cancel(client, tgt_pid, target["id"], target["cancel_token"])
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "cancelled"


def test_reassign_promotes_the_origin_waitlist(client):
    src_pid, resv = book_one(client)
    # Someone queues for the occupied source window.
    w = waitlist(client, src_pid, "w-1", at(9), at(10))
    assert w.status_code == 201, w.text
    assert w.json()["status"] == "waiting"
    wid, wtoken = w.json()["id"], w.json()["query_token"]

    tgt_pid = open_point(client)
    r = reassign(
        client, src_pid, resv["id"], resv["cancel_token"], "mv",
        tgt_pid, at(10), at(11), 1,
    )
    assert r.status_code == 200, r.text
    # The release promoted the origin waitlist entry in the same transaction.
    state = client.get(
        f"/points/{src_pid}/waitlist/{wid}", params={"query_token": wtoken}
    ).json()
    assert state["status"] == "promoted"
    assert state["reservation"]["start"] == iso(at(9))
    assert state["reservation"]["status"] == "active"


# ---------------------------------------------------------------------------
# Reassign idempotency
# ---------------------------------------------------------------------------

def test_same_reassign_key_same_content_replays_first_target_snapshot(client):
    src_pid, resv = book_one(client)
    tgt_pid = open_point(client)
    first = reassign(
        client, src_pid, resv["id"], resv["cancel_token"], "idem",
        tgt_pid, at(10), at(11), 1,
    )
    assert first.status_code == 200, first.text
    replay = reassign(
        client, src_pid, resv["id"], resv["cancel_token"], "idem",
        tgt_pid, at(10), at(11), 1,
    )
    assert replay.status_code == 200, replay.text
    assert replay.json() == first.json()
    # No second target reservation was booked: the window is held exactly once.
    clash = reserve(client, tgt_pid, "adjacent", at(10, 30), at(11, 30))
    assert clash.status_code == 409
    assert clash.json()["error"]["code"] == "reservation_overlap"


def test_reassign_replay_after_target_cancel_reports_live_status(client):
    src_pid, resv = book_one(client)
    tgt_pid = open_point(client)
    first = reassign(
        client, src_pid, resv["id"], resv["cancel_token"], "idem",
        tgt_pid, at(10), at(11), 1,
    )
    assert first.status_code == 200, first.text
    target = first.json()["target_reservation"]
    cancel(client, tgt_pid, target["id"], target["cancel_token"])

    replay = reassign(
        client, src_pid, resv["id"], resv["cancel_token"], "idem",
        tgt_pid, at(10), at(11), 1,
    )
    assert replay.status_code == 200, replay.text
    body = replay.json()
    # First-generated snapshot: original window / revision 1 ...
    assert body["target_reservation"]["id"] == target["id"]
    assert body["target_reservation"]["revision"] == 1
    assert body["target_reservation"]["start"] == iso(at(10))
    # ... but the status reflects the live row.
    assert body["target_reservation"]["status"] == "cancelled"
    assert body["target_reservation"]["cancelled_at"] is not None
    assert body["source_reservation"]["status"] == "cancelled"


def test_same_reassign_key_different_content_conflicts(client):
    src_pid, resv = book_one(client)
    tgt_pid = open_point(client)
    ok = reassign(
        client, src_pid, resv["id"], resv["cancel_token"], "idem",
        tgt_pid, at(10), at(11), 1,
    )
    assert ok.status_code == 200, ok.text
    for different in (
        dict(start=at(10, 30), end=at(11, 30)),          # different window
        dict(target_pid=open_point(client)),              # different target
    ):
        conflict = reassign(
            client, src_pid, resv["id"], resv["cancel_token"], "idem",
            different.get("target_pid", tgt_pid),
            different.get("start", at(10)),
            different.get("end", at(11)),
            1,
        )
        assert conflict.status_code == 409
        err = conflict.json()["error"]
        assert err["code"] == "idempotency_conflict"
        assert err["details"]["target_point_id"] == tgt_pid
        assert err["details"]["start"] == iso(at(10))


def test_reassign_keys_are_scoped_per_source_reservation(client):
    p1, r1 = book_one(client, key="a")
    p2, r2 = book_one(client, key="b")
    t1, t2 = open_point(client), open_point(client)
    resp1 = reassign(client, p1, r1["id"], r1["cancel_token"], "shared", t1, at(10), at(11), 1)
    resp2 = reassign(client, p2, r2["id"], r2["cancel_token"], "shared", t2, at(10), at(11), 1)
    assert resp1.status_code == 200, resp1.text
    assert resp2.status_code == 200, resp2.text


def test_reassign_key_is_independent_of_creation_and_reschedule_keys(client):
    src_pid, resv = book_one(client, key="create-key")
    tgt_pid = open_point(client)
    # Reusing the creation key as a reassign key is a separate namespace.
    r = reassign(
        client, src_pid, resv["id"], resv["cancel_token"], "create-key",
        tgt_pid, at(10), at(11), 1,
    )
    assert r.status_code == 200, r.text


def test_creation_key_replay_after_reassign_returns_first_created_snapshot(client):
    src_pid, resv = book_one(client, key="orig")
    tgt_pid = open_point(client)
    r = reassign(
        client, src_pid, resv["id"], resv["cancel_token"], "mv",
        tgt_pid, at(10), at(11), 1,
    )
    assert r.status_code == 200, r.text
    replay = reserve(client, src_pid, "orig", at(9), at(10))
    assert replay.status_code == 200
    body = replay.json()
    assert body["id"] == resv["id"]
    assert body["start"] == iso(at(9))
    assert body["end"] == iso(at(10))
    assert body["revision"] == 1
    assert body["cancel_token"] == resv["cancel_token"]
    assert body["status"] == "cancelled"  # live status


# ---------------------------------------------------------------------------
# Validation failures leave both points untouched
# ---------------------------------------------------------------------------

def test_reassign_to_same_point_is_invalid_request(client):
    src_pid, resv = book_one(client)
    r = reassign(
        client, src_pid, resv["id"], resv["cancel_token"], "same-point",
        src_pid, at(10), at(11), 1,
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_request"
    # Source reservation untouched.
    state = cancel(client, src_pid, resv["id"], resv["cancel_token"]).json()
    assert state["revision"] == 1


def test_reassign_unknown_target_point_is_404(client):
    src_pid, resv = book_one(client)
    r = reassign(
        client, src_pid, resv["id"], resv["cancel_token"], "ghost",
        999999, at(10), at(11), 1,
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "point_not_found"


def test_reassign_unknown_source_point_is_404(client):
    r = reassign(client, 999999, 1, "tok", "k", 1, at(10), at(11), 1)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "point_not_found"


def test_reassign_unknown_reservation_is_404(client):
    src_pid = open_point(client)
    tgt_pid = open_point(client)
    r = reassign(client, src_pid, 999999, "tok", "ghost", tgt_pid, at(10), at(11), 1)
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "reservation_not_found"


def test_reassign_on_wrong_source_point_is_404(client):
    _pid, resv = book_one(client)
    other_src = open_point(client)
    tgt_pid = open_point(client)
    r = reassign(
        client, other_src, resv["id"], resv["cancel_token"], "x",
        tgt_pid, at(10), at(11), 1,
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "reservation_not_found"


def test_reassign_without_token_is_invalid_cancel_token(client):
    src_pid, resv = book_one(client)
    tgt_pid = open_point(client)
    r = client.post(
        f"/points/{src_pid}/reservations/{resv['id']}/reassign",
        json={
            "idempotency_key": "no-token",
            "expected_revision": 1,
            "target_point_id": tgt_pid,
            "start": iso(at(10)),
            "end": iso(at(11)),
        },
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "invalid_cancel_token"


def test_reassign_with_wrong_token_is_invalid_cancel_token(client):
    src_pid, resv = book_one(client)
    tgt_pid = open_point(client)
    r = reassign(client, src_pid, resv["id"], "wrong-token", "bad", tgt_pid, at(10), at(11), 1)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "invalid_cancel_token"


def test_cancelled_reservation_cannot_be_reassigned(client):
    src_pid, resv = book_one(client)
    token = resv["cancel_token"]
    assert cancel(client, src_pid, resv["id"], token).status_code == 200
    tgt_pid = open_point(client)
    r = reassign(client, src_pid, resv["id"], token, "after-cancel", tgt_pid, at(10), at(11), 1)
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "reservation_not_reschedulable"
    assert err["details"]["reason"] == "reservation_cancelled"


def test_started_reservation_cannot_be_reassigned(client, monkeypatch):
    import app.main as main_module

    src_pid, resv = book_one(client, start=at(9), end=at(11))
    frozen = at(9, 30)
    monkeypatch.setattr(main_module, "datetime", _FrozenDateTime(frozen))
    tgt_pid = open_point(client)
    r = reassign(
        client, src_pid, resv["id"], resv["cancel_token"], "too-late",
        tgt_pid, at(10), at(11), 1,
    )
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "reservation_not_reschedulable"
    assert err["details"]["reason"] == "reservation_started"


def test_stale_expected_revision_conflicts(client):
    src_pid, resv = book_one(client)
    token = resv["cancel_token"]
    # Move once in place to reach revision 2.
    r = client.post(
        f"/points/{src_pid}/reservations/{resv['id']}/reschedule",
        json={
            "cancel_token": token,
            "idempotency_key": "bump",
            "expected_revision": 1,
            "start": iso(at(10)),
            "end": iso(at(11)),
        },
    )
    assert r.status_code == 200, r.text
    tgt_pid = open_point(client)
    stale = reassign(client, src_pid, resv["id"], token, "mv", tgt_pid, at(8), at(9), 1)
    assert stale.status_code == 409
    err = stale.json()["error"]
    assert err["code"] == "revision_conflict"
    assert err["details"]["expected_revision"] == 1
    assert err["details"]["current_revision"] == 2
    # Source reservation still active at revision 2.
    state = cancel(client, src_pid, resv["id"], token).json()
    assert state["revision"] == 2


def test_reassign_invalid_window_is_invalid_period(client):
    src_pid, resv = book_one(client)
    token = resv["cancel_token"]
    tgt_pid = open_point(client)
    # end not after start
    r = reassign(client, src_pid, resv["id"], token, "w1", tgt_pid, at(10), at(10), 1)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_period"
    # longer than two hours
    r = reassign(client, src_pid, resv["id"], token, "w2", tgt_pid, at(8), at(10, 1), 1)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_period"
    # start in the past
    past = datetime.now(UTC) - timedelta(minutes=30)
    r = reassign(
        client, src_pid, resv["id"], token, "w3", tgt_pid,
        past, past + timedelta(hours=1), 1,
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_period"
    # beyond the 30-day horizon
    far = datetime.now(UTC) + timedelta(days=31)
    r = reassign(
        client, src_pid, resv["id"], token, "w4", tgt_pid,
        far, far + timedelta(hours=1), 1,
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_period"


def test_reassign_naive_timestamps_are_invalid_request(client):
    src_pid, resv = book_one(client)
    tgt_pid = open_point(client)
    r = client.post(
        f"/points/{src_pid}/reservations/{resv['id']}/reassign",
        json={
            "cancel_token": resv["cancel_token"],
            "idempotency_key": "naive",
            "expected_revision": 1,
            "target_point_id": tgt_pid,
            "start": "2026-10-01T10:00:00",
            "end": "2026-10-01T11:00:00",
        },
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_request"


def test_reassign_to_point_without_published_rules_conflicts(client):
    src_pid, resv = book_one(client)
    tgt_pid = create_point(client)  # no rules published
    r = reassign(
        client, src_pid, resv["id"], resv["cancel_token"], "mv",
        tgt_pid, at(10), at(11), 1,
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "no_published_version"


def test_reassign_outside_target_open_hours_rejected_and_nothing_changes(client):
    src_pid, resv = book_one(client)
    tgt_pid = open_point(client)  # open 08:00-12:00 tomorrow
    r = reassign(
        client, src_pid, resv["id"], resv["cancel_token"], "late",
        tgt_pid, at(13), at(14), 1,
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "outside_open_hours"
    # Source still active and holding its window; target still free.
    clash = reserve(client, src_pid, "squatter", at(9), at(10))
    assert clash.status_code == 409
    assert clash.json()["error"]["code"] == "reservation_overlap"
    free = reserve(client, tgt_pid, "target-tenant", at(13), at(14))
    assert free.status_code == 409  # outside hours there too, but not occupied
    assert free.json()["error"]["code"] == "outside_open_hours"
    state = cancel(client, src_pid, resv["id"], resv["cancel_token"]).json()
    assert state["revision"] == 1
    assert state["start"] == iso(at(9))


def test_reassign_overlapping_target_reservation_rejected_and_nothing_changes(client):
    src_pid, resv = book_one(client)
    tgt_pid = open_point(client)
    holder = reserve(client, tgt_pid, "holder", at(10), at(11))
    assert holder.status_code == 201, holder.text
    r = reassign(
        client, src_pid, resv["id"], resv["cancel_token"], "onto-holder",
        tgt_pid, at(10, 30), at(11, 30), 1,
    )
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "reservation_overlap"
    assert err["details"]["conflicting_reservation_ids"] == [holder.json()["id"]]
    # Source untouched, target occupancy unchanged.
    state = cancel(client, src_pid, resv["id"], resv["cancel_token"]).json()
    assert state["revision"] == 1
    assert state["start"] == iso(at(9))


def test_reassign_touching_target_reservation_is_allowed(client):
    src_pid, resv = book_one(client)
    tgt_pid = open_point(client)
    reserve(client, tgt_pid, "neighbor", at(11), at(12))
    r = reassign(
        client, src_pid, resv["id"], resv["cancel_token"], "touch",
        tgt_pid, at(10), at(11), 1,
    )
    assert r.status_code == 200, r.text


def test_reassign_validates_against_target_timezone_calendar(client):
    # Source in UTC; target on a Shanghai calendar (UTC+8).  09:00-10:00 UTC
    # is 17:00-18:00 local — outside a 08:00-12:00 local opening.
    src_pid, resv = book_one(client)
    tomorrow = (datetime.now(UTC) + timedelta(days=1)).date()
    tgt_pid = open_point(
        client,
        weekly=[
            {
                "weekday": tomorrow.isoweekday(),
                "start_time": "08:00",
                "end_time": "12:00",
            }
        ],
        tz="Asia/Shanghai",
    )
    r = reassign(
        client, src_pid, resv["id"], resv["cancel_token"], "tz",
        tgt_pid, at(9), at(10), 1,
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "outside_open_hours"
    # 01:00-02:00 UTC is 09:00-10:00 in Shanghai: inside the opening.
    ok = reassign(
        client, src_pid, resv["id"], resv["cancel_token"], "tz-ok",
        tgt_pid, at(1), at(2), 1,
    )
    assert ok.status_code == 200, ok.text


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------

def test_concurrent_reassigns_of_one_reservation_exactly_one_wins(client):
    src_pid, resv = book_one(client)
    token = resv["cancel_token"]
    t1, t2 = open_point(client), open_point(client)
    barrier = threading.Barrier(2)
    outcomes = []

    def attempt(key, target):
        barrier.wait()
        r = reassign(client, src_pid, resv["id"], token, key, target, at(10), at(11), 1)
        outcomes.append((r.status_code, r.json()))

    threads = [
        threading.Thread(target=attempt, args=("c1", t1)),
        threading.Thread(target=attempt, args=("c2", t2)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    statuses = sorted(s for s, _ in outcomes)
    assert statuses == [200, 409]
    # The loser observes the winner's committed cancellation (or, had the
    # winner only bumped the revision, a stale revision): either way a
    # distinguishable 409, never a 500.
    loser = next(b for s, b in outcomes if s == 409)
    assert loser["error"]["code"] in (
        "reservation_not_reschedulable",
        "revision_conflict",
    )
    # Exactly one reassign landed; the source is cancelled, revision still 1.
    winner = next(b for s, b in outcomes if s == 200)
    assert winner["source_reservation"]["status"] == "cancelled"


def test_concurrent_opposite_direction_reassigns_no_deadlock_no_double_booking(client):
    # r1 lives on point A, r2 on point B; reassign r1 -> B and r2 -> A at
    # non-overlapping windows.  Both must complete (no deadlock, no 500).
    pid_a, r1 = book_one(client, key="a", start=at(9), end=at(10))
    pid_b, r2 = book_one(client, key="b", start=at(9), end=at(10))
    barrier = threading.Barrier(2)
    outcomes = {}

    def a_to_b():
        barrier.wait()
        outcomes["a2b"] = reassign(
            client, pid_a, r1["id"], r1["cancel_token"], "a2b",
            pid_b, at(10), at(11), 1,
        ).status_code

    def b_to_a():
        barrier.wait()
        outcomes["b2a"] = reassign(
            client, pid_b, r2["id"], r2["cancel_token"], "b2a",
            pid_a, at(10), at(11), 1,
        ).status_code

    threads = [threading.Thread(target=a_to_b), threading.Thread(target=b_to_a)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive(), "deadlock suspected"
    assert outcomes["a2b"] == 200
    assert outcomes["b2a"] == 200


def test_concurrent_reassign_and_target_booking_no_double_occupancy(client):
    src_pid, resv = book_one(client)
    tgt_pid = open_point(client)
    barrier = threading.Barrier(2)
    outcomes = {}

    def do_reassign():
        barrier.wait()
        r = reassign(
            client, src_pid, resv["id"], resv["cancel_token"], "mv",
            tgt_pid, at(10), at(11), 1,
        )
        outcomes["reassign"] = (r.status_code, r.json())

    def do_book():
        barrier.wait()
        r = reserve(client, tgt_pid, "race", at(10), at(11))
        outcomes["book"] = (r.status_code, r.json())

    threads = [threading.Thread(target=do_reassign), threading.Thread(target=do_book)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    rs_status, _ = outcomes["reassign"]
    bk_status, bk_body = outcomes["book"]
    # Exactly one of them got the 10:00-11:00 window at the target point.
    if rs_status == 200:
        assert bk_status == 409
        assert bk_body["error"]["code"] == "reservation_overlap"
    else:
        assert bk_status == 201
        assert outcomes["reassign"][1]["error"]["code"] == "reservation_overlap"
        # The failed reassign left the source reservation active.
        state = cancel(client, src_pid, resv["id"], resv["cancel_token"]).json()
        assert state["revision"] == 1


def test_concurrent_reassign_and_breaking_publish_at_target_exactly_one_wins(client):
    src_pid, resv = book_one(client)
    tgt_pid = open_point(client)
    barrier = threading.Barrier(2)
    outcomes = {}

    def do_reassign():
        barrier.wait()
        r = reassign(
            client, src_pid, resv["id"], resv["cancel_token"], "mv",
            tgt_pid, at(10), at(11), 1,
        )
        outcomes["reassign"] = (r.status_code, r.json())

    def do_publish():
        barrier.wait()
        r = client.post(
            f"/points/{tgt_pid}/rules",
            json={
                "expected_version": 1,
                "rules": {"weekly": [], "closed_dates": [], "makeup": []},
            },
        )
        outcomes["publish"] = (r.status_code, r.json())

    threads = [threading.Thread(target=do_reassign), threading.Thread(target=do_publish)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    rs_status, rs_body = outcomes["reassign"]
    pub_status, pub_body = outcomes["publish"]
    if rs_status == 200:
        # Reassign committed first; emptying the target calendar would
        # un-cover the new reservation, so the publish is refused.
        assert pub_status == 409
        assert pub_body["error"]["code"] == "reservation_change_conflict"
    else:
        # Publish committed first; the window is no longer open.
        assert rs_status == 409
        assert rs_body["error"]["code"] == "outside_open_hours"
        assert pub_status == 201
        # Source reservation untouched by the failed reassign.
        state = cancel(client, src_pid, resv["id"], resv["cancel_token"]).json()
        assert state["revision"] == 1
