# Resume Parsing Pipeline — Architecture, Model Selection & Phased Build Guide

**The sample setup:** HR has already aggregated every resume into a OneDrive folder. Their "database" is an existing Excel workbook with a fixed set of columns. Your job is the **screening-phase parse**: take the messy pile (native PDFs, scanned PDFs, DOCX, DOC, JPGs, phone screenshots — Indian CA-applicant reality, low formatting discipline) and turn each file into a complete structured record that lands as a row in their Excel sheet, extracting *everything* (every education entry with years, every job with dates and description, qualifications, articleship, skills, the lot).

This document covers: the model decision (with current pricing/free tiers), the end-to-end architecture, the **routing engine in depth** (every edge case where vision is required), **storage connectors** (how to extract from OneDrive, Google Drive, Dropbox, Box, S3, network shares, and email — with auth, incremental sync, and real costs), the full data schema, a phase-by-phase build plan for a **test database**, a comprehensive testing strategy, and **the complete extraction surface** (the full end-state vision the current architecture can grow into).

---

## Part 1 — Model selection (the research, and the decision)

### 1.1 The decisive constraint: you need *vision*, not just an LLM

Because a large share of your inputs are JPGs, photos, and screenshots, a text-only model is a non-starter — there's no text layer to read. You need a **vision-language model (VLM)** that reads the pixels, OR an OCR→LLM pipeline. Industry guidance is consistent: vision parsing is the right engine for "short documents with complex visual layouts like scanned PDFs, tables, and image-based forms," while a pure-text engine is for clean digital text. Resumes from your applicant pool fall squarely in the vision/hybrid bucket.

**Important nuance (cost optimization):** not every file is an image. A native-text PDF or a DOCX *does* have a clean text layer. Feeding those as images wastes money and accuracy. The best systems use **auto-mode routing** — cheap text path when a reliable text layer exists, vision path when it doesn't, hybrid (text + image) for borderline cases. Hybrid approaches that combine extracted text with vision consistently beat screenshot-only methods (~5% accuracy lift in published benchmarks). Build the router; don't send everything to vision.

### 1.2 Candidate models (current pricing, mid-2026)

| Model | Vision? | Price (input / output per 1M tok) | Free tier | Notes |
|---|---|---|---|---|
| **Gemini 2.5 Flash** | Yes | $0.30 / $2.50 | Yes — 1,500 req/day, no card | GA, strong vision + native JSON schema output |
| **Gemini 2.5 Flash-Lite** | Yes | $0.10 / $0.40 (cheapest major model) | Yes | Ultra-budget; slightly lower quality |
| **Gemini 3 Flash / 3.1 Flash-Lite** | Yes | 3.1 Flash-Lite $0.25 / $1.50 | Yes | Newer; 3 Flash is Google's recommended free-tier model |
| **Qwen3-VL-235B-A22B** | Yes | ~$0.26 / $0.90 | DashScope free quota: 1M+1M tokens, 90 days | Top open VLM for docs/OCR; **Apache 2.0, self-hostable** |
| **Qwen-VL-Plus / smaller VL** | Yes | cheaper than 235B | Same shared free quota | Good cost/quality for high volume |
| GPT-class mini (vision) | Yes | meaningfully higher on vision | Limited | More expensive per page for this task; skip for cost |

Key facts that drove the decision:

- **Gemini's free tier is the most generous of any major provider** — roughly 1,500 requests/day on Flash models with **no credit card required**, and it doesn't expire. That's enough to parse a real test corpus for free.
- On a like-for-like document-extraction cost test, **Gemini Flash came in around $1.67 to process 10,000 pages**, versus roughly $50–100 for GPT-4-class vision — a ~30–60× difference. Per multi-page resume you're looking at a fraction of a cent.
- **Qwen3-VL is the strongest *open* option** for document/OCR work, is released under Apache 2.0 (so you can self-host via vLLM for near-zero marginal cost and full data control), and DashScope hands you **1M + 1M free tokens shared across all Qwen models for 90 days**.
- For context on the prize: **commercial resume-parsing APIs charge roughly $0.08–0.10 per resume.** An LLM pipeline runs at a small fraction of that — that gap is a big part of your business case.

### 1.3 ⚠️ The privacy trap you must design around now

**Gemini's free tier uses your prompts to improve Google's products.** Resume content is candidate PII. Therefore:

- **For the TEST RUN:** use the free tier, but feed it **synthetic / dummy / fully anonymized resumes only.** (You should use synthetic data for testing anyway — see Part 5 — because real annotated resume datasets are scarce and PII-laden. The free tier and the synthetic-data approach line up perfectly.)
- **For PRODUCTION with real candidate data:** you **must** move to a no-training tier:
  - **Gemini paid tier / Vertex AI** — paid and Vertex usage is *not* used for training; DPAs and data-residency available, or
  - **Qwen paid tier (DashScope)** — standard paid tiers are not used for training by default; DPA available on request, or
  - **Self-hosted Qwen3-VL** — data never leaves your infrastructure (best privacy, cheapest at high volume, more ops effort).
- This matters extra in India under the **DPDP Act 2023**. Decide the production model early so your architecture doesn't have to change later.

### 1.4 The recommendation

- **Test run:** **Gemini 2.5 Flash on the free tier** (no card, generous limits, native `response_schema` structured output, excellent vision). Use synthetic resumes. If you already hold credits elsewhere, **Qwen3-VL via DashScope's free quota** is the equivalent fallback.
- **Production:** **Gemini 2.5 Flash-Lite (paid / Vertex)** for cheapest-at-scale, OR **self-hosted Qwen3-VL** when data control and marginal cost dominate. Keep the extraction layer model-agnostic (see 2.2) so you can A/B them and switch without rewriting the pipeline.

> Model names and prices in this space change monthly. Before you start, re-check the live Gemini and DashScope pricing/model pages — treat the table above as accurate to mid-2026, not forever.

---

## Part 2 — End-to-end architecture

