"""End-to-end tests for the waitlist (候补) feature."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

UTC = timezone.utc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def create_point(client, tz="UTC"):
    r = client.post("/points", json={"name": "候补测试点", "timezone": tz})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


def at(hour, minute=0, day_offset=1):
    day = (datetime.now(UTC) + timedelta(days=day_offset)).date()
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)


def open_point(client, weekly=None, makeup=None, tz="UTC"):
    """Point with rules covering ``weekly``; default 08:00-12:00 tomorrow."""
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


def reserve(client, pid, key, start, end):
    return client.post(
        f"/points/{pid}/reservations",
        json={"idempotency_key": key, "start": iso(start), "end": iso(end)},
    )


def waitlist(client, pid, key, start, end):
    return client.post(
        f"/points/{pid}/waitlist",
        json={"idempotency_key": key, "start": iso(start), "end": iso(end)},
    )


def status(client, pid, wid, token):
    return client.get(
        f"/points/{pid}/waitlist/{wid}", params={"query_token": token}
    )


def withdraw(client, pid, wid, token):
    return client.post(
        f"/points/{pid}/waitlist/{wid}/withdraw",
        json={"query_token": token},
    )


def cancel(client, pid, rid, token):
    return client.post(
        f"/points/{pid}/reservations/{rid}/cancel",
        json={"cancel_token": token},
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


def occupied_slot(client, key="holder", start=None, end=None):
    """Point + active reservation holding 09:00-10:00 tomorrow by default."""
    pid = open_point(client)
    start = start or at(9)
    end = end or at(10)
    r = reserve(client, pid, key, start, end)
    assert r.status_code == 201, r.text
    return pid, r.json()


class _FrozenDateTime:
    def __init__(self, frozen):
        self._frozen = frozen

    def now(self, tz=None):
        if tz is None:
            return self._frozen.replace(tzinfo=None)
        return self._frozen.astimezone(tz)


# ---------------------------------------------------------------------------
# Application: eligibility
# ---------------------------------------------------------------------------


def test_waitlist_success_returns_number_token_and_queue_status(client):
    pid, holder = occupied_slot(client)
    r = waitlist(client, pid, "w-1", at(9), at(10))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["id"] > 0
    assert body["point_id"] == pid
    assert body["status"] == "waiting"
    assert body["position"] == 1
    assert body["query_token"]
    assert body["rules_version"] == 1
    assert body["start"] == iso(at(9))
    assert body["end"] == iso(at(10))
    assert body["reservation"] is None


def test_free_window_is_rejected_use_reservation_flow(client):
    pid = open_point(client)  # nothing booked, window is free
    r = waitlist(client, pid, "free", at(9), at(10))
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "waitlist_window_available"
    # The ordinary flow still works.
    ok = reserve(client, pid, "normal", at(9), at(10))
    assert ok.status_code == 201


def test_window_must_match_reservation_policy(client):
    pid, _ = occupied_slot(client)
    # end not after start
    r = waitlist(client, pid, "bad-1", at(10), at(10))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_period"
    # longer than the max duration
    r = waitlist(client, pid, "bad-2", at(8), at(10, 1))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_period"
    # start in the past
    past = datetime.now(UTC) - timedelta(minutes=30)
    r = waitlist(client, pid, "bad-3", past, past + timedelta(minutes=30))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_period"
    # beyond the lead-time horizon
    far = datetime.now(UTC) + timedelta(days=31)
    r = waitlist(client, pid, "bad-4", far, far + timedelta(hours=1))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_period"


def test_window_must_be_covered_by_current_calendar(client):
    pid, _ = occupied_slot(client)
    r = waitlist(client, pid, "late", at(13), at(14))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "outside_open_hours"


def test_no_published_rules_remains_no_published_version(client):
    pid = create_point(client)
    r = waitlist(client, pid, "x", at(9), at(10))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "no_published_version"


def test_naive_timestamps_are_invalid_request(client):
    pid, _ = occupied_slot(client)
    r = client.post(
        f"/points/{pid}/waitlist",
        json={
            "idempotency_key": "naive",
            "start": "2026-10-01T09:00:00",
            "end": "2026-10-01T10:00:00",
        },
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_request"


def test_partial_overlap_with_holder_is_enough_to_join(client):
    pid, holder = occupied_slot(client)
    # Overlaps the holder 09:00-10:00 by half an hour.
    r = waitlist(client, pid, "partial", at(9, 30), at(10, 30))
    assert r.status_code == 201, r.text
    # Touching does not overlap: the window is bookable, so no waitlist.
    r = waitlist(client, pid, "touch", at(10), at(11))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "waitlist_window_available"


def test_cancelled_holder_no_longer_blocks_apply(client):
    pid, holder = occupied_slot(client)
    cancel(client, pid, holder["id"], holder["cancel_token"])
    # The sweep promotes nothing for an empty queue; the window is free now.
    r = waitlist(client, pid, "latecomer", at(9), at(10))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "waitlist_window_available"


def test_waitlist_key_scoped_per_point(client):
    pid1, _ = occupied_slot(client)
    pid2, _ = occupied_slot(client)
    r1 = waitlist(client, pid1, "shared", at(9), at(10))
    r2 = waitlist(client, pid2, "shared", at(9), at(10))
    assert r1.status_code == 201 and r2.status_code == 201
    assert r1.json()["id"] != r2.json()["id"]


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_same_key_same_content_replays_original_application(client):
    pid, _ = occupied_slot(client)
    first = waitlist(client, pid, "idem", at(9), at(10))
    assert first.status_code == 201
    replay = waitlist(client, pid, "idem", at(9), at(10))
    assert replay.status_code == 200
    first_body, replay_body = first.json(), replay.json()
    assert replay_body["id"] == first_body["id"]
    assert replay_body["query_token"] == first_body["query_token"]
    assert replay_body["start"] == first_body["start"]
    assert replay_body["end"] == first_body["end"]
    assert replay_body["rules_version"] == 1


def test_same_key_different_content_conflicts(client):
    pid, _ = occupied_slot(client)
    assert waitlist(client, pid, "idem", at(9), at(10)).status_code == 201
    conflict = waitlist(client, pid, "idem", at(9, 30), at(10, 30))
    assert conflict.status_code == 409
    err = conflict.json()["error"]
    assert err["code"] == "idempotency_conflict"
    assert err["details"]["waitlist_id"] > 0
    assert err["details"]["start"] == iso(at(9))


# ---------------------------------------------------------------------------
# Queue positions
# ---------------------------------------------------------------------------


def test_positions_follow_application_order_and_move(client):
    pid, _ = occupied_slot(client)
    w1 = waitlist(client, pid, "q1", at(9, 15), at(9, 45)).json()
    w2 = waitlist(client, pid, "q2", at(9, 20), at(9, 50)).json()
    w3 = waitlist(client, pid, "q3", at(9, 25), at(9, 55)).json()
    assert w1["position"] == 1
    assert w2["position"] == 2
    assert w3["position"] == 3
    # Withdraw the first: the others move up.
    assert withdraw(client, pid, w1["id"], w1["query_token"]).status_code == 200
    assert status(client, pid, w2["id"], w2["query_token"]).json()["position"] == 1
    assert status(client, pid, w3["id"], w3["query_token"]).json()["position"] == 2


# ---------------------------------------------------------------------------
# Status query
# ---------------------------------------------------------------------------


def test_status_with_token_reports_waiting_state(client):
    pid, _ = occupied_slot(client)
    w = waitlist(client, pid, "s", at(9), at(10)).json()
    r = status(client, pid, w["id"], w["query_token"])
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "waiting"
    assert body["position"] == 1
    assert body["reservation"] is None


def test_status_missing_or_wrong_token_is_403(client):
    pid, _ = occupied_slot(client)
    w = waitlist(client, pid, "s", at(9), at(10)).json()
    r = client.get(f"/points/{pid}/waitlist/{w['id']}")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "invalid_waitlist_token"
    r = status(client, pid, w["id"], "wrong")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "invalid_waitlist_token"


def test_status_unknown_entry_or_wrong_point_is_404(client):
    pid, _ = occupied_slot(client)
    r = status(client, pid, 999999, "anything")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "waitlist_entry_not_found"
    other, _ = occupied_slot(client)
    w = waitlist(client, pid, "cross", at(9), at(10)).json()
    r = status(client, other, w["id"], w["query_token"])
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "waitlist_entry_not_found"


# ---------------------------------------------------------------------------
# Withdrawal
# ---------------------------------------------------------------------------


def test_withdraw_moves_entry_to_withdrawn_and_is_idempotent(client):
    pid, _ = occupied_slot(client)
    w = waitlist(client, pid, "w", at(9), at(10)).json()
    r = withdraw(client, pid, w["id"], w["query_token"])
    assert r.status_code == 200
    assert r.json()["status"] == "withdrawn"
    assert r.json()["position"] is None
    # Repeat withdrawal replays the withdrawn state.
    again = withdraw(client, pid, w["id"], w["query_token"])
    assert again.status_code == 200
    assert again.json()["status"] == "withdrawn"


def test_withdraw_without_or_with_wrong_token_is_403(client):
    pid, _ = occupied_slot(client)
    w = waitlist(client, pid, "w", at(9), at(10)).json()
    r = client.post(
        f"/points/{pid}/waitlist/{w['id']}/withdraw", json={}
    )
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "invalid_waitlist_token"
    r = withdraw(client, pid, w["id"], "wrong")
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "invalid_waitlist_token"


def test_withdrawn_entry_is_skipped_by_the_sweep(client):
    pid, holder = occupied_slot(client)
    w = waitlist(client, pid, "w", at(9), at(10)).json()
    withdraw(client, pid, w["id"], w["query_token"])
    cancel(client, pid, holder["id"], holder["cancel_token"])
    body = status(client, pid, w["id"], w["query_token"]).json()
    assert body["status"] == "withdrawn"
    # The slot was genuinely freed and stays bookable.
    assert reserve(client, pid, "after", at(9), at(10)).status_code == 201


# ---------------------------------------------------------------------------
# Promotion on cancellation
# ---------------------------------------------------------------------------


def test_cancel_promotes_earliest_matching_waitlist_entry(client):
    pid, holder = occupied_slot(client)
    w = waitlist(client, pid, "w", at(9), at(10)).json()
    r = cancel(client, pid, holder["id"], holder["cancel_token"])
    assert r.status_code == 200
    body = status(client, pid, w["id"], w["query_token"]).json()
    assert body["status"] == "promoted"
    assert body["position"] is None
    reservation = body["reservation"]
    assert reservation is not None
    assert reservation["id"] != holder["id"]
    assert reservation["status"] == "active"
    assert reservation["revision"] == 1
    assert reservation["start"] == iso(at(9))
    assert reservation["end"] == iso(at(10))
    assert reservation["cancel_token"]
    assert reservation["rules_version"] == 1


def test_promoted_reservation_token_can_cancel_and_reschedule(client):
    pid, holder = occupied_slot(client)
    w = waitlist(client, pid, "w", at(9), at(10)).json()
    cancel(client, pid, holder["id"], holder["cancel_token"])
    promoted = status(client, pid, w["id"], w["query_token"]).json()
    rid = promoted["reservation"]["id"]
    token = promoted["reservation"]["cancel_token"]
    moved = reschedule(
        client, pid, rid, token, "m", at(10), at(10, 30), expected_revision=1
    )
    assert moved.status_code == 200, moved.text
    assert moved.json()["revision"] == 2
    cx = cancel(client, pid, rid, token)
    assert cx.status_code == 200
    assert cx.json()["status"] == "cancelled"
    # The status view reflects the live cancelled reservation.
    body = status(client, pid, w["id"], w["query_token"]).json()
    assert body["status"] == "promoted"
    assert body["reservation"]["status"] == "cancelled"
    assert body["reservation"]["cancelled_at"] is not None


def test_sweep_promotes_at_most_one_entry_per_release(client):
    pid, holder = occupied_slot(client)
    w1 = waitlist(client, pid, "w1", at(9, 10), at(9, 50)).json()
    w2 = waitlist(client, pid, "w2", at(9, 20), at(9, 40)).json()
    cancel(client, pid, holder["id"], holder["cancel_token"])
    assert (
        status(client, pid, w1["id"], w1["query_token"]).json()["status"]
        == "promoted"
    )
    # The second applicant now overlaps the promoted reservation: stays queued.
    second = status(client, pid, w2["id"], w2["query_token"]).json()
    assert second["status"] == "waiting"
    assert second["position"] == 1


def test_sweep_skips_still_conflicting_then_promotes_next(client):
    pid, holder = occupied_slot(client)
    # A second active reservation covering 10:00-11:00 (touches the holder).
    other = reserve(client, pid, "other", at(10), at(11)).json()
    # w1 wants 09:30-10:30: freed by cancelling holder but still overlaps other.
    w1 = waitlist(client, pid, "w1", at(9, 30), at(10, 30)).json()
    # w2 wants 09:00-09:30: fully freed by cancelling holder.
    w2 = waitlist(client, pid, "w2", at(9), at(9, 30)).json()
    cancel(client, pid, holder["id"], holder["cancel_token"])
    first = status(client, pid, w1["id"], w1["query_token"]).json()
    assert first["status"] == "waiting"
    second = status(client, pid, w2["id"], w2["query_token"]).json()
    assert second["status"] == "promoted"
    assert second["reservation"]["id"] != holder["id"]
    # Releasing the other reservation later promotes the remaining w1.
    cancel(client, pid, other["id"], other["cancel_token"])
    first = status(client, pid, w1["id"], w1["query_token"]).json()
    assert first["status"] == "promoted"


def test_non_matching_cancel_does_not_promote(client):
    pid, holder = occupied_slot(client)
    other = reserve(client, pid, "far-key", at(11), at(11, 30)).json()
    w = waitlist(client, pid, "w", at(9), at(10)).json()
    # Cancelling the unrelated 11:00 reservation must not touch the queue.
    cancel(client, pid, other["id"], other["cancel_token"])
    body = status(client, pid, w["id"], w["query_token"]).json()
    assert body["status"] == "waiting"
    assert body["position"] == 1
    # The holder is still active.
    clash = reserve(client, pid, "clash", at(9), at(10))
    assert clash.status_code == 409


def test_expired_entries_are_marked_at_sweep_time(client, monkeypatch):
    import app.main as main_module

    pid, holder = occupied_slot(client)
    # e1 starts at 09:00 (will be in the past under the frozen sweep clock),
    # e2 starts at 09:40; both overlap the 09:00-10:00 holder at apply time.
    e1 = waitlist(client, pid, "e1", at(9), at(9, 20)).json()
    e2 = waitlist(client, pid, "e2", at(9, 40), at(10)).json()

    frozen = at(9, 30)  # e1's start has passed; e2 still lies ahead
    monkeypatch.setattr(main_module, "datetime", _FrozenDateTime(frozen))
    cancel(client, pid, holder["id"], holder["cancel_token"])

    expired = status(client, pid, e1["id"], e1["query_token"]).json()
    assert expired["status"] == "expired"
    assert expired["position"] is None
    assert expired["reservation"] is None
    promoted = status(client, pid, e2["id"], e2["query_token"]).json()
    assert promoted["status"] == "promoted"
    assert promoted["reservation"]["rules_version"] == 1


# ---------------------------------------------------------------------------
# Promotion on reschedule
# ---------------------------------------------------------------------------


def test_reschedule_release_promotes_matching_entry(client):
    pid, holder = occupied_slot(client)
    w = waitlist(client, pid, "w", at(9), at(10)).json()
    moved = reschedule(
        client,
        pid,
        holder["id"],
        holder["cancel_token"],
        "move",
        at(10),
        at(11),
        expected_revision=1,
    )
    assert moved.status_code == 200, moved.text
    body = status(client, pid, w["id"], w["query_token"]).json()
    assert body["status"] == "promoted"
    assert body["reservation"]["start"] == iso(at(9))


def test_promoted_entry_does_not_overlap_the_moved_reservation(client):
    pid, holder = occupied_slot(client)
    # Moves to 09:30-10:00; the queued 09:00-09:30 window only touches it.
    w = waitlist(client, pid, "w", at(9), at(9, 30)).json()
    r = reschedule(
        client,
        pid,
        holder["id"],
        holder["cancel_token"],
        "move",
        at(9, 30),
        at(10),
        expected_revision=1,
    )
    assert r.status_code == 200, r.text
    body = status(client, pid, w["id"], w["query_token"]).json()
    assert body["status"] == "promoted"
    # Both active reservations coexist (end-to-end touch) and the moving
    # reservation kept its id.
    assert body["reservation"]["id"] != holder["id"]
    assert reserve(client, pid, "gap?", at(9), at(9, 30)).status_code == 409
    assert reserve(client, pid, "touch", at(10), at(10, 30)).status_code == 201


def test_reschedule_replay_does_not_sweep_again(client):
    pid, holder = occupied_slot(client)
    w = waitlist(client, pid, "w", at(9), at(10)).json()
    body = {
        "cancel_token": holder["cancel_token"],
        "idempotency_key": "move",
        "expected_revision": 1,
        "start": iso(at(10)),
        "end": iso(at(11)),
    }
    first = client.post(
        f"/points/{pid}/reservations/{holder['id']}/reschedule", json=body
    )
    assert first.status_code == 200
    # A second queued entry must not be promoted by the idempotent replay.
    w2 = waitlist(client, pid, "w2", at(10), at(11)).json()
    replay = client.post(
        f"/points/{pid}/reservations/{holder['id']}/reschedule", json=body
    )
    assert replay.status_code == 200
    # The first entry was promoted by the real reschedule; the second entry is
    # not swept again by the idempotent replay.
    assert (
        status(client, pid, w["id"], w["query_token"]).json()["status"]
        == "promoted"
    )
    assert (
        status(client, pid, w2["id"], w2["query_token"]).json()["status"]
        == "waiting"
    )


# ---------------------------------------------------------------------------
# Rules version at promotion
# ---------------------------------------------------------------------------


def test_promotion_uses_the_rules_version_at_promotion_time(client):
    pid, holder = occupied_slot(client)
    w = waitlist(client, pid, "w", at(9), at(10)).json()
    weekday = at(0).isoweekday()
    pub = client.post(
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
    assert pub.status_code == 201, pub.text
    cancel(client, pid, holder["id"], holder["cancel_token"])
    body = status(client, pid, w["id"], w["query_token"]).json()
    assert body["rules_version"] == 1  # version seen at apply time
    assert body["reservation"]["rules_version"] == 2  # issued at promotion


def test_entry_left_queued_when_new_rules_close_its_window(client):
    # R 09:00-09:45 is the active reservation.  W 09:30-10:30 overlaps it and
    # is fully open under v1 (08:00-12:00).  v2 keeps R covered but trims the
    # opening to 10:15, so W's window is no longer fully open when R's
    # cancellation runs the sweep: W must stay in the queue.  Reopening later
    # and releasing another overlapping slot then promotes it.
    pid = open_point(client)
    holder = reserve(client, pid, "r", at(9), at(9, 45)).json()
    w = waitlist(client, pid, "w", at(9, 30), at(10, 30)).json()
    weekday = at(0).isoweekday()
    trim = client.post(
        f"/points/{pid}/rules",
        json={
            "expected_version": 1,
            "rules": {
                "weekly": [
                    {"weekday": weekday, "start_time": "08:00", "end_time": "10:15"}
                ],
                "closed_dates": [],
                "makeup": [],
            },
        },
    )
    assert trim.status_code == 201, trim.text
    cancel(client, pid, holder["id"], holder["cancel_token"])
    body = status(client, pid, w["id"], w["query_token"]).json()
    assert body["status"] == "waiting"
    assert body["position"] == 1

    # Reopen fully, then take and release a slot overlapping W: the sweep now
    # finds W open and free and promotes it (exactly once).
    reopen = client.post(
        f"/points/{pid}/rules",
        json={
            "expected_version": 2,
            "rules": {
                "weekly": [
                    {"weekday": weekday, "start_time": "08:00", "end_time": "12:00"}
                ],
                "closed_dates": [],
                "makeup": [],
            },
        },
    )
    assert reopen.status_code == 201, reopen.text
    second = reserve(client, pid, "r2", at(10), at(10, 30)).json()
    assert cancel(client, pid, second["id"], second["cancel_token"]).status_code == 200
    body = status(client, pid, w["id"], w["query_token"]).json()
    assert body["status"] == "promoted"
    assert body["reservation"]["rules_version"] == 3


# ---------------------------------------------------------------------------
# Exactly-once promotion / atomicity
# ---------------------------------------------------------------------------


def test_promotion_failure_rolls_back_the_cancellation(client, monkeypatch):
    import pytest

    import app.db as db_module
    import app.scheduler as scheduler_module

    pid, holder = occupied_slot(client)
    w = waitlist(client, pid, "w", at(9), at(10)).json()
    real_build = scheduler_module.build_open_intervals

    def boom(*args, **kwargs):
        raise RuntimeError("sweep failure simulated")

    monkeypatch.setattr(db_module, "build_open_intervals", boom)
    try:
        with pytest.raises(RuntimeError, match="sweep failure simulated"):
            cancel(client, pid, holder["id"], holder["cancel_token"])
    finally:
        monkeypatch.setattr(db_module, "build_open_intervals", real_build)

    # The release was rolled back together with the sweep:
    # the holder is still active and still blocks the window.
    clash = reserve(client, pid, "clash", at(9), at(10))
    assert clash.status_code == 409
    assert clash.json()["error"]["code"] == "reservation_overlap"
    body = status(client, pid, w["id"], w["query_token"]).json()
    assert body["status"] == "waiting"
    # And the cancellation did not consume its effect: retrying works and now
    # promotes the entry.
    again = cancel(client, pid, holder["id"], holder["cancel_token"])
    assert again.status_code == 200
    assert (
        status(client, pid, w["id"], w["query_token"]).json()["status"]
        == "promoted"
    )


# ---------------------------------------------------------------------------
# Backwards compatibility of the existing endpoints
# ---------------------------------------------------------------------------


def test_cancel_response_shape_is_unchanged(client):
    pid, holder = occupied_slot(client)
    r = cancel(client, pid, holder["id"], holder["cancel_token"])
    body = r.json()
    assert set(body) == {
        "id",
        "point_id",
        "status",
        "revision",
        "start",
        "end",
        "rules_version",
        "created_at",
        "cancelled_at",
    }
    assert body["status"] == "cancelled"
    assert "cancel_token" not in body


def test_repeat_cancel_does_not_sweep_and_stays_cancelled(client):
    pid, holder = occupied_slot(client)
    w = waitlist(client, pid, "w", at(9), at(10)).json()
    first = cancel(client, pid, holder["id"], holder["cancel_token"])
    assert first.status_code == 200
    again = cancel(client, pid, holder["id"], holder["cancel_token"])
    assert again.status_code == 200
    assert again.json()["status"] == "cancelled"
    # One sweep per real release: exactly one entry promoted.
    assert (
        status(client, pid, w["id"], w["query_token"]).json()["status"]
        == "promoted"
    )


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


def test_concurrent_cancel_and_booking_no_overlap_and_one_promotion(client):
    pid, holder = occupied_slot(client)
    w = waitlist(client, pid, "w", at(9), at(10)).json()
    barrier = threading.Barrier(2)
    outcomes = {}

    def do_cancel():
        barrier.wait()
        outcomes["cancel"] = cancel(
            client, pid, holder["id"], holder["cancel_token"]
        ).status_code

    def do_book():
        barrier.wait()
        outcomes["book"] = reserve(
            client, pid, "racer", at(9), at(10)
        ).status_code

    threads = [threading.Thread(target=do_cancel), threading.Thread(target=do_book)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes["cancel"] == 200
    # Either the sweep promoted the waitlist entry (blocking the racer) or the
    # racer booked the freed slot first (the entry stays queued) — never both.
    entry = status(client, pid, w["id"], w["query_token"]).json()
    if entry["status"] == "promoted":
        assert outcomes["book"] == 409
    else:
        assert outcomes["book"] == 201
        assert entry["status"] == "waiting"


def test_concurrent_cancels_promote_only_one_of_two_entries(client):
    # Two reservations touching end-to-end, each with one matching waiter; the
    # two cancellations race. Exactly two promotions land, no overlaps.
    pid = open_point(client)
    r1 = reserve(client, pid, "r1", at(9), at(10)).json()
    r2 = reserve(client, pid, "r2", at(10), at(11)).json()
    w1 = waitlist(client, pid, "w1", at(9), at(10)).json()
    w2 = waitlist(client, pid, "w2", at(10), at(11)).json()
    barrier = threading.Barrier(2)
    statuses = []

    def do_cancel(holder):
        barrier.wait()
        statuses.append(
            cancel(client, pid, holder["id"], holder["cancel_token"]).status_code
        )

    threads = [
        threading.Thread(target=do_cancel, args=(r1,)),
        threading.Thread(target=do_cancel, args=(r2,)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(statuses) == [200, 200]
    s1 = status(client, pid, w1["id"], w1["query_token"]).json()
    s2 = status(client, pid, w2["id"], w2["query_token"]).json()
    assert s1["status"] == "promoted"
    assert s2["status"] == "promoted"
    # The promoted reservations occupy disjoint, touching windows.
    new_id_1 = s1["reservation"]["id"]
    new_id_2 = s2["reservation"]["id"]
    assert new_id_1 != new_id_2
    assert reserve(client, pid, "full", at(9), at(11)).status_code == 409
    assert reserve(client, pid, "exact-1", at(9), at(10)).status_code == 409
    assert reserve(client, pid, "after", at(11), at(11, 30)).status_code == 201


def test_concurrent_waitlist_applies_serialize_under_point_lock(client):
    pid, holder = occupied_slot(client)
    barrier = threading.Barrier(2)
    bodies = []

    def apply(key):
        barrier.wait()
        bodies.append(waitlist(client, pid, key, at(9), at(10)).json())

    threads = [
        threading.Thread(target=apply, args=("c1",)),
        threading.Thread(target=apply, args=("c2",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert [b["position"] for b in sorted(bodies, key=lambda b: b["id"])] == [
        1,
        2,
    ]


def test_concurrent_withdraw_and_cancel_never_double_sells(client):
    pid, holder = occupied_slot(client)
    w = waitlist(client, pid, "w", at(9), at(10)).json()
    barrier = threading.Barrier(2)
    outcomes = {}

    def do_withdraw():
        barrier.wait()
        outcomes["withdraw"] = withdraw(
            client, pid, w["id"], w["query_token"]
        ).status_code

    def do_cancel():
        barrier.wait()
        outcomes["cancel"] = cancel(
            client, pid, holder["id"], holder["cancel_token"]
        ).status_code

    threads = [
        threading.Thread(target=do_withdraw),
        threading.Thread(target=do_cancel),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes["cancel"] == 200
    entry = status(client, pid, w["id"], w["query_token"]).json()
    if outcomes["withdraw"] == 200:
        # Withdrawal committed first: the sweep skipped the entry and the
        # freed slot stays bookable by exactly one new visitor.
        assert entry["status"] == "withdrawn"
        assert reserve(client, pid, "only-buyer", at(9), at(10)).status_code == 201
        assert reserve(client, pid, "second-buyer", at(9), at(10)).status_code == 409
    else:
        # The cancel/sweep won the point lock: the entry was promoted, so the
        # withdrawal of a promoted entry is refused with the distinguishable
        # code and the slot is held by the promoted reservation.
        assert outcomes["withdraw"] == 409
        assert entry["status"] == "promoted"
        assert reserve(client, pid, "squatter", at(9), at(10)).status_code == 409
