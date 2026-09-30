"""Unit tests for the pure calendar expansion engine."""

from datetime import date, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from app.scheduler import (
    MakeupRule,
    RuleSet,
    UnknownTimezone,
    WeeklyRule,
    build_calendar,
    build_open_intervals,
    get_zone,
    resolve_end,
    resolve_start,
)

T = lambda h, m=0: time(h, m)  # noqa: E731
UTC = timezone.utc


def utc(y, mo, d, h, mi=0):
    from datetime import datetime

    return datetime(y, mo, d, h, mi, tzinfo=UTC)


def u(dt):
    """Normalize an aware datetime to UTC for comparisons."""
    return dt.astimezone(UTC)


def intervals_for(result, day):
    for dr in result:
        if dr.local_date == day:
            return dr.intervals
    raise KeyError(day)


def bounds(intervals, index=0):
    """(start, end) of one interval normalized to UTC."""
    s, e, _ = intervals[index]
    return (u(s), u(e))


# ---------------------------------------------------------------------------
# Basic weekly rules incl. overnight
# ---------------------------------------------------------------------------

def test_same_day_weekly_rule_utc_bounds():
    rules = RuleSet(
        weekly=(WeeklyRule(3, T(9), T(17)),),
        closed_dates=frozenset(),
        makeup=(),
    )
    # 2026-09-02 is a Wednesday; Asia/Shanghai is UTC+8 (no DST).
    cal = build_calendar("Asia/Shanghai", rules, date(2026, 9, 2), date(2026, 9, 2))
    intervals = intervals_for(cal, date(2026, 9, 2))
    assert len(intervals) == 1
    start, end, sources = intervals[0]
    assert u(start) == utc(2026, 9, 2, 1)  # 09:00 +08
    assert u(end) == utc(2026, 9, 2, 9)  # 17:00 +08
    assert sources[0].kind == "weekly"
    assert sources[0].weekday == 3
    assert sources[0].start_time == "09:00:00"


def test_overnight_rule_matches_start_weekday_and_is_clipped_per_day():
    # Friday 20:00 -> Saturday 02:00 (New York).
    rules = RuleSet(
        weekly=(WeeklyRule(5, T(20), T(2)),),
        closed_dates=frozenset(),
        makeup=(),
    )
    cal = build_calendar(
        "America/New_York", rules, date(2026, 9, 4), date(2026, 9, 5)
    )  # Fri, Sat
    fri = intervals_for(cal, date(2026, 9, 4))
    sat = intervals_for(cal, date(2026, 9, 5))
    # September is EDT (-04)
    assert len(fri) == 1
    assert bounds(fri) == (utc(2026, 9, 5, 0), utc(2026, 9, 5, 4))
    assert len(sat) == 1
    assert bounds(sat) == (utc(2026, 9, 5, 4), utc(2026, 9, 5, 6))
    # Both pieces carry the same source (matched by Friday's weekday).
    assert fri[0][2] == sat[0][2]
    assert sat[0][2][0].weekday == 5


def test_non_matching_days_are_empty_but_present():
    rules = RuleSet(
        weekly=(WeeklyRule(2, T(8), T(12)),),
        closed_dates=frozenset(),
        makeup=(),
    )
    cal = build_calendar("UTC", rules, date(2026, 9, 1), date(2026, 9, 7))
    assert [d.local_date for d in cal] == [date(2026, 9, 1) + timedelta(i) for i in range(7)]
    assert intervals_for(cal, date(2026, 9, 1))  # Tuesday
    assert not intervals_for(cal, date(2026, 9, 2))  # Wednesday


# ---------------------------------------------------------------------------
# Closures remove that day's regular pieces (incl. previous-night carryover)
# and makeup is added on top
# ---------------------------------------------------------------------------

def test_closed_day_removes_overnight_carryover_and_adds_makeup():
    # Fri 20:00->02:00 ; Saturday closed ; makeup Saturday 10:00-13:00.
    rules = RuleSet(
        weekly=(WeeklyRule(5, T(20), T(2)),),
        closed_dates=frozenset({date(2026, 9, 5)}),
        makeup=(MakeupRule(date(2026, 9, 5), T(10), T(13)),),
    )
    cal = build_calendar(
        "America/New_York", rules, date(2026, 9, 4), date(2026, 9, 5)
    )
    # Friday keeps its own evening piece.
    fri = intervals_for(cal, date(2026, 9, 4))
    assert len(fri) == 1
    assert bounds(fri) == (utc(2026, 9, 5, 0), utc(2026, 9, 5, 4))
    # Saturday: no carryover from Friday night; only the makeup interval.
    sat = intervals_for(cal, date(2026, 9, 5))
    assert len(sat) == 1
    start, end, sources = sat[0]
    assert (u(start), u(end)) == (utc(2026, 9, 5, 14), utc(2026, 9, 5, 17))
    assert sources[0].kind == "makeup"
    assert sources[0].day == date(2026, 9, 5)


