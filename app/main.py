"""FastAPI application: recycling-point service calendar HTTP API."""

from __future__ import annotations

import secrets
from datetime import date, datetime, timedelta, timezone
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
import psycopg

from . import db
from .config import settings
from .errors import ApiError
from .errors import (
    IdempotencyConflictError,
    InvalidPeriodError,
    InvalidRequestError,
    InvalidWaitlistTokenError,
    OutsideOpenHoursError,
    ReservationNotReschedulableError,
    ReservationOverlapError,
    RevisionConflictError,
    WaitlistWindowAvailableError,
)
from .schemas import (
    CalendarOut,
    DayOut,
    IntervalOut,
    PointCreateIn,
    PointOut,
    PointUpdateIn,
    PublishOut,
    PublishRulesIn,
    ReassignOut,
    ReservationCancelIn,
    ReservationCreateIn,
    ReservationOut,
    ReservationReassignIn,
    ReservationRescheduleIn,
    ReservationStateOut,
    VersionOut,
    VersionSummaryOut,
    WaitlistCreateIn,
    WaitlistOut,
    WaitlistTokenIn,
    utc_iso,
    validate_reservation_window,
    validate_rules_payload,
    validate_timezone,
)
from .scheduler import build_calendar, build_open_intervals, get_zone
from .db import payload_to_ruleset


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db(settings.database_url)
    yield
    db.close_pool()


app = FastAPI(
    title="Recycling Point Service Calendar API",
    version="1.0.0",
    description=(
        "Versioned weekly openings, full-day closures and same-day makeup "
        "openings for community recycling points, expanded to UTC intervals."
    ),
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Error handling — every error is a distinguishable JSON envelope
# ---------------------------------------------------------------------------

@app.exception_handler(ApiError)
async def api_error_handler(request: Request, exc: ApiError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "invalid_request",
                "message": "request payload failed validation",
                # Validator exceptions live in the error context; render them
                # as text so the envelope is always JSON-serializable.
                "details": {
                    "errors": jsonable_encoder(
                        exc.errors(), custom_encoder={Exception: str}
                    )
                },
            }
        },
    )


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.get("/health", tags=["health"])
def health() -> dict:
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Points
# ---------------------------------------------------------------------------

@app.post("/points", response_model=PointOut, status_code=201, tags=["points"])
def create_point(body: PointCreateIn) -> PointOut:
    validate_timezone(body.timezone)
    with db.conn() as connection:
        with connection.transaction():
            row = db.create_point(connection.cursor(), body.name, body.timezone)
    return PointOut(**row)


@app.get("/points/{point_id}", response_model=PointOut, tags=["points"])
def read_point(point_id: int) -> PointOut:
    with db.conn() as connection:
        row = db.get_point(connection.cursor(), point_id)
    return PointOut(**row)


@app.patch("/points/{point_id}", response_model=PointOut, tags=["points"])
def patch_point(point_id: int, body: PointUpdateIn) -> PointOut:
    fields = body.model_dump(exclude_unset=True)
    if "timezone" in fields:
        validate_timezone(fields["timezone"])
    with db.conn() as connection:
        with connection.transaction():
            cur = connection.cursor()
            if "timezone" in fields:
                # A timezone change re-interprets every wall-clock rule; it is
                # refused when it would un-cover an active reservation.  The
                # point row lock serializes this check against concurrent
                # reservation creations.
                db.lock_point(cur, point_id)
                latest = db.get_latest_payload(cur, point_id)
                if latest is not None:
                    db.assert_reservations_fit(
                        cur, point_id, fields["timezone"], latest["payload"]
                    )
            row = db.update_point(cur, point_id, **fields)
    return PointOut(**row)


# ---------------------------------------------------------------------------
# Versioned rule sets
# ---------------------------------------------------------------------------

