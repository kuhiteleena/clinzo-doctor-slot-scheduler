from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class DoctorCreate(BaseModel):
    name: str
    timezone: str = "UTC"


class DoctorOut(BaseModel):
    id: str
    name: str
    timezone: str
    model_config = {"from_attributes": True}


class AvailabilityWindowCreate(BaseModel):
    start_utc: datetime = Field(..., description="ISO8601 UTC start, e.g. 2026-08-04T10:00:00")
    end_utc: datetime = Field(..., description="ISO8601 UTC end, e.g. 2026-08-04T18:00:00")
    slot_duration_minutes: int = 15
    buffer_minutes: int = 0


class SlotOut(BaseModel):
    id: str
    doctor_id: str
    start_utc: datetime
    end_utc: datetime
    status: str
    model_config = {"from_attributes": True}


class HoldRequest(BaseModel):
    patient_id: str


class ConfirmRequest(BaseModel):
    patient_id: str


class CancelRequest(BaseModel):
    actor: str


class RescheduleRequest(BaseModel):
    patient_id: str
    new_slot_id: str


class BookingOut(BaseModel):
    id: str
    slot_id: str
    patient_id: str
    status: str
    hold_expires_at: Optional[datetime] = None
    created_at: datetime
    confirmed_at: Optional[datetime] = None
    cancelled_at: Optional[datetime] = None
    rescheduled_from_booking_id: Optional[str] = None
    model_config = {"from_attributes": True}
