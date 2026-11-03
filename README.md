# Commercial Auto Submission Intake

Take-home case study for the AI Solutions Developer role (Commercial Auto).

Chandrika Vasantha
vasanthachandrika7@gmail.com

---

## What this is

Agents send trucking submissions as one email with a pile of attachments: applications, quote forms,
loss runs, IFTA returns, MVRs, driver and vehicle lists. Some are clean PDFs, some are scans or phone
photos, and the file names often don't match what's inside. An underwriter usually spends the first
hour just sorting it out.

This prototype does that first hour. You give it the agent's email and it:

1. stores the email and every attachment (zips get unpacked, exact duplicates are caught by hash)
2. reads each page, using the PDF text where it exists and OCR where the page is only an image
3. works out what each page actually is from its content, not its file name
4. pulls out the facts (insured, drivers, MVRs, vehicles, VINs, claims, IFTA miles by state...)
5. lines the documents up against each other and flags anything that doesn't match
6. runs the Knight trucking guideline as a set of rules and recommends a next step
7. writes an underwriter brief, findings and a draft email back to the agent
8. shows all of it in a small web app where every value links to the page it came from

For the Cobalt Mesa sample the result is **REFER to Small Fleet Underwriting**: 4 scheduled power
units, 17 conflicts between documents (10 still need a person), 22 items to ask the agent for, and 14
of the 32 guideline rules needing review.

Some of the things it finds in the sample package:

- The application says the farthest territory is Texas, but four quarters of IFTA show 514,435 miles
  in 34 jurisdictions. 56% of those miles are in states that aren't on the program's list.
- One IFTA return is filed by COBALT MESA TRANSPORT **LLC**; every other document says **INC**, and
  the FEIN is blank.
- The vehicle schedule has 4 tractors, the prior-insurance table says 5 units, and a 2021 Peterbilt
  that isn't on the schedule shows up in a 2025 claim with a driver who isn't on the driver list either.
- The owner is listed as driver #1 but only holds a Class C (non-commercial) license, while the
  application says the owner doesn't drive.
- "MTC 23-24.pdf" looks like a loss run but is a letter saying "no paid losses", which doesn't rule
  out open claims.
- One VIN is 16 characters; Luis Morales' date of birth differs between the driver list and his MVR;
  the 2022-23 loss run is missing and there's a 308-day gap in coverage that needs explaining.

---

## Running it on a Mac

Tested on macOS with Python 3.12. I'd avoid Python 3.14/3.15 for now because some of the image and
PDF packages don't have builds for it yet.

**1. Install Homebrew** (skip if you have it)

```
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

When it finishes it prints two or three "Next steps" commands to add `brew` to your PATH. Run those,
then check with `brew --version`.

**2. Install Python 3.12 and Tesseract** (Tesseract is the OCR engine)

```
brew install python@3.12 tesseract
```

**3. Unzip the project and set up a virtual environment**

```
cd ~/Downloads/submission-copilot
/opt/homebrew/bin/python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

**4. Start the app**

```
python app.py
```

Then open http://localhost:8000 in a browser. Press Ctrl+C in the terminal to stop it.

Next time you only need:

```
cd ~/Downloads/submission-copilot
source .venv/bin/activate
python app.py
```

### Linux / Windows / Docker

- Linux: `sudo apt install tesseract-ocr`, then steps 3 and 4 with your Python 3.11 or 3.12.
- Windows: install Tesseract from https://github.com/UB-Mannheim/tesseract/wiki (tick "add to PATH"),
  then `py -3.12 -m venv .venv`, `.venv\Scripts\activate`, `pip install -r requirements.txt`, `python app.py`.
- Docker: `docker build -t intake .` then `docker run -p 8000:8000 intake`.

---

## Using it

On the inbox page you have three ways to send it a submission:

- **Cobalt Mesa sample package** - the 15 files from the case study, wrapped in an email from the agent
  (`samples/cobalt_mesa_submission.eml`).
- **Same package, messy version** - the same facts, but with misleading file names (the vehicle list is
  called `Drivers.xlsx`, a loss run is `IFTA_2025.pdf`, a license photo is `LossRuns.jpg`) and one IFTA
  return re-shot as a skewed, noisy phone photo. I made this to show the classifier doesn't depend on
  file names and that bad OCR gets caught instead of trusted.
- **Drop files** - drag an `.eml`, a `.zip` (the original case-study zip works as-is) or any set of
  files onto the drop area and click Process.

After you send one you'll see a live screen with the documents ticking off as they're read. It takes
about 40 seconds, mostly OCR on the IFTA scans. Then open the review.

The review page has a fixed column on the left (the recommendation, key numbers and a section menu)
and these sections:

- **Underwriter brief** - short summary, biggest concerns, what works for the risk
- **Documents received** - what each attachment really is, page by page, with confidence and how it was
  read. Click a page to see the image next to the text the system read.
- **Extracted information** - insured, operations, drivers, power units, loss history, prior insurance,
  IFTA territory. The small blue chips open the exact source page.
