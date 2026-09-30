"""PostgreSQL access: pool lifecycle, schema init and rule repositories."""

from __future__ import annotations

import secrets
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import TracebackType

import psycopg
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from .errors import (
    InvalidCancelTokenError,
    InvalidWaitlistTokenError,
    NotFoundError,
    NoPublishedVersionError,
    ReservationChangeConflictError,
    ReservationNotFoundError,
    RevisionConflictError,
    VersionConflictError,
    VersionNotFoundError,
    WaitlistEntryNotActiveError,
    WaitlistEntryNotFoundError,
)
from .scheduler import (
    MakeupRule,
    RuleSet,
    WeeklyRule,
    build_open_intervals,
    get_zone,
)

_SCHEMA_SQL = Path(__file__).with_name("schema.sql").read_text(encoding="utf-8")

pool: ConnectionPool | None = None


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def open_pool(dsn: str, *, min_size: int = 1, max_size: int = 10) -> ConnectionPool:
    global pool
    if pool is None:
        pool = ConnectionPool(dsn, min_size=min_size, max_size=max_size, open=True)
    return pool


def close_pool() -> None:
    global pool
    if pool is not None:
        pool.close()
        pool = None


def init_db(dsn: str, *, retries: int = 30, delay: float = 2.0) -> None:
    """Wait for PostgreSQL, create the pool and initialize the schema."""
    conn: psycopg.Connection | None = None
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            conn = psycopg.connect(dsn, connect_timeout=5)
            break
        except psycopg.OperationalError as exc:
            last_exc = exc
            time.sleep(delay)
    if conn is None:
        raise RuntimeError(f"database not reachable: {last_exc}")
    try:
        with conn:
            conn.execute(_SCHEMA_SQL)
    finally:
        conn.close()
    open_pool(dsn)


class _ConnCtx:
    """Connection from the pool with transactional autocommit semantics."""

    def __init__(self) -> None:
        assert pool is not None, "database pool is not open"
        self._cm = pool.connection()

    def __enter__(self) -> psycopg.Connection:
        self.conn = self._cm.__enter__()
        self.conn.row_factory = dict_row
        return self.conn

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self._cm.__exit__(exc_type, exc, tb)


def conn() -> _ConnCtx:
    return _ConnCtx()


# ---------------------------------------------------------------------------
# Rule set (de)serialization
# ---------------------------------------------------------------------------

def payload_to_ruleset(payload: dict) -> RuleSet:
    weekly = tuple(
        WeeklyRule(
            weekday=item["weekday"],
            start_time=datetime.strptime(item["start_time"], "%H:%M").time(),
            end_time=datetime.strptime(item["end_time"], "%H:%M").time(),
        )
        for item in payload.get("weekly", [])
    )
    closed = frozenset(
        date.fromisoformat(day) for day in payload.get("closed_dates", [])
    )
    makeup = tuple(
        MakeupRule(
            day=date.fromisoformat(item["day"]),
            start_time=datetime.strptime(item["start_time"], "%H:%M").time(),
            end_time=datetime.strptime(item["end_time"], "%H:%M").time(),
        )
        for item in payload.get("makeup", [])
    )
    return RuleSet(weekly=weekly, closed_dates=closed, makeup=makeup)


# ---------------------------------------------------------------------------
# Points
# ---------------------------------------------------------------------------

def create_point(cur: psycopg.Cursor, name: str, timezone: str) -> dict:
    row = cur.execute(
        "INSERT INTO points (name, timezone) VALUES (%s, %s) "
        "RETURNING id, name, timezone, version, created_at, updated_at",
        (name, timezone),
    ).fetchone()
    return row


