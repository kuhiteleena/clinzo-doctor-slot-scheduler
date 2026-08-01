# clinzo-doctor-slot-scheduler
# Clinzo Doctor Slot Scheduling — Design & Implementation

A backend service that turns a doctor's broad availability window into
discrete, bookable consultation slots, and lets patients book them without
ever double-booking the same slot — even under concurrent access.

Built for the Clinzo Backend Engineering Assessment.

---

## 1. Approach, in one paragraph

A doctor's availability window (e.g. "Monday 10:00–18:00") is **materialized**
into individual `Slot` rows the moment the window is created — not computed
on the fly at read time. Booking a slot is a strict two-phase operation:
**hold** (a short-lived reservation, atomically flips a row from `AVAILABLE`
to `HELD`) followed by **confirm** (flips `HELD` → `BOOKED`). Every one of
these state transitions is written as a single atomic conditional
`UPDATE ... WHERE status = X`, never a read-then-write — that's the entire
mechanism that makes concurrent booking safe. Cancellation and rescheduling
reuse the same primitives. Every transition is written to an append-only
`AuditLog` table for traceability.
---
## 2. Data model
| Table | Purpose |
|---|---|
| `doctors` | Identity + IANA timezone for display conversion |
| `availability_windows` | The doctor's stated availability + slot config (duration, buffer). Soft-deactivated, never hard-deleted, on edit/removal |
| `slots` | The unit everything is booked against. Carries `status`, an optimistic-lock `version`, and hold metadata (`held_until`, `held_by_patient_id`) |
| `bookings` | A patient's booking lifecycle (`HELD → CONFIRMED`, or `CANCELLED` / `RESCHEDULED` / `EXPIRED`), decoupled from `Slot.status` so we keep full history even after a slot cycles through many bookings over time |
| `audit_log` | Append-only: entity type/id, action, actor, timestamp, JSON details |

Slot and Booking are **deliberately separate tables** rather than one
merged row. A `Slot` is a fixed point in a doctor's calendar; a `Booking`
is a patient's relationship to it, and a single slot can accumulate many
booking rows over its lifetime (held → cancelled → rebooked → confirmed →
rescheduled away). Merging them would mean overwriting history on every
lifecycle transition — bad for the "auditability" requirement.

---

## 3. The concurrency problem, and how it's actually solved

**The race:** two patients hit "book the 11:15am slot" at the same instant.
Naive code reads the slot's status, checks it's `AVAILABLE` in application
code, then writes `BOOKED` — leaving a gap between the read and the write
where both requests can pass the check.

**The fix:** never read-then-write. Every booking-critical mutation in
`booking_service.py` is a single atomic conditional UPDATE, e.g.:

```sql
UPDATE slots SET status = 'HELD', held_until = ..., held_by_patient_id = ...
WHERE id = :slot_id
  AND (status = 'AVAILABLE' OR (status = 'HELD' AND held_until < now()))
```

The database's own row-level write serialization resolves the race — not
application code. Whichever request's transaction commits first "wins" the
row; the WHERE clause of every other concurrent request then matches zero
rows, and we check `rowcount == 0` to detect and reject the loser with a
clean `SlotUnavailableError` (surfaced as HTTP 409). No explicit lock,
mutex, or `SELECT ... FOR UPDATE` is required in application code — the
same conditional-UPDATE pattern is correct on SQLite (whole-DB write lock)
and Postgres (row-level locking) alike, which is also why swapping
`DATABASE_URL` from SQLite to Postgres requires no code changes.

**Proof, not just an argument:** `tests/test_concurrency.py` spins up 20
threads, each with its own DB connection, synchronized with a
`threading.Barrier` so they all fire at the same instant against one slot
row. Repeated across 15 trials (300 total attempts), exactly one thread
succeeds every single time; the other 19 cleanly fail with
`SlotUnavailableError`. A second test does the same race one level up, for
concurrent `confirm` attempts.

---

## 4. Reservation holds (why booking is two-phase, not one-shot)

Booking isn't "hold + instantly booked" — it's a **hold with a TTL**
(default 5 minutes), which the client must separately **confirm**. This
directly serves the "correctness under partial failure" requirement: real
booking flows usually have a step between "user clicked book" and "booking
is final" — payment, a confirmation email round-trip, a second consent
screen. If that step crashes or the user abandons it, a one-shot booking
would either leave the slot permanently stuck, or you'd need out-of-band
compensation logic to release it. With a hold:

