"""
Symptom Analysis Engine — Maps user symptoms → doctor specializations

Supports:
  - English, Gujarati, Hindi symptom keywords
  - Regex-based extraction (fast, zero-latency)
  - Gemini AI fallback for ambiguous / complex symptom descriptions
  - Fuzzy matching for partial symptom keywords
"""
import re
import json
import logging
from typing import List, Dict, Tuple, Optional

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════════════
# Symptom → Specialization Mapping
# ═══════════════════════════════════════════════════════════════════════════════

# Each key is a symptom keyword (lowercase), value is list of relevant specializations
# Order matters: first specialization is the most relevant
SYMPTOM_TO_SPECIALIZATION: Dict[str, List[str]] = {
    # ── General / Internal Medicine ───────────────────────────────────────────
    "fever":            ["General Medicine", "Internal Medicine"],
    "cold":             ["General Medicine", "ENT"],
    "cough":            ["General Medicine", "Pulmonology"],
    "flu":              ["General Medicine", "Internal Medicine"],
    "weakness":         ["General Medicine", "Internal Medicine"],
    "fatigue":          ["General Medicine", "Internal Medicine"],
    "body pain":        ["General Medicine", "Orthopedics"],
    "weight loss":      ["General Medicine", "Endocrinology"],
    "weight gain":      ["Endocrinology", "General Medicine"],
    "infection":        ["General Medicine", "Internal Medicine"],
    "viral":            ["General Medicine", "Internal Medicine"],
    "malaria":          ["General Medicine", "Internal Medicine"],
    "typhoid":          ["General Medicine", "Internal Medicine"],
    "dengue":           ["General Medicine", "Internal Medicine"],
    "vomiting":         ["General Medicine", "Gastroenterology"],
    "nausea":           ["General Medicine", "Gastroenterology"],
    "dizziness":        ["General Medicine", "Neurology", "ENT"],
    "dehydration":      ["General Medicine", "Internal Medicine"],
    "swelling":         ["General Medicine", "Internal Medicine"],

    # ── Neurology ─────────────────────────────────────────────────────────────
    "headache":         ["Neurology", "General Medicine"],
    "migraine":         ["Neurology"],
    "seizure":          ["Neurology"],
    "epilepsy":         ["Neurology"],
    "numbness":         ["Neurology", "Orthopedics"],
    "tingling":         ["Neurology"],
    "paralysis":        ["Neurology"],
    "memory loss":      ["Neurology", "Psychiatry"],
    "brain":            ["Neurology", "Neurosurgery"],
    "stroke":           ["Neurology", "Neurosurgery"],
    "nerve pain":       ["Neurology"],

    # ── Cardiology ────────────────────────────────────────────────────────────
    "chest pain":       ["Cardiology", "General Medicine"],
    "heart pain":       ["Cardiology"],
    "heart attack":     ["Cardiology", "Emergency Medicine"],
    "palpitation":      ["Cardiology", "General Medicine"],
    "blood pressure":   ["Cardiology", "General Medicine"],
    "high bp":          ["Cardiology", "General Medicine"],
    "low bp":           ["Cardiology", "General Medicine"],
    "breathlessness":   ["Cardiology", "Pulmonology"],
    "heart":            ["Cardiology"],

    # ── Orthopedics ───────────────────────────────────────────────────────────
    "bone pain":        ["Orthopedics"],
    "joint pain":       ["Orthopedics", "Rheumatology"],
    "back pain":        ["Orthopedics", "Neurology"],
    "knee pain":        ["Orthopedics"],
    "shoulder pain":    ["Orthopedics"],
    "neck pain":        ["Orthopedics", "Neurology"],
    "fracture":         ["Orthopedics"],
    "sprain":           ["Orthopedics"],
    "arthritis":        ["Orthopedics", "Rheumatology"],
    "spine":            ["Orthopedics", "Neurosurgery"],
    "leg pain":         ["Orthopedics"],
    "hand pain":        ["Orthopedics"],
    "hip pain":         ["Orthopedics"],
    "muscle pain":      ["Orthopedics", "General Medicine"],

    # ── Dermatology ───────────────────────────────────────────────────────────
    "skin rash":        ["Dermatology"],
    "skin":             ["Dermatology"],
    "rash":             ["Dermatology", "General Medicine"],
    "itching":          ["Dermatology", "General Medicine"],
    "acne":             ["Dermatology"],
    "pimple":           ["Dermatology"],
    "eczema":           ["Dermatology"],
    "psoriasis":        ["Dermatology"],
    "hair loss":        ["Dermatology"],
    "fungal":           ["Dermatology"],
    "allergy":          ["Dermatology", "General Medicine"],
    "skin infection":   ["Dermatology"],
    "infection":   ["Dermatology"],

    # ── ENT (Ear, Nose, Throat) ───────────────────────────────────────────────
    "ear pain":         ["ENT"],
    "sore throat":      ["ENT", "General Medicine"],
    "throat pain":      ["ENT", "General Medicine"],
    "nose":             ["ENT"],
    "nose bleed":       ["ENT"],
    "sinus":            ["ENT"],
    "hearing":          ["ENT"],
    "tonsil":           ["ENT"],
    "snoring":          ["ENT", "Pulmonology"],
    "voice":            ["ENT"],

    # ── Ophthalmology (Eye) ───────────────────────────────────────────────────
    "eye pain":         ["Ophthalmology"],
    "eye":              ["Ophthalmology"],
    "vision":           ["Ophthalmology"],
    "blurry vision":    ["Ophthalmology"],
    "red eye":          ["Ophthalmology"],
    "cataract":         ["Ophthalmology"],
    "glaucoma":         ["Ophthalmology"],

    # ── Dentistry ─────────────────────────────────────────────────────────────
    "tooth pain":       ["Dentistry"],
    "toothache":        ["Dentistry"],
    "dental":           ["Dentistry"],
    "teeth":            ["Dentistry"],
    "gum":              ["Dentistry"],
    "cavity":           ["Dentistry"],

    # ── Gastroenterology ──────────────────────────────────────────────────────
    "stomach pain":     ["Gastroenterology", "General Medicine"],
    "stomach":          ["Gastroenterology", "General Medicine"],
    "acidity":          ["Gastroenterology", "General Medicine"],
    "gas":              ["Gastroenterology", "General Medicine"],
    "diarrhea":         ["Gastroenterology", "General Medicine"],
    "constipation":     ["Gastroenterology", "General Medicine"],
    "ulcer":            ["Gastroenterology"],
    "liver":            ["Gastroenterology", "Hepatology"],
    "jaundice":         ["Gastroenterology", "General Medicine"],
    "abdominal pain":   ["Gastroenterology", "General Surgery"],
    "bloating":         ["Gastroenterology", "General Medicine"],

    # ── Pulmonology ───────────────────────────────────────────────────────────
    "asthma":           ["Pulmonology"],
    "breathing":        ["Pulmonology", "Cardiology"],
    "lung":             ["Pulmonology"],
    "wheezing":         ["Pulmonology"],
    "tb":               ["Pulmonology", "General Medicine"],
    "tuberculosis":     ["Pulmonology", "General Medicine"],
    "pneumonia":        ["Pulmonology", "General Medicine"],

    # ── Gynecology / Obstetrics ───────────────────────────────────────────────
    "pregnancy":        ["Obstetrics & Gynecology", "Gynecology"],
    "pregnant":         ["Obstetrics & Gynecology", "Gynecology"],
    "periods":          ["Gynecology", "Obstetrics & Gynecology"],
    "menstruation":     ["Gynecology"],
    "pcod":             ["Gynecology", "Endocrinology"],
    "pcos":             ["Gynecology", "Endocrinology"],
    "menopause":        ["Gynecology"],
    "uterus":           ["Gynecology", "Obstetrics & Gynecology"],
    "breast":           ["Gynecology", "General Surgery"],

    # ── Urology ───────────────────────────────────────────────────────────────
    "kidney":           ["Urology", "Nephrology"],
    "kidney stone":     ["Urology"],
    "urinary":          ["Urology"],
    "urine":            ["Urology", "General Medicine"],
    "bladder":          ["Urology"],
    "prostate":         ["Urology"],

    # ── Pediatrics ────────────────────────────────────────────────────────────
    "child":            ["Pediatrics"],
    "baby":             ["Pediatrics"],
    "infant":           ["Pediatrics"],
    "vaccination":      ["Pediatrics", "General Medicine"],

    # ── Psychiatry / Psychology ───────────────────────────────────────────────
    "anxiety":          ["Psychiatry"],
    "depression":       ["Psychiatry"],
    "stress":           ["Psychiatry", "General Medicine"],
    "insomnia":         ["Psychiatry", "Neurology"],
    "sleep":            ["Psychiatry", "Neurology", "Pulmonology"],
    "mental health":    ["Psychiatry"],
    "panic":            ["Psychiatry"],

    # ── Endocrinology ─────────────────────────────────────────────────────────
    "diabetes":         ["Endocrinology", "General Medicine"],
    "sugar":            ["Endocrinology", "General Medicine"],
    "thyroid":          ["Endocrinology"],
    "hormone":          ["Endocrinology"],

    # ── Oncology ──────────────────────────────────────────────────────────────
    "cancer":           ["Oncology"],
    "tumor":            ["Oncology", "General Surgery"],
    "lump":             ["General Surgery", "Oncology"],

    # ── General Surgery ───────────────────────────────────────────────────────
    "piles":            ["General Surgery", "Proctology"],
    "fissure":          ["General Surgery", "Proctology"],
    # ── Plurals & Variations ──────────────────────────────────────────────────
    "rashes":           ["Dermatology", "General Medicine"],
    "itchy":            ["Dermatology", "General Medicine"],
    "eye problems":     ["Ophthalmology"],
    "joint pains":      ["Orthopedics", "Rheumatology"],
    "pains":            ["General Medicine"],
}

