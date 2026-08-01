from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db, init_db
from app.models import Doctor, AvailabilityWindow, Slot, Booking, SlotStatus
from app.schemas import (
    DoctorCreate, DoctorOut, AvailabilityWindowCreate, SlotOut,
    HoldRequest, ConfirmRequest, CancelRequest, RescheduleRequest, BookingOut,
)
from app.services import slot_service, booking_service, availability_service

app = FastAPI(title="Clinzo Doctor Slot Scheduling API")


@app.on_event("startup")
def on_startup():
    init_db()


# ---------------------------------------------------------------- Doctors --

@app.post("/doctors", response_model=DoctorOut)
def create_doctor(payload: DoctorCreate, db: Session = Depends(get_db)):
    doctor = Doctor(name=payload.name, timezone=payload.timezone)
    db.add(doctor)
    db.commit()
    db.refresh(doctor)
    return doctor


# ------------------------------------------------------------ Availability --

@app.post("/doctors/{doctor_id}/availability", response_model=list[SlotOut])
def create_availability_window(doctor_id: str, payload: AvailabilityWindowCreate,
                                db: Session = Depends(get_db)):
    doctor = db.get(Doctor, doctor_id)
    if doctor is None:
        raise HTTPException(404, "Doctor not found")

    window = AvailabilityWindow(
        doctor_id=doctor_id,
        start_utc=payload.start_utc,
        end_utc=payload.end_utc,
        slot_duration_minutes=payload.slot_duration_minutes,
        buffer_minutes=payload.buffer_minutes,
    )
    db.add(window)
    db.commit()
    db.refresh(window)

    try:
        slots = slot_service.generate_slots_for_window(db, window)
    except slot_service.InvalidWindowError as e:
        raise HTTPException(400, str(e))
    return slots


@app.delete("/availability/{window_id}")
def remove_availability_window(window_id: str, actor: str = Query(...), db: Session = Depends(get_db)):
    try:
        result = availability_service.deactivate_window(db, window_id, actor)
    except ValueError as e:
        raise HTTPException(404, str(e))
    return result


# ------------------------------------------------------------------ Slots --

@app.get("/doctors/{doctor_id}/slots", response_model=list[SlotOut])
def list_available_slots(
    doctor_id: str,
    on_date: date | None = Query(None, description="Filter to a single calendar date"),
    display_tz: str = Query("UTC", description="IANA tz name to interpret `on_date` in"),
    db: Session = Depends(get_db),
):
    slot_service.expire_stale_holds(db)  # self-heal any dead holds before listing

    q = db.query(Slot).filter(Slot.doctor_id == doctor_id, Slot.status == SlotStatus.AVAILABLE)

    if on_date is not None:
        tz = ZoneInfo(display_tz)
        day_start_local = datetime.combine(on_date, datetime.min.time(), tzinfo=tz)
        day_end_local = day_start_local + timedelta(days=1)
        day_start_utc = day_start_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
        day_end_utc = day_end_local.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
        q = q.filter(Slot.start_utc >= day_start_utc, Slot.start_utc < day_end_utc)

    return q.order_by(Slot.start_utc).all()


# --------------------------------------------------------------- Bookings --

@app.post("/slots/{slot_id}/hold", response_model=BookingOut)
def hold_slot(slot_id: str, payload: HoldRequest, db: Session = Depends(get_db)):
    slot_service.expire_stale_holds(db)
    try:
        return booking_service.hold_slot(db, slot_id, payload.patient_id)
    except booking_service.SlotUnavailableError as e:
        raise HTTPException(409, str(e))


@app.post("/bookings/{booking_id}/confirm", response_model=BookingOut)
def confirm_booking(booking_id: str, payload: ConfirmRequest, db: Session = Depends(get_db)):
    try:
        return booking_service.confirm_booking(db, booking_id, payload.patient_id)
    except booking_service.InvalidBookingStateError as e:
        raise HTTPException(409, str(e))


@app.post("/bookings/{booking_id}/cancel", response_model=BookingOut)
def cancel_booking(booking_id: str, payload: CancelRequest, db: Session = Depends(get_db)):
    try:
        return booking_service.cancel_booking(db, booking_id, payload.actor)
    except booking_service.InvalidBookingStateError as e:
        raise HTTPException(409, str(e))


@app.post("/bookings/{booking_id}/reschedule", response_model=BookingOut)
def reschedule_booking(booking_id: str, payload: RescheduleRequest, db: Session = Depends(get_db)):
    slot_service.expire_stale_holds(db)
    try:
        return booking_service.reschedule_booking(db, booking_id, payload.new_slot_id, payload.patient_id)
    except (booking_service.SlotUnavailableError, booking_service.InvalidBookingStateError) as e:
        raise HTTPException(409, str(e))
