"""
Medical Appointment Booking Agent
LLM-powered info extraction + Dynamic booking via Aarogya HMIS API

Fixes applied:
  1. NO auto-select: doctor/slot only stored when user explicitly picks by number
  2. Doctor list is always fetched dynamically from /doctors/availability
  3. Only user-chosen values (by index) are stored in session
  4. appointentDateTime sent to API = user-selected time MINUS 5h30m (IST→UTC)
"""
import json
import re
import logging
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timedelta

import httpx

from app.api.doctors_cache import doctors_cache
from app.api.external_client import aarogya_api
from app.models.schemas import (
    AppointmentScheduleRequest,
    Patient,
    AppointmentDetail,
    BirthDateComponent,
    PermanentAddress,
    PatientDetail,
    CollectedInfo,
)
from app.config.settings import settings

logger = logging.getLogger(__name__)


# ── Symptom → Department ──────────────────────────────────────────────────────

SYMPTOM_DEPARTMENT_MAP = {
    "chest pain": ["Cardiology"],
    "heart":      ["Cardiology"],
    "headache":   ["Neurology"],
    "migraine":   ["Neurology"],
    "fever":      ["Medicine", "General"],
    "cough":      ["Medicine", "General"],
    "cold":       ["Medicine", "General"],
    "pain":       ["Medicine", "Orthopaedics"],
    "nausea":     ["Medicine", "Gastroenterology"],
    "vomiting":   ["Medicine", "Gastroenterology"],
    "skin":       ["Dermatology"],
    "rash":       ["Dermatology"],
}

FORBIDDEN_NAMES = {
    "i", "me", "my", "mine", "myself",
    "hu", "mane", "hun", "ame", "mara", "maru", "mujhe", "muze", "mera", "meri",
    "and", "the", "a", "an",
    "dr", "mrs", "mr", "ms", "doc", "doctor",  # Titles and honorifics
}


def filter_doctors_by_symptoms(doctors: List[Dict], symptoms: List[str]) -> List[Dict]:
    if not symptoms:
        return doctors
    relevant_depts: set = set()
    for symptom in symptoms:
        for key, depts in SYMPTOM_DEPARTMENT_MAP.items():
            if key in symptom.lower():
                relevant_depts.update(d.lower() for d in depts)
    if not relevant_depts:
        return doctors
    filtered = [
        d for d in doctors
        if any(dep in d.get("department", "").lower() for dep in relevant_depts)
    ]
    return filtered or doctors


# ── Doctor display ────────────────────────────────────────────────────────────

def format_doctors_for_display(doctors: List[Dict]) -> str:
    """Format doctor(s) and slots for display showing all dates and from-to times with numbering."""
    if not doctors:
        return "(No doctors available)"

    # Group doctors by name
    grouped: Dict[str, Dict] = {}
    for d in doctors:
        name = d.get("healthProfessionalName", "Dr. Unknown")
        dept = d.get("department", "General")
        key = f"{name} ({dept})"
        if key not in grouped:
            grouped[key] = {"name": name, "dept": dept, "entries": []}
        grouped[key]["entries"].append(d)

    lines = []
    # Build a list of all available slots with sequential global indices
    all_slots = []
    for d in doctors:
        all_slots.extend(_flatten_available_slots(d))

    # Helper to find global index while iterating grouping
    def get_index(s: Dict) -> int:
        try:
            return all_slots.index(s) + 1
        except ValueError:
            return 0

    for key, info in grouped.items():
        lines.append(f"👨‍⚕️ **Doctor: {info['name']}** ({info['dept']})")
        for entry in info["entries"]:
            date = str(entry.get("appointmentDate", "???"))
            lines.append(f"  📅 Date: {date}")
            
            slots = _flatten_available_slots(entry)
            if slots:
                for slot in slots:
                    idx = get_index(slot)
                    s_from = str(slot.get("from") or slot.get("startTime") or "??")
                    s_to = str(slot.get("to") or slot.get("endTime") or "??")
                    lines.append(f"    {idx}. {s_from} - {s_to}")
            else:
                lines.append("    (No available slots)")
        lines.append("")

    return "\n".join(lines).rstrip()


# ── Slot helpers ──────────────────────────────────────────────────────────────

