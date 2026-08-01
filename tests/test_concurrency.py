"""
PROOF: double-booking is impossible under concurrent access.

This spins up N threads, each with its OWN database session/connection
(mirroring N separate API request handlers), and has all of them race to
hold_slot() the *same single slot* at the same time using a Barrier so
they fire as simultaneously as the GIL/OS scheduler allows.

Expected & asserted result: exactly one thread succeeds; every other
thread gets SlotUnavailableError. We repeat the race across many trials
to make a flaky race condition statistically very unlikely to hide.

Run with: pytest tests/test_concurrency.py -v
"""
import os
import tempfile
import threading
from datetime import datetime

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import Doctor, AvailabilityWindow, Slot, SlotStatus
from app.services import slot_service, booking_service

# Cross-platform temp path (tempfile.gettempdir() -> C:\Users\...\AppData\Local\Temp
# on Windows, /tmp on Linux/Mac) instead of a hardcoded Unix "/tmp/...".
DB_FILE = os.path.join(tempfile.gettempdir(), "clinzo_concurrency_test.db")


def fresh_engine():
    # File-backed SQLite (not :memory:) so every thread's own connection
    # sees the same database -- this is what makes it a genuine
    # multi-connection race rather than threads sharing one session.
    if os.path.exists(DB_FILE):
        os.remove(DB_FILE)
    engine = create_engine(
        f"sqlite:///{DB_FILE}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(engine)
    return engine


def make_slot(engine):
    Session = sessionmaker(bind=engine)
    db = Session()
    doctor = Doctor(name="Dr. Race", timezone="UTC")
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
    slot_id = slots[0].id
    db.close()
    return slot_id


def test_concurrent_holds_on_same_slot_only_one_wins():
    N_THREADS = 20
    N_TRIALS = 15

    total_successes = 0

    for trial in range(N_TRIALS):
        engine = fresh_engine()
        slot_id = make_slot(engine)
        Session = sessionmaker(bind=engine)

        barrier = threading.Barrier(N_THREADS)
        results = [None] * N_THREADS

        def worker(i):
            db = Session()
            try:
                barrier.wait()  # line everyone up, release simultaneously
                try:
                    booking = booking_service.hold_slot(db, slot_id, patient_id=f"patient-{i}")
                    results[i] = ("success", booking.id)
                except booking_service.SlotUnavailableError:
                    results[i] = ("failed", None)
                except Exception as e:  # noqa: BLE001 - surface unexpected errors as test failures
                    results[i] = ("error", str(e))
            finally:
                db.close()

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(N_THREADS)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        successes = [r for r in results if r and r[0] == "success"]
        errors = [r for r in results if r and r[0] == "error"]

        assert not errors, f"Trial {trial}: unexpected errors: {errors}"
        assert len(successes) == 1, (
            f"Trial {trial}: expected exactly 1 successful hold out of "
            f"{N_THREADS} concurrent attempts, got {len(successes)}: {results}"
        )
        total_successes += len(successes)

        # Final DB state must show exactly one HELD slot referencing exactly
        # one booking -- not two "successful" bookings that both think they won.
        check_db = Session()
        slot = check_db.get(Slot, slot_id)
        assert slot.status == SlotStatus.HELD
        check_db.close()
        engine.dispose()

    assert total_successes == N_TRIALS


def test_concurrent_confirm_attempts_only_one_wins():
    """
    Same race, one level up: N threads all try to confirm bookings for
    holds on the same slot (simulating N patients whose holds all somehow
    still point at a not-yet-BOOKED slot -- e.g. a bug elsewhere tried to
    create two holds). Only a hold whose slot is genuinely HELD-by-them
    can transition it to BOOKED.
    """
    engine = fresh_engine()
    slot_id = make_slot(engine)
    Session = sessionmaker(bind=engine)

    db = Session()
    booking = booking_service.hold_slot(db, slot_id, patient_id="patient-0")
    db.close()

    N_THREADS = 10
    barrier = threading.Barrier(N_THREADS)
    results = [None] * N_THREADS

    def worker(i):
        db = Session()
        try:
            barrier.wait()
            try:
                booking_service.confirm_booking(db, booking.id, patient_id="patient-0")
                results[i] = "success"
            except booking_service.InvalidBookingStateError:
                results[i] = "failed"
        finally:
            db.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert results.count("success") == 1
    check_db = Session()
    slot = check_db.get(Slot, slot_id)
    assert slot.status == SlotStatus.BOOKED
    check_db.close()
