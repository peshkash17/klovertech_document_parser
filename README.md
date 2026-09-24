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

## Tech stack

| Layer | Choice | Why |
|---|---|---|
| API | FastAPI + uvicorn | Async, auto-generates `/docs`, `BackgroundTasks` built-in |
| Agent | LangGraph `StateGraph` | Explicit graph — each step is a named node, easy to inspect and demo |
| PDF extraction | PyMuPDF | Fastest, most reliable; `.get_pixmap()` converts scanned pages to images |
| Word extraction | python-docx | Standard |
| Image / scan OCR | GPT-4o Vision | Handles multilingual text in images — Tesseract would fail on Arabic/Hindi/Chinese |
| Language detect + translate | GPT-4o (JSON mode) | One model, one API key — no langdetect, no DeepL |
| Database | PostgreSQL (Supabase) | Structured storage, free tier, SQL-queryable during demo |
| Deployment | Render | Free web service, Git-push deploy, no extra Postgres needed |

---

## Local setup

### Prerequisites
- Python 3.11+
- PostgreSQL (or a free [Supabase](https://supabase.com) project)
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

You already have Supabase Postgres — reuse that URL. Do **not** create a new Render database unless you want one.

1. Push this folder to a GitHub repo (Render deploys from Git).
2. Go to [dashboard.render.com](https://dashboard.render.com) → **New** → **Blueprint** and select the repo  
   (or **New** → **Web Service** and point it at the repo).
3. If you create the service manually:
   - **Runtime:** Python 3
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `uvicorn main:app --host 0.0.0.0 --port $PORT`
   - **Instance type:** Free
4. Add environment variables (same values as local `.env`):
   - `OPENAI_API_KEY`
   - `DATABASE_URL` — your existing Supabase URL is fine (`postgresql://...` or `postgresql+asyncpg://...`)
5. Deploy. The app is live at `https://<service-name>.onrender.com`.

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