### 2.1 The pipeline at a glance

```
                          ┌─────────────────────────────────────────────┐
                          │  SOURCE: OneDrive folder (HR-aggregated)      │
                          └───────────────────────┬─────────────────────┘
                                                  │ Microsoft Graph API (list + download + delta)
                                                  ▼
   ┌──────────────────────────────────────────────────────────────────────────────┐
   │ 1. INGESTION                                                                    │
   │    - enumerate files, capture metadata (filename, path, role/folder, modified) │
   │    - download bytes, compute file hash (dedup + idempotency key)                │
   │    - push job onto queue                                                        │
   └───────────────────────────────┬───────────────────────────────────────────────┘
                                    ▼
   ┌──────────────────────────────────────────────────────────────────────────────┐
   │ 2. FILE TRIAGE / TYPE DETECTION  (route to cheapest correct path)              │
   │    PDF(text-layer) ─► TEXT PATH        PDF(scanned)/JPG/PNG/screenshot ─► VISION│
   │    DOCX ─► TEXT PATH (+embedded imgs)  DOC(legacy) ─► convert ─► reclassify     │
   └───────────────┬───────────────────────────────────────┬───────────────────────┘
                   ▼ TEXT PATH                               ▼ VISION PATH
   ┌────────────────────────────────┐        ┌───────────────────────────────────────┐
   │ 2a. extract text layer          │        │ 2b. image prep: rasterize PDF pages,   │
   │     (PyMuPDF / pdfplumber /     │        │     deskew, upscale low-DPI, denoise,  │
   │      python-docx / mammoth)     │        │     normalize size                     │
   └───────────────┬────────────────┘        └───────────────────┬───────────────────┘
                   └──────────────┬────────────────────────────-─┘
                                  ▼  (HYBRID for borderline = text + image together)
   ┌──────────────────────────────────────────────────────────────────────────────┐
   │ 3. EXTRACTION ENGINE  (model-agnostic adapter)                                 │
   │    - strict JSON schema / structured output (response_schema / function call)  │
   │    - "reason then extract" + optional task decomposition for hard sections      │
   │    - returns full record + raw model output                                    │
   └───────────────────────────────┬───────────────────────────────────────────────┘
                                    ▼
   ┌──────────────────────────────────────────────────────────────────────────────┐
   │ 4. NORMALIZE + ENRICH                                                           │
   │    - parse/normalize dates, compute durations & total experience               │
   │    - map skills to taxonomy, normalize titles/companies                        │
   │    - India/CA fields (CA/CMA/CS level, ICAI no., articleship)                   │
   │    - build dedup key (name+email+phone fuzzy)                                   │
   └───────────────────────────────┬───────────────────────────────────────────────┘
                                    ▼
   ┌──────────────────────────────────────────────────────────────────────────────┐
   │ 5. VALIDATE + CONFIDENCE                                                        │
   │    - schema validation, business rules (email/phone/date sanity)               │
   │    - per-field + overall confidence; hallucination guard (value present in src?)│
   │    - if low-confidence ─► REVIEW QUEUE (human-in-the-loop)                      │
   └───────────────┬───────────────────────────────────┬───────────────────────────┘
                   ▼ pass                               ▼ flagged
   ┌────────────────────────────────┐     ┌──────────────────────────────────────────┐
   │ 6. EXCEL WRITER                 │     │ 6b. lightweight review sheet/UI; corrected │
   │   - map fields ─► existing cols │     │     rows feed back as new ground truth     │
   │   - idempotent upsert by key    │     └──────────────────────────────────────────┘
   │   - provenance cols (src, model,│
   │     confidence, parse date)     │
   │   - write back to OneDrive      │
   └────────────────────────────────┘
            ▲
   Cross-cutting: orchestration (queue, concurrency within rate limits, retries+backoff,
   dead-letter), structured logging, cost/latency tracking, run reports.
```

### 2.2 Why a model-agnostic extraction adapter

Wrap the model call behind one interface — `extract(record_schema, text=None, images=None) -> dict`. Underneath, have a `GeminiExtractor` and a `QwenExtractor`. Benefits: you run the **same test set through both** to pick the accuracy/cost winner (Part 5), you swap free-tier → paid/self-hosted for production by changing one line, and you're insulated from monthly model churn.

### 2.3 Key design principles (all research-backed)

1. **Route, don't blanket-vision.** Text path for files with a real text layer; vision only when needed. Saves cost and improves accuracy.
2. **Strict structured output.** Use the model's native JSON-schema / structured-output mode (Gemini `response_schema`, or function-calling on the OpenAI-compatible Qwen endpoint). This pushes valid-JSON rates well into the high-90s%.
3. **Reason before extracting, and decompose the hard parts.** A single giant "extract everything" prompt can *degrade* accuracy because the model juggles too many disparate targets at once. Two mitigations: (a) let the model briefly reason about layout/structure before emitting JSON, and (b) for sections that underperform (typically work experience and education with messy dates), run a **second, specialized pass** just for that section and merge. Start with one structured call; decompose only where testing shows weakness.
4. **Idempotency everywhere.** File hash + candidate dedup key means re-running the folder never creates duplicate rows.
5. **Provenance on every row.** Store source file, parse timestamp, model used, and confidence. You cannot debug or audit a parse you can't trace.

### 2.4 The routing engine in depth (every edge case that needs vision)

The naive check — "does the file have a text layer of length > N?" — is necessary but nowhere near sufficient. Build the router as a **decision cascade with an escalation loop**: start on the cheapest viable path, run extraction, score the result, and automatically re-route to vision if quality signals fail. Two principles make it robust:

- **Route per *page*, not just per file.** A single PDF can be typed on page 1 and a scanned image on page 2.
- **Cheap-first, vision-as-safety-net.** Vision is the fallback that catches the hard 10–20%, so average cost stays low while accuracy stays high.

**Cases that demand vision (or hybrid) even when a file "looks" text-based:**

