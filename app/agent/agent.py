"""
Medical Appointment Booking Agent — 4-Step MCP Tool Flow

Uses MCP tools in strict sequence:
  1. get_doctors_list          → Show doctors
  2. get_doctor_facilities     → Show facilities for selected doctor
  3. get_doctor_availability   → Show available time slots
  4. book_appointment          → Book the appointment

The agent detects user intent, manages conversation state, and calls
MCP tools directly (in-process) for zero-overhead tool execution.
"""
import json
import re
import logging
import uuid
import asyncio
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timedelta

import httpx

from app.config.settings import settings

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# MCP Tool imports (called directly in-process for speed)
# ═══════════════════════════════════════════════════════════════════════════════

from app.mcp.mcp_server import (
    get_doctors_list as mcp_get_doctors_list,
    get_doctor_facilities as mcp_get_doctor_facilities,
    get_doctor_availability as mcp_get_doctor_availability,
    book_appointment as mcp_book_appointment,
)


# ═══════════════════════════════════════════════════════════════════════════════
# Constants
# ═══════════════════════════════════════════════════════════════════════════════

FORBIDDEN_NAMES = {
    "i", "me", "my", "mine", "myself",
    "hu", "mane", "hun", "ame", "mara", "maru", "mujhe", "muze", "mera", "meri",
    "and", "the", "a", "an",
    "dr", "mrs", "mr", "ms", "doc", "doctor",
}

REQUIRED_PATIENT_FIELDS = [
    "firstName", "lastName", "mobile", "gender", "address",
]


# ═══════════════════════════════════════════════════════════════════════════════
# Session Manager
# ═══════════════════════════════════════════════════════════════════════════════

class SessionManager:
    """Manages conversation state across multiple chat turns."""

    def __init__(self):
        self._sessions: Dict[str, Dict] = {}

    def get(self, sid: str) -> Dict:
        if sid not in self._sessions:
            self._sessions[sid] = self._new_session()
        return self._sessions[sid]

    def _new_session(self) -> Dict:
        return {
            "messages": [],
            "stage": "start",
            # Stage flow: start → doctors_shown → facility_shown → slots_shown
            #           → patient_collection → confirm → booked

            # ── MCP data (stored from tool responses) ────────────────────
            "doctors_data": [],          # Raw doctor list from MCP tool 1
            "facilities_data": [],       # Raw facilities from MCP tool 2
            "availability_data": [],     # Raw availability from MCP tool 3
            "flat_slots": [],            # Flattened slots for numbered selection

            # ── User selections ──────────────────────────────────────────
            "selected_doctor": None,     # {healthProfessionalId, name, index}
            "selected_facility": None,   # {facilityId, name, index}
            "selected_slot": None,       # {date, startTime, endTime, session}

            # ── Patient details ──────────────────────────────────────────
            "patient": {
                "firstName": None,
                "lastName": None,
                "mobile": None,
                "gender": None,
                "birthDate": None,
                "address": None,
                "pinCode": None,
                "area": None,
            },

            "booked": False,
        }

    def add_message(self, sid: str, role: str, content: str):
        self.get(sid)["messages"].append({"role": role, "content": content})

    def messages(self, sid: str) -> List[Dict]:
        return self.get(sid)["messages"]

    def update_patient(self, sid: str, updates: Dict):
        patient = self.get(sid)["patient"]
        for k, v in updates.items():
            if v is not None and str(v).strip() not in ("", "null", "none"):
                if k in patient:
                    patient[k] = v

    def reset(self, sid: str):
        self._sessions.pop(sid, None)


session_manager = SessionManager()