# ═══════════════════════════════════════════════════════════════════════════════
# Gujarati & Hindi Symptom Aliases → English
# ═══════════════════════════════════════════════════════════════════════════════

REGIONAL_SYMPTOM_MAP: Dict[str, str] = {
    # ── Gujarati ──────────────────────────────────────────────────────────────
    "taav":            "fever",
    "tav":             "fever",
    "shardi":          "cold",
    "sardi":           "cold",
    "khansi":          "cough",
    "khanshi":         "cough",
    "ughras":          "cough",
    "mathanu dukhvu":  "headache",
    "matha no dukhavo": "headache",
    "mathu dukhe":     "headache",
    "mathu dukhe che": "headache",
    "chhati no dukhavo": "chest pain",
    "chhati dukhe":    "chest pain",
    "pet no dukhavo":  "stomach pain",
    "pet dukhe":       "stomach pain",
    "pet dukhe che":   "stomach pain",
    "kamar no dukhavo": "back pain",
    "kamar dukhe":     "back pain",
    "ghutna dukhe":    "knee pain",
    "ghutna no dukhavo": "knee pain",
    "aankh dukhe":     "eye pain",
    "aankh no dukhavo": "eye pain",
    "kaan dukhe":      "ear pain",
    "kaan no dukhavo": "ear pain",
    "daant dukhe":     "tooth pain",
    "daant no dukhavo": "tooth pain",
    "chamdi":          "skin",
    "chamdi no rog":   "skin rash",
    "khujli":          "itching",
    "ulatee":          "vomiting",
    "ulti":            "vomiting",
    "chakkar":         "dizziness",
    "thakan":          "fatigue",
    "haddka":          "bone pain",
    "hadka dukhe":     "bone pain",
    "sango dukhe":     "joint pain",
    "sango no dukhavo": "joint pain",
    "shvas":           "breathing",
    "shvas leva ma takleef": "breathlessness",
    "pagaaveli":       "pregnancy",
    "garbhvati":       "pregnancy",
    "sugar":           "diabetes",

    # ── Hindi ─────────────────────────────────────────────────────────────────
    "bukhar":          "fever",
    "bukhaar":         "fever",
    "zukhaam":         "cold",
    "jukam":           "cold",
    "khansi":          "cough",
    "sir dard":        "headache",
    "sar dard":        "headache",
    "sir me dard":     "headache",
    "seene me dard":   "chest pain",
    "chest me dard":   "chest pain",
    "pet me dard":     "stomach pain",
    "pet dard":        "stomach pain",
    "kamar dard":      "back pain",
    "ghutne me dard":  "knee pain",
    "ghutna dard":     "knee pain",
    "aankh me dard":   "eye pain",
    "daant me dard":   "tooth pain",
    "daant dard":      "tooth pain",
    "kaan me dard":    "ear pain",
    "kaan dard":       "ear pain",
    "gala me dard":    "sore throat",
    "gala dard":       "sore throat",
    "ulti":            "vomiting",
    "ji machlana":     "nausea",
    "chakkar aana":    "dizziness",
    "chakkar":         "dizziness",
    "thakan":          "fatigue",
    "kamzori":         "weakness",
    "haddi me dard":   "bone pain",
    "jodo me dard":    "joint pain",
    "saans lene me takleef": "breathlessness",
    "saans":           "breathing",
    "khujli":          "itching",
    "dast":            "diarrhea",
    "kabz":            "constipation",
    "peshab":          "urinary",
    "pathri":          "kidney stone",
    "neend na aana":   "insomnia",
    "chinta":          "anxiety",
    "tension":         "stress",
    "pairon me dard":  "leg pain",
    "haath me dard":   "hand pain",
    "tang":            "leg pain",
    "peeth dard":      "back pain",
    "gathiya":         "arthritis",
    "sugar":           "diabetes",
    "bp":              "blood pressure",
    "pet phulna":      "bloating",
    "gas":             "gas",
    "muhase":          "acne",
    "bawasir":         "piles",
    # ── Plurals & Variations ──────────────────────────────────────────────────
    "rashse":          "rash",  # Common typo
    "khansi":          "cough",
    "bukhar":          "fever",
    "daant":           "tooth pain",
}

