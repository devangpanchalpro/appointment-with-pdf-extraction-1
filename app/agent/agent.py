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
import uuid
import asyncio
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
    """Filter doctors by symptom keywords mapped to departments.

    If no matching department is found, return the original list.
    """
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


def sanitize_doctors_list(doctors: List[Dict]) -> List[Dict]:
    """Ensure the displayed doctor list only contains real entries from the API.

    The API sometimes returns entries with missing or placeholder names (e.g. "Dr. Unknown").
    We do not want to show these as they can confuse users and appear as fake.
    """
    cleaned: List[Dict] = []
    for d in doctors:
        name = (d.get("healthProfessionalName") or "").strip()
        if not name:
            continue
        lower_name = name.lower()
        if lower_name in ("dr. unknown", "dr unknown", "unknown", "n/a", "none"):
            continue
        cleaned.append(d)
    return cleaned


# ── Doctor display ────────────────────────────────────────────────────────────

def format_doctors_for_display(doctors: List[Dict]) -> str:
    """Format doctor(s) and slots with global numbering for selection."""
    unique_slots = _get_unique_slots_for_selection(doctors)
    if not unique_slots:
        return "No slots available."

    lines = []
    current_doc = None
    current_date = None
    
    for i, s in enumerate(unique_slots, 1):
        doc_header = f"👨‍⚕️ Dr. {s['doctor_name'].replace('Dr.', '').strip()}"
        if doc_header != current_doc:
            if current_doc is not None:
                lines.append("") # Double break for new doctor
            lines.append(f"**{doc_header}**  ") # Bold and two spaces for break
            current_doc = doc_header
            current_date = None
            
        if s['date'] != current_date:
            try:
                date_obj = datetime.strptime(s['date'], "%Y-%m-%d")
                display_date = date_obj.strftime("%Y-%m-%d")
            except:
                display_date = s['date']
            lines.append(f"📅 Date : {display_date}  ")
            current_date = s['date']
            
        lines.append(f"{i}. ⏰ {s['from']} - {s['to']}  ")

    return "\n".join(lines).strip()


# ── Slot helpers ──────────────────────────────────────────────────────────────

def _flatten_available_slots(doc: Dict) -> List[Dict]:
    """Return a flat list of all available slots for a doctor entry."""
    slots: List[Dict] = []

    def slot_is_available(value: Any) -> bool:
        if value is None:
            return True
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        return text in ("true", "1", "yes", "available")

    for sched in doc.get("schedule", []):
        for s in sched.get("slots", []):
            avail = s.get("isAvailable")
            if slot_is_available(avail):
                slots.append(s)

    return slots


def _get_unique_slots_for_selection(doctors: List[Dict]) -> List[Dict]:
    """
    Returns a flattened, deduplicated list of available slots across all doctors.
    This list matches the numbered order shown to the user.
    """
    seen_slots = set()
    results = []

    # Sort doctors for deterministic ordering (same as display)
    sorted_doctors = sorted(doctors, key=lambda d: (
        (d.get("healthProfessionalName") or "").lower(),
        (d.get("department") or "").lower(),
        str(d.get("appointmentDate", ""))
    ))

    for d in sorted_doctors:
        name = d.get("healthProfessionalName", "Dr. Unknown").strip()
        dept = d.get("department", "General").strip()
        date_str = str(d.get("appointmentDate", "???"))
        
        slots = _flatten_available_slots(d)
        # Sort slots by start time
        slots_sorted = sorted(slots, key=lambda s: str(s.get("from") or s.get("startTime") or ""))
        
        for s in slots_sorted:
            s_from = str(s.get("from") or s.get("startTime") or "??").strip()
            s_to   = str(s.get("to")   or s.get("endTime")   or "??").strip()
            
            slot_key = (name.lower().replace("dr.", "").strip(), dept.lower(), date_str, s_from, s_to)
            if slot_key not in seen_slots:
                seen_slots.add(slot_key)
                results.append({
                    "from": s_from,
                    "to": s_to,
                    "date": date_str,
                    "doctor_name": name,
                    "dept": dept,
                    "raw_slot": s,
                    "raw_doc": d
                })
    return results


def _slot_external_id(slot: Dict, doc: Dict) -> str:
    """
    Return a unique GUID for every slot selection to satisfy API requirements.
    """
    return str(uuid.uuid4())


# ── Context / missing fields ──────────────────────────────────────────────────

