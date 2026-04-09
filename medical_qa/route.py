import re, os, shutil
from pathlib import Path
from fastapi import UploadFile, File, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, validator
from medical_qa.config import BASE_DIR
from medical_qa.document_parse import SUPPORTED_EXTS
from medical_qa.rag_store import rag
from medical_qa.file_processor import parsed_store, upload_progress, process_bytes_bg
from medical_qa.chat_engine import build_chat_answer


class ChatRequest(BaseModel):
    abha_number: str = ""
    query: str
    top_k: int = 4
    history: list = []

    @validator("abha_number")
    def check_abha(cls, v):
        if not v or v.strip() == "":
            return ""
        digits = re.sub(r"[-\s]", "", v.strip())
        if not digits.isdigit() or len(digits) != 14:
            raise ValueError("ABHA number must be exactly 14 digits")
        return digits


def register_qa_routes(app):

    @app.post("/api/qa/upload/{abha_number}")
    async def qa_upload(abha_number: str, file: UploadFile = File(...)):
        digits = re.sub(r"[-\s]", "", abha_number.strip())
        if not digits.isdigit() or len(digits) != 14:
            raise HTTPException(400, "ABHA number must be exactly 14 digits")

        ext = Path(file.filename).suffix.lower()
        if ext not in SUPPORTED_EXTS:
            raise HTTPException(400, f"Unsupported file extension: {ext}")

        data = await file.read()
        process_bytes_bg(data, file.filename, digits)

        return {
            "abha_number": digits,
            "filename": file.filename,
            "message": "1 file queued for processing",
            "chat_status": "Chat available immediately!"
        }


    @app.post("/api/qa/chat/")
    async def qa_chat(req: ChatRequest):
        answer  = build_chat_answer(req.query, req.abha_number, req.history)
        sources = rag.files(req.abha_number) if req.abha_number else []
        return {
            "answer":   answer,
            "sources":  sources,
            "has_docs": bool(sources),
            "abha":     req.abha_number
        }

    @app.get("/api/qa/files/{abha_number}")
    async def qa_files(abha_number: str):
        digits = re.sub(r"[-\s]", "", abha_number.strip())
        if not digits.isdigit() or len(digits) != 14:
            raise HTTPException(400, "Invalid ABHA number")
        indexed  = rag.files(digits)
        user_dir = os.path.join(BASE_DIR, digits)
        if os.path.exists(user_dir):
            all_files = sorted(os.listdir(user_dir))
            saved_jsons = [f for f in all_files if f.endswith(".json")]
            original_docs = [f for f in all_files if not f.endswith(".json")]
        else:
            saved_jsons = []
            original_docs = []
            
        return {
            "abha_number":      digits,
            "indexed_files":    indexed,
            "saved_json_files": saved_jsons,
            "original_documents": original_docs,
            "processing_status": upload_progress.get(digits, {})
        }

    @app.get("/api/qa/download/{abha_number}/{filename}")
    async def qa_download(abha_number: str, filename: str):
        digits = re.sub(r"[-\s]", "", abha_number.strip())
        path   = os.path.join(BASE_DIR, digits, filename)
        if not os.path.exists(path):
            raise HTTPException(404, f"File not found: {filename}")
        return FileResponse(path, media_type="application/json", filename=filename)

    @app.get("/api/qa/upload-form", response_class=HTMLResponse)
    async def upload_form():
        return """
        <html><body>
          <h3>Single PDF/Image upload (only one file)</h3>
          <form action="/api/qa/upload/12345678901234" method="post" enctype="multipart/form-data">
            <input type="file" name="file" accept=".pdf,.jpg,.jpeg,.png,.gif,.webp,.bmp,.tiff">
            <br><br><button type="submit">Upload</button>
          </form>
        </body></html>
        """


    @app.delete("/api/qa/clear/{abha_number}")
    async def qa_clear(abha_number: str):
        digits = re.sub(r"[-\s]", "", abha_number.strip())
        rag.clear(digits)
        parsed_store.pop(digits, None)
        upload_progress.pop(digits, None)
        user_dir = os.path.join(BASE_DIR, digits)
        if os.path.exists(user_dir):
            shutil.rmtree(user_dir)
        return {"message": f"All data cleared for ABHA {digits}"}