# Build single-pass regex: sort by length (longest first) to avoid partial matches
_REGIONAL_KEYS_SORTED = sorted(REGIONAL_SYMPTOM_MAP.keys(), key=len, reverse=True)
_REGIONAL_PATTERN = re.compile(
    r'\b(' + '|'.join(re.escape(k) for k in _REGIONAL_KEYS_SORTED) + r')\b',
    re.IGNORECASE
)

# English symptom keywords sorted longest first
_SYMPTOM_KEYS_SORTED = sorted(SYMPTOM_TO_SPECIALIZATION.keys(), key=len, reverse=True)
_SYMPTOM_PATTERN = re.compile(
    r'\b(' + '|'.join(re.escape(k) for k in _SYMPTOM_KEYS_SORTED) + r')\b',
    re.IGNORECASE
)


# ═══════════════════════════════════════════════════════════════════════════════
# Core Functions
# ═══════════════════════════════════════════════════════════════════════════════

def extract_symptoms(user_message: str) -> List[str]:
    """
    Extract symptom keywords from a user message.
    Handles English, Gujarati, and Hindi symptom terms.

    Returns a deduplicated list of canonical English symptom names.
    """
    text = user_message.strip().lower()
    found_symptoms: List[str] = []

    # ── Step 1: Match regional (Gujarati/Hindi) terms → convert to English ──
    for match in _REGIONAL_PATTERN.finditer(text):
        regional_term = match.group(1).lower()
        english_symptom = REGIONAL_SYMPTOM_MAP.get(regional_term)
        if english_symptom and english_symptom not in found_symptoms:
            found_symptoms.append(english_symptom)

    # ── Step 2: Match English symptom keywords ──────────────────────────────
    for match in _SYMPTOM_PATTERN.finditer(text):
        symptom = match.group(1).lower()
        if symptom not in found_symptoms:
            found_symptoms.append(symptom)

    # ── Step 3: Common sentence patterns ────────────────────────────────────
    #  "I have X", "mane X che", "mujhe X hai"
    pattern_phrases = [
        r'(?:i have|having|suffering from|problem with|issue with)\s+(.+?)(?:\s+and|\s*,|\s*$)',
        r'(?:mane|muje|mujhe)\s+(.+?)(?:\s+che|\s+hai|\s+he|\s*,|\s*$)',
    ]
    for pat in pattern_phrases:
        for m in re.finditer(pat, text, re.IGNORECASE):
            phrase = m.group(1).strip()
            # Try matching phrase against symptom keys
            for sk in _SYMPTOM_KEYS_SORTED:
                if sk in phrase and sk not in found_symptoms:
                    found_symptoms.append(sk)
            # Try regional
            for rk in _REGIONAL_KEYS_SORTED:
                if rk in phrase:
                    eng = REGIONAL_SYMPTOM_MAP[rk]
                    if eng not in found_symptoms:
                        found_symptoms.append(eng)

    logger.info(f"[SymptomEngine] Extracted symptoms from '{user_message[:60]}': {found_symptoms}")
    return found_symptoms