@app.post(
    "/points/{point_id}/rules",
    response_model=PublishOut,
    status_code=201,
    tags=["rules"],
)
def publish_rules(point_id: int, body: PublishRulesIn) -> PublishOut:
    # Semantic validation (invalid periods, duplicates) before touching the DB.
    payload = validate_rules_payload(body.rules)
    with db.conn() as connection:
        with connection.transaction():
            row = db.publish_rules(
                connection.cursor(), point_id, body.expected_version, payload
            )
    return PublishOut(
        point_id=point_id, version=row["version"], published_at=row["published_at"]
    )


@app.get(
    "/points/{point_id}/rules",
    response_model=VersionOut,
    tags=["rules"],
)
def read_current_rules(point_id: int) -> VersionOut:
    with db.conn() as connection:
        point, row = db.get_current_rules(connection.cursor(), point_id)
    return VersionOut(
        point_id=point_id,
        version=row["version"],
        published_at=row["published_at"],
        rules=row["payload"],
    )


@app.get(
    "/points/{point_id}/rules/versions",
    response_model=list[VersionSummaryOut],
    tags=["rules"],
)
def list_rule_versions(point_id: int) -> list[VersionSummaryOut]:
    with db.conn() as connection:
        rows = db.list_versions(connection.cursor(), point_id)
    return [VersionSummaryOut(**row) for row in rows]


@app.get(
    "/points/{point_id}/rules/versions/{version}",
    response_model=VersionOut,
    tags=["rules"],
)
def read_rule_version(point_id: int, version: int) -> VersionOut:
    with db.conn() as connection:
        row = db.get_version(connection.cursor(), point_id, version)
    return VersionOut(
        point_id=point_id,
        version=row["version"],
        published_at=row["published_at"],
        rules=row["payload"],
    )


# ---------------------------------------------------------------------------
# Calendar query — always against one published version
# ---------------------------------------------------------------------------

@app.get(
    "/points/{point_id}/calendar",
    response_model=CalendarOut,
    tags=["calendar"],
)
def calendar(
    point_id: int,
    date_from: date = Query(..., alias="from"),
    date_to: date = Query(..., alias="to"),
    version: int | None = Query(
        default=None,
        ge=1,
        description="Pin the query to one published version; defaults to latest.",
    ),
) -> CalendarOut:
    if date_to < date_from:
        raise InvalidPeriodError("'to' must be on or after 'from'")
    span = (date_to - date_from).days + 1
    if span > settings.max_query_days:
        raise InvalidPeriodError(
            f"date range spans {span} days; maximum is {settings.max_query_days}",
            details={"max_query_days": settings.max_query_days, "span_days": span},
        )
    with db.conn() as connection:
        # Repeatable-read snapshot: the point row and the loaded rule version
        # always agree, even if a concurrent publish commits mid-query.
        previous_level = connection.isolation_level
        connection.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
        try:
            with connection.transaction():
                cur = connection.cursor()
                if version is None:
                    point, row = db.get_current_rules(cur, point_id)
                else:
                    point, row = db.get_pinned_rules(cur, point_id, version)
        finally:
            connection.isolation_level = previous_level
    used_version = row["version"]
    ruleset = payload_to_ruleset(row["payload"])
    days = build_calendar(point["timezone"], ruleset, date_from, date_to)
    return CalendarOut(
        point_id=point_id,
        version=used_version,
        timezone=point["timezone"],
        date_from=date_from,
        date_to=date_to,
        days=[
            DayOut(
                local_date=d.local_date,
                intervals=[
                    IntervalOut(
                        start=utc_iso(start),
                        end=utc_iso(end),
                        sources=[s.as_dict() for s in sources],
                    )
                    for start, end, sources in d.intervals
                ],
            )
            for d in days
        ],
    )


# ---------------------------------------------------------------------------
# Reservations — booked against the currently published calendar
# ---------------------------------------------------------------------------

def _reservation_out(row: dict) -> ReservationOut:
    return ReservationOut(
        id=row["id"],
        point_id=row["point_id"],
        status=row["status"],
        revision=row["revision"],
        start=utc_iso(row["start_utc"]),
        end=utc_iso(row["end_utc"]),
        rules_version=row["rules_version"],
        cancel_token=row["cancel_token"],
        created_at=row["created_at"],
        cancelled_at=row["cancelled_at"],
    )


