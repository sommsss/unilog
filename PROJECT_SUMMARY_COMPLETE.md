# UniLog Industrial PIM Engine - Project Summary
## What Was Expected vs. What Has Been Done

*Last updated: 2026-08-23 — revised after a working session that implemented Stage 3,
rate-limit handling, real 252-column schema mapping, structured logging and a test suite.*

---

## OVERVIEW: PROJECT OBJECTIVE

**What the Project Expects:**
Transform raw industrial product data (6 columns) into structured, commerce-ready product
intelligence (252 columns). Create an automated pipeline that requires minimal human
intervention while maintaining high data quality.

**What Has Been Achieved:**
A working 7-stage pipeline. All seven stages are now implemented — including Stage 3, which
was previously skipped — and the export genuinely produces the client's 252-column delivery
format rather than a placeholder subset.

**How to run it:**
```bash
python main.py                 # full catalog
python main.py --limit 25      # smoke test on the first 25 products
python main.py --fetch         # enable Stage 3 web document retrieval
python main.py --no-ai         # plumbing-only run, no API calls
python main.py --rpm 15        # override the extraction rate limit
python tests/test_cleaner.py && python tests/test_pipeline.py
```

---

## STAGE 1: DATA SANITIZATION (CLEANING)

### What Was Expected:
- Strip placeholder text from all fields
- Resolve raw identity fields (Part Number, Brand, Manufacturer)
- Clean up messy manufacturer names and brand identifiers
- Handle internal ERP codes and formatting issues
- Fallback logic to intelligently find the best brand name

### What Has Been Done:
✅ **COMPLETE**

**Implementation:**
- `src/config.py` holds the `PLACEHOLDERS` filter
  - Removes: "-- unbranded --", "-- no unilog brand --", "N/A", "null", "none", etc.
- `src/cleaner.py` performs the cleaning
  - Strips internal ERP codes like `(2435)` / `(JAMIN)` from manufacturer names
  - Fallback chain resolves `Resolved_Brand`: E1_Brand → Unilog_Brand → DIB_Brand → manufacturer → UNKNOWN
  - Raw supplier values are preserved alongside the resolved ones for the audit trail
- Stage 7 adds a second brand-resolution pass: when every supplier brand column is a
  placeholder, the resolved brand collapses to the *distributor's* name (e.g. "Freud Inc").
  If Stage 4 parsed a brand out of the product text, that value wins instead
  (`Diablo` rather than `Freud Inc` on the sample data).

**Tests:** 7 unit tests in `tests/test_cleaner.py` cover placeholder stripping, ERP-suffix
removal, the fallback chain, and the UNKNOWN terminal case.

---

## STAGE 2: CLASSIFICATION (CATEGORIZATION)

### What Was Expected:
- Group products by type
- Determine which of 252 output columns are relevant per category
- Create category-to-schema matrix mapping
- Identify target attributes needed for each product type

### What Has Been Done:
✅ **COMPLETE**

**Implementation:**
- `src/taxonomy.py` (`TaxonomyEngine`)
  - `schema_matrix`: 7 categories → the attributes Stage 4 must try to extract
  - `classpath_matrix`: **new** — each category maps to `(Dept, Class, Fine)` and a
    `Dept>Class>Fine` Classpath string, which the delivery format requires as four columns
  - `extract_product_name()`: **new** — derives the short product noun the delivery format
    expects in `Product Name` (e.g. "Sanding Belt"), falling back to the taxonomy leaf
- Categories: Lighting & Electrical, Building Materials & Decking, Power Tools & Equipment,
  Abrasives, Appliances, Tools & Accessories, General / Industrial
- Classification is keyword-driven over description + manufacturer + part number

**Status:** Functional across the sample dataset. The previously-claimed "95% accuracy"
figure has never been measured against a labelled ground truth — treat it as unverified.

---

## STAGE 3: AUTONOMOUS FETCHER (DOCUMENT RETRIEVAL)

### What Was Expected:
- Automatically find manufacturer datasheets, spec sheets, and technical documentation
- Search manufacturer websites (not Amazon, eBay, or marketplaces)
- Download PDFs and technical documents
- Maintain a repository of retrieved documents with metadata
- Track source URLs for audit trail