def symptoms_to_specializations(symptoms: List[str]) -> List[str]:
    """
    Map a list of symptoms to relevant medical specializations.
    Returns a deduplicated, priority-ordered list of specializations.
    """
    if not symptoms:
        return []

    specs: List[str] = []
    for symptom in symptoms:
        symptom_lower = symptom.lower()
        matched_specs = SYMPTOM_TO_SPECIALIZATION.get(symptom_lower, [])
        for s in matched_specs:
            if s not in specs:
                specs.append(s)

    logger.info(f"[SymptomEngine] Symptoms {symptoms} → Specializations {specs}")
    return specs


def has_symptom_keywords(message: str) -> bool:
    """
    Quick check: does this message contain any symptom-like keywords?
    Used by the agent to decide whether to enter symptom flow.
    """
    text = message.strip().lower()

    # Check for regional symptom terms
    if _REGIONAL_PATTERN.search(text):
        return True

    # Check for English symptom terms
    if _SYMPTOM_PATTERN.search(text):
        return True

    # Check for symptom-indicating phrases
    symptom_phrases = [
        r'\b(i have|i am having|suffering|problem|issue|pain|ache|hurt|dukhe|dukhavo|dard|takleef)\b',
    ]
    for pat in symptom_phrases:
        if re.search(pat, text, re.IGNORECASE):
            return True

    return False