1. **Garbled / broken text layer.** Some PDFs extract as gibberish — bad font encoding, missing `ToUnicode` maps, ligature soup, or no spaces between words. The length check passes but the text is useless. Detect with quality heuristics: ratio of real dictionary words, proportion of non-alphanumeric characters, average "word" length, and whether spaces exist at all. Low quality → vision.
2. **Partial text layer.** Typed body plus image blocks — a scanned signature, a pasted screenshot of a certificate, a logo-heavy header. Route the image regions to vision (hybrid).
3. **The resume is one full-page image inside a PDF or DOCX.** Extremely common in your applicant pool: people "Save as PDF" from a photo, or paste a phone screenshot into Word. The text layer is empty/trivial despite the `.pdf`/`.docx` extension → vision.
4. **DOCX with content in text boxes, shapes, SmartArt, headers/footers, or tables.** `python-docx` silently misses text boxes and many shapes. If extracted text is suspiciously short relative to file size or embedded-image count, fall back to rendering DOCX → PDF → image → vision.
5. **Complex multi-column layouts.** Linear text extraction scrambles reading order (the left column interleaves with the right). Detect multi-column geometry and prefer vision or a layout-aware extractor so the model sees spatial structure.
6. **Tables and grids** — skills matrices, education tables, semester marks. Flat text destroys row/column association; vision preserves it.
7. **Rotated or skewed pages.** Scanned sideways or photographed at an angle. Deskew/rotate in preprocessing, then vision (even an existing OCR layer on a rotated scan is often wrong).
8. **Non-Latin or mixed scripts** where embedded-font extraction is unreliable. Vision OCR is more robust.
9. **Handwritten annotations, stamps, signatures, seals** — common on Indian mark sheets and articleship/experience certificates. Vision.
10. **AcroForm / XFA PDF forms** (rare for resumes, common for application forms). Pull field values via the form API — not text extraction, not vision.
11. **Encrypted / password-protected PDFs.** Decrypt with a known password first; if none is available, flag → review. Never guess.
12. **Corrupt / zero-byte / unsupported files.** Flag → dead-letter; never write a row from garbage.

**The routing decision table:**

| Input signal | Path |
|---|---|
| Image extension (jpg/png/webp/tiff/heic) | **vision** |
| PDF, rich + clean text layer (quality check passes) | **text** |
| PDF, text layer present but low-quality/garbled | **vision** |
| PDF, empty/trivial text layer (image-only) | **vision** |
| PDF, mixed typed + image pages | **hybrid / per-page** |
| PDF AcroForm/XFA | **form-field extraction** |
| DOCX, rich extractable text | **text** |
| DOCX, short text + images/textboxes | **render → vision** |
| Legacy DOC | **convert (LibreOffice) → re-route** |
| Encrypted (no password) / corrupt | **flag → review / dead-letter** |

**The escalation loop (the real "auto-mode"):**

```
classify (extension + text-layer quality probe) ─► pick text | hybrid | vision | form
        │
        ▼
   run extraction
        │
        ▼
   score result: schema-valid? key fields present (name/contact/≥1 education)?
                 overall confidence ≥ threshold? hallucination guard passes?
        │
   ┌────┴────┐
   ▼ pass    ▼ fail
  ship    escalate one tier (text→hybrid→vision) and retry once
                 │
                 ▼ still failing ─► flag → human review
```

This is what keeps you cheap *and* accurate: ~80% of files clear on the text path at near-zero cost; the rest escalate to vision automatically, and only the genuinely unparseable ones reach a human.

### 2.5 File storage connectors — extracting from every system firms use

HR has already aggregated the resumes; your job is to *read* from wherever they put them. Different firms use different stores, so build a **`StorageConnector` abstraction** — a single interface (`list_files()`, `download(file_id)`, `delta()/changes()`) with one implementation per platform. This is not just clean engineering: **storage-agnostic ingestion *is* your "plug into their existing architecture" selling point.** Add a connector, win a new class of customer, without touching the rest of the pipeline.

Almost every cloud platform exposes a **changes/delta API**, so after the first full pass you only fetch *new* files — essential for cost and idempotency.

**Connector comparison:**

| Platform | API | Auth | Incremental sync | API itself free? | Onboarding hurdle |
|---|---|---|---|---|---|
| **OneDrive / OneDrive for Business / SharePoint Online** | Microsoft Graph (DriveItem) | Entra ID app; OAuth2; delegated or app-only | `/delta` | **Yes** (cost = existing M365 license; throttled, not billed) | **Low** — app registration + admin consent |
| **Google Drive / Shared Drives** | Google Drive API v3 | GCP project; OAuth2 or service account (+ domain-wide delegation) | Changes API | **Yes** (generous free quota) — **but see CASA below** | **High** — restricted-scope verification + paid annual audit |
| **SharePoint On-Premises** | SharePoint REST / CSOM | NTLM/Kerberos, on-network | change log | Yes (their infra) | **High** — runs inside their network |
| **Dropbox / Dropbox Business** | Dropbox API v2 | OAuth2 (+ team scopes/admin for Business) | cursor (`list_folder/continue`) | **Yes** (cost = their plan) | Moderate |
| **Box** | Box API (REST) | OAuth2 / JWT / Client-Credentials (app-only) | Events API | **Yes** (enterprise plan) | Moderate — admin app authorization |
| **Amazon S3** (≈ Azure Blob, GCS) | AWS SDK (boto3) | IAM keys/roles | event notifications / list-diff | **No** — per-request + storage + egress (tiny but billed) | Low — scoped credentials |
| **Network file share / on-prem server** | SMB/NFS (or on-prem agent) | AD / service account | filesystem watch | Yes (their infra) | **Highest** — needs VPN / installed agent |
| **Email inbox (careers@)** | IMAP / Gmail API / Graph mail | mailbox creds / OAuth2 | UID / history-id | **Yes** | Low–moderate |

**Platform notes and gotchas:**

