#!/usr/bin/env bash
# End-to-end usage example for the recycling-point service calendar API.
# Requires: bash, curl.  Assumes the API is up (default: http://localhost:8000).
#
#   ./examples/curl_examples.sh                 # uses BASE_URL below
#   BASE_URL=http://localhost:8000 ./examples/curl_examples.sh
set -euo pipefail

BASE_URL="${BASE_URL:-http://localhost:8000}"
api() { curl -sS -X "$1" "${BASE_URL}$2" ${3:+-H 'Content-Type: application/json' -d "$3"}; echo; }

echo "== 1. Create a recycling point (IANA timezone) =="
POINT_JSON="$(api POST /points '{"name":"阳光社区回收点","timezone":"Asia/Shanghai"}')"
echo "$POINT_JSON"
PID="$(echo "$POINT_JSON" | sed -n 's/.*"id":\([0-9]*\).*/\1/p')"
echo "point id = $PID"

echo
echo "== 2. Publish rules v1 (optimistic on expected_version=0) =="
api POST "/points/$PID/rules" '{
  "expected_version": 0,
  "rules": {
    "weekly": [
      {"weekday": 1, "start_time": "09:00", "end_time": "17:00"},
      {"weekday": 3, "start_time": "09:00", "end_time": "17:00"},
      {"weekday": 5, "start_time": "20:00", "end_time": "02:00"}
    ],
    "closed_dates": ["2026-10-01"],
    "makeup": [
      {"day": "2026-10-01", "start_time": "13:00", "end_time": "16:00"}
    ]
  }
}'

echo
echo "== 3. Re-publishing with the stale expected_version 0 conflicts =="
api POST "/points/$PID/rules" '{
  "expected_version": 0,
  "rules": {"weekly": [], "closed_dates": [], "makeup": []}
}'

echo
echo "== 4. Query the calendar (inclusive local dates, both ends included) =="
api GET "/points/$PID/calendar?from=2026-09-28&to=2026-10-04"

echo
echo "== 5. Query pinned to the published version 1 =="
api GET "/points/$PID/calendar?from=2026-09-28&to=2026-10-04&version=1"

echo
echo "== 6. List and inspect published versions =="
api GET "/points/$PID/rules/versions"
api GET "/points/$PID/rules/versions/1"

echo
echo "== Error examples =="
echo "-- unknown timezone --"
api POST /points '{"name":"bad","timezone":"Mars/Olympus"}'
echo "-- invalid period (weekly zero length) --"
api POST "/points/$PID/rules" '{"expected_version":1,"rules":{
  "weekly":[{"weekday":2,"start_time":"10:00","end_time":"10:00"}],
  "closed_dates":[],"makeup":[]}}'
echo "-- no published rules (create another point) --"
EMPTY="$(api POST /points '{"name":"未配置点","timezone":"UTC"}')"
EPID="$(echo "$EMPTY" | sed -n 's/.*"id":\([0-9]*\).*/\1/p')"
api GET "/points/$EPID/calendar?from=2026-09-28&to=2026-10-04"

echo
echo "== 7. Reservations (booked against the published calendar) =="
# Reservations must start within the next 30 days, so compute the window
# dynamically: tomorrow (UTC), with a weekly rule covering its weekday.
D1="$(date -u -d '+1 day' +%F 2>/dev/null || date -u -v+1d +%F)"
WD="$(date -u -d "$D1" +%u 2>/dev/null || date -u -j -f '%F' "$D1" +%u)"

echo "-- create a UTC point and publish a rule covering tomorrow ($D1, weekday $WD) --"
RPOINT_JSON="$(api POST /points '{"name":"预约演示点","timezone":"UTC"}')"
echo "$RPOINT_JSON"
RPID="$(echo "$RPOINT_JSON" | sed -n 's/.*"id":\([0-9]*\).*/\1/p')"
api POST "/points/$RPID/rules" "{
  \"expected_version\": 0,
  \"rules\": {
    \"weekly\": [{\"weekday\": $WD, \"start_time\": \"08:00\", \"end_time\": \"12:00\"}],
    \"closed_dates\": [],
    \"makeup\": []
  }
}"

