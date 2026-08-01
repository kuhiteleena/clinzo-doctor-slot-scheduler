from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Doctor, AvailabilityWindow, SlotStatus
from app.services import slot_service


@pytest.fixture
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()


def make_doctor(db):
    doctor = Doctor(name="Dr. Test", timezone="UTC")
    db.add(doctor)
    db.commit()
    db.refresh(doctor)
    return doctor


def test_basic_slot_generation_no_buffer(db):
    doctor = make_doctor(db)
    window = AvailabilityWindow(
        doctor_id=doctor.id,
        start_utc=datetime(2026, 8, 3, 10, 0),
        end_utc=datetime(2026, 8, 3, 11, 0),
        slot_duration_minutes=15,
        buffer_minutes=0,
    )
    db.add(window)
    db.commit()
    db.refresh(window)

    slots = slot_service.generate_slots_for_window(db, window)

    assert len(slots) == 4
    assert slots[0].start_utc == datetime(2026, 8, 3, 10, 0)
    assert slots[0].end_utc == datetime(2026, 8, 3, 10, 15)
    assert slots[-1].start_utc == datetime(2026, 8, 3, 10, 45)
    assert slots[-1].end_utc == datetime(2026, 8, 3, 11, 0)
    assert all(s.status == SlotStatus.AVAILABLE for s in slots)
    # No overlaps, no gaps (buffer=0): each slot's end == next slot's start
    for a, b in zip(slots, slots[1:]):
        assert a.end_utc == b.start_utc


def test_slot_generation_with_buffer(db):
    doctor = make_doctor(db)
    window = AvailabilityWindow(
        doctor_id=doctor.id,
        start_utc=datetime(2026, 8, 3, 10, 0),
        end_utc=datetime(2026, 8, 3, 11, 0),
        slot_duration_minutes=15,
        buffer_minutes=5,
    )
    db.add(window)
    db.commit()
    db.refresh(window)

    slots = slot_service.generate_slots_for_window(db, window)

    # step = 20 min (15 slot + 5 buffer); window is 60 min -> 3 slots fit
    # (10:00-10:15, 10:20-10:35, 10:40-10:55); a 4th would need to start at
    # 11:00 which has no room for a full slot before end_utc.
    assert len(slots) == 3
    assert slots[0].start_utc == datetime(2026, 8, 3, 10, 0)
    assert slots[1].start_utc == datetime(2026, 8, 3, 10, 20)
    assert slots[2].start_utc == datetime(2026, 8, 3, 10, 40)
    assert slots[2].end_utc == datetime(2026, 8, 3, 10, 55)
    # Verify the buffer gap explicitly
    assert slots[1].start_utc - slots[0].end_utc == timedelta(minutes=5)


def test_partial_trailing_slot_is_dropped(db):
    doctor = make_doctor(db)
    # 40-minute window, 15-min slots, no buffer -> 2 full slots, 10 min dropped
    window = AvailabilityWindow(
        doctor_id=doctor.id,
        start_utc=datetime(2026, 8, 3, 9, 0),
        end_utc=datetime(2026, 8, 3, 9, 40),
        slot_duration_minutes=15,
        buffer_minutes=0,
    )
    db.add(window)
    db.commit()
    db.refresh(window)

    slots = slot_service.generate_slots_for_window(db, window)
    assert len(slots) == 2
    assert slots[-1].end_utc == datetime(2026, 8, 3, 9, 30)


def test_configurable_duration_not_hardcoded(db):
    doctor = make_doctor(db)
    window = AvailabilityWindow(
        doctor_id=doctor.id,
        start_utc=datetime(2026, 8, 3, 9, 0),
        end_utc=datetime(2026, 8, 3, 10, 0),
        slot_duration_minutes=30,  # different duration
        buffer_minutes=0,
    )
    db.add(window)
    db.commit()
    db.refresh(window)

    slots = slot_service.generate_slots_for_window(db, window)
    assert len(slots) == 2
    assert (slots[0].end_utc - slots[0].start_utc) == timedelta(minutes=30)


def test_invalid_window_rejected(db):
    doctor = make_doctor(db)
    window = AvailabilityWindow(
        doctor_id=doctor.id,
        start_utc=datetime(2026, 8, 3, 10, 0),
        end_utc=datetime(2026, 8, 3, 9, 0),  # end before start
        slot_duration_minutes=15,
        buffer_minutes=0,
    )
    db.add(window)
    db.commit()
    db.refresh(window)

    with pytest.raises(slot_service.InvalidWindowError):
        slot_service.generate_slots_for_window(db, window)