REQUIRED_PATIENT_FIELDS = [
    "firstName", "lastName",
    "mobile", "gender", "address",
]


def missing_patient_fields(collected: Dict) -> List[str]:
    missing: List[str] = []

    for k in REQUIRED_PATIENT_FIELDS:
        val = collected.get(k)
        if not val or str(val).strip().lower() in ("", "null", "none"):
            missing.append(k)
            continue

        # Logic check for names
        if k in ("firstName", "lastName"):
            v = str(val).strip()
            # If name is from FORBIDDEN list or too short, mark as missing
            if v.lower() in FORBIDDEN_NAMES or len(v) < 2:
                missing.append(k)
        if k == "mobile":
            if not re.match(r"^\d{10}$", str(val).replace(" ", "")):
                missing.append(k)

    # Need birthDate
    dob = collected.get("birthDate")
    if not dob:
        missing.append("Date of Birth (YYYY-MM-DD)")
    else:
        # Check if it was normalized successfully (YYYY-MM-DD)
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", str(dob)):
            missing.append("Date of Birth (YYYY-MM-DD)")

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

SYSTEM_PROMPT = """You are a highly strict, task-oriented medical appointment booking assistant. Your only purpose is to assist users in booking doctor appointments.
You must follow all instructions exactly and never produce unnecessary or extra text.

CORE BEHAVIOR RULES:
- Respond ONLY to what the user asks.
- Keep responses minimal, structured, and action-focused.
- NEVER hallucinate data. Use ONLY values explicitly provided in the current state.
- NEVER auto-select a doctor or slot.
- STRICT HALLUCINATION GUARD: Do NOT write doctor/slot lists yourself. 
- You MUST use the placeholder [[DOCTOR_LIST]] when you need to show available options.

FLOW LOGIC:

1. GREETINGS & INTRO:
   If user says "hello", "hi", or similar greetings → Respond: "How can I help you with booking an appointment?"

2. DOCTOR OR SYMPTOM INPUT:
   If user provides a doctor name or symptoms → Show ONLY available slots via [[DOCTOR_LIST]].
   Response format:
   [[DOCTOR_LIST]]

3. SLOT SELECTION:
   Once user selects a slot (by number), if patient details are missing, ask for them.
   Use format: "Please provide patient Full name, DOB, gender, address, Contact for book you appointment"

4. FINAL CONFIRMATION:
   After all details are successfully collected, respond ONLY with:
   "Your appointment is scheduled with Dr. {Doctor Name} on {Date} at {Time}. Please arrive 15 minutes before the scheduled time."

No extra text allowed.

## CURRENT STATUS:
{situation}

## AVAILABLE DOCTORS & SLOTS (inserted via placeholder):
{doctor_list}
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
  "pinCode":    "6-digit string" or null,
  "address":    string or null,
  "area":       string or null,
  "symptoms":   array of strings or null,
  "doctor_name": string or null,
  "appointment_date": "YYYY-MM-DD" or null,
  "appointment_time": "HH:MM" or null
}}

CRITICAL RULES:

⚠️  PATIENT vs DOCTOR SEPARATION:
  - If user mentions ONLY a doctor name (e.g., "I want to see Dr. Smith"):
    → Extract ONLY doctor_name="Dr. Smith"
    → Set ALL patient fields (firstName, lastName, mobile, etc.) to NULL
  - NEVER extract doctor titles (Dr, Mrs, Mr) or names into patient fields.
  - Extract patient names ONLY if explicitly provided (e.g., "My name is John").

⚠️  NO HALLUCINATION:
  - Do NOT extract fields that are not explicitly mentioned in the User text.
  - NEVER guess or use example values (like "395006" or "2026-03-20") unless they appear exactly in the User text.
  - If a field is missing, set it to NULL.

⚠️  DATES & TIMES:
  - birthDate: extract ONLY if user says "date of birth", "dob", or "born on".
  - appointment_date: extract ONLY if user says "on [date]" for the appointment.
  - appointment_time: extract ONLY if user mentions a specific time or slot.

⚠️  CLEANING:
  - Remove fillers like "my name is", "lives at", "contact number" etc.
  - Address: Return strictly the location, no "is at" or similar.

Examples:
Text: "I want to see Dr. Dhruv Barot"
Output: {{"doctor_name": "Dr. Dhruv Barot", "firstName": null, "lastName": null, "appointment_date": null}}

Text: "My name is Rahul Patel, mobile 9999911111"
Output: {{"firstName": "Rahul", "lastName": "Patel", "mobile": "9999911111"}}

Return ONLY the JSON object.

Current year: {year}

User text:
\"\"\"{text}\"\"\"
"""


