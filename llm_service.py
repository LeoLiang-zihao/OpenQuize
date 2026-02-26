"""
LLM service using OpenAI Agents SDK to extract questions from PDFs
and generate bilingual explanations.
"""

import json
import fitz  # PyMuPDF
import docx  # python-docx
from lxml import etree
from pathlib import Path
from pydantic import BaseModel, Field
from agents import Agent, Runner, function_tool
from openai import AsyncOpenAI


# ── OMML (Office Math Markup Language) namespace ──
_MATH_NS = "http://schemas.openxmlformats.org/officeDocument/2006/math"
_WORD_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_M = f"{{{_MATH_NS}}}"
_W = f"{{{_WORD_NS}}}"


# ── Helpers for Agents SDK streaming ──

def _build_agent_input(history: list[dict]) -> list[dict]:
    """Convert chat history to Agents SDK input format."""
    recent = history[-20:] if len(history) > 20 else history
    items = []
    for h in recent:
        role = h["role"]
        content = h["content"]
        if role == "user":
            items.append({"role": "user", "content": content})
        elif role == "assistant":
            items.append({"role": "assistant", "content": content})
    return items if items else [{"role": "user", "content": "hello"}]


def _is_text_delta(event) -> bool:
    """Check if a stream event is a text delta (not reasoning).

    The event type 'response.output_text.delta' is only for text output;
    reasoning tokens use a separate 'response.reasoning_text.delta' type,
    so no output_index filtering is needed.
    """
    if type(event).__name__ != "RawResponsesStreamEvent":
        return False
    d = event.data
    return hasattr(d, "type") and d.type == "response.output_text.delta"


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


# ── Pydantic models for open questions ──

class OpenQuestionItem(BaseModel):
    question_text: str = Field(description="The full problem/question text, formatted as clean Markdown with LaTeX math notation (use \\( \\) for inline math, \\[ \\] for display math). Use lists for sub-parts, bold for emphasis.")
    category: str = Field(description="Topic category of this question")


class ExtractedOpenQuestions(BaseModel):
    questions: list[OpenQuestionItem]


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


# ── OMML to LaTeX converter ──

