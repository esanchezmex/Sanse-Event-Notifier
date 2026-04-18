# Monthly / Weekly Local Events Digest — Project Plan

## Does this make sense?

Yes. You are describing a **small, opinionated data pipeline**: discover content from the web (or APIs), normalize it, enrich or summarize it (optionally with an LLM), store it, and **deliver** it on a schedule (email or similar). That is a textbook way to practice data-engineering ideas without building a giant platform.

The tension you named is right: **learn real patterns (orchestration, idempotency, observability)** while **keeping the moving parts few enough** that you can explain the whole system in one sitting. This plan optimizes for that.

---

## Guiding principles

1. **Start with a script + cron (or a single container cron)**. Keep orchestration simple and deterministic.
2. **Design for scraper resilience first** because your primary source is HTML content, not APIs.
3. **Separate “fetch raw” from “interpret.”** Raw artifacts (JSON/HTML) land in storage first; transforms are replayable.
4. **Make schedules and parameters data**, not code (city, radius, date window, sources list in config or a tiny DB table).

---

## End-to-end pipeline (explicit stages)

Think of every run as the same ordered pipeline, whether you run it manually or via cron.

### Primary source profile

- **Main source:** `https://www.diariodesanse.com/` (primary and currently only confirmed source).
- **Use case fit:** frequently publishes local event-relevant items (including events like Smout Out and Latineando).
- **Implication:** ingest strategy is **scraping-first** with robust parsing, snapshot storage, and change detection.

### Stage 0 — Trigger & configuration

| Piece | Responsibility |
|--------|----------------|
| **Scheduler** | Two cron schedules: crawl every 3 days, send digest twice monthly. |
| **Config** | Area (city + optional coordinates), overlap window, source list, output channel (email), local model choice and prompt version. |

**Practice note:** With cron, a `.env` or YAML file is enough for run-time parameters.

---

### Stage 1 — Ingest (collect)

**Goal:** Pull *raw* data from each source with minimal interpretation.

| Sub-step | What happens |
|----------|----------------|
| **1a. Source adapter (`diariodesanse`)** | Crawl homepage/category/list pages (e.g. Cultura, Fiestas, Sociedad), extract article URLs, then fetch article detail pages. |
| **1b. HTTP strategy** | Use retries with exponential backoff, polite rate limiting, stable user-agent, and conditional requests (`ETag` / `Last-Modified`) where available. |
| **1c. Raw landing** | Save HTML responses to local `data/raw/{run_id}/` so parsing can be replayed without re-requesting the site. |
| **1d. Crawl scope** | Respect robots/TOS, cap max pages per run, and stop when articles are older than your configured window. |
| **1e. Checkpoint + overlap** | Keep `last_seen_published_at`; on each run, scan from `(last_seen - overlap_days)` to now, then deduplicate by URL/hash. |

**Data-engineering ideas:** idempotent filenames (`source=diariodesanse/date=2026-04-15/run_id=…`), parser versioning, and selector fallback rules when page structure shifts.

---

### Stage 2 — Parse & normalize (bronze → silver)

**Goal:** Turn heterogeneous blobs into **one internal event model**.

| Field (example) | Why |
|-----------------|-----|
| `event_id` | Stable dedup key (hash of URL + start time + title). |
| `title`, `start_at`, `end_at`, `venue`, `url` | Core display + dedup. |
| `source`, `fetched_at` | Lineage. |
| `raw_ref` | Pointer to raw file (S3 key or path). |
| `category`, `published_at` | Useful ranking/filter signals from the article context. |
| `confidence_score` | How likely the article truly contains an event announcement. |

**Practice note:** This is where you’d use **dbt** if you had warehouse tables; for a personal project, **SQLite or DuckDB** is enough to learn SQL transforms without cloud cost.

---

### Stage 3 — Quality & deduplication

**Goal:** One row per real-world event (as best you can).

| Check | Example |
|-------|---------|
| **Dedup** | Same URL or fuzzy match on title + start time. |
| **Event intent filter** | Keep only posts likely to announce events (keywords: festival, concierto, programación, entradas, etc.). |
| **Program-specific filter** | Prioritize explicit mentions like “Smout Out” and “Latineando” with boosted score. |
| **Null handling** | Drop or flag rows missing `start_at` or `url`. |
| **Parser health** | Alert if selector success rate drops unexpectedly (site layout change signal). |

Log counts: `raw_rows`, `normalized_rows`, `deduped_rows`. That’s your **data quality dashboard** v0.