- **OneDrive / SharePoint Online (Microsoft Graph).** Your primary case. Enumerate via `/me/drive/root/children` or `/drives/{id}/items/{item-id}/children`; SharePoint document libraries are drives under `/sites/{site-id}/drives`; download via the item's `@microsoft.graph.downloadUrl` or `/content`; incremental sync via `/delta`. **The Graph API is not billed per call** — the cost is the M365 licensing the company already pays. It enforces **dynamic throttling** instead: HTTP 429 with a `Retry-After` header, a resource-unit model (a read ≈ 1 RU, a write/upload ≈ 2 RU), license-based daily quotas (on the order of millions of RUs/day for a mid-size tenant), and a global ceiling around 130,000 requests per 10 seconds per app across all tenants. Design for `$select`/`$filter` to shrink payloads, batch where possible, and back off on 429. **Onboarding is easy:** register an Entra ID app and get tenant-admin consent for app-only access. No paid security audit. **Zero-cost test path:** for development and pilots you can skip Azure entirely and use **rclone**, which has its own built-in OneDrive OAuth app — `rclone config` once (browser login), then read the folder via `rclone lsjson` / `rclone copyto`. This needs no Azure account, app registration, or subscription, and is the approach the build uses for Phase 5.

- **Google Drive.** The API is free with generous quotas — **but reading users' existing Drive files broadly requires *restricted* scopes (e.g., `drive.readonly`), which trigger Google's CASA security assessment**: a paid, third-party audit you must pass and then **re-certify every 12 months**. Google charges nothing itself, but you pay the independent assessor; costs vary by tier and assessor (roughly a few hundred to a few thousand dollars/year at the common tier, higher at the top tier or with some assessors). There's also a **100-user cap until your app is verified**, and the cap is *scope-specific*. **Mitigations:** (a) use the narrower `drive.file` scope, where the app only sees files the user explicitly picks via the Google Picker — this avoids CASA but breaks "scan the whole folder automatically"; or (b) have each client run the connector inside *their own* Google Cloud project so the verification burden is theirs, not yours. **Business takeaway: OneDrive is materially cheaper and faster to onboard than Google Drive for a multi-tenant SaaS — factor that into which clients you pursue first and how you price Google-Drive onboarding.**

- **SharePoint On-Premises** (older/regulated firms). No Graph; use SharePoint REST/CSOM with NTLM/Kerberos, and you must run inside their network. Treat like the network-share pattern.

- **Dropbox / Dropbox Business.** `files/list_folder` + `files/download`, cursor-based delta. API access is free; you ride on the account's storage plan. Business team-member file access needs team scopes and admin approval.

- **Box** (common in finance/regulated enterprises). REST API with OAuth2, JWT app auth, or the Client-Credentials Grant for app-only enterprise access; Events API for incremental changes. API is free; enterprise plan and admin app-authorization required.

- **Amazon S3 / Azure Blob / Google Cloud Storage.** Some firms store in raw object buckets. Use the cloud SDK (`boto3` `list_objects_v2` + `get_object` for S3). **Unlike the SaaS file platforms, object stores are billed per request** — an S3 GET is on the order of $0.0004 per 1,000 requests, plus storage and egress. Negligible at resume volumes, but it's the company's bill, and you should scope credentials tightly (read-only, single prefix).

- **Network file shares / on-prem servers (SMB/NFS).** The traditional-firm reality. There's no cloud API, so the robust pattern is a small **on-prem agent/connector** you deploy inside their network: it watches the folder and ships file bytes (or just metadata + content) to your pipeline over an authenticated channel — or you run the whole pipeline on-prem. This is the heaviest integration (VPN / installed software / their IT's security review) but it's unavoidable for air-gapped firms, and it's exactly the kind of bespoke fit that justifies an integration fee.

- **Email inboxes (careers@).** For some firms the "aggregation" is literally an inbox. Pull attachments via IMAP (universal), the Gmail API, or Graph (Outlook mail). Free; low effort.

- **ATS / HRIS exports.** If a client already runs an ATS, ingest via its API or a CSV export instead of files. Secondary to your stated OneDrive→Excel wedge, but the same connector pattern applies.

**The unifying point:** one `StorageConnector` interface, many implementations, all feeding the identical downstream pipeline. The connector you reach for is a per-client config choice — which is precisely the "personalization" you're selling.

---

## Part 3 — The complete data schema ("absolutely everything")

This is the contract that flows from the extractor → Excel. Define it once (as JSON Schema or Pydantic), tuned for the Indian/CA context. Lists capture *all* entries, not just the latest.

