-- Schema for the recycling-point service calendar.
-- Executed once at application startup (idempotent).

CREATE TABLE IF NOT EXISTS points (
    id          BIGSERIAL PRIMARY KEY,
    name        TEXT NOT NULL,
    timezone    TEXT NOT NULL,                 -- IANA tz database name
    version     INTEGER NOT NULL DEFAULT 0,    -- number of published versions
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- One row per *published* rule set.  Version 0 means "no rules yet" and is
-- never present; the point's current version always points at the newest row.
CREATE TABLE IF NOT EXISTS rule_versions (
    id          BIGSERIAL PRIMARY KEY,
    point_id    BIGINT NOT NULL REFERENCES points(id) ON DELETE CASCADE,
    version     INTEGER NOT NULL,
    payload     JSONB NOT NULL,                -- {weekly, closed_dates, makeup}
    published_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (point_id, version)
);

CREATE INDEX IF NOT EXISTS idx_rule_versions_point
    ON rule_versions (point_id, version DESC);

-- Visit reservations.  (point_id, idempotency_key) is the client's idempotency
-- scope: the first successful request wins, same-content retries replay it.
-- ``revision`` starts at 1 and is bumped on every successful reschedule; the
-- ``created_*`` columns are an immutable snapshot of the original creation, so
-- create-key replays keep returning the first-created window even after the
-- reservation has been rescheduled.
CREATE TABLE IF NOT EXISTS reservations (
    id                     BIGSERIAL PRIMARY KEY,
    point_id               BIGINT NOT NULL REFERENCES points(id) ON DELETE CASCADE,
    idempotency_key        TEXT NOT NULL,
    start_utc              TIMESTAMPTZ NOT NULL,
    end_utc                TIMESTAMPTZ NOT NULL,
    rules_version          INTEGER NOT NULL,        -- published version validated against
    cancel_token           TEXT NOT NULL,           -- capability token for cancellation
    status                 TEXT NOT NULL DEFAULT 'active'
                           CHECK (status IN ('active', 'cancelled')),
    revision               INTEGER NOT NULL DEFAULT 1,
    created_start_utc      TIMESTAMPTZ NOT NULL,
    created_end_utc        TIMESTAMPTZ NOT NULL,
    created_rules_version  INTEGER NOT NULL,
    created_at             TIMESTAMPTZ NOT NULL DEFAULT now(),
    cancelled_at           TIMESTAMPTZ,
    UNIQUE (point_id, idempotency_key)
);

-- Upgrade path for databases created before rescheduling existed: add the new
-- columns (if missing) and backfill the immutable creation snapshot.
ALTER TABLE reservations
    ADD COLUMN IF NOT EXISTS revision INTEGER NOT NULL DEFAULT 1;
ALTER TABLE reservations
    ADD COLUMN IF NOT EXISTS created_start_utc TIMESTAMPTZ;
ALTER TABLE reservations
    ADD COLUMN IF NOT EXISTS created_end_utc TIMESTAMPTZ;
ALTER TABLE reservations
    ADD COLUMN IF NOT EXISTS created_rules_version INTEGER;
UPDATE reservations
   SET created_start_utc = start_utc,
       created_end_utc = end_utc,
       created_rules_version = rules_version
 WHERE created_start_utc IS NULL
    OR created_end_utc IS NULL
    OR created_rules_version IS NULL;

CREATE INDEX IF NOT EXISTS idx_reservations_point_active
    ON reservations (point_id, start_utc) WHERE status = 'active';

-- One row per successful reschedule.  (reservation_id, idempotency_key) is the
-- idempotency scope of a reschedule attempt: same key + same content replays
-- this row (the result of that successful attempt), different content conflicts.
CREATE TABLE IF NOT EXISTS reservation_reschedules (
    id                BIGSERIAL PRIMARY KEY,
    reservation_id    BIGINT NOT NULL
                      REFERENCES reservations(id) ON DELETE CASCADE,
    point_id          BIGINT NOT NULL,
    idempotency_key   TEXT NOT NULL,
    expected_revision INTEGER NOT NULL,        -- optimistic revision the client saw
    start_utc         TIMESTAMPTZ NOT NULL,    -- window produced by this attempt
    end_utc           TIMESTAMPTZ NOT NULL,
    rules_version     INTEGER NOT NULL,        -- rules version validated against
    revision          INTEGER NOT NULL,        -- reservation revision after applying
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (reservation_id, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_reservation_reschedules_reservation
    ON reservation_reschedules (reservation_id);

-- Waitlist (候补) entries.  A visitor may only join the waitlist for a window
-- that *would* be bookable under the current rules but is currently taken by an
-- active (non-cancelled) reservation.  (point_id, idempotency_key) is the
-- idempotency scope, mirroring reservations: same key + same content replays
-- the original application, different content conflicts.
--
-- status lifecycle:
--   waiting   -- queued; ordered by (point_id, id), i.e. application order
--   promoted  -- converted into a reservation (reservation_id set), exactly once
--   expired   -- start time had already passed when a release sweep ran
--   withdrawn -- holder of query_token withdrew it while still waiting
CREATE TABLE IF NOT EXISTS waitlist_entries (
    id              BIGSERIAL PRIMARY KEY,
    point_id        BIGINT NOT NULL REFERENCES points(id) ON DELETE CASCADE,
    idempotency_key TEXT NOT NULL,
    start_utc       TIMESTAMPTZ NOT NULL,
    end_utc         TIMESTAMPTZ NOT NULL,
    rules_version   INTEGER NOT NULL,        -- published version validated at apply time
    query_token     TEXT NOT NULL,           -- capability token for status/withdraw
    status          TEXT NOT NULL DEFAULT 'waiting'
                    CHECK (status IN ('waiting', 'promoted', 'expired', 'withdrawn')),
    reservation_id  BIGINT REFERENCES reservations(id) ON DELETE SET NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (point_id, idempotency_key)
);

-- The release sweep locks the point row and then scans the waiting queue in
-- application order; this partial index is the queue.
CREATE INDEX IF NOT EXISTS idx_waitlist_point_waiting
    ON waitlist_entries (point_id, id) WHERE status = 'waiting';

CREATE INDEX IF NOT EXISTS idx_waitlist_point_reservation
    ON waitlist_entries (point_id, reservation_id);