def get_point(cur: psycopg.Cursor, point_id: int) -> dict:
    row = cur.execute(
        "SELECT id, name, timezone, version, created_at, updated_at "
        "FROM points WHERE id = %s",
        (point_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"recycling point {point_id} does not exist")
    return row


def lock_point(cur: psycopg.Cursor, point_id: int) -> dict:
    """Lock the point row (FOR UPDATE) and return it, or 404.

    Every mutation that can affect reservations (reservation creation, rule
    publication, timezone change) takes this lock first, so they serialize
    per point and each sees the other's committed results.
    """
    row = cur.execute(
        "SELECT id, name, timezone, version, created_at, updated_at "
        "FROM points WHERE id = %s FOR UPDATE",
        (point_id,),
    ).fetchone()
    if row is None:
        raise NotFoundError(f"recycling point {point_id} does not exist")
    return row


def update_point(cur: psycopg.Cursor, point_id: int, **fields) -> dict:
    assignments = [f"{key} = %s" for key in fields]
    assignments.append("updated_at = now()")
    params = list(fields.values()) + [point_id]
    row = cur.execute(
        f"UPDATE points SET {', '.join(assignments)} WHERE id = %s "
        f"RETURNING id, name, timezone, version, created_at, updated_at",
        params,
    ).fetchone()
    if row is None:
        raise NotFoundError(f"recycling point {point_id} does not exist")
    return row


# ---------------------------------------------------------------------------
# Atomic, conditional rule publication
# ---------------------------------------------------------------------------

def publish_rules(
    cur: psycopg.Cursor, point_id: int, expected_version: int, payload: dict
) -> dict:
    """Conditionally publish a new version inside the caller's transaction.

    The point row is locked first so concurrent publish attempts for the same
    point serialize; a stale ``expected_version`` aborts the whole
    transaction with a 409 and reports the current version.  Publishing is
    also refused when it would leave a not-yet-finished active reservation
    no longer fully open.
    """
    point = cur.execute(
        "SELECT id, version, timezone FROM points WHERE id = %s FOR UPDATE",
        (point_id,),
    ).fetchone()
    if point is None:
        raise NotFoundError(f"recycling point {point_id} does not exist")
    current = point["version"]
    if current != expected_version:
        raise VersionConflictError(
            f"expected version {expected_version} but point {point_id} is at "
            f"version {current}",
            details={"expected_version": expected_version, "current_version": current},
        )
    assert_reservations_fit(cur, point_id, point["timezone"], payload)

    next_version = current + 1
    cur.execute(
        "INSERT INTO rule_versions (point_id, version, payload) "
        "VALUES (%s, %s, %s::jsonb)",
        (point_id, next_version, psycopg.types.json.Json(payload)),
    )
    cur.execute(
        "UPDATE points SET version = %s, updated_at = now() WHERE id = %s",
        (next_version, point_id),
    )
    row = cur.execute(
        "SELECT version, published_at FROM rule_versions "
        "WHERE point_id = %s AND version = %s",
        (point_id, next_version),
    ).fetchone()
    return row


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def get_current_rules(cur: psycopg.Cursor, point_id: int) -> tuple[dict, dict]:
    """Return ``(point, version_row)`` for the single published version.

    A consistent snapshot is used so a concurrent publish cannot make the
    point version and the loaded rules disagree.
    """
    point = get_point(cur, point_id)
    row = cur.execute(
        "SELECT version, payload, published_at FROM rule_versions "
        "WHERE point_id = %s ORDER BY version DESC LIMIT 1",
        (point_id,),
    ).fetchone()
    if row is None:
        raise NoPublishedVersionError(
            f"point {point_id} has no published rule set yet",
            details={"point_id": point_id, "current_version": point["version"]},
        )
    return point, row


def list_versions(cur: psycopg.Cursor, point_id: int) -> list[dict]:
    get_point(cur, point_id)
    return cur.execute(
        "SELECT version, published_at FROM rule_versions "
        "WHERE point_id = %s ORDER BY version DESC",
        (point_id,),
    ).fetchall()


def get_version(cur: psycopg.Cursor, point_id: int, version: int) -> dict:
    _point, row = get_pinned_rules(cur, point_id, version)
    return row


def get_pinned_rules(
    cur: psycopg.Cursor, point_id: int, version: int
) -> tuple[dict, dict]:
    """Return ``(point, version_row)`` for one specific published version."""
    point = get_point(cur, point_id)
    row = cur.execute(
        "SELECT version, payload, published_at FROM rule_versions "
        "WHERE point_id = %s AND version = %s",
        (point_id, version),
    ).fetchone()
    if row is None:
        raise VersionNotFoundError(
            f"version {version} of point {point_id} does not exist",
            details={"point_id": point_id, "requested_version": version},
        )
    return point, row


def get_latest_payload(cur: psycopg.Cursor, point_id: int) -> dict | None:
    """Return the newest published ``{version, payload}`` row, if any."""
    return cur.execute(
        "SELECT version, payload FROM rule_versions "
        "WHERE point_id = %s ORDER BY version DESC LIMIT 1",
        (point_id,),
    ).fetchone()


# ---------------------------------------------------------------------------
# Reservations
# ---------------------------------------------------------------------------

_RESERVATION_COLS = (
    "id, point_id, idempotency_key, start_utc, end_utc, rules_version, "
    "cancel_token, status, revision, created_start_utc, created_end_utc, "
    "created_rules_version, created_at, cancelled_at"
)

_RESCHEDULE_COLS = (
    "id, reservation_id, point_id, idempotency_key, expected_revision, "
    "start_utc, end_utc, rules_version, revision, created_at"
)


def get_reservation_by_key(
    cur: psycopg.Cursor, point_id: int, idempotency_key: str
) -> dict | None:
    return cur.execute(
        f"SELECT {_RESERVATION_COLS} FROM reservations "
        "WHERE point_id = %s AND idempotency_key = %s",
        (point_id, idempotency_key),
    ).fetchone()


def get_reservation_by_id(cur: psycopg.Cursor, reservation_id: int) -> dict | None:
    return cur.execute(
        f"SELECT {_RESERVATION_COLS} FROM reservations WHERE id = %s",
        (reservation_id,),
    ).fetchone()


def get_reservation_for_update(
    cur: psycopg.Cursor, point_id: int, reservation_id: int
) -> dict:
    """Lock the reservation row and return it, or raise 404 (cancel semantics)."""
    row = cur.execute(
        f"SELECT {_RESERVATION_COLS} FROM reservations "
        "WHERE id = %s AND point_id = %s FOR UPDATE",
        (reservation_id, point_id),
    ).fetchone()
    if row is None:
        raise ReservationNotFoundError(
            f"reservation {reservation_id} of point {point_id} does not exist",
            details={"point_id": point_id, "reservation_id": reservation_id},
        )
    return row


def check_cancel_token(row: dict, cancel_token: str | None) -> None:
    """Validate a capability token against a locked reservation row."""
    if cancel_token is None:
        raise InvalidCancelTokenError("cancel_token is required")
    if not secrets.compare_digest(row["cancel_token"], cancel_token):
        raise InvalidCancelTokenError("cancel_token does not match")


def insert_reservation(
    cur: psycopg.Cursor,
    point_id: int,
    idempotency_key: str,
    start_utc: datetime,
    end_utc: datetime,
    rules_version: int,
) -> dict:
    return cur.execute(
        f"INSERT INTO reservations "
        f"(point_id, idempotency_key, start_utc, end_utc, rules_version, "
        f" cancel_token, created_start_utc, created_end_utc, "
        f" created_rules_version) "
        f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
        f"RETURNING {_RESERVATION_COLS}",
        (
            point_id,
            idempotency_key,
            start_utc,
            end_utc,
            rules_version,
            secrets.token_urlsafe(32),
            start_utc,
            end_utc,
            rules_version,
        ),
    ).fetchone()


def find_overlapping(
    cur: psycopg.Cursor,
    point_id: int,
    start_utc: datetime,
    end_utc: datetime,
    exclude_reservation_id: int | None = None,
) -> list[dict]:
    """Active reservations overlapping ``[start_utc, end_utc)``.

    Strict inequality on both sides means reservations that merely touch
    (one ends exactly when the other starts) coexist peacefully.  When
    rescheduling, the reservation's own row is ignored.
    """
    if exclude_reservation_id is None:
        return cur.execute(
            "SELECT id, start_utc, end_utc FROM reservations "
            "WHERE point_id = %s AND status = 'active' "
            "AND start_utc < %s AND end_utc > %s ORDER BY id",
            (point_id, end_utc, start_utc),
        ).fetchall()
    return cur.execute(
        "SELECT id, start_utc, end_utc FROM reservations "
        "WHERE point_id = %s AND status = 'active' "
        "AND id <> %s "
        "AND start_utc < %s AND end_utc > %s ORDER BY id",
        (point_id, exclude_reservation_id, end_utc, start_utc),
    ).fetchall()


def get_reschedule_by_key(
    cur: psycopg.Cursor, reservation_id: int, idempotency_key: str
) -> dict | None:
    return cur.execute(
        f"SELECT {_RESCHEDULE_COLS} FROM reservation_reschedules "
        "WHERE reservation_id = %s AND idempotency_key = %s",
        (reservation_id, idempotency_key),
    ).fetchone()


def apply_reschedule(
    cur: psycopg.Cursor,
    row: dict,
    idempotency_key: str,
    expected_revision: int,
    start_utc: datetime,
    end_utc: datetime,
    rules_version: int,
) -> dict:
    """Advance a locked reservation to a new window and record the attempt.

    The caller has already verified status ("active", not started), the
    expected revision, the booking window, calendar containment and the
    absence of overlaps, and holds the point row lock.  The optimistic
    revision is re-checked here as a belt-and-braces guard.
    """
    current_revision = row["revision"]
    if current_revision != expected_revision:
        raise RevisionConflictError(
            f"expected revision {expected_revision} but reservation "
            f"{row['id']} is at revision {current_revision}",
            details={
                "reservation_id": row["id"],
                "expected_revision": expected_revision,
                "current_revision": current_revision,
            },
        )
    new_revision = current_revision + 1
    cur.execute(
        "UPDATE reservations SET start_utc = %s, end_utc = %s, "
        "rules_version = %s, revision = %s WHERE id = %s",
        (start_utc, end_utc, rules_version, new_revision, row["id"]),
    )
    return cur.execute(
        "INSERT INTO reservation_reschedules "
        "(reservation_id, point_id, idempotency_key, expected_revision, "
        " start_utc, end_utc, rules_version, revision) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
        f"RETURNING {_RESCHEDULE_COLS}",
        (
            row["id"],
            row["point_id"],
            idempotency_key,
            expected_revision,
            start_utc,
            end_utc,
            rules_version,
            new_revision,
        ),
    ).fetchone()


def cancel_reservation(
    cur: psycopg.Cursor, point_id: int, reservation_id: int, cancel_token: str | None
) -> tuple[dict, bool]:
    """Cancel one reservation; idempotent on repeat calls with the token.

    Returns ``(row, released)`` where ``released`` is True exactly when this
    call performed the cancellation (a repeat cancel of an already-cancelled
    reservation releases nothing and must not run the waitlist sweep).
    """
    row = get_reservation_for_update(cur, point_id, reservation_id)
    check_cancel_token(row, cancel_token)
    if row["status"] == "cancelled":
        return row, False
    row = cur.execute(
        f"UPDATE reservations SET status = 'cancelled', cancelled_at = now() "
        f"WHERE id = %s RETURNING {_RESERVATION_COLS}",
        (reservation_id,),
    ).fetchone()
    return row, True


def assert_reservations_fit(
    cur: psycopg.Cursor, point_id: int, timezone_name: str, payload: dict
) -> None:
    """Refuse a rule/timezone change that un-covers active reservations.

    Every not-yet-finished, non-cancelled reservation must remain fully
    inside one continuous open interval of the calendar produced by
    ``payload`` interpreted in ``timezone_name``.  Callers hold the point
    row lock, so this check is race-free against concurrent reservations.
    """
    now = datetime.now(timezone.utc)
    rows = cur.execute(
        "SELECT id, start_utc, end_utc FROM reservations "
        "WHERE point_id = %s AND status = 'active' AND end_utc > %s ORDER BY id",
        (point_id, now),
    ).fetchall()
    if not rows:
        return
    zone = get_zone(timezone_name)
    ruleset = payload_to_ruleset(payload)
    date_from = min(r["start_utc"] for r in rows).astimezone(zone).date()
    date_to = max(r["end_utc"] for r in rows).astimezone(zone).date()
    intervals = build_open_intervals(
        timezone_name, ruleset, date_from - timedelta(days=1), date_to + timedelta(days=1)
    )
    violating = [
        r["id"]
        for r in rows
        if not any(s <= r["start_utc"] and r["end_utc"] <= e for s, e in intervals)
    ]
    if violating:
        raise ReservationChangeConflictError(
            f"{len(violating)} active reservation(s) of point {point_id} would "
            f"no longer be fully open under the proposed change",
            details={"conflicting_reservation_ids": violating},
        )


# ---------------------------------------------------------------------------
# Waitlist (候补)
# ---------------------------------------------------------------------------

_WAITLIST_COLS = (
    "id, point_id, idempotency_key, start_utc, end_utc, rules_version, "
    "query_token, status, reservation_id, created_at"
)


def insert_waitlist_entry(
    cur: psycopg.Cursor,
    point_id: int,
    idempotency_key: str,
    start_utc: datetime,
    end_utc: datetime,
    rules_version: int,
) -> dict:
    return cur.execute(
        f"INSERT INTO waitlist_entries "
        f"(point_id, idempotency_key, start_utc, end_utc, rules_version, "
        f" query_token) "
        f"VALUES (%s, %s, %s, %s, %s, %s) "
        f"RETURNING {_WAITLIST_COLS}",
        (
            point_id,
            idempotency_key,
            start_utc,
            end_utc,
            rules_version,
            secrets.token_urlsafe(32),
        ),
    ).fetchone()


def get_waitlist_by_key(
    cur: psycopg.Cursor, point_id: int, idempotency_key: str
) -> dict | None:
    return cur.execute(
        f"SELECT {_WAITLIST_COLS} FROM waitlist_entries "
        "WHERE point_id = %s AND idempotency_key = %s",
        (point_id, idempotency_key),
    ).fetchone()


def get_waitlist_entry(
    cur: psycopg.Cursor, point_id: int, entry_id: int, *, for_update: bool = False
) -> dict:
    """Return one waitlist entry or raise 404; optionally lock the row."""
    clause = " FOR UPDATE" if for_update else ""
    row = cur.execute(
        f"SELECT {_WAITLIST_COLS} FROM waitlist_entries "
        f"WHERE point_id = %s AND id = %s{clause}",
        (point_id, entry_id),
    ).fetchone()
    if row is None:
        raise WaitlistEntryNotFoundError(
            f"waitlist entry {entry_id} of point {point_id} does not exist",
            details={"point_id": point_id, "waitlist_id": entry_id},
        )
    return row


def check_waitlist_token(row: dict, query_token: str | None) -> None:
    """Validate a waitlist capability token against a fetched entry."""
    if query_token is None:
        raise InvalidWaitlistTokenError("query_token is required")
    if not secrets.compare_digest(row["query_token"], query_token):
        raise InvalidWaitlistTokenError("query_token does not match")


def waitlist_position(cur: psycopg.Cursor, point_id: int, entry_id: int) -> int:
    """1-based place in the application-ordered queue of waiting entries."""
    return cur.execute(
        "SELECT COUNT(*) AS n FROM waitlist_entries "
        "WHERE point_id = %s AND status = 'waiting' AND id <= %s",
        (point_id, entry_id),
    ).fetchone()["n"]


def withdraw_waitlist_entry(
    cur: psycopg.Cursor, point_id: int, entry_id: int, query_token: str | None
) -> dict:
    """Withdraw a still-waiting entry; repeat withdrawal is idempotent.

    Only a ``waiting`` entry can move to ``withdrawn``; a promoted or expired
    entry cannot be withdrawn (nor can it be withdrawn twice).
    """
    row = get_waitlist_entry(cur, point_id, entry_id, for_update=True)
    check_waitlist_token(row, query_token)
    if row["status"] == "waiting":
        row = cur.execute(
            "UPDATE waitlist_entries SET status = 'withdrawn' "
            f"WHERE id = %s RETURNING {_WAITLIST_COLS}",
            (entry_id,),
        ).fetchone()
    elif row["status"] != "withdrawn":
        raise WaitlistEntryNotActiveError(
            f"waitlist entry {entry_id} is {row['status']} and cannot be withdrawn",
            details={
                "waitlist_id": entry_id,
                "status": row["status"],
                "reservation_id": row["reservation_id"],
            },
        )
    return row


def promote_waitlist(
    cur: psycopg.Cursor,
    point: dict,
    rules_row: dict,
    now: datetime,
) -> dict | None:
    """Process this point's waitlist after a cancel/reschedule release.

    Runs inside the caller's transaction while the point row is locked, so the
    released window, the waitlist queue and any concurrent reservation
    creation/reschedule serialize together.

    Waiting entries are scanned in application order (``id``):

    * an entry whose start time has already passed is marked ``expired``;
    * an entry that is not open under the *currently published* rules, or that
      still overlaps an active reservation, stays queued (``waiting``);
    * the earliest entry that is open and free is converted into a reservation
      exactly once, using a server-generated reservation idempotency key and
      the rules version of the *promotion* moment.

    At most one promotion happens per sweep (one release frees one slot).
    Returns the promoted entry row (with its new reservation id) or None.
    """
    point_id = point["id"]
    zone = get_zone(point["timezone"])
    ruleset = payload_to_ruleset(rules_row["payload"])
    rules_version = rules_row["version"]

    entries = cur.execute(
        f"SELECT {_WAITLIST_COLS} FROM waitlist_entries "
        "WHERE point_id = %s AND status = 'waiting' ORDER BY id",
        (point_id,),
    ).fetchall()
    promoted = None
    for entry in entries:
        start = entry["start_utc"]
        end = entry["end_utc"]
        if start <= now:
            cur.execute(
                "UPDATE waitlist_entries SET status = 'expired' WHERE id = %s",
                (entry["id"],),
            )
            continue
        date_from = (start - timedelta(days=1)).astimezone(zone).date()
        date_to = (end + timedelta(days=1)).astimezone(zone).date()
        intervals = build_open_intervals(
            point["timezone"], ruleset, date_from, date_to
        )
        is_open = any(s <= start and end <= e for s, e in intervals)
        # Against the post-release set of active reservations: touching is
        # allowed, strictly overlapping means the entry must keep waiting.
        still_busy = find_overlapping(cur, point_id, start, end)
        if not is_open or still_busy:
            # Not currently promotable: leave it in the queue and keep
            # checking later applicants — one of them may already fit.
            continue
        reservation = insert_reservation(
            cur,
            point_id,
            f"__waitlist_promotion__:{entry['id']}:{secrets.token_urlsafe(16)}",
            start,
            end,
            rules_version,
        )
        cur.execute(
            "UPDATE waitlist_entries SET status = 'promoted', "
            "reservation_id = %s WHERE id = %s AND status = 'waiting'",
            (reservation["id"], entry["id"]),
        )
        promoted = cur.execute(
            f"SELECT {_WAITLIST_COLS} FROM waitlist_entries WHERE id = %s",
            (entry["id"],),
        ).fetchone()
        break
    return promoted