def _creation_replay_out(row: dict) -> ReservationOut:
    """First-creation snapshot, with the reservation's live status.

    A create-key replay always reports the *original* created window, rules
    version and revision 1 (even after reschedules), while status /
    cancelled_at reflect the reservation as it stands now.
    """
    return ReservationOut(
        id=row["id"],
        point_id=row["point_id"],
        status=row["status"],
        revision=1,
        start=utc_iso(row["created_start_utc"]),
        end=utc_iso(row["created_end_utc"]),
        rules_version=row["created_rules_version"],
        cancel_token=row["cancel_token"],
        created_at=row["created_at"],
        cancelled_at=row["cancelled_at"],
    )


def _reservation_state_out(row: dict) -> ReservationStateOut:
    return ReservationStateOut(
        id=row["id"],
        point_id=row["point_id"],
        status=row["status"],
        revision=row["revision"],
        start=utc_iso(row["start_utc"]),
        end=utc_iso(row["end_utc"]),
        rules_version=row["rules_version"],
        created_at=row["created_at"],
        cancelled_at=row["cancelled_at"],
    )


@app.post(
    "/points/{point_id}/reservations",
    response_model=ReservationOut,
    status_code=201,
    tags=["reservations"],
)
def create_reservation(
    point_id: int, body: ReservationCreateIn, response: Response
) -> ReservationOut:
    start = body.start.astimezone(timezone.utc)
    end = body.end.astimezone(timezone.utc)
    now = datetime.now(timezone.utc)
    with db.conn() as connection:
        with connection.transaction():
            cur = connection.cursor()
            # The point row lock serializes creations against each other and
            # against rule publishes / timezone changes for this point.
            point = db.lock_point(cur, point_id)

            # Idempotency: the first successful request wins.  A same-content
            # retry replays the *first creation* snapshot (even if it has since
            # been rescheduled or cancelled); different content under the same
            # key conflicts.
            existing = db.get_reservation_by_key(
                cur, point_id, body.idempotency_key
            )
            if existing is not None:
                if (
                    existing["created_start_utc"] == start
                    and existing["created_end_utc"] == end
                ):
                    response.status_code = 200
                    return _creation_replay_out(existing)
                raise IdempotencyConflictError(
                    f"idempotency key {body.idempotency_key!r} was already used "
                    f"on point {point_id} with a different time window",
                    details={
                        "reservation_id": existing["id"],
                        "start": utc_iso(existing["created_start_utc"]),
                        "end": utc_iso(existing["created_end_utc"]),
                    },
                )

            validate_reservation_window(
                start,
                end,
                now,
                max_days_ahead=settings.reservation_max_days_ahead,
                max_duration=timedelta(
                    minutes=settings.reservation_max_duration_minutes
                ),
            )

            # The reservation must fit inside one continuous open interval of
            # the currently published calendar (intervals may cross local
            # midnight; they are not clipped per day here).
            _, rules_row = db.get_current_rules(cur, point_id)
            zone = get_zone(point["timezone"])
            ruleset = payload_to_ruleset(rules_row["payload"])
            date_from = (start - timedelta(days=1)).astimezone(zone).date()
            date_to = (end + timedelta(days=1)).astimezone(zone).date()
            intervals = build_open_intervals(
                point["timezone"], ruleset, date_from, date_to
            )
            if not any(s <= start and end <= e for s, e in intervals):
                raise OutsideOpenHoursError(
                    "reservation is not fully contained in one continuous "
                    "open interval of the published calendar",
                    details={
                        "start": utc_iso(start),
                        "end": utc_iso(end),
                        "rules_version": rules_row["version"],
                    },
                )

            overlapping = db.find_overlapping(cur, point_id, start, end)
            if overlapping:
                raise ReservationOverlapError(
                    "reservation overlaps an existing active reservation",
                    details={
                        "conflicting_reservation_ids": [r["id"] for r in overlapping]
                    },
                )

            row = db.insert_reservation(
                cur,
                point_id,
                body.idempotency_key,
                start,
                end,
                rules_row["version"],
            )
    return _reservation_out(row)