```jsonc
{
  // ---- Identity & contact ----
  "full_name": "string",
  "first_name": "string", "middle_name": "string|null", "last_name": "string",
  "emails": ["string"],            // can be multiple
  "phones": ["string"],            // normalized to +91… where possible
  "location": { "city": "string|null", "state": "string|null", "country": "string|null" },
  "date_of_birth": "YYYY-MM-DD|null",
  "gender": "string|null",         // present on many Indian resumes
  "nationality": "string|null",
  "marital_status": "string|null",
  "has_photo": "boolean",          // flag; quarantine the image, don't store sensitive inferences
  "links": { "linkedin": "url|null", "github": "url|null", "portfolio": "url|null", "other": ["url"] },
  "languages_known": [ { "language": "string", "proficiency": "string|null" } ],

  // ---- Summary ----
  "summary_objective": "string|null",

  // ---- Education (ALL entries) ----
  "education": [
    {
      "degree": "string",                 // e.g., B.Com, M.Com, Class XII
      "specialization": "string|null",
      "institution": "string|null",
      "board_or_university": "string|null",
      "location": "string|null",
      "start_date": "YYYY-MM|null",
      "end_date": "YYYY-MM|null",
      "duration_years": "number|null",    // derived
      "status": "completed|pursuing|discontinued|null",
      "grade_type": "CGPA|percentage|grade|null",
      "grade_value": "string|null"
    }
  ],

  // ---- Professional qualifications / certifications (CRITICAL for CA) ----
  "qualifications": [
    {
      "name": "string",                   // CA, CMA, CS, ACCA, CPA, etc.
      "body": "string|null",              // ICAI, ICSI, ICMAI…
      "level": "Foundation|Intermediate|Final|null",
      "membership_number": "string|null", // e.g., ICAI membership no.
      "attempts": "string|null",
      "date_cleared": "YYYY-MM|null",
      "status": "cleared|pursuing|pending_result|null"
    }
  ],

  // ---- Work experience (ALL entries, with full descriptions) ----
  "work_experience": [
    {
      "company": "string",
      "designation": "string",
      "employment_type": "full_time|part_time|contract|intern|null",
      "location": "string|null",
      "start_date": "YYYY-MM|null",
      "end_date": "YYYY-MM|null",
      "is_current": "boolean",
      "duration_months": "number|null",   // derived
      "responsibilities": ["string"],     // bullet points, verbatim-ish
      "achievements": ["string"],
      "tools_technologies": ["string"],
      "industry": "string|null"
    }
  ],

  // ---- Articleship / internships (India CA-specific) ----
  "articleship_internships": [
    {
      "firm_or_org": "string",
      "role": "string|null",
      "start_date": "YYYY-MM|null",
      "end_date": "YYYY-MM|null",
      "duration_months": "number|null",
      "areas": ["string"]                 // audit, direct tax, GST, statutory audit…
    }
  ],

  // ---- Skills, projects, extras ----
  "skills": [ { "name": "string", "category": "string|null", "proficiency": "string|null" } ],
  "projects": [ { "title": "string", "description": "string|null", "role": "string|null",
                  "tools_technologies": ["string"], "duration": "string|null" } ],
  "achievements_awards": ["string"],
  "publications": ["string"],
  "extracurriculars": ["string"],

  // ---- Derived / computed fields ----
  "derived": {
    "total_experience_years": "number|null",
    "current_employer": "string|null",
    "current_designation": "string|null",
    "highest_qualification": "string|null",
    "key_skill_tags": ["string"]
  },

  // ---- Provenance & QA metadata (NOT from the resume) ----
  "meta": {
    "source_file": "string", "source_path": "string", "file_type": "string",
    "parse_timestamp": "ISO-8601", "model_used": "string", "path_taken": "text|vision|hybrid",
    "overall_confidence": "number 0-1",
    "field_confidences": { "field_name": "number 0-1" },
    "needs_review": "boolean", "review_reasons": ["string"]
  }
}
```

**Excel mapping note:** the workbook is flat (one row per candidate) but the schema has nested lists. Decide a flattening convention up front, e.g.: one row per candidate with the *derived* summary fields in dedicated columns, plus the full nested JSON dumped into a `raw_json` column (so nothing is ever lost), OR companion sheets (`Education`, `Experience`) keyed by a `candidate_id`. For the test, the single-row-plus-`raw_json` approach is simplest and reversible.

---

## Part 4 — Phased build plan (for the test database)

Each phase has a goal, the work, and an **exit criterion** you must hit before moving on. Estimates assume one developer.

### Phase 0 — Foundations & test corpus (1–2 days)
- Get API access: **Gemini free tier** key from Google AI Studio (no card); optionally a **DashScope** key for the Qwen comparison.
- Set up the repo, Python env, and a `.env` for keys (never commit keys).
- **Finalize the schema** (Part 3) as Pydantic models.
- **Assemble the test corpus** (the single most important asset): 50–200 resumes spanning every format and failure mode — native PDF, scanned PDF, DOCX, legacy DOC, clean JPG, *phone screenshot*, multi-column, multi-page, single-page, designer/infographic, low-DPI scan, mixed English+regional, and CA-specific layouts (articleship, ICAI numbers). Use **synthetic resumes** you generate (so you know the ground truth *and* can legally run them through the free tier), plus a handful of real ones you've anonymized.
- Create the **target test workbook** mirroring the client's real columns.
- **Exit:** keys work; corpus + ground-truth labels exist; schema compiles.

### Phase 1 — Single-resume PoC, vision path (1–2 days)
- One image/scanned PDF → strict JSON via Gemini Flash `response_schema`.
- Validate it parses against your Pydantic schema; eyeball field accuracy.
- **Exit:** one messy image resume produces schema-valid JSON with the obvious fields correct.

### Phase 2 — Multi-format handling + routing (2–4 days)
- Add the **text path** (PyMuPDF/pdfplumber for text PDFs, python-docx/mammoth for DOCX).
- Add **image preprocessing** (rasterize PDF pages, deskew, upscale, denoise).
- Add legacy **DOC conversion** (LibreOffice headless → reclassify).
- Implement the **auto-router**: text layer present and rich → text path; else vision; borderline → hybrid.
- **Exit:** every file type in the corpus is correctly routed and produces a record.

### Phase 3 — Normalize, enrich, validate, score (2–3 days)
- Date normalization, duration & total-experience computation, title/company normalization, skill-tag mapping, dedup-key generation, India/CA field handling.
- Validation rules (email/phone format, date sanity, end ≥ start, no future dates).
- **Confidence scoring** (model signal + rule checks) and a **hallucination guard** (is each extracted value actually findable in the source text/image?).
- Flag low-confidence records with reasons.
- **Exit:** records are normalized; bad/uncertain ones are flagged, not silently shipped.

### Phase 4 — Excel output + idempotent upsert (1–2 days)
- Map normalized fields → existing columns; add provenance columns; dump `raw_json`.
- **Idempotent upsert** by dedup key (re-running never duplicates).
- Write back: for the test, `openpyxl` on a copy; for a live shared workbook, the Graph **workbook/table API** (add-rows in a session) to avoid overwrite conflicts.
- **Exit:** running the corpus fills the workbook correctly; re-running changes nothing.

