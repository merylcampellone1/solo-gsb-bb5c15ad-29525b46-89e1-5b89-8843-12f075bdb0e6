"""Calendar expansion engine.

Pure-Python, no database dependency.  Given a point's IANA timezone and one
published rule set (weekly opening rules, full-day closures, same-day makeup
openings) it expands an inclusive local-date range into concrete open
intervals in UTC.

Conventions
-----------
* Weekdays are ISO numbers: 1=Monday ... 7=Sunday.
* A weekly rule with ``end_time < start_time`` spans into the next day.  The
  rule matches by the weekday of its *start* day.
* A closed local date removes every regular (weekly) piece that falls on that
  date, including the piece carried over from the previous night; the whole
  weekly session starting on that date is cancelled, so its tail spilling into
  the next date is removed too.  Makeup openings are then added on top.
* DST gap ("spring forward"): a boundary that does not exist moves forward to
  the first valid instant.
* DST repeated hour ("fall back"): a start uses the earlier occurrence, an
  end uses the later occurrence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = timezone.utc


def _as_utc(dt: datetime) -> datetime:
    """Normalize an aware instant to UTC.

    All interval math (clipping, ordering, merging) is done in UTC on purpose:
    Python compares two aware datetimes that share the *same* tzinfo object by
    their naive wall-clock, which silently ignores ``fold`` during a DST
    repeated hour.  UTC instants never have that problem.
    """
    return dt.astimezone(UTC)


@dataclass(frozen=True)
class WeeklyRule:
    weekday: int  # ISO weekday 1..7
    start_time: time
    end_time: time  # earlier than start_time => spans into next day

    @property
    def overnight(self) -> bool:
        return self.end_time <= self.start_time


@dataclass(frozen=True)
class MakeupRule:
    day: date
    start_time: time
    end_time: time  # makeup openings are always same-day


@dataclass(frozen=True)
class RuleSet:
    weekly: tuple[WeeklyRule, ...]
    closed_dates: frozenset[date]
    makeup: tuple[MakeupRule, ...]


@dataclass(frozen=True)
class Source:
    """Provenance of a (merged) open interval."""

    kind: str  # "weekly" | "makeup"
    weekday: int | None = None  # ISO weekday of the weekly start day
    day: date | None = None  # local date of a makeup opening
    start_time: str | None = None
    end_time: str | None = None

    def as_dict(self) -> dict:
        out: dict = {"kind": self.kind}
        if self.weekday is not None:
            out["weekday"] = self.weekday
        if self.day is not None:
            out["day"] = self.day.isoformat()
        if self.start_time is not None:
            out["start_time"] = self.start_time
        if self.end_time is not None:
            out["end_time"] = self.end_time
        return out


@dataclass
class DayResult:
    local_date: date
    intervals: list[tuple[datetime, datetime, tuple[Source, ...]]] = field(
        default_factory=list
    )


class UnknownTimezone(ValueError):
    """Raised when an IANA timezone name cannot be resolved."""


def get_zone(tz_name: str) -> ZoneInfo:
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, ValueError) as exc:  # pragma: no cover - ValueError defensive
        raise UnknownTimezone(tz_name) from exc


# ---------------------------------------------------------------------------
# Wall-clock -> instant resolution
# ---------------------------------------------------------------------------

def _exists(dt: datetime, zone: ZoneInfo) -> bool:
    """True if a naive wall-clock ``dt`` names a real instant in ``zone``.

    A wall-clock exists when the instant it maps to converts back to the same
    naive wall-clock.  For ambiguous (repeated) wall-clocks both folds exist.
    """
    assert dt.tzinfo is None
    for fold in (0, 1):
        candidate = dt.replace(fold=fold, tzinfo=zone)
        back = candidate.astimezone(ZoneInfo("UTC")).astimezone(zone)
        if back.replace(tzinfo=None) == dt:
            return True
    return False


def _first_valid_at_or_after(naive: datetime, zone: ZoneInfo) -> datetime:
    """First valid aware instant whose local wall-clock is >= ``naive``."""
    probe = naive
    # Gaps created by civil time changes are a few hours at most; stepping one
    # minute at a time over a single day is deterministic and avoids depending
    # on private zoneinfo internals.
    for _ in range(24 * 60):
        if _exists(probe, zone):
            return probe.replace(fold=0, tzinfo=zone)
        probe += timedelta(minutes=1)
    raise RuntimeError("could not resolve a valid instant within 24h")  # pragma: no cover


def resolve_start(naive: datetime, zone: ZoneInfo) -> datetime:
    """Resolve a start boundary to UTC: gap -> first valid moment; repeat early."""
    naive = naive.replace(tzinfo=None)
    dt = naive.replace(fold=0, tzinfo=zone)
    back = dt.astimezone(UTC).astimezone(zone)
    if back.replace(tzinfo=None) != naive:
        return _as_utc(_first_valid_at_or_after(naive, zone))
    return _as_utc(dt)  # fold=0 is the earlier occurrence when ambiguous


def resolve_end(naive: datetime, zone: ZoneInfo) -> datetime:
    """Resolve an end boundary to UTC: gap -> first valid moment; repeat late."""
    naive = naive.replace(tzinfo=None)
    dt = naive.replace(fold=0, tzinfo=zone)
    back = dt.astimezone(UTC).astimezone(zone)
    if back.replace(tzinfo=None) != naive:
        return _as_utc(_first_valid_at_or_after(naive, zone))
    # Exists.  Use the later occurrence when the wall-clock is repeated.
    return _as_utc(dt.replace(fold=1))


# ---------------------------------------------------------------------------
# Calendar expansion
# ---------------------------------------------------------------------------

def _day_bounds(day: date, zone: ZoneInfo) -> tuple[datetime, datetime]:
    start = resolve_start(datetime.combine(day, time.min), zone)
    end = resolve_end(datetime.combine(day + timedelta(days=1), time.min), zone)
    return start, end


def _merge_pieces(
    pieces: list[tuple[datetime, datetime, Source, date]],
) -> list[tuple[datetime, datetime, tuple[Source, ...]]]:
    """Merge UTC intervals that overlap or touch; union the sources."""
    pieces = sorted(pieces, key=lambda p: (p[0], p[1]))
    merged: list[tuple[datetime, datetime, list[Source]]] = []
    for start, end, source, _owner in pieces:
        if merged and start <= merged[-1][1]:
            mstart, mend, msources = merged[-1]
            if end > mend:
                merged[-1] = (mstart, end, msources)
            if source not in msources:
                msources.append(source)
        else:
            merged.append((start, end, [source]))
    return [(s, e, tuple(srcs)) for s, e, srcs in merged]


def _collect_day_pieces(
    rules: RuleSet,
    zone: ZoneInfo,
    seed: date,
    date_to: date,
) -> dict[date, list[tuple[datetime, datetime, Source, date]]]:
    """Generate the located-day pieces for every day in ``[seed, date_to]``.

    Weekly sessions starting on ``date_to`` may locate a tail piece on
    ``date_to + 1``.  Closures and makeup openings are applied here, so the
    returned pieces are the definitive open fragments.
    """
    by_weekday: dict[int, list[WeeklyRule]] = {}
    for rule in rules.weekly:
        by_weekday.setdefault(rule.weekday, []).append(rule)

    # pieces keyed by the local day they actually occupy.  Each piece also
    # remembers ``owner_day``: the day whose weekly session it belongs to
    # (the rule's start day).  A closure on day D cancels the whole session
    # owned by D, including its tail on D+1.
    day_pieces: dict[
        date, list[tuple[datetime, datetime, Source, date]]
    ] = {}

    def add_piece(
        located_day: date,
        start: datetime,
        end: datetime,
        source: Source,
        owner_day: date,
    ) -> None:
        if end <= start:
            # A DST jump can collapse an interval; it is not open then.
            return
        day_pieces.setdefault(located_day, []).append(
            (start, end, source, owner_day)
        )

    # -- weekly rules, matched by the weekday of their start day -------------
    cursor = seed
    while cursor <= date_to:
        for rule in by_weekday.get(cursor.isoweekday(), ()):
            source = Source(
                kind="weekly",
                weekday=rule.weekday,
                start_time=rule.start_time.isoformat(),
                end_time=rule.end_time.isoformat(),
            )
            start = resolve_start(datetime.combine(cursor, rule.start_time), zone)
            if rule.overnight:
                end = resolve_end(
                    datetime.combine(cursor + timedelta(days=1), rule.end_time), zone
                )
            else:
                end = resolve_end(datetime.combine(cursor, rule.end_time), zone)
            if end <= start:
                continue
            # Clip to the two calendar days the rule can touch.
            first_start, first_end = _day_bounds(cursor, zone)
            if start < first_end and end > first_start:
                add_piece(
                    cursor, max(start, first_start), min(end, first_end),
                    source, cursor,
                )
            if rule.overnight:
                nxt = cursor + timedelta(days=1)
                next_start, next_end = _day_bounds(nxt, zone)
                if start < next_end and end > next_start:
                    add_piece(
                        nxt, max(start, next_start), min(end, next_end),
                        source, cursor,
                    )
        cursor += timedelta(days=1)

    # -- closures: remove every regular piece located on a closed date -------
    # including the piece carried over from the previous night, and cancel the
    # whole session that *starts* on the closed date (its tail spilling into
    # the next morning).  Makeup openings are added afterwards.
    for closed in rules.closed_dates:
        day_pieces.pop(closed, None)
        following = closed + timedelta(days=1)
        if following in day_pieces:
            day_pieces[following] = [
                piece for piece in day_pieces[following] if piece[3] != closed
            ]
            if not day_pieces[following]:
                day_pieces.pop(following)

    for makeup in rules.makeup:
        source = Source(
            kind="makeup",
            day=makeup.day,
            start_time=makeup.start_time.isoformat(),
            end_time=makeup.end_time.isoformat(),
        )
        start = resolve_start(datetime.combine(makeup.day, makeup.start_time), zone)
        end = resolve_end(datetime.combine(makeup.day, makeup.end_time), zone)
        day_start, day_end = _day_bounds(makeup.day, zone)
        if start < day_end and end > day_start:
            add_piece(
                makeup.day, max(start, day_start), min(end, day_end),
                source, makeup.day,
            )

    return day_pieces


def build_calendar(
    tz_name: str,
    rules: RuleSet,
    date_from: date,
    date_to: date,
) -> list[DayResult]:
    """Expand the inclusive local-date range ``[date_from, date_to]``.

    Returns one :class:`DayResult` per local date, in ascending order.  Every
    interval is clipped to that local day and expressed in UTC.
    """
    if date_to < date_from:
        raise ValueError("date_to must be on or after date_from")
    zone = get_zone(tz_name)

    # Weekly rules matching on the previous evening may spill into date_from,
    # so the seed day is one day earlier.  Nothing spills more than one day.
    seed = date_from - timedelta(days=1)
    day_pieces = _collect_day_pieces(rules, zone, seed, date_to)

    results: list[DayResult] = []
    day = date_from
    while day <= date_to:
        results.append(DayResult(day, _merge_pieces(day_pieces.get(day, []))))
        day += timedelta(days=1)
    return results


def build_open_intervals(
    tz_name: str,
    rules: RuleSet,
    date_from: date,
    date_to: date,
) -> list[tuple[datetime, datetime]]:
    """Merged *continuous* open intervals (UTC) over the local-date range.

    Unlike :func:`build_calendar`, intervals are not clipped at local
    midnights: pieces that touch across a day seam merge into one continuous
    interval, so an overnight weekly session appears as a single span.  A
    reservation is "fully open" exactly when it fits inside one of these
    intervals.  Sessions spilling one day past either range edge are included.
    """
    if date_to < date_from:
        raise ValueError("date_to must be on or after date_from")
    zone = get_zone(tz_name)
    seed = date_from - timedelta(days=1)
    day_pieces = _collect_day_pieces(rules, zone, seed, date_to)
    all_pieces = [piece for pieces in day_pieces.values() for piece in pieces]
    return [(start, end) for start, end, _sources in _merge_pieces(all_pieces)]