# ═══════════════════════════════════════════════════════════════════════════════
# Patient info extraction (regex-based, no LLM needed)
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_patient_info(text: str) -> Dict:
    """
    Zero-latency extraction using regex for structured patient data.
    Handles labels, comma-separated, and natural language input.
    """
    result: Dict = {}
    t = text.strip()
    tl = t.lower()

    # ── Explicit Labels (Highest Priority) ──────────────────────────────────
    name_label_m = re.search(r'(?:name|full name|patient name)\s*[:\-]\s*([A-Za-z\s]+)', tl)
    if name_label_m:
        parts = name_label_m.group(1).strip().split()
        parts = [p for p in parts if p.lower() not in FORBIDDEN_NAMES]
        if len(parts) >= 1:
            result["firstName"] = parts[0].capitalize()
        if len(parts) >= 2:
            result["lastName"] = " ".join(p.capitalize() for p in parts[1:])

    dob_label_m = re.search(r'(?:dob|date of birth)\s*[:\-]\s*([\d\-\/]+)', tl)
    if dob_label_m:
        dob_str = dob_label_m.group(1).strip()
        d1 = re.search(r'(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})', dob_str)
        d2 = re.search(r'(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})', dob_str)
        if d1:
            result["birthDate"] = f"{d1.group(3)}-{d1.group(2).zfill(2)}-{d1.group(1).zfill(2)}"
        elif d2:
            result["birthDate"] = f"{d2.group(1)}-{d2.group(2).zfill(2)}-{d2.group(3).zfill(2)}"

    addr_label_m = re.search(r'(?:address|addr)\s*[:\-]\s*([^,\n]+)', tl)
    if addr_label_m:
        result["address"] = addr_label_m.group(1).strip()

    mob_label_m = re.search(r'(?:mobile|phone|contact)\s*[:\-]\s*(\d{10})', tl)
    if mob_label_m:
        result["mobile"] = mob_label_m.group(1).strip()

    gender_label_m = re.search(r'(?:gender|sex)\s*[:\-]\s*(male|female|m|f|purush|stri)', tl)
    if gender_label_m:
        g = gender_label_m.group(1).strip()
        result["gender"] = 2 if g in ['female', 'f', 'stri'] else 1

    # ── Fallback unstructured matching ──────────────────────────────────────
    if "mobile" not in result:
        mob = re.search(r'\b(\d{10})\b', t)
        if mob:
            result["mobile"] = mob.group(1)

    pin = re.search(r'\b(\d{6})\b', t)
    if pin and pin.group(1) != result.get("mobile", ""):
        result["pinCode"] = pin.group(1)

    if "birthDate" not in result:
        dob1 = re.search(r'\b(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})\b', t)
        dob2 = re.search(r'\b(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})\b', t)
        if dob1:
            result["birthDate"] = f"{dob1.group(3)}-{dob1.group(2).zfill(2)}-{dob1.group(1).zfill(2)}"
        elif dob2:
            result["birthDate"] = f"{dob2.group(1)}-{dob2.group(2).zfill(2)}-{dob2.group(3).zfill(2)}"

    if "gender" not in result:
        if re.search(r'\b(female|stri|mahila|woman|girl)\b', tl):
            result["gender"] = 2
        elif re.search(r'\b(male|purush|man|boy)\b', tl):
            result["gender"] = 1

    if "firstName" not in result:
        name_m = re.search(
            r'(?:my\s+name\s+is|i\s+am|name\s*[:=]\s*|naam\s+(?:hai\s+)?|maro\s+naam\s+(?:che\s+)?|mera\s+naam\s+(?:hai\s+)?)([A-Za-z]+(?:\s+[A-Za-z]+)*)',
            tl
        )
        if name_m:
            parts = name_m.group(1).strip().split()
            parts = [p for p in parts if p.lower() not in FORBIDDEN_NAMES]
            if len(parts) >= 1:
                result["firstName"] = parts[0].capitalize()
            if len(parts) >= 2:
                result["lastName"] = " ".join(p.capitalize() for p in parts[1:])

    if "address" not in result:
        addr_m = re.search(r'(?:address|addr|rehta|rehti|rahata|rahti)[\s:]+([^,\n]+)', tl)
        if addr_m:
            result["address"] = addr_m.group(1).strip()

    # Comma-separated fallback (e.g. "Rahul Patel, 12-05-1990, Male, 9876543210")
    if "firstName" not in result and "," in t:
        chunks = [c.strip() for c in t.split(",") if c.strip()]
        if len(chunks) >= 3:
            if re.match(r'^[A-Za-z\s]+$', chunks[0]) and chunks[0].lower() not in FORBIDDEN_NAMES:
                np = chunks[0].split()
                result["firstName"] = np[0].capitalize()
                if len(np) > 1:
                    result["lastName"] = " ".join(x.capitalize() for x in np[1:])
            for c in chunks:
                if "address" not in result and len(c) > 5 and not re.match(r'^[\d\-\/]+$', c) and c.lower() not in ['male', 'female', 'stri', 'purush']:
                    if c != chunks[0]:
                        result["address"] = c

    return result


