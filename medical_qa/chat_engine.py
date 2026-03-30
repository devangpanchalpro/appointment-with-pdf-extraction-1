import logging
from medical_qa.config import client, GEMINI_MODEL
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
    "8. Support Hindi and Gujarati language questions.\n"
)

SYS_NO_DOCS = (
    "You are a helpful and knowledgeable medical assistant for an Indian healthcare platform.\n\n"
    "The user has not uploaded any documents yet. Answer all medical questions clearly.\n\n"
    "GUIDELINES:\n"
    "- Provide detailed, evidence-based medical information\n"
    "- Explain medications: purpose, dosage, side effects, precautions\n"
    "- Always recommend consulting a qualified doctor for personal medical decisions\n"
    "- Support Hindi and Gujarati language questions\n"
)

SEP = "\n\n---\n\n"


def build_chat_answer(query: str, abha: str, history: list) -> str:
    logger.info(f"--- QA Chat Start [ABHA: {abha}] ---")
    logger.info(f"Query: {query}")
    
    user_files = rag.files(abha) if abha else []
    has_docs   = bool(user_files)
    logger.info(f"Has Documents: {has_docs} | Files: {user_files}")

    if has_docs:
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