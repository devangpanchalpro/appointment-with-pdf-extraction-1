"""
HMIS API Service — Fully dynamic, no static patient data.

NOTE: appointentDateTime must already be in UTC when passed to schedule_appointment().
      The IST → UTC conversion (−5 h 30 m) is performed in agent.py before calling here.
"""
import json
import logging
import uuid
from typing import List, Dict, Any, Optional
from datetime import datetime

from app.api.external_client import aarogya_api
from app.models.schemas import (
    AppointmentScheduleRequest,
    Patient,
    AppointmentDetail,
    BirthDateComponent,
    PermanentAddress,
    PatientDetail,
)

logger = logging.getLogger(__name__)


class HMISService:
    """All data is dynamic — collected from conversation."""

    @staticmethod
    def build_appointment_request(
        first_name: str,
        last_name: str,
        mobile: str,
        gender: int,
        birth_date: datetime,
        health_professional_id: str,
        facility_id: str,
        chief_complaints: List[str],
        appointment_date_time: datetime,   # ← must be UTC already
        middle_name: str = "",
        pin_code: str = "",
        address: str = "",
        area: str = "",
        external_id: Optional[str] = None,
    ) -> AppointmentScheduleRequest:
        """Build AppointmentScheduleRequest from patient and appointment data."""

        # Auto-generate a unique externalId if not provided
        generated_external_id = external_id or str(uuid.uuid4())

        bd_comp = BirthDateComponent(
            year  = birth_date.year,
            month = birth_date.month,
            day   = birth_date.day,
        )

        patient = Patient(
            firstName  = first_name,
            middleName = middle_name or "",
            lastName   = last_name,
            mobile     = mobile,
            gender     = gender,
            birthDate  = birth_date,
            birthDateComponent = bd_comp,
            healthId      = "",
            healthAddress = "",
            patientDetail = PatientDetail(
                permanentAddress=PermanentAddress(
                    pinCode = pin_code or "",
                    address = address or "",
                    area    = area or "",
                )
            ),
        )

        appointment_detail = AppointmentDetail(
            system               = 1,
            consultationType     = 1,
            slotDuration         = 0,
            externalId           = generated_external_id,
            healthProfessionalId = health_professional_id,
            facilityId           = facility_id,
            chiefComplaints      = chief_complaints,
            appointentDateTime   = appointment_date_time,  # UTC
        )

        request = AppointmentScheduleRequest(
            patient           = patient,
            appointmentDetail = appointment_detail,
        )

        # ── Print the full request body in terminal ──────────────────────────
        payload = request.model_dump(mode="json")
        print("\n" + "=" * 70)
        print("  APPOINTMENT SCHEDULE REQUEST BODY")
        print("=" * 70)
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        print("=" * 70 + "\n")
        logger.info(f"[HMIS] Request body:\n{json.dumps(payload, indent=2)}")

        return request

    async def get_doctors_availability(
        self,
        facility_id: str,
        from_date: Optional[str] = None,
        to_date: Optional[str] = None,
        health_professional_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """Delegate to the API client."""
        return await aarogya_api.get_doctors_availability(
            facility_id            = facility_id,
            from_date              = from_date,
            to_date                = to_date,
            health_professional_id = health_professional_id,
        )

    async def schedule_appointment(
        self,
        first_name: str,
        last_name: str,
        mobile: str,
        gender: int,
        birth_date: datetime,
        health_professional_id: str,
        facility_id: str,
        chief_complaints: List[str],
        appointment_date_time: datetime,   # ← UTC, converted by caller
        middle_name: str = "",
        pin_code: str = "",
        address: str = "",
        area: str = "",
        external_id: Optional[str] = None,
    ) -> Dict[str, Any]:

        request = self.build_appointment_request(
            first_name             = first_name,
            last_name              = last_name,
            mobile                 = mobile,
            gender                 = gender,
            birth_date             = birth_date,
            health_professional_id = health_professional_id,
            facility_id            = facility_id,
            chief_complaints       = chief_complaints,
            appointment_date_time  = appointment_date_time,
            middle_name            = middle_name,
            pin_code               = pin_code,
            address                = address,
            area                   = area,
            external_id            = external_id,
        )

        logger.info(
            f"[HMIS] Scheduling appointment for {first_name} {last_name} "
            f"with healthProfessionalId={health_professional_id} "
            f"at {appointment_date_time.isoformat()} UTC"
        )
        return await aarogya_api.schedule_appointment(request)


hmis_service = HMISService()