# CLAUDE.md — Resume Parser

## What this is
Pipeline: reads resumes from a client's **OneDrive** folder (HR already aggregated there) → routes by file type → parses with a vision-capable LLM → writes structured rows into the client's **existing Excel workbook**. Inputs are messy: JPGs, phone screenshots, scanned PDFs — Indian CA-applicant reality, low formatting discipline.

## How we work
- **One phase at a time.** Implement only the phase I explicitly name. Part 8 (endpoint vision) is a future roadmap — do not build it.
- A phase is **done** only when its DoD is satisfied, tests pass, and you've run the verification loop and reported results. Then **stop and wait for me.**

## Verification loop (MANDATORY before declaring any phase done)
1. Run `pytest` + `eval.py` for the current phase.
2. Report: tests pass/fail, metrics, review-rate.
3. If any test fails or metric misses target → fix and repeat. Do not claim done.
4. When green: summarize changes + DoD items satisfied, then stop.

**eval.py** runs the pipeline over the test corpus against ground-truth labels and prints: per-field accuracy, list-completeness (every education/experience/qualification entry captured), schema-valid %, hallucination %, $/resume, review-rate.

## Phases

**Phase 0 — Foundations.**
DoD: env + keys load; Pydantic schema defined; test corpus with ground-truth labels exists (every format: native PDF, scanned PDF, DOCX, DOC, JPG, screenshot, multi-column, low-DPI, CA-specific); test workbook exists.
Tests: smoke test loads keys, schema compiles, corpus + labels discoverable.

**Phase 1 — Vision PoC.**
DoD: one image/scanned resume → schema-valid JSON via Gemini.
Tests: ≥3 sample images parse; assert schema-valid + correct name/email/phone.

**Phase 2 — Multi-format routing.**
DoD: text path (PDF text layer, DOCX) + image preprocessing + DOC conversion + routing cascade with escalation loop (see Routing below).
Tests: every corpus format routes correctly; garbled-text-layer PDF and image-only PDF both escalate to vision.

**Phase 3 — Normalize / validate / score.**
DoD: date normalization, duration + total-experience computation, validation rules, confidence scoring, hallucination guard, low-confidence flagging.
Tests: messy dates normalize; injected hallucinated value caught; bad email/phone fails; low-confidence fixtures flagged.

**Phase 4 — Excel output + idempotent upsert.**
DoD: fields mapped → client columns (ASK me for headers first), provenance columns added, upsert by dedup key.
Tests: corpus run fills workbook correctly; re-running same input changes nothing; duplicate-candidate merges not appends.

**Phase 5 — OneDrive ingestion (via rclone) + batch orchestration.**
DoD: rate-limit-aware queue, retries + backoff, dead-letter, per-run report (counts, $, latency, review-rate). Build LocalFolderConnector (runs now) + RcloneConnector (lists via `rclone lsjson remote:path`, downloads via `rclone copyto`, delta via stored hash/modtime manifest). NO Azure account/app registration/subscription needed — rclone ships its own built-in OneDrive OAuth app.
Tests: full end-to-end over local connector; RcloneConnector unit tests mock the rclone subprocess (lsjson JSON output + copyto); mocked failure → dead-letter, not row; idempotent re-run adds nothing.

**Phase 6 — Testing harness.**
DoD: frozen golden set; per-field + per-format metrics; regression on every change.
Tests: harness runs + produces metrics; regression catches a deliberately broken prompt.

**Phase 7 — Hardening (only on explicit instruction).**
DoD: no-training model tier, PII/security, monitoring, scaling. Do not build until I ask.

## Schema ownership
Part 3 below is a reference schema — treat as starting point, not frozen contract. You design the final Pydantic v2 schema. Rules: capture ALL list entries (never just latest); always keep provenance/meta block; ask before inventing columns.

## Repo structure
Contract: `connector → router → extractor → record → sink`. New storage = new connector only. New output = new sink only.
```
resume-parser/
  connectors/    # StorageConnector interface + implementations
  routing/       # cascade + escalation loop
  extraction/    # model-agnostic Extractor (Gemini + Qwen adapters)
  prompts/       # versioned prompts — change = re-run golden set
  normalize/     # dates, durations, dedup keys
  validate/      # schema rules, confidence, hallucination guard
  output/        # ExcelSink + idempotent upsert
  tests/         # pytest + fixtures + frozen golden set
  eval.py        # verification-loop script
  run_single.py  # dev loop: one file → print record
  run_batch.py   # full pipeline over folder
  .env.example   # key names only
```

## Conventions
- Library choice is yours — pick what works, swap if it fights you.
- Secrets from env vars only; `.env.example` committed, `.env` gitignored.
- Fail gracefully: bad/uncertain file → flagged or dead-lettered, never crashes batch, never writes junk row.
- Idempotency: re-running same input never duplicates rows.
- Log counts/path/cost/failures — subject to PII rule below.

