import os
from dotenv import load_dotenv
from google import genai

load_dotenv()

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL   = "gemini-2.5-flash"
BASE_DIR       = "user_data"

client = genai.Client(api_key=GEMINI_API_KEY)
os.makedirs(BASE_DIR, exist_ok=True)