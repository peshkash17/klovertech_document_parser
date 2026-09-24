# KloverTech — Intelligent Document Processing Agent

An agentic application that ingests PDFs, Word documents, and images, extracts their content, detects the source language, and automatically translates non-English text to English. Designed to run locally and on [Render](https://render.com).

**Live demo:** deploy to Render, then paste your `*.onrender.com` URL here.

---

## What it does

1. **Upload** a PDF, DOCX, or image (PNG, JPG, WEBP, TIFF, BMP)
2. A **LangGraph agent** runs in the background and:
   - Routes to the appropriate extractor based on file type
   - Scores extraction quality — if too low (likely a scanned doc), retries with GPT-4o Vision
   - Detects the source language
   - Translates to English if needed
   - Saves the result to PostgreSQL
3. The **UI polls live** and shows each agent step as it happens
4. Final results show: source language, quality score, original text, translated text

---

## Architecture

```
Browser
  │
  ├── POST /upload ──► FastAPI ──► BackgroundTasks ──► LangGraph StateGraph
  │                                                          │
  │                                                    ┌─────▼──────┐
  │                                                    │   route    │  by MIME type
  │                                                    └─────┬──────┘
  │                                         ┌────────────────┼────────────────┐
  │                                    extract_pdf    extract_word    extract_image
  │                                         └────────────────┼────────────────┘
  │                                                    ┌─────▼──────┐
  │                                                    │assess_qual │  score < 0.3 → retry Vision
  │                                                    └─────┬──────┘
  │                                                    ┌─────▼──────┐
  │                                                    │ translate  │  GPT-4o JSON mode
  │                                                    └─────┬──────┘
  │                                                    ┌─────▼──────┐
  │                                                    │  save_doc  │  → PostgreSQL
  │                                                    └────────────┘
  │
  ├── GET /documents/{id}/logs  ◄── UI polls every 2s (step trace)
  └── GET /documents/{id}       ◄── UI polls until status=done
```

---

## Architecture decisions

The assignment asked for a working prototype in a short time. Each choice below is the simplest thing that still shows a real agent, not a one-shot script.

**LangGraph instead of a single Python function.**  
The pipeline is extract → judge quality → maybe retry → translate → save. A `StateGraph` makes that visible: named nodes, conditional edges, and a live step log. A hidden `if/else` script would work, but you could not demo routing or self-reflection.

**PyMuPDF first, GPT-4o Vision only as fallback.**  
Digital PDFs already contain selectable text. Paying for Vision on every file is slow and expensive. PyMuPDF is the fast path; if quality is poor (empty, CID garbage, or an LLM “this looks garbled”), we convert pages to images and OCR with Vision. That is the self-reflection + retry loop.

**One model (GPT-4o) for OCR, quality review, language detect, and translate.**  
Tesseract + langdetect + DeepL would mean three extra systems and weak multilingual OCR. One API key keeps the prototype simple. JSON mode gives structured `{source_language, is_english, translated_text}` without a parser.

**PostgreSQL rows, not a JSON blob on disk.**  
“Structured” here means a real schema: filename, type, language, quality, status, original text, translated text, plus a `document_logs` table. Documents vary too much to invent invoice fields for every upload. Text columns plus metadata is the smallest schema that is still queryable in a demo.

**Quality is two-layer, not a character-ratio only.**  
A naive “% alphanumeric” check fails short valid docs and can pass broken `(cid:12)` PDF extracts. Cheap structural checks run first (empty, control chars, CID markers). Only unclear extracts call GPT-4o. Score `< 0.3` retries Vision once (`vision_attempted` stops a loop).

**FastAPI `BackgroundTasks` instead of Celery.**  
Upload must return immediately so the UI can poll. For one process and a take-home, in-process tasks are enough. Celery + Redis would isolate CPU work; that is a known weakness, not a day-one requirement.

**Polling instead of WebSockets.**  
The UI hits `/documents/{id}` and `/logs` every 2s. Simpler to deploy and debug than a socket server. Fine at this volume.

**Render for hosting, any Postgres for data.**  
Render’s free web service is enough for a public demo URL. The app does **not** require Supabase. `DATABASE_URL` can be local Postgres, Render Postgres, Supabase, or any other host. Tables are created on boot.

**Why this is the simplest solution.**  
One web process, one graph, one model, one database. No queue, no object store, no field-level IE pipeline. That matches “do not over-engineer” while still covering ingest, structure, translation, and an agentic pattern.

---

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| API | FastAPI + uvicorn | Async, auto-generates `/docs`, `BackgroundTasks` built-in |
| Agent | LangGraph `StateGraph` | Explicit graph — each step is a named node, easy to inspect and demo |
| PDF extraction | PyMuPDF | Fast path for digital PDFs; `.get_pixmap()` for scanned pages |
| Word extraction | python-docx | Standard |
| Image / scan OCR | GPT-4o Vision | Multilingual scans — Tesseract is weak on Arabic/Hindi/Chinese |
| Language detect + translate | GPT-4o (JSON mode) | One model, one API key |
| Database | PostgreSQL | Structured rows + logs; any host works |
| Deployment | Render | Free public URL, Git-push deploy |

---

## Local setup

### Prerequisites
- Python 3.11+
- A PostgreSQL database (local install, [Render Postgres](https://render.com/docs/postgresql), or [Supabase](https://supabase.com) — any of these is fine)
- OpenAI API key (GPT-4o access required)

### Steps

```bash
cd klovertech

python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

pip install -r requirements.txt

cp .env.example .env
# Edit .env and fill in OPENAI_API_KEY and DATABASE_URL

uvicorn main:app --reload
```

Open `http://localhost:8000` — tables are created automatically on first boot.

---

## Environment variables

| Variable | Description |
|---|---|
| `OPENAI_API_KEY` | OpenAI secret key (GPT-4o access required) |
| `DATABASE_URL` | Async Postgres URL: `postgresql+asyncpg://user:pass@host:5432/db` |

---

## Deploy to Render

You need two things: a **web service** and a **Postgres URL**. Supabase is optional — any Postgres host works.

1. Push this folder to a GitHub repo (Render deploys from Git).
2. Get a `DATABASE_URL` from one of:
   - **Render Postgres** — Dashboard → **New** → **PostgreSQL** → Free (expires after 30 days)
   - **Supabase** — Project → Database → connection string
   - Any other Postgres instance you already run
   A plain `postgresql://...` URL is fine; the app adds the `+asyncpg` driver itself.
3. Go to [dashboard.render.com](https://dashboard.render.com) → **New** → **Web Service** and point it at the repo.
4. Service settings:
   - **Runtime:** Python 3
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
   - **Instance type:** Free
5. Environment variables:
   - `OPENAI_API_KEY`
   - `DATABASE_URL`
6. Deploy. The app is live at `https://<service-name>.onrender.com`. Tables are created on first boot.

**Demo note:** free Render instances sleep after 15 minutes idle and take ~1 minute to wake. Hit the URL once before the live walkthrough so it is warm.

If the native Python build fails on PyMuPDF, switch the service to **Docker** — this repo includes a `Dockerfile`.

---

## API endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/` | Web UI |
| `POST` | `/upload` | Upload a file (multipart/form-data, field name: `file`) |
| `GET` | `/documents` | List all documents |
| `GET` | `/documents/{id}` | Document detail + agent logs |
| `GET` | `/documents/{id}/logs` | Agent step trace only |
| `GET` | `/docs` | Auto-generated Swagger UI |

---

## Agentic patterns demonstrated

- **Tool use**: the agent calls different extraction tools (`extract_pdf`, `extract_word`, `extract_image`) depending on context
- **Routing**: a conditional edge sends each file to the correct extractor based on MIME type
- **Self-reflection + retry**: `assess_quality` first runs cheap structural checks (empty text, control characters, PDF `(cid:N)` garbage). Borderline extracts get a GPT-4o review. Score below 0.3 routes back to `extract_image` for a Vision retry
- **Multi-step reasoning**: the graph enforces an ordered pipeline — extract → assess → translate → save — with each step logged to the database

---

## Known weaknesses

- **No authentication** — anyone with the URL can upload files; add OAuth/API keys before production use
- **Original files are discarded** — only extracted text is stored; if re-extraction is needed the file must be re-uploaded
- **GPT-4o cost per upload** is non-trivial at scale; for high volume, a dedicated OCR service (Google Document AI, AWS Textract) would be cheaper
- **BackgroundTasks shares the server process** — a CPU-heavy extraction could slow concurrent requests; Celery + Redis would isolate it properly
- **No retry on transient API failures** — if the OpenAI call fails mid-graph, the document is marked `failed`; exponential backoff + retries would improve reliability
- **Translation truncates at 12,000 characters** — very long documents are translated partially to manage token cost

---

## Project structure

```
klovertech/
├── main.py              # FastAPI app — all endpoints
├── agent.py             # LangGraph StateGraph
├── models.py            # SQLAlchemy ORM + Pydantic schemas
├── database.py          # Async engine + lifespan
├── tools/
│   ├── extractor.py     # PDF / Word / Vision extraction + quality score
│   ├── translator.py    # GPT-4o language detection + translation
│   └── storage.py       # All DB read/write helpers
├── templates/
│   └── index.html       # Single-page UI
├── test_files/          # Sample Hindi / Arabic PDFs for the demo
├── requirements.txt
├── Dockerfile           # Optional — use if the native Python build fails
├── render.yaml          # Render Blueprint
├── .python-version
└── .env.example
```
