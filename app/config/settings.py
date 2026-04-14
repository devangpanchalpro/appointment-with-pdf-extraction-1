"""
Configuration settings for the Appointment Booking Agent System
"""
from pydantic_settings import BaseSettings
from typing import Optional
from pydantic import ConfigDict  # Ensure ConfigDict is imported


class Settings(BaseSettings):
    model_config = ConfigDict(extra='ignore', env_file=".env", env_file_encoding="utf-8")  # Load from .env file
    # Environment
    ENVIRONMENT: str = "dev"  # dev or prod

    # App
    APP_NAME: str = "Medical Appointment Booking Agent"
    APP_VERSION: str = "1.0.0"
    DEBUG: bool = True  # Set to False for faster startup (disable hot reload)

    # LLM (Ollama + Llama 3.2)
    OLLAMA_BASE_URL: str = "http://localhost:11434"
    LLM_MODEL: str = "llama3.2:latest"
    LLM_TEMPERATURE: float = 0.3
    LLM_MAX_TOKENS: int = 2048

    # Embeddings: Set to True to use Gemini embeddings (faster/offloads local CPU),
    # False to use local SentenceTransformer.
    USE_GEMINI_EMBEDDINGS: bool = False

    # Timeout for Ollama LLM requests (seconds)
    OLLAMA_TIMEOUT: int = 300

    # Gemini AI (for symptom analysis fallback)
    GEMINI_API_KEY: Optional[str] = None

    # JWT / JWE Authentication
    JWT_AUDIENCE: str = "dev-Aarogya.one"
    JWT_ISSUER: str = "dev-identify-Aarogya.one"
    JWT_KID: str = "5ac27dd68ec9403181c3b4fb002b2502"
    JWT_ENCRYPTION_KEY: str = "-----BEGIN PRIVATE KEY-----\r\n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\r\nQEyVyMvIHowBeN326MEdjtY7\r\n-----END PRIVATE KEY-----"
    JWT_SIGNING_KEY: str = "30819B301006072A8648CE3D020106052B81040023038186000401CFD634428AA6CD8B797CFF0D4D36F0F0CEBC1F43E807B9CC1230C93FF472A8FEE6F8CF2A45F42026F06DBD9263278F52D645907EA1AD220BFF99BD10373461839200BA6D5F76901D72752A20CCBD1C21AD182FE55E9FF6C465061F9C3D3E03BF6DE92CF3B3671C7304F9AD1338C87F3384DEDE38DA89B95639D75AD22AF4831B13AC8A"
    # JWT_ALGORITHM: str = "HS256"
    # JWT_EXPIRE_MINUTES: int = 60

    # External Hospital API — replace with your real API
    EXTERNAL_API_BASE_URL_DEV: str = "https://dev-hmis-api.aarogya.one/api/v1"
    EXTERNAL_API_BASE_URL_PROD: str = "https://beta-hmis.aarogya.one/api/v1"
    EXTERNAL_API_KEY_DEV: Optional[str] = None
    EXTERNAL_API_KEY_PROD: Optional[str] = None
    EXTERNAL_API_TIMEOUT: int = 30

    # API Endpoint paths
    DOCTORS_LIST_ENDPOINT: str = "/doctors"
    DOCTOR_FACILITIES_ENDPOINT: str = "/doctors"  # /{id}/facilities appended dynamically
    DOCTORS_AVAILABILITY_ENDPOINT: str = "/doctors/availability"
    BOOK_APPOINTMENT_ENDPOINT: str = "/appointment/schedule"

    # Computed properties
    @property
    def EXTERNAL_API_BASE_URL(self) -> str:
        if self.ENVIRONMENT == "prod":
            return self.EXTERNAL_API_BASE_URL_PROD
        return self.EXTERNAL_API_BASE_URL_DEV

    @property
    def EXTERNAL_API_KEY(self) -> Optional[str]:
        if self.ENVIRONMENT == "prod":
            return self.EXTERNAL_API_KEY_PROD
        return self.EXTERNAL_API_KEY_DEV

    # Default facility ID (required — Aarogya API needs this for doctor queries)
    DEFAULT_FACILITY_ID: str = "crmpo1ob0004991hpkvg"  # Tara Hospital default

    # FastAPI
    FASTAPI_HOST: str = "0.0.0.0"
    FASTAPI_PORT: int = 8000

settings = Settings()




