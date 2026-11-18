# Media Quotes Collection System (MQCS)

Automated detection, collection, verification and presentation of media quotes that matter for
understanding the **current trends and expectations of Russia's war on Ukraine**.

```
 ┌───────────── deterministic ─────────────┐   ┌─ model judgement ─┐   ┌──────── deterministic ────────┐
 sitemaps / RSS / topic pages ─► keyword rules ─► fetch + snapshot ─► Claude picks sentence spans ─► rebuild text from
 (daily, per source)            (versioned)       + clean extraction    (IDs only, + its own copy)    source + verify ─► SQLite
                                                                                                                       │
 Daily: 14-day window ─► Claude: sections / obsolete / summaries (quote IDs only) ─► validate plan ─► render MD from DB ─► verify MD
```

**Core principle — the model never writes stored text.** Claude decides *which* sentences are material,
*where* they belong and *which* are obsolete, but every quote, title, date, name and link written to the
database or the digest is copied by code from the publisher's page. Claude refers to sentences by ID
(`p4.s1`) and also returns its own copy of the text. The system rebuilds the quote from the stored article
and **rejects it unless the two match exactly**. That rules out "frankenquotes" and hallucinated wording.

---

## Quick start

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows   (source .venv/bin/activate on Linux/macOS)
pip install -r requirements.txt
copy .env.example .env            # then put your ANTHROPIC_API_KEY in .env

python -m mqcs init               # create DB, register BBC + Reuters
python -m mqcs backfill           # collect 1 Oct 2026 → today
python -m mqcs report             # write today's 14-day digest
python -m mqcs dashboard          # http://127.0.0.1:8050
```

| Command | Purpose |
|---|---|
| `python -m mqcs daily` | Daily job: collect the last 3 days from every source, then write the 14-day digest |
| `python -m mqcs backfill --from 2026-08-01 --to 2026-09-30 [--source bbc] [--exhaustive]` | Collect any past period on request; quotes are appended |
| `python -m mqcs add-source --name "The Guardian" --url https://www.theguardian.com/` | Auto-build a pipeline for a new outlet or think tank and backfill it from 1 Oct 2026 |
| `python -m mqcs report [--date 2026-11-17] [--days 14]` | (Re)generate the digest for a window |
| `python -m mqcs import-html --source reuters --url URL --file page.html` | Ingest a page you saved yourself, for publishers that block bots |
| `python -m mqcs audit` | Re-verify every stored quote against its raw HTML snapshot |
| `python -m mqcs export` | Rewrite `data/export/quotes.{jsonl,json,csv}` and `manifest.json` |
| `python -m mqcs status` | Database statistics |
| `--no-llm` (global flag) | Run discovery/fetch/filtering only (no API key needed) |

Scheduling: `scripts/install_windows_task.ps1` (Windows Task Scheduler), `scripts/cron.sh` (cron), or
`.github/workflows/daily.yml` (GitHub Actions, commits each digest and export, so git history is a second audit trail).

---

## How each requirement is met

### 2.1 Database
- **SQLite** (`data/quotes.db`): `quotes`, `articles`, `sources`, `runs`, `events`, `reports`, `rejected_quotes`.
- Each quote stores: `quote_id`, `quote_text`, `source_id`, `source_name`, `publication_name`, `published_at`,
  `article_title`, `article_url`, `extracted_at`, plus `speaker`, `quote_kind`, `topics`, `materiality`,
  `rationale`, `locator` (paragraph/sentence span), `quote_sha256`, `extractor_model`, `run_id` and a full
  `verification` record (every check, ruleset version, body and raw-page hashes).
- **Auditable:** SQLite triggers make `quotes` **append-only** (no UPDATE or DELETE). Every fetched page is kept as
  a gzip snapshot addressed by its SHA-256 hash, so any quote can be re-verified later (`mqcs audit`).
  Rejected model proposals are kept with the reasons they failed.
- **LLM-ready:** `data/export/quotes.jsonl` has one self-contained JSON object per quote with complete
  metadata, ready to load into a context window, a RAG index or a vector store. `/api/quotes` serves the same as JSON.
  A `manifest.json` records file hashes.

### 2.2 Detection: deterministic
- **Discovery** runs daily from fixed channels per source: Google-News sitemaps, RSS feeds, the BBC
  "Russia-Ukraine war" topic pages (paginated back to the start date), Reuters section and dated sitemaps.
  `--exhaustive` also scans the full BBC archive sitemaps, which list every article modified in the window.
- **Relevance** is decided by a versioned keyword ruleset (`config/relevance.json`), in two stages:
  candidate (URL + title) then full text (title or body term counts). The matched rule and term counts are
  stored with every article. No model is involved, so the same inputs always select the same articles.
- **Reading and reasoning:** the article body is extracted (site-specific parsers with a generic
  trafilatura fallback; captions, video placeholders, newsletter prompts and credit lines are removed) and
  Claude selects the material quotes.

### 2.3 Collection and verification
Deterministic checks before a quote is stored (`mqcs/quotes.py`):

