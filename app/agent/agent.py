"""
Medical Appointment Booking Agent — Facility-First Flow

Three booking scenarios:
  1. Generic appointment → Show facilities → Select facility → Show doctors → Select doctor → Slots → Book
  2. Doctor name search  → Find doctor → Show facilities (if multi) → Slots → Book
  3. Symptom-based       → Analyze symptoms → Search ALL facilities → Show matching doctors+facilities → Slots → Book

Stage flow:
  Scenario 1: start → facilities_shown → facility_doctors_shown → slots_shown → patient_collection → confirm → booked
  Scenario 2: start → doctor_facilities_shown → slots_shown → patient_collection → confirm → booked
              start → slots_shown → patient_collection → confirm → booked  (if single facility)
  Scenario 3: start → symptom_doctors_shown → slots_shown → patient_collection → confirm → booked
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

    get_facilities_list as mcp_get_facilities_list,
    search_doctor_by_name as mcp_search_doctor_by_name,
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
            # Stage flow:
            #   Facility path:  start → facilities_shown → facility_doctors_shown → slots_shown
            #                   → patient_collection → confirm → booked
            #   Doctor search:  start → doctor_facilities_shown → slots_shown
            #                   → patient_collection → confirm → booked


            # ── Facility data ────────────────────────────────────────────
            "all_facilities_data": [],     # From GET /facilities
            "facility_doctors_data": [],   # Doctors at a selected facility

            # ── Doctor search data ───────────────────────────────────────
            "doctor_search_results": None, # {doctor, facilities} from name search



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
            "previous_patients": [],
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
# Doctor name extraction from user message
# ═══════════════════════════════════════════════════════════════════════════════

def _extract_doctor_name(msg: str) -> Optional[str]:
    """
    Try to extract a doctor name from the user's message.
    Returns the doctor name string if found, None otherwise.
    """
    msg_low = msg.strip().lower()

    # Pattern: "Dr. FirstName LastName" / "doctor FirstName LastName"
    patterns = [
        r'(?:dr\.?\s*|doctor\s+|doc\s+)([a-zA-Z]+(?:\s+[a-zA-Z]+){0,2})',
        r'(?:appointment\s+(?:with|for|of)\s+)([a-zA-Z]+(?:\s+[a-zA-Z]+){0,2})',
        r'(?:book\s+(?:with|for)\s+)([a-zA-Z]+(?:\s+[a-zA-Z]+){0,2})',
    ]

    for pattern in patterns:
        m = re.search(pattern, msg, re.IGNORECASE)
        if m:
            name = m.group(1).strip()
            # Filter out non-name words
            name_parts = [p for p in name.split() if p.lower() not in FORBIDDEN_NAMES and len(p) > 1]
            if name_parts:
                # Check there's at least one capitalized/real name word
                candidate = " ".join(name_parts)
                # Filter out common intent words
                skip_words = {"appointment", "book", "booking", "chahiye", "karvu", "karo",
                              "available", "slot", "time", "schedule", "today", "tomorrow",
                              "ki", "ka", "ke", "hai", "hee", "che", "no", "na"}
                if not all(w.lower() in skip_words for w in name_parts):
                    return candidate

    return None


# ═══════════════════════════════════════════════════════════════════════════════
# The Agent — Facility-First Flow
# ═══════════════════════════════════════════════════════════════════════════════

class AppointmentAgent:
    """
    Agentic booking assistant with facility-first flow.

    Three paths:
      1. Generic appointment → facilities → doctors → slots → book
      2. Doctor name search  → find doctor → facilities → slots → book
      3. Symptom-based       → analyze → search all facilities → doctors+facility → slots → book
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

        elif stage == "facilities_shown":
            return await self._handle_facility_selection_from_list(session_id, session, user_message)

        elif stage == "facility_doctors_shown":
            return await self._handle_facility_doctor_selection(session_id, session, user_message)


        elif stage == "doctor_search_results_shown":
            return await self._handle_doctor_search_selection(session_id, session, user_message)

        elif stage == "doctor_facilities_shown":
            return await self._handle_doctor_facility_selection(session_id, session, user_message)

        elif stage == "slots_shown":
            return await self._handle_slot_selection(session_id, session, user_message)

        elif stage == "patient_collection":
            return await self._handle_patient_info(session_id, session, user_message)

        elif stage == "confirm":
            return await self._handle_confirmation(session_id, session, user_message)

        elif stage == "booking_failed":
            return await self._handle_booking_failed(session_id, session, user_message)

        elif stage == "booking_failed_duplicate":
            return await self._handle_booking_failed_duplicate(session_id, session, user_message)

        elif stage == "booked":
            return await self._handle_booked(session_id, session, user_message)

        else:
            return await self._handle_start(session_id, session, user_message)

    # ══════════════════════════════════════════════════════════════════════════
    # STAGE: start — Detect intent and route
    # ══════════════════════════════════════════════════════════════════════════

    async def _handle_start(self, sid: str, session: Dict, msg: str) -> Dict:
        msg_low = msg.strip().lower()

        # Handle "new" command to restart
        if msg_low in ("new", "reset", "start over", "restart"):
            session_manager.reset(sid)
            return self._reply(sid, "Session reset! How can I help you book an appointment?")

        # Greetings
        greetings = {"hi", "hello", "hey", "namaste", "kem cho", "hii", "hiii",
                     "hyy", "hy", "helo", "hlo", "namaskar", "good morning",
                     "good afternoon", "good evening"}
        if msg_low.strip("!") in greetings or msg_low in greetings:
            return self._reply(
                sid,
                "Hello! I can help you with booking an appointment.\n\n"
                "How can I help you today?\n\n"
                "You can:\n"
                "1. **Search by doctor name** - _\"Dr. Dhruv Barot ki appointment\"_\n"
                "2. **Browse hospitals** - type **\"appointment\"** or **\"book\"**\n"
            )
        # ── Check for doctor name in message ─────────────────────────────
        doctor_name = _extract_doctor_name(msg)
        if doctor_name:
            return await self._handle_doctor_name_search(sid, session, doctor_name)

        # ── Generic appointment intent → show facilities ─────────────────
        booking_keywords = r'\b(book|appointment|appoint|doctor|visit|checkup|consult|opd|milvu|batavo|dekhado|chahiye|karvu|hospital|all doctors|all|show all|list all|badha doctors|badha)\b'
        if re.search(booking_keywords, msg_low):
            return await self._show_facilities(sid, session)

        # Nothing recognized — prompt
        return self._reply(
            sid,
            "I can help you book a doctor's appointment! 🏥\n\n"
            "You can:\n"
            "1️⃣ **Search by doctor name** — _\"Dr. Dhruv Barot\"_\n"
            "2️⃣ **Browse hospitals** — type **\"appointment\"** or **\"book\"**\n"
        )

    # ══════════════════════════════════════════════════════════════════════════
    # SCENARIO 1: Generic appointment → Show facilities
    # ══════════════════════════════════════════════════════════════════════════

    async def _show_facilities(self, sid: str, session: Dict, prefix: str = "") -> Dict:
        """Fetch and show all facilities (hospitals)."""
        response_text = prefix + "Fetching available hospitals…\n\n"

        raw = await mcp_get_facilities_list()
        data = json.loads(raw)

        if not data.get("success") or not data.get("facilities"):
            return self._reply(sid, "⚠️ No hospitals are available at the moment. Please try again later.")

        facilities = data["facilities"]
        session["all_facilities_data"] = facilities

        lines = ["🏥 **Available Hospitals / Facilities:**\n"]
        for fac in facilities:
            addr = fac.get("address", "")
            specs = ", ".join(fac.get("specializations", []))
            line = f"  {fac['index']}. **{fac['name']}**"
            if addr:
                line += f"\n     📍 {addr}"
            if specs and specs != "string":
                line += f"\n     🔬 {specs}"
            lines.append(line)

        lines.append("\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append("Which hospital would you like to visit? (Enter the number)")

        session["stage"] = "facilities_shown"
        response_text += "\n".join(lines)
        return self._reply(sid, response_text)

    # ══════════════════════════════════════════════════════════════════════════
    # STAGE: facilities_shown — User selects a facility → show doctors
    # ══════════════════════════════════════════════════════════════════════════

    async def _handle_facility_selection_from_list(self, sid: str, session: Dict, msg: str) -> Dict:
        """User selected a facility from the list. Show doctors at that facility."""
        facilities = session.get("all_facilities_data", [])
        msg_low = msg.strip().lower()

        # ── Handle yes/no responses (when bot asked "try another hospital?") ──
        if re.search(r'\b(yes|yeah|yup|ha|haa|haan|sure|ok|okay)\b', msg_low):
            return await self._show_facilities(sid, session, "No problem! Let me show you the hospitals again.\n\n")
        if re.search(r'\b(no|nahi|nope|cancel|naa|exit|quit)\b', msg_low):
            session_manager.reset(sid)
            return self._reply(
                sid,
                "No worries! Sorry we couldn't find what you were looking for. "
                "Feel free to come back anytime. Have a great day! 🙏"
            )

        # Try to parse number selection
        digit_match = re.search(r'^\s*(\d+)\s*$', msg)
        if digit_match:
            index = int(digit_match.group(1))
            selected = next((f for f in facilities if f["index"] == index), None)
        else:
            # Try to match by name
            selected = next(
                (f for f in facilities if msg_low in f.get("name", "").lower()),
                None
            )

        if not selected:
            return self._reply(
                sid,
                f"Please enter a valid number between 1 and {len(facilities)}, "
                f"or type **yes** to see the list again."
            )

        session["selected_facility"] = {
            "facilityId": selected["facilityId"],
            "name": selected["name"],
            "index": selected["index"],
        }
        logger.info(f"[{sid}] Facility selected: {selected['name']}")

        # Fetch doctors at this facility
        response_text = f"Fetching doctors at **{selected['name']}**…\n\n"

        raw = await mcp_get_doctors_list(facility_id=selected["facilityId"])
        data = json.loads(raw)

        if not data.get("success") or not data.get("doctors"):
            # Stay in facilities_shown stage so yes/no handling works
            session["stage"] = "facilities_shown"
            return self._reply(
                sid,
                f"⚠️ No doctors available at **{selected['name']}** right now.\n\n"
                f"Would you like to choose another hospital? (Yes/No)"
            )

        doctors = data["doctors"]
        session["facility_doctors_data"] = doctors

        lines = [f"👨‍⚕️ **Doctors at {selected['name']}:**\n"]
        for doc in doctors:
            lines.append(f"  {doc['index']}. **{doc['name']}**")

        lines.append("\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append("Which doctor would you like to book with? (Enter the number)")

        session["stage"] = "facility_doctors_shown"
        response_text += "\n".join(lines)
        return self._reply(sid, response_text)

    # ══════════════════════════════════════════════════════════════════════════
    # STAGE: facility_doctors_shown — User selects doctor at chosen facility
    # ══════════════════════════════════════════════════════════════════════════

    async def _handle_facility_doctor_selection(self, sid: str, session: Dict, msg: str) -> Dict:
        """User selected a doctor from the per-facility list. Fetch slots."""
        doctors = session.get("facility_doctors_data", [])
        msg_low = msg.strip().lower()

        # ── Handle yes/no responses (when bot asked "try another doctor/hospital?") ──
        if re.search(r'\b(yes|yeah|yup|ha|haa|haan|sure|ok|okay)\b', msg_low):
            return await self._show_facilities(sid, session, "No problem! Let me show you the hospitals again.\n\n")
        if re.search(r'\b(no|nahi|nope|cancel|naa|exit|quit)\b', msg_low):
            session_manager.reset(sid)
            return self._reply(
                sid,
                "No worries! Sorry we couldn't find a suitable doctor. "
                "Feel free to come back anytime. Have a great day! 🙏"
            )

        digit_match = re.search(r'^\s*(\d+)\s*$', msg)
        if digit_match:
            index = int(digit_match.group(1))
            selected = next((d for d in doctors if d["index"] == index), None)
        else:
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
        facility = session["selected_facility"]
        logger.info(f"[{sid}] Doctor selected: {selected['name']} at {facility['name']}")

        response_text = f"Fetching available slots for **{selected['name']}** at **{facility['name']}**…\n\n"
        return await self._fetch_and_show_slots(sid, session, response_text)

    # ══════════════════════════════════════════════════════════════════════════
    # SCENARIO 2: Doctor name search
    # ══════════════════════════════════════════════════════════════════════════

    async def _handle_doctor_name_search(self, sid: str, session: Dict, doctor_name: str) -> Dict:
        """Search for a doctor by name across all facilities."""
        response_text = f"🔍 Searching for **Dr. {doctor_name}** across all hospitals…\n\n"

        raw = await mcp_search_doctor_by_name(doctor_name=doctor_name)
        data = json.loads(raw)

        if not data.get("success") or data.get("doctorCount", 0) == 0:
            return self._reply(
                sid,
                f"⚠️ Could not find a doctor matching **\"{doctor_name}\"**.\n\n"
                "Would you like to:\n"
                "- Try a different name\n"
                "- Type **\"appointment\"** to browse hospitals"
            )

        doctors = data["doctors"]
        
        # Deduplicate by healthProfessionalId
        unique_docs_map = {}
        for d in doctors:
            if d["healthProfessionalId"] not in unique_docs_map:
                unique_docs_map[d["healthProfessionalId"]] = d
        unique_docs = list(unique_docs_map.values())
        for i, d in enumerate(unique_docs, 1):
            d["index"] = i
            
        session["doctor_search_results"] = unique_docs

        if len(unique_docs) > 1:
            # Show list of unique doctor names
            lines = [f"✅ Found **{len(unique_docs)} doctors** matching \"{doctor_name}\":\n"]
            for doc in unique_docs:
                lines.append(f"  {doc['index']}. **{doc['name']}**")
            
            lines.append("\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            lines.append("Which doctor would you like to book with? (Enter the number)")
            
            session["stage"] = "doctor_search_results_shown"
            response_text += "\n".join(lines)
            return self._reply(sid, response_text)
        else:
            # Exactly 1 doctor matched
            doctor = unique_docs[0]
            session["selected_doctor"] = {
                "healthProfessionalId": doctor["healthProfessionalId"],
                "name": doctor["name"],
                "index": 1,
            }
            logger.info(f"[{sid}] Single doctor match: {doctor['name']}")
            prefix = response_text + f"✅ Found **{doctor['name']}**.\n"
            
            # Now fetch facilities for this doctor
            return await self._check_doctor_facilities_and_proceed(
                sid, session, doctor["healthProfessionalId"], doctor["name"], prefix
            )

    # ══════════════════════════════════════════════════════════════════════════
    # STAGE: doctor_search_results_shown — User selects doctor+facility combo
    # ══════════════════════════════════════════════════════════════════════════

    async def _handle_doctor_search_selection(self, sid: str, session: Dict, msg: str) -> Dict:
        """User selected a doctor from the name search results. Now fetch their facilities."""
        doctors = session.get("doctor_search_results", [])
        msg_low = msg.strip().lower()

        # ── Handle yes/no responses (when bot asked "try another?") ──
        if re.search(r'\b(yes|yeah|yup|ha|haa|haan|sure|ok|okay)\b', msg_low):
            return await self._show_facilities(sid, session, "No problem! Let me show you the hospitals instead.\n\n")
        if re.search(r'\b(no|nahi|nope|cancel|naa|exit|quit)\b', msg_low):
            session_manager.reset(sid)
            return self._reply(
                sid,
                "No worries! Sorry we couldn't find what you were looking for. "
                "Feel free to come back anytime. Have a great day! 🙏"
            )

        digit_match = re.search(r'^\s*(\d+)\s*$', msg)
        if digit_match:
            index = int(digit_match.group(1))
            selected = next((d for d in doctors if d["index"] == index), None)
        else:
            selected = next(
                (d for d in doctors if msg_low in d.get("name", "").lower()),
                None
            )

        if not selected:
            return self._reply(
                sid,
                f"Please enter a valid number between 1 and {len(doctors)}, "
                f"or type **yes** to browse hospitals instead."
            )

        # Set doctor from the selection
        session["selected_doctor"] = {
            "healthProfessionalId": selected["healthProfessionalId"],
            "name": selected["name"],
            "index": selected["index"],
        }
        
        prefix = f"✅ Selected **{selected['name']}**.\n"
        return await self._check_doctor_facilities_and_proceed(
            sid, session, selected["healthProfessionalId"], selected["name"], prefix
        )

    # ══════════════════════════════════════════════════════════════════════════
    # STAGE: doctor_facilities_shown — Doctor found in multiple facilities
    # ══════════════════════════════════════════════════════════════════════════

    async def _handle_doctor_facility_selection(self, sid: str, session: Dict, msg: str) -> Dict:
        """Doctor works at multiple facilities, user selects one."""
        facilities = session.get("doctor_search_results_facilities", [])
        msg_low = msg.strip().lower()

        # ── Handle yes/no responses ──
        if re.search(r'\b(yes|yeah|yup|ha|haa|haan|sure|ok|okay)\b', msg_low):
            return await self._show_facilities(sid, session, "No problem! Let me show you all hospitals.\n\n")
        if re.search(r'\b(no|nahi|nope|cancel|naa|exit|quit)\b', msg_low):
            session_manager.reset(sid)
            return self._reply(
                sid,
                "No worries! Feel free to come back anytime. Have a great day! 🙏"
            )

        digit_match = re.search(r'^\s*(\d+)\s*$', msg)
        if digit_match:
            index = int(digit_match.group(1))
            selected = next((f for f in facilities if f["index"] == index), None)
        else:
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
        doctor = session["selected_doctor"]
        logger.info(f"[{sid}] Facility selected for Dr. {doctor['name']}: {selected['name']}")

        response_text = f"Fetching available slots for **Dr. {doctor['name']}** at **{selected['name']}**…\n\n"
        return await self._fetch_and_show_slots(sid, session, response_text)

    # ══════════════════════════════════════════════════════════════════════════
    # SCENARIO 3: SYMPTOM FLOW — AI-powered, searches ALL facilities
    # ══════════════════════════════════════════════════════════════════════════



    # ══════════════════════════════════════════════════════════════════════════
    # Fetch facilities for a selected doctor and route dynamically
    # ══════════════════════════════════════════════════════════════════════════

    async def _check_doctor_facilities_and_proceed(self, sid: str, session: Dict, hp_id: str, doctor_name: str, prefix_text: str) -> Dict:
        """Called when a single doctor is selected by name. Finds their actual facilities."""
        raw_facs = await mcp_get_doctor_facilities(health_professional_id=hp_id)
        data_facs = json.loads(raw_facs)
        
        if not data_facs.get("success") or data_facs.get("count", 0) == 0:
            return self._reply(
                sid, f"{prefix_text}\n⚠️ This doctor is not currently assigned to any available hospitals."
            )
            
        facilities = data_facs["facilities"]
        if len(facilities) > 1:
            session["doctor_search_results_facilities"] = facilities
            session["stage"] = "doctor_facilities_shown"
            
            lines = [f"{prefix_text}**Dr. {doctor_name}** works at **{len(facilities)} hospitals**. Please select one:\n"]
            for fac in facilities:
                lines.append(f"  {fac['index']}. 🏥 **{fac['name']}**")
                    
            lines.append("\n━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
            lines.append("Which hospital would you like to visit? (Enter the number)")
            return self._reply(sid, "\n".join(lines))
        else:
            # Doctor is at exactly 1 facility
            fac = facilities[0]
            session["selected_facility"] = {
                "facilityId": fac["facilityId"],
                "name": fac["name"],
                "index": 1,
            }
            prefix_text += f"🏥 Doctor is available at **{fac['name']}**.\nFetching available slots…\n\n"
            return await self._fetch_and_show_slots(sid, session, prefix_text)

    # ══════════════════════════════════════════════════════════════════════════
    # Helper: Fetch availability and display slots
    # ══════════════════════════════════════════════════════════════════════════

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
            # Go back to facility_doctors_shown so yes/no handling works
            session["stage"] = "facility_doctors_shown"
            return self._reply(
                sid,
                f"{prefix}⚠️ No available slots found for **{doctor['name']}** at **{facility['name']}**.\n\n"
                f"Would you like to choose another hospital or doctor? (Yes/No)"
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
        msg_low = msg.strip().lower()

        # ── Handle yes/no responses (when bot asked "try different slot/doctor?") ──
        if re.search(r'\b(yes|yeah|yup|ha|haa|haan|sure|ok|okay)\b', msg_low):
            return await self._show_facilities(sid, session, "No problem! Let me show you the hospitals again.\n\n")
        if re.search(r'\b(no|nahi|nope|cancel|naa|exit|quit)\b', msg_low):
            session_manager.reset(sid)
            return self._reply(
                sid,
                "No worries! Sorry we couldn't find a suitable slot. "
                "Feel free to come back anytime. Have a great day! 🙏"
            )

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
            session["stage"] = "confirm"
            return self._show_confirmation(sid, session)
        else:
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
            # Ensure the user doesn't use the exact same patient details again
            current = session["patient"]
            for prev in session.get("previous_patients", []):
                if (current.get("firstName") == prev.get("firstName") and
                    current.get("lastName") == prev.get("lastName") and
                    current.get("mobile") == prev.get("mobile") and
                    current.get("birthDate") == prev.get("birthDate")):
                    
                    # Clear the duplicated details and ask for new ones
                    session["patient"] = {
                        "firstName": None, "lastName": None, "mobile": None, 
                        "gender": None, "birthDate": None, "address": None, 
                        "pinCode": None, "area": None
                    }
                    return self._reply(
                        sid, 
                        f"⚠️ An appointment is already booked for **{current.get('firstName')} {current.get('lastName')}** ({current.get('mobile')}).\n\n"
                        "If you wish to book another appointment, it must be for a different **family member**.\n"
                        "Please provide their new details (Name, Mobile, DOB, etc)."
                    )

            # All fields collected and valid → go to confirmation
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
                symptoms=session.get("symptoms", ["General consultation"]),
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
                    "Please arrive 10 minutes early. See you there! 🙏\n\n"
                    "Would you like to book another appointment? (Yes/No)"
                )
                return self._reply(sid, success_msg, booked=True)
            else:
                error = data.get("error", "Unknown error")
                error_msg = str(error)
                try:
                    if isinstance(error, str):
                        match = re.search(r'\[.*\]|\{.*\}', error)
                        if match:
                            parsed = json.loads(match.group(0))
                            if isinstance(parsed, list):
                                error_msg = " ".join([str(e.get("message", e)) if isinstance(e, dict) else str(e) for e in parsed])
                            elif isinstance(parsed, dict):
                                error_msg = str(parsed.get("message", parsed))
                        else:
                            error_msg = re.sub(r'^HTTP \d+:?\s*', '', error).strip()
                    elif isinstance(error, list):
                        error_msg = " ".join([str(e.get("message", e)) if isinstance(e, dict) else str(e) for e in error])
                except Exception:
                    pass

                error_msg_lower = error_msg.lower()
                is_duplicate = "already have an appointment" in error_msg_lower or "a1_appointment_008" in error_msg_lower

                if is_duplicate:
                    session["stage"] = "booking_failed_duplicate"
                    reply_text = (
                        f"❌ Booking failed: {error_msg}\n\n"
                        "You have already booked an appointment. You **cannot** book another appointment for yourself using the same Name, DOB, and Mobile number.\n\n"
                        "If you need to book an appointment for a **family member**, you can proceed. "
                        "Would you like to start a booking for a family member? (Yes/No)"
                    )
                else:
                    session["stage"] = "booking_failed"
                    reply_text = (
                        f"❌ Booking failed: {error_msg}\n\n"
                        "Would you like to try a different time slot? (Yes/No)"
                    )

                return self._reply(sid, reply_text)

        except Exception as e:
            logger.error(f"[{sid}] Booking error: {e}", exc_info=True)
            session["stage"] = "booking_failed"
            return self._reply(
                sid,
                f"❌ Something went wrong: {e}\n\nWould you like to try a different time slot? (Yes/No)"
            )

    # ══════════════════════════════════════════════════════════════════════════
    # STAGE: booking_failed — Handle user response after error
    # ══════════════════════════════════════════════════════════════════════════

    async def _handle_booking_failed(self, sid: str, session: Dict, msg: str) -> Dict:
        msg_low = msg.strip().lower()

        if re.search(r'\b(yes|yeah|yup|ha|haa|haan|sure|ok|okay|retry|different|slot)\b', msg_low):
            doctor = session.get("selected_doctor", {})
            facility = session.get("selected_facility", {})
            prefix = f"Let's try picking a different time slot for **{doctor.get('name')}** at **{facility.get('name')}**...\n\n"
            return await self._fetch_and_show_slots(sid, session, prefix)

        if re.search(r'\b(no|nahi|nope|cancel|naa|exit|quit)\b', msg_low):
            session_manager.reset(sid)
            return self._reply(sid, "Booking cancelled. How else can I help you today?")

        return self._reply(
            sid,
            "Please type **Yes** to try a different time slot, or **No** to cancel."
        )

    # ══════════════════════════════════════════════════════════════════════════
    # STAGE: booking_failed_duplicate — Handle duplicate error response
    # ══════════════════════════════════════════════════════════════════════════

    async def _handle_booking_failed_duplicate(self, sid: str, session: Dict, msg: str) -> Dict:
        msg_low = msg.strip().lower()

        if re.search(r'\b(yes|yeah|yup|ha|haa|haan|sure|ok|okay|family|proceed|start|new)\b', msg_low):
            # Save the failing patient to previous_patients list so we reject it later if they try again
            past_patients = session.get("previous_patients", [])
            current_patient = session.get("patient", {}).copy()
            if current_patient and current_patient not in past_patients:
                past_patients.append(current_patient)

            session_manager.reset(sid)
            new_session = session_manager.get(sid)
            new_session["previous_patients"] = past_patients
            
            return self._reply(
                sid,
                "Great! Let's book a new appointment for your family member.\n"
                "Please remember to provide their distinct details (Name, Mobile, DOB) when asked.\n\n"
                "I can help you book a doctor's appointment! 🏥\n\n"
                "You can:\n"
                "1️⃣ **Search by doctor name** — _\"Dr. Dhruv Barot\"_\n"
                "2️⃣ **Browse hospitals** — type **\"appointment\"** or **\"book\"**\n"
            )

        if re.search(r'\b(no|nahi|nope|cancel|naa|exit|quit)\b', msg_low):
            session_manager.reset(sid)
            return self._reply(sid, "Booking cancelled. How else can I help you today?")

        return self._reply(
            sid,
            "Please type **Yes** to book for a family member, or **No** to cancel."
        )

    # ══════════════════════════════════════════════════════════════════════════
    # STAGE: booked — Appointment already booked, handle rebooking
    # ══════════════════════════════════════════════════════════════════════════

    async def _handle_booked(self, sid: str, session: Dict, msg: str) -> Dict:
        """Handle user input after a successful booking. Allow rebooking."""
        msg_low = msg.strip().lower()

        # User wants to book another appointment
        book_again = (
            re.search(r'\b(yes|yeah|yup|ha|haa|haan|sure|ok|okay|new|reset|book|appointment|another)\b', msg_low)
        )
        if book_again:
            # Save the booked patient to previous_patients list
            past_patients = session.get("previous_patients", [])
            current_patient = session.get("patient", {}).copy()
            if current_patient and current_patient not in past_patients:
                past_patients.append(current_patient)

            session_manager.reset(sid)
            new_session = session_manager.get(sid)
            new_session["previous_patients"] = past_patients
            
            logger.info(f"[{sid}] Rebooking — reset session, enforcing different patient details")
            return await self._show_facilities(
                sid, new_session,
                "✅ Your appointment is already booked.\n\n"
                "If you want to book an appointment for a **family member**, we can proceed! "
                "However, you cannot use the same Name, Mobile, and DOB as before.\n\n"
                "Let's start by selecting a hospital for them:\n\n"
            )

        # User doesn't want to book again
        if re.search(r'\b(no|nahi|nope|cancel|naa|exit|quit|bye|thanks|thank)\b', msg_low):
            session_manager.reset(sid)
            return self._reply(
                sid,
                "Thank you for using AarogyaOne! 🙏\n\n"
                "Have a great day and take care! Feel free to come back anytime."
            )

        # Unrecognized input — prompt clearly
        return self._reply(
            sid,
            "Your appointment is already booked! ✅\n\n"
            "Would you like to book another appointment? (Yes/No)"
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