def test_closed_day_cancels_own_overnight_tail_on_next_day():
    # Friday closed; the Friday 20:00->Saturday 02:00 session is cancelled
    # whole, so no dangling piece remains on Saturday morning.
    rules = RuleSet(
        weekly=(WeeklyRule(5, T(20), T(2)),),
        closed_dates=frozenset({date(2026, 9, 4)}),
        makeup=(),
    )
    cal = build_calendar(
        "America/New_York", rules, date(2026, 9, 4), date(2026, 9, 5)
    )
    assert not intervals_for(cal, date(2026, 9, 4))
    assert not intervals_for(cal, date(2026, 9, 5))

    # Sanity: without the closure the same data does produce both pieces.
    open_rules = RuleSet(
        weekly=(WeeklyRule(5, T(20), T(2)),),
        closed_dates=frozenset(),
        makeup=(),
    )
    cal_open = build_calendar(
        "America/New_York", open_rules, date(2026, 9, 4), date(2026, 9, 5)
    )
    assert intervals_for(cal_open, date(2026, 9, 5))


def test_closed_day_removes_pieces_starting_that_day_too():
    rules = RuleSet(
        weekly=(WeeklyRule(3, T(22), T(23)), WeeklyRule(2, T(22), T(2))),
        closed_dates=frozenset({date(2026, 9, 2)}),  # Wednesday closed
        makeup=(),
    )
    cal = build_calendar("UTC", rules, date(2026, 9, 1), date(2026, 9, 2))
    assert not intervals_for(cal, date(2026, 9, 2))  # Wednesday own rule gone
    # Tuesday overnight spill into Wednesday is also gone on the Wednesday piece
    # (Tuesday's own piece remains).
    tue = intervals_for(cal, date(2026, 9, 1))
    assert len(tue) == 1
    assert bounds(tue) == (utc(2026, 9, 1, 22), utc(2026, 9, 2, 0))


def test_makeup_without_closure_still_added():
    rules = RuleSet(
        weekly=(),
        closed_dates=frozenset(),
        makeup=(MakeupRule(date(2026, 9, 3), T(7), T(8)),),
    )
    cal = build_calendar("UTC", rules, date(2026, 9, 3), date(2026, 9, 3))
    intervals = intervals_for(cal, date(2026, 9, 3))
    assert len(intervals) == 1
    assert bounds(intervals) == (utc(2026, 9, 3, 7), utc(2026, 9, 3, 8))


# ---------------------------------------------------------------------------
# Overlaps merge and keep all sources
# ---------------------------------------------------------------------------

def test_overlapping_intervals_merge_with_union_of_sources():
    rules = RuleSet(
        weekly=(
            WeeklyRule(3, T(9), T(12)),
            WeeklyRule(3, T(11), T(15)),  # overlaps
        ),
        closed_dates=frozenset(),
        makeup=(),
    )
    cal = build_calendar("UTC", rules, date(2026, 9, 2), date(2026, 9, 2))
    intervals = intervals_for(cal, date(2026, 9, 2))
    assert len(intervals) == 1
    assert bounds(intervals) == (utc(2026, 9, 2, 9), utc(2026, 9, 2, 15))
    assert len(intervals[0][2]) == 2
    # touch-only intervals also merge
    rules2 = RuleSet(
        weekly=(WeeklyRule(3, T(9), T(10)), WeeklyRule(3, T(10), T(11))),
        closed_dates=frozenset(),
        makeup=(),
    )
    cal2 = build_calendar("UTC", rules2, date(2026, 9, 2), date(2026, 9, 2))
    intervals2 = intervals_for(cal2, date(2026, 9, 2))
    assert len(intervals2) == 1
    assert bounds(intervals2) == (utc(2026, 9, 2, 9), utc(2026, 9, 2, 11))