@app.post(
    "/points/{point_id}/reservations/{reservation_id}/cancel",
    response_model=ReservationStateOut,
    tags=["reservations"],
)
def cancel_reservation(
    point_id: int, reservation_id: int, body: ReservationCancelIn
) -> ReservationStateOut:
    with db.conn() as connection:
        with connection.transaction():
            cur = connection.cursor()
            # Take the point lock first (same order as every other mutating
            # path) so the subsequent waitlist sweep is race-free against
            # concurrent booking, rescheduling and rule publication.
            point = db.lock_point(cur, point_id)
            row, released = db.cancel_reservation(
                cur, point_id, reservation_id, body.cancel_token
            )
            if released:
                # A slot actually opened: promote the earliest eligible
                # waitlist entry in the same transaction.  If promotion fails,
                # the cancellation is rolled back together with it.
                _, rules_row = db.get_current_rules(cur, point_id)
                db.promote_waitlist(
                    cur, point, rules_row, datetime.now(timezone.utc)
                )
    return _reservation_state_out(row)


def _reschedule_replay_out(reservation: dict, attempt: dict) -> ReservationOut:
    """Build the response of a successful reschedule attempt on replay.

    The window / rules version / revision come from the stored attempt; the
    status fields come from the reservation's current live row (so a replay
    after a later cancellation reports ``cancelled``).
    """
    return ReservationOut(
        id=reservation["id"],
        point_id=reservation["point_id"],
        status=reservation["status"],
        revision=attempt["revision"],
        start=utc_iso(attempt["start_utc"]),
        end=utc_iso(attempt["end_utc"]),
        rules_version=attempt["rules_version"],
        cancel_token=reservation["cancel_token"],
        created_at=reservation["created_at"],
        cancelled_at=reservation["cancelled_at"],
    )


