"""
FastAPI Application
"""
import logging
import uuid
from datetime import datetime
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from app.agent.agent import appointment_agent, session_manager
from app.mcp.mcp_client import mcp_client
from app.api.doctors_cache import doctors_cache
from app.config.settings import settings
from fastapi import Depends, Body
from app.api.auth import verify_jwt

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
logger = logging.getLogger(__name__)

from contextlib import asynccontextmanager

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(f"🚀 {settings.APP_NAME} started")
    logger.info(f"📖 Docs: http://localhost:{settings.FASTAPI_PORT}/docs")
    
    # Pre-fetch doctors on startup (DISABLED FOR DEBUGGING CONNECTION ISSUES)
    # try:
    #     import asyncio
    #     doctors = await asyncio.wait_for(doctors_cache.get_doctors(), timeout=5.0)
    #     logger.info(f"✅ Cached {len(doctors)} doctors from Aarogya API")
    # except Exception as e:
    #     logger.warning(f"⚠️ Could not pre-fetch doctors on startup: {e}")
    
    yield
    
    # Shutdown logic here if needed
    logger.info("🛑 Shutting down application")

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    lifespan=lifespan,
)

# ── CORS Middleware ──────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Models ────────────────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    session_id: Optional[str] = None
    message: str


class ChatResponse(BaseModel):
    session_id: str
    response: str
    appointment_booked: bool = False
    booking_details: Optional[dict] = {}
    timestamp: str


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/", tags=["General"])
async def root(token: dict = Depends(verify_jwt)):
    return {
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "docs": "/docs",
        "endpoints": {
            "chat": "POST /chat",
            "doctors": "GET /doctors",
            "health": "GET /health",
            "token": "POST /token"
        }
    }


@app.get("/health", tags=["General"])
async def health(token: dict = Depends(verify_jwt)):
    import httpx
    ollama_ok = False
    try:
        async with httpx.AsyncClient(timeout=3) as c:
            r = await c.get(f"{settings.OLLAMA_BASE_URL}/api/tags")
            ollama_ok = r.status_code == 200
    except:
        pass
    
    tools = await mcp_client.list_tools()
    
    return {
        "status": "healthy",
        "ollama": "running" if ollama_ok else "offline",
        "mcp_tools": len(tools),
        "external_api": settings.EXTERNAL_API_BASE_URL,
    }


@app.get("/doctors", tags=["Doctors"])
async def get_doctors(
    facility_id: Optional[str] = None,
    refresh: bool = False,
    token: dict = Depends(verify_jwt)
):
    """
    GET /doctors — Legacy endpoint (cached availability data).
    """
    doctors = await doctors_cache.get_doctors(facility_id, force_refresh=refresh)
    return {
        "count": len(doctors),
        "doctors": doctors,
    }


# ── MCP-aligned 4-Step Booking API ───────────────────────────────────────────

from app.api.external_client import aarogya_api as api_client


@app.get("/api/doctors-list", tags=["Booking Flow"])
async def api_get_doctors_list(
    facility_id: Optional[str] = None,
    page_size: int = 20,
    skip_count: int = 0,
    token: dict = Depends(verify_jwt),
):
    """
    Step 1: GET /api/doctors-list
    Fetches the full list of available doctors at a facility.
    """
    from app.mcp.mcp_server import get_doctors_list
    result = await get_doctors_list(
        facility_id=facility_id,
        page_size=page_size,
        skip_count=skip_count,
    )
    import json
    return json.loads(result)


@app.get("/api/doctor-facilities", tags=["Booking Flow"])
async def api_get_doctor_facilities(
    health_professional_id: str,
    token: dict = Depends(verify_jwt),
):
    """
    Step 2: GET /api/doctor-facilities
    Fetches all facilities where a specific doctor is available.
    """
    from app.mcp.mcp_server import get_doctor_facilities
    result = await get_doctor_facilities(
        health_professional_id=health_professional_id,
    )
    import json
    return json.loads(result)


@app.get("/api/doctor-availability", tags=["Booking Flow"])
async def api_get_doctor_availability(
    health_professional_id: str,
    facility_id: str,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
    token: dict = Depends(verify_jwt),
):
    """
    Step 3: GET /api/doctor-availability
    Fetches available appointment slots for a doctor at a facility.
    """
    from app.mcp.mcp_server import get_doctor_availability
    result = await get_doctor_availability(
        health_professional_id=health_professional_id,
        facility_id=facility_id,
        from_date=from_date,
        to_date=to_date,
    )
    import json
    return json.loads(result)


class BookAppointmentRequest(BaseModel):
    first_name: str
    last_name: str
    mobile: str
    gender: int = 1
    birth_date: str
    health_professional_id: str
    facility_id: str
    slot_date: str
    slot_start_time: str
    symptoms: Optional[list] = None
    middle_name: str = ""
    pin_code: str = ""
    address: str = ""
    area: str = ""


@app.post("/api/appointments/schedule", tags=["Booking Flow"])
async def api_book_appointment(
    req: BookAppointmentRequest,
    token: dict = Depends(verify_jwt),
):
    """
    Step 4: POST /api/appointments/schedule
    Books the selected appointment slot with all patient details.
    """
    from app.mcp.mcp_server import book_appointment
    result = await book_appointment(
        first_name=req.first_name,
        last_name=req.last_name,
        mobile=req.mobile,
        gender=req.gender,
        birth_date=req.birth_date,
        health_professional_id=req.health_professional_id,
        facility_id=req.facility_id,
        slot_date=req.slot_date,
        slot_start_time=req.slot_start_time,
        symptoms=req.symptoms,
        middle_name=req.middle_name,
        pin_code=req.pin_code,
        address=req.address,
        area=req.area,
    )
    import json
    return json.loads(result)


@app.post("/chat", response_model=ChatResponse, tags=["Agent"])
async def chat(req: ChatRequest, token: dict = Depends(verify_jwt)):
    """
    POST /chat
    
    Chat with the Medical Appointment Booking Agent.
    """
    sid = req.session_id or str(uuid.uuid4())
    
    if not req.message.strip():
        raise HTTPException(400, "Message cannot be empty")
    
    logger.info(f"CHAT | session={sid} | msg={req.message[:80]}")
    
    try:
        result = await appointment_agent.chat(sid, req.message)
        return ChatResponse(
            session_id=sid,
            response=result["response"],
            appointment_booked=result.get("appointment_booked", False),
            booking_details=result.get("booking_details", {}),
            timestamp=datetime.now().isoformat(),
        )
    except Exception as e:
        logger.error(f"Chat error: {e}", exc_info=True)
        raise HTTPException(500, str(e))



@app.get("/session/{session_id}", tags=["Session"])
async def get_session(session_id: str, token: dict = Depends(verify_jwt)):
    s = session_manager.get(session_id)
    return {
        "session_id": session_id,
        "collected_info": s.get("collected"),
        "message_count": len(s.get("messages", [])),
        "booked": s.get("booked", False),
    }


@app.delete("/session/{session_id}", tags=["Session"])
async def reset_session(session_id: str, token: dict = Depends(verify_jwt)):
    appointment_agent.reset(session_id)
    return {"message": "Session reset", "session_id": session_id}









from medical_qa.route import register_qa_routes
register_qa_routes(app)


    # f0c6129e-582e-4012-aa9a-f88547a1d6bc