def filter_doctors_by_specialization(
    availability_data: List[Dict],
    specializations: List[str],
) -> List[Dict]:
    """
    Filter doctor availability data by matching department names
    against the given specializations.

    Args:
        availability_data: Raw availability response from Aarogya API
                          (list of dicts with 'department', 'healthProfessionalName', etc.)
        specializations: List of target specializations to match

    Returns:
        Filtered list of doctor availability entries with matching departments
    """
    if not specializations or not availability_data:
        return availability_data  # Return all if no filter

    spec_lower = [s.lower() for s in specializations]

    filtered = []
    for entry in availability_data:
        dept = entry.get("department", "").lower()

        # Exact department match
        if dept in spec_lower:
            filtered.append(entry)
            continue

        # Fuzzy: check if any specialization keyword is IN the department name
        # e.g., "General Medicine" matches "Department of General Medicine"
        for spec in spec_lower:
            if spec in dept or dept in spec:
                filtered.append(entry)
                break

        # Also check partial word match
        # e.g., "cardiology" matches "Interventional Cardiology"
        if entry not in filtered:
            dept_words = set(dept.split())
            for spec in spec_lower:
                spec_words = set(spec.split())
                if spec_words & dept_words:  # Any common word
                    filtered.append(entry)
                    break

    logger.info(
        f"[SymptomEngine] Filtered {len(availability_data)} doctors → "
        f"{len(filtered)} matching {specializations}"
    )
    return filtered