echo
echo "-- book 09:00-10:00 UTC tomorrow (201) --"
RES_JSON="$(api POST "/points/$RPID/reservations" "{
  \"idempotency_key\": \"demo-visit-1\",
  \"start\": \"${D1}T09:00:00Z\",
  \"end\": \"${D1}T10:00:00Z\"
}")"
echo "$RES_JSON"
RID="$(echo "$RES_JSON" | sed -n 's/.*"id":\([0-9]*\).*/\1/p')"
TOKEN="$(echo "$RES_JSON" | sed -n 's/.*"cancel_token":"\([^"]*\)".*/\1/p')"

echo
echo "-- same key + same content replays the original result (200) --"
api POST "/points/$RPID/reservations" "{
  \"idempotency_key\": \"demo-visit-1\",
  \"start\": \"${D1}T09:00:00Z\",
  \"end\": \"${D1}T10:00:00Z\"
}"

echo
echo "-- same key + different content conflicts (409 idempotency_conflict) --"
api POST "/points/$RPID/reservations" "{
  \"idempotency_key\": \"demo-visit-1\",
  \"start\": \"${D1}T10:00:00Z\",
  \"end\": \"${D1}T11:00:00Z\"
}"

echo
echo "-- overlapping window rejected (409 reservation_overlap) --"
api POST "/points/$RPID/reservations" "{
  \"idempotency_key\": \"demo-visit-2\",
  \"start\": \"${D1}T09:30:00Z\",
  \"end\": \"${D1}T10:30:00Z\"
}"

echo
echo "-- reschedule 09:00-10:00 -> 10:00-11:00 with the cancel token (200, revision 2) --"
api POST "/points/$RPID/reservations/$RID/reschedule" "{
  \"cancel_token\": \"$TOKEN\",
  \"idempotency_key\": \"demo-reschedule-1\",
  \"expected_revision\": 1,
  \"start\": \"${D1}T10:00:00Z\",
  \"end\": \"${D1}T11:00:00Z\"
}"

echo
echo "-- same reschedule key + same content replays the attempt (200, still revision 2) --"
api POST "/points/$RPID/reservations/$RID/reschedule" "{
  \"cancel_token\": \"$TOKEN\",
  \"idempotency_key\": \"demo-reschedule-1\",
  \"expected_revision\": 1,
  \"start\": \"${D1}T10:00:00Z\",
  \"end\": \"${D1}T11:00:00Z\"
}"

echo
echo "-- same reschedule key + different content conflicts (409 idempotency_conflict) --"
api POST "/points/$RPID/reservations/$RID/reschedule" "{
  \"cancel_token\": \"$TOKEN\",
  \"idempotency_key\": \"demo-reschedule-1\",
  \"expected_revision\": 2,
  \"start\": \"${D1}T08:00:00Z\",
  \"end\": \"${D1}T09:00:00Z\"
}"

echo
echo "-- stale expected_revision conflicts (409 revision_conflict) --"
api POST "/points/$RPID/reservations/$RID/reschedule" "{
  \"cancel_token\": \"$TOKEN\",
  \"idempotency_key\": \"demo-reschedule-2\",
  \"expected_revision\": 1,
  \"start\": \"${D1}T08:00:00Z\",
  \"end\": \"${D1}T09:00:00Z\"
}"

echo
echo "-- reschedule without a valid token is rejected (403 invalid_cancel_token) --"
api POST "/points/$RPID/reservations/$RID/reschedule" "{
  \"cancel_token\": \"wrong\",
  \"idempotency_key\": \"demo-reschedule-3\",
  \"expected_revision\": 2,
  \"start\": \"${D1}T08:00:00Z\",
  \"end\": \"${D1}T09:00:00Z\"
}"

echo
echo "-- the original creation key still replays the first-created window (200, revision 1) --"
api POST "/points/$RPID/reservations" "{
  \"idempotency_key\": \"demo-visit-1\",
  \"start\": \"${D1}T09:00:00Z\",
  \"end\": \"${D1}T10:00:00Z\"
}"

echo
echo "-- the old window was freed by rescheduling: 09:00-10:00 can be booked again (201) --"
api POST "/points/$RPID/reservations" "{
  \"idempotency_key\": \"demo-visit-3\",
  \"start\": \"${D1}T09:00:00Z\",
  \"end\": \"${D1}T10:00:00Z\"
}"

echo
echo "-- outside open hours (409 outside_open_hours) --"
api POST "/points/$RPID/reservations" "{
  \"idempotency_key\": \"demo-visit-4\",
  \"start\": \"${D1}T13:00:00Z\",
  \"end\": \"${D1}T14:00:00Z\"
}"

echo
echo "-- publishing rules that un-cover active reservations is refused (409) --"
api POST "/points/$RPID/rules" '{
  "expected_version": 1,
  "rules": {"weekly": [], "closed_dates": [], "makeup": []}
}'

echo
echo "-- cancel with a wrong token (403 invalid_cancel_token) --"
api POST "/points/$RPID/reservations/$RID/cancel" '{"cancel_token":"wrong"}'

echo
echo "-- cancel with the issued token (200), then repeat cancel stays cancelled --"
api POST "/points/$RPID/reservations/$RID/cancel" "{\"cancel_token\":\"$TOKEN\"}"
api POST "/points/$RPID/reservations/$RID/cancel" "{\"cancel_token\":\"$TOKEN\"}"

