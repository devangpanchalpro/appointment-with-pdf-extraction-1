import logging
import os
import json
from medical_qa.config import client, GEMINI_MODEL, BASE_DIR
from medical_qa.rag_store import rag

logger = logging.getLogger(__name__)

SYS_WITH_DOCS = (
    "You are an expert medical assistant and report analyst for an Indian healthcare platform.\n\n"
    "The patient has uploaded their personal medical documents. Use the following approach:\n\n"
    "ANSWER STRATEGY:\n"
    "1. FIRST check the provided document context for the answer.\n"
    "2. If the exact answer IS in the documents → quote it with exact values.\n"
    "3. If the question needs more explanation → answer from documents AND supplement with medical knowledge.\n"
    "4. If the question is NOT in documents → answer from medical knowledge and mention it.\n"
    "5. Flag abnormal results clearly with ⚠️\n"
    "6. Always cite which document/file you found data in.\n"
    "7. Never fabricate values.\n"
    "8. IMPORTANT: ALWAYS answer in the exact same language as the user's question. If asked in English, reply in English. If asked in Hindi, reply in Hindi.\n"
)

SYS_NO_DOCS = (
    "You are a helpful and knowledgeable medical assistant for an Indian healthcare platform.\n\n"
    "The user has not uploaded any documents yet. Answer all medical questions clearly.\n\n"
    "GUIDELINES:\n"
    "- Provide detailed, evidence-based medical information\n"
    "- Explain medications: purpose, dosage, side effects, precautions\n"
    "- Always recommend consulting a qualified doctor for personal medical decisions\n"
    "- IMPORTANT: ALWAYS answer in the exact same language as the user's question. If asked in English, reply in English.\n"
)

SEP = "\n\n---\n\n"


def build_chat_answer(query: str, abha: str, history: list) -> str:
    logger.info(f"--- QA Chat Start [ABHA: {abha}] ---")
    logger.info(f"Query: {query}")
    
    user_files = rag.files(abha) if abha else []
    
    # Fallback to check disk if RAG is empty (due to restart/persistence)
    if not user_files and abha:
        user_dir = os.path.join(BASE_DIR, abha)
        if os.path.exists(user_dir):
            user_files = [f for f in os.listdir(user_dir) if f.endswith(".json")]
            
    has_docs   = bool(user_files)
    logger.info(f"Has Documents: {has_docs} | Files: {user_files}")

    if has_docs:
        # Check if user wants a summary of all documents
        q_lower = query.lower()
        is_summary = any(w in q_lower for w in ["summary", "summarize", "all document", "all report", "badha document", "badha report", "saare document", "sare document", "saare report", "saransh"])
        
        user_dir = os.path.join(BASE_DIR, abha)
        if is_summary and os.path.exists(user_dir):
            logger.info("Summary requested: loading all JSON data unconditionally.")
            jsons_data = []
            for fname in os.listdir(user_dir):
                if fname.endswith(".json"):
                    try:
                        with open(os.path.join(user_dir, fname), "r", encoding="utf-8") as f:
                            jsons_data.append(f"--- {fname} ---\n{f.read()}")
                    except: pass
            context = "\n\n".join(jsons_data) if jsons_data else "No readable data found."
            hits = [{"meta": {"filename": f}} for f in user_files]  # Fake hits for citation
        else:
            hits    = rag.search(query, abha, top_k=7)
            logger.info(f"RAG Search: {len(hits)} hits found")
            context = SEP.join(h["text"] for h in hits) if hits else "No relevant context found."
            
        system  = SYS_WITH_DOCS
        user_content = (
            f"Patient ABHA: {abha}\n"
            f"Uploaded documents: {', '.join(user_files)}\n\n"
            f"=== DOCUMENT CONTEXT ===\n{context}\n=== END CONTEXT ===\n\n"
            f"Patient question: {query}"
        )
    else:
        system       = SYS_NO_DOCS
        user_content = query
        hits         = []

    contents = [system]
    for turn in history[-6:]:
        u = turn[0] or ""
        a = turn[1] or ""
        if u: contents.append(f"User: {u}")
        if a: contents.append(f"Assistant: {a}")
    contents.append(f"User: {user_content}")

    try:
        logger.info("Calling Gemini for QA...")
        resp   = client.models.generate_content(model=GEMINI_MODEL, contents=contents)
        answer = resp.text.strip()
        if has_docs and hits:
            srcs   = list({h["meta"]["filename"] for h in hits})
            answer += f"\n\n📄 *Source: {', '.join(srcs)}*"
            logger.info(f"Answer generated. Sources: {srcs}")
        
        logger.info(f"QA Answer: {answer[:100]}...")
        logger.info(f"--- QA Chat End [ABHA: {abha}] ---")
        return answer
    except Exception as e:
        logger.error(f"Error calling Gemini in QA: {e}")
        return f"❌ Error calling Gemini: {e}"