def _omml_to_latex(el) -> str:
    """Recursively convert an OMML math element to a LaTeX string."""
    tag = etree.QName(el.tag).localname

    if tag == "r":  # math run → text
        t = el.find(f"{_M}t")
        return t.text if t is not None and t.text else ""

    if tag == "f":  # fraction
        num = _convert_children(el.find(f"{_M}num"))
        den = _convert_children(el.find(f"{_M}den"))
        return f"\\frac{{{num}}}{{{den}}}"

    if tag == "sSup":  # superscript
        base = _convert_children(el.find(f"{_M}e"))
        sup = _convert_children(el.find(f"{_M}sup"))
        return f"{base}^{{{sup}}}"

    if tag == "sSub":  # subscript
        base = _convert_children(el.find(f"{_M}e"))
        sub = _convert_children(el.find(f"{_M}sub"))
        return f"{base}_{{{sub}}}"

    if tag == "sSubSup":  # sub-superscript
        base = _convert_children(el.find(f"{_M}e"))
        sub = _convert_children(el.find(f"{_M}sub"))
        sup = _convert_children(el.find(f"{_M}sup"))
        return f"{base}_{{{sub}}}^{{{sup}}}"

    if tag == "rad":  # radical / sqrt
        deg = _convert_children(el.find(f"{_M}deg"))
        body = _convert_children(el.find(f"{_M}e"))
        if deg.strip():
            return f"\\sqrt[{deg}]{{{body}}}"
        return f"\\sqrt{{{body}}}"

    if tag == "nary":  # n-ary: sum, integral, product
        pr = el.find(f"{_M}naryPr")
        char = "\u2211"  # default: summation
        if pr is not None:
            chr_el = pr.find(f"{_M}chr")
            if chr_el is not None:
                char = chr_el.get(f"{_M}val", "\u2211")
        op_map = {"\u2211": "\\sum", "\u220F": "\\prod", "\u222B": "\\int",
                   "\u222C": "\\iint", "\u222D": "\\iiint", "\u222E": "\\oint"}
        op = op_map.get(char, char)
        sub = _convert_children(el.find(f"{_M}sub"))
        sup = _convert_children(el.find(f"{_M}sup"))
        body = _convert_children(el.find(f"{_M}e"))
        result = op
        if sub.strip():
            result += f"_{{{sub}}}"
        if sup.strip():
            result += f"^{{{sup}}}"
        return f"{result} {body}"

    if tag == "d":  # delimiter (parentheses, brackets)
        pr = el.find(f"{_M}dPr")
        open_c, close_c = "(", ")"
        if pr is not None:
            beg = pr.find(f"{_M}begChr")
            end = pr.find(f"{_M}endChr")
            if beg is not None:
                open_c = beg.get(f"{_M}val", "(")
            if end is not None:
                close_c = end.get(f"{_M}val", ")")
        # Collect all <m:e> children (delimiters can have multiple entries)
        entries = el.findall(f"{_M}e")
        inner = ", ".join(_convert_children(e) for e in entries) if entries else ""
        delim_map = {"(": ("\\left(", "\\right)"), "[": ("\\left[", "\\right]"),
                      "{": ("\\left\\{", "\\right\\}"), "|": ("\\left|", "\\right|"),
                      "\u2016": ("\\left\\|", "\\right\\|")}
        if open_c in delim_map:
            o, c = delim_map[open_c]
            return f"{o} {inner} {c}"
        return f"{open_c}{inner}{close_c}"

    if tag == "func":  # function name + argument
        fname = _convert_children(el.find(f"{_M}fName")).strip()
        body = _convert_children(el.find(f"{_M}e"))
        builtin = {"sin": "\\sin", "cos": "\\cos", "tan": "\\tan", "log": "\\log",
                    "ln": "\\ln", "lim": "\\lim", "max": "\\max", "min": "\\min",
                    "exp": "\\exp", "det": "\\det", "inf": "\\inf", "sup": "\\sup"}
        fname_cmd = builtin.get(fname, f"\\operatorname{{{fname}}}")
        return f"{fname_cmd}{{{body}}}"

    if tag == "acc":  # accent (hat, bar, tilde, vec)
        pr = el.find(f"{_M}accPr")
        char = "\u0302"  # default hat
        if pr is not None:
            chr_el = pr.find(f"{_M}chr")
            if chr_el is not None:
                char = chr_el.get(f"{_M}val", "\u0302")
        body = _convert_children(el.find(f"{_M}e"))
        acc_map = {"\u0302": "\\hat", "\u0304": "\\bar", "\u0303": "\\tilde",
                    "\u20D7": "\\vec", "\u0307": "\\dot", "\u0308": "\\ddot",
                    "\u0305": "\\overline", "^": "\\hat", "~": "\\tilde",
                    "\u00AF": "\\bar", "\u2192": "\\vec"}
        cmd = acc_map.get(char, "\\hat")
        return f"{cmd}{{{body}}}"

    if tag == "bar":
        body = _convert_children(el.find(f"{_M}e"))
        return f"\\overline{{{body}}}"

    if tag == "m":  # matrix
        rows = el.findall(f"{_M}mr")
        row_strs = []
        for mr in rows:
            cells = [_convert_children(e) for e in mr.findall(f"{_M}e")]
            row_strs.append(" & ".join(cells))
        return "\\begin{pmatrix} " + " \\\\ ".join(row_strs) + " \\end{pmatrix}"

    if tag == "eqArr":  # equation array (align)
        rows = [_convert_children(e) for e in el.findall(f"{_M}e")]
        return "\\begin{aligned} " + " \\\\ ".join(rows) + " \\end{aligned}"

    if tag == "limLow":  # lower limit
        base = _convert_children(el.find(f"{_M}e"))
        lim = _convert_children(el.find(f"{_M}lim"))
        return f"{base}_{{{lim}}}"

    if tag == "limUpp":  # upper limit
        base = _convert_children(el.find(f"{_M}e"))
        lim = _convert_children(el.find(f"{_M}lim"))
        return f"{base}^{{{lim}}}"

    if tag == "box" or tag == "borderBox":
        return _convert_children(el.find(f"{_M}e"))

    # fallback: just recurse into children
    return _convert_children(el)