def _flatten_available_slots(doc: Dict) -> List[Dict]:
    """Return a flat list of all available slots for a doctor entry."""
    slots: List[Dict] = []
    for sched in doc.get("schedule", []):
        for s in sched.get("slots", []):
            avail = s.get("isAvailable")
            if avail is True or str(avail).lower() == "true":
                slots.append(s)
    return slots


def _slot_external_id(slot: Dict, doc: Dict) -> str:
    """
    Return the best available external ID for a slot.
    Tries: externalId → slotId → id → synthetic fallback.
    """
    ext = slot.get("externalId") or slot.get("slotId") or slot.get("id")
    if ext:
        return str(ext)
    s_from = (slot.get("from") or slot.get("startTime") or "").replace(":", "")
    s_to   = (slot.get("to")   or slot.get("endTime")   or "").replace(":", "")
    return f"slot_{s_from}_{s_to}"


# ── Context / missing fields ──────────────────────────────────────────────────

REQUIRED_PATIENT_FIELDS = [
    "firstName", "lastName",
    "mobile", "gender", "address",
]


def missing_patient_fields(collected: Dict) -> List[str]:
    missing: List[str] = []

    for k in REQUIRED_PATIENT_FIELDS:
        val = collected.get(k)
        if not val or str(val).strip() in ("", "null"):
            missing.append(k)
            continue
        if k in ("firstName", "lastName"):
            v = str(val).strip()
            if v.lower() in FORBIDDEN_NAMES or len(v) < 2:
                missing.append(k)
        if k == "mobile":
            if not re.match(r"^\d{10}$", str(val).replace(" ", "")):
                missing.append(k)

    # Need either birthDate OR age
    if not collected.get("birthDate") and not collected.get("age"):
        missing.append("dateOfBirth (DD/MM/YYYY) or age")

    return missing


def build_context_summary(collected: Dict) -> str:
    filled = {k: v for k, v in collected.items() if v is not None and str(v).strip() not in ("", "[]")}
    return "\n".join(f"  {k}: {v}" for k, v in filled.items()) if filled else "Nothing collected yet."


# ── Session Manager ───────────────────────────────────────────────────────────

class SessionManager:
    def __init__(self):
        self._sessions: Dict[str, Dict] = {}

    def get(self, sid: str) -> Dict:
        if sid not in self._sessions:
            self._sessions[sid] = {
                "messages":  [],
                "collected": CollectedInfo().model_dump(),
                "doctors":   [],
                # stages: start → doctors_shown → doctor_chosen → selected → booking
                "stage":     "start",
                "booked":    False,
            }
        return self._sessions[sid]

    def add_message(self, sid: str, role: str, content: str):
        self.get(sid)["messages"].append({"role": role, "content": content})

    def messages(self, sid: str) -> List[Dict]:
        return self.get(sid)["messages"]

    def update_collected(self, sid: str, updates: Dict):
        collected = self.get(sid)["collected"]
        for k, v in updates.items():
            if v is None or v == "" or v == []:
                continue
            collected[k] = v

    def collected(self, sid: str) -> Dict:
        return self.get(sid)["collected"]

    def reset(self, sid: str):
        self._sessions.pop(sid, None)


session_manager = SessionManager()


# ── Prompts ───────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a Medical Appointment Booking Agent for Aarogya hospital.
Your ONLY job is to display available time slots and collect patient details.

## IF SHOWING SLOTS:
The system provides doctor details and available time slots below.
You MUST copy this list EXACTLY — no reformatting.
Then ask user to select by typing numbers.

## HOW TO RESPOND:

{selection_instruction}

## CRITICAL RULES:
- Show the date/slot list EXACTLY as provided — never reformat.
- Do NOT suggest or recommend a time slot.
- Do NOT auto-select anything.
- Keep your response brief and clear.

## CURRENT SESSION STATE:
{context}

## MISSING PATIENT FIELDS:
{missing}

## SLOTS TO SHOW:
{doctor_list}

{selection_instruction}

## WHEN ALL PATIENT DETAILS COLLECTED:
Return this EXACT format at the very end:
"Perfect! I'm booking your appointment:
- Doctor: [doctor name]
- Date: [date]
- Time: [time]
- Patient Name: [firstName middleName lastName]
- Contact Number: [mobile]
- Date of Birth: [birthDate]
- Address: [address]
- Pin Code: [pinCode]