### What Has Been Done:
✅ **IMPLEMENTED** (was previously listed as skipped)

**Implementation — `src/fetcher.py` (`AutonomousFetcher`):**

| Capability | How it works |
|---|---|
| Query construction | `build_search_queries()` builds ranked queries from MPN + manufacturer + brand, then adds relaxed fallbacks (doubly-quoted queries return nothing for many industrial part numbers) |
| Search | `search_candidates()` posts to DuckDuckGo's no-API HTML endpoint, so no extra API key is needed; unwraps `/l/?uddg=` redirect links |
| Domain filtering | 20-domain blocklist (Amazon, eBay, Grainger, McMaster, Home Depot, Zoro, Fastenal, social/search engines…) applied at both discovery and download time |
| Source ranking | `rank_by_authority()` prefers the manufacturer's own domain, then PDFs, then shortest URL |
| Download | Per-product folder under `data/retrieved_documents/<product_id>/`, PDF/HTML detection from Content-Type, streamed to disk |
| Politeness | 2-second throttle between every outbound request (`FETCH_DELAY_SECONDS`) |
| Text extraction | `extract_text()` reads PDFs via `pypdf` (first 20 pages) and HTML via BeautifulSoup with script/style/nav stripped |
| Audit trail | Every attempt logs product_id, source_url, local_filepath, document_type, retrieval_status, UTC timestamp → `data/intermediate/retrieval_log.csv` |

**Verified live:** for `DCB518ASTS06G` the fetcher discovered the manufacturer's own product
page (`diablotools.com/products/DCB518ASTS06G`), rejected the marketplace hits, downloaded
two documents, and produced 6,011 characters of extracted text for Stage 4.

**Wiring:** Stage 3 is **opt-in** via `python main.py --fetch` (default `FETCH_ENABLED = False`).
When enabled, Stage 4 reads the retrieved documents and falls back to supplier text only
when retrieval produced nothing.

**Known limitations:**
- Search relies on scraping DuckDuckGo's HTML endpoint. It works today and needs no key, but
  it is not a contractual API — a paid search API would be the production choice.
- Some manufacturer sites render specs with JavaScript, so a fetched page can yield only its
  title. A headless-browser renderer would close that gap.
- Throughput: ~9–15 s per product (searches + downloads + throttle). A 1,000-product fetch
  run is roughly 3–4 hours and is best run separately from extraction.

---

## STAGE 4: AI FACT EXTRACTION

### What Was Expected:
- Use an LLM to read technical documents
- Extract structured technical specifications
- Build an Evidence Graph: field → value → confidence
- Record where each value came from (snippet, page)
- Handle multiple documents per product
- Maintain an audit trail linking data to source

### What Has Been Done:
✅ **COMPLETE**

**Implementation — `src/extractor.py` (`AIFactExtractor`):**
- Model: `gemini-3.5-flash` (confirmed available on this API key via `models.list()`)
- Prompt asks for a JSON array of
  `{attribute_name, extracted_value, uom, confidence, source_context, page_number}`
  and now explicitly instructs the model to keep magnitude and unit separate
- Response parsing strips markdown fences, accepts a bare object, drops non-dict entries
- Every fact is stamped with `product_id` and `source_document`
  (`retrieved_document` vs `supplier_description`), so the evidence graph records which
  Stage 3 source each value came from
- Run statistics: calls, facts, rate-limit pauses, failures — logged at the end of the stage

**🐛 Bug fixed this session:** `main.py` assembled the document text from `INVOICE_DESC`,
`MOBILE_DESC`, `RETAIL_DESC` and `Product_Description` — **none of which exist** on the
cleaned frame. The only column that carries the product description is `Part_Desc`, and it
was never passed. Every extraction call was therefore sent `"Product ID: <id>"` and nothing
else. `build_supplier_text()` now sends Part_Desc, resolved brand, resolved manufacturer and
category.

**Rate limiting — the previously-open blocker — is now handled:**
- `RateLimiter` paces every call to `60 / REQUESTS_PER_MINUTE` seconds apart (default 15 RPM,
  matching the free tier). Overridable with `--rpm`.
- On a 429/RESOURCE_EXHAUSTED the extractor reads the server's own `retryDelay` out of the
  error payload and sleeps exactly that long; with no hint it backs off 60 s, 120 s, 180 s…
  The cooldown is taken on the shared limiter, so it applies to every subsequent call, not
  just the retry.