def _convert_children(el) -> str:
    """Recursively convert all children of an element."""
    if el is None:
        return ""
    return "".join(_omml_to_latex(child) for child in el)


# ── DOCX text extraction (with formatting + math) ──

def _render_paragraph_xml(para_el) -> str:
    """Render a <w:p> element to Markdown, preserving bold/italic and inline math."""
    parts = []
    for child in para_el:
        tag = etree.QName(child.tag).localname

        if tag == "r":  # word run
            t_el = child.find(f"{_W}t")
            if t_el is not None and t_el.text:
                text = t_el.text
                rPr = child.find(f"{_W}rPr")
                is_bold = rPr is not None and rPr.find(f"{_W}b") is not None
                is_italic = rPr is not None and rPr.find(f"{_W}i") is not None
                if is_bold and is_italic:
                    text = f"***{text}***"
                elif is_bold:
                    text = f"**{text}**"
                elif is_italic:
                    text = f"*{text}*"
                parts.append(text)

        elif tag == "oMath":  # inline math
            latex = _omml_to_latex(child).strip()
            if latex:
                parts.append(f" \\( {latex} \\) ")

        elif tag == "hyperlink":
            # extract text from runs inside hyperlink
            for r in child.findall(f"{_W}r"):
                t_el = r.find(f"{_W}t")
                if t_el is not None and t_el.text:
                    parts.append(t_el.text)

    return "".join(parts)


def _render_table_xml(tbl_el) -> str:
    """Render a <w:tbl> element to a Markdown table."""
    rows = tbl_el.findall(f"{_W}tr")
    if not rows:
        return ""
    md_rows = []
    for row in rows:
        cells = row.findall(f"{_W}tc")
        cell_texts = []
        for cell in cells:
            # A cell can contain multiple paragraphs
            paras = cell.findall(f"{_W}p")
            cell_text = " ".join(_render_paragraph_xml(p) for p in paras).strip()
            cell_texts.append(cell_text.replace("|", "\\|"))
        md_rows.append("| " + " | ".join(cell_texts) + " |")
    # Insert header separator after the first row
    if len(md_rows) >= 1:
        ncols = md_rows[0].count("|") - 1
        sep = "| " + " | ".join(["---"] * max(ncols, 1)) + " |"
        md_rows.insert(1, sep)
    return "\n".join(md_rows)