---

### Stage 4 — LLM pipeline (three roles, all local via Ollama)

All LLM calls run locally through **Ollama** — no data leaves the machine.

#### 4a. The Gatekeeper (triage)

Runs on every crawl (every 3 days). Receives article title, published date, category, and the **full cleaned article body text** extracted from HTML.

Outputs one of three decisions:

| Decision | Meaning | Action |
|----------|---------|--------|
| `include` | Clearly event-relevant | Pass to The Parser |
| `review` | Uncertain / maybe | Pass to The Parser, flagged for manual review |
| `exclude` | Not relevant | Drop silently — no feedback loop, no re-processing |

Persist `decision`, `confidence`, and `reason` for every article so you can audit and improve the prompt over time.

#### 4b. The Parser (structured extraction)

Runs only on `include` and `review` articles after Gatekeeper triage. Extracts a structured event record:

| Field | Notes |
|-------|-------|
| `event_name` | Normalized event title |
| `start_date` | Parsed date/time if found |
| `location` | Venue or area if mentioned |
| `description` | Short summary |
| `review_flag` | `true` for `review` articles |

Output is written to **SQLite** with the Gatekeeper decision attached. `exclude` articles are not parsed and never reach event tables. This is the handoff point between the two cron schedules.

#### 4c. The Owl (digest generation)

Runs on the every-15-days digest cron. Reads confirmed events from SQLite for the past window, then composes the email body. It knows to:

- Group `include` events as main content.
- Add a clearly labelled **"Needs your eye"** section for `review` items.
- Summarize concisely — its only input is structured rows, not articles.

**Practice note:** Version your system prompts for all three roles. Store the prompt hash alongside each LLM output so regressions are traceable.

---

### Stage 5 — Present (gold / delivery format)

**Goal:** A single artifact you can read or send.

| Output | Notes |
|--------|--------|
| **HTML email** | Nice typography, sections by date/theme, and a “Needs Review” section for `review` items. |
| **Markdown → HTML** | Easier to write templates. |
| **Static page** | Upload to S3 + CloudFront or GitHub Pages if you want a link instead of email. |

Keep a **Jinja2** (or similar) template so layout is not tangled with Python logic.

---

### Stage 6 — Deliver & notify

| Piece | Responsibility |
|--------|----------------|
| **Email transport** | SMTP (Gmail app password, SendGrid, Resend, etc.). |
| **Secrets** | Never commit keys; use env vars or a minimal secret manager if on cloud. |

**Practice note:** Send a “dry run” to yourself with `DRY_RUN=1` that writes HTML to disk but does not send mail.

---

### Stage 7 — Observability & operations

Minimum viable ops (still very “data eng”):

| Artifact | Purpose |
|----------|---------|
| **Structured logs** | JSON lines: stage, duration, row counts, errors. |
| **Run metadata** | `run_id`, start/end, success flag — SQLite table is fine. |
| **Alerts** | Email yourself only on failure (simplest), or Slack webhook later. |

Also track `candidate_articles`, `event_like_articles`, and `selector_failures` per run so scraper regressions are visible immediately.

---

## Scheduling pattern (two independent cron jobs)

Use cron as the orchestrator and keep business logic in plain Python functions. The two cron jobs are fully independent — a failed crawl does not block the digest, and vice versa.

### Cron A — Crawl (every 3 days)

`0 8 */3 * *`

1. Load checkpoint (`last_seen_published_at`) from SQLite.
2. Crawl `diariodesanse.com`, scanning articles from `(last_seen - overlap_days)` to now.
3. Dedup by URL hash; skip already-stored articles.
4. For each new article: run **The Gatekeeper**.
5. For `include`/`review`: run **The Parser**, write structured event to SQLite.
6. For `exclude`: drop — no feedback loop.
7. Update checkpoint to `max(published_at)` of successfully processed articles.

### Cron B — Digest (twice monthly)

`0 9 1,15 * *`

1. Query SQLite for events in the past 15-day window not yet included in a sent digest.
2. Run **The Owl** on the structured rows.
3. Render HTML email (Jinja2 template) with two sections: confirmed events + "Needs your eye."
4. Send via smtplib. Mark rows as `digest_sent`.

### Overlap policy

Always scan `last_seen_published_at - 2 days` to guard against:
- Articles published slightly before the last run that were missed.
- Temporary site or network outages during the previous crawl.
- Dedup prevents any article from being processed twice.

