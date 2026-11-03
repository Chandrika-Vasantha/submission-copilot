# Design notes

Chandrika Vasantha · vasanthachandrika7@gmail.com

## 1. The problem, restated

Underwriters spend the first hour of every submission sorting attachments, re-keying data, chasing
missing items and spotting contradictions. Most submissions are not quotable as received. The value of
intake automation is therefore less "extract fields" and more:

1. tell the underwriter, in minutes, **whether the risk is in appetite and what is blocking a quote**, and
2. give them **evidence they can trust**: every number traceable to a page, every inconsistency surfaced,
   every decision reproducible.

That framed three design choices that run through the whole prototype:

* **Deterministic first, AI where it earns its place.** Parsing, validation and rules are deterministic and
  auditable; the LLM handles ambiguity (weak classifications, unreadable scans) and judgment (what matters,
  how to say it).
* **Provenance on everything.** Every extracted value is a `Fact(value, file, page, method, confidence)`.
* **AI advises, rules decide, people confirm.**

## 2. Solution architecture

| Concern | Prototype | Production |
|---|---|---|
| Email ingestion | `.eml` parsed with the stdlib; raw message stored immutably with SHA-256; attachments (and zips inside them) expanded; duplicate attachments detected by hash; upload form wraps loose files in a synthetic email so every path converges | Shared mailbox via Microsoft Graph change notifications (or SES inbound → S3). The receiver only stores the raw message and enqueues an id - no parsing in the hot path. Agent replies thread back to the same submission by Message-ID / subject token |
| Document processing | File type from magic bytes; PDF text layer via pdfplumber; image pages rendered at 300 dpi and OCR'd with Tesseract after orientation detection, deskew, background flattening and (for weak reads) extra passes | Same pipeline, with a managed OCR/layout service (AWS Textract / Azure Document Intelligence) for tables and handwriting; Tesseract kept as fallback. HEIC/TIFF/DOCX/MSG converters |
| AI services | Claude (Anthropic API) for page classification fallback, vision re-read and analysis; deterministic offline engine with the same interface | Claude via Bedrock / Vertex / Anthropic in-region; prompt + model versions pinned; evaluation set from labelled historical submissions |
| Business rules | `rules/knight_trucking.yaml` + check functions; version + hash stored on every decision | Same pattern, rules in Git with UW sign-off (PR review) or a rules UI writing the same YAML; per-program rule sets |
| Data storage | SQLite (submissions, append-only audit) + a folder per submission | Postgres for structured data; object storage with object-lock for originals and page images; search index for submissions |
| User interface | Flask + server-rendered HTML | Same information architecture inside the carrier's workbench / SSO, or a React front end on the same API |
| Orchestration | In-process steps, timed and audited; per-document OCR in a thread pool | Queue + durable workflow (Step Functions / Durable Functions / Temporal): idempotent steps, retries, per-document fan-out, dead-letter queue, SLA timers |