## NEVER DO
- Hardcode or commit secrets/keys/tokens.
- Put real candidate data through a free-tier model (trains on data). Test-only = synthetic/anonymized resumes.
- Log full PII or raw resume content at info level.
- Write a row from a failed or low-confidence parse without flagging it.
- Touch the client's production OneDrive folder or live Excel during dev — always a test copy.
- Perform irreversible/external actions (delete files, send messages, change permissions) — ask first.
- Silently undo the Decision Log choices below.

## OPEN QUESTIONS — ask me, don't guess
1. **Excel column headers** — needed before Phase 4. Not yet provided; ask me.
2. **Dedup match rule** — email only? email + phone? fuzzy name + DOB? Ask before implementing upsert.
3. **Confidence threshold** for review routing (starting default 0.7, calibrate in Phase 6 — but confirm).
4. **Storage target** — assume OneDrive unless I say otherwise.
5. **Schema edge fields** — unsure if a field belongs → ask, don't invent.

## Decision Log (do not silently undo)
- **Vision model required:** inputs include JPGs/screenshots with no text layer.
- **Routing cascade, cheap-first + vision escalation:** text layers frequently garbled or empty; route per page.
- **Model-agnostic extractor:** Gemini + Qwen behind one interface → A/B and tier-switch without rewrite.
- **No-training tier in production:** resume PII under India DPDP Act; free tier trains on data.
- **Idempotent upsert + provenance every row:** re-runnable, auditable, debuggable.
- **OneDrive via rclone (test):** rclone reads OneDrive for free using its own built-in OAuth app — no Azure account, app registration, or subscription. Pipeline treats downloaded files like a local folder. Production options: client registers their own Azure app and supplies credentials (zero cost to us), or we supply our own rclone client_id to avoid shared-app throttling.

---

## REFERENCE — Architecture & Spec

### Models
- **Use:** `gemini-2.5-flash`, temp 0, `response_mime_type="application/json"`, `response_schema=<PydanticModel>`.
- **Free tier:** 1,500 req/day, no card. **Test-only** — trains on data.
- **Production:** paid Gemini/Vertex or self-hosted Qwen3-VL (Apache 2.0). Never put real PII through free tier.
- **Qwen fallback:** OpenAI-compatible endpoint at DashScope (`qwen-vl-plus`). 1M+1M free tokens, 90 days.
- **Extractor interface:** `extract(images=None, text=None, schema=...) -> dict` with `GeminiExtractor` and `QwenExtractor` behind it.

### Pipeline
`ingest → hash(dedup) → route → [preprocess] → extract → normalize → validate+score → [review queue] → ExcelSink`

Cross-cutting: queue, concurrency capped to rate limit, retry+backoff on 429/5xx, dead-letter on repeated failure, structured logs, per-run cost/latency report.

### Routing cascade (route per page, cheap-first, escalate on fail)

| Signal | Path |
|---|---|
| jpg/png/webp/tiff/heic | vision |
| PDF, clean text layer (quality check passes) | text |
| PDF, garbled/empty text layer | vision |
| PDF, mixed pages | hybrid (per-page) |
| PDF AcroForm/XFA | form-field extraction |
| DOCX, rich text | text |
| DOCX, thin text + images/textboxes | render → vision |
| DOC (legacy) | LibreOffice convert → re-route |
| Encrypted (no password) / corrupt / zero-byte | flag → dead-letter |

**Vision-escalation triggers** (beyond empty text layer):
- Garbled text: no spaces, junk-char ratio >30%, <30 real word tokens
- DOCX text suspiciously short vs file size (textboxes/shapes swallowing content)
- Multi-column layout (reading order scrambled by linear extraction)
- Tables/grids (vision preserves row-column association)
- Rotated/skewed pages (deskew first, then vision)
- Non-Latin / mixed scripts
- Handwritten annotations, stamps, seals (common on Indian mark sheets)

**Escalation loop:** classify → extract → score (schema-valid? key fields present? confidence ≥ threshold? hallucination guard pass?) → if fail: escalate text→hybrid→vision, retry once → if still fail: flag → human review.

**Prompt rule:** reason about layout first, then extract all fields; capture ALL list entries; null if absent; never invent.

### Storage connectors

| Platform | API | Incremental | Cost | Hurdle |
|---|---|---|---|---|
| OneDrive/SharePoint Online | MS Graph DriveItem | /delta | Free (M365 license) | Low — app reg + admin consent |
| Google Drive | Drive API v3 | Changes API | Free but CASA audit ~$hundreds–thousands/yr for broad scope | High |
| Dropbox | API v2 | cursor | Free (on their plan) | Moderate |
| Box | REST + Events | Events API | Free (enterprise plan) | Moderate |
| S3/Azure Blob/GCS | SDK | list-diff/events | Billed per request (negligible) | Low |
| On-prem SMB/NFS | agent | fs watch | Free | Highest (VPN/agent) |
| Email (careers@) | IMAP/Graph/Gmail | UID/history-id | Free | Low |