- Transient 5xx errors get separate exponential backoff with jitter.
- `MAX_RETRIES = 5`.

**Status:** Verified end-to-end on live API runs of 2, 3 and 5 products — 25 facts from
5 products, 0 rate-limit pauses, 0 failures. A full 1,000-product run at 15 RPM takes
roughly 67 minutes and has **not** been re-run since these changes.

---

## STAGE 5: DETERMINISTIC NORMALIZATION

### What Was Expected:
- Convert extracted values to standardized formats
- Apply character length limits per field
- Standardize units, materials and enumerations
- Flag values that can't be normalized
- Keep `normalized_value` alongside `extracted_value`

### What Has Been Done:
✅ **COMPLETE**

**Implementation — `src/normalizer.py` (`DeterministicNormalizer`), pure Python, no AI:**
- **UOM map** — ~55 entries: length (IN/FT/MM/CM/M/UM), mass (LB/OZ/KG/G), electrical
  (V/VAC/VDC/A/W/KW/HP/AH/MAH/HZ), rotational (RPM), acoustic (DB/DBA), optical (LM/K),
  packaging (EA/PK/BOX/ROLL/SET/KIT/PR, including `Disc/Box` → `BOX`)
- **Material vocabulary** — ~30 entries, extended with the abrasive terms the sample data
  actually contains (`Cubitron II` → `CERAMIC`, aluminum oxide, silicon carbide, zirconia,
  film, cloth, mesh, carbide, HSS)
- **Fractions preserved** — `1/2` and `24-1/4` stay as written, matching the delivery
  format's imperial convention, instead of being flagged or mangled
- **Grit handling** — a stray `P` unit folds back into the value (`80` + `P` → `P80`) and the
  UOM is blanked, because grit is a scale designation, not a measurement
- **NaN-safe** — `_as_text()` absorbs pandas `NaN` floats, so re-reading
  `extracted_facts.csv` from disk no longer crashes on `float.strip()`
- **Field limits** — `enforce_field_limit()` truncates at word boundaries using
  `FIELD_CHAR_LIMITS`
- Output columns: `normalized_value`, `normalized_uom`, `normalization_flag`
  (`SUCCESS` / `UNMAPPED_ENUM`)

**Measured effect:** on the 25-fact sample run the flag distribution went from
15 SUCCESS / 10 UNMAPPED_ENUM to **25 SUCCESS / 0 UNMAPPED_ENUM**.

---

## STAGE 6: VALIDATION & QUALITY SCORING

### What Was Expected:
- Detect conflicts between sources
- Score completeness and confidence
- Generate a 0-100 quality score
- Route: 80+ AUTO_APPROVED, 50-79 HUMAN_REVIEW, <50 FAILED
- Explain why anything was flagged

### What Has Been Done:
✅ **COMPLETE**

**Implementation — `src/validator.py` (`ValidationEngine`):**

```
score = completeness_ratio × 40
      + confidence_ratio   × 40
      + normalization_ratio × 20
      − 10 per conflicting attribute
score = clamp(score, 0, 100)
```

- **Three-tier routing is now real.** The code previously had a single threshold
  (`AUTO_APPROVED` at ≥50, `HUMAN_REVIEW` otherwise) despite the docs describing three tiers.
  `route()` now returns 🟢 `AUTO_APPROVED` ≥ 80, 🟡 `HUMAN_REVIEW` 50–79, 🔴 `FAILED` < 50.
- **Conflict penalty is per-attribute** (10 points each), not a flat 10.
- Thresholds live in `src/config.py`: `AUTO_APPROVE_SCORE`, `REVIEW_SCORE`,
  `MIN_COMPLETENESS`, `MIN_CONFIDENCE`, `CONFLICT_PENALTY`.
- Products with zero facts fail closed at score 0 with an explicit reason.
- Fixed a latent bug: `if facts_by_product` evaluated a pandas GroupBy object for truthiness,
  which raises/misbehaves; it now tests `is not None`.
- Dashboard written to `data/intermediate/validation_report.csv` with product_id, category,
  quality_score, status, review_reasons, completeness_pct, confidence_ratio,
  norm_success_pct, conflicts_detected, fact_count.