The numbered flow (also on the app's *How it works* page): intake → load/OCR → classify → extract →
reconcile → rules → AI analysis → workbench → human decision (→ agent request → reply re-enters intake).

## 3. AI design

### Classification
* **Per page, not per file.** One attachment often holds several documents (the sample's MVR PDF holds 6
  MVRs, a stale MVR and 2 license photos; the application workbook holds an application, a driver tab and a
  lookup tab).
* **Content signatures first**: weighted patterns per document type ("Violations/Convictions", "Taxable
  Gal", "Run Date"...). Confidence = absolute evidence + margin over the runner-up.
* **File name is a weak prior** (+0.75 points) and is compared to the content result; disagreement is shown
  ("File name suggests loss run; content says no-loss letter"). The stress-test email renames the vehicle list
  to `Drivers.xlsx`, a loss run to `IFTA_2025.pdf` and a license photo to `LossRuns.jpg`; all are classified
  correctly.
* **LLM fallback** for pages below 0.70 confidence (page text + image for scans).
* Spreadsheets are parsed by **header meaning**: any sheet with VIN + year/make is a vehicle table, any sheet
  with DOB + license is a driver table - whatever the file is called.

### Extraction
* Type-specific parsers produce Facts. Examples: loss-run header/totals/claims; MVR violations, license
  class, CDL events; IFTA jurisdiction rows; quote-form check boxes; application label/value pairs.
* **Validators** turn reading errors into visible uncertainty instead of silent errors:
  IFTA rows must sum to the printed total; claim totals must sum to policy totals; VIN length, check digit,
  manufacturer code and model-year code; dates must parse and be plausible.
* **Vision re-read** (Claude engine): pages with OCR confidence < 85 (license photos) and IFTA tables failing
  the sum check are re-read from the image. The re-read is accepted only if it passes the same validator.
  In the stress test the degraded phone photo of Q2 2026 reads 72,136 of 103,773 miles - flagged at 60%
  confidence and raised as a finding rather than trusted.

### Confidence
* Base by method: spreadsheet 0.97, text layer 0.95, OCR 0.85 × (0.6 + 0.4 × tesseract word confidence),
  vision 0.88, derived 0.90.
* Validators adjust (reconciled table 0.97 / unreconciled 0.6).
* Cross-document agreement raises confidence (+0.02 per agreeing source); a conflict cuts the field to 60%
  of its best source until resolved by a person (then 1.0, method "human").
* Findings: offline findings carry calibrated fixed confidences; LLM findings are capped by grounding
  (uncited → max 0.4).
* In production these numbers would be **calibrated** against labelled data (reliability curves per field
  type) and thresholds set from the cost of a miss vs. the cost of a review.

### Conflicting information
* Every field keeps all candidate Facts. Disagreement → a Conflict with all values, their sources, a
  **proposed value from an explicit source-authority policy** (MVR > license copy > driver list for identity;
  VIN decode > loss-run text for make; IFTA > application for territory; newer document > older) and the
  reasoning.
* Severity decides handling: low-impact conflicts (agency phone, 1-day policy offset, outdated license photo
  superseded by the MVR) are **auto-resolved but shown**; material ones (entity, unit count, territory, owner
  as driver, DOB) stay **open** and trigger rule DQ-01 → refer.
* Duplicates are merged and listed (same driver on three documents; stale MVR superseded; license photo
  matched to MVR; claim vehicle matched to schedule by VIN).

### Human review
* The workbench is built around review: decision sign, sections for missing items/conflicts/findings/rules,
  and **every value links to the page image and the text the system read**.
* Underwriters resolve conflicts (choose a value + note) and waive rules (reason required). The decision is
  re-derived immediately, and every action is in the audit trail.
* Corrections are the training signal: in production, resolved conflicts and field corrections feed an
  evaluation set and parser/prompt improvements.

## 4. Rules & workflow

* **Maintained as data.** `rules/knight_trucking.yaml` lists every rule with id, category, the guideline
  sentence it implements, parameters (thresholds, look-backs) and action (`decline`, `refer`, `missing`,
  `info`). Underwriting changes thresholds or actions without a code release; checks are small, unit-tested
  Python functions. Example: switching APP-04 from `refer` to `decline` applies the strict reading of
  "non-listed states" and the sample becomes DECLINE.
* **AI ↔ rules.** Rules consume reconciled facts (which may include vision-read values, already validated).
  The LLM receives the rule results and may explain them, add findings and draft communications, but cannot
  change a result. If AI and rules disagree, the underwriter sees both.
* **Decision logic.** Any `decline` failure → DECLINE (recommendation; underwriter confirms). Any `refer`
  or `missing` failure, or any rule that cannot be evaluated (`unknown`, e.g. SAFER not checked, I-10 not
  verifiable) → REFER. Otherwise PROCEED. Routing by power units: 1-10 Small Fleet, 11+ Fleet.
* **Exceptions.** A failing attachment is recorded and routed to manual review, never sinks the submission;
  a rule that throws returns `unknown` (→ refer), never a silent pass; unparseable/unknown pages are listed;
  processing errors show in the inbox.
* **Auditability.** Raw email + attachment hashes; per-step timings; rules version + file hash; AI engine /
  model id; every human action with user, time, before/after and reason. The result JSON is a complete,
  replayable snapshot of what the underwriter saw.

## 5. Scalability & reliability (production)

* Stateless workers behind a queue; one workflow per submission, OCR fanned out per page. Volume of
  "hundreds of emails a day" is small; peaks (renewal season, Monday mornings) are absorbed by queue depth
  and autoscaling OCR workers.
* Idempotency keyed on Message-ID + attachment SHA-256 (re-sent emails don't create duplicate submissions;
  replies attach to the existing one).
* Cost control: deterministic path first; LLM only for ambiguous pages and the final analysis; prompt
  caching for the guideline; batch API for non-urgent re-processing.
* Resilience: retries with backoff on OCR/LLM; LLM outage → offline engine still routes; DLQ + alerting;
  SLA timer that escalates submissions stuck in a step.
* Quality monitoring: per-field accuracy from underwriter corrections, classifier confusion matrix,
  % of conflicts resolved vs. proposed, rule hit rates, drift alerts when a new agency format appears.

## 6. Security & compliance

* **Sensitive data**: driver PII (DOB, license numbers), MVRs (DPPA / FCRA-governed), FEIN/SSN. Field-level
  classification; mask license numbers and DOB in lists/logs; MVR use limited to underwriting purpose.
* **Access control**: SSO (Entra ID / Okta), role-based (intake ops, underwriter, manager, admin), row-level
  by program/team; break-glass access logged.
* **Encryption**: TLS 1.2+ in transit; KMS-managed keys at rest (object storage, database, backups);
  originals under object-lock (WORM) for the retention period.
* **Audit logging**: append-only audit table streamed to SIEM; includes every view of PII-bearing pages in
  production.
* **AI vendor controls**: zero-retention / no-training terms, in-region processing, data minimisation in
  prompts (only fields needed), prompt/response logging inside the carrier boundary.
* **Regulatory**: state insurance data-security laws (NAIC Model Law #668, NYDFS Part 500 where
  applicable), GLBA safeguards, DPPA/FCRA for MVRs, record-retention schedules, NAIC AI Model Bulletin
  (governance, explainability, human oversight of AI-supported decisions). The design keeps humans
  accountable for declines and documents why every recommendation was made.
* Email-borne threats: attachment scanning, no macro execution, archives depth-limited, files processed in
  isolated workers.

## 7. User experience

* **Operations / intake**: inbox with status (queued → processing → ready → awaiting agent), recommendation
  and team; one click sends the drafted information request to the agent.
* **Underwriter**: opens a submission and sees, top to bottom, the decision sign (REFER → Small Fleet),
  key numbers, the summary with what works and the biggest concerns, then evidence: documents (what each
  file really is), extracted data with confidence and source chips, missing items, conflicts to resolve, AI
  findings with citations, rule outcomes with the guideline text, next actions and the audit trail.
* Colour has one meaning everywhere: green pass, amber refer, red decline.
* Designed to remove re-keying and hunting, not judgment: the underwriter still owns the call.

## 8. Assumptions (made in place of answers)

1. **"Available States" governs domicile/garaging**, not every state driven through, because the target risk
   hauls "across state lines". Travel in non-listed states is referred (APP-04), not auto-declined; one line in
   the YAML switches to the strict reading.
2. **Freshness is measured from the application date** (10/22/2026): MVRs ≤ 90 days, loss runs ≤ 60 days.
3. **"Most recent 4 quarters" of IFTA** means the four most recent returns *due* by the application date;
   Q3 2026 (due 10/31/2026) wasn't due yet, so Q3 2025-Q2 2026 is complete, with a note to request Q3 2026.
4. **Loss-run years** are counted on auto liability terms aligned to the requested effective date:
   expiring 2025-26 + 2024-25, 2023-24, 2022-23. A cargo "no paid losses" letter is not accepted as a loss run.
5. **Continuous coverage** is measured backwards from the effective date (3 years → from 12/15/2023:
   continuous). The 2023 lapse is still flagged because the guideline requires any lapse > 30 days to be
   explained.
6. **Driver CDL experience** uses the earliest evidence of CDL privileges (license photo issue date, MVR CDL
   events, commercial issue date), falling back to the self-reported years.
7. A driver who fails the baseline (the owner's Class C) is a **refer with "exclude or remove"**, not an
   automatic decline: the guideline's decline trigger is "unacceptable driver history"; a non-driving owner
   can be excluded by endorsement.
8. **Cargo** is outside the program snapshot (which lists only AL and APD) → informational; APD requested →
   "not available" note.
9. The application and quote form are both **agent-completed forms of equal weight**; government filings
   (IFTA) and state records (MVR) outweigh them.
10. The unscheduled 2021 Peterbilt in the 2025 claim is **probably the 5th unit** on the prior-insurance
    table; the system proposes this, it does not assume it.
11. SAFER/SMS is **not in the package**; production calls the FMCSA API at intake; the prototype marks it
    unknown → refer.

## 9. Clarifying questions I would ask

**Appetite & rules**
1. Does "Available States" mean domicile/garaging, or every state of operation? If operation, is there a
   mileage threshold?
2. Is cargo (MTC) written in this program or placed elsewhere? Is trailer interchange in scope?
3. For a driver who fails the baseline: exclude-and-proceed, or decline?
4. What counts as a "serious" SAFER violation (OOS rates, BASIC percentiles, specific violations)?
5. How is the $13,000/unit test applied when the agent hasn't stated a target premium?
6. Freshness: from application date, receipt date or effective date?

**Workflow & users**
7. Who triages today, and what is the current turnaround and drop-out rate? What does "good" look like
   (time to first touch, % quotable on first pass)?
8. Should the system email the agent directly for missing items, or always via the underwriter?
9. How should agent replies and corrected documents be merged (new version vs. append)?
10. Where must results land - policy admin / rating system, CRM, a workbench? Which system is the record?

**Data, risk & compliance**
11. Sample of historical submissions with the underwriter's final decisions and corrections, to build the
    evaluation set and calibrate confidence?
12. Volume, peak load, attachment size and format mix (how much handwriting, HEIC, MSG)?
13. Approved AI vendors / regions, data-retention and redaction requirements for MVRs?
14. Is an automated decline ever acceptable, or always a human decision?

## 10. Risks & trade-offs

| Choice | Trade-off |
|---|---|
| Deterministic parsers first | Reproducible, cheap and explainable, but each new form layout needs a parser or falls to the LLM. Mitigation: LLM extraction with the same validators for unknown layouts; parser library grows from corrections |
| LLM never decides | Slower to "auto-quote", but defensible to regulators and underwriters |
| Conservative REFER on unknowns | More manual reviews early on; tune as SAFER/telematics integrations come online |
| Page-level classification | More compute than per-file, but handles bundled PDFs, which are the norm |
| Tesseract in prototype | Free and local; weaker on handwriting and complex tables than managed OCR (hence the validators and vision re-read) |
| Fixed confidence formula | Transparent but uncalibrated; production needs calibration on labelled data |

## 11. What the prototype does not do (yet)

Live mailbox connection; SAFER/FMCSA, telematics and premium/rating integration; handwriting-specific OCR;
multi-user auth; rule-editing UI; reply threading. Each has an obvious slot in the architecture above.
