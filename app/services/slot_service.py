"""
Turns a doctor's wide availability window into discrete, bookable slots.

Design choice: MATERIALIZED slots (real rows), not computed-on-the-fly.
See README "Slot representation" section for the full tradeoff discussion.
Short version: booking needs a row to atomically UPDATE ... WHERE status=X
against. You cannot take a row-level lock on a slot that doesn't exist as
a row. Computing slots on the fly is fine for *display*, but the moment a
patient tries to book one, it has to be materialized anyway -- so we
materialize eagerly at window-creation time and keep it simple.
"""
import json
from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from app.models import AvailabilityWindow, Slot, SlotStatus, AuditLog, gen_id


class InvalidWindowError(Exception):
    pass


def generate_slots_for_window(db: Session, window: AvailabilityWindow) -> list[Slot]:
    """
    Divide [window.start_utc, window.end_utc) into
    window.slot_duration_minutes-long slots, leaving window.buffer_minutes
    of gap between the end of one slot and the start of the next.

    Any remainder that doesn't fit a full slot + buffer is simply dropped
    (a doctor available 10:00-10:40 with 15-min slots gets two slots:
    10:00-10:15 and 10:20-10:35 if buffer=5; the trailing 10:35-10:40 is
    not a bookable slot).
    """
    if window.end_utc <= window.start_utc:
        raise InvalidWindowError("end_utc must be after start_utc")
    if window.slot_duration_minutes <= 0:
        raise InvalidWindowError("slot_duration_minutes must be positive")
    if window.buffer_minutes < 0:
        raise InvalidWindowError("buffer_minutes cannot be negative")

    slot_len = timedelta(minutes=window.slot_duration_minutes)
    buffer_len = timedelta(minutes=window.buffer_minutes)
    step = slot_len + buffer_len

    slots: list[Slot] = []
    cursor = window.start_utc
    while cursor + slot_len <= window.end_utc:
        slot = Slot(
            id=gen_id(),
            doctor_id=window.doctor_id,
            availability_window_id=window.id,
            start_utc=cursor,
            end_utc=cursor + slot_len,
            status=SlotStatus.AVAILABLE,
        )
        slots.append(slot)
        cursor += step

    db.add_all(slots)
    db.add(AuditLog(
        id=gen_id(),
        entity_type="availability_window",
        entity_id=window.id,
        action="generate_slots",
        actor="system",
        details=json.dumps({"slot_count": len(slots)}),
    ))
    db.commit()
    return slots


def expire_stale_holds(db: Session) -> int:
    """
    Reservation holds that were never confirmed in time get released back
    to AVAILABLE. Call this on a schedule (e.g. every 30s via a background
    worker/cron) and also opportunistically before listing/booking, so a
    dead client never permanently locks a slot.

    Returns the number of slots released.
    """
    now = datetime.utcnow()
    stale = (
        db.query(Slot)
        .filter(Slot.status == SlotStatus.HELD, Slot.held_until < now)
        .all()
    )
    for slot in stale:
        slot.status = SlotStatus.AVAILABLE
        slot.held_until = None
        slot.held_by_patient_id = None
        slot.version += 1
        db.add(AuditLog(
            id=gen_id(), entity_type="slot", entity_id=slot.id,
            action="hold_expired", actor="system", details=None,
        ))
    if stale:
        db.commit()
    return len(stale)