def test_makeup_overlapping_weekly_merges_and_keeps_both_sources():
    rules = RuleSet(
        weekly=(WeeklyRule(3, T(9), T(12)),),
        closed_dates=frozenset(),
        makeup=(MakeupRule(date(2026, 9, 2), T(11), T(14)),),
    )
    cal = build_calendar("UTC", rules, date(2026, 9, 2), date(2026, 9, 2))
    intervals = intervals_for(cal, date(2026, 9, 2))
    assert len(intervals) == 1
    assert bounds(intervals) == (utc(2026, 9, 2, 9), utc(2026, 9, 2, 14))
    kinds = {s.kind for s in intervals[0][2]}
    assert kinds == {"weekly", "makeup"}


# ---------------------------------------------------------------------------
# DST handling
# ---------------------------------------------------------------------------

def test_spring_forward_gap_start_moves_to_first_valid_moment():
    # New York 2026-03-08: clocks jump 02:00 -> 03:00.
    z = get_zone("America/New_York")
    start = resolve_start(
        __import__("datetime").datetime(2026, 3, 8, 2, 30), z
    )
    # 02:30 does not exist -> first valid instant is 03:00 EDT = 07:00Z.
    assert u(start) == utc(2026, 3, 8, 7)
    end = resolve_end(__import__("datetime").datetime(2026, 3, 8, 2, 30), z)
    assert u(end) == utc(2026, 3, 8, 7)


def test_fall_back_start_early_end_late():
    # New York 2026-11-01: 01:00 repeats (EDT then EST).
    from datetime import datetime

    z = get_zone("America/New_York")
    start = resolve_start(datetime(2026, 11, 1, 1, 30), z)
    end = resolve_end(datetime(2026, 11, 1, 1, 30), z)
    assert u(start) == utc(2026, 11, 1, 5, 30)  # earlier instance: EDT -04
    assert u(end) == utc(2026, 11, 1, 6, 30)  # later instance: EST -05
    assert end > start


def test_fall_back_overnight_rule_spans_both_hours():
    # Sunday 00:30 -> Monday 02:30 around the Nov 1 fall-back, UTC.
    # More meaningful: rule Sunday 00:00 -> 03:00 on the fall-back day gives
    # a 4-hour UTC interval due to the repeated hour.
    rules = RuleSet(
        weekly=(WeeklyRule(7, T(0), T(3)),),
        closed_dates=frozenset(),
        makeup=(),
    )
    cal = build_calendar(
        "America/New_York", rules, date(2026, 11, 1), date(2026, 11, 1)
    )
    intervals = intervals_for(cal, date(2026, 11, 1))
    assert len(intervals) == 1
    # 00:00 EDT = 04:00Z ; 03:00 EST = 08:00Z -> 4 hours
    assert bounds(intervals) == (
        utc(2026, 11, 1, 4),
        utc(2026, 11, 1, 8),
    )


def test_spring_forward_overnight_split_seam_is_continuous():
    # Havana jumps at midnight 2026-03-08: local 23:59(-05) -> 01:00(-04).
    # Friday 22:00 -> Saturday 03:00 crosses the transition; the two daily
    # pieces must meet exactly at the 05:00Z seam.
    rules = RuleSet(
        weekly=(WeeklyRule(7, T(22), T(3)),),  # Sunday 22:00? use Sat overnight
        closed_dates=frozenset(),
        makeup=(),
    )
    # Saturday 2026-03-07 is weekday 6.
    rules = RuleSet(
        weekly=(WeeklyRule(6, T(22), T(3)),),
        closed_dates=frozenset(),
        makeup=(),
    )
    cal = build_calendar(
        "America/Havana", rules, date(2026, 3, 7), date(2026, 3, 8)
    )
    sat = intervals_for(cal, date(2026, 3, 7))
    sun = intervals_for(cal, date(2026, 3, 8))
    assert sat[-1][1] == sun[0][0]  # no gap, no overlap
    seam = sat[-1][1]
    assert u(seam) == utc(2026, 3, 8, 5)
    # Saturday piece: 22:00 -05 = 03:00Z next day up to seam
    assert u(sat[0][0]) == utc(2026, 3, 8, 3)
    # Sunday piece ends at 03:00 -04 = 07:00Z
    assert u(sun[-1][1]) == utc(2026, 3, 8, 7)