def extract_docx_text(file_path: str) -> str:
    """Extract text from a .docx file, preserving formatting as Markdown with LaTeX math."""
    doc = docx.Document(file_path)
    body = doc.element.body
    output: list[str] = []

    for child in body:
        tag = etree.QName(child.tag).localname

        if tag == "p":
            # Check for display math: <m:oMathPara> inside <w:p>
            math_paras = child.findall(f"{_M}oMathPara")
            if math_paras:
                for mp in math_paras:
                    omath_els = mp.findall(f"{_M}oMath")
                    for om in omath_els:
                        latex = _omml_to_latex(om).strip()
                        if latex:
                            output.append(f"\\[ {latex} \\]")
                # Also render any non-math content in the paragraph
                text = _render_paragraph_xml(child).strip()
                if text:
                    output.append(text)
                continue

            # Check for standalone <m:oMath> directly under <w:p> (display-style)
            omath_direct = child.findall(f"{_M}oMath")

            # Detect heading style
            pPr = child.find(f"{_W}pPr")
            heading_level = 0
            is_list = False
            list_marker = "- "
            if pPr is not None:
                pStyle = pPr.find(f"{_W}pStyle")
                if pStyle is not None:
                    style_val = pStyle.get(f"{_W}val", "")
                    if style_val.startswith("Heading"):
                        try:
                            heading_level = int(style_val.replace("Heading", ""))
                        except ValueError:
                            pass
                    elif style_val in ("ListParagraph",):
                        is_list = True

                # Detect numbered/bulleted list
                numPr = pPr.find(f"{_W}numPr")
                if numPr is not None:
                    is_list = True
                    ilvl = numPr.find(f"{_W}ilvl")
                    indent = int(ilvl.get(f"{_W}val", "0")) if ilvl is not None else 0
                    list_marker = "  " * indent + "- "

            text = _render_paragraph_xml(child).strip()
            if not text and not omath_direct:
                output.append("")
                continue

            if heading_level > 0:
                output.append(f"{'#' * heading_level} {text}")
            elif is_list:
                output.append(f"{list_marker}{text}")
            else:
                output.append(text)

        elif tag == "tbl":
            table_md = _render_table_xml(child)
            if table_md:
                output.append("")
                output.append(table_md)
                output.append("")

    # Clean up excessive blank lines
    lines = "\n".join(output)
    while "\n\n\n" in lines:
        lines = lines.replace("\n\n\n", "\n\n")
    return lines.strip()


# ── Generic file text extraction ──

def extract_file_text(file_path: str) -> str:
    """Extract text from PDF or DOCX based on file extension."""
    ext = Path(file_path).suffix.lower()
    if ext == ".pdf":
        return extract_pdf_text(file_path)
    elif ext == ".docx":
        return extract_docx_text(file_path)
    else:
        raise ValueError(f"Unsupported file type: {ext}")


# ── Reference material loading ──

def load_reference_texts(reference_paths: list[str]) -> str:
    """Load reference files (PDF or DOCX) for the agent to consult."""
    refs = []
    for p in reference_paths:
        if Path(p).exists():
            text = extract_file_text(p)
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


# ── Open question extraction ──

async def extract_open_questions_from_file(
    file_path: str,
    user_prompt: str = "",
    reference_paths: list[str] | None = None,
) -> list[dict]:
    """
    Extract open-ended math/science problems from a file (PDF or DOCX).
    """
    file_text = extract_file_text(file_path)

    ref_text = ""
    if reference_paths:
        ref_text = load_reference_texts(reference_paths)

    instructions = """You are an expert educational content analyzer. Your job is to:

1. Extract ALL open-ended problems/questions from the provided document text.
2. For each problem, identify:
   - The full problem text, formatted as clean Markdown
   - A topic category
3. Do NOT attempt to solve the problems or provide answers.

FORMATTING RULES:
- Convert all mathematical notation to LaTeX: use \\( \\) for inline math and \\[ \\] for display math.
- Use Markdown formatting for structure: **bold** for emphasis, numbered/bulleted lists for sub-parts, etc.
- If a problem has multiple parts (a, b, c, etc.), format them as a clear list within a single question.
- Include any hints or sub-parts that are part of the problem.
- Each distinct problem should be a separate item.
- The output should look clean and readable when rendered as Markdown with LaTeX.
"""

    agent = Agent(
        name="Open Question Extractor",
        instructions=instructions,
        model="gpt-5.2-codex",
        output_type=ExtractedOpenQuestions,
    )

    user_msg_parts = []
    if user_prompt:
        user_msg_parts.append(f"User instruction: {user_prompt}")
    user_msg_parts.append(f"Document Content:\n{file_text}")
    if ref_text:
        user_msg_parts.append(f"Reference Materials:\n{ref_text}")

    user_msg = "\n\n".join(user_msg_parts)

    result = await Runner.run(agent, user_msg)
    extracted: ExtractedOpenQuestions = result.final_output

    return [q.model_dump() for q in extracted.questions]


# ── Answer generation for open questions ──

