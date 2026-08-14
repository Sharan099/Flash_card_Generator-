---
title: Flash Card Generator
emoji: 📚
colorFrom: blue
colorTo: indigo
sdk: gradio
app_file: app.py
app_port: 7860
pinned: false
---

# Lingo Local

A local, single-user German vocabulary trainer that converts PDF to Markdown, extracts CEFR-targeted vocabulary flashcards using a free LLM gateway, and lets you drill them with a spaced repetition Leitner deck system.

## Setup

1. **Environment Setup**:
   Create a virtual environment and install the dependencies:
   ```bash
   python -m venv .venv
   # Windows
   .venv\Scripts\activate
   # macOS/Linux
   source .venv/bin/activate

   pip install -r requirements.txt
   ```

2. **API Configuration**:
   Create a `.env` file in the root directory (copied from `.env.example`) and fill in your gateway credentials:
   ```bash
   LLM_BASE_URL=https://api.groq.com/openai/v1
   LLM_API_KEY=your_key_here
   LLM_MODEL=llama-3.3-70b-versatile
   GROQ_API_KEY=
   ```

   `LLM_API_KEY` is required for LLM-backed PDF extraction. If it is missing, uploads that need the LLM will fail with a clear runtime error.

## Run

### Hugging Face Space (Gradio)

The Space entry point is `app.py`. Run locally:

```bash
pip install -r requirements.txt
python app.py
```

Open the URL printed in the terminal (default `http://127.0.0.1:7860`).

### Share with a friend (Hugging Face Space)

1. Create the Space at `sharan099/flash-card-generator` if it does not exist yet (SDK: **Gradio**).
2. In the Space **Settings**, set visibility to **Public**.
3. Add Space **Secrets**: `LLM_API_KEY` (and optional `GROQ_API_KEY`, `LLM_BASE_URL`, `LLM_MODEL`).
4. Share this URL:

   `https://huggingface.co/spaces/sharan099/flash-card-generator`

Do **not** use `share=True` in `demo.launch()` — the public Space URL is the share link.

### Local development (FastAPI + study UI)

Start the full FastAPI application with deck management and spaced repetition:

```bash
uvicorn server:app --reload --port 8000
```

Open your browser at `http://localhost:8000`.

### Docker (optional — local or Cloud Run)

Docker is **not** required for the Hugging Face Gradio Space. It remains available for local container testing or other deployments:

```bash
docker build -t flash-card-generator .
docker run --rm -p 7860:7860 --env-file .env flash-card-generator
```

Open your browser at `http://localhost:7860`.

Health check: `GET /health` → `{"status":"ok"}`

## Features & Controls

- **Upload PDF**: Convert a German PDF to clean markdown and extract flashcards at your chosen CEFR Level (A1-C2).
- **Leitner Spaced Repetition**: 5-box system (intervals: `[0, 1, 3, 7, 21]` days). Correct reviews push cards to higher boxes, incorrect reviews demote to Box 1.
- **German Voice Synthesis**: Toggle `SOUND` to speak the German answers out loud using standard browser `speechSynthesis` (ensure `de-DE` locale voice is installed on your OS).
- **Flip Board**: Swap between English-front -> German-back and German-front -> English-back modes.
- **Keyboard Shortcuts**:
  - `Space / Enter`: Reveal card or Mark "Knew It"
  - `K` or `Right Arrow`: Mark "Knew It"
  - `J` or `Left Arrow`: Mark "Review Again"
  - `S`: Toggle sound ON/OFF
  - `F`: Toggle flip board direction
  - `U`: Trigger file upload picker