OneDrive (test, zero-cost): use **rclone** — it has a built-in OneDrive OAuth app, so no Azure account/registration/subscription. Client (or you) runs `rclone config` once (browser login, pick OneDrive, leave client_id blank). RcloneConnector then shells out: `rclone lsjson remote:path` to list (returns Name/Size/ModTime/Hashes JSON), `rclone copyto remote:path/file localpath` to download, hash+modtime manifest for delta. On macOS prefer the CLI commands over `rclone mount` (mount needs macFUSE). Phase 5 builds LocalFolderConnector (runs now) + RcloneConnector (subprocess mocked in tests). Production: client registers their own Azure app + supplies credentials, or supply your own rclone client_id to dodge shared-app throttling. Direct Graph/MSAL (`/me/drive`, `/delta`) remains an option if a client prefers the native API.

### Schema (Pydantic v2 reference — adapt as needed, keep ALL list entries)

```python
# Identity
full_name, first_name, middle_name, last_name: str
emails: list[str]              # multiple OK
phones: list[str]              # normalize to +91...
location: {city, state, country}
date_of_birth: str|None        # YYYY-MM-DD
gender, nationality, marital_status: str|None
has_photo: bool                # flag only; quarantine image, no inferences
links: {linkedin, github, portfolio, other[]}
languages_known: [{language, proficiency}]
summary_objective: str|None

# Education — ALL entries
education: [{
  degree, specialization, institution, board_or_university,
  location, start_date, end_date,     # YYYY-MM
  duration_years,                      # derived
  status,                              # completed|pursuing|discontinued
  grade_type,                          # CGPA|percentage|grade
  grade_value
}]

# Qualifications — CRITICAL for CA
qualifications: [{
  name,                  # CA, CMA, CS, ACCA, CPA...
  body,                  # ICAI, ICSI, ICMAI...
  level,                 # Foundation|Intermediate|Final
  membership_number,     # ICAI membership no.
  attempts, date_cleared,
  status                 # cleared|pursuing|pending_result
}]

# Work experience — ALL entries
work_experience: [{
  company, designation,
  employment_type,       # full_time|part_time|contract|intern
  location, start_date, end_date, is_current,
  duration_months,       # derived
  responsibilities: list[str],
  achievements: list[str],
  tools_technologies: list[str],
  industry
}]

# Articleship — India CA-specific
articleship_internships: [{
  firm_or_org, role, start_date, end_date, duration_months,
  areas: list[str]       # audit, direct tax, GST, statutory audit...
}]

# Extras
skills: [{name, category, proficiency}]
projects: [{title, description, role, tools_technologies, duration}]
achievements_awards: list[str]
publications: list[str]
extracurriculars: list[str]

# Derived (computed, not extracted)
derived: {
  total_experience_years, current_employer, current_designation,
  highest_qualification, key_skill_tags: list[str]
}

# Provenance — always present, never skip
meta: {
  source_file, source_path, file_type, parse_timestamp,
  model_used, path_taken,      # text|vision|hybrid
  overall_confidence,          # 0–1
  field_confidences: dict,
  needs_review: bool,
  review_reasons: list[str]
}
```

### Testing targets (Phase 6 acceptance criteria)
- ≥95% field accuracy on key fields (name, email, phone, current role, each education/qualification entry)
- ≥99% schema-valid JSON
- <1% hallucination rate
- ≤fraction of a cent per resume
- ≤15% records needing manual review

**Hallucination guard:** verify each non-null scalar against source text (text-path); vision-sourced fields marked for lighter scrutiny. Flag if value not findable in source.

**Dedup key (default, confirm with me):** lowercase email → fallback phone → fallback fuzzy name+DOB. SHA1 hash as row key.

### Excel output
- Flat row: derived/summary fields as columns + `raw_json` (full nested record) + provenance columns.
- Field→column mapping as a single config dict — swap in real client headers in one place.
- Failed parses / `needs_review` → separate review sheet, never main sheet.
- Upsert: update existing row if dedup key matches, else append.
- Re-run = no change (assert on row count + content hash).

### Future layers (do NOT build now — roadmap only)
- L1: cover letters, supporting docs (certificates, mark sheets) parsed + linked; gov IDs detected but NOT stored
- L2: cross-file dedup, multi-file assembly, version history
- L3: canonical title/company/institution normalization, skill taxonomy, LinkedIn enrichment
- L4: semantic search via embeddings + vector store (highest ROI, lowest risk — do before scoring)
- L5: JD↔resume matching/ranking — WARNING: triggers anti-discrimination/AI-hiring law obligations (EEOC, NYC LL144, EU AI Act). Treat as regulated feature.
- L6: pipeline analytics, diversity analytics (governance required)
- L7: multi-destination sinks (ATS, HRIS, DB, BI)