@app.post(
    "/points/{point_id}/reservations/{reservation_id}/reschedule",
    response_model=ReservationOut,
    tags=["reservations"],
)
def reschedule_reservation(
    point_id: int, reservation_id: int, body: ReservationRescheduleIn
) -> ReservationOut:
    start = body.start.astimezone(timezone.utc)
    end = body.end.astimezone(timezone.utc)
    now = datetime.now(timezone.utc)
    with db.conn() as connection:
        with connection.transaction():
            cur = connection.cursor()
            # Same per-point lock as creation / rule publishing / timezone
            # changes, so a reschedule never races a calendar change or
            # another booking, and calendar coverage + non-overlap hold under
            # concurrency.
            point = db.lock_point(cur, point_id)

            # 404 / 403 semantics mirror the cancel endpoint: existence first,
            # then the capability token.
            resv = db.get_reservation_for_update(cur, point_id, reservation_id)
            db.check_cancel_token(resv, body.cancel_token)

            # Reschedule idempotency is scoped to one reservation: same key +
            # same content replays that successful attempt (even when the
            # reservation has since moved on or been cancelled); same key +
            # different content conflicts.  Replay wins over every other
            # state/revision check.
            attempt = db.get_reschedule_by_key(
                cur, reservation_id, body.idempotency_key
            )
            if attempt is not None:
                if attempt["start_utc"] == start and attempt["end_utc"] == end:
                    return _reschedule_replay_out(resv, attempt)
                raise IdempotencyConflictError(
                    f"reschedule idempotency key {body.idempotency_key!r} was "
                    f"already used on reservation {reservation_id} with a "
                    f"different time window",
                    details={
                        "reservation_id": reservation_id,
                        "start": utc_iso(attempt["start_utc"]),
                        "end": utc_iso(attempt["end_utc"]),
                    },
                )

            if resv["status"] == "cancelled":
                raise ReservationNotReschedulableError(
                    f"reservation {reservation_id} is cancelled and cannot be "
                    f"rescheduled",
                    details={
                        "reservation_id": reservation_id,
                        "status": "cancelled",
                        "reason": "reservation_cancelled",
                    },
                )
            if resv["start_utc"] <= now:
                raise ReservationNotReschedulableError(
                    f"reservation {reservation_id} has already started and "
                    f"cannot be rescheduled",
                    details={
                        "reservation_id": reservation_id,
                        "status": resv["status"],
                        "reason": "reservation_started",
                        "start": utc_iso(resv["start_utc"]),
                        "now": utc_iso(now),
                    },
                )
            if resv["revision"] != body.expected_revision:
                raise RevisionConflictError(
                    f"expected revision {body.expected_revision} but reservation "
                    f"{reservation_id} is at revision {resv['revision']}",
                    details={
                        "reservation_id": reservation_id,
                        "expected_revision": body.expected_revision,
                        "current_revision": resv["revision"],
                    },
                )

            validate_reservation_window(
                start,
                end,
                now,
                max_days_ahead=settings.reservation_max_days_ahead,
                max_duration=timedelta(
                    minutes=settings.reservation_max_duration_minutes
                ),
            )

            # The new window must fit inside one continuous open interval of
            # the currently published calendar.
            _, rules_row = db.get_current_rules(cur, point_id)
            zone = get_zone(point["timezone"])
            ruleset = payload_to_ruleset(rules_row["payload"])
            date_from = (start - timedelta(days=1)).astimezone(zone).date()
            date_to = (end + timedelta(days=1)).astimezone(zone).date()
            intervals = build_open_intervals(
                point["timezone"], ruleset, date_from, date_to
            )
            if not any(s <= start and end <= e for s, e in intervals):
                raise OutsideOpenHoursError(
                    "rescheduled window is not fully contained in one "
                    "continuous open interval of the published calendar",
                    details={
                        "reservation_id": reservation_id,
                        "start": utc_iso(start),
                        "end": utc_iso(end),
                        "rules_version": rules_row["version"],
                    },
                )

            # Overlap against other active reservations; this reservation's
            # own (still held) old window is ignored.
            overlapping = db.find_overlapping(
                cur,
                point_id,
                start,
                end,
                exclude_reservation_id=reservation_id,
            )
            if overlapping:
                raise ReservationOverlapError(
                    "rescheduled window overlaps another active reservation",
                    details={
                        "conflicting_reservation_ids": [r["id"]]
                        for r in overlapping
                    },
                )

            attempt = db.apply_reschedule(
                cur,
                resv,
                body.idempotency_key,
                body.expected_revision,
                start,
                end,
                rules_row["version"],
            )
            # Moving away frees the old window: in the same transaction,
            # promote the earliest waitlist entry that now fits.  A failure
            # here rolls the reschedule back as well, so the old window stays
            # occupied.
            db.promote_waitlist(cur, point, rules_row, now)
            # Rescheduling only moves the window and bumps the revision;
            # status / cancel token / created_at are unchanged, so the locked
            # ``resv`` row plus the stored attempt fully describe the result.
            return _reschedule_replay_out(resv, attempt)


def _reassign_out(
    source_row: dict, target_row: dict, target_rules_version: int
) -> ReassignOut:
    """Fresh reassign result: cancelled source + newly booked target."""
    return ReassignOut(
        source_reservation=_reservation_state_out(source_row),
        target_reservation=_reservation_out(target_row),
        target_rules_version=target_rules_version,
    )


def _reassign_replay_out(cur, source_row: dict, attempt: dict) -> ReassignOut:
    """Replay of a successful reassign attempt.

    The target reservation is reported as its *first generated* snapshot
    (original window, target rules version, revision 1 — it is a fresh
    creation at the target point) while its status fields reflect the live
    row, mirroring creation-key replay semantics.  The source reservation is
    reported as it stands now (cancelled by the reassignment).
    """
    target_row = db.get_reservation_by_id(cur, attempt["target_reservation_id"])
    return ReassignOut(
        source_reservation=_reservation_state_out(source_row),
        target_reservation=_creation_replay_out(target_row),
        target_rules_version=attempt["target_rules_version"],
    )