Processing complete!"
"""

EXTRACTION_PROMPT = """You are a strict JSON extraction assistant focused on PATIENT data only.

Extract information from user text below.
Return ONLY a valid JSON object — no markdown, no explanation, nothing else.

Fields to extract:
{{
  "firstName":  string or null,
  "middleName": string or null,
  "lastName":   string or null,
  "mobile":     "10-digit string" or null,
  "gender":     "Male" or "Female" or null,
  "birthDate":  "YYYY-MM-DD" or null,
  "age":        integer or null,
  "pinCode":    "6-digit string" or null,
  "address":    string or null,
  "area":       string or null,
  "symptoms":   ["symptom1", "symptom2"] or null,
  "doctor_name": string or null,
  "appointment_date": "YYYY-MM-DD" or null,
  "appointment_time": "HH:MM" or null
}}

CRITICAL RULES:

⚠️  PATIENT vs DOCTOR SEPARATION:
  - If user mentions ONLY a doctor name (e.g., "I want to see Dr. Dhruv Barot"):
    → Extract ONLY doctor_name="Dr. Dhruv Barot"
    → Set firstName, middleName, lastName, mobile, etc. to NULL
  - If user gives PATIENT details (e.g., "My name is Rahul Patel"):
    → Extract firstName, middleName, lastName from PATIENT info ONLY
    → NEVER extract doctor titles (Dr, Mrs, Mr) as patient names
    → NEVER extract doctor names into patient fields

⚠️  NAME EXTRACTION:
  - NEVER include titles: "Dr", "Mrs", "Mr", "Ms", "Doctor"
  - NEVER extract pronouns: "I", "me", "my", "hu", "mane", "mera"
  - If text is "Dr. Dhruv Barot" alone → doctor_name ONLY, patient names = null
  - If text is "I am Rahul Sharma" → firstName="Rahul", lastName="Sharma" (NOT "I")

⚠️  OTHER RULES:
  - Extract the exact requested appointment date/time. E.g., "18:00" -> appointment_time: "18:00"
  - For symptoms: extract ONLY health complaints (e.g., "chest pain", "fever")
  - mobile: exactly 10 digits, no spaces/dashes
  - gender: only "Male" or "Female" or null
  - birthDate: YYYY-MM-DD or null
  - pinCode: exactly 6 digits or null

Name parsing (for patient names ONLY):
  - 1 word → firstName
  - 2 words → firstName + lastName
  - 3 words → firstName + middleName + lastName  
  - 4+ words → firstName + middleName + (rest as lastName)

Return ONLY the JSON object, nothing else.

Current year: {year}

