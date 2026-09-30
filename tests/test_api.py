"""End-to-end HTTP API tests against a real PostgreSQL instance."""

from __future__ import annotations


def create_point(client, name="示例回收点", tz="Asia/Shanghai"):
    r = client.post("/points", json={"name": name, "timezone": tz})
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# Points
# ---------------------------------------------------------------------------

def test_health(client):
    assert client.get("/health").json() == {"status": "ok"}


def test_create_get_point(client):
    point = create_point(client, tz="America/New_York")
    assert point["version"] == 0
    fetched = client.get(f"/points/{point['id']}").json()
    assert fetched["timezone"] == "America/New_York"
    assert fetched["id"] == point["id"]


def test_unknown_timezone_rejected_with_distinguishable_code(client):
    r = client.post("/points", json={"name": "x", "timezone": "Mars/Olympus"})
    assert r.status_code == 422
    body = r.json()["error"]
    assert body["code"] == "unknown_timezone"


def test_missing_point_404(client):
    r = client.get("/points/99999999")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "point_not_found"


# ---------------------------------------------------------------------------
# No published version
# ---------------------------------------------------------------------------

def test_calendar_without_published_rules_returns_distinguishable_error(client):
    point = create_point(client, name="尚未配置", tz="UTC")
    r = client.get(f"/points/{point['id']}/calendar?from=2026-09-01&to=2026-09-07")
    assert r.status_code == 409
    body = r.json()["error"]
    assert body["code"] == "no_published_version"
    assert body["details"]["current_version"] == 0


# ---------------------------------------------------------------------------
# Conditional, atomic publication
# ---------------------------------------------------------------------------

def test_publish_requires_expected_version_zero_first(client):
    point = create_point(client, name="乐观锁", tz="UTC")
    body = {
        "expected_version": 5,
        "rules": {"weekly": [], "closed_dates": [], "makeup": []},
    }
    r = client.post(f"/points/{point['id']}/rules", json=body)
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "version_conflict"
    assert err["details"] == {"expected_version": 5, "current_version": 0}


def test_publish_versioning_and_history(client):
    point = create_point(client, name="版本历史", tz="UTC")
    pid = point["id"]

    r1 = client.post(
        f"/points/{pid}/rules",
        json={
            "expected_version": 0,
            "rules": {
                "weekly": [{"weekday": 3, "start_time": "09:00", "end_time": "17:00"}],
                "closed_dates": [],
                "makeup": [],
            },
        },
    )
    assert r1.status_code == 201, r1.text
    assert r1.json()["version"] == 1

    # Reusing expected_version 0 now conflicts.
    r_again = client.post(
        f"/points/{pid}/rules",
        json={
            "expected_version": 0,
            "rules": {"weekly": [], "closed_dates": [], "makeup": []},
        },
    )
    assert r_again.status_code == 409
    assert r_again.json()["error"]["details"]["current_version"] == 1

    r2 = client.post(
        f"/points/{pid}/rules",
        json={
            "expected_version": 1,
            "rules": {
                "weekly": [
                    {"weekday": 3, "start_time": "09:00", "end_time": "12:00"},
                ],
                "closed_dates": ["2026-09-02"],
                "makeup": [
                    {"day": "2026-09-02", "start_time": "14:00", "end_time": "18:00"}
                ],
            },
        },
    )
    assert r2.status_code == 201
    assert r2.json()["version"] == 2

    versions = client.get(f"/points/{pid}/rules/versions").json()
    assert [v["version"] for v in versions] == [2, 1]

    v1 = client.get(f"/points/{pid}/rules/versions/1").json()
    assert v1["rules"]["weekly"][0]["end_time"] == "17:00"
    cur = client.get(f"/points/{pid}/rules").json()
    assert cur["version"] == 2 and cur["rules"]["closed_dates"] == ["2026-09-02"]

    missing = client.get(f"/points/{pid}/rules/versions/99")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "version_not_found"