@app.post(
    "/points/{point_id}/reservations/{reservation_id}/reassign",
    response_model=ReassignOut,
    tags=["reservations"],
)
def reassign_reservation(
    point_id: int, reservation_id: int, body: ReservationReassignIn
) -> ReassignOut:
    if body.target_point_id == point_id:
        raise InvalidRequestError(
            "target_point_id must name a different recycling point than the "
            "source; use the reschedule endpoint for same-point moves",
            details={"point_id": point_id, "target_point_id": body.target_point_id},
        )
    start = body.start.astimezone(timezone.utc)
    end = body.end.astimezone(timezone.utc)
    now = datetime.now(timezone.utc)
    with db.conn() as connection:
        with connection.transaction():
            cur = connection.cursor()
            # Lock both point rows in ascending id order.  Every other
            # mutation takes exactly one point lock, and a reassign in the
            # opposite direction takes the same two locks in the same order,
            # so concurrent reassigns / bookings / publishes serialize
            # without deadlock and cannot double-book a window.
            points = {}
            for pid in sorted({point_id, body.target_point_id}):
                points[pid] = db.lock_point(cur, pid)
            source_point = points[point_id]
            target_point = points[body.target_point_id]

            # 404 / 403 semantics mirror the cancel and reschedule endpoints:
            # existence first, then the capability token.
            resv = db.get_reservation_for_update(cur, point_id, reservation_id)
            db.check_cancel_token(resv, body.cancel_token)

            # Reassign idempotency is scoped to the source reservation: same
            # key + same content replays the first generated target
            # reservation; same key + different content conflicts.  Replay
            # wins over every other state/revision check.
            attempt = db.get_reassign_by_key(
                cur, reservation_id, body.idempotency_key
            )
            if attempt is not None:
                if (
                    attempt["target_point_id"] == body.target_point_id
                    and attempt["start_utc"] == start
                    and attempt["end_utc"] == end
                ):
                    return _reassign_replay_out(cur, resv, attempt)
                raise IdempotencyConflictError(
                    f"reassign idempotency key {body.idempotency_key!r} was "
                    f"already used on reservation {reservation_id} with "
                    f"different content",
                    details={
                        "reservation_id": reservation_id,
                        "target_point_id": attempt["target_point_id"],
                        "start": utc_iso(attempt["start_utc"]),
                        "end": utc_iso(attempt["end_utc"]),
                    },
                )

            if resv["status"] == "cancelled":
                raise ReservationNotReschedulableError(
                    f"reservation {reservation_id} is cancelled and cannot be "
                    f"reassigned",
                    details={
                        "reservation_id": reservation_id,
                        "status": "cancelled",
                        "reason": "reservation_cancelled",
                    },
                )
            if resv["start_utc"] <= now:
                raise ReservationNotReschedulableError(
                    f"reservation {reservation_id} has already started and "
                    f"cannot be reassigned",
                    details={
                        "reservation_id": reservation_id,
                        "status": resv["status"],
                        "reason": "reservation_started",
                        "start": utc_iso(resv["start_utc"]),
                        "now": utc_iso(now),
                    },
                )
            if resv["revision"] != body.expected_revision:
                raise RevisionConflictError(
                    f"expected revision {body.expected_revision} but reservation "
                    f"{reservation_id} is at revision {resv['revision']}",
                    details={
                        "reservation_id": reservation_id,
                        "expected_revision": body.expected_revision,
                        "current_revision": resv["revision"],
                    },
                )

            # The target window follows the same booking policy as an
            # ordinary reservation: lead time, duration, one continuous open
            # interval of the target point's currently published calendar,
            # and no overlap with that point's active reservations.
            validate_reservation_window(
                start,
                end,
                now,
                max_days_ahead=settings.reservation_max_days_ahead,
                max_duration=timedelta(
                    minutes=settings.reservation_max_duration_minutes
                ),
            )
            _, target_rules = db.get_current_rules(cur, body.target_point_id)
            zone = get_zone(target_point["timezone"])
            ruleset = payload_to_ruleset(target_rules["payload"])
            date_from = (start - timedelta(days=1)).astimezone(zone).date()
            date_to = (end + timedelta(days=1)).astimezone(zone).date()
            intervals = build_open_intervals(
                target_point["timezone"], ruleset, date_from, date_to
            )
            if not any(s <= start and end <= e for s, e in intervals):
                raise OutsideOpenHoursError(
                    "reassigned window is not fully contained in one "
                    "continuous open interval of the target point's "
                    "published calendar",
                    details={
                        "reservation_id": reservation_id,
                        "target_point_id": body.target_point_id,
                        "start": utc_iso(start),
                        "end": utc_iso(end),
                        "rules_version": target_rules["version"],
                    },
                )
            overlapping = db.find_overlapping(cur, body.target_point_id, start, end)
            if overlapping:
                raise ReservationOverlapError(
                    "reassigned window overlaps an active reservation at the "
                    "target point",
                    details={
                        "conflicting_reservation_ids": [r["id"]]
                        for r in overlapping
                    },
                )

            # All validations passed: cancel the source reservation (this
            # releases its window) and, per the existing release rules, try
            # to promote the origin point's waitlist in the same transaction.
            cancelled, released = db.cancel_reservation(
                cur, point_id, reservation_id, body.cancel_token
            )
            assert released  # status was verified "active" above
            _, source_rules = db.get_current_rules(cur, point_id)
            db.promote_waitlist(cur, source_point, source_rules, now)

            # Book the new reservation at the target point with a
            # server-generated creation key and a fresh cancel token.
            new_reservation = db.insert_reservation(
                cur,
                body.target_point_id,
                f"__reassign__:{reservation_id}:{secrets.token_urlsafe(16)}",
                start,
                end,
                target_rules["version"],
            )
            db.insert_reassign(
                cur,
                reservation_id=reservation_id,
                source_point_id=point_id,
                target_point_id=body.target_point_id,
                idempotency_key=body.idempotency_key,
                expected_revision=body.expected_revision,
                start_utc=start,
                end_utc=end,
                target_reservation_id=new_reservation["id"],
                target_rules_version=target_rules["version"],
            )
            # Any failure above (validation or waitlist promotion) aborts the
            # transaction, leaving the source reservation and the target
            # point's occupancy untouched.
            return _reassign_out(cancelled, new_reservation, target_rules["version"])