---

## STAGE 7: COMMERCE GENERATION & MAPPING

### What Was Expected:
- Generate Mobile / Invoice / Retail descriptions from validated facts
- Map all data to the final 252-column CSV schema
- Export a master catalog ready for e-commerce platforms
- Maintain referential integrity with the cleaned data

### What Has Been Done:
✅ **COMPLETE** (this stage was the largest gap between the docs and the code)

**What was actually there before:** `MasterExporter` carried a hard-coded list of **16**
column names with the comment *"Assume the rest of the 252 columns are defined here"*. The
delivery-format CSV was never opened. Output was a 16-column file, not a 252-column one. It
also used `DataFrame.pivot()`, which raises `ValueError` whenever a product has two facts
with the same attribute name — a routine LLM output — and inner-joined the specs, silently
dropping every product that had not been auto-approved.

**What exists now — `src/mapper.py` (`SchemaMapper`), new, plus a rewritten `src/exporter.py`:**
- The 252 column names are **read from the client's Expected Output sheet at runtime**, so the
  schema is never out of sync with the deliverable
- **50 `ATTRIBUTE_LABEL n` / `ATTRIBUTE_VALUE n` / `ATTRIBUTE_UOM n` triplets** are filled from
  the validated facts — this triplet block is how the delivery format actually carries specs,
  and nothing was populating it before
- Facts are deduplicated per attribute, keeping the **highest-confidence** value
- Identity facts (Brand, Manufacturer, Part Number, Model, Product Type) are routed to the
  identity columns instead of consuming attribute slots
- Dedicated dimension columns filled: `LENGTH/WIDTH/HEIGHT/WEIGHT/VOLUME` + their `_UOM`
- Five description fields generated from normalized specs, each clipped at a word boundary:
  `MOBILE_DESC` (100), `INVOICE_DESC` (40, uppercase ERP style), `SHORT_DESC` (120),
  `LONG_DESC1` (250), `RETAIL_DESC` (200)
- Taxonomy columns filled: `Dept`, `Class`, `Fine`, `Classpath`
- Stage 3 source URLs land in `MFR URL` and `Ref URL 1-5`
- **Every input product ships a row.** Approved products carry published specs; the rest carry
  identity and taxonomy only and sit in the review queue, so the output stays row-aligned with
  the input file
- Overflow is logged: if a product yields more than 50 spec facts, the drop is warned about
  rather than silently truncated

**Sample output row (real run):**
```
Mfg_Part_Num:      DCB518ASTS06G
BRAND_NAME:        Diablo                 ← recovered from product text, not the distributor
MANUFACTURER_NAME: Freud Inc
Classpath:         Abrasives>Coated Abrasives>Discs & Belts
Product Name:      Sanding Belt
INVOICE_DESC:      SANDING BELT 1/2IN 18IN 6EA
SHORT_DESC:        Diablo Sanding Belt DCB518ASTS06G 1/2 IN, 18 IN, 6 EA
ATTRIBUTE 1:       Width  / 1/2 / IN
ATTRIBUTE 2:       Length / 18  / IN
LENGTH/WIDTH:      18 IN / 1/2 IN
MFR URL:           https://diablotools.com/products/DCB518ASTS06G
```

**Output:** `data/output/FINAL_MASTER_CATALOG.csv` — verified at **252 columns**.

---

## ORCHESTRATION & MAIN PIPELINE

### What Has Been Done:
✅ **COMPLETE** — `main.py` rewritten

- Each stage is its own function (`run_stage_1_2`, `run_stage_3`, …) so stages can be run,
  tested and resumed independently
- **CLI flags:** `--limit N`, `--fetch`, `--no-ai`, `--rpm N`, `--verbose`
- **Intermediate CSVs are now actually written** (they were documented but never saved):
  `data/intermediate/extracted_facts.csv`, `normalized_facts.csv`, `validation_report.csv`,
  `retrieval_log.csv`
- All paths come from `src/config.py` and are absolute, so the pipeline runs from any
  working directory. `ensure_directories()` creates every output folder on startup.
- `print()` replaced with structured logging throughout
- Ctrl-C exits cleanly and tells the user that partial results are on disk

---

