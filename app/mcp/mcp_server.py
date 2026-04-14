"""
MCP Server for Aarogya HMIS — 4-Step Sequential Booking Flow

Tools (call in order):
  1. get_doctors_list          → GET /doctors?FacilityId=...
  2. get_doctor_facilities     → GET /doctors/{id}/facilities
  3. get_doctor_availability   → GET /doctors/availability?...
  4. book_appointment          → POST /appointment/schedule
"""
import json
import logging
from datetime import datetime, timedelta
from typing import List, Optional, Any

from mcp.server.fastmcp import FastMCP

from app.api.external_client import aarogya_api
from app.api.hmis_service import HMISService
from app.config.settings import settings

logger = logging.getLogger(__name__)


mcp = FastMCP("AarogyaMCP", "1.0.0")


# ═══════════════════════════════════════════════════════════════════════════════
# Tool 1: get_doctors_list
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def get_doctors_list(
    facility_id: Optional[str] = None,
    page_size: int = 20,
    skip_count: int = 0,
) -> str:
    """
    Fetch the full list of available doctors from the backend.

    Call this FIRST when the user expresses any booking intent.
    Returns doctor names, specialties, and healthProfessionalIDs.

    Args:
        facility_id: Optional facility ID to filter doctors (defaults to DEFAULT_FACILITY_ID)
        page_size: Number of doctors per page (default 20)
        skip_count: Number of doctors to skip for pagination (default 0)

    Returns:
        JSON with doctors array containing name, healthProfessionalId, facility info
    """
    fac_id = facility_id or settings.DEFAULT_FACILITY_ID
    logger.info(f"[MCP] get_doctors_list | facility={fac_id}")

    try:
        doctors = await aarogya_api.get_doctors_list(
            facility_id=fac_id,
            page_size=page_size,
            skip_count=skip_count,
        )

        if not doctors:
            return json.dumps({
                "success": False,
                "doctors": [],
                "message": "No doctors available currently.",
            })

        # Format clean output — no raw IDs exposed to user
        formatted = []
        for i, doc in enumerate(doctors, 1):
            name_obj = doc.get("name", {})
            full_name = f"{name_obj.get('firstName', '')} {name_obj.get('lastName', '')}".strip()
            if not full_name or full_name.lower() in ("unknown", "n/a"):
                continue

            formatted.append({
                "index": i,
                "healthProfessionalId": doc.get("healthProfessionalId", ""),
                "name": full_name,
                "facilityId": doc.get("facilityId", ""),
                "facilityName": doc.get("facility", ""),
                "gender": doc.get("gender"),
                "charges": doc.get("professionalDetail", {}).get("charges", {}),
            })

        return json.dumps({
            "success": True,
            "count": len(formatted),
            "doctors": formatted,
        })

    except Exception as e:
        logger.error(f"[MCP] get_doctors_list error: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e), "doctors": []})


# ═══════════════════════════════════════════════════════════════════════════════
# Tool 2: get_doctor_facilities
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def get_doctor_facilities(
    health_professional_id: str,
) -> str:
    """
    Fetch all facilities (hospitals/clinics) where a specific doctor is available.

    Call this AFTER the user selects a doctor from the get_doctors_list results.

    Args:
        health_professional_id: The doctor's unique ID from get_doctors_list response

    Returns:
        JSON with facilities array containing facilityId, name, address, appointment slots
    """
    logger.info(f"[MCP] get_doctor_facilities | doctor={health_professional_id}")

    try:
        facilities = await aarogya_api.get_doctor_facilities(
            health_professional_id=health_professional_id,
        )

        if not facilities:
            return json.dumps({
                "success": False,
                "facilities": [],
                "message": "No facilities found for this doctor.",
            })

        formatted = []
        for i, fac_entry in enumerate(facilities, 1):
            fac = fac_entry.get("facility", {})
            location = fac.get("location", {})

            formatted.append({
                "index": i,
                "facilityId": fac.get("facilityId", ""),
                "name": fac.get("name", ""),
                "address": location.get("address", ""),
                "pincode": location.get("pincode", ""),
                "state": location.get("state", {}).get("name", ""),
                "district": location.get("district", {}).get("name", ""),
                "charges": fac.get("charges", {}),
                "appointmentSlots": fac_entry.get("appointmentSlots", []),
            })

        return json.dumps({
            "success": True,
            "count": len(formatted),
            "facilities": formatted,
        })

    except Exception as e:
        logger.error(f"[MCP] get_doctor_facilities error: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e), "facilities": []})


# ═══════════════════════════════════════════════════════════════════════════════
# Tool 3: get_doctor_availability
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def get_doctor_availability(
    health_professional_id: str,
    facility_id: str,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
) -> str:
    """
    Fetch available appointment date and time slots for a specific doctor
    at a specific facility.

    Call this AFTER facilityID is confirmed (user selected or auto-selected).
    Both healthProfessionalID and facilityID must be provided.

    Args:
        health_professional_id: Doctor's unique ID (from get_doctors_list)
        facility_id: Facility's unique ID (from get_doctor_facilities)
        from_date: Start date for availability search (YYYY-MM-DD, defaults to today)
        to_date: End date for availability search (YYYY-MM-DD, defaults to today+3 days)

    Returns:
        JSON with available slots grouped by date, including startTime, endTime, session info
    """
    if not from_date:
        from_date = datetime.now().strftime("%Y-%m-%d")
    if not to_date:
        to_date = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")

    logger.info(
        f"[MCP] get_doctor_availability | doctor={health_professional_id} "
        f"facility={facility_id} from={from_date} to={to_date}"
    )

    try:
        availability = await aarogya_api.get_doctors_availability(
            facility_id=facility_id,
            from_date=from_date,
            to_date=to_date,
            health_professional_id=health_professional_id,
        )

        if not availability:
            return json.dumps({
                "success": False,
                "slots": [],
                "message": "No available slots found for this doctor at this facility.",
            })

        # Format slots grouped by date
        all_dates = []
        for doc_entry in availability:
            date_str = doc_entry.get("appointmentDate", "")
            doctor_name = doc_entry.get("healthProfessionalName", "")
            department = doc_entry.get("department", "")

            date_slots = []
            for sched in doc_entry.get("schedule", []):
                session_name = sched.get("session", "")
                for slot in sched.get("slots", []):
                    if _slot_is_available(slot.get("isAvailable")):
                        date_slots.append({
                            "startTime": slot.get("from", ""),
                            "endTime": slot.get("to", ""),
                            "session": session_name,
                            "consultationTypeId": slot.get("consultationTypeId"),
                            "booked": slot.get("booked", 0),
                        })

            if date_slots:
                all_dates.append({
                    "date": date_str,
                    "doctorName": doctor_name,
                    "department": department,
                    "slots": date_slots,
                })

        return json.dumps({
            "success": True,
            "healthProfessionalId": health_professional_id,
            "facilityId": facility_id,
            "availability": all_dates,
            "totalSlots": sum(len(d["slots"]) for d in all_dates),
        })

    except Exception as e:
        logger.error(f"[MCP] get_doctor_availability error: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e), "slots": []})


# ═══════════════════════════════════════════════════════════════════════════════
# Tool 4: book_appointment
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def book_appointment(
    first_name: str,
    last_name: str,
    mobile: str,
    gender: int,
    birth_date: str,
    health_professional_id: str,
    facility_id: str,
    slot_date: str,
    slot_start_time: str,
    symptoms: Optional[List[str]] = None,
    middle_name: str = "",
    pin_code: str = "",
    address: str = "",
    area: str = "",
) -> str:
    """
    Book the selected appointment slot by posting all confirmed details to the backend.

    Call this ONLY after the user confirms their preferred date and time slot.
    All required fields must be provided.

    Args:
        first_name: Patient's first name
        last_name: Patient's last name
        mobile: Patient's 10-digit mobile number
        gender: Patient gender (1=Male, 2=Female)
        birth_date: Patient date of birth (YYYY-MM-DD)
        health_professional_id: Doctor ID (from get_doctors_list)
        facility_id: Facility ID (from get_doctor_facilities)
        slot_date: Appointment date (YYYY-MM-DD from get_doctor_availability)
        slot_start_time: Appointment start time (HH:MM from get_doctor_availability)
        symptoms: List of chief complaints/symptoms
        middle_name: Patient's middle name (optional)
        pin_code: Patient's PIN code (optional)
        address: Patient's address (optional)
        area: Patient's area (optional)

    Returns:
        JSON with booking confirmation including appointmentID, doctor, facility, date, time
    """
    logger.info(f"[MCP] book_appointment | patient={first_name} {last_name}")

    try:
        # Parse birth date
        bdate = datetime.strptime(birth_date, "%Y-%m-%d")

        # Build appointment datetime from slot_date + slot_start_time
        appt_dt_ist = datetime.strptime(f"{slot_date} {slot_start_time}", "%Y-%m-%d %H:%M")

        # Convert IST → UTC (subtract 5h30m)
        IST_OFFSET = timedelta(hours=5, minutes=30)
        appt_dt_utc = appt_dt_ist - IST_OFFSET

        logger.info(f"[MCP] IST: {appt_dt_ist.isoformat()} → UTC: {appt_dt_utc.isoformat()}")

        chief_complaints = symptoms if symptoms else ["General consultation"]

        # Build request via HMISService
        request = HMISService.build_appointment_request(
            first_name=first_name,
            middle_name=middle_name,
            last_name=last_name,
            mobile=mobile,
            gender=gender,
            birth_date=bdate,
            health_professional_id=health_professional_id,
            facility_id=facility_id,
            chief_complaints=chief_complaints,
            appointment_date_time=appt_dt_utc,
            pin_code=pin_code,
            address=address,
            area=area,
            external_id="",
        )

        result = await aarogya_api.schedule_appointment(request)

        if result.get("success"):
            return json.dumps({
                "success": True,
                "message": "Appointment booked successfully!",
                "data": result.get("data", {}),
                "summary": {
                    "patient": f"{first_name} {last_name}",
                    "doctor": health_professional_id,
                    "facility": facility_id,
                    "date": slot_date,
                    "time": slot_start_time,
                },
            })
        else:
            return json.dumps({
                "success": False,
                "error": result.get("error", "Unknown booking error"),
            })

    except Exception as e:
        logger.error(f"[MCP] book_appointment error: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e)})


# ═══════════════════════════════════════════════════════════════════════════════
# Tool 5: get_doctors_by_symptoms (AI-powered symptom → doctor matching)
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def get_doctors_by_symptoms(
    specializations: List[str],
    facility_id: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
) -> str:
    """
    Fetch doctors filtered by medical specializations that match the user's symptoms.

    This is a composite tool that:
      1. Fetches all doctor availability for the facility
      2. Filters by departments matching the given specializations
      3. Returns filtered doctors with their available slots

    Args:
        specializations: List of target medical specializations (e.g., ["General Medicine", "Neurology"])
        facility_id: Facility ID (defaults to DEFAULT_FACILITY_ID)
        from_date: Start date (YYYY-MM-DD, defaults to today)
        to_date: End date (YYYY-MM-DD, defaults to today+3)

    Returns:
        JSON with filtered doctors grouped by department, with available slots
    """
    from app.agent.symptom_engine import filter_doctors_by_specialization

    fac_id = facility_id or settings.DEFAULT_FACILITY_ID
    if not from_date:
        from_date = datetime.now().strftime("%Y-%m-%d")
    if not to_date:
        to_date = (datetime.now() + timedelta(days=3)).strftime("%Y-%m-%d")

    logger.info(
        f"[MCP] get_doctors_by_symptoms | specializations={specializations} "
        f"facility={fac_id} from={from_date} to={to_date}"
    )

    try:
        # Step 1: Fetch ALL doctors' availability for this facility
        availability = await aarogya_api.get_doctors_availability(
            facility_id=fac_id,
            from_date=from_date,
            to_date=to_date,
        )

        if not availability:
            return json.dumps({
                "success": False,
                "doctors": [],
                "message": "No doctors available at this facility currently.",
            })

        # Step 2: Filter by specializations
        filtered = filter_doctors_by_specialization(availability, specializations)

        if not filtered:
            # No matching specialists found — return all doctors as fallback
            logger.info("[MCP] No specialists matched, returning all doctors")
            filtered = availability
            fallback = True
        else:
            fallback = False

        # Step 3: Format output — group by doctor with embedded slots
        doctors_map = {}  # healthProfessionalId → doctor info
        for entry in filtered:
            hp_id = entry.get("healthProfessionalId", "")
            hp_name = entry.get("healthProfessionalName", "")
            dept = entry.get("department", "")
            date_str = entry.get("appointmentDate", "")

            if hp_id not in doctors_map:
                doctors_map[hp_id] = {
                    "healthProfessionalId": hp_id,
                    "name": hp_name,
                    "department": dept,
                    "dates": [],
                }

            # Extract available slots for this date
            date_slots = []
            for sched in entry.get("schedule", []):
                session_name = sched.get("session", "")
                for slot in sched.get("slots", []):
                    if _slot_is_available(slot.get("isAvailable")):
                        date_slots.append({
                            "startTime": slot.get("from", ""),
                            "endTime": slot.get("to", ""),
                            "session": session_name,
                        })

            if date_slots:
                doctors_map[hp_id]["dates"].append({
                    "date": date_str,
                    "slots": date_slots,
                })

        # Convert to sorted list
        doctors_list = []
        for i, (hp_id, doc) in enumerate(doctors_map.items(), 1):
            total_slots = sum(len(d["slots"]) for d in doc["dates"])
            if total_slots > 0:
                doc["index"] = i
                doc["totalSlots"] = total_slots
                doctors_list.append(doc)

        return json.dumps({
            "success": True,
            "count": len(doctors_list),
            "specializations": specializations,
            "fallback": fallback,
            "doctors": doctors_list,
        })

    except Exception as e:
        logger.error(f"[MCP] get_doctors_by_symptoms error: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e), "doctors": []})


# ═══════════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════════

def _slot_is_available(value: Any) -> bool:
    """Check if a slot is available based on various possible value types."""
    if value is None:
        return True
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    return text in ("true", "1", "yes", "available")


def run_mcp_server():
    mcp.run(transport="stdio")


if __name__ == "__main__":
    run_mcp_server()