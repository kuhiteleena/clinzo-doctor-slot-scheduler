"""
Requirement #5: "Handle a doctor changing or removing their availability
window after some slots in it have already been booked."

Policy (stated explicitly, since this is a business decision as much as
a technical one -- see README):

  - AVAILABLE slots under the window: removed immediately (status ->
    REMOVED). Nobody has a claim on them.
  - HELD slots (someone mid-checkout): removed immediately too. A hold
    is not a confirmed appointment; the in-flight patient will simply
    see the slot vanish and the hold's confirm_booking() call will fail
    cleanly with InvalidBookingStateError.
  - BOOKED slots: NEVER auto-removed. A confirmed appointment is a
    commitment to a patient; the system does not unilaterally cancel it.
    Instead we flag the window inactive, leave booked slots + bookings
    exactly as they are, and write an audit entry so downstream
    processes (e.g. "notify patients + doctor to manually resolve
    conflicts") have something to act on. This is surfaced back to the
    caller as `still_booked_slot_ids` so the API layer can warn the
    doctor synchronously.
"""
import json
from datetime import datetime

from sqlalchemy.orm import Session

from app.models import AvailabilityWindow, Slot, SlotStatus, AuditLog, gen_id


def deactivate_window(db: Session, window_id: str, actor: str) -> dict:
    window = db.get(AvailabilityWindow, window_id)
    if window is None:
        raise ValueError("Availability window not found")

    slots = db.query(Slot).filter(Slot.availability_window_id == window_id).all()

    removed_ids, kept_booked_ids = [], []
    for slot in slots:
        if slot.status in (SlotStatus.AVAILABLE, SlotStatus.HELD):
            slot.status = SlotStatus.REMOVED
            slot.held_until = None
            slot.held_by_patient_id = None
            slot.version += 1
            removed_ids.append(slot.id)
        elif slot.status == SlotStatus.BOOKED:
            kept_booked_ids.append(slot.id)
        # REMOVED slots: already gone, leave as-is.

    window.is_active = False

    db.add(AuditLog(
        id=gen_id(), entity_type="availability_window", entity_id=window_id,
        action="deactivated", actor=actor,
        details=json.dumps({
            "removed_slot_count": len(removed_ids),
            "still_booked_slot_ids": kept_booked_ids,
        }),
    ))
    db.commit()

    return {
        "window_id": window_id,
        "removed_slot_ids": removed_ids,
        "still_booked_slot_ids": kept_booked_ids,  # requires manual/doctor follow-up
    }