| Check | Guards against |
|---|---|
| Text rebuilt from sentence IDs and compared with the model's copy (`model_copy_matches_source`) | hallucinated or altered wording, frankenquotes |
| Span is contiguous, ordered, at most 3 paragraphs and 6 sentences, never crosses a subheading | spliced or overlong passages |
| Text present verbatim in the stored article body | anything not on the page |
| Length bounds plus a contamination filter (ads, "sign up", URLs, table pipes, page numbers) | page-furniture pollution |
| Source, publication, title, URL and publication date all present (from the page's JSON-LD and meta tags) | empty or incorrect metadata |
| Named speaker appears in the article near the quote; otherwise the attribution is dropped | invented attribution |

**Formatting is preserved:** original punctuation and quotation marks are kept, multi-paragraph quotes keep
their paragraph breaks, and list items keep their bullets. The only change is collapsing whitespace.

**Period on request:** `backfill --from --to` (or the dashboard form) collects any past range and appends to the DB.
**New source on request:** `add-source` reads the outlet's robots.txt sitemaps and RSS feeds, registers a
generic adapter and backfills from 1 Oct 2026.

### 2.4 Daily digest (`reports/quotes-digest_<start>_to_<end>.md`)
- The title shows the period, e.g. *"Russia's War on Ukraine — Media Quotes Digest: 4–17 November 2026"*.
- Sections (`config/sections.json`): War Intensity & Battlefield, Peace Initiatives & Negotiations, International
  Support, Sanctions & Economic Pressure, Conflicting Parties' Resources, Domestic Politics, Humanitarian Impact,
  Escalation Risks, Expectations & Outlook.
- Claude receives every verified quote in the 14-day window and returns **IDs only**:
  - the section for each quote;
  - which quotes are **obsolete**, each citing the newer quote(s) that supersede it;
  - near-duplicates;
  - a 1–2 sentence inference per section.

  Code then enforces the rules:
  - unknown IDs are dropped;
  - an obsolete claim must cite a newer quote, or the quote is reinstated;
  - any quote the model forgot is placed under its topic, never silently dropped.
- The Markdown is **rendered from the database**, and the rendered file is then checked to confirm every quote and link
  is verbatim. Each digest has a `.decisions.json` sidecar listing what was excluded as obsolete and why.

### 3. Outputs
- Auditable database plus exports (above).
- **Dashboard** (`python -m mqcs dashboard`):
  - latest digests, rendered and downloadable, each with its obsolescence audit;
  - searchable quote database with per-quote provenance and verification;
  - articles with relevance evidence, extracted body and rejected proposals;
  - runs and logs filterable by level;
  - buttons to run the daily job, backfill a period, add a source, import a page or run the audit.
- Logs are written to the `events` table and to `logs/YYYY-MM-DD.jsonl`. Every run records its parameters and statistics.

### 4. Quality and robustness
- **Recall:**
  - several overlapping discovery channels per source, with a lenient candidate rule at stage 1;
  - a stricter full-text rule, then Claude asked to be complete within each article;
  - `--exhaustive` mode for archive-level recall.
- **Errors:**
  - retries with backoff on network errors and 5xx/429 responses;
  - robots.txt honoured, with blocked publishers detected and reported;
  - a failed article never aborts a run, and failed articles are retried next run;
  - the SDK retries API calls, with refusal fallback and truncation detection;
  - if no API key is set, articles wait in the `fetched` state and are processed on the next run.
- **Any size or type of article:** extraction works paragraph by paragraph with no length cap, and Opus 5.5 has a 1M-token context.
  Live pages, video pages and pages with little text are handled. Pages with no extractable body are logged as `failed`.

---

## Known limitation: Reuters
Reuters' `robots.txt` disallows automated clients, and its pages return HTTP 401 to non-browser requests.
The system **respects this**: it logs Reuters as blocked and does not try to get around the restriction.
The Reuters adapter (news sitemap, dated sitemaps, section pages, a `data-testid="paragraph-*"` parser) is
implemented and becomes active once access is permitted, for example through a licensed Reuters Connect feed,
an allow-listed user agent, or an approved proxy. Until then, Reuters articles can be ingested with
`import-html` or the dashboard upload form, and they go through exactly the same verification.

## Configuration
Environment variables (see `.env.example`):
- `ANTHROPIC_API_KEY`
- `MQCS_MODEL` (default `claude-opus-5-5`)
- `MQCS_START_DATE` (default `2026-10-01`)
- `MQCS_REPORT_WINDOW_DAYS` (default 14)
- `MQCS_DAILY_LOOKBACK_DAYS`
- `MQCS_REQUEST_INTERVAL` (politeness delay per host)
- `MQCS_LLM_CONCURRENCY`

API calls use structured outputs and server-side refusal fallbacks (`fallbacks: "default"`).

## Tests
`python -m pytest -q` runs offline tests:
- sentence splitting;
- every verification rule (frankenquote, hallucination, heading-crossing, missing metadata, untraceable speaker);
- the relevance rules;
- report-plan validation and verbatim rendering;
- append-only enforcement;
- an end-to-end run with a fake model (extraction → verification → storage → digest → audit → every dashboard page).

## Layout
```
mqcs/            settings, db, fetch, discovery, relevance, extract, quotes (verification), llm, report,
                 export, pipeline, logs, cli, dashboard/
config/          sources.json, relevance.json (versioned ruleset), sections.json
data/            quotes.db, raw/ (page snapshots), export/ (jsonl/json/csv + manifest)
reports/         daily digests (.md) + decision sidecars (.decisions.json)
logs/            JSONL event logs
```
