import asyncio
import logging
from typing import Dict, List
from medical_qa.document_parse import doc_parser, SUPPORTED_EXTS
from medical_qa.rag_store import rag

logger = logging.getLogger(__name__)

parsed_store:    Dict[str, List[dict]] = {}
upload_progress: Dict[str, Dict[str, str]] = {}


async def _process_single(fpath_or_bytes, filename: str, abha: str, is_bytes=False):
    logger.info(f"--- Processing Start [File: {filename}, ABHA: {abha}] ---")
    upload_progress.setdefault(abha, {})[filename] = "⏳ parsing..."
    try:
        if is_bytes:
            logger.info(f"Parsing from bytes: {filename}")
            result = await doc_parser.parse_bytes(fpath_or_bytes, filename)
        else:
            logger.info(f"Parsing from path: {fpath_or_bytes}")
            result = await doc_parser.parse_path(fpath_or_bytes)

        logger.info(f"Extraction complete for {filename}. Extracting text for RAG...")
        text = doc_parser.full_text_for_rag(result)
        rag.add(text, filename, abha)
        doc_parser.save_json(result, abha)
        parsed_store.setdefault(abha, []).append(result)

        dtype = result.get("document_type", "unknown")
        conf  = result.get("overall_confidence", "?")
        pages = len(result.get("pages", []))
        upload_progress[abha][filename] = f"✅ {dtype} | conf:{conf} | {pages}p"
        logger.info(f"✅ Success: {filename} → {dtype} (conf: {conf}, pages: {pages})")
        logger.info(f"--- Processing End [File: {filename}] ---")
        return result
    except Exception as e:
        upload_progress[abha][filename] = f"❌ {str(e)[:60]}"
        logger.error(f"❌ Error processing {filename}: {e}", exc_info=True)
        err = {"source_file": filename, "error": str(e), "pages": []}
        parsed_store.setdefault(abha, []).append(err)
        return err


def process_files_bg(file_paths: List[str], abha: str):
    tasks = []
    for fp in file_paths:
        import os
        fname = os.path.basename(fp)
        # Use asyncio.create_task for non-blocking execution in the event loop
        t = asyncio.create_task(_process_single(fp, fname, abha, False))
        tasks.append((t, fname))
    return tasks


def process_bytes_bg(data: bytes, filename: str, abha: str):
    # Use asyncio.create_task for non-blocking execution in the event loop
    return asyncio.create_task(_process_single(data, filename, abha, True))