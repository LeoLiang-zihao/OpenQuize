"""
LLM service using OpenAI Agents SDK to extract questions from PDFs
and generate bilingual explanations.
"""

import json
import fitz  # PyMuPDF
from pathlib import Path
from pydantic import BaseModel, Field
from agents import Agent, Runner, function_tool
from openai import AsyncOpenAI


# ── Pydantic models for structured output ──

class BilingualText(BaseModel):
    en: str = Field(description="English explanation")
    zh: str = Field(description="Chinese explanation")


class OptionExplanations(BaseModel):
    a: BilingualText = Field(description="Explanation for option A")
    b: BilingualText = Field(description="Explanation for option B")
    c: BilingualText = Field(description="Explanation for option C")
    d: BilingualText = Field(description="Explanation for option D")


class Explanation(BaseModel):
    correct: BilingualText = Field(description="Why the correct answer is correct")
    options: OptionExplanations = Field(description="Explanation for each option")


class QuestionItem(BaseModel):
    question_text: str = Field(description="The full question text")
    options: list[str] = Field(description="List of answer options, e.g. ['Case series', 'Expert opinion', ...]")
    correct_index: int = Field(description="0-based index of the correct answer")
    category: str = Field(description="Topic category of this question")
    explanation: Explanation = Field(description="Bilingual explanations for the correct answer and each option")


class ExtractedQuestions(BaseModel):
    questions: list[QuestionItem]


# ── PDF text extraction ──

def extract_pdf_text(file_path: str) -> str:
    doc = fitz.open(file_path)
    text_parts = []
    for page_num, page in enumerate(doc):
        text = page.get_text()
        if text.strip():
            text_parts.append(f"--- Page {page_num + 1} ---\n{text}")
    doc.close()
    return "\n\n".join(text_parts)


# ── Reference material loading ──

def load_reference_texts(reference_paths: list[str]) -> str:
    """Load reference PDFs for the agent to consult."""
    refs = []
    for p in reference_paths:
        if Path(p).exists():
            text = extract_pdf_text(p)
            refs.append(f"=== Reference: {Path(p).name} ===\n{text}")
    return "\n\n".join(refs)


# ── Main extraction function ──

async def extract_questions_from_pdf(
    pdf_path: str,
    user_prompt: str = "",
    reference_paths: list[str] | None = None,
) -> list[dict]:
    """
    Use OpenAI Agents SDK to extract questions from a PDF
    and generate bilingual explanations.
    """
    # Extract text
    pdf_text = extract_pdf_text(pdf_path)

    # Load references if provided
    ref_text = ""
    if reference_paths:
        ref_text = load_reference_texts(reference_paths)

    # Build the agent
    instructions = """You are an expert educational content analyzer. Your job is to:

1. Extract ALL multiple-choice questions from the provided PDF text.
2. For each question, identify:
   - The full question text
   - All answer options (as a list)
   - The correct answer (as a 0-based index)
   - A topic category
3. Generate concise, elegant explanations in BOTH English and Chinese:
   - Why the correct answer is correct
   - Why each other option is incorrect

Use the reference materials (if provided) to verify your answers.

IMPORTANT:
- Be precise about which answer is correct. Cross-reference with lecture materials.
- Explanations should be concise but thorough - help the student truly understand.
- Chinese explanations (zh) should be natural Chinese, not machine-translated.
- The "options" in explanation should use keys "a", "b", "c", "d" etc.
"""

    agent = Agent(
        name="Question Extractor",
        instructions=instructions,
        model="gpt-5.2-codex",
        output_type=ExtractedQuestions,
    )

    # Build the user message
    user_msg_parts = []
    if user_prompt:
        user_msg_parts.append(f"User instruction: {user_prompt}")
    user_msg_parts.append(f"PDF Content:\n{pdf_text}")
    if ref_text:
        user_msg_parts.append(f"Reference Materials:\n{ref_text}")

    user_msg = "\n\n".join(user_msg_parts)

    # Run the agent
    result = await Runner.run(agent, user_msg)
    extracted: ExtractedQuestions = result.final_output

    # Convert to dicts
    return [q.model_dump() for q in extracted.questions]