# ---------------------------------------------------------------------------
# Waitlist (候补) — applications for windows currently held by another visitor
# ---------------------------------------------------------------------------

def _waitlist_out(cur, entry: dict) -> WaitlistOut:
    """Build the public waitlist view; queue position only while waiting.

    A promoted entry embeds the reservation's *live* row, so its status and
    cancel token reflect later cancellation/rescheduling.
    """
    position = None
    reservation_out = None
    if entry["status"] == "waiting":
        position = db.waitlist_position(cur, entry["point_id"], entry["id"])
    elif entry["reservation_id"] is not None:
        reservation_row = db.get_reservation_by_id(cur, entry["reservation_id"])
        if reservation_row is not None:
            reservation_out = _reservation_out(reservation_row)
    return WaitlistOut(
        id=entry["id"],
        point_id=entry["point_id"],
        status=entry["status"],
        position=position,
        start=utc_iso(entry["start_utc"]),
        end=utc_iso(entry["end_utc"]),
        rules_version=entry["rules_version"],
        query_token=entry["query_token"],
        created_at=entry["created_at"],
        reservation=reservation_out,
    )


@app.post(
    "/points/{point_id}/waitlist",
    response_model=WaitlistOut,
    status_code=201,
    tags=["waitlist"],
)
def create_waitlist_entry(
    point_id: int, body: WaitlistCreateIn, response: Response
) -> WaitlistOut:
    start = body.start.astimezone(timezone.utc)
    end = body.end.astimezone(timezone.utc)
    now = datetime.now(timezone.utc)
    with db.conn() as connection:
        with connection.transaction():
            cur = connection.cursor()
            # Same per-point lock as creation / cancel / reschedule / rule
            # publishing: application order and every promotion decision are
            # taken against the same serialized state.
            point = db.lock_point(cur, point_id)

            existing = db.get_waitlist_by_key(
                cur, point_id, body.idempotency_key
            )
            if existing is not None:
                if (
                    existing["start_utc"] == start
                    and existing["end_utc"] == end
                ):
                    response.status_code = 200
                    return _waitlist_out(cur, existing)
                raise IdempotencyConflictError(
                    f"waitlist idempotency key {body.idempotency_key!r} was "
                    f"already used on point {point_id} with a different time "
                    f"window",
                    details={
                        "waitlist_id": existing["id"],
                        "start": utc_iso(existing["start_utc"]),
                        "end": utc_iso(existing["end_utc"]),
                    },
                )

            # The application window must itself be a legal booking window
            # (duration / lead time) and must be fully covered by the
            # currently published calendar — same rule version that would
            # govern an ordinary reservation.
            validate_reservation_window(
                start,
                end,
                now,
                max_days_ahead=settings.reservation_max_days_ahead,
                max_duration=timedelta(
                    minutes=settings.reservation_max_duration_minutes
                ),
            )
            _, rules_row = db.get_current_rules(cur, point_id)
            zone = get_zone(point["timezone"])
            ruleset = payload_to_ruleset(rules_row["payload"])
            date_from = (start - timedelta(days=1)).astimezone(zone).date()
            date_to = (end + timedelta(days=1)).astimezone(zone).date()
            intervals = build_open_intervals(
                point["timezone"], ruleset, date_from, date_to
            )
            if not any(s <= start and end <= e for s, e in intervals):
                raise OutsideOpenHoursError(
                    "waitlist window is not fully contained in one continuous "
                    "open interval of the published calendar",
                    details={
                        "start": utc_iso(start),
                        "end": utc_iso(end),
                        "rules_version": rules_row["version"],
                    },
                )

            # The waitlist is only for windows that are already taken: it must
            # overlap at least one active, non-cancelled reservation.  A free
            # window keeps going through the ordinary reservation flow.
            if not db.find_overlapping(cur, point_id, start, end):
                raise WaitlistWindowAvailableError(
                    "the requested window is currently open for booking; use "
                    "the reservation endpoint instead of the waitlist",
                    details={
                        "start": utc_iso(start),
                        "end": utc_iso(end),
                        "rules_version": rules_row["version"],
                    },
                )

            entry = db.insert_waitlist_entry(
                cur,
                point_id,
                body.idempotency_key,
                start,
                end,
                rules_row["version"],
            )
            return _waitlist_out(cur, entry)