- The slot is provisionally locked so nobody else can grab it mid-flow.
- If the client never confirms, `expire_stale_holds()` (run opportunistically
  before every slot listing/booking, and intended to also run on a
  scheduled job in production) atomically releases it back to `AVAILABLE`
  once `held_until` passes — self-healing, no manual cleanup needed.
- Confirmation is ownership-checked (`held_by_patient_id` must match) and
  atomic, so a stale/expired hold can't be confirmed out from under a
  reassigned slot.

---

## 5. Retroactive availability changes (requirement #5)

When a doctor edits or removes an availability window, `availability_service.deactivate_window()`
applies an explicit policy, since this is a product decision as much as a
technical one:

- **`AVAILABLE` slots** → immediately marked `REMOVED`. Nobody had a claim.
- **`HELD` slots** (a patient mid-checkout) → also `REMOVED` immediately.
  A hold isn't a confirmed commitment; the in-flight `confirm_booking()`
  call will simply fail cleanly with `InvalidBookingStateError`.
- **`BOOKED` slots** → **never auto-cancelled.** A confirmed appointment is
  a commitment to a patient that the system will not unilaterally break.
  The window is marked inactive, the booked slot and its booking are left
  completely untouched, and the still-booked slot IDs are returned to the
  caller (and audit-logged) so the doctor/support team can manually
  resolve the conflict — call the patient, offer a reschedule, etc.

---

## 6. Other requirements, and where they're handled

| Requirement | Where |
|---|---|
| Configurable slot duration | `AvailabilityWindow.slot_duration_minutes`, not hardcoded anywhere in `slot_service.py` |
| Buffer time between appointments | `AvailabilityWindow.buffer_minutes`; folded into the generation step size (`slot_duration + buffer`) |
| Time zones | All timestamps stored as naive UTC in the DB (`DateTime` columns); `Doctor.timezone` and a `display_tz` query param on the listing endpoint convert to local time only at the API boundary, via `zoneinfo` |
| Rescheduling preserves the original appointment on failure | `reschedule_booking()` holds the *new* slot **first**; only if that succeeds does it release the old slot and mark the old booking `RESCHEDULED`. If the new slot is taken by someone else, the original booking is provably untouched (see `test_reschedule_to_unavailable_slot_preserves_original_booking`) |
| Auditability | Every hold/confirm/cancel/reschedule/expire/window-deactivation writes an `AuditLog` row with actor, action, and JSON details |

---

## 7. Slot representation: materialized vs. computed — the tradeoff

**Chosen: materialized** (real `Slot` rows generated at window-creation
time), not slots computed on-the-fly from the window at read time.

Why: booking requires an atomic conditional UPDATE against a specific row.
You cannot take a row-level lock, or run `WHERE status = 'AVAILABLE'`, on a
slot that doesn't exist as a row yet. A computed-on-the-fly approach might
look cheaper for *listing* slots, but the instant a patient tries to book
one, it has to be materialized anyway — so we do it eagerly and keep the
booking path simple and uniform.

Cost of this choice: a doctor with a very large recurring availability
window (e.g. "every weekday, months in advance") generates many rows
up front. This is a reasonable and common tradeoff (most scheduling
systems — flights, restaurant bookings — do the same), and it's mitigated
by only materializing a rolling window (e.g. next 60–90 days) rather than
unbounded future time, which would be the natural next step for a
recurring-availability feature (see §9).

---

## 8. Assumptions

- A slot belongs to exactly one doctor; multi-doctor pooled booking
  (bonus item) is discussed in §9 but not implemented.
- `patient_id` and `actor` are passed in as plain strings by the caller —
  there's no auth/identity layer in this assessment; a real system would
  authenticate the caller and derive `patient_id` from a session/token
  rather than trusting the request body.
- Hold TTL defaults to 5 minutes; configurable per-call via
  `hold_ttl_seconds` in `hold_slot()`.
- A partially-fitting trailing slot at the end of a window (e.g. 40 minutes
  of availability with 15-minute slots) is dropped rather than shortened —
  consultations are assumed to be fixed-length, not elastic.
- `expire_stale_holds()` is called opportunistically (before listing/booking)
  in this codebase rather than wired to a real scheduler; in production this
  would also run as a periodic background job so holds expire even if no
  one happens to hit the API in the meantime.

---

## 9. Bonus discussion (not implemented, but designed for)