def _missing_patient_fields(patient: Dict) -> List[str]:
    """Returns list of missing required patient fields."""
    missing = []
    for k in REQUIRED_PATIENT_FIELDS:
        val = patient.get(k)
        if not val or str(val).strip().lower() in ("", "null", "none"):
            missing.append(k)
            continue
        if k in ("firstName", "lastName"):
            v = str(val).strip()
            if v.lower() in FORBIDDEN_NAMES or len(v) < 2:
                missing.append(k)
        if k == "mobile":
            if not re.match(r"^\d{10}$", str(val).replace(" ", "")):
                missing.append(k)

    dob = patient.get("birthDate")
    if not dob or not re.match(r"^\d{4}-\d{2}-\d{2}$", str(dob)):
        missing.append("Date of Birth (YYYY-MM-DD)")

    return missing


# ═══════════════════════════════════════════════════════════════════════════════
# The Agent — 4-Step MCP Flow
# ═══════════════════════════════════════════════════════════════════════════════

class AppointmentAgent:
    """
    Agentic booking assistant that calls MCP tools in sequence:
      get_doctors_list → get_doctor_facilities → get_doctor_availability → book_appointment
    """

    # ── Main chat handler ─────────────────────────────────────────────────────

    async def chat(self, session_id: str, user_message: str) -> Dict[str, Any]:
        session = session_manager.get(session_id)
        stage = session["stage"]

        logger.info(f"--- Chat [{session_id}] stage={stage} msg={user_message[:80]} ---")

        session_manager.add_message(session_id, "user", user_message)

        # ── Route to the correct stage handler ─────────────────────────────
        if stage == "start":
            return await self._handle_start(session_id, session, user_message)

        elif stage == "doctors_shown":
            return await self._handle_doctor_selection(session_id, session, user_message)

        elif stage == "facility_shown":
            return await self._handle_facility_selection(session_id, session, user_message)

        elif stage == "slots_shown":
            return await self._handle_slot_selection(session_id, session, user_message)

        elif stage == "patient_collection":
            return await self._handle_patient_info(session_id, session, user_message)

        elif stage == "confirm":
            return await self._handle_confirmation(session_id, session, user_message)

        elif stage == "booked":
            return self._reply(session_id, "Your appointment is already booked! Type 'new' to book another one.")

        else:
            return await self._handle_start(session_id, session, user_message)

    # ══════════════════════════════════════════════════════════════════════════
    # STAGE: start — Detect intent, fetch doctors via MCP Tool 1
    # ══════════════════════════════════════════════════════════════════════════

    async def _handle_start(self, sid: str, session: Dict, msg: str) -> Dict:
        msg_low = msg.strip().lower()

        # Handle "new" command to restart
        if msg_low in ("new", "reset", "start over", "restart"):
            session_manager.reset(sid)
            return self._reply(sid, "Session reset! How can I help you book an appointment?")

        # Greetings
        if msg_low in ("hi", "hello", "hey", "namaste", "kem cho"):
            return self._reply(sid, "Hello! 👋 How can I help you with booking an appointment?")

        # Any booking-related intent → fetch doctors
        response_text = "Let me fetch the available doctors for you…\n\n"

        # ── MCP Tool 1: get_doctors_list ──────────────────────────────────
        raw = await mcp_get_doctors_list()
        data = json.loads(raw)

        if not data.get("success") or not data.get("doctors"):
            return self._reply(sid, "⚠️ No doctors are available at the moment. Please try again later.")

        doctors = data["doctors"]
        session["doctors_data"] = doctors

        # Build numbered list of doctor names ONLY
        lines = ["Here are our available doctors:\n"]
        for doc in doctors:
            lines.append(f"  {doc['index']}. {doc['name']}")

        lines.append("\nWhich doctor would you like to book with? (Enter the number)")

        session["stage"] = "doctors_shown"
        response_text += "\n".join(lines)
        return self._reply(sid, response_text)

    # ══════════════════════════════════════════════════════════════════════════
    # STAGE: doctors_shown — User selects a doctor → MCP Tool 2
    # ══════════════════════════════════════════════════════════════════════════

    async def _handle_doctor_selection(self, sid: str, session: Dict, msg: str) -> Dict:
        doctors = session.get("doctors_data", [])

        # Try to parse a number selection
        digit_match = re.search(r'^\s*(\d+)\s*$', msg)
        if digit_match:
            index = int(digit_match.group(1))
            selected = next((d for d in doctors if d["index"] == index), None)
        else:
            # Try to match by name
            msg_low = msg.strip().lower()
            selected = next(
                (d for d in doctors if msg_low in d.get("name", "").lower()),
                None
            )

        if not selected:
            return self._reply(
                sid,
                f"Please enter a valid number between 1 and {len(doctors)}, "
                f"or type the doctor's name."
            )

        session["selected_doctor"] = {
            "healthProfessionalId": selected["healthProfessionalId"],
            "name": selected["name"],
            "index": selected["index"],
        }
        logger.info(f"[{sid}] Doctor selected: {selected['name']}")

        # ── MCP Tool 2: get_doctor_facilities ────────────────────────────
        response_text = f"Checking available locations for **{selected['name']}**…\n\n"

        raw = await mcp_get_doctor_facilities(
            health_professional_id=selected["healthProfessionalId"]
        )
        data = json.loads(raw)

        if not data.get("success") or not data.get("facilities"):
            return self._reply(sid, f"⚠️ No facilities found for {selected['name']}. Please try another doctor.")

        facilities = data["facilities"]
        session["facilities_data"] = facilities

        if len(facilities) == 1:
            # ── Auto-select single facility, immediately fetch availability ──
            fac = facilities[0]
            session["selected_facility"] = {
                "facilityId": fac["facilityId"],
                "name": fac["name"],
                "index": 1,
            }
            logger.info(f"[{sid}] Auto-selected facility: {fac['name']}")

            response_text += f"**{selected['name']}** is available at **{fac['name']}**.\n"
            response_text += "Let me check the available slots…\n\n"

            # ── MCP Tool 3: get_doctor_availability (auto-chained) ───────
            return await self._fetch_and_show_slots(sid, session, response_text)

        else:
            # Multiple facilities → ask user to choose
            lines = [f"**{selected['name']}** is available at multiple locations:\n"]
            for fac in facilities:
                addr = fac.get("address", "")
                lines.append(f"  {fac['index']}. {fac['name']}" + (f" — {addr}" if addr else ""))
            lines.append("\nWhich facility would you like to visit? (Enter the number)")

            session["stage"] = "facility_shown"
            response_text += "\n".join(lines)
            return self._reply(sid, response_text)

    # ══════════════════════════════════════════════════════════════════════════
    # STAGE: facility_shown — User selects a facility → MCP Tool 3
    # ══════════════════════════════════════════════════════════════════════════

    async def _handle_facility_selection(self, sid: str, session: Dict, msg: str) -> Dict:
        facilities = session.get("facilities_data", [])

        digit_match = re.search(r'^\s*(\d+)\s*$', msg)
        if digit_match:
            index = int(digit_match.group(1))
            selected = next((f for f in facilities if f["index"] == index), None)
        else:
            msg_low = msg.strip().lower()
            selected = next(
                (f for f in facilities if msg_low in f.get("name", "").lower()),
                None
            )

        if not selected:
            return self._reply(
                sid,
                f"Please enter a valid number between 1 and {len(facilities)}."
            )

        session["selected_facility"] = {
            "facilityId": selected["facilityId"],
            "name": selected["name"],
            "index": selected["index"],
        }
        logger.info(f"[{sid}] Facility selected: {selected['name']}")

        response_text = f"Fetching available slots at **{selected['name']}**…\n\n"

        # ── MCP Tool 3: get_doctor_availability ──────────────────────────
        return await self._fetch_and_show_slots(sid, session, response_text)

    # ── Helper: Fetch availability and display slots ─────────────────────────

    async def _fetch_and_show_slots(self, sid: str, session: Dict, prefix: str) -> Dict:
        """Calls MCP Tool 3 and formats the time slots for display."""
        doctor = session["selected_doctor"]
        facility = session["selected_facility"]

        raw = await mcp_get_doctor_availability(
            health_professional_id=doctor["healthProfessionalId"],
            facility_id=facility["facilityId"],
        )
        data = json.loads(raw)

        if not data.get("success") or data.get("totalSlots", 0) == 0:
            return self._reply(
                sid,
                f"{prefix}⚠️ No available slots found for **{doctor['name']}** at **{facility['name']}**. "
                f"Would you like to try a different doctor?"
            )

        availability = data["availability"]
        session["availability_data"] = availability

        # Build a flat numbered list of all slots
        flat_slots = []
        slot_num = 1
        lines = [f"Available slots for **{doctor['name']}** at **{facility['name']}**:\n"]

        for date_group in availability:
            date_str = date_group["date"]
            try:
                date_obj = datetime.strptime(date_str, "%Y-%m-%d")
                display_date = date_obj.strftime("%A, %d %B %Y")
            except ValueError:
                display_date = date_str

            lines.append(f"\n📅 **{display_date}**")

            for slot in date_group["slots"]:
                start = slot["startTime"]
                end = slot["endTime"]
                session_name = slot.get("session", "")

                flat_slots.append({
                    "index": slot_num,
                    "date": date_str,
                    "startTime": start,
                    "endTime": end,
                    "session": session_name,
                })
                lines.append(f"  {slot_num}. ⏰ {start} – {end}  ({session_name})")
                slot_num += 1

        session["flat_slots"] = flat_slots
        lines.append("\nWhich slot would you like to book? (Enter the number)")

        session["stage"] = "slots_shown"
        return self._reply(sid, prefix + "\n".join(lines))

    # ══════════════════════════════════════════════════════════════════════════
    # STAGE: slots_shown — User selects a time slot
    # ══════════════════════════════════════════════════════════════════════════

    async def _handle_slot_selection(self, sid: str, session: Dict, msg: str) -> Dict:
        flat_slots = session.get("flat_slots", [])

        digit_match = re.search(r'^\s*(\d+)\s*$', msg)
        if not digit_match:
            return self._reply(sid, f"Please enter a slot number between 1 and {len(flat_slots)}.")

        index = int(digit_match.group(1))
        selected = next((s for s in flat_slots if s["index"] == index), None)

        if not selected:
            return self._reply(sid, f"Invalid slot number. Please enter a number between 1 and {len(flat_slots)}.")

        session["selected_slot"] = selected
        doctor = session["selected_doctor"]
        facility = session["selected_facility"]

        logger.info(f"[{sid}] Slot selected: {selected['date']} {selected['startTime']}-{selected['endTime']}")

        # Check if we already have patient info
        missing = _missing_patient_fields(session["patient"])

        if not missing:
            # All patient details already available → go to confirmation
            session["stage"] = "confirm"
            return self._show_confirmation(sid, session)
        else:
            # Need patient details
            session["stage"] = "patient_collection"
            response = (
                f"Great! You selected: **{selected['date']}** at **{selected['startTime']} – {selected['endTime']}** "
                f"with **{doctor['name']}** at **{facility['name']}**.\n\n"
            )
            response += self._ask_for_missing_fields(session["patient"])
            return self._reply(sid, response)

    # ══════════════════════════════════════════════════════════════════════════
    # STAGE: patient_collection — Collect missing patient details
    # ══════════════════════════════════════════════════════════════════════════

    async def _handle_patient_info(self, sid: str, session: Dict, msg: str) -> Dict:
        # Extract patient info from the message
        extracted = _extract_patient_info(msg)

        if extracted:
            session_manager.update_patient(sid, extracted)
            logger.info(f"[{sid}] Extracted patient info: {extracted}")

        missing = _missing_patient_fields(session["patient"])

        if missing:
            return self._reply(sid, self._ask_for_missing_fields(session["patient"]))
        else:
            # All fields collected → go to confirmation
            session["stage"] = "confirm"
            return self._show_confirmation(sid, session)

    # ══════════════════════════════════════════════════════════════════════════
    # STAGE: confirm — Show summary and ask for confirmation
    # ══════════════════════════════════════════════════════════════════════════

    async def _handle_confirmation(self, sid: str, session: Dict, msg: str) -> Dict:
        msg_low = msg.strip().lower()

        if msg_low in ("no", "cancel", "nope", "nahi"):
            session_manager.reset(sid)
            return self._reply(sid, "Booking cancelled. How else can I help?")

        if re.search(r'\b(yes|yeah|confirm|ok|yup|ha|haa)\b', msg_low):
            return await self._do_booking(sid, session)

        return self._reply(sid, "Please type **Yes** to confirm or **No** to cancel.")

    # ── Show confirmation summary ────────────────────────────────────────────

    def _show_confirmation(self, sid: str, session: Dict) -> Dict:
        patient = session["patient"]
        doctor = session["selected_doctor"]
        facility = session["selected_facility"]
        slot = session["selected_slot"]
        gender_str = "Female" if patient.get("gender") == 2 else "Male"

        conf_msg = (
            "**📄 Please confirm your booking details:**\n\n"
            f"👤 **Patient:** {patient.get('firstName', '')} {patient.get('lastName', '')}\n"
            f"📱 **Mobile:** {patient.get('mobile', '')}\n"
            f"🎂 **DOB:** {patient.get('birthDate', '')}\n"
            f"⚧ **Gender:** {gender_str}\n"
            f"🏠 **Address:** {patient.get('address', '')}\n\n"
            f"👨‍⚕️ **Doctor:** {doctor['name']}\n"
            f"🏥 **Facility:** {facility['name']}\n"
            f"📅 **Date:** {slot['date']}\n"
            f"⏰ **Time:** {slot['startTime']} – {slot['endTime']}\n\n"
            "Type **Yes** to confirm, or **No** to cancel."
        )
        return self._reply(sid, conf_msg)

    # ══════════════════════════════════════════════════════════════════════════
    # BOOKING — MCP Tool 4: book_appointment
    # ══════════════════════════════════════════════════════════════════════════

    async def _do_booking(self, sid: str, session: Dict) -> Dict:
        """Calls MCP Tool 4 to book the appointment."""
        patient = session["patient"]
        doctor = session["selected_doctor"]
        facility = session["selected_facility"]
        slot = session["selected_slot"]

        logger.info(f"[{sid}] Booking appointment…")

        try:
            raw = await mcp_book_appointment(
                first_name=patient.get("firstName", ""),
                last_name=patient.get("lastName", ""),
                mobile=patient.get("mobile", ""),
                gender=patient.get("gender", 1),
                birth_date=patient.get("birthDate", "2000-01-01"),
                health_professional_id=doctor["healthProfessionalId"],
                facility_id=facility["facilityId"],
                slot_date=slot["date"],
                slot_start_time=slot["startTime"],
                symptoms=["General consultation"],
                middle_name="",
                pin_code=patient.get("pinCode", ""),
                address=patient.get("address", ""),
                area=patient.get("area", ""),
            )
            data = json.loads(raw)

            if data.get("success"):
                session["stage"] = "booked"
                session["booked"] = True

                success_msg = (
                    "🎉 **Your appointment has been booked successfully!**\n\n"
                    f"👨‍⚕️ **Doctor:** {doctor['name']}\n"
                    f"🏥 **Facility:** {facility['name']}\n"
                    f"📅 **Date:** {slot['date']}\n"
                    f"⏰ **Time:** {slot['startTime']} – {slot['endTime']}\n\n"
                    "Please arrive 10 minutes early. See you there! 🙏"
                )
                return self._reply(sid, success_msg, booked=True)
            else:
                error = data.get("error", "Unknown error")
                return self._reply(
                    sid,
                    f"❌ Booking failed: {error}\n\n"
                    "Would you like to try a different time slot, or shall I retry?"
                )

        except Exception as e:
            logger.error(f"[{sid}] Booking error: {e}", exc_info=True)
            return self._reply(
                sid,
                f"❌ Something went wrong: {e}\n\nWould you like to try again?"
            )

    # ═══════════════════════════════════════════════════════════════════════════
    # Helpers
    # ═══════════════════════════════════════════════════════════════════════════

    def _ask_for_missing_fields(self, patient: Dict) -> str:
        """Build a prompt asking for missing patient fields."""
        missing = _missing_patient_fields(patient)
        field_map = {
            "firstName": "Full Name",
            "lastName": "Full Name",
            "mobile": "Mobile (10 digits)",
            "gender": "Gender (Male/Female)",
            "address": "Address",
            "Date of Birth (YYYY-MM-DD)": "Date of Birth (YYYY-MM-DD)",
        }
        # Deduplicate labels
        labels = []
        for f in missing:
            label = field_map.get(f, f)
            if label not in labels:
                labels.append(label)

        lines = ["**Please provide your details:**\n"]
        for label in labels:
            lines.append(f"**{label}:** ")

        lines.append("\n_(You can type all details in one message, e.g.: Name: Rahul Patel, DOB: 15-05-1995, Gender: Male, Mobile: 9876543210, Address: Ahmedabad)_")
        return "\n".join(lines)

    def _reply(self, sid: str, text: str, booked: bool = False) -> Dict:
        """Build a standard response dict."""
        session_manager.add_message(sid, "assistant", text)
        session = session_manager.get(sid)
        return {
            "session_id": sid,
            "response": text,
            "appointment_booked": booked,
            "booking_details": {
                "doctor": session.get("selected_doctor"),
                "facility": session.get("selected_facility"),
                "slot": session.get("selected_slot"),
                "patient": session.get("patient"),
            } if booked else {},
        }

    def reset(self, session_id: str):
        session_manager.reset(session_id)


appointment_agent = AppointmentAgent()