# ── Generate questions from prompt (no PDF required) ──

async def generate_questions_from_prompt(
    prompt: str,
    reference_paths: list[str] | None = None,
) -> list[dict]:
    """
    Generate questions based on a text prompt (optionally with reference PDFs).
    """
    ref_text = ""
    if reference_paths:
        ref_text = load_reference_texts(reference_paths)

    instructions = """You are an expert educational content creator. Your job is to:

1. Generate multiple-choice questions based on the user's description/prompt.
2. For each question, provide:
   - The full question text
   - 4 answer options (as a list)
   - The correct answer (as a 0-based index)
   - A topic category
3. Generate concise, elegant explanations in BOTH English and Chinese:
   - Why the correct answer is correct
   - Why each other option is incorrect

Use the reference materials (if provided) to ensure accuracy.

IMPORTANT:
- Generate high-quality, exam-style questions that test understanding.
- Explanations should be concise but thorough.
- Chinese explanations (zh) should be natural Chinese, not machine-translated.
- The "options" in explanation should use keys "a", "b", "c", "d" etc.
"""

    agent = Agent(
        name="Question Generator",
        instructions=instructions,
        model="gpt-5.2-codex",
        output_type=ExtractedQuestions,
    )

    user_msg_parts = [f"User instruction: {prompt}"]
    if ref_text:
        user_msg_parts.append(f"Reference Materials:\n{ref_text}")

    user_msg = "\n\n".join(user_msg_parts)

    result = await Runner.run(agent, user_msg)
    extracted: ExtractedQuestions = result.final_output

    return [q.model_dump() for q in extracted.questions]


# ── Chat with context (streaming) ──

async def chat_with_context_stream(
    message: str,
    history: list[dict],
    question_context: dict | None = None,
):
    """
    Async generator that yields streaming deltas for chat responses.
    Uses openai.AsyncOpenAI directly for simplicity.
    """
    client = AsyncOpenAI()

    system_parts = [
        "You are a helpful AI study assistant. The student is reviewing quiz questions. "
        "Answer their questions concisely. You can explain concepts, translate terms, "
        "clarify confusing topics, etc. Respond in the same language the student uses, "
        "or use bilingual (English + Chinese) when helpful."
    ]

    if question_context:
        labels = "abcdefghij"
        options_text = "\n".join(
            f"  {labels[i].upper()}. {opt}"
            for i, opt in enumerate(question_context.get("options", []))
        )
        system_parts.append(
            f"\nCurrent question context:\n"
            f"Category: {question_context.get('category', 'N/A')}\n"
            f"Question: {question_context.get('question_text', '')}\n"
            f"Options:\n{options_text}\n"
            f"Correct answer index: {question_context.get('correct_index', '?')}"
        )

        explanation = question_context.get("explanation", {})
        if isinstance(explanation, dict) and explanation.get("correct"):
            correct_exp = explanation["correct"]
            system_parts.append(
                f"Explanation (EN): {correct_exp.get('en', '')}\n"
                f"Explanation (ZH): {correct_exp.get('zh', '')}"
            )

    system_msg = "\n".join(system_parts)

    # Build messages: system + recent history (last 20)
    # Note: history already includes the current user message (saved before calling this)
    messages = [{"role": "system", "content": system_msg}]

    recent_history = history[-20:] if len(history) > 20 else history
    for h in recent_history:
        messages.append({"role": h["role"], "content": h["content"]})

    stream = await client.chat.completions.create(
        model="gpt-4.1-mini",
        messages=messages,
        stream=True,
    )

    async for chunk in stream:
        delta = chunk.choices[0].delta if chunk.choices else None
        if delta and delta.content:
            yield delta.content