### Phase 5 — OneDrive ingestion (via rclone) + batch orchestration (2–4 days)
- Access OneDrive through **rclone**, not the Azure Graph SDK — rclone ships its own built-in OneDrive OAuth app, so **no Azure account, app registration, or subscription is needed**. One-time setup: `brew install rclone` then `rclone config` (pick OneDrive, leave client_id blank, browser login).
- Build a `LocalFolderConnector` (runs against `test_corpus/files/`) and an `RcloneConnector` behind the `StorageConnector` interface. The RcloneConnector shells out to the CLI: `rclone lsjson <remote>:<path>` to list (returns Name/Size/ModTime/Hashes), `rclone copyto` to download, and a stored hash/modtime manifest for delta. Prefer CLI calls over `rclone mount` (mount needs macFUSE on macOS).
- Orchestrate the full run: a queue/loop with **concurrency capped to the model's rate limit**, retries with exponential backoff, a dead-letter for repeated failures, structured logging, and a per-run report (counts, $ spent, time, review rate).
- Test by mocking the rclone subprocess (fake `lsjson` JSON in, assert parsing + correct `copyto` command out) — no live remote required.
- **Exit:** point it at a local folder (or a configured rclone remote), walk away, come back to a populated workbook + a run report. Production: client registers their own Azure app and supplies a client_id to rclone, or prefers the direct Graph/MSAL path.

### Phase 6 — Comprehensive testing & evaluation (runs in parallel from Phase 1)
See Part 5. This is not a phase you "finish" — it's the harness you build early and run on every change.

### Phase 7 — Hardening & production readiness (later, before real clients)
- Swap free tier → **no-training paid/Vertex or self-hosted Qwen** (PII).
- Security: encryption at rest/in transit, access control, secrets manager, audit logs; DPDP/GDPR posture; sensitive-attribute handling (flag/quarantine photo, gender, DOB rather than scoring on them).
- Monitoring/alerting, cost dashboards, scaling for campus-season bursts.

---

## Part 5 — Comprehensive testing strategy

Resume parsing fails in subtle, field-specific ways (a wrong end-date, a missed second degree, an invented phone number). Aggregate "looks good" is not testing. Build this harness in Phase 1 and run it continuously.

### 5.1 Ground-truth dataset
- Hand-label (or generate-with-known-answers) every resume in the corpus into the schema. **Synthetic resumes are gold here** — you control the ground truth exactly, and they're safe to run through the free tier. Published resume-IE work generates thousands of layout-diverse synthetic resumes precisely because real labeled data is scarce and private; do the same at smaller scale.
- Stratify the set so each format/failure mode (Phase 0 list) is represented — you want **per-format** scores, not just an average that hides the screenshot disaster.

### 5.2 Metrics (compute per-field *and* per-format)
- **Field-level precision / recall / F1.** Exact-match for structured fields (dates, emails); fuzzy-match (e.g., token/Levenshtein) for names, companies, titles.
- **List completeness (entry recall):** did you capture *all* education / work / qualification entries, or drop the second job? This is the metric most naive parsers fail.
- **Normalized-value accuracy:** are computed dates, durations, and total-experience correct?
- **Schema-valid JSON rate:** target ≥99%.
- **Hallucination rate:** fraction of emitted values *not* present in the source — the most dangerous error class for hiring data. Target near-zero.
- **Overall record acceptance rate:** % of records good enough to ship without human edits.

### 5.3 Confidence calibration
- Check that low-confidence flags actually correlate with real errors. Plot confidence vs. observed accuracy; tune the **review-queue threshold** so you catch most errors while keeping the human review rate low (e.g., aim to auto-accept ~85–90% and route the rest).

### 5.4 Cost & latency benchmarking
- Measure tokens/resume, **$/resume**, $/1,000 resumes, and p50/p95 latency. Confirm the unit economics beat the ~$0.08–0.10/resume commercial benchmark by a wide margin (it will).
- Measure throughput under the free-tier rate limit so you know how long a real folder takes.

### 5.5 Model A/B (this is how you *pick* the model)
- Run the **identical ground-truth set** through Gemini 2.5 Flash, Gemini 2.5 Flash-Lite, and Qwen3-VL. Plot the **accuracy-vs-cost frontier**. Pick the cheapest model that clears your accuracy bar; keep the runner-up as a fallback. The model-agnostic adapter (2.2) makes this a config change, not a rewrite.

### 5.6 Robustness / adversarial suite
- Rotated images, very low DPI, watermarks, huge files, password-removed PDFs, two-people-in-one-file edge cases, blank/garbage files. The pipeline should fail *gracefully* (flag + dead-letter), never crash or write garbage rows.

### 5.7 Regression testing
- Freeze a **golden set** with locked expected outputs. Re-run it automatically on every prompt tweak, schema change, or model swap. Resume parsing is brittle to prompt/model changes — without regression tests you'll fix one format and silently break another.

### 5.8 The human-review feedback loop (active learning)
- Every correction a reviewer makes becomes a **new labeled example.** Feed those back into the ground-truth set and your prompt few-shot examples. Track review-rate over time — it should fall as the system learns your real-world distribution.

### 5.9 Acceptance criteria (set explicit targets before you start)
Example bar to declare the test a success:
- ≥95% field accuracy on key fields (name, email, phone, current role, each education entry, each qualification),
- ≥99% schema-valid JSON,
- <1% hallucination rate,
- ≤ a fraction of a cent per resume,
- ≤10–15% of records needing manual review.

---

## Part 6 — Suggested tech stack (keep the test lean)

- **Language:** Python.
- **Ingestion:** Microsoft Graph (MSAL for auth; Graph REST for list/download/delta; Graph workbook API for live Excel writes).
- **Text extraction:** PyMuPDF (`fitz`) or `pdfplumber` (PDF text), `python-docx` / `mammoth` (DOCX), LibreOffice headless (legacy DOC).
- **Image prep:** Pillow + OpenCV (deskew, upscale, denoise), `pdf2image`/PyMuPDF for rasterizing.
- **Model calls:** `google-genai` SDK for Gemini; the OpenAI-compatible client pointed at DashScope for Qwen (so both share one code shape).
- **Schema/validation:** Pydantic.
- **Excel:** `openpyxl` (test) / Graph workbook API (production live file).
- **Orchestration (test):** a simple job loop or a small queue; add Celery/RQ only if needed.
- **Testing:** `pytest` for the regression/golden-set harness; a small notebook for the metrics dashboards.