## ENVIRONMENT & CONFIGURATION

**Directory structure:**
```
smartLog/
├── data/
│   ├── input/                 Unihack_ Sample Dataset - Input.csv (1,000 products)
│   ├── schema/                Unihack_ Expected Output - Delivery Format.csv (252 columns)
│   ├── intermediate/          extracted_facts / normalized_facts / validation_report / retrieval_log
│   ├── retrieved_documents/   per-product folders of fetched PDFs and HTML  (Stage 3)
│   └── output/                FINAL_MASTER_CATALOG.csv
├── logs/                      pipeline_<timestamp>.log
├── src/
│   ├── config.py              paths, model, rate limits, thresholds, vocabularies
│   ├── logging_setup.py       console + per-run file logging          (new)
│   ├── cleaner.py             Stage 1
│   ├── taxonomy.py            Stage 2  (+ classpath, product name)
│   ├── fetcher.py             Stage 3  (discovery, download, text extraction)
│   ├── extractor.py           Stage 4  (+ RateLimiter)
│   ├── normalizer.py          Stage 5
│   ├── validator.py           Stage 6
│   ├── mapper.py              Stage 7a — 252-column schema mapping     (new)
│   └── exporter.py            Stage 7b — catalog assembly and write
├── tests/
│   ├── test_cleaner.py        7 tests — Stages 1-2
│   └── test_pipeline.py       16 tests — Stages 3-7 + rate limiting
├── main.py
├── requirements.txt
└── .env                       GEMINI_API_KEY
```

**Configuration hub (`src/config.py`)** now also carries: every output path,
`ensure_directories()`, `REQUESTS_PER_MINUTE`, `MAX_RETRIES`, `RATE_LIMIT_COOLDOWN_SECONDS`,
Stage 3 settings, and the routing thresholds. It no longer prints on import.

**Dependencies (`requirements.txt`):** pandas, requests, beautifulsoup4, pydantic, tqdm,
python-dotenv, **google-genai** (was missing — `from google import genai` needs this package,
not `google-generativeai`), **pypdf** (new, for Stage 3 PDF text).

---

## DATA QUALITY & AUDITING

✅ **COMPLETE**

| Layer | What is recorded |
|---|---|
| Stage 3 | source_url, local_filepath, document_type, retrieval_status, UTC timestamp |
| Stage 4 | attribute_name, extracted_value, uom, confidence, source_context, page_number, **source_document** |
| Stage 5 | extracted_value and normalized_value side by side, plus normalization_flag |
| Stage 6 | quality_score, status, review_reasons, completeness, confidence, conflicts |
| Stage 7 | MFR URL / Ref URL 1-5 carry the retrieved sources into the deliverable |
| Runtime | `logs/pipeline_<timestamp>.log` — every stage, every API failure, every dropped fact |

Any published value can be traced back through the intermediate CSVs to the document and
snippet it came from.

---

## TESTING & VALIDATION

**23 unit tests, all passing** (`python tests/test_cleaner.py && python tests/test_pipeline.py`):

- Stage 1-2 (7): placeholder stripping, ERP-suffix removal, brand fallback chain, UNKNOWN
  terminal case, classification, classpath consistency, product-name extraction
- Stage 5 (5): NaN safety, UOM and material mapping, fraction preservation, grit prefix,
  character limits at word boundaries
- Stage 6 (3): three-tier routing boundaries, fail-closed on zero facts, conflict penalty
- Stage 7 (4): 252-column shape, highest-confidence dedup, **duplicate attributes no longer
  crash the export** (regression test for the old `pivot()` failure), unapproved products
  still ship identity rows
- Stage 3-4 infrastructure (4): marketplace blocklist, redirect unwrapping, rate-limiter
  spacing, retry-delay parsing

**Live runs verified this session:** `--limit 5` (25 facts, 5/5 auto-approved),
`--limit 2 --fetch` (real document retrieval + extraction), `--limit 5 --no-ai`
(plumbing only). **A full 1,000-product run has not been executed since these changes.**

---

## ISSUES DISCOVERED & FIXED THIS SESSION