- **Missing information** - one list of what to request, each item tied to the rule or conflict behind it
- **Conflicts between documents** - every disagreement with all the values, where each came from, a
  proposed answer and the reasoning. You can resolve them here.
- **AI findings** - the analysis, each with evidence links
- **Guideline rules** - each rule with the guideline sentence it implements; you can waive one with a reason
- **Recommended next actions** - plus a draft email to the agent you can edit and send
- **Audit trail** - everything the system, the rules and the user did

The **Rules** page lists all 32 rules. **How it works** has the architecture diagram.

There's also a command line version: `python cli.py samples/cobalt_mesa_submission.eml`

And tests (about 2 minutes, they run the whole pipeline): `python -m pytest -q tests`

---

## AI: optional Claude, works without it

The app runs fully offline by default, using a deterministic analysis engine I wrote with the
underwriting checks built in. That way it works anywhere and gives the same answer every time.

If you set an Anthropic API key before starting, it uses Claude for three things: classifying pages
the rules aren't sure about, re-reading poor scans from the image (accepted only if the result passes
the same checks, e.g. IFTA rows adding up to the printed total), and writing the brief, findings and
agent email.

```
export ANTHROPIC_API_KEY=your-key
python app.py
```

Optional settings: `AI_ENGINE=offline|claude|auto`, `CLAUDE_MODEL`, `CLAUDE_FAST_MODEL`.

Either way the AI never decides the routing. The rules do, and the underwriter has the final say.
Claude's findings have to cite evidence from the submission; anything it can't back up is shown as
"ungrounded" and its confidence is capped at 40%.

---

## How it's put together

```
app.py                       web app (Flask)
cli.py                       run one email from the terminal
pipeline/
  intake.py                  email parsing, attachment storage, zip handling, duplicate detection
  loaders.py                 file type from the bytes, PDF text, OCR with rotation/deskew/shadow cleanup
  classify.py                page-level classification, file-name mismatch
  extract.py                 one parser per document type; every value keeps file, page, method, confidence
  dispatch.py                sends each page to the right parser based on what it is
  reconcile.py               matching drivers/vehicles across documents, duplicates, conflicts
  rules.py                   the rule checks and the decision/routing logic
  ai.py                      Claude engine and the offline engine (same interface)
  knowledge.py               VIN decoding and check digit, state names, MVR code meanings
  run.py                     runs the steps in order and writes the audit log
  store.py                   SQLite: submissions and audit log
rules/knight_trucking.yaml   the guideline as rules (thresholds and actions live here, not in code)
samples/                     the sample email, its attachments and the messy version
docs/DESIGN.md               the longer write-up (architecture, AI design, security, scale, questions)
docs/Demo_Video_Guide.docx   how I recorded the demo and what I say in it
tests/                       end-to-end tests
```

A few decisions worth calling out:

- **Rules are data.** `rules/knight_trucking.yaml` quotes the guideline line each rule comes from.
  Underwriting can change a threshold or switch a rule from refer to decline without touching code,
  and every decision records which version of the file it ran under.
- **Every value has a source.** Nothing is shown without the file and page it came from, and a
  confidence based on how it was read.
- **Checks catch bad reads.** IFTA rows have to add up to the printed total, claim totals have to
  match the policy totals, VINs are checked for length, check digit and manufacturer code.
- **Conflicts are never silently resolved.** Small ones (a 1-day policy offset, an old license photo
  superseded by the MVR) are settled automatically but still shown. Material ones stay open for a person.

---

## Assumptions I made

The brief leaves some things open on purpose, so here is what I assumed. All of these are easy to change.

- "Available States" means where the business is based and garaged. Driving through other states is
  sent for review, not declined. Changing `action: refer` to `decline` on rule APP-04 applies the
  strict reading, and the sample then comes out as DECLINE.
- MVR (90 days) and loss run (60 days) freshness is measured from the application date, 10/22/2026.
- "Most recent 4 quarters" of IFTA means the four returns that were due by the application date, so
  Q3 2025 to Q2 2026 is complete. Q3 2026 wasn't due until 10/31.
- Loss history is counted on auto liability terms. The cargo "no paid losses" letter doesn't count as a loss run.
- A driver who doesn't meet the baseline (the owner with a Class C) is a referral, since a non-driving
  owner can be excluded by endorsement. The guideline's decline trigger is driving history.
- Cargo isn't in the program snapshot and physical damage isn't available, so those requests are
  flagged as information for the agent.
- Government and state records (IFTA, MVR) outweigh what the agent filled in on the forms.
- The FMCSA SAFER check isn't in the package, so it's marked "can't verify" and sent for review.

The questions I'd ask the business, and the longer reasoning, are in `docs/DESIGN.md`.

---

## What's not in the prototype

No live mailbox connection (it takes `.eml` files or uploads), no FMCSA/SAFER or telematics lookups,
no rating, no login. In production I'd put the mail intake behind Microsoft Graph or SES, run the steps
on a queue, use a managed OCR service for handwriting and complex tables, and store data in Postgres
plus object storage. That's covered in `docs/DESIGN.md`.

All companies, people and records in the sample data are fictitious.
