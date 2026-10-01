"""End-to-end tests for cross-point reassignment (到访改派)."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def create_point(client, tz="UTC", name="改派测试点"):
    r = client.post("/points", json={"name": name, "timezone": tz})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def open_point(client, weekly=None, tz="UTC", name="改派测试点"):
    """Point with a rule covering ``weekly``; default 08:00-12:00 tomorrow."""
    pid = create_point(client, tz=tz, name=name)
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
                "makeup": [],
            },
        },
    )
    assert r.status_code == 201, r.text
    return pid


def at(hour, minute=0, day_offset=1):
    day = (datetime.now(UTC) + timedelta(days=day_offset)).date()
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


def iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def reserve(client, pid, key, start, end):
    return client.post(
        f"/points/{pid}/reservations",
        json={"idempotency_key": key, "start": iso(start), "end": iso(end)},
    )


def reassign(
    client, pid, rid, token, key, target_pid, start, end, expected_revision
):
    return client.post(
        f"/points/{pid}/reservations/{rid}/reassign",
        json={
            "cancel_token": token,
            "target_point_id": target_pid,
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
    pid = open_point(client, name="原点")
    start = start or at(9)
    end = end or at(10)
    created = reserve(client, pid, key, start, end)
    assert created.status_code == 201, created.text
    return pid, created.json()


def two_points(client, src_window=(at(9), at(10)), dst_window=(at(9), at(10))):
    src_pid, resv = book_one(
        client, key="src-visit", start=src_window[0], end=src_window[1]
    )
    dst_pid = open_point(client, name="目标点")
    return src_pid, dst_pid, resv


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_reassign_success_cancels_source_and_books_target(client):
    src_pid, dst_pid, resv = two_points(client)
    new_start, new_end = at(10), at(11)
    r = reassign(
        client, src_pid, resv["id"], resv["cancel_token"], "move-across",
        dst_pid, new_start, new_end, expected_revision=1,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["source_point_id"] == src_pid
    assert body["target_point_id"] == dst_pid
    assert body["target_rules_version"] == 1

    source = body["source"]
    assert source["id"] == resv["id"]
    assert source["status"] == "cancelled"
    assert source["cancelled_at"] is not None
    assert source["start"] == iso(at(9))
    assert "cancel_token" not in source

    new = body["new_reservation"]
    assert new["id"] != resv["id"]
    assert new["point_id"] == dst_pid
    assert new["status"] == "active"
    assert new["revision"] == 1
    assert new["start"] == iso(new_start)
    assert new["end"] == iso(new_end)
    assert new["rules_version"] == 1
    assert new["cancel_token"]
    assert new["cancel_token"] != resv["cancel_token"]


def test_reassign_frees_source_window_and_occupies_target(client):
    src_pid, dst_pid, resv = two_points(client)
    ok = reassign(
        client, src_pid, resv["id"], resv["cancel_token"], "k",
        dst_pid, at(10), at(11), 1,
    )
    assert ok.status_code == 201, ok.text

    # Source window released: someone else can book it.
    again = reserve(client, src_pid, "replacement", at(9), at(10))
    assert again.status_code == 201, again.text

    # Target window occupied: the same window now overlaps.
    clash = reserve(client, dst_pid, "squatter", at(10), at(11))
    assert clash.status_code == 409
    assert clash.json()["error"]["code"] == "reservation_overlap"


def test_reassign_can_move_reservation_that_was_rescheduled(client):
    src_pid, resv = book_one(client)
    dst_pid = open_point(client)
    token = resv["cancel_token"]
    moved = client.post(
        f"/points/{src_pid}/reservations/{resv['id']}/reschedule",
        json={
            "cancel_token": token,
            "idempotency_key": "same-point-move",
            "expected_revision": 1,
            "start": iso(at(10)),
            "end": iso(at(11)),
        },
    )
    assert moved.status_code == 200, moved.text
    r = reassign(
        client, src_pid, resv["id"], token, "cross-point", dst_pid,
        at(9), at(9, 30), expected_revision=2,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["source"]["revision"] == 2
    assert body["new_reservation"]["revision"] == 1


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def test_same_reassign_key_same_content_replays_first_snapshot(client):
    src_pid, dst_pid, resv = two_points(client)
    payload = dict(
        token=resv["cancel_token"], key="idem", target=dst_pid,
        start=at(10), end=at(11), revision=1,
    )
    first = reassign(
        client, src_pid, resv["id"], payload["token"], payload["key"],
        payload["target"], payload["start"], payload["end"], payload["revision"],
    )
    assert first.status_code == 201
    replay = reassign(
        client, src_pid, resv["id"], payload["token"], payload["key"],
        payload["target"], payload["start"], payload["end"], payload["revision"],
    )
    assert replay.status_code == 200, replay.text
    first_body, replay_body = first.json(), replay.json()
    assert replay_body["new_reservation"]["id"] == first_body["new_reservation"]["id"]
    assert (
        replay_body["new_reservation"]["cancel_token"]
        == first_body["new_reservation"]["cancel_token"]
    )
    assert replay_body["source"] == first_body["source"]
    assert replay_body["target_rules_version"] == 1


def test_reassign_replay_returns_live_status_of_new_reservation(client):
    src_pid, dst_pid, resv = two_points(client)
    first = reassign(
        client, src_pid, resv["id"], resv["cancel_token"], "idem",
        dst_pid, at(10), at(11), 1,
    ).json()
    new = first["new_reservation"]
    # Cancel the new (target) reservation with its own fresh token.
    cancelled = cancel(client, dst_pid, new["id"], new["cancel_token"])
    assert cancelled.status_code == 200
    replay = reassign(
        client, src_pid, resv["id"], resv["cancel_token"], "idem",
        dst_pid, at(10), at(11), 1,
    )
    assert replay.status_code == 200
    replayed_new = replay.json()["new_reservation"]
    assert replayed_new["id"] == new["id"]
    assert replayed_new["status"] == "cancelled"
    assert replayed_new["cancelled_at"] is not None
    # The window / rules version / revision stay pinned to the first attempt.
    assert replayed_new["start"] == iso(at(10))
    assert replayed_new["revision"] == 1
    assert replay.json()["source"]["status"] == "cancelled"


def test_same_reassign_key_different_content_conflicts(client):
    src_pid, dst_pid, resv = two_points(client)
    ok = reassign(
        client, src_pid, resv["id"], resv["cancel_token"], "idem",
        dst_pid, at(10), at(11), 1,
    )
    assert ok.status_code == 201
    # Different window under the same reassignment key.
    conflict = reassign(
        client, src_pid, resv["id"], resv["cancel_token"], "idem",
        dst_pid, at(8), at(9), 1,
    )
    assert conflict.status_code == 409
    err = conflict.json()["error"]
    assert err["code"] == "idempotency_conflict"
    assert err["details"]["target_point_id"] == dst_pid
    assert err["details"]["start"] == iso(at(10))


def test_same_reassign_key_different_target_conflicts(client):
    src_pid, dst_pid, resv = two_points(client)
    third_pid = open_point(client, name="第三点")
    ok = reassign(
        client, src_pid, resv["id"], resv["cancel_token"], "idem",
        dst_pid, at(10), at(11), 1,
    )
    assert ok.status_code == 201
    conflict = reassign(
        client, src_pid, resv["id"], resv["cancel_token"], "idem",
        third_pid, at(10), at(11), 1,
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["code"] == "idempotency_conflict"


def test_reassign_keys_are_scoped_per_source_reservation(client):
    src1, dst1, r1 = two_points(client)
    src2, dst2, r2 = two_points(client)
    resp1 = reassign(
        client, src1, r1["id"], r1["cancel_token"], "shared", dst1, at(10), at(11), 1
    )
    resp2 = reassign(
        client, src2, r2["id"], r2["cancel_token"], "shared", dst2, at(10), at(11), 1
    )
    assert resp1.status_code == 201, resp1.text
    assert resp2.status_code == 201, resp2.text


# ---------------------------------------------------------------------------
# Original creation key still returns the first-created snapshot
# ---------------------------------------------------------------------------

def test_original_create_key_replay_after_reassign_returns_first_snapshot(client):
    src_pid, dst_pid, resv = two_points(client)
    assert reassign(
        client, src_pid, resv["id"], resv["cancel_token"], "k",
        dst_pid, at(10), at(11), 1,
    ).status_code == 201
    replay = reserve(client, src_pid, "src-visit", at(9), at(10))
    assert replay.status_code == 200
    body = replay.json()
    assert body["id"] == resv["id"]
    assert body["revision"] == 1
    assert body["start"] == iso(at(9))
    assert body["rules_version"] == 1
    # Status reflects the current state: the reassignment cancelled it.
    assert body["status"] == "cancelled"
    assert body["cancel_token"] == resv["cancel_token"]


def test_original_create_key_different_content_after_reassign_conflicts(client):
    src_pid, dst_pid, resv = two_points(client)
    assert reassign(
        client, src_pid, resv["id"], resv["cancel_token"], "k",
        dst_pid, at(10), at(11), 1,
    ).status_code == 201
    conflict = reserve(client, src_pid, "src-visit", at(11), at(11, 30))
    assert conflict.status_code == 409
    err = conflict.json()["error"]
    assert err["code"] == "idempotency_conflict"
    assert err["details"]["start"] == iso(at(9))


# ---------------------------------------------------------------------------
# State / revision / token / existence semantics
# ---------------------------------------------------------------------------

def test_stale_expected_revision_conflicts_and_changes_nothing(client):
    src_pid, resv = book_one(client)
    dst_pid = open_point(client)
    token = resv["cancel_token"]
    # Bump the revision with an ordinary reschedule first.
    assert client.post(
        f"/points/{src_pid}/reservations/{resv['id']}/reschedule",
        json={
            "cancel_token": token,
            "idempotency_key": "m1",
            "expected_revision": 1,
            "start": iso(at(10)),
            "end": iso(at(11)),
        },
    ).status_code == 200
    stale = reassign(
        client, src_pid, resv["id"], token, "stale", dst_pid, at(9), at(10), 1
    )
    assert stale.status_code == 409
    err = stale.json()["error"]
    assert err["code"] == "revision_conflict"
    assert err["details"]["expected_revision"] == 1
    assert err["details"]["current_revision"] == 2
    # Nothing moved: target window is still free, source still active at 09:00.
    assert reserve(client, dst_pid, "target-free", at(9), at(10)).status_code == 201
    state = cancel(client, src_pid, resv["id"], token).json()
    assert state["status"] == "cancelled"
    assert state["start"] == iso(at(10))
    assert state["revision"] == 2


def test_cancelled_reservation_cannot_be_reassigned(client):
    src_pid, resv = book_one(client)
    dst_pid = open_point(client)
    token = resv["cancel_token"]
    assert cancel(client, src_pid, resv["id"], token).status_code == 200
    r = reassign(
        client, src_pid, resv["id"], token, "late", dst_pid, at(10), at(11), 1
    )
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "reservation_not_reschedulable"
    assert err["details"]["reason"] == "reservation_cancelled"
    # Target untouched.
    assert reserve(client, dst_pid, "still-free", at(10), at(11)).status_code == 201


def test_started_reservation_cannot_be_reassigned(client, monkeypatch):
    import app.main as main_module

    src_pid, resv = book_one(client, start=at(9), end=at(11))
    dst_pid = open_point(client)
    monkeypatch.setattr(main_module, "datetime", _FrozenDateTime(at(9, 30)))
    r = reassign(
        client, src_pid, resv["id"], resv["cancel_token"], "too-late",
        dst_pid, at(10), at(11), 1,
    )
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "reservation_not_reschedulable"
    assert err["details"]["reason"] == "reservation_started"


class _FrozenDateTime:
    def __init__(self, frozen):
        self._frozen = frozen

    def now(self, tz=None):
        if tz is None:
            return self._frozen.replace(tzinfo=None)
        return self._frozen.astimezone(tz)


def test_reassign_without_token_is_403(client):
    src_pid, dst_pid, resv = two_points(client)
    r = client.post(
        f"/points/{src_pid}/reservations/{resv['id']}/reassign",
        json={
            "target_point_id": dst_pid,
            "idempotency_key": "no-token",
            "expected_revision": 1,
            "start": iso(at(10)),
            "end": iso(at(11)),
        },
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "invalid_cancel_token"


def test_reassign_with_wrong_token_is_403(client):
    src_pid, dst_pid, resv = two_points(client)
    r = reassign(
        client, src_pid, resv["id"], "wrong", "bad", dst_pid, at(10), at(11), 1
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "invalid_cancel_token"


def test_reassign_unknown_reservation_is_404(client):
    src_pid = open_point(client)
    dst_pid = open_point(client)
    r = reassign(
        client, src_pid, 999999, "x", "ghost", dst_pid, at(10), at(11), 1
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "reservation_not_found"


def test_reassign_reservation_on_wrong_source_point_is_404(client):
    src_pid, dst_pid, resv = two_points(client)
    other = open_point(client, name="无关点")
    r = reassign(
        client, other, resv["id"], resv["cancel_token"], "x", dst_pid,
        at(10), at(11), 1,
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "reservation_not_found"


def test_reassign_to_unknown_target_point_is_404(client):
    src_pid, resv = book_one(client)
    r = reassign(
        client, src_pid, resv["id"], resv["cancel_token"], "x", 999999,
        at(10), at(11), 1,
    )
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "point_not_found"


def test_reassign_to_the_same_point_is_invalid_period(client):
    src_pid, resv = book_one(client)
    r = reassign(
        client, src_pid, resv["id"], resv["cancel_token"], "self", src_pid,
        at(10), at(11), 1,
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_period"


def test_reassign_naive_timestamps_are_invalid_request(client):
    src_pid, dst_pid, resv = two_points(client)
    r = client.post(
        f"/points/{src_pid}/reservations/{resv['id']}/reassign",
        json={
            "cancel_token": resv["cancel_token"],
            "target_point_id": dst_pid,
            "idempotency_key": "naive",
            "expected_revision": 1,
            "start": "2026-10-01T10:00:00",
            "end": "2026-10-01T11:00:00",
        },
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_request"


# ---------------------------------------------------------------------------
# Target window validation
# ---------------------------------------------------------------------------

def test_reassign_outside_target_open_hours_changes_nothing(client):
    src_pid, resv = book_one(client)
    # Target point publishes rules but 13:00-14:00 is outside 08:00-12:00.
    dst_pid = open_point(client, name="目标点")
    r = reassign(
        client, src_pid, resv["id"], resv["cancel_token"], "late",
        dst_pid, at(13), at(14), 1,
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "outside_open_hours"
    # Source stays occupied; target stays empty.
    clash = reserve(client, src_pid, "squatter", at(9), at(10))
    assert clash.status_code == 409
    assert reserve(client, dst_pid, "target-free", at(9), at(10)).status_code == 201
    state = cancel(client, src_pid, resv["id"], resv["cancel_token"]).json()
    assert state["revision"] == 1
    assert state["start"] == iso(at(9))


def test_reassign_to_point_without_published_rules_is_no_published_version(client):
    src_pid, resv = book_one(client)
    dst_pid = create_point(client)
    r = reassign(
        client, src_pid, resv["id"], resv["cancel_token"], "k",
        dst_pid, at(10), at(11), 1,
    )
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "no_published_version"


def test_reassign_window_policy_failures_are_invalid_period(client):
    src_pid, resv = book_one(client)
    dst_pid = open_point(client)
    token = resv["cancel_token"]
    # end not after start
    r = reassign(client, src_pid, resv["id"], token, "w1", dst_pid, at(10), at(10), 1)
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_period"
    # longer than two hours
    r = reassign(client, src_pid, resv["id"], token, "w2", dst_pid, at(8), at(10, 1), 1)
    assert r.status_code == 422
    # start in the past
    past = datetime.now(UTC) - timedelta(minutes=30)
    r = reassign(
        client, src_pid, resv["id"], token, "w3", dst_pid,
        past, past + timedelta(hours=1), 1,
    )
    assert r.status_code == 422
    # beyond the 30-day horizon
    far = datetime.now(UTC) + timedelta(days=31)
    r = reassign(
        client, src_pid, resv["id"], token, "w4", dst_pid,
        far, far + timedelta(hours=1), 1,
    )
    assert r.status_code == 422


def test_reassign_overlapping_target_reservation_rejected(client):
    src_pid, resv = book_one(client)
    dst_pid = open_point(client)
    holder = reserve(client, dst_pid, "holds-10", at(10), at(11))
    assert holder.status_code == 201
    r = reassign(
        client, src_pid, resv["id"], resv["cancel_token"], "onto",
        dst_pid, at(10), at(11), 1,
    )
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "reservation_overlap"
    assert err["details"]["conflicting_reservation_ids"] == [holder.json()["id"]]
    # Source not released.
    clash = reserve(client, src_pid, "squatter", at(9), at(10))
    assert clash.status_code == 409


def test_reassign_touching_target_reservation_is_allowed(client):
    src_pid, resv = book_one(client)
    dst_pid = open_point(client)
    holder = reserve(client, dst_pid, "holds-09", at(9), at(10))
    assert holder.status_code == 201
    # Touching (start exactly when the holder ends) is allowed.
    r = reassign(
        client, src_pid, resv["id"], resv["cancel_token"], "touch",
        dst_pid, at(10), at(11), 1,
    )
    assert r.status_code == 201, r.text


def test_reassign_uses_target_timezone_and_rules_version(client):
    # Target in Asia/Shanghai publishes a wide window; pass an offset-bearing
    # timestamp and confirm it is interpreted correctly against that calendar.
    src_pid, resv = book_one(client)
    tgt = create_point(client, tz="Asia/Shanghai", name="上海点")
    # Tomorrow in Shanghai: publish 09:00-17:00 local.  10:00 Shanghai is
    # 02:00 UTC on the same local date.
    tomorrow_local = (
        datetime.now(timezone(timedelta(hours=8))) + timedelta(days=1)
    ).date()
    pub = client.post(
        f"/points/{tgt}/rules",
        json={
            "expected_version": 0,
            "rules": {
                "weekly": [
                    {
                        "weekday": tomorrow_local.isoweekday(),
                        "start_time": "09:00",
                        "end_time": "17:00",
                    }
                ],
                "closed_dates": [],
                "makeup": [],
            },
        },
    )
    assert pub.status_code == 201, pub.text
    start = datetime(
        tomorrow_local.year, tomorrow_local.month, tomorrow_local.day,
        10, 0, tzinfo=timezone(timedelta(hours=8)),
    )
    end = start + timedelta(minutes=30)
    r = reassign(
        client, src_pid, resv["id"], resv["cancel_token"], "to-shanghai",
        tgt, start.astimezone(UTC), end.astimezone(UTC), 1,
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["target_rules_version"] == 1
    assert body["new_reservation"]["start"] == iso(start.astimezone(UTC))


def test_reassign_against_newly_published_target_version(client):
    src_pid, resv = book_one(client)
    dst_pid = open_point(client)
    weekday = at(0).isoweekday()
    pub = client.post(
        f"/points/{dst_pid}/rules",
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
        client, src_pid, resv["id"], resv["cancel_token"], "v2",
        dst_pid, at(13), at(14), 1,
    )
    assert r.status_code == 201, r.text
    assert r.json()["target_rules_version"] == 2
    assert r.json()["new_reservation"]["rules_version"] == 2


# ---------------------------------------------------------------------------
# Same-transaction waitlist promotion on the origin point
# ---------------------------------------------------------------------------

def test_reassign_promotes_origin_waitlist_in_same_transaction(client):
    src_pid = open_point(client, name="原点")
    holder = reserve(client, src_pid, "holder", at(9), at(10)).json()
    waiter = client.post(
        f"/points/{src_pid}/waitlist",
        json={
            "idempotency_key": "wait-9",
            "start": iso(at(9)),
            "end": iso(at(10)),
        },
    )
    assert waiter.status_code == 201, waiter.text
    wid, wtoken = waiter.json()["id"], waiter.json()["query_token"]

    dst_pid = open_point(client, name="目标点")
    r = reassign(
        client, src_pid, holder["id"], holder["cancel_token"], "cross",
        dst_pid, at(11), at(11, 30), 1,
    )
    assert r.status_code == 201, r.text

    state = client.get(
        f"/points/{src_pid}/waitlist/{wid}?query_token={wtoken}"
    ).json()
    assert state["status"] == "promoted"
    promoted_resv = state["reservation"]
    assert promoted_resv is not None
    assert promoted_resv["start"] == iso(at(9))
    assert promoted_resv["point_id"] == src_pid
    assert promoted_resv["id"] != holder["id"]


def test_reassign_failure_does_not_promote_origin_waitlist(client, monkeypatch):
    import app.db as db_module

    src_pid = open_point(client, name="原点")
    holder = reserve(client, src_pid, "holder", at(9), at(10)).json()
    waiter = client.post(
        f"/points/{src_pid}/waitlist",
        json={
            "idempotency_key": "wait-9",
            "start": iso(at(9)),
            "end": iso(at(10)),
        },
    ).json()
    dst_pid = open_point(client, name="目标点")

    real_build = db_module.build_open_intervals

    def boom(*args, **kwargs):
        raise RuntimeError("sweep failure simulated")

    # Fail inside the origin waitlist sweep, after target booking would have
    # happened: the whole transaction must roll back.
    monkeypatch.setattr(db_module, "build_open_intervals", boom)
    with pytest.raises(RuntimeError, match="sweep failure simulated"):
        reassign(
            client, src_pid, holder["id"], holder["cancel_token"], "cross",
            dst_pid, at(11), at(11, 30), 1,
        )
    monkeypatch.setattr(db_module, "build_open_intervals", real_build)

    # Origin reservation still active and occupies the window...
    clash = reserve(client, src_pid, "squatter", at(9), at(9, 30))
    assert clash.status_code == 409
    # ...waitlist entry is still waiting...
    state = client.get(
        f"/points/{src_pid}/waitlist/{waiter['id']}?query_token={waiter['query_token']}"
    ).json()
    assert state["status"] == "waiting"
    # ...and the target slot is still free (target reservation rolled back).
    assert reserve(client, dst_pid, "target-free", at(11), at(11, 30)).status_code == 201


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------

def test_concurrent_reassign_of_same_reservation_exactly_one_wins(client):
    src_pid, dst_pid, resv = two_points(client)
    # Two distinct target windows so the requests have different idempotency
    # keys and are not replays of one another.
    dst2 = open_point(client, name="目标点2")
    barrier = threading.Barrier(2)
    outcomes = []

    def attempt(key, target):
        barrier.wait()
        r = reassign(
            client, src_pid, resv["id"], resv["cancel_token"], key,
            target, at(10), at(11), 1,
        )
        outcomes.append((r.status_code, r.json()))

    threads = [
        threading.Thread(target=attempt, args=("k1", dst_pid)),
        threading.Thread(target=attempt, args=("k2", dst2)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    statuses = sorted(s for s, _ in outcomes)
    assert statuses == [201, 409]
    loser = next(b for s, b in outcomes if s == 409)
    assert loser["error"]["code"] == "reservation_not_reschedulable"
    # Exactly one target reservation landed; the losing target stays free.
    winner = next(b for s, b in outcomes if s == 201)
    winning_target = winner["target_point_id"]
    losing_target = dst2 if winning_target == dst_pid else dst_pid
    assert reserve(client, losing_target, "free", at(9), at(10)).status_code == 201


def test_concurrent_cross_reassignments_do_not_deadlock_or_double_book(client):
    # Point A holds reservation a at 09:00; point B holds reservation b at
    # 10:00.  Simultaneously move a -> B@11:00 and b -> A@11:00.  Lock order is
    # ascending point id, so this must finish without deadlock and without any
    # overlap.
    a_pid = open_point(client, name="A点")
    b_pid = open_point(client, name="B点")
    a = reserve(client, a_pid, "a", at(9), at(10)).json()
    b = reserve(client, b_pid, "b", at(10), at(11)).json()
    barrier = threading.Barrier(2)
    outcomes = []

    def move_a():
        barrier.wait()
        r = reassign(
            client, a_pid, a["id"], a["cancel_token"], "a-to-b",
            b_pid, at(11), at(11, 30), 1,
        )
        outcomes.append(("a", r.status_code, r.json()))

    def move_b():
        barrier.wait()
        r = reassign(
            client, b_pid, b["id"], b["cancel_token"], "b-to-a",
            a_pid, at(11), at(11, 30), 1,
        )
        outcomes.append(("b", r.status_code, r.json()))

    threads = [threading.Thread(target=move_a), threading.Thread(target=move_b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert all(not t.is_alive() for t in threads), "deadlock: a thread is stuck"

    statuses = {name: status for name, status, _ in outcomes}
    # Both move to disjoint 11:00 windows on different points, so both may
    # commit — and never 500.  (If lock ordering were wrong this would hang.)
    assert set(statuses) == {"a", "b"}
    for name, status, body in outcomes:
        assert status == 201, (name, body)

    # The target windows are occupied and cannot be double-booked.
    for pid in (a_pid, b_pid):
        clash = reserve(client, pid, f"squatter-{pid}", at(11), at(11, 30))
        assert clash.status_code == 409, clash.text


def test_concurrent_reassign_and_target_booking_do_not_double_book(client):
    src_pid, resv = book_one(client)
    dst_pid = open_point(client)
    barrier = threading.Barrier(2)
    outcomes = {}

    def do_reassign():
        barrier.wait()
        r = reassign(
            client, src_pid, resv["id"], resv["cancel_token"], "cross",
            dst_pid, at(10), at(11), 1,
        )
        outcomes["reassign"] = (r.status_code, r.json())

    def do_book():
        barrier.wait()
        r = reserve(client, dst_pid, "racer", at(10), at(11))
        outcomes["book"] = (r.status_code, r.json())

    threads = [
        threading.Thread(target=do_reassign),
        threading.Thread(target=do_book),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    rs_status = outcomes["reassign"][0]
    bk_status = outcomes["book"][0]
    assert {rs_status, bk_status} == {201, 409}
    if rs_status == 409:
        assert outcomes["reassign"][1]["error"]["code"] == "reservation_overlap"
        # Failed reassignment kept the origin reservation occupied.
        clash = reserve(client, src_pid, "still-held", at(9), at(10))
        assert clash.status_code == 409


def test_concurrent_reassign_and_target_publish_are_consistent(client):
    src_pid, resv = book_one(client)
    dst_pid = open_point(client)
    barrier = threading.Barrier(2)
    outcomes = {}

    def do_reassign():
        barrier.wait()
        r = reassign(
            client, src_pid, resv["id"], resv["cancel_token"], "cross",
            dst_pid, at(10), at(11), 1,
        )
        outcomes["reassign"] = (r.status_code, r.json())

    def do_publish():
        barrier.wait()
        r = client.post(
            f"/points/{dst_pid}/rules",
            json={
                "expected_version": 1,
                "rules": {"weekly": [], "closed_dates": [], "makeup": []},
            },
        )
        outcomes["publish"] = (r.status_code, r.json())

    threads = [
        threading.Thread(target=do_reassign),
        threading.Thread(target=do_publish),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    rs_status, rs_body = outcomes["reassign"]
    pub_status, pub_body = outcomes["publish"]
    if rs_status == 201:
        # Reassignment committed under v1 while the reservation covers that
        # window; emptying the target calendar would un-cover the new
        # reservation, so the publish is refused.
        assert pub_status == 409
        assert pub_body["error"]["code"] == "reservation_change_conflict"
    else:
        # Publish committed first: the target window is no longer open.
        assert rs_status == 409
        assert rs_body["error"]["code"] == "outside_open_hours"
        assert pub_status == 201
        # The failed reassignment left the source reservation untouched.
        clash = reserve(client, src_pid, "still-held", at(9), at(10))
        assert clash.status_code == 409
        state = cancel(client, src_pid, resv["id"], resv["cancel_token"]).json()
        assert state["revision"] == 1