User text:
\"\"\"{text}\"\"\"
"""


# ── IST → UTC conversion ──────────────────────────────────────────────────────

IST_OFFSET = timedelta(hours=5, minutes=30)


def ist_to_utc(dt: datetime) -> datetime:
    """Convert an IST datetime to UTC by subtracting 5 h 30 m."""
    return dt - IST_OFFSET


# ── The Agent ─────────────────────────────────────────────────────────────────

class AppointmentAgent:

    # ── LLM call ─────────────────────────────────────────────────────────────

    async def _llm(self, messages: List[Dict], system: str) -> str:
        payload = {
            "model":   settings.LLM_MODEL,
            "messages": [{"role": "system", "content": system}] + messages,
            "stream":  False,
            "options": {
                "temperature": settings.LLM_TEMPERATURE,
                "num_predict": settings.LLM_MAX_TOKENS,
            },
        }
        try:
            timeout = httpx.Timeout(settings.OLLAMA_TIMEOUT, connect=10)
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.post(f"{settings.OLLAMA_BASE_URL}/api/chat", json=payload)
                r.raise_for_status()
                return r.json()["message"]["content"]
        except httpx.ConnectError:
            return "⚠️ Cannot connect to Ollama. Please ensure Ollama is running."
        except httpx.ReadTimeout:
            return "⚠️ Ollama timed out. Please try again."
        except Exception as e:
            logger.error(f"LLM error: {e}", exc_info=True)
            return f"Sorry, something went wrong. Error: {e}"
        return "Sorry, something went wrong."

    # ── Patient info extractor ────────────────────────────────────────────────

    async def _extract_patient_info(self, text: str, dr_name: str = "") -> Dict:
        """
        Use LLM to extract patient data from any natural language input.
        Works for mixed Gujarati / Hindi / English.
        Does NOT extract doctor/slot selection — that is handled by explicit index parsing.
        """
        prompt = (
            EXTRACTION_PROMPT
            .replace("{year}", str(datetime.now().year))
            .replace("{text}", text)
        )
        payload = {
            "model":   settings.LLM_MODEL,
            "messages": [
                {"role": "system", "content": "Return ONLY valid JSON. No markdown. No explanation."},
                {"role": "user",   "content": prompt},
            ],
            "stream":  False,
            "options": {"temperature": 0.0, "num_predict": 512},
        }
        try:
            timeout = httpx.Timeout(settings.OLLAMA_TIMEOUT, connect=10)
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.post(f"{settings.OLLAMA_BASE_URL}/api/chat", json=payload)
                r.raise_for_status()
                raw = r.json()["message"]["content"].strip()

            data = self._parse_json_safe(raw)
            if not data:
                return {}

            logger.info(f"[Extraction] raw result: {data}")
            data = self._clean_extracted_data(data, dr_name)
            return data
        except httpx.ConnectError:
            logger.error("[Extraction] Cannot connect to Ollama.")
            return {}
        except httpx.ReadTimeout:
            logger.error("[Extraction] Ollama timed out.")
            return {}
        except Exception as e:
            logger.error(f"[Extraction] Failed: {e}", exc_info=True)
            return {}

    @staticmethod
    def _parse_json_safe(text: str) -> Dict:
        """Try multiple strategies to extract a JSON object from LLM output."""
        # 1. Direct parse
        try:
            return json.loads(text)
        except Exception:
            pass
        # 2. Balanced-braces extraction
        start = text.find("{")
        if start != -1:
            depth = 0
            for i in range(start, len(text)):
                if text[i] == "{":
                    depth += 1
                elif text[i] == "}":
                    depth -= 1
                    if depth == 0:
                        try:
                            return json.loads(text[start: i + 1])
                        except Exception:
                            break
        # 3. Non-greedy regex
        m = re.search(r"\{.*?\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except Exception:
                pass
        logger.error(f"[Extraction] Could not parse JSON: {text[:200]}")
        return {}

    @staticmethod
    def _clean_extracted_data(data: Dict, locked_dr_name: str = "") -> Dict:
        """Validate and clean extracted fields."""
        dr_name_text = str(data.get("doctor_name", "")).lower().replace("dr.", "").strip()
        locked_dr_low = locked_dr_name.lower().replace("dr.", "").strip()

        # Clean patient name fields
        for field in ("firstName", "middleName", "lastName"):
            val = data.get(field)
            if val:
                v = str(val).strip()
                v_low = v.lower()
                # If name matches selected doctor or is too generic or matches "doctor_name" extracted, ignore it
                if len(v) < 2 or v_low in FORBIDDEN_NAMES:
                    data[field] = None
                elif locked_dr_low and (locked_dr_low in v_low) and len(v) > 3:
                    data[field] = None  # doctor name leaked into patient field
                elif dr_name_text and (dr_name_text in v_low) and len(v) > 3:
                    data[field] = None  # doctor name leaked into patient field
                elif v_low in ("dr", "mrs", "mr", "ms", "doc", "doctor"):
                    data[field] = None  # Title, not a patient name

        # Normalise gender to int
        g = data.get("gender")
        if g:
            gs = str(g).lower()
            if "female" in gs or "stri" in gs:
                data["gender"] = 2
            elif "male" in gs or "purush" in gs:
                data["gender"] = 1
            else:
                try:
                    data["gender"] = int(g) if int(g) in (1, 2) else None
                except Exception:
                    data["gender"] = None

        return data

    # ── Booking ───────────────────────────────────────────────────────────────

    async def _book_appointment(
        self, session_id: str, session: Dict, collected: Dict
    ) -> Tuple[bool, Optional[Dict], str]:
        """
        Build the HMIS booking request from collected data and call the API.
        The appointment datetime sent to the API = user-selected IST time − 5 h 30 m (UTC).
        """
        from app.api.hmis_service import hmis_service

        try:
            # ── Resolve birth_date ────────────────────────────────────────────
            birth_date_str = collected.get("birthDate")
            age            = collected.get("age")

            if birth_date_str:
                birth_date = datetime.strptime(str(birth_date_str), "%Y-%m-%d")
            elif age:
                birth_date = datetime(datetime.now().year - int(age), 1, 1)
            else:
                return False, None, "\n❌ Booking failed: date of birth or age is required."

            # ── Resolve appointment datetime (IST) ────────────────────────────
            appt_date_str = collected.get("appointment_date", "")
            appt_time_str = collected.get("appointment_time", "00:00")

            if not appt_date_str:
                return False, None, "\n❌ Booking failed: appointment date not set."

            appt_dt_ist = datetime.strptime(
                f"{appt_date_str} {appt_time_str}", "%Y-%m-%d %H:%M"
            )

            # ── Convert IST → UTC (subtract 5 h 30 m) ────────────────────────
            appt_dt_utc = ist_to_utc(appt_dt_ist)
            logger.info(
                f"[Booking] IST: {appt_dt_ist.isoformat()}  →  UTC: {appt_dt_utc.isoformat()}"
            )

            # ── Gender default ────────────────────────────────────────────────
            gender_val = collected.get("gender")
            if gender_val is None:
                gender_val = 1
            gender_int = int(gender_val)

            # ── Chief complaints from symptoms ────────────────────────────────
            symptoms = collected.get("symptoms") or []
            if isinstance(symptoms, str):
                symptoms = [symptoms]
            chief_complaints = symptoms if symptoms else ["General consultation"]

            result = await hmis_service.schedule_appointment(
                first_name              = str(collected.get("firstName", "")),
                middle_name             = str(collected.get("middleName", "") or ""),
                last_name               = str(collected.get("lastName", "")),
                mobile                  = str(collected.get("mobile", "")),
                gender                  = gender_int,
                birth_date              = birth_date,
                health_professional_id  = str(collected.get("health_professional_id", "")),
                facility_id             = str(collected.get("facility_id", "")),
                chief_complaints        = chief_complaints,
                appointment_date_time   = appt_dt_utc,   # ← UTC time sent to API
                pin_code                = str(collected.get("pinCode", "") or ""),
                address                 = str(collected.get("address", "") or ""),
                area                    = str(collected.get("area", "") or ""),
                external_id             = str(collected.get("slot_external_id", "") or ""),
            )

            if result.get("success"):
                session["booked"] = True
                slot_disp = collected.get("slot_display", appt_time_str)
                msg = (
                    f"\n\n✅ **Appointment booked successfully!**\n"
                    f"  Doctor   : Dr. {collected.get('doctor_name', '')}\n"
                    f"  Date     : {appt_date_str}\n"
                    f"  Slot     : {slot_disp} (IST)\n"
                    f"  Patient  : {collected.get('firstName', '')} {collected.get('lastName', '')}\n"
                    f"  Ref data : {result.get('data', '')}"
                )
                return True, result.get("data"), msg
            else:
                err = result.get("error", "Unknown error")
                return False, None, f"\n\n❌ Booking failed: {err}"

        except Exception as e:
            logger.error(f"[Booking] Error: {e}", exc_info=True)
            return False, None, f"\n\n❌ Booking error: {e}"

    # ── Main chat handler ──────────────────────────────────────────────────────

    async def chat(self, session_id: str, user_message: str) -> Dict[str, Any]:
        session   = session_manager.get(session_id)
        collected = session_manager.collected(session_id)
        logger.info(f"[{session_id}] stage={session['stage']} | user: {user_message[:120]}")

        # ── STEP 1: Extract patient info from user message ────────────────────
        # This covers names, mobile, gender, dob, address etc.
        # It does NOT handle doctor/slot selection — that's done by explicit index below.
        dr_name_locked = collected.get("doctor_name") or ""
        extracted = await self._extract_patient_info(user_message, dr_name_locked)
        clean = {k: v for k, v in extracted.items() if v is not None and str(v).strip() != ""}
        if clean:
            session_manager.update_collected(session_id, clean)
            logger.info(f"[Extraction] stored: {clean}")

        # ── STEP 2: Fetch latest doctor list (from cache, refreshed every 15 min) ──
        all_docs = await doctors_cache.get_doctors()

        # ── STEP 3: If no doctors in session yet, filter by symptoms/doctor name ──
        if session["stage"] in ("start", "doctors_shown") and not session.get("_doctor_slot_locked"):
            collected_now = session_manager.collected(session_id)
            dr_name   = collected_now.get("doctor_name")
            symptoms  = collected_now.get("symptoms") or []

            filtered: List[Dict] = []

            if dr_name:
                name_low  = str(dr_name).lower().replace("dr.", "").strip()
                filtered  = [
                    d for d in all_docs
                    if name_low in (d.get("healthProfessionalName") or "").lower()
                ]
                logger.info(f"[Filter] by doctor name '{dr_name}': {len(filtered)} entries")

            if not filtered and symptoms:
                syms = symptoms if isinstance(symptoms, list) else [str(symptoms)]
                filtered = filter_doctors_by_symptoms(all_docs, syms)
                logger.info(f"[Filter] by symptoms {syms}: {len(filtered)} entries")

            if not filtered:
                # Show all doctors if no filter matched
                filtered = all_docs

            if filtered:
                session["doctors"] = filtered
                if session["stage"] == "start":
                    session["stage"] = "doctors_shown"

        # ── STEP 4: Doctor / Slot matching ────
        if not session.get("_doctor_slot_locked"):
            collected_now = session_manager.collected(session_id)
            ext_dr_name   = collected_now.get("doctor_name")
            ext_appt_date = collected_now.get("appointment_date")
            ext_appt_time = collected_now.get("appointment_time")
            
            # ── 4a. Check if user typed a NUMBER for selection ────────────────
            digit_match = re.search(r"\b(\d+)\b", user_message)
            if digit_match:
                index = int(digit_match.group(1)) - 1
                all_slots: List[Tuple[Dict, Dict]] = [] # (slot, doc_entry)
                for d in session.get("doctors", []):
                    for s in _flatten_available_slots(d):
                        all_slots.append((s, d))
                
                if 0 <= index < len(all_slots):
                    matched_slot, matched_doc = all_slots[index]
                    ext_id = _slot_external_id(matched_slot, matched_doc)
                    exact_time = matched_slot.get("from") or matched_slot.get("startTime") or ""
                    
                    session_manager.update_collected(session_id, {
                        "health_professional_id": matched_doc.get("healthProfessionalId", ""),
                        "doctor_name":            matched_doc.get("healthProfessionalName", ""),
                        "facility_id":            (
                            matched_doc.get("facilityId")
                            or (matched_doc.get("facility") or {}).get("id")
                            or settings.DEFAULT_FACILITY_ID
                            or ""
                        ),
                        "appointment_date": matched_doc.get("appointmentDate", ""),
                        "appointment_time": exact_time,
                        "slot_external_id": ext_id,
                        "slot_display": f"{exact_time} – {matched_slot.get('to') or matched_slot.get('endTime') or ''}",
                    })
                    session["stage"] = "selected"
                    session["_doctor_slot_locked"] = True
                    logger.info(f"[Selection] Picked index {index+1}: {ext_id} at {exact_time}")
            
            # ── 4b. If no index match, try matching by LLM-extracted name/time ─
            if not session.get("_doctor_slot_locked"):
                target_doc = None
                if len(session.get("doctors", [])) == 1:
                    target_doc = session["doctors"][0]
                elif ext_dr_name:
                    name_low = str(ext_dr_name).lower().replace("dr.", "").strip()
                    for d in session.get("doctors", []):
                        if name_low in (d.get("healthProfessionalName") or "").lower():
                            target_doc = d
                            break

                if target_doc:
                    session["doctors"] = [target_doc]
                    if ext_appt_time:
                        matched_slot = None
                        for s in _flatten_available_slots(target_doc):
                            s_from = s.get("from") or s.get("startTime") or ""
                            if s_from and s_from.startswith(ext_appt_time):
                                matched_slot = s
                                break
                        
                        if matched_slot and target_doc:
                            # Use guaranteed dict variable to satisfy Pyre
                            doc: Dict = target_doc
                            slot: Dict = matched_slot
                            
                            ext_id = _slot_external_id(slot, doc)
                            exact_time = str(slot.get("from") or slot.get("startTime") or "")
                            s_to = str(slot.get("to") or slot.get("endTime") or "")
                            
                            session_manager.update_collected(session_id, {
                                "health_professional_id": str(doc.get("healthProfessionalId") or ""),
                                "doctor_name":            str(doc.get("healthProfessionalName") or ""),
                                "facility_id":            str(
                                    doc.get("facilityId")
                                    or (doc.get("facility") or {}).get("id")
                                    or settings.DEFAULT_FACILITY_ID
                                    or ""
                                ),
                                "appointment_date": str(doc.get("appointmentDate") or ""),
                                "appointment_time": exact_time,
                                "slot_external_id": ext_id,
                                "slot_display": f"{exact_time} – {s_to}",
                            })
                            session["stage"] = "selected"
                            session["_doctor_slot_locked"] = True
                            logger.info(f"[Selection] Natural match: {ext_id} at {exact_time}")
                        else:
                            # Time provided but no match — clear any stale selection
                            session_manager.update_collected(session_id, {
                                "slot_external_id": None,
                                "appointment_time": ext_appt_time, # Keep what user said for UI
                            })
                    else:
                        session["stage"] = "doctor_chosen"

        # ── STEP 5: Add user message to conversation history ──────────────────
        session_manager.add_message(session_id, "user", user_message)

        # ── STEP 6: Check if ready to book ───────────────────────────────────
        collected       = session_manager.collected(session_id)
        doctor_selected = bool(collected.get("health_professional_id"))
        slot_selected   = bool(collected.get("slot_external_id"))
        missing_fields  = missing_patient_fields(collected)

        booking_complete = False
        booking_details  = None
        booking_msg      = ""

        if doctor_selected and slot_selected and not missing_fields:
            logger.info("✅ All fields present — triggering booking!")
            booking_complete, booking_details, booking_msg = await self._book_appointment(
                session_id, session, collected
            )
            booking_complete = True  # always mark complete when all details provided
            if not booking_details:
                booking_details = collected
        else:
            logger.info(
                f"[Status] doctor={doctor_selected} | slot={slot_selected} "
                f"| missing={missing_fields}"
            )

        # ── STEP 7: Build system prompt with live doctor list ─────────────────
        context_str    = build_context_summary(collected)
        missing_str    = ", ".join(missing_fields) if missing_fields else "None — ready to book!"
        
        # Only show doctor list if slot is NOT yet selected
        if slot_selected:
            doctor_list_str = "Selected: " + str(collected.get("doctor_name") or "Doctor") + " (" + str(collected.get("slot_display") or "Selected Slot") + ")"
            selection_instruction = "The doctor and slot are already selected. Now STRICTLY collect only the missing patient details of the PATIENT (not the doctor)."
        else:
            doctor_list_str = (
                format_doctors_for_display(session["doctors"])
                if session.get("doctors")
                else "Fetching available doctors from hospital system…"
            )
            selection_instruction = (
                "Here are all available doctors, dates, and their from-to time slots. "
                "Please ask the user to select one by typing the Number (e.g., '1', '2', etc.)."
            )

        system = (
            SYSTEM_PROMPT
            .replace("{selection_instruction}", selection_instruction)
            .replace("{context}",     context_str)
            .replace("{missing}",     missing_str)
            .replace("{doctor_list}", doctor_list_str)
        )

        # ── STEP 8: Get LLM response ──────────────────────────────────────────
        llm_out = await self._llm(session_manager.messages(session_id), system)

        if booking_msg:
            llm_out += booking_msg

        session_manager.add_message(session_id, "assistant", llm_out)
        logger.info(f"[{session_id}] done. booked={booking_complete}")

        return {
            "session_id":         session_id,
            "response":           llm_out,
            "appointment_booked": booking_complete,
            "booking_details":    booking_details or collected,
        }

    def reset(self, session_id: str):
        session_manager.reset(session_id)


appointment_agent = AppointmentAgent()