**Variable-length appointment types:** `AvailabilityWindow.slot_duration_minutes`
is already per-window, not global — so "first visit" (30 min) vs.
"follow-up" (15 min) windows are already supported by creating separate
windows per appointment type. A cleaner extension would add an
`AppointmentType` table (name, duration) and reference it from
`AvailabilityWindow` instead of storing duration directly.

**Waitlist:** would be a new `Waitlist` table (`slot_id` or a
time-range preference, `patient_id`, `created_at`). On `cancel_booking()`,
instead of just flipping the slot to `AVAILABLE`, check for a waiting
patient FIFO-style and atomically offer them a short hold before opening it
to general availability — same atomic-UPDATE pattern, just with an extra
priority check baked into the WHERE clause window.

**Booking across multiple doctors** (e.g. "any dermatologist available at
3pm"): the current model already supports querying `Slot` across many
`doctor_id`s in one listing query. Booking would work identically — hold
whichever specific slot (with its specific `doctor_id`) the patient picks —
so the main addition needed is a search/ranking layer on top (e.g. "give me
the earliest slot across doctors X, Y, Z"), not a change to the booking
primitive itself, since the race-safety guarantee is per-slot-row and
doesn't care which doctor owns it.

---

## 10. Setup & running

### Requirements
- Python 3.11+
- (Optional) A Postgres instance if you want to run against Postgres
  instead of the zero-setup default SQLite

### Install
```bash
python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### Run the tests (17 tests: generation, booking lifecycle, concurrency proof)
```bash
pytest tests/ -v
```

### Run the API
```bash
uvicorn app.main:app --reload --port 8000
```
Interactive docs: **http://127.0.0.1:8000/docs**

### Point it at Postgres instead of SQLite (optional)
```bash
export DATABASE_URL="postgresql+psycopg://user:pass@host:5432/dbname"
pip install "psycopg[binary]"
uvicorn app.main:app --reload --port 8000
```

### Example end-to-end flow (curl)
```bash
# 1. Create a doctor
curl -X POST localhost:8000/doctors -H "Content-Type: application/json" \
  -d '{"name":"Dr. Priya Sharma","timezone":"Asia/Kolkata"}'

# 2. Create availability -> auto-generates slots
curl -X POST localhost:8000/doctors/<DOCTOR_ID>/availability -H "Content-Type: application/json" \
  -d '{"start_utc":"2026-08-04T04:30:00","end_utc":"2026-08-04T12:30:00","slot_duration_minutes":15,"buffer_minutes":5}'

# 3. List available slots
curl "localhost:8000/doctors/<DOCTOR_ID>/slots?on_date=2026-08-04&display_tz=Asia/Kolkata"

# 4. Hold a slot
curl -X POST localhost:8000/slots/<SLOT_ID>/hold -H "Content-Type: application/json" \
  -d '{"patient_id":"patient-1"}'

# 5. Confirm the hold
curl -X POST localhost:8000/bookings/<BOOKING_ID>/confirm -H "Content-Type: application/json" \
  -d '{"patient_id":"patient-1"}'

# 6. Cancel it
curl -X POST localhost:8000/bookings/<BOOKING_ID>/cancel -H "Content-Type: application/json" \
  -d '{"actor":"patient-1"}'

# 7. Reschedule a confirmed booking to a different slot
curl -X POST localhost:8000/bookings/<BOOKING_ID>/reschedule -H "Content-Type: application/json" \
  -d '{"patient_id":"patient-1","new_slot_id":"<NEW_SLOT_ID>"}'

# 8. Remove/deactivate a doctor's availability window
curl -X DELETE "localhost:8000/availability/<WINDOW_ID>?actor=dr-priya"
```

---

## 11. What I'd do differently for a real production system

- Wire `expire_stale_holds()` to an actual scheduled job (cron/Celery
  beat/etc.) rather than only running it opportunistically on request.
- Add authentication and derive `patient_id`/`actor` from a verified
  session instead of trusting the request body.
- Materialize slots on a rolling window (e.g. next 90 days) for recurring
  availability, with a background job extending the horizon, rather than
  generating unboundedly far into the future at window-creation time.
- Add rate limiting / idempotency keys on the hold endpoint to protect
  against a retry storm from a flaky client re-issuing the same hold
  request.
- Move from `datetime.utcnow()` (deprecated in newer Python) to
  timezone-aware `datetime.now(timezone.utc)` throughout.
