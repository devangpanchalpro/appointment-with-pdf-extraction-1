import re

def validate_abha(raw: str) -> tuple:
    if not raw or not raw.strip():
        return False, "", "⚠️ ABHA number is required."
    digits = re.sub(r"[-\s]", "", raw.strip())
    if not digits.isdigit():
        return False, "", "⚠️ ABHA number must contain only digits."
    if len(digits) != 14:
        return False, "", f"⚠️ Must be exactly 14 digits. You entered {len(digits)}."
    formatted = f"{digits[0:2]}-{digits[2:6]}-{digits[6:10]}-{digits[10:14]}"
    return True, digits, f"✅ Valid ABHA: {formatted}"