import os, json, re, time, tempfile
from pathlib import Path
from datetime import datetime
from typing import List
from PIL import Image
from pdf2image import convert_from_path
from google.genai import types
from medical_qa.config import client, GEMINI_MODEL, BASE_DIR

SUPPORTED_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tiff"}
SUPPORTED_EXTS       = {".pdf"} | SUPPORTED_IMAGE_EXTS
MIME_MAP = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".gif": "image/gif",  ".webp": "image/webp",
    ".bmp": "image/png",  ".tiff": "image/png"
}

DYNAMIC_EXTRACTION_PROMPT = """You are an expert medical document analyst with perfect accuracy.

TASK: Analyze this document image completely and extract ALL information into a dynamic JSON structure.

ANTI-HALLUCINATION RULES (STRICTLY ENFORCED):
1. Extract ONLY what is VISIBLY PRESENT in the image — never guess, infer, or fabricate
2. Use null for any field that is blank, illegible, or not present
3. Preserve EXACT values — numbers, units, dates exactly as shown
4. If a value is partially visible, mark it with a "?" suffix
5. confidence_score = 0.0-1.0 per field

DYNAMIC JSON RULES:
- Detect document type automatically (lab_report, prescription, discharge_summary,
  radiology_report, vaccination_record, diagnostic_report, ecg_report, etc.)
- Build JSON structure that PERFECTLY FITS this specific document
- For tabular data use arrays of objects
- Each test result MUST include: test_name, value, unit, reference_range, status
- Medications MUST include: drug_name, dosage, frequency, route, duration

Return ONLY valid JSON (no markdown, no explanation):
{
  "document_type": "<detected_type>",
  "document_subtype": "<specific_type>",
  "language_detected": "<English|Hindi|Gujarati|Mixed>",
  "overall_confidence": "<high|medium|low>",
  "extraction_timestamp": "<ISO timestamp>",
  "patient_info": {},
  "document_data": {},
  "abnormal_flags": [],
  "handwritten_fields": [],
  "missing_critical_fields": [],
  "raw_text": "<complete verbatim text>"
}"""