# ── IST → UTC conversion ──────────────────────────────────────────────────────

IST_OFFSET = timedelta(hours=5, minutes=30)


def ist_to_utc(dt: datetime) -> datetime:
    """Convert an IST datetime to UTC by subtracting 5 h 30 m."""
    return dt - IST_OFFSET


# ── Fast Regex Extractor (NO LLM, instant) ───────────────────────────────────

def _fast_extract(text: str) -> Dict:
    """
    Zero-latency extraction using regex patterns for structured inputs.
    Handles: 10-digit mobile, name phrases, gender, age, DOB, address, pincode.
    Returns a partial dict (may be empty if nothing matched).
    """
    result: Dict = {}
    t = text.strip()
    tl = t.lower()

    # ── Mobile number ─────────────────────────────────────────────────────────
    mob = re.search(r'\b(\d{10})\b', t)
    if mob:
        result["mobile"] = mob.group(1)

    # ── 6-digit pin code ──────────────────────────────────────────────────────
    pin = re.search(r'\b(\d{6})\b', t)
    if pin and pin.group(1) != result.get("mobile", ""):
        result["pinCode"] = pin.group(1)

    # ── Date of birth vs Appointment Date ─────────────────────────────────────
    dob1 = re.search(r'\b(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})\b', t)
    dob2 = re.search(r'\b(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})\b', t)
    extracted_date = None
    if dob1:
        d, m, y = dob1.group(1), dob1.group(2), dob1.group(3)
        extracted_date = f"{y}-{m.zfill(2)}-{d.zfill(2)}"
    elif dob2:
        y, m, d = dob2.group(1), dob2.group(2), dob2.group(3)
        extracted_date = f"{y}-{m.zfill(2)}-{d.zfill(2)}"
        
    if extracted_date:
        year_int = int(extracted_date.split('-')[0])
        # If year is this year or next year, it's an appointment date unless 'dob' is mentioned.
        # Historic years should remain birthDate and must not silently overwrite slot appointment_date.
        if year_int < datetime.now().year - 1 or re.search(r'\b(dob|birth|born|age|janma)\b', tl):
            result["birthDate"] = extracted_date
        else:
            result["appointment_date"] = extracted_date

    # ── Appointment Time ──────────────────────────────────────────────────────
    time_m = re.search(r'\b(\d{1,2}:\d{2}(?:\s*-\s*\d{1,2}:\d{2})?)\b', t)
    if time_m:
        result["appointment_time"] = time_m.group(1).replace(" ", "")

    # ── Age (Removed as per requirements) ──────────────────────────────────────

    # ── Gender ────────────────────────────────────────────────────────────────
    if re.search(r'\b(female|stri|mahila|woman|girl)\b', tl):
        result["gender"] = 2
    elif re.search(r'\b(male|purush|man|boy)\b', tl):
        result["gender"] = 1

    # ── Name phrases ("my name is X Y", "naam X Y hai", "name X Y") ──────────
    name_m = re.search(
        r'(?:my\s+name\s+is|i\s+am|name\s*[:=]\s*|naam\s+(?:hai\s+)?|maro\s+naam\s+(?:che\s+)?|mera\s+naam\s+(?:hai\s+)?)([A-Za-z]+(?:\s+[A-Za-z]+)*)',
        tl
    )
    if name_m:
        parts = name_m.group(1).strip().split()
        # Filter forbidden single-word hits
        parts = [p for p in parts if p.lower() not in FORBIDDEN_NAMES]
        if len(parts) >= 1:
            result["firstName"] = parts[0].capitalize()
        if len(parts) >= 2:
            result["lastName"] = " ".join(p.capitalize() for p in parts[1:])

    # ── Address keywords ──────────────────────────────────────────────────────
    addr_m = re.search(r'(?:address|addr|rehta|rehti|rahata|rahti)[:\s]+(.+)', tl)
    if addr_m:
        result["address"] = addr_m.group(1).strip()

    # ── Symptoms ──────────────────────────────────────────────────────────────
    symptoms = []
    for symptom_key in SYMPTOM_DEPARTMENT_MAP.keys():
        if symptom_key in tl:
            symptoms.append(symptom_key)
    if symptoms:
        result["symptoms"] = symptoms

    return result


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

        # Some Ollama models may take longer; retry once on timeout to improve resilience.
        for attempt in range(1, 3):
            try:
                timeout = httpx.Timeout(settings.OLLAMA_TIMEOUT, connect=10)
                async with httpx.AsyncClient(timeout=timeout) as client:
                    r = await client.post(f"{settings.OLLAMA_BASE_URL}/api/chat", json=payload)
                    r.raise_for_status()
                    return r.json()["message"]["content"]
            except httpx.ConnectError:
                # Connection problems are unlikely to recover quickly.
                return ("⚠️ Cannot connect to Ollama. Please ensure Ollama is running "
                        f"and accessible at {settings.OLLAMA_BASE_URL}.")
            except httpx.ReadTimeout:
                logger.warning(f"Ollama request timed out (attempt {attempt})")
                if attempt >= 2:
                    return ("⚠️ Ollama timed out. Please try again. "
                            f"(Ensuring Ollama is running at {settings.OLLAMA_BASE_URL} can help.)")
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"LLM error: {e}", exc_info=True)
                return f"Sorry, something went wrong. Error: {e}"

        return "⚠️ Ollama timed out. Please try again."  # fallback



    async def _llm_stream(self, messages: List[Dict], system: str):
        payload = {
            "model":   settings.LLM_MODEL,
            "messages": [{"role": "system", "content": system}] + messages,
            "stream":  True,
            "options": {
                "temperature": settings.LLM_TEMPERATURE,
                "num_predict": settings.LLM_MAX_TOKENS,
            },
        }

        for attempt in range(1, 3):
            try:
                timeout = httpx.Timeout(settings.OLLAMA_TIMEOUT, connect=10)
                async with httpx.AsyncClient(timeout=timeout) as client:
                    async with client.stream("POST", f"{settings.OLLAMA_BASE_URL}/api/chat", json=payload) as r:
                        r.raise_for_status()
                        async for chunk in r.aiter_text():
                            for line in chunk.strip().split('\n'):
                                if line:
                                    try:
                                        data = json.loads(line)
                                        if "message" in data and "content" in data["message"]:
                                            yield data["message"]["content"]
                                    except Exception:
                                        pass
                        return
            except httpx.ConnectError:
                yield "⚠️ Cannot connect to Ollama. Please ensure Ollama is running."
                return
            except Exception as e:
                logger.warning(f"Ollama stream error (attempt {attempt}): {e}")
                if attempt >= 2:
                    yield "⚠️ Ollama timed out or failed. Please try again."
                    return
                await asyncio.sleep(1)

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
            logger.info(f"[Extraction] cleaned result: {data}")
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
                            # Using explicit int casting for slice indices to satisfy strict type checkers
                            stop_idx = int(i) + 1
                            return json.loads(text[int(start) : stop_idx])
                        except Exception:
                            break
        # 3. Non-greedy regex
        m = re.search(r"\{.*?\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except Exception:
                pass
        logger.error(f"[Extraction] Could not parse JSON: {str(text)[:200]}")
        return {}

    @staticmethod
    def _clean_extracted_data(data: Dict, locked_dr_name: str = "") -> Dict:
        """Validate and clean extracted fields."""
        dr_name_text = str(data.get("doctor_name", "")).lower().replace("dr.", "").strip()
        locked_dr_low = locked_dr_name.lower().replace("dr.", "").strip()

        # Clean patient name fields
        for field in ("firstName", "lastName"):
            val = data.get(field)
            if val:
                v = str(val).strip()
                v_low = v.lower()
                
                # Stronger protection: if any word in patient name matches any word in doctor name, clear it
                dr_words = set(dr_name_text.split()) | set(locked_dr_low.split())
                v_words = set(v_low.split())
                
                if len(v) < 2 or v_low in FORBIDDEN_NAMES:
                    data[field] = None
                elif v_low in ("dr", "mrs", "mr", "ms", "doc", "doctor"):
                    data[field] = None
                elif dr_words and (v_words & dr_words):
                    # Leakage detected: patient name overlaps with doctor name
                    data[field] = None

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

        # ── Birth Date normalization ──────────────────────────────────────────
        dob = data.get("birthDate")
        if dob:
            d_str = str(dob).strip()
            normalized = None
            # Try various formats
            formats = ["%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%Y/%m/%d"]
            for f in formats:
                try:
                    dt = datetime.strptime(d_str, f)
                    # Sanity check: year must be reasonable (e.g., > 1900)
                    if dt.year > 1900 and dt.year <= datetime.now().year:
                        normalized = dt.strftime("%Y-%m-%d")
                        break
                except ValueError:
                    continue
            
            # If still not normalized, maybe it's missing the year (e.g., "12/07")
            if not normalized:
                data["birthDate"] = None # Mark as invalid
            else:
                data["birthDate"] = normalized

        # If extraction returned both appointment_date and a valid birthDate (typically patient DOB), do not overwrite selected slot.
        if data.get("birthDate") and data.get("appointment_date"):
            try:
                y = int(str(data.get("birthDate")).split("-")[0])
                if y < datetime.now().year - 1:
                    data.pop("appointment_date", None)
            except Exception:
                pass

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

            if birth_date_str:
                try:
                    birth_date = datetime.strptime(str(birth_date_str), "%Y-%m-%d")
                except ValueError:
                    logger.error(f"[Booking] Invalid birthDate format in collected info: {birth_date_str}")
                    return False, None, "\n❌ Booking failed: invalid date of birth format. Please provide it as YYYY-MM-DD."
            else:
                return False, None, "\n❌ Booking failed: date of birth is required."

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
                middle_name             = "",
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
                msg = f"Your appointment is scheduled with Dr. {collected.get('doctor_name', '')} on {appt_date_str} at {appt_time_str}. Please arrive 15 minutes before the scheduled time."
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
        logger.info(f"--- Chat Start [{session_id}] ---")
        logger.info(f"Stage: {session['stage']} | Collected: {collected}")
        logger.info(f"User Message: {user_message}")

        # ── Fast-path: Skip extraction if message is just a digit (slot selection) ──
        digit_match = re.search(r"^\s*(\d+)\s*$", user_message)
        
        if digit_match:
            logger.info("Skipping patient extraction for perfect digit match.")
            all_docs = await doctors_cache.get_doctors()
        else:
            # ── STEP 1: Try instant regex extraction FIRST (no LLM needed) ───
            fast_result = _fast_extract(user_message)
            fast_clean = {k: v for k, v in fast_result.items() if v is not None and str(v).strip() != ""}
            
            if fast_clean:
                slot_selected_before = bool(collected.get("slot_external_id"))
                if not slot_selected_before:
                    # In inquiry phase, only allow doctor/symptom/selection fields from fast-path
                    fast_filtered = {k: v for k, v in fast_clean.items() if k in ["doctor_name", "symptoms", "appointment_date", "appointment_time"]}
                    if fast_filtered:
                        session_manager.update_collected(session_id, fast_filtered)
                        logger.info(f"[FastExtract] Pre-selection phase - stored only: {fast_filtered}")
                else:
                    # After selection, allow all patient details but preserve booked slot date/time
                    post_filtered = {k: v for k, v in fast_clean.items() if k not in ["appointment_date", "appointment_time"]}
                    if post_filtered:
                        session_manager.update_collected(session_id, post_filtered)
                        logger.info(f"[FastExtract] Post-selection phase - stored patient details only: {post_filtered}")

            # ── STEP 1b: Fetch doctor list in parallel while deciding extraction
            # PHASE LOGIC: Only extract patient info IF a slot is already selected.
            # Otherwise, only extract doctor_name and symptoms.
            slot_selected_before = bool(collected.get("slot_external_id"))
            
            words = user_message.strip().split()
            needs_llm = len(words) > 1

            if needs_llm:
                dr_name_locked = collected.get("doctor_name") or ""
                extracted, all_docs = await asyncio.gather(
                    self._extract_patient_info(user_message, dr_name_locked),
                    doctors_cache.get_doctors()
                )
                
                # Phase-based filter
                to_store = {}
                if not slot_selected_before:
                    # Only store doctor/symptoms + date/time (for selection)
                    for k in ["doctor_name", "symptoms", "appointment_date", "appointment_time"]:
                        if k in extracted and extracted[k]:
                            to_store[k] = extracted[k]
                    logger.info(f"[Extraction] Pre-selection phase - stored only: {to_store}")
                else:
                    # Already have a slot - store patient details only and preserve chosen slot date/time
                    to_store = {k: v for k, v in extracted.items() if k not in ["appointment_date", "appointment_time"] and v}
                    logger.info(f"[Extraction] Post-selection phase - stored patient details only: {to_store}")

                if to_store:
                    session_manager.update_collected(session_id, to_store)
            else:
                all_docs = await doctors_cache.get_doctors()
        sanitized_docs = sanitize_doctors_list(all_docs)
        if not sanitized_docs and all_docs:
            # If the cached payload contains only placeholders, re-fetch once.
            all_docs = await doctors_cache.get_doctors(force_refresh=True)
            sanitized_docs = sanitize_doctors_list(all_docs)

        # Use sanitized list for matching/display, but fall back to raw list if it somehow becomes empty.
        all_docs = sanitized_docs or all_docs

        # ── STEP 3: Filter by doctor name or symptoms ─────────────────────────
        if session["stage"] in ("start", "doctors_shown") and not session.get("_doctor_slot_locked"):
            collected_now = session_manager.collected(session_id)
            dr_name   = collected_now.get("doctor_name")
            symptoms  = collected_now.get("symptoms") or []

            filtered: List[Dict] = []

            if dr_name:
                name_low = str(dr_name).lower().replace("dr.", "").strip()
                filtered = [
                    d for d in all_docs
                    if name_low in (d.get("healthProfessionalName") or "").lower()
                ]
                logger.info(f"[Filter] by doctor name '{dr_name}': {len(filtered)} entries")

                # ── Invalid doctor name: give immediate helpful feedback ───────
                if not filtered:
                    no_dr_msg = "No slots available."
                    # Clear the invalid doctor_name so the full list is shown fresh
                    session_manager.update_collected(session_id, {"doctor_name": None})
                    session_manager.add_message(session_id, "user", user_message)
                    session_manager.add_message(session_id, "assistant", no_dr_msg)
                    session["doctors"] = all_docs
                    session["stage"] = "doctors_shown"
                    return {
                        "session_id": session_id,
                        "response": no_dr_msg,
                        "appointment_booked": False,
                        "booking_details": {},
                    }
                # If we have a direct match by name, ONLY use those.
                session["doctors"] = filtered
            elif symptoms:
                syms = symptoms if isinstance(symptoms, list) else [str(symptoms)]
                filtered = filter_doctors_by_symptoms(all_docs, syms)
                logger.info(f"[Filter] by symptoms {syms}: {len(filtered)} entries")
                session["doctors"] = filtered
            else:
                session["doctors"] = all_docs

            if session["doctors"]:
                if session["stage"] == "start":
                    session["stage"] = "doctors_shown"

        # ── STEP 4: Doctor / Slot matching ────
        if not session.get("_doctor_slot_locked"):
            collected_now = session_manager.collected(session_id)
            ext_dr_name   = collected_now.get("doctor_name")
            ext_appt_date = collected_now.get("appointment_date")
            ext_appt_time = collected_now.get("appointment_time")
            
            # ── 4a. Check if user typed a NUMBER for selection ────────────────
            # (Deprecated logically, but left for backward compatibility)
            digit_match = re.search(r"^\s*(\d+)\s*$", user_message)
            if digit_match:
                index = int(digit_match.group(1)) - 1
                unique_slots = _get_unique_slots_for_selection(session.get("doctors", []))
                
                if 0 <= index < len(unique_slots):
                    item = unique_slots[index]
                    matched_slot = item["raw_slot"]
                    matched_doc = item["raw_doc"]
                    
                    ext_id = _slot_external_id(matched_slot, matched_doc)
                    exact_time = item["from"]
                    
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
                        "slot_display": f"{exact_time} - {item['to']}",
                    })
                    session["stage"] = "selected"
                    session["_doctor_slot_locked"] = True
                    logger.info(f"[Selection] Picked index {index+1}: {ext_id} at {exact_time}")
            
            # ── 4b. If no index match, try matching by LLM-extracted name/time ─
            if not session.get("_doctor_slot_locked"):
                target_docs = []
                if len(session.get("doctors", [])) == 1:
                    target_docs = session["doctors"]
                elif ext_dr_name:
                    name_low = str(ext_dr_name).lower().replace("dr.", "").strip()
                    for d in session.get("doctors", []):
                        if name_low in (d.get("healthProfessionalName") or "").lower():
                            target_docs.append(d)

                if target_docs:
                    session["doctors"] = target_docs
                    if ext_appt_time:
                        matched_slot = None
                        matched_doc = None
                        
                        # Compare against slots for matching doctors, prioritizing the extracted date if provided
                        for d in session.get("doctors", []):
                            doc_date = str(d.get("appointmentDate", ""))
                            if ext_appt_date and doc_date and doc_date != ext_appt_date:
                                continue

                            for s in _flatten_available_slots(d):
                                s_from = str(s.get("from") or s.get("startTime") or "")
                                s_to = str(s.get("to") or s.get("endTime") or "")
                                slot_label = f"{s_from} - {s_to}"
                                
                                # Match either exact full string or starting time
                                if s_from == ext_appt_time or ext_appt_time.replace(" ", "") in slot_label.replace(" ", ""):
                                    matched_slot = s
                                    matched_doc = d
                                    logger.info(f"[Selection] Slot matched via extracted time: {s_from}")
                                    break
                            if matched_slot:
                                break
                        
                        if matched_slot and matched_doc:
                            # Use guaranteed dict variable to satisfy Pyre
                            doc: Dict = matched_doc
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
                                "slot_display": f"{exact_time} - {s_to}",
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

        # If a slot is selected but required patient info is missing, do not run the LLM.
        # Instead, explicitly ask the user for the missing patient details.
        if slot_selected and missing_fields:
            # Format missing fields as a structured list
            field_map = {
                "firstName": "full name",
                "lastName": "full name",
                "mobile": "mobile",
                "gender": "gender",
                "address": "address",
                "Date of Birth (DD/MM/YYYY)": "DOB"
            }
            # Use a set to avoid showing "full name" twice
            unique_labels = []
            for f in missing_fields:
                label = field_map.get(f, f)
                if label not in unique_labels:
                    unique_labels.append(label)

            ask_lines = ["Please provide:"]
            for label in unique_labels:
                ask_lines.append(f"> {label}")
            
            ask = "\n".join(ask_lines)
            session_manager.add_message(session_id, "assistant", ask)
            return {
                "session_id": session_id,
                "response": ask,
                "appointment_booked": False,
                "booking_details": collected,
            }

        # ── STEP 7: Build system prompt ───────────────────────────────────────
        if slot_selected:
            situation = (
                f"Slot selected: {collected.get('doctor_name')} at {collected.get('slot_display')}. "
                + (f"Still need: {', '.join(missing_fields)}" if missing_fields else "All patient details collected — confirm booking.")
            )
            doctor_list_str = "(slot already chosen)"
        else:
            situation = "Show available doctors and slots. Wait for user to select. DO NOT ask for patient info yet!"
            doctor_list_str = (
                format_doctors_for_display(session["doctors"])
                if session.get("doctors")
                else "No doctors available."
            )

        system = (
            SYSTEM_PROMPT
            .replace("{situation}", situation)
            .replace("{doctor_list}", "IMPORTANT: If you need to show doctors and slots, simply use the placeholder [[DOCTOR_LIST]] and nothing else for the list. I will insert the list for you. Do not write the slots yourself as it is too slow.")
        )
        logger.info(f"[{session_id}] context: dr_list_chars={len(doctor_list_str)}")

        # ── STEP 8: Get LLM response ──────────────────────────────────────────
        llm_out = await self._llm(session_manager.messages(session_id), system)

        # Post-process: Insert the actual formatted list if placeholder exists
        if "[[DOCTOR_LIST]]" in llm_out:
            llm_out = llm_out.replace("[[DOCTOR_LIST]]", doctor_list_str)
        elif not slot_selected and session.get("doctors"):
            # If the LLM forgot the placeholder but we are supposed to show doctors, append it
            llm_out += "\n\n" + doctor_list_str

        if booking_msg:
            llm_out += "\n\n" + booking_msg

        session_manager.add_message(session_id, "assistant", llm_out)
        logger.info(f"[{session_id}] Response: {llm_out[:100]}...")
        logger.info(f"[{session_id}] Done. Booked: {booking_complete}")
        logger.info(f"--- Chat End [{session_id}] ---")

        return {
            "session_id":         session_id,
            "response":           llm_out,
            "appointment_booked": booking_complete,
            "booking_details":    booking_details or collected,
        }



    def reset(self, session_id: str):
        session_manager.reset(session_id)


appointment_agent = AppointmentAgent()