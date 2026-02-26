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

from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

import database as db
from llm_service import (
    extract_questions_from_pdf,
    generate_questions_from_prompt,
    chat_with_context_stream,
    extract_open_questions_from_file,
    generate_answer_for_question,
    guided_dialogue_stream,
    transcribe_audio,
)

UPLOAD_DIR = Path(__file__).parent / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    yield

app = FastAPI(title="Open Anki Quiz", lifespan=lifespan)


# ── API Routes ──

@app.post("/api/upload")
async def upload_file(
    files: list[UploadFile] = File(...),
    prompt: str = Form(""),
    name: str = Form(""),
    references: str = Form(""),  # comma-separated paths
    set_type: str = Form("mcq"),  # 'mcq' or 'open'
):
    """Upload one or more PDF/DOCX files, extract questions via LLM, store in DB."""
    allowed_ext = (".pdf", ".docx")

    # Validate all files first
    for f in files:
        if not any(f.filename.lower().endswith(ext) for ext in allowed_ext):
            raise HTTPException(400, f"Only PDF and DOCX files are supported (got {f.filename})")

    # Parse reference paths
    ref_paths = [p.strip() for p in references.split(",") if p.strip()] if references else []

    # Create question set (use provided name, or first file's stem)
    set_name = name or Path(files[0].filename).stem
    first_save = UPLOAD_DIR / files[0].filename
    set_id = db.create_question_set(set_name, str(first_save), prompt, set_type=set_type)

    try:
        for uploaded in files:
            # Save uploaded file
            save_path = UPLOAD_DIR / uploaded.filename
            with open(save_path, "wb") as f:
                shutil.copyfileobj(uploaded.file, f)

            if set_type == "open":
                questions = await extract_open_questions_from_file(
                    str(save_path), prompt, ref_paths
                )
                for q in questions:
                    answer = await generate_answer_for_question(q["question_text"])
                    db.add_question(
                        set_id=set_id,
                        question_text=q["question_text"],
                        options=[],
                        correct_index=-1,
                        explanation=answer,
                        category=q.get("category", ""),
                        q_type="open",
                    )
            else:
                questions = await extract_questions_from_pdf(
                    str(save_path), prompt, ref_paths
                )
                for q in questions:
                    db.add_question(
                        set_id=set_id,
                        question_text=q["question_text"],
                        options=q["options"],
                        correct_index=q["correct_index"],
                        explanation=q["explanation"],
                        category=q.get("category", ""),
                        q_type="mcq",
                    )

        total = db.get_set_stats(set_id)["total"]
        return {
            "set_id": set_id,
            "name": set_name,
            "question_count": total,
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
    files: list[UploadFile] = File([]),
    references: str = Form(""),
):
    """Append new questions to an existing set (supports multiple files)."""
    conn = db.get_conn()
    row = conn.execute("SELECT id, set_type FROM question_sets WHERE id = ?", (set_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Question set not found")

    set_type = row["set_type"] or "mcq"
    ref_paths = [p.strip() for p in references.split(",") if p.strip()] if references else []
    allowed_ext = (".pdf", ".docx")

    # Filter out empty file entries (browser sends empty entry when no file selected)
    valid_files = [f for f in files if f.filename]

    try:
        added = 0

        if valid_files:
            for uploaded in valid_files:
                if not any(uploaded.filename.lower().endswith(ext) for ext in allowed_ext):
                    raise HTTPException(400, f"Only PDF and DOCX files are supported (got {uploaded.filename})")
                save_path = UPLOAD_DIR / uploaded.filename
                with open(save_path, "wb") as f:
                    shutil.copyfileobj(uploaded.file, f)

                if set_type == "open":
                    questions = await extract_open_questions_from_file(str(save_path), prompt, ref_paths)
                else:
                    questions = await extract_questions_from_pdf(str(save_path), prompt, ref_paths)

                for q in questions:
                    if set_type == "open":
                        answer = await generate_answer_for_question(q["question_text"])
                        db.add_question(
                            set_id=set_id,
                            question_text=q["question_text"],
                            options=[],
                            correct_index=-1,
                            explanation=answer,
                            category=q.get("category", ""),
                            q_type="open",
                        )
                    else:
                        db.add_question(
                            set_id=set_id,
                            question_text=q["question_text"],
                            options=q["options"],
                            correct_index=q["correct_index"],
                            explanation=q["explanation"],
                            category=q.get("category", ""),
                            q_type="mcq",
                        )
                    added += 1
        else:
            questions = await generate_questions_from_prompt(prompt, ref_paths)
            for q in questions:
                if set_type == "open":
                    answer = await generate_answer_for_question(q["question_text"])
                    db.add_question(
                        set_id=set_id,
                        question_text=q["question_text"],
                        options=[],
                        correct_index=-1,
                        explanation=answer,
                        category=q.get("category", ""),
                        q_type="open",
                    )
                else:
                    db.add_question(
                        set_id=set_id,
                        question_text=q["question_text"],
                        options=q["options"],
                        correct_index=q["correct_index"],
                        explanation=q["explanation"],
                        category=q.get("category", ""),
                        q_type="mcq",
                    )
                added += 1

        return {"added_count": added}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(500, f"Failed to generate questions: {str(e)}")


# ── Chat ──

@app.get("/api/sets/{set_id}/chat/history")
async def get_chat_history(set_id: int, question_id: int | None = Query(None)):
    """Get chat history for a set, optionally filtered by question."""
    return db.get_chat_history(set_id, question_id=question_id)


@app.delete("/api/sets/{set_id}/chat")
async def delete_chat(set_id: int, question_id: int | None = Query(None)):
    """Delete chat history for a set, optionally filtered by question."""
    db.delete_chat_history(set_id, question_id=question_id)
    return {"ok": True}


class ChatRequest(BaseModel):
    message: str
    question_id: int | None = None


@app.post("/api/sets/{set_id}/chat/stream")
async def chat_stream(set_id: int, req: ChatRequest):
    """Stream a chat response via SSE."""
    # Save user message
    db.add_chat_message(set_id, "user", req.message, req.question_id)

    # Get history for context (filtered by question_id)
    history = db.get_chat_history(set_id, question_id=req.question_id)

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
            try:
                question_context["explanation"] = json.loads(question_context["explanation"])
            except (json.JSONDecodeError, TypeError):
                pass  # open questions store explanation as plain text

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


# ── Reset Progress ──

@app.post("/api/sets/{set_id}/reset")
async def reset_progress(set_id: int):
    """Reset all progress for a set so user can redo it."""
    db.reset_set_progress(set_id)
    return {"ok": True}


# ── Voice Transcription ──

@app.post("/api/transcribe")
async def transcribe_voice(file: UploadFile = File(...)):
    """Transcribe audio via OpenAI Whisper."""
    audio_data = await file.read()
    text = await transcribe_audio(audio_data, filename=file.filename)
    return {"text": text}


# ── Guided Dialogue (per-question chat for open questions) ──

@app.get("/api/questions/{question_id}/chat/history")
async def get_question_chat_history(question_id: int):
    """Get chat history for a specific question."""
    return db.get_question_chat_history(question_id)


@app.delete("/api/questions/{question_id}/chat")
async def delete_question_chat(question_id: int):
    """Delete guided dialogue chat history for a question."""
    db.delete_question_chat_history(question_id)
    return {"ok": True}


class GuidedChatRequest(BaseModel):
    message: str


@app.post("/api/questions/{question_id}/chat/stream")
async def guided_chat_stream(question_id: int, req: GuidedChatRequest):
    """Stream a guided dialogue response for an open question."""
    # Save user message
    db.add_question_chat_message(question_id, "user", req.message)

    # Get question details
    conn = db.get_conn()
    qrow = conn.execute("SELECT * FROM questions WHERE id = ?", (question_id,)).fetchone()
    conn.close()

    if not qrow:
        raise HTTPException(404, "Question not found")

    question_text = qrow["question_text"]
    answer_key = qrow["explanation"]  # For open questions, explanation stores the answer

    # Get chat history
    history = db.get_question_chat_history(question_id)

    async def event_generator():
        full_response = []
        try:
            async for delta in guided_dialogue_stream(
                req.message, history, question_text, answer_key
            ):
                full_response.append(delta)
                yield f"data: {json.dumps({'delta': delta})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)})}\n\n"

        complete = "".join(full_response)
        understood = "[UNDERSTOOD]" in complete
        if complete:
            db.add_question_chat_message(question_id, "assistant", complete)

        if understood:
            yield f"data: {json.dumps({'understood': True})}\n\n"

        yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
        },
    )


# ── Reveal Answer ──

@app.get("/api/questions/{question_id}/answer")
async def reveal_answer(question_id: int):
    """Get the answer for an open question (marks as seen)."""
    conn = db.get_conn()
    qrow = conn.execute(
        "SELECT explanation, type FROM questions WHERE id = ?", (question_id,)
    ).fetchone()
    conn.close()

    if not qrow:
        raise HTTPException(404, "Question not found")

    # Record as seen but not correct
    db.record_answer(question_id, False)

    return {"answer": qrow["explanation"], "type": qrow["type"]}


# ── Get set type ──

@app.get("/api/sets/{set_id}/type")
async def get_set_type(set_id: int):
    return {"set_type": db.get_set_type(set_id)}


# ── Static files (frontend) ──

app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


@app.get("/")
async def index():
    return FileResponse(str(Path(__file__).parent / "static" / "index.html"))


@app.get("/test-latex")
async def test_latex():
    return FileResponse(str(Path(__file__).parent / "static" / "test_latex.html"))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("server:app", host="127.0.0.1", port=8000, reload=True)