def test_inclusive_date_range_boundaries():
    # Range Tue 2026-09-01 .. Mon 2026-09-07 (both ends must be included).
    rules = RuleSet(
        weekly=(WeeklyRule(2, T(0), T(0, 1)), WeeklyRule(1, T(23), T(23, 59))),
        closed_dates=frozenset(),
        makeup=(),
    )
    cal = build_calendar("UTC", rules, date(2026, 9, 1), date(2026, 9, 7))
    assert intervals_for(cal, date(2026, 9, 1))  # first day included
    assert intervals_for(cal, date(2026, 9, 7))  # last day included


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------

def test_unknown_timezone_raises_distinguishable_error():
    with pytest.raises(UnknownTimezone):
        build_calendar(
            "Mars/Olympus",
            RuleSet((), frozenset(), ()),
            date(2026, 9, 1),
            date(2026, 9, 1),
        )


def test_inverted_date_range_rejected():
    with pytest.raises(ValueError):
        build_calendar(
            "UTC",
            RuleSet((), frozenset(), ()),
            date(2026, 9, 7),
            date(2026, 9, 1),
        )


def test_source_dict_shapes():
    rules = RuleSet(
        weekly=(WeeklyRule(1, T(8), T(9)),),
        closed_dates=frozenset(),
        makeup=(MakeupRule(date(2026, 9, 1), T(10), T(11)),),
    )
    cal = build_calendar("UTC", rules, date(2026, 9, 1), date(2026, 9, 1))
    for _, _, sources in intervals_for(cal, date(2026, 9, 1)):
        for s in sources:
            d = s.as_dict()
            assert d["kind"] in ("weekly", "makeup")
            assert "start_time" in d and "end_time" in d


# Silence the unused-import helper lint for ZoneInfo in some runners.
_ = ZoneInfo


# ---------------------------------------------------------------------------
# Continuous open intervals (reservation containment)
# ---------------------------------------------------------------------------

def test_open_intervals_overnight_session_is_one_continuous_span():
    rules = RuleSet(
        weekly=(WeeklyRule(5, T(20), T(2)),),  # Friday 20:00 -> Saturday 02:00
        closed_dates=frozenset(),
        makeup=(),
    )
    intervals = build_open_intervals(
        "UTC", rules, date(2026, 10, 2), date(2026, 10, 3)
    )
    # 2026-10-02 is a Friday; the session is a single span across midnight.
    assert intervals == [(utc(2026, 10, 2, 20), utc(2026, 10, 3, 2))]


def test_open_intervals_closure_on_first_day_removes_whole_session():
    rules = RuleSet(
        weekly=(WeeklyRule(5, T(20), T(2)),),
        closed_dates=frozenset({date(2026, 10, 2)}),
        makeup=(),
    )
    assert build_open_intervals("UTC", rules, date(2026, 10, 2), date(2026, 10, 3)) == []


def test_open_intervals_closure_on_second_day_keeps_first_day_upto_midnight():
    rules = RuleSet(
        weekly=(WeeklyRule(5, T(20), T(2)),),
        closed_dates=frozenset({date(2026, 10, 3)}),
        makeup=(),
    )
    intervals = build_open_intervals(
        "UTC", rules, date(2026, 10, 2), date(2026, 10, 3)
    )
    assert intervals == [(utc(2026, 10, 2, 20), utc(2026, 10, 3, 0))]


def test_open_intervals_touching_sessions_merge_across_sources():
    rules = RuleSet(
        weekly=(WeeklyRule(3, T(8), T(12)),),
        closed_dates=frozenset(),
        makeup=(MakeupRule(date(2026, 10, 7), T(12), T(14)),),
    )
    intervals = build_open_intervals(
        "UTC", rules, date(2026, 10, 7), date(2026, 10, 7)
    )
    assert intervals == [(utc(2026, 10, 7, 8), utc(2026, 10, 7, 14))]


def test_open_intervals_separate_sessions_stay_separate():
    rules = RuleSet(
        weekly=(WeeklyRule(3, T(8), T(10)), WeeklyRule(3, T(12), T(14))),
        closed_dates=frozenset(),
        makeup=(),
    )
    intervals = build_open_intervals(
        "UTC", rules, date(2026, 10, 7), date(2026, 10, 7)
    )
    assert intervals == [
        (utc(2026, 10, 7, 8), utc(2026, 10, 7, 10)),
        (utc(2026, 10, 7, 12), utc(2026, 10, 7, 14)),
    ]
