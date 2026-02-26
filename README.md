# OpenQuiz

A local AI-powered quiz tool that extracts questions from your study materials (PDF/DOCX) and helps you review them interactively. Supports multiple-choice and open-ended questions with bilingual explanations (EN/ZH), LaTeX math rendering, guided dialogue, and voice input.

## Features

- **Auto-extract questions** from PDF and DOCX files using GPT
- **Multiple Choice (MCQ)** and **Open-ended** question types
- **Bilingual explanations** (English + Chinese)
- **LaTeX math rendering** via KaTeX
- **Guided dialogue** — AI walks you through solving problems step by step
- **AI chat assistant** — ask follow-up questions about any topic
- **Voice input** — speak your answers using Whisper transcription
- **Dark theme** UI

## Quick Start

> **Just clone the repo, open it in your IDE with an AI coding agent (e.g. Claude Code, Cursor), and ask the agent to set it up for you.** It will handle venv creation, dependency installation, `.env` config, and startup.

If you prefer to do it manually:

```bash
# 1. Clone
git clone https://github.com/LeoLiang-zihao/OpenQuize.git
cd OpenQuize

# 2. Create virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Set up environment variables
cp .env.example .env   # then edit .env and add your OpenAI API key
# Required: OPENAI_API_KEY=sk-...

# 5. Run
python server.py
```

Then open http://localhost:8000 in your browser.

## Tech Stack

- **Backend**: FastAPI + SQLite (WAL mode) + OpenAI Agents SDK
- **Frontend**: Vanilla JS/HTML/CSS (single-page app)
- **Models**: GPT for question extraction & chat, Whisper for voice input
- **Rendering**: marked.js (Markdown) + KaTeX (LaTeX math)

## Project Structure

```
server.py          # FastAPI routes & API endpoints
llm_service.py     # LLM agents (extraction, chat, guided dialogue, voice)
database.py        # SQLite CRUD operations
static/
  index.html       # App HTML
  app.js           # Frontend logic
  style.css        # Styles
requirements.txt   # Python dependencies
```

## License

MIT