---

## Part 7 — Starter code skeletons

> Illustrative and current to mid-2026 SDKs; check the live SDK docs as they evolve. Keys come from environment variables — never hard-code them.

**Pydantic schema (abbreviated — expand to the full Part 3 schema):**
```python
from pydantic import BaseModel
from typing import List, Optional

class Education(BaseModel):
    degree: str
    specialization: Optional[str] = None
    institution: Optional[str] = None
    board_or_university: Optional[str] = None
    start_date: Optional[str] = None      # "YYYY-MM"
    end_date: Optional[str] = None
    status: Optional[str] = None
    grade_type: Optional[str] = None
    grade_value: Optional[str] = None

class WorkExperience(BaseModel):
    company: str
    designation: str
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    is_current: bool = False
    responsibilities: List[str] = []
    tools_technologies: List[str] = []

class Resume(BaseModel):
    full_name: str
    emails: List[str] = []
    phones: List[str] = []
    education: List[Education] = []
    work_experience: List[WorkExperience] = []
    # … add qualifications, articleship, skills, projects, derived, meta …
```

**Vision extraction via Gemini (free tier), structured output:**
```python
from google import genai
from PIL import Image
import os

client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

PROMPT = (
    "You are a meticulous resume parser. First, briefly reason about the document's "
    "layout and reading order. Then extract EVERY field into the given schema. "
    "Capture ALL education, work, and qualification entries — never just the latest. "
    "If a value is not present, use null. Do not invent values."
)

def extract_from_image(image_path: str) -> Resume:
    img = Image.open(image_path)
    resp = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=[PROMPT, img],
        config={
            "response_mime_type": "application/json",
            "response_schema": Resume,   # Gemini enforces the Pydantic schema
            "temperature": 0,
        },
    )
    return Resume.model_validate_json(resp.text)
```

**Qwen alternative (OpenAI-compatible endpoint) — same shape, swap-in:**
```python
from openai import OpenAI
import os, base64

qwen = OpenAI(
    api_key=os.environ["DASHSCOPE_API_KEY"],
    base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",  # SEA endpoint
)

def extract_with_qwen(image_path: str) -> dict:
    b64 = base64.b64encode(open(image_path, "rb").read()).decode()
    resp = qwen.chat.completions.create(
        model="qwen-vl-plus",
        messages=[{"role": "user", "content": [
            {"type": "text", "text": PROMPT + " Respond ONLY with JSON."},
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ]}],
        temperature=0,
    )
    import json
    return json.loads(resp.choices[0].message.content)
```

**Router (cascade + quality probe; pairs with the escalation loop in 2.4):**
```python
import fitz  # PyMuPDF
import re

# crude but effective text-quality heuristic to catch garbled text layers
def text_quality_ok(text: str) -> bool:
    t = text.strip()
    if len(t) < 200:
        return False
    if " " not in t:                       # no spaces => almost certainly garbled
        return False
    alnum = sum(c.isalnum() or c.isspace() for c in t)
    if alnum / max(len(t), 1) < 0.7:       # too much junk/non-text noise
        return False
    words = re.findall(r"[A-Za-z]{2,}", t)
    if len(words) < 30:                    # not enough real word-like tokens
        return False
    return True

def pdf_signal(pdf_path: str) -> str:
    doc = fitz.open(pdf_path)
    if doc.is_encrypted:
        return "flag"                      # needs password; do not guess
    if doc.first_widget is not None:       # AcroForm/XFA fields present
        return "form"
    text = "".join(p.get_text() for p in doc)
    img_pages = sum(1 for p in doc if p.get_images())
    if not text.strip():                   # image-only PDF
        return "vision"
    if not text_quality_ok(text):          # garbled / broken text layer
        return "vision"
    if img_pages and len(text.strip()) < 500 * img_pages:
        return "hybrid"                    # mostly images with thin text
    return "text"

def route(path: str) -> str:
    ext = path.lower().rsplit(".", 1)[-1]
    if ext in {"jpg", "jpeg", "png", "webp", "tiff", "heic"}:
        return "vision"
    if ext == "pdf":
        return pdf_signal(path)
    if ext == "docx":
        return "text"          # downstream: if extracted text is thin, render->vision
    if ext == "doc":
        return "convert_then_route"
    return "vision"            # unknown extension: let the VLM try, else it gets flagged

def extract_with_escalation(path, text_extract, vision_extract, score) -> dict:
    """Cheap-first, vision-as-safety-net. `score` returns True if the record is good enough."""
    plan = route(path)
    if plan in {"flag", "form"}:
        return {"path": plan, "needs_review": plan == "flag"}  # handle separately
    order = {"text": ["text", "hybrid", "vision"],
             "hybrid": ["hybrid", "vision"],
             "vision": ["vision"],
             "convert_then_route": ["vision"]}[plan]
    last = None
    for tier in order:
        rec = text_extract(path) if tier == "text" else vision_extract(path, mode=tier)
        rec.setdefault("meta", {})["path_taken"] = tier
        if score(rec):
            return rec
        last = rec
    last["meta"]["needs_review"] = True     # exhausted cheap+vision => human
    return last
```