This keeps orchestration lightweight and makes operations easy to reason about.

---

## Cloud: necessary or not?

| Need | Local-first | Cloud when… |
|------|-------------|-------------|
| Storage | `data/raw`, `data/processed` | You want durability + sharing across machines |
| Scheduler | cron, launchd, GitHub Actions | You need always-on or team runs |
| Secrets | `.env` (gitignored) | You graduate to IAM + Secrets Manager |
| Email | SMTP provider from anywhere | Same — provider is usually SaaS |

**Recommendation:** Stay local + free-tier email API until the pipeline is reliable; then run the same stages on a small VM or scheduled CI if you want always-on cloud execution.

---

## Scraping vs LLM — division of labor

| Layer | Role |
|-------|------|
| **Scraper (BS4)** | Fetch raw HTML; extract article text, title, date, category. Never interpret intent. |
| **The Gatekeeper** | Classify article relevance (`include / review / exclude`). Light prompt, fast. |
| **The Parser** | Structured extraction of event fields from confirmed articles. More detailed prompt. |
| **The Owl** | Digest composition from SQLite rows. Runs only on send day. |

Using an LLM to **parse arbitrary websites directly** is fragile and expensive; the scraper handles extraction, the LLMs handle interpretation.

---

## Legal & etiquette (short)

- Respect **robots.txt**, **terms of service**, and **rate limits**.
- Since this plan is source-constrained, keep crawl frequency conservative and cache responses aggressively.
- If available later, add official/open feeds as secondary corroboration sources.

---

## Suggested first milestone (concrete)

1. **Config:** `config.yaml` with municipality, overlap days, Ollama model names for each role, SMTP settings.
2. **Crawl script:** `diariodesanse` list-page crawler + article-page fetcher (BS4); saves raw HTML and checkpoint.
3. **Gatekeeper prompt:** classify using title + metadata + full cleaned body text → `include / review / exclude`; persist decision + reason to SQLite.
4. **Parser prompt:** only for `include`/`review` articles, extract `event_name`, `start_date`, `location`, `description`; `exclude` articles are dropped — no feedback loop.
5. **SQLite schema:** `articles_raw`, `article_decisions`, `events`, `digest_runs`.
6. **Digest script:** The Owl composes email from SQLite rows; Jinja2 renders two-section HTML (confirmed + “Needs your eye”).
7. **Send:** smtplib with `DRY_RUN=1` mode that writes HTML to disk instead of sending.
8. **Wire cron A and cron B** as two separate CLI entrypoints (`python -m src.crawl`, `python -m src.digest`).

After that works, harden reliability (retries, alerts, runbooks) without rewriting business logic.

---

## Repo layout (suggestion)

```
MonthlyEmail/
  .venv/
  plan.md
  config.yaml
  prompts/
    gatekeeper.txt      # The Gatekeeper system prompt (versioned)
    parser.txt          # The Parser system prompt (versioned)
    owl.txt             # The Owl system prompt (versioned)
  src/
    crawl.py            # Cron A entrypoint: scrape → gatekeeper → parser → SQLite
    digest.py           # Cron B entrypoint: SQLite → owl → render → send
    scraper/
      diariodesanse.py  # BS4 crawler + checkpoint logic
    llm/
      gatekeeper.py     # Triage: include / review / exclude
      parser.py         # Structured event extraction
      owl.py            # Digest composition
      client.py         # Shared Ollama client wrapper
    db/
      schema.sql        # articles_raw, article_decisions, events, digest_runs
      queries.py        # Named query helpers
    email/
      render.py         # Jinja2 → HTML
      send.py           # smtplib wrapper with DRY_RUN support
  templates/
    digest.html.j2      # Two-section template (confirmed + "Needs your eye")
  data/                 # gitignored — raw HTML snapshots, SQLite DB
  tests/
```

---

## Summary

You get a **real pipeline** with two independent cron jobs:

- **Cron A (every 3 days):** `diariodesanse` scraper (BS4) → The Gatekeeper (Ollama, triage) → The Parser (Ollama, extraction) → SQLite.
- **Cron B (1st and 15th):** SQLite → The Owl (Ollama, digest) → Jinja2 → smtplib.

`NO` articles are dropped cleanly. `YES` and `review` articles are stored and surfaced to you in two clearly separated digest sections. The overlap checkpoint prevents any article from slipping through between crawl runs. Everything runs locally — no cloud, no external APIs, no data leaving your machine.