async def generate_answer_for_question(question_text: str) -> str:
    """
    Generate a concise, elegant answer key for an open-ended problem.
    Uses the Agents SDK (same as extraction) so it works with gpt-5.2-codex.
    """
    agent = Agent(
        name="Answer Generator",
        instructions="You generate concise, elegant answer keys for math and science problems. Use LaTeX notation (\\( \\) for inline, \\[ \\] for display math).",
        model="gpt-5.2-codex",
    )

    user_msg = (
        "Please provide a brief key for the following problem. "
        "Make it as concise and elegant as possible.\n\n"
        f"{question_text}"
    )

    result = await Runner.run(agent, user_msg)
    return result.final_output


# ── Guided dialogue for open questions (streaming) ──

async def guided_dialogue_stream(
    message: str,
    history: list[dict],
    question_text: str,
    answer_key: str,
):
    """
    Streaming guided dialogue using Agents SDK with gpt-5.2-codex.
    The agent guides the student WITHOUT revealing the answer.
    """
    system_prompt = f"""You are a Socratic tutor helping a student understand a math/science problem.

PROBLEM:
{question_text}

ANSWER KEY (for your reference only — NEVER reveal this to the student):
{answer_key}

RULES:
1. NEVER directly reveal the answer, solution steps, or any part of the answer key.
2. Use Socratic questioning to guide the student toward understanding.
3. Ask probing questions to test their understanding.
4. If the student is on the wrong track, gently redirect without giving away the answer.
5. Respond in the same language the student uses (English or Chinese).
6. If you are confident the student has fully understood the problem and can solve it independently, end your message with exactly: [UNDERSTOOD]
7. Keep your responses concise and focused.
8. Use LaTeX notation for math when needed (use \\( \\) for inline and \\[ \\] for display math)."""

    agent = Agent(
        name="Guided Tutor",
        instructions=system_prompt,
        model="gpt-5.2-codex",
    )

    # Build input: history + current message
    input_items = _build_agent_input(history)

    result = Runner.run_streamed(agent, input_items)
    async for event in result.stream_events():
        if _is_text_delta(event):
            yield event.data.delta


# ── Voice transcription ──

async def transcribe_audio(audio_data: bytes, filename: str = "audio.webm") -> str:
    """Transcribe audio using OpenAI Whisper API."""
    client = AsyncOpenAI()

    # Create a file-like object for the API
    import io
    audio_file = io.BytesIO(audio_data)
    audio_file.name = filename

    transcript = await client.audio.transcriptions.create(
        model="whisper-1",
        file=audio_file,
    )

    return transcript.text


# ── Chat with context (streaming) ──

async def chat_with_context_stream(
    message: str,
    history: list[dict],
    question_context: dict | None = None,
):
    """
    Async generator that yields streaming deltas for chat responses.
    Uses Agents SDK with gpt-5.2-codex.
    """
    system_parts = [
        "You are a helpful AI study assistant. The student is reviewing quiz questions. "
        "Answer their questions concisely. You can explain concepts, translate terms, "
        "clarify confusing topics, etc. Respond in the same language the student uses, "
        "or use bilingual (English + Chinese) when helpful. "
        "Use LaTeX notation for math when needed (use \\( \\) for inline and \\[ \\] for display math)."
    ]

    if question_context:
        q_type = question_context.get("type", "mcq")
        if q_type == "mcq":
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
        else:
            system_parts.append(
                f"\nCurrent question context:\n"
                f"Category: {question_context.get('category', 'N/A')}\n"
                f"Question: {question_context.get('question_text', '')}\n"
                f"Answer key: {question_context.get('explanation', '')}"
            )

    system_msg = "\n".join(system_parts)

    agent = Agent(
        name="Study Assistant",
        instructions=system_msg,
        model="gpt-5.2-codex",
    )

    input_items = _build_agent_input(history)

    result = Runner.run_streamed(agent, input_items)
    async for event in result.stream_events():
        if _is_text_delta(event):
            yield event.data.delta