echo
echo "-- the freed window can be booked again (201) --"
api POST "/points/$RPID/reservations" "{
  \"idempotency_key\": \"demo-visit-5\",
  \"start\": \"${D1}T10:00:00Z\",
  \"end\": \"${D1}T11:00:00Z\"
}"

echo
echo "== 8. Waitlist (候补) =="
# A dedicated point for the waitlist walk-through.
WPOINT_JSON="$(api POST /points '{"name":"候补演示点","timezone":"UTC"}')"
echo "$WPOINT_JSON"
WPID="$(echo "$WPOINT_JSON" | sed -n 's/.*"id":\([0-9]*\).*/\1/p')"
api POST "/points/$WPID/rules" "{
  \"expected_version\": 0,
  \"rules\": {
    \"weekly\": [{\"weekday\": $WD, \"start_time\": \"08:00\", \"end_time\": \"12:00\"}],
    \"closed_dates\": [],
    \"makeup\": []
  }
}"

echo
echo "-- someone holds 08:00-08:30 (201) --"
HOLD_JSON="$(api POST "/points/$WPID/reservations" "{
  \"idempotency_key\": \"demo-holder\",
  \"start\": \"${D1}T08:00:00Z\",
  \"end\": \"${D1}T08:30:00Z\"
}")"
echo "$HOLD_JSON"
HOLD_ID="$(echo "$HOLD_JSON" | sed -n 's/.*"id":\([0-9]*\).*/\1/p')"
HOLD_TOKEN="$(echo "$HOLD_JSON" | sed -n 's/.*"cancel_token":"\([^"]*\)".*/\1/p')"

echo
echo "-- join the waitlist for the occupied window (201: number + query key) --"
WAIT_JSON="$(api POST "/points/$WPID/waitlist" "{
  \"idempotency_key\": \"demo-wait-1\",
  \"start\": \"${D1}T08:00:00Z\",
  \"end\": \"${D1}T08:30:00Z\"
}")"
echo "$WAIT_JSON"
WID="$(echo "$WAIT_JSON" | sed -n 's/.*"id":\([0-9]*\).*/\1/p')"
WTOKEN="$(echo "$WAIT_JSON" | sed -n 's/.*"query_token":"\([^"]*\)".*/\1/p')"

echo
echo "-- same point + same key + same content replays the application (200) --"
api POST "/points/$WPID/waitlist" "{
  \"idempotency_key\": \"demo-wait-1\",
  \"start\": \"${D1}T08:00:00Z\",
  \"end\": \"${D1}T08:30:00Z\"
}"

echo
echo "-- same key with different content conflicts (409 idempotency_conflict) --"
api POST "/points/$WPID/waitlist" "{
  \"idempotency_key\": \"demo-wait-1\",
  \"start\": \"${D1}T08:15:00Z\",
  \"end\": \"${D1}T08:45:00Z\"
}"

echo
echo "-- a free window is not accepted onto the waitlist (409 waitlist_window_available) --"
api POST "/points/$WPID/waitlist" "{
  \"idempotency_key\": \"demo-wait-free\",
  \"start\": \"${D1}T11:00:00Z\",
  \"end\": \"${D1}T11:30:00Z\"
}"

echo
echo "-- status query with the query key (200; position is the queue place) --"
api GET "/points/$WPID/waitlist/$WID?query_token=$WTOKEN"

echo
echo "-- status query with a wrong key is rejected (403 invalid_waitlist_token) --"
api GET "/points/$WPID/waitlist/$WID?query_token=wrong"

echo
echo "-- another queued application, then withdrawn with its query key (200) --"
WAIT2_JSON="$(api POST "/points/$WPID/waitlist" "{
  \"idempotency_key\": \"demo-wait-2\",
  \"start\": \"${D1}T08:00:00Z\",
  \"end\": \"${D1}T08:15:00Z\"
}")"
echo "$WAIT2_JSON"
WID2="$(echo "$WAIT2_JSON" | sed -n 's/.*"id":\([0-9]*\).*/\1/p')"
WTOKEN2="$(echo "$WAIT2_JSON" | sed -n 's/.*"query_token":"\([^"]*\)".*/\1/p')"
api POST "/points/$WPID/waitlist/$WID2/withdraw" "{\"query_token\":\"$WTOKEN2\"}"

echo
echo "-- cancel the holder: the earliest matching entry is promoted in one transaction --"
api POST "/points/$WPID/reservations/$HOLD_ID/cancel" "{\"cancel_token\":\"$HOLD_TOKEN\"}"

echo
echo "-- waitlist status now reports promoted + reservation number/cancel token --"
api GET "/points/$WPID/waitlist/$WID?query_token=$WTOKEN"
