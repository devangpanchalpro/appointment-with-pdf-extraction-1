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
    Fetch the full list of available doctors at a specific facility.

    Args:
        facility_id: Facility ID to filter doctors (required)
        page_size: Number of doctors per page (default 20)
        skip_count: Number of doctors to skip for pagination (default 0)

    Returns:
        JSON with doctors array containing name, healthProfessionalId, facility info
    """
    if not facility_id:
        return json.dumps({
            "success": False,
            "doctors": [],
            "message": "facility_id is required. Please select a facility first.",
        })
    fac_id = facility_id
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
            first = name_obj.get("firstName", "").strip()
            middle = name_obj.get("middleName", "").strip()
            last = name_obj.get("lastName", "").strip()
            
            # Handle cases where firstName is "Dr."
            if first.lower().rstrip(".") in ("dr", "doctor"):
                full_name = f"Dr. {middle} {last}".strip()
            elif first.lower().startswith("dr. "):
                full_name = f"{first} {middle} {last}".strip()
            else:
                parts = [first, middle, last]
                full_name = " ".join(p for p in parts if p).strip()

            if not full_name or full_name.lower() in ("unknown", "n/a", "dr.", "dr. "):
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
        raw_data = await aarogya_api.get_doctors_availability(
            facility_id=facility_id,
            from_date=from_date,
            to_date=to_date,
            health_professional_id=health_professional_id,
        )

        if not raw_data:
            return json.dumps({
                "success": False,
                "slots": [],
                "message": "No available slots found for this doctor at this facility.",
            })

        # Extract result array from raw API response
        if isinstance(raw_data, dict) and "result" in raw_data:
            result_entries = raw_data["result"]
        elif isinstance(raw_data, list):
            result_entries = raw_data
        else:
            logger.warning(f"[MCP] Unexpected availability response: {str(raw_data)[:300]}")
            return json.dumps({
                "success": False,
                "slots": [],
                "message": "Unexpected response format from availability API.",
            })

        if not result_entries:
            return json.dumps({
                "success": False,
                "slots": [],
                "message": "No available slots found for this doctor at this facility.",
            })

        logger.info(f"[MCP] availability result has {len(result_entries)} date entries")

        # Extract slots from the raw response — handle ALL possible structures
        all_dates = []
        for date_entry in result_entries:
            date_str = date_entry.get("appointmentDate", "")

            # Collect ALL healthProfessionals from this date entry
            # Structure A: date → departments[] → healthProfessionals[]
            # Structure B: date → healthProfessionals[] (no departments wrapper)
            all_hps = []

            # Try Structure A: via departments
            for dept in date_entry.get("departments", []):
                for hp in dept.get("healthProfessionals", []):
                    hp["_department"] = dept.get("name", "")
                    all_hps.append(hp)

            # Try Structure B: direct healthProfessionals
            if not all_hps:
                for hp in date_entry.get("healthProfessionals", []):
                    hp["_department"] = ""
                    all_hps.append(hp)

            # Extract slots from each healthProfessional
            for hp in all_hps:
                doctor_name = hp.get("healthProfessionalName", "")
                department = hp.get("_department", "")

                date_slots = []
                for sched in hp.get("schedule", []):
                    session_name = sched.get("session", "")
                    for slot in sched.get("slots", []):
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

        total_slots = sum(len(d["slots"]) for d in all_dates)
        logger.info(f"[MCP] Extracted {total_slots} slots across {len(all_dates)} date groups")

        if total_slots == 0:
            return json.dumps({
                "success": False,
                "slots": [],
                "message": "No available slots found for this doctor at this facility.",
            })

        return json.dumps({
            "success": True,
            "healthProfessionalId": health_professional_id,
            "facilityId": facility_id,
            "availability": all_dates,
            "totalSlots": total_slots,
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
# Tool 5: get_doctors_by_symptoms (AI-powered symptom -> doctor matching)
#         Searches ALL facilities using get_doctors_list (not availability)
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def get_doctors_by_symptoms(
    specializations: List[str],
    facility_id: Optional[str] = None,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
) -> str:
    """
    Fetch doctors filtered by medical specializations across ALL facilities.

    Uses get_doctors_list (not availability) to find ALL doctors whose
    professionalDetail.specialization or subSpecialization match the symptoms.

    Args:
        specializations: List of target medical specializations
        facility_id: Optional single facility ID (searches only that facility)
        from_date: Not used in this version (kept for compatibility)
        to_date: Not used in this version (kept for compatibility)

    Returns:
        JSON with matched doctors, their specializations, and facility info
    """
    import asyncio

    logger.info(
        f"[MCP] get_doctors_by_symptoms | specializations={specializations} "
        f"facility={facility_id}"
    )

    # Normalize specializations for matching
    spec_lower = [s.lower().strip() for s in specializations]

    try:
        # ── Determine which facilities to search ─────────────────────────
        if facility_id:
            facilities_to_search = [{"facilityId": facility_id, "name": ""}]
        else:
            all_facilities = await aarogya_api.get_facilities_list()
            if not all_facilities:
                return json.dumps({
                    "success": False,
                    "doctors": [],
                    "message": "No facilities available currently.",
                })
            facilities_to_search = [
                {"facilityId": f.get("facilityId", ""), "name": f.get("name", "")}
                for f in all_facilities
            ]

        # ── Fetch doctors from all facilities in parallel ────────────────
        async def fetch_doctors_from_facility(fac):
            fac_id = fac["facilityId"]
            fac_name = fac["name"]
            try:
                doctors_raw = await aarogya_api.get_doctors_list(
                    facility_id=fac_id,
                    page_size=100,  # Get all doctors
                )
                # Tag each doctor with facility info
                for doc in doctors_raw:
                    doc["_facilityId"] = fac_id
                    doc["_facilityName"] = fac_name
                return doctors_raw
            except Exception as e:
                logger.warning(f"[MCP] Failed to fetch doctors for {fac_name}: {e}")
                return []

        results = await asyncio.gather(*[fetch_doctors_from_facility(f) for f in facilities_to_search])
        all_doctors_raw = []
        for result in results:
            all_doctors_raw.extend(result)

        if not all_doctors_raw:
            return json.dumps({
                "success": False,
                "doctors": [],
                "message": "No doctors found at any facility.",
            })

        logger.info(f"[MCP] Total doctors fetched across all facilities: {len(all_doctors_raw)}")

        # ── Filter by specialization / subSpecialization ─────────────────
        def matches_specialization(doc):
            """Check if doctor's specialization/subSpecialization matches any target."""
            prof = doc.get("professionalDetail", {})
            doc_specs = prof.get("specialization", [])
            doc_sub_specs = prof.get("subSpecialization", [])

            # Also check 'aboutMe' field for keywords
            about_me = prof.get("aboutMe", "").lower()

            # Combine all searchable fields
            all_doc_specs = []
            if isinstance(doc_specs, list):
                all_doc_specs.extend([s.lower().strip() for s in doc_specs])
            elif isinstance(doc_specs, str):
                all_doc_specs.append(doc_specs.lower().strip())

            if isinstance(doc_sub_specs, list):
                all_doc_specs.extend([s.lower().strip() for s in doc_sub_specs])
            elif isinstance(doc_sub_specs, str):
                all_doc_specs.append(doc_sub_specs.lower().strip())

            for target_spec in spec_lower:
                for doc_spec in all_doc_specs:
                    # Exact match
                    if target_spec == doc_spec:
                        return True
                    # Partial match (e.g., "cardiology" in "interventional cardiology")
                    if target_spec in doc_spec or doc_spec in target_spec:
                        return True
                    # Word-level match (e.g., "general" + "medicine")
                    target_words = set(target_spec.split())
                    doc_words = set(doc_spec.split())
                    if target_words & doc_words:
                        return True

                # Also check aboutMe
                if target_spec in about_me:
                    return True

            return False

        matched_doctors = [d for d in all_doctors_raw if matches_specialization(d)]
        fallback = False

        if not matched_doctors:
            logger.info("[MCP] No specialists matched, returning all doctors as fallback")
            matched_doctors = all_doctors_raw
            fallback = True

        logger.info(f"[MCP] Matched doctors: {len(matched_doctors)} (fallback={fallback})")

        # ── Deduplicate by healthProfessionalId ──────────────────────────
        doctors_map = {}  # healthProfessionalId -> doctor info
        for doc in matched_doctors:
            hp_id = doc.get("healthProfessionalId", "")
            if not hp_id:
                continue

            # Build doctor name correctly:  firstName middleName lastName
            name_obj = doc.get("name", {})
            first = name_obj.get("firstName", "").strip()
            middle = name_obj.get("middleName", "").strip()
            last = name_obj.get("lastName", "").strip()

            # Handle cases where firstName is "Dr." — use middleName as first
            if first.lower().rstrip(".") in ("dr", "doctor"):
                display_name = f"Dr. {middle} {last}".strip()
            else:
                parts = [first, middle, last]
                display_name = " ".join(p for p in parts if p)

            if not display_name or display_name.lower() in ("unknown", "n/a"):
                continue

            fac_id = doc.get("_facilityId", "")
            fac_name = doc.get("_facilityName", "")

            # Get specialization info for display
            prof = doc.get("professionalDetail", {})
            doc_specs = prof.get("specialization", [])
            doc_sub_specs = prof.get("subSpecialization", [])
            if isinstance(doc_specs, list):
                dept_display = ", ".join(doc_specs)
            else:
                dept_display = str(doc_specs)
            if isinstance(doc_sub_specs, list):
                sub_display = ", ".join(s for s in doc_sub_specs if s and s.lower() != "demo")
            else:
                sub_display = str(doc_sub_specs) if doc_sub_specs and str(doc_sub_specs).lower() != "demo" else ""

            if hp_id not in doctors_map:
                doctors_map[hp_id] = {
                    "healthProfessionalId": hp_id,
                    "name": display_name,
                    "department": dept_display,
                    "subSpecialization": sub_display,
                    "qualification": prof.get("qualification", ""),
                    "experience": prof.get("yearsOfExperience", ""),
                    "facilities": [],
                    "facilityId": fac_id,
                    "facilityName": fac_name,
                }

            # Track unique facilities for this doctor
            existing_fac_ids = [f["facilityId"] for f in doctors_map[hp_id]["facilities"]]
            if fac_id and fac_id not in existing_fac_ids:
                doctors_map[hp_id]["facilities"].append({
                    "facilityId": fac_id,
                    "facilityName": fac_name,
                })

        # Convert to indexed list
        doctors_list = []
        for i, (hp_id, doc) in enumerate(doctors_map.items(), 1):
            doc["index"] = i
            doc["facilityCount"] = len(doc["facilities"])
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
# Tool 6: get_facilities_list
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def get_facilities_list() -> str:
    """
    Fetch all available facilities (hospitals/clinics).

    Call this when the user asks for an appointment without specifying
    a doctor name. Shows them available hospitals to choose from.

    Returns:
        JSON with facilities array containing facilityId, name, address, specializations
    """
    logger.info("[MCP] get_facilities_list")

    try:
        facilities = await aarogya_api.get_facilities_list()

        if not facilities:
            return json.dumps({
                "success": False,
                "facilities": [],
                "message": "No facilities available currently.",
            })

        formatted = []
        for i, fac in enumerate(facilities, 1):
            location = fac.get("location", {})
            formatted.append({
                "index": i,
                "facilityId": fac.get("facilityId", ""),
                "name": fac.get("name", ""),
                "address": location.get("address", ""),
                "pincode": location.get("pincode", ""),
                "state": location.get("state", {}).get("name", ""),
                "district": location.get("district", {}).get("name", ""),
                "specializations": fac.get("specialization", []),
            })

        return json.dumps({
            "success": True,
            "count": len(formatted),
            "facilities": formatted,
        })

    except Exception as e:
        logger.error(f"[MCP] get_facilities_list error: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e), "facilities": []})


# ═══════════════════════════════════════════════════════════════════════════════
# Tool 7: search_doctor_by_name
# ═══════════════════════════════════════════════════════════════════════════════

@mcp.tool()
async def search_doctor_by_name(
    doctor_name: str,
) -> str:
    """
    Search for a doctor by name across ALL facilities in a single API call.

    Steps:
      1. Fetch ALL doctors (no FacilityId → API returns everyone)
      2. Filter locally by name (partial match — all search terms must appear)
      3. Deduplicate by healthProfessionalId
      4. Return matched list; facility fetch happens separately when user selects.

    Args:
        doctor_name: The doctor's name to search for (e.g., "Barot", "Deep Patel")

    Returns:
        JSON with list of matched doctors (healthProfessionalId + full name)
    """
    logger.info(f"[MCP] search_doctor_by_name | name={doctor_name}")

    search_terms = doctor_name.lower().strip()
    # Strip common prefixes
    for prefix in ["dr.", "dr ", "doctor ", "doc "]:
        if search_terms.startswith(prefix):
            search_terms = search_terms[len(prefix):].strip()

    try:
        # Single API call — FacilityId omitted → returns ALL doctors
        all_doctors = await aarogya_api.get_doctors_list(page_size=500)

        if not all_doctors:
            return json.dumps({
                "success": False,
                "message": "No doctors available at the moment.",
                "doctorCount": 0,
                "doctors": [],
            })

        logger.info(f"[MCP] Total doctors fetched (no facility filter): {len(all_doctors)}")

        matches = []
        for doc in all_doctors:
            name_obj = doc.get("name", {})
            first  = name_obj.get("firstName",  "").strip()
            middle = name_obj.get("middleName", "").strip()
            last   = name_obj.get("lastName",   "").strip()

            # Build display name, handling "Dr." stored as firstName
            if first.lower().rstrip(".") in ("dr", "doctor"):
                display_name = " ".join(p for p in ["Dr.", middle, last] if p).strip()
            elif first.lower().startswith("dr.") or first.lower().startswith("dr "):
                display_name = " ".join(p for p in [first, middle, last] if p).strip()
            else:
                display_name = " ".join(p for p in [first, middle, last] if p).strip()

            if not display_name or display_name.lower() in ("dr.", "unknown", "n/a"):
                continue

            full_name_lower = display_name.lower()

            # Match: ALL search terms must appear somewhere in the full name
            if all(t in full_name_lower for t in search_terms.split()):
                h_id = doc.get("healthProfessionalId", "")
                fac_id = doc.get("facilityId", "")
                fac_name = doc.get("facility", "")
                if h_id:
                    matches.append({
                        "healthProfessionalId": h_id,
                        "name": display_name,
                        "facilityId": fac_id,
                        "facilityName": fac_name,
                    })

        if not matches:
            return json.dumps({
                "success": False,
                "message": f"No doctor found matching '{doctor_name}'.",
                "doctorCount": 0,
                "doctors": [],
            })

        # Deduplicate by (healthProfessionalId + facilityId) pair
        # Same doctor at different facilities = separate entries (user picks combo)
        # Same doctor at same facility = show once
        unique_map: dict = {}
        for m in matches:
            pair_key = f"{m['healthProfessionalId']}_{m['facilityId']}"
            if pair_key not in unique_map:
                unique_map[pair_key] = m

        doctors_list = list(unique_map.values())
        for i, doc in enumerate(doctors_list, 1):
            doc["index"] = i

        logger.info(f"[MCP] Matched {len(doctors_list)} doctor-facility pair(s) for '{doctor_name}'")

        return json.dumps({
            "success": True,
            "doctorCount": len(doctors_list),
            "doctors": doctors_list,
        })

    except Exception as e:
        logger.error(f"[MCP] search_doctor_by_name error: {e}", exc_info=True)
        return json.dumps({"success": False, "error": str(e), "doctorCount": 0, "doctors": []})


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