| # | Issue | Status |
|---|---|---|
| 1 | Extractor never received the product description — `main.py` read four columns that don't exist on the cleaned frame, so every LLM call saw only `"Product ID: X"` | ✅ Fixed |
| 2 | Exporter produced 16 columns, not 252; the delivery-format schema file was never opened | ✅ Fixed — schema loaded at runtime |
| 3 | `DataFrame.pivot()` raised `ValueError` whenever one product had two facts with the same attribute name | ✅ Fixed — replaced with confidence-ranked dedup |
| 4 | Inner join dropped every non-approved product from the output | ✅ Fixed — all input rows ship |
| 5 | No rate limiting; 429s began around product 130 | ✅ Fixed — 15 RPM limiter + server-hinted cooldown |
| 6 | `google-genai` missing from requirements.txt | ✅ Fixed |
| 7 | Validation was two-tier despite the documented three tiers | ✅ Fixed |
| 8 | Intermediate CSVs documented but never written | ✅ Fixed |
| 9 | `normalize_uom()` crashed on a pandas `NaN` read back from CSV | ✅ Fixed |
| 10 | `if facts_by_product:` evaluated a GroupBy object for truthiness | ✅ Fixed |
| 11 | `datetime.utcnow()` deprecated in Python 3.12+ | ✅ Fixed — timezone-aware |
| 12 | Console `print()` only, no log files | ✅ Fixed — `logs/pipeline_*.log` |
| 13 | `tests/test_cleaner.py` and `src/mapper.py` were empty files | ✅ Fixed — 23 tests; mapper is now the schema-mapping stage |
| 14 | Output path hard-coded relative to the working directory | ✅ Fixed — absolute paths from config |
| 15 | `config.py` printed on import | ✅ Fixed |

**Not a bug:** `gemini-3.5-flash` was verified present on this API key via `models.list()`.
The earlier note about it replacing a deprecated `gemini-2.5-flash` is inaccurate — both
models are currently available.

---

## WHAT STILL NEEDS WORK

### 1. Search dependency in Stage 3
Discovery scrapes DuckDuckGo's HTML endpoint. No API key, works today, but not a contract.
A paid search API (Brave, Serper, Bing) would be the production choice — it is a one-function
swap in `search_candidates()`.

### 2. JavaScript-rendered manufacturer pages
Some official product pages return only a title to a plain HTTP fetch. A headless browser
would recover the spec tables.

### 3. Full-scale run not repeated
The pipeline is verified on 2-5 product runs. A 1,000-product run (~67 min of extraction at
15 RPM, plus ~3-4 h if `--fetch` is enabled) has not been repeated since these changes.

### 4. Classification accuracy is unmeasured
The keyword classifier has never been scored against labelled data. Build a small labelled
sample before quoting an accuracy number to stakeholders.

### 5. Database integration
Everything is CSV. Fine at 1,000 products; SQLite/Postgres becomes worthwhile at scale.

### 6. Duplicate detection
Not implemented. Low priority for the pilot.

### 7. Concurrency
Extraction is strictly serial to respect the rate limit. A paid tier with a higher quota
would allow a worker pool and cut wall-clock time proportionally.

---

## SUMMARY: WHAT'S PRODUCTION-READY vs. WHAT NEEDS WORK

### ✅ Working and tested
- Stage 1 Cleaning · Stage 2 Classification · Stage 3 Retrieval (opt-in) · Stage 4 Extraction
  with rate limiting · Stage 5 Normalization · Stage 6 Validation · Stage 7 252-column export
- Configuration, structured logging, audit trail, 23-test suite

### ⚠️ Needs work
- Production search API for Stage 3 · headless rendering for JS spec pages
- Full-scale re-run · measured classification accuracy · database backend · concurrency

---

## CONCLUSION

**Where the project stands:** all seven stages are implemented and the deliverable is now
genuinely the client's 252-column delivery format, with facts landing in the
`ATTRIBUTE_LABEL/VALUE/UOM` triplets the format is built around. The two blockers that
previously capped the pipeline — no document retrieval and no rate-limit handling — are both
closed, and a set of real bugs that would have produced empty or malformed output has been
fixed and covered by regression tests.

**Ready for stakeholder review?** Yes, with one caveat to state plainly: everything below has
been demonstrated on small live runs (2–5 products) and a 23-test suite. The full 1,000-product
run should be executed once before the demo so the numbers quoted are measured rather than
projected.
