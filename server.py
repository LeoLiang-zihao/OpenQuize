"""
FastAPI server for Open Anki Quiz.
"""

import os
import json
import shutil
from pathlib import Path
from contextlib import asynccontextmanager

from dotenv import load_dotenv
load_dotenv()

from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

import database as db
from llm_service import extract_questions_from_pdf, generate_questions_from_prompt, chat_with_context_stream

UPLOAD_DIR = Path(__file__).parent / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield

app = FastAPI(title="Open Anki Quiz", lifespan=lifespan)


# ── API Routes ──

@app.post("/api/upload")
async def upload_pdf(
    file: UploadFile = File(...),
    prompt: str = Form(""),
    name: str = Form(""),
    references: str = Form(""),  # comma-separated paths
):
    """Upload a PDF, extract questions via LLM, store in DB."""
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are supported")

    # Save uploaded file
    save_path = UPLOAD_DIR / file.filename
    with open(save_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    # Parse reference paths
    ref_paths = [p.strip() for p in references.split(",") if p.strip()] if references else []

    # Create question set
    set_name = name or Path(file.filename).stem
    set_id = db.create_question_set(set_name, str(save_path), prompt)

    try:
        # Extract questions via LLM agent
        questions = await extract_questions_from_pdf(
            str(save_path), prompt, ref_paths
        )

        # Store each question
        for q in questions:
            db.add_question(
                set_id=set_id,
                question_text=q["question_text"],
                options=q["options"],
                correct_index=q["correct_index"],
                explanation=q["explanation"],
                category=q.get("category", ""),
            )

        return {
            "set_id": set_id,
            "name": set_name,
            "question_count": len(questions),
        }
    except Exception as e:
        raise HTTPException(500, f"Failed to extract questions: {str(e)}")


class ReorderRequest(BaseModel):
    ordered_ids: list[int]


@app.get("/api/sets")
async def list_sets():
    """List all question sets."""
    return db.get_all_question_sets()


@app.delete("/api/sets/{set_id}")
async def delete_set(set_id: int):
    """Delete a question set and its associated data."""
    # Get source file path before deleting
    conn = db.get_conn()
    row = conn.execute(
        "SELECT source_file FROM question_sets WHERE id = ?", (set_id,)
    ).fetchone()
    conn.close()

    if not row:
        raise HTTPException(404, "Question set not found")

    # Delete uploaded PDF if it exists
    if row["source_file"]:
        pdf_path = Path(row["source_file"])
        if pdf_path.exists():
            pdf_path.unlink()

    db.delete_question_set(set_id)
    return {"ok": True}


@app.put("/api/sets/reorder")
async def reorder_sets(req: ReorderRequest):
    """Reorder question sets."""
    db.reorder_question_sets(req.ordered_ids)
    return {"ok": True}


@app.get("/api/sets/{set_id}/questions")
async def get_questions(set_id: int):
    """Get all questions in a set."""
    return db.get_questions_by_set(set_id)


@app.get("/api/sets/{set_id}/review")
async def get_review_queue(set_id: int):
    """Get unmastered questions for review."""
    return db.get_review_queue(set_id)


@app.get("/api/sets/{set_id}/stats")
async def get_stats(set_id: int):
    """Get progress stats for a set."""
    return db.get_set_stats(set_id)


@app.get("/api/sets/{set_id}/categories")
async def get_categories(set_id: int):
    """Get distinct categories for a question set."""
    return db.get_categories_for_set(set_id)


class AnswerRequest(BaseModel):
    question_id: int
    selected_index: int


@app.post("/api/answer")
async def submit_answer(req: AnswerRequest):
    """Submit an answer and get result."""
    # Get the question to check correctness
    conn = db.get_conn()
    row = conn.execute(
        "SELECT correct_index FROM questions WHERE id = ?", (req.question_id,)
    ).fetchone()
    conn.close()

    if not row:
        raise HTTPException(404, "Question not found")

    is_correct = req.selected_index == row["correct_index"]
    db.record_answer(req.question_id, is_correct)

    return {"correct": is_correct, "correct_index": row["correct_index"]}


class MasterRequest(BaseModel):
    question_id: int
    mastered: bool = True


@app.post("/api/master")
async def toggle_master(req: MasterRequest):
    """Mark/unmark a question as mastered."""
    db.mark_mastered(req.question_id, req.mastered)
    return {"ok": True}


# ── Append questions to existing set ──

@app.post("/api/sets/{set_id}/append")
async def append_questions(
    set_id: int,
    prompt: str = Form(...),
    file: UploadFile | None = File(None),
    references: str = Form(""),
):
    """Append new questions to an existing set."""
    # Verify set exists
    conn = db.get_conn()
    row = conn.execute("SELECT id FROM question_sets WHERE id = ?", (set_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Question set not found")

    ref_paths = [p.strip() for p in references.split(",") if p.strip()] if references else []

    try:
        if file and file.filename:
            # Has PDF — use PDF extraction
            if not file.filename.lower().endswith(".pdf"):
                raise HTTPException(400, "Only PDF files are supported")
            save_path = UPLOAD_DIR / file.filename
            with open(save_path, "wb") as f:
                shutil.copyfileobj(file.file, f)
            questions = await extract_questions_from_pdf(str(save_path), prompt, ref_paths)
        else:
            # No PDF — generate from prompt
            questions = await generate_questions_from_prompt(prompt, ref_paths)

        added = 0
        for q in questions:
            db.add_question(
                set_id=set_id,
                question_text=q["question_text"],
                options=q["options"],
                correct_index=q["correct_index"],
                explanation=q["explanation"],
                category=q.get("category", ""),
            )
            added += 1

        return {"added_count": added}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Failed to generate questions: {str(e)}")


# ── Chat ──

@app.get("/api/sets/{set_id}/chat/history")
async def get_chat_history(set_id: int):
    """Get chat history for a set."""
    return db.get_chat_history(set_id)


class ChatRequest(BaseModel):
    message: str
    question_id: int | None = None


@app.post("/api/sets/{set_id}/chat/stream")
async def chat_stream(set_id: int, req: ChatRequest):
    """Stream a chat response via SSE."""
    # Save user message
    db.add_chat_message(set_id, "user", req.message, req.question_id)

    # Get history for context
    history = db.get_chat_history(set_id)

    # Get question context if provided
    question_context = None
    if req.question_id:
        conn = db.get_conn()
        qrow = conn.execute(
            "SELECT * FROM questions WHERE id = ?", (req.question_id,)
        ).fetchone()
        conn.close()
        if qrow:
            question_context = dict(qrow)
            question_context["options"] = json.loads(question_context["options"])
            question_context["explanation"] = json.loads(question_context["explanation"])

    async def event_generator():
        full_response = []
        try:
            async for delta in chat_with_context_stream(
                req.message, history, question_context
            ):
                full_response.append(delta)
                yield f"data: {json.dumps({'delta': delta})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

        # Save the complete assistant response
        complete = "".join(full_response)
        if complete:
            db.add_chat_message(set_id, "assistant", complete, req.question_id)

        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


# ── Static files (frontend) ──

app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


@app.get("/")
async def index():
    return FileResponse(str(Path(__file__).parent / "static" / "index.html"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=True)
