import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    Column, String, Integer, Float, DateTime, Boolean, ForeignKey, Enum, Text
)
from sqlalchemy.orm import relationship

from app.database import Base


def gen_id() -> str:
    return str(uuid.uuid4())


class SlotStatus(str, enum.Enum):
    AVAILABLE = "AVAILABLE"      # bookable
    HELD = "HELD"                # short-lived reservation hold, not yet confirmed
    BOOKED = "BOOKED"            # confirmed booking
    REMOVED = "REMOVED"          # doctor withdrew this slot's availability (never booked)


class BookingStatus(str, enum.Enum):
    HELD = "HELD"                # hold created, awaiting confirmation
    CONFIRMED = "CONFIRMED"      # confirmed booking
    CANCELLED = "CANCELLED"
    RESCHEDULED = "RESCHEDULED"  # superseded by a new booking (kept for audit trail)
    EXPIRED = "EXPIRED"          # hold expired without confirmation


class Doctor(Base):
    __tablename__ = "doctors"

    id = Column(String, primary_key=True, default=gen_id)
    name = Column(String, nullable=False)
    # IANA tz name, e.g. "Asia/Kolkata". All *stored* timestamps are UTC;
    # this is only used to convert for display / recurring-window generation.
    timezone = Column(String, nullable=False, default="UTC")

    availability_windows = relationship("AvailabilityWindow", back_populates="doctor")
    slots = relationship("Slot", back_populates="doctor")


class AvailabilityWindow(Base):
    """
    A doctor's raw availability, e.g. "Monday 10:00-18:00 IST, 15 min slots".
    Slots are materialized (pre-generated rows) from this window -- see
    slot_service.py and the README for the materialized-vs-computed tradeoff.
    """
    __tablename__ = "availability_windows"

    id = Column(String, primary_key=True, default=gen_id)
    doctor_id = Column(String, ForeignKey("doctors.id"), nullable=False)

    start_utc = Column(DateTime, nullable=False)
    end_utc = Column(DateTime, nullable=False)

    slot_duration_minutes = Column(Integer, nullable=False, default=15)
    buffer_minutes = Column(Integer, nullable=False, default=0)

    # Soft-delete / retroactive-change support. When a doctor edits or
    # removes a window, we don't hard-delete it (audit trail); we mark it
    # inactive and update the slots that depended on it.
    is_active = Column(Boolean, nullable=False, default=True)

    created_at = Column(DateTime, default=datetime.utcnow)

    doctor = relationship("Doctor", back_populates="availability_windows")
    slots = relationship("Slot", back_populates="availability_window")


class Slot(Base):
    """
    A single, discrete, bookable unit of time. This is the row every
    booking-race is fought over, so it carries an optimistic-lock
    `version` column in addition to the atomic conditional-update pattern
    used in booking_service.py (belt-and-suspenders).
    """
    __tablename__ = "slots"

    id = Column(String, primary_key=True, default=gen_id)
    doctor_id = Column(String, ForeignKey("doctors.id"), nullable=False)
    availability_window_id = Column(String, ForeignKey("availability_windows.id"), nullable=False)

    start_utc = Column(DateTime, nullable=False, index=True)
    end_utc = Column(DateTime, nullable=False)

    status = Column(Enum(SlotStatus), nullable=False, default=SlotStatus.AVAILABLE, index=True)

    # Reservation-hold expiry. Only meaningful when status == HELD.
    held_until = Column(DateTime, nullable=True)
    held_by_patient_id = Column(String, nullable=True)

    version = Column(Integer, nullable=False, default=0)

    doctor = relationship("Doctor", back_populates="slots")
    availability_window = relationship("AvailabilityWindow", back_populates="slots")
    bookings = relationship("Booking", back_populates="slot")


class Booking(Base):
    __tablename__ = "bookings"

    id = Column(String, primary_key=True, default=gen_id)
    slot_id = Column(String, ForeignKey("slots.id"), nullable=False, index=True)
    patient_id = Column(String, nullable=False, index=True)

    status = Column(Enum(BookingStatus), nullable=False, default=BookingStatus.HELD)

    # If this booking was created by rescheduling another one.
    rescheduled_from_booking_id = Column(String, ForeignKey("bookings.id"), nullable=True)

    hold_expires_at = Column(DateTime, nullable=True)

    created_at = Column(DateTime, default=datetime.utcnow)
    confirmed_at = Column(DateTime, nullable=True)
    cancelled_at = Column(DateTime, nullable=True)

    slot = relationship("Slot", back_populates="bookings")


class AuditLog(Base):
    """
    Append-only trail of every state transition. Required by the
    "auditability" NFR -- lets us answer "who booked/cancelled/rescheduled
    what, and when" without reconstructing it from mutable rows.
    """
    __tablename__ = "audit_log"

    id = Column(String, primary_key=True, default=gen_id)
    entity_type = Column(String, nullable=False)   # "slot" | "booking" | "availability_window"
    entity_id = Column(String, nullable=False, index=True)
    action = Column(String, nullable=False)        # "hold" | "confirm" | "cancel" | "reschedule" | ...
    actor = Column(String, nullable=True)          # patient_id / doctor_id / "system"
    details = Column(Text, nullable=True)           # free-form JSON string
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