class DynamicDocumentParser:

    def __init__(self, genai_client):
        self.client = genai_client
        self.model  = GEMINI_MODEL

    def _load_img_bytes(self, path: str):
        ext = Path(path).suffix.lower()
        if ext in (".bmp", ".tiff"):
            tmp = f"/tmp/conv_{Path(path).stem}.png"
            Image.open(path).save(tmp, "PNG")
            path = tmp
        with open(path, "rb") as f:
            return f.read(), MIME_MAP.get(ext, "image/jpeg")

    def _pdf_to_imgs(self, pdf_path: str) -> List[str]:
        pages = convert_from_path(pdf_path, dpi=220)
        paths = []
        for i, pg in enumerate(pages):
            p = f"/tmp/pg_{int(time.time()*1000)}_{i}.png"
            pg.save(p, "PNG")
            paths.append(p)
        print(f"   📄 PDF → {len(paths)} page(s)")
        return paths

    def _pdf_bytes_to_imgs(self, data: bytes) -> List[str]:
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as t:
            t.write(data)
            tpath = t.name
        return self._pdf_to_imgs(tpath)

    async def _call_gemini(self, img_bytes: bytes, mime: str, page_num: int) -> dict:
        try:
            # use await for the async call if the client supports it, 
            # but google-genai client.models.generate_content is normally sync in this version
            # let's check if it has an async version or use run_in_executor
            import asyncio
            loop = asyncio.get_event_loop()
            
            def call_sync():
                return self.client.models.generate_content(
                    model=self.model,
                    contents=[
                        types.Part.from_bytes(data=img_bytes, mime_type=mime),
                        DYNAMIC_EXTRACTION_PROMPT
                    ]
                )
            
            resp = await loop.run_in_executor(None, call_sync)
            
            raw = resp.text.strip()
            raw = re.sub(r"^```[a-zA-Z]*\n?", "", raw)
            raw = re.sub(r"\n?```$", "", raw).strip()
            start = raw.find("{")
            end   = raw.rfind("}") + 1
            if start != -1 and end > start:
                raw = raw[start:end]
            result = json.loads(raw)
            result["_page_number"] = page_num
            return result
        except json.JSONDecodeError as e:
            print(f"   ⚠️ Page {page_num}: JSON parse error — {e}")
            return {"_page_number": page_num, "document_type": "parse_error",
                    "error": "JSON parse failed"}
        except Exception as e:
            print(f"   ❌ Page {page_num}: {e}")
            return {"_page_number": page_num, "error": str(e)}

    async def parse_path(self, file_path: str) -> dict:
        fp  = Path(file_path)
        ext = fp.suffix.lower()
        print(f"\n📄 Parsing: {fp.name}")
        result = {"source_file": fp.name,
                  "extraction_timestamp": datetime.now().isoformat(),
                  "pages": []}
        
        tasks = []
        if ext == ".pdf":
            import asyncio
            loop = asyncio.get_event_loop()
            imgs = await loop.run_in_executor(None, self._pdf_to_imgs, str(fp))
            for i, p in enumerate(imgs):
                print(f"   🔍 Queueing Page {i+1}/{len(imgs)}...")
                b, m = self._load_img_bytes(p)
                tasks.append(self._call_gemini(b, m, i+1))
        elif ext in SUPPORTED_IMAGE_EXTS:
            b, m = self._load_img_bytes(str(fp))
            tasks.append(self._call_gemini(b, m, 1))
        else:
            result["error"] = f"Unsupported format: {ext}"
            return result

        if tasks:
            print(f"   ⚡ Processing {len(tasks)} pages in parallel...")
            result["pages"] = await asyncio.gather(*tasks)
        
        self._merge(result)
        return result

    async def parse_bytes(self, data: bytes, filename: str) -> dict:
        ext = Path(filename).suffix.lower()
        print(f"\n📄 Parsing: {filename}")
        result = {"source_file": filename,
                  "extraction_timestamp": datetime.now().isoformat(),
                  "pages": []}
        
        tasks = []
        if ext == ".pdf":
            import asyncio
            loop = asyncio.get_event_loop()
            imgs = await loop.run_in_executor(None, self._pdf_bytes_to_imgs, data)
            for i, p in enumerate(imgs):
                print(f"   🔍 Queueing Page {i+1}/{len(imgs)}...")
                b, m = self._load_img_bytes(p)
                tasks.append(self._call_gemini(b, m, i+1))
        elif ext in SUPPORTED_IMAGE_EXTS:
            m = MIME_MAP.get(ext, "image/jpeg")
            tasks.append(self._call_gemini(data, m, 1))
        else:
            result["error"] = f"Unsupported format: {ext}"
            return result

        if tasks:
            print(f"   ⚡ Processing {len(tasks)} pages in parallel...")
            result["pages"] = await asyncio.gather(*tasks)

        self._merge(result)
        return result

    def _merge(self, result: dict):
        if not result.get("pages"): return
        first = result["pages"][0]
        # Copy top-level fields from first page to result root
        for k in ["document_type", "document_subtype", "patient_info", "overall_confidence"]:
            if k in first: result[k] = first[k]
        
    def full_text_for_rag(self, result: dict) -> str:
        texts = []
        for pg in result.get("pages", []):
            if "raw_text" in pg:
                texts.append(pg["raw_text"])
        return "\n\n".join(texts)

    def save_json(self, result: dict, abha: str):
        out_dir = Path(BASE_DIR) / "data" / "extractions" / abha
        out_dir.mkdir(parents=True, exist_ok=True)
        fname = Path(result["source_file"]).stem + ".json"
        with open(out_dir / fname, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2)


doc_parser = DynamicDocumentParser(client)