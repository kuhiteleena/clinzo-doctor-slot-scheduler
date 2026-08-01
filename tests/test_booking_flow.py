from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Doctor, AvailabilityWindow, Slot, SlotStatus, BookingStatus
from app.services import slot_service, booking_service, availability_service


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


@pytest.fixture
def one_slot(db):
    doctor = Doctor(name="Dr. Test", timezone="UTC")
    db.add(doctor)
    db.commit()
    db.refresh(doctor)

    window = AvailabilityWindow(
        doctor_id=doctor.id,
        start_utc=datetime(2026, 8, 3, 10, 0),
        end_utc=datetime(2026, 8, 3, 10, 15),
        slot_duration_minutes=15,
    )
    db.add(window)
    db.commit()
    db.refresh(window)
    slots = slot_service.generate_slots_for_window(db, window)
    return slots[0]


def test_hold_then_confirm_books_the_slot(db, one_slot):
    booking = booking_service.hold_slot(db, one_slot.id, patient_id="patient-1")
    assert booking.status == BookingStatus.HELD

    slot = db.get(Slot, one_slot.id)
    assert slot.status == SlotStatus.HELD

    confirmed = booking_service.confirm_booking(db, booking.id, patient_id="patient-1")
    assert confirmed.status == BookingStatus.CONFIRMED

    slot = db.get(Slot, one_slot.id)
    assert slot.status == SlotStatus.BOOKED


def test_booked_slot_disappears_from_available_listing(db, one_slot):
    booking_service.hold_slot(db, one_slot.id, "patient-1")
    available = db.query(Slot).filter(Slot.status == SlotStatus.AVAILABLE).all()
    assert one_slot.id not in [s.id for s in available]


def test_second_hold_on_already_held_slot_fails(db, one_slot):
    booking_service.hold_slot(db, one_slot.id, "patient-1")
    with pytest.raises(booking_service.SlotUnavailableError):
        booking_service.hold_slot(db, one_slot.id, "patient-2")


def test_confirm_by_wrong_patient_rejected(db, one_slot):
    booking = booking_service.hold_slot(db, one_slot.id, "patient-1")
    with pytest.raises(booking_service.InvalidBookingStateError):
        booking_service.confirm_booking(db, booking.id, patient_id="patient-2")


def test_cancel_releases_slot_immediately(db, one_slot):
    booking = booking_service.hold_slot(db, one_slot.id, "patient-1")
    booking_service.confirm_booking(db, booking.id, "patient-1")

    booking_service.cancel_booking(db, booking.id, actor="patient-1")

    slot = db.get(Slot, one_slot.id)
    assert slot.status == SlotStatus.AVAILABLE

    # And it can immediately be booked by someone else
    new_booking = booking_service.hold_slot(db, one_slot.id, "patient-2")
    assert new_booking.status == BookingStatus.HELD


def test_expired_hold_is_released_and_rebookable(db, one_slot):
    booking = booking_service.hold_slot(db, one_slot.id, "patient-1", hold_ttl_seconds=-1)
    # hold_ttl_seconds=-1 => already expired at creation time
    released = slot_service.expire_stale_holds(db)
    assert released == 1

    slot = db.get(Slot, one_slot.id)
    assert slot.status == SlotStatus.AVAILABLE

    new_booking = booking_service.hold_slot(db, one_slot.id, "patient-2")
    assert new_booking.status == BookingStatus.HELD


def test_confirm_after_hold_expired_raises(db, one_slot):
    booking = booking_service.hold_slot(db, one_slot.id, "patient-1", hold_ttl_seconds=-1)
    with pytest.raises(booking_service.InvalidBookingStateError):
        booking_service.confirm_booking(db, booking.id, "patient-1")


def test_reschedule_moves_booking_and_frees_old_slot(db):
    doctor = Doctor(name="Dr. Test", timezone="UTC")
    db.add(doctor)
    db.commit()
    db.refresh(doctor)

    window = AvailabilityWindow(
        doctor_id=doctor.id, start_utc=datetime(2026, 8, 3, 10, 0),
        end_utc=datetime(2026, 8, 3, 10, 30), slot_duration_minutes=15,
    )
    db.add(window)
    db.commit()
    db.refresh(window)
    slots = slot_service.generate_slots_for_window(db, window)
    slot_a, slot_b = slots[0], slots[1]

    booking = booking_service.hold_slot(db, slot_a.id, "patient-1")
    booking_service.confirm_booking(db, booking.id, "patient-1")

    new_booking = booking_service.reschedule_booking(db, booking.id, slot_b.id, "patient-1")
    assert new_booking.status == BookingStatus.HELD
    assert new_booking.rescheduled_from_booking_id == booking.id

    old = db.get(Slot, slot_a.id)
    new = db.get(Slot, slot_b.id)
    assert old.status == SlotStatus.AVAILABLE
    assert new.status == SlotStatus.HELD


def test_reschedule_to_unavailable_slot_preserves_original_booking(db):
    doctor = Doctor(name="Dr. Test", timezone="UTC")
    db.add(doctor)
    db.commit()
    db.refresh(doctor)

    window = AvailabilityWindow(
        doctor_id=doctor.id, start_utc=datetime(2026, 8, 3, 10, 0),
        end_utc=datetime(2026, 8, 3, 10, 30), slot_duration_minutes=15,
    )
    db.add(window)
    db.commit()
    db.refresh(window)
    slots = slot_service.generate_slots_for_window(db, window)
    slot_a, slot_b = slots[0], slots[1]

    booking = booking_service.hold_slot(db, slot_a.id, "patient-1")
    booking_service.confirm_booking(db, booking.id, "patient-1")

    # Someone else takes slot_b first
    booking_service.hold_slot(db, slot_b.id, "patient-2")

    with pytest.raises(booking_service.SlotUnavailableError):
        booking_service.reschedule_booking(db, booking.id, slot_b.id, "patient-1")

    # Original booking must be untouched
    from app.models import Booking
    original = db.get(Booking, booking.id)
    assert original.status == BookingStatus.CONFIRMED
    slot_a_after = db.get(Slot, slot_a.id)
    assert slot_a_after.status == SlotStatus.BOOKED


def test_retroactive_window_removal_keeps_booked_slots(db):
    doctor = Doctor(name="Dr. Test", timezone="UTC")
    db.add(doctor)
    db.commit()
    db.refresh(doctor)

    window = AvailabilityWindow(
        doctor_id=doctor.id, start_utc=datetime(2026, 8, 3, 10, 0),
        end_utc=datetime(2026, 8, 3, 10, 30), slot_duration_minutes=15,
    )
    db.add(window)
    db.commit()
    db.refresh(window)
    slots = slot_service.generate_slots_for_window(db, window)
    slot_a, slot_b = slots[0], slots[1]

    booking = booking_service.hold_slot(db, slot_a.id, "patient-1")
    booking_service.confirm_booking(db, booking.id, "patient-1")
    # slot_b stays AVAILABLE

    result = availability_service.deactivate_window(db, window.id, actor="dr-1")

    assert slot_b.id in result["removed_slot_ids"]
    assert slot_a.id in result["still_booked_slot_ids"]

    assert db.get(Slot, slot_a.id).status == SlotStatus.BOOKED  # untouched
    assert db.get(Slot, slot_b.id).status == SlotStatus.REMOVED