@app.get(
    "/points/{point_id}/waitlist/{entry_id}",
    response_model=WaitlistOut,
    tags=["waitlist"],
)
def read_waitlist_entry(
    point_id: int, entry_id: int, query_token: str | None = None
) -> WaitlistOut:
    if query_token is None:
        # Distinguishable 403 instead of the framework's 422 for a missing
        # capability credential.
        raise InvalidWaitlistTokenError("query_token is required")
    with db.conn() as connection:
        with connection.transaction():
            cur = connection.cursor()
            entry = db.get_waitlist_entry(cur, point_id, entry_id)
            db.check_waitlist_token(entry, query_token)
            return _waitlist_out(cur, entry)


@app.post(
    "/points/{point_id}/waitlist/{entry_id}/withdraw",
    response_model=WaitlistOut,
    tags=["waitlist"],
)
def withdraw_waitlist_entry(
    point_id: int, entry_id: int, body: WaitlistTokenIn
) -> WaitlistOut:
    with db.conn() as connection:
        with connection.transaction():
            cur = connection.cursor()
            # Point lock first, then the entry row: same lock order as every
            # release sweep, so withdrawal cannot deadlock against a promotion.
            db.lock_point(cur, point_id)
            entry = db.withdraw_waitlist_entry(
                cur, point_id, entry_id, body.query_token
            )
            return _waitlist_out(cur, entry)