def test_concurrent_publish_exactly_one_wins(client):
    import threading

    from app import db
    from app.errors import ApiError

    point = create_point(client, name="并发发布", tz="UTC")
    pid = point["id"]
    payload = {"weekly": [], "closed_dates": [], "makeup": []}
    results: list[Exception | dict] = []
    barrier = threading.Barrier(2)

    def attempt():
        barrier.wait()
        try:
            with db.conn() as connection:
                with connection.transaction():
                    row = db.publish_rules(connection.cursor(), pid, 0, payload)
            results.append(row)
        except ApiError as exc:
            results.append(exc)

    threads = [threading.Thread(target=attempt) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 2
    successes = [r for r in results if not isinstance(r, Exception)]
    conflicts = [r for r in results if isinstance(r, Exception)]
    assert len(successes) == 1
    assert len(conflicts) == 1
    assert conflicts[0].code == "version_conflict"
    assert client.get(f"/points/{pid}").json()["version"] == 1


def test_invalid_periods_rejected(client):
    point = create_point(client, name="无效时段", tz="UTC")
    pid = point["id"]

    def pub(rules):
        return client.post(
            f"/points/{pid}/rules",
            json={"expected_version": 0, "rules": rules},
        )

    # zero-length same-day weekly rule
    r = pub({"weekly": [{"weekday": 1, "start_time": "10:00", "end_time": "10:00"}],
             "closed_dates": [], "makeup": []})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_period"

    # bad weekday
    r = pub({"weekly": [{"weekday": 8, "start_time": "10:00", "end_time": "11:00"}],
             "closed_dates": [], "makeup": []})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_period"

    # bad time format
    r = pub({"weekly": [{"weekday": 1, "start_time": "noon", "end_time": "11:00"}],
             "closed_dates": [], "makeup": []})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_period"

    # overnight makeup not allowed (must be same-day)
    r = pub({"weekly": [], "closed_dates": [],
             "makeup": [{"day": "2026-09-02", "start_time": "22:00", "end_time": "02:00"}]})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_period"

    # point version must remain 0: nothing was published
    assert client.get(f"/points/{pid}").json()["version"] == 0


# ---------------------------------------------------------------------------
# Calendar query
# ---------------------------------------------------------------------------

def test_full_calendar_scenario_closures_makeup_merge_sources(client):
    point = create_point(client, name="综合场景", tz="America/New_York")
    pid = point["id"]
    r = client.post(
        f"/points/{pid}/rules",
        json={
            "expected_version": 0,
            "rules": {
                "weekly": [
                    # Mon-Thu 08:00-18:00 ; Fri overnight to Sat 02:00
                    {"weekday": d, "start_time": "08:00", "end_time": "18:00"}
                    for d in (1, 2, 3, 4)
                ] + [{"weekday": 5, "start_time": "20:00", "end_time": "02:00"}],
                "closed_dates": ["2026-09-04"],  # Friday closed
                "makeup": [
                    {"day": "2026-09-04", "start_time": "10:00", "end_time": "12:00"}
                ],
            },
        },
    )
    assert r.status_code == 201, r.text

    cal = client.get(
        f"/points/{pid}/calendar?from=2026-09-03&to=2026-09-06"
    ).json()
    assert cal["version"] == 1
    assert cal["timezone"] == "America/New_York"
    days = {d["local_date"]: d["intervals"] for d in cal["days"]}

    # Thursday 08:00-18:00 EDT = 12:00-22:00Z
    thu = days["2026-09-03"]
    assert len(thu) == 1
    assert thu[0]["start"] == "2026-09-03T12:00:00Z"
    assert thu[0]["end"] == "2026-09-03T22:00:00Z"
    assert thu[0]["sources"][0]["kind"] == "weekly"

    # Friday closed: overnight piece that would start Friday 20:00 is gone,
    # only makeup 10:00-12:00 EDT = 14:00-16:00Z remains.
    fri = days["2026-09-04"]
    assert len(fri) == 1
    assert fri[0]["start"] == "2026-09-04T14:00:00Z"
    assert fri[0]["end"] == "2026-09-04T16:00:00Z"
    assert fri[0]["sources"][0]["kind"] == "makeup"

    # Saturday: no carryover from the closed Friday night.
    assert days["2026-09-05"] == []
    # Sunday empty too, but the day itself is present.
    assert days["2026-09-06"] == []


def test_overlapping_makeup_merges_with_all_sources(client):
    point = create_point(client, name="来源合并", tz="UTC")
    pid = point["id"]
    client.post(
        f"/points/{pid}/rules",
        json={
            "expected_version": 0,
            "rules": {
                "weekly": [{"weekday": 3, "start_time": "09:00", "end_time": "12:00"}],
                "closed_dates": [],
                "makeup": [
                    {"day": "2026-09-02", "start_time": "11:00", "end_time": "14:00"}
                ],
            },
        },
    )
    cal = client.get(f"/points/{pid}/calendar?from=2026-09-02&to=2026-09-02").json()
    intervals = cal["days"][0]["intervals"]
    assert len(intervals) == 1
    assert intervals[0]["start"] == "2026-09-02T09:00:00Z"
    assert intervals[0]["end"] == "2026-09-02T14:00:00Z"
    kinds = sorted(s["kind"] for s in intervals[0]["sources"])
    assert kinds == ["makeup", "weekly"]


def test_dst_spring_forward_and_fall_back_via_http(client):
    # Spring forward at midnight: Havana jumps 2026-03-08 00:00 -> 01:00.
    havana = create_point(client, name="哈瓦那", tz="America/Havana")
    client.post(
        f"/points/{havana['id']}/rules",
        json={
            "expected_version": 0,
            "rules": {
                # Saturday 22:00 -> Sunday 03:00 (overnight)
                "weekly": [{"weekday": 6, "start_time": "22:00", "end_time": "03:00"}],
                "closed_dates": [],
                "makeup": [],
            },
        },
    )
    cal = client.get(
        f"/points/{havana['id']}/calendar?from=2026-03-07&to=2026-03-08"
    ).json()
    sat = cal["days"][0]["intervals"]
    sun = cal["days"][1]["intervals"]
    # 22:00 -05 = 03:00Z ; missing midnight -> first valid 01:00 -04 = 05:00Z
    assert sat[0]["start"] == "2026-03-08T03:00:00Z"
    assert sat[0]["end"] == "2026-03-08T05:00:00Z"
    # Pieces meet at the seam; Sunday piece ends 03:00 -04 = 07:00Z.
    assert sun[0]["start"] == "2026-03-08T05:00:00Z"
    assert sun[0]["end"] == "2026-03-08T07:00:00Z"

    # Fall back: New York repeats 01:00 on 2026-11-01.
    ny = create_point(client, name="纽约", tz="America/New_York")
    client.post(
        f"/points/{ny['id']}/rules",
        json={
            "expected_version": 0,
            "rules": {
                "weekly": [{"weekday": 7, "start_time": "00:00", "end_time": "03:00"}],
                "closed_dates": [],
                "makeup": [],
            },
        },
    )
    cal = client.get(
        f"/points/{ny['id']}/calendar?from=2026-11-01&to=2026-11-01"
    ).json()
    interval = cal["days"][0]["intervals"][0]
    # 00:00 EDT (04:00Z) -> 03:00 EST (08:00Z): the repeated hour is counted.
    assert interval["start"] == "2026-11-01T04:00:00Z"
    assert interval["end"] == "2026-11-01T08:00:00Z"


def test_calendar_can_pin_a_historical_version(client):
    point = create_point(client, name="固定版本", tz="UTC")
    pid = point["id"]
    client.post(
        f"/points/{pid}/rules",
        json={"expected_version": 0, "rules": {
            "weekly": [{"weekday": 3, "start_time": "09:00", "end_time": "17:00"}],
            "closed_dates": [], "makeup": []}},
    )
    client.post(
        f"/points/{pid}/rules",
        json={"expected_version": 1, "rules": {
            "weekly": [], "closed_dates": [], "makeup": []}},
    )
    # Latest version is empty...
    latest = client.get(f"/points/{pid}/calendar?from=2026-09-02&to=2026-09-02").json()
    assert latest["version"] == 2
    assert latest["days"][0]["intervals"] == []
    # ...but pinning v1 still returns the old Wednesday opening.
    pinned = client.get(
        f"/points/{pid}/calendar?from=2026-09-02&to=2026-09-02&version=1"
    ).json()
    assert pinned["version"] == 1
    assert len(pinned["days"][0]["intervals"]) == 1

    missing = client.get(
        f"/points/{pid}/calendar?from=2026-09-02&to=2026-09-02&version=99"
    )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "version_not_found"


def test_calendar_query_validation(client):
    point = create_point(client, name="查询校验", tz="UTC")
    pid = point["id"]
    client.post(
        f"/points/{pid}/rules",
        json={"expected_version": 0,
              "rules": {"weekly": [], "closed_dates": [], "makeup": []}},
    )
    # inverted range
    r = client.get(f"/points/{pid}/calendar?from=2026-09-07&to=2026-09-01")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_period"
    # missing params -> request shape validation
    r = client.get(f"/points/{pid}/calendar?from=2026-09-01")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_request"


def test_empty_published_ruleset_is_valid_and_queryable(client):
    point = create_point(client, name="空规则", tz="UTC")
    pid = point["id"]
    r = client.post(
        f"/points/{pid}/rules",
        json={"expected_version": 0,
              "rules": {"weekly": [], "closed_dates": [], "makeup": []}},
    )
    assert r.status_code == 201
    cal = client.get(f"/points/{pid}/calendar?from=2026-09-01&to=2026-09-02").json()
    assert cal["version"] == 1
    assert all(day["intervals"] == [] for day in cal["days"])
    assert len(cal["days"]) == 2