**Excel writer (test path, idempotent):**
```python
import openpyxl, json, hashlib

def dedup_key(r: dict) -> str:
    basis = (r.get("full_name","") + "|" +
             ";".join(sorted(r.get("emails", []))) + "|" +
             ";".join(sorted(r.get("phones", [])))).lower()
    return hashlib.sha1(basis.encode()).hexdigest()

def upsert_row(xlsx_path: str, record: dict):
    wb = openpyxl.load_workbook(xlsx_path)
    ws = wb.active
    headers = [c.value for c in ws[1]]
    key = dedup_key(record)
    key_col = headers.index("dedup_key") + 1
    # update existing row if key matches, else append
    for row in ws.iter_rows(min_row=2):
        if row[key_col-1].value == key:
            target = row[0].row
            break
    else:
        target = ws.max_row + 1
    flat = {
        "full_name": record.get("full_name"),
        "emails": ";".join(record.get("emails", [])),
        "phones": ";".join(record.get("phones", [])),
        "highest_qualification": record.get("derived", {}).get("highest_qualification"),
        "total_experience_years": record.get("derived", {}).get("total_experience_years"),
        "dedup_key": key,
        "source_file": record.get("meta", {}).get("source_file"),
        "overall_confidence": record.get("meta", {}).get("overall_confidence"),
        "raw_json": json.dumps(record, ensure_ascii=False),
    }
    for col_idx, h in enumerate(headers, start=1):
        if h in flat:
            ws.cell(row=target, column=col_idx, value=flat[h])
    wb.save(xlsx_path)
```

---

## Part 8 — The complete extraction surface (the full endpoint vision)

You don't build all of this now. But it's worth seeing the full end-state, because the current architecture — storage connectors in, model-agnostic extraction, a normalized record with provenance, idempotent output — is already the right foundation for *every* layer below. Each higher layer simply consumes the normalized record. You're building Layers 0–1; Layers 2–4 are incremental additions; Layer 5 is a product-and-compliance decision, not just an engineering one.

**Layer 0 — Core parse (building now).** The full structured record per resume (Part 3 schema): identity, contact, all education with years, all work experience with dates and descriptions, qualifications (CA/CMA/CS, ICAI numbers, levels), articleship, skills, projects, derived fields. This is the bedrock everything else sits on.

**Layer 1 — Document breadth (parse more than the resume itself).**
- **Cover letters** — parse and link to the candidate.
- **Supporting documents in the same folder** — degree certificates, mark sheets, experience letters, articleship-completion certificates, professional certifications. Parse and attach; these can be used to *verify* resume claims.
- ⚠️ **Government IDs (PAN, Aadhaar, passport)** — detect but **do not extract or store** unless there is a specific, consented, compliant reason. This is a DPDP/PII landmine; default to flag-and-skip.
- **Photos** — detect and quarantine the image; never infer sensitive attributes from it.

**Layer 2 — Candidate-level consolidation.**
- **Cross-database deduplication** — the same person across multiple files/applications, merged into one golden profile (the dedup key from Part 4 is the seed).
- **Multi-file assembly** — resume + cover letter + certificates → one unified candidate record.
- **Version history** — track re-applications and resume updates over time.

**Layer 3 — Normalization & enrichment.**
- Canonicalize job titles, company names, and institutions against reference data; flag unrecognized "universities."
- Skill-taxonomy mapping plus inferred/adjacent skills.
- Email and phone validation and formatting.
- (Carefully, with ToS/consent) public-profile enrichment from LinkedIn/GitHub/portfolio; company enrichment (industry, size).

**Layer 4 — Search & retrieval (high value, low risk — do this early).**
- **Semantic search over the candidate database** — "find CAs with GST and statutory-audit experience, 3+ years, in Bengaluru" — via embeddings + a vector store. This is arguably the highest-ROI, lowest-risk addition: it turns a parsed pile into an instantly queryable talent pool, which is something HR feels immediately.
- Faceted/structured filters (qualification, experience band, location) and hybrid natural-language + Boolean queries.

**Layer 5 — Intelligence & scoring (high value, high legal risk — proceed deliberately).**
- JD ↔ resume **matching and ranking** against a job description.
- Auto-tagging by role, seniority, and domain; recruiter-facing candidate one-pagers.
- ⚠️ The moment you rank, score, shortlist, or filter candidates, you inherit anti-discrimination and fairness obligations — EEOC-style adverse-impact exposure, NYC Local Law 144-style AI-hiring audit requirements, EU AI Act "high-risk" classification, and India's evolving norms. That means bias testing, explainability, human oversight, and disclosures. **Recommendation: ship semantic search (Layer 4) well before automated scoring (Layer 5); when you do build scoring, treat it as a regulated feature, not just a model call.**

**Layer 6 — Analytics & reporting.**
- Pipeline analytics (applicant volume by role, source, qualification mix, over time); skill-supply dashboards.
- ⚠️ Diversity analytics only with governance — these touch sensitive attributes.

**Layer 7 — Output destinations & workflow (beyond Excel).**
- **Multi-destination writers** via a `Sink` adapter (the mirror of the storage connector): the same normalized record can flow into an ATS (Greenhouse, Lever, Darwinbox, Naukri RMS), an HRIS, a database, or a BI tool. One record, many sinks.
- Shortlist generation, recruiter notifications, candidate-status callbacks.

The shape to keep in mind: **`StorageConnector` (in) → router → model-agnostic `Extractor` → normalized record (+ provenance) → `Sink` (out)**, with search, enrichment, and intelligence as services that read the normalized record. Get those four contracts right now and every layer above becomes additive rather than a rewrite.

---

## Part 9 — The 90-day-test critical path (TL;DR)

1. **Day 0:** Gemini free-tier key + synthetic test corpus + final schema.
2. **Days 1–4:** vision PoC → multi-format router.
3. **Days 5–9:** normalize/validate/confidence → Excel upsert.
4. **Days 10–14:** OneDrive ingestion + batch run on the whole folder.
5. **Throughout:** run the testing harness (Part 5); A/B Gemini vs Qwen on the same ground truth; pick the winner.
6. **Before any real client:** move off the free tier to a no-training tier (Part 1.3) and harden for PII.

The whole test costs **₹0 in model spend** if you stay on the free tier with synthetic data — which is exactly the setup you should use anyway.