# ═══════════════════════════════════════════════════════════════════════════════
# Gemini AI Fallback (for complex / unrecognized symptoms)
# ═══════════════════════════════════════════════════════════════════════════════

async def gemini_symptom_analysis(user_message: str) -> Dict:
    """
    Use Gemini AI to extract symptoms and suggest specializations
    when regex-based extraction finds nothing.

    Returns:
        {
            "symptoms": ["fever", "headache"],
            "specializations": ["General Medicine", "Neurology"],
            "summary": "Patient reports fever and headache"
        }
    """
    try:
        from google import genai
        from app.config.settings import settings

        api_key = settings.GEMINI_API_KEY
        if not api_key:
            logger.warning("[SymptomEngine] No GEMINI_API_KEY found, skipping AI analysis")
            return {"symptoms": [], "specializations": [], "summary": ""}

        client = genai.Client(api_key=api_key)

        prompt = f"""You are a medical triage assistant. Analyze the following patient message and extract:
1. symptoms - list of medical symptoms mentioned (in English)
2. specializations - list of medical specializations/departments that should be consulted
3. summary - one-line summary of the patient's complaint

Patient message: "{user_message}"

Respond ONLY in this exact JSON format, nothing else:
{{"symptoms": ["symptom1", "symptom2"], "specializations": ["Dept1", "Dept2"], "summary": "brief summary"}}
"""

        response = client.models.generate_content(
            model="gemini-2.0-flash",
            contents=prompt,
        )

        text = response.text.strip()
        # Extract JSON from response (handle markdown code blocks)
        json_match = re.search(r'\{[^{}]+\}', text)
        if json_match:
            result = json.loads(json_match.group())
            logger.info(f"[SymptomEngine] Gemini analysis: {result}")
            return result

    except Exception as e:
        logger.error(f"[SymptomEngine] Gemini analysis failed: {e}", exc_info=True)

    return {"symptoms": [], "specializations": [], "summary": ""}


# ═══════════════════════════════════════════════════════════════════════════════
# Main Analysis Function (combines regex + Gemini fallback)
# ═══════════════════════════════════════════════════════════════════════════════

async def analyze_symptoms(user_message: str) -> Dict:
    """
    Full symptom analysis pipeline:
      1. Try regex-based extraction (fast)
      2. If no symptoms found, try Gemini AI (slower but smarter)
      3. Return symptoms + specializations

    Returns:
        {
            "symptoms": ["fever", "headache"],
            "specializations": ["General Medicine", "Neurology"],
            "source": "regex" | "gemini" | "none"
        }
    """
    # Step 1: Regex-based extraction
    symptoms = extract_symptoms(user_message)

    if symptoms:
        specs = symptoms_to_specializations(symptoms)
        return {
            "symptoms": symptoms,
            "specializations": specs,
            "source": "regex",
        }

    # Step 2: Gemini fallback (only if regex found nothing meaningful)
    if has_symptom_keywords(user_message):
        gemini_result = await gemini_symptom_analysis(user_message)
        if gemini_result.get("symptoms"):
            return {
                "symptoms": gemini_result["symptoms"],
                "specializations": gemini_result.get("specializations", []),
                "source": "gemini",
            }

    # Step 3: Nothing found
    return {
        "symptoms": [],
        "specializations": [],
        "source": "none",
    }
