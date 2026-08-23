# PROJECT STATUS - QUICK REFERENCE

*Updated 2026-08-23. Stage 3 implemented, rate limiting closed, export now genuinely
252 columns. See PROJECT_SUMMARY_COMPLETE.md for detail.*

## 7-STAGE PIPELINE COMPLETION STATUS

```
STAGE 1: DATA SANITIZATION
┌─────────────────────────────────────────────┐
│ Status: ✅ COMPLETE                          │
├─────────────────────────────────────────────┤
│ • Placeholder filtering                     │
│ • ERP code stripping — "Freud Inc (2435)"   │
│ • Brand fallback chain → UNKNOWN            │
│ • Second-pass brand recovery in Stage 7     │
│                                             │
│ Tests: 7 passing                            │
└─────────────────────────────────────────────┘

STAGE 2: CLASSIFICATION
┌─────────────────────────────────────────────┐
│ Status: ✅ COMPLETE                          │
├─────────────────────────────────────────────┤
│ • 7 categories, keyword-driven              │
│ • Target attributes per category            │
│ • NEW: Dept / Class / Fine / Classpath      │
│ • NEW: Product Name noun extraction         │
│                                             │
│ Accuracy: UNMEASURED (no labelled set yet)  │
└─────────────────────────────────────────────┘

STAGE 3: DOCUMENT FETCHER
┌─────────────────────────────────────────────┐
│ Status: ✅ IMPLEMENTED (opt-in: --fetch)     │
├─────────────────────────────────────────────┤
│ • Ranked query building + relaxed fallbacks │
│ • Web search, no API key required           │
│ • 20-domain marketplace blocklist           │
│ • Manufacturer-domain + PDF ranking         │
│ • Download to data/retrieved_documents/     │
│ • PDF (pypdf) + HTML text extraction        │
│ • 2 s politeness throttle                   │
│ • Full retrieval_log.csv audit trail        │
│                                             │
│ Verified: found + fetched the real Diablo   │
│ product page for DCB518ASTS06G              │
│ Speed: ~9-15 s/product (~3-4 h for 1,000)   │
└─────────────────────────────────────────────┘

STAGE 4: AI FACT EXTRACTION
┌─────────────────────────────────────────────┐
│ Status: ✅ COMPLETE                          │
├─────────────────────────────────────────────┤
│ • Gemini 3.5-flash, JSON fact array         │
│ • Confidence + source_context + page        │
│ • source_document: retrieved vs supplier    │
│ • RATE LIMITER: 15 RPM, configurable        │
│ • 429 → reads server retryDelay, sleeps it  │
│ • 5xx → exponential backoff with jitter     │
│ • Per-run stats: calls/facts/pauses/fails   │
│                                             │
│ 🐛 FIXED: the extractor was being sent only │
│    "Product ID: X" — Part_Desc was never    │
│    passed. Descriptions now reach the LLM.  │
└─────────────────────────────────────────────┘

STAGE 5: NORMALIZATION
┌─────────────────────────────────────────────┐
│ Status: ✅ COMPLETE                          │
├─────────────────────────────────────────────┤
│ • ~55 UOM conversions                       │
│ • ~30 material vocabulary entries           │
│ • Imperial fractions preserved (1/2, 24-1/4)│
│ • Grit scale handling (80 + P → P80)        │
│ • NaN-safe when reloaded from CSV           │
│ • Word-boundary character limits            │
│                                             │
│ Sample run: 15/25 SUCCESS → 25/25 SUCCESS   │
└─────────────────────────────────────────────┘

STAGE 6: VALIDATION & QUALITY SCORING
┌─────────────────────────────────────────────┐
│ Status: ✅ COMPLETE                          │
├─────────────────────────────────────────────┤
│ • Conflict detection                        │
│ • Score = 40 completeness + 40 confidence   │
│           + 20 normalization − 10/conflict  │
│ • THREE-TIER routing now real:              │
│     🟢 ≥80 AUTO_APPROVED                    │
│     🟡 50-79 HUMAN_REVIEW                   │
│     🔴 <50 FAILED                           │
│ • Reasons recorded per product              │
│                                             │
│ Tests: 3 passing                            │
└─────────────────────────────────────────────┘

STAGE 7: EXPORT & MAPPING
┌─────────────────────────────────────────────┐
│ Status: ✅ COMPLETE                          │
├─────────────────────────────────────────────┤
│ • 252 columns READ FROM the delivery sheet  │
│ • 50 ATTRIBUTE_LABEL/VALUE/UOM triplets     │
│ • Dimension columns + _UOM                  │
│ • 5 description fields, char-limited        │
│ • Stage 3 URLs → MFR URL / Ref URL 1-5      │
│ • Every input product ships a row           │
│                                             │
│ 🐛 WAS: 16 hard-coded columns, schema file  │
│    never opened, pivot() crashed on repeat  │
│    attributes, non-approved rows dropped.   │
└─────────────────────────────────────────────┘
```

---

## KEY METRICS

| Component | Status | Verified on | Notes |
|-----------|--------|-------------|-------|
| **Data Cleaning** | ✅ | 1,000 rows | 7 unit tests |
| **Classification** | ✅ | 1,000 rows | accuracy unmeasured |
| **Document Retrieval** | ✅ | live, 2 products | opt-in `--fetch` |
| **AI Extraction** | ✅ | live, 5 products | 0 rate-limit pauses |
| **Normalization** | ✅ | 25 facts | 100% SUCCESS flags |
| **Validation** | ✅ | 1,000 rows | 3-tier routing |
| **Export** | ✅ | 1,000 rows | 252 columns confirmed |
| **Rate Limiting** | ✅ | unit + live | 15 RPM + 429 cooldown |
| **Audit Trail** | ✅ | 4 intermediate CSVs | + per-run log file |
| **Test Suite** | ✅ | 23 tests | all passing |

---

## WHAT WORKS RIGHT NOW

```
✅ Load 1,000 raw products
   ↓
✅ Clean messy data (placeholders, ERP codes, brand fallback)
   ↓
✅ Classify + assign Dept/Class/Fine/Classpath
   ↓
✅ Fetch manufacturer documents            (--fetch)
   ↓
✅ Extract specs with AI, rate-limited      (evidence graph)
   ↓
✅ Standardize units, materials, fractions
   ↓
✅ Score quality → 🟢 / 🟡 / 🔴
   ↓
✅ Generate 5 description fields
   ↓
✅ Export 252-column CSV with attribute triplets
```

**Commands**
```bash
python main.py                 # full catalog
python main.py --limit 25      # smoke test
python main.py --fetch         # with document retrieval
python main.py --no-ai         # plumbing only, no API calls
python main.py --rpm 15        # override rate limit
python tests/test_cleaner.py && python tests/test_pipeline.py
```

**Execution time (1,000 products):** ~67 min extraction at 15 RPM; add ~3-4 h if `--fetch`
is enabled. Not yet re-run at full scale since these changes.

---

## WHAT DOESN'T WORK YET

```
⚠️ Stage 3 search scrapes DuckDuckGo's HTML endpoint
   (no API key needed and working, but swap in a paid
    search API for production — one function to change)

⚠️ JS-rendered manufacturer pages return only a title
   (needs a headless browser to read spec tables)

⚠️ Classification accuracy has never been measured
   (no labelled ground-truth sample exists)

⚠️ Full 1,000-product run not repeated since the rewrite

❌ Database backend — everything is still CSV
❌ Duplicate detection — not implemented
❌ Concurrency — extraction is serial by design (rate limit)
```

---

## RATE LIMITING — SOLVED

**The old problem:**
```
Free tier limit: 15 requests/minute
Old request rate: as fast as pandas could iterate
Result: 429 RESOURCE_EXHAUSTED around product 130
```

**What the code does now (`src/extractor.py`):**
```python
RateLimiter(REQUESTS_PER_MINUTE)   # 60/15 = 4.0 s between calls

# On 429: read the server's own hint out of the error payload
cooldown = _parse_retry_delay(error) or 60 * (attempt + 1)
self.limiter.pause(cooldown)       # shared pause — blocks every later call too

# On 503/500/504: exponential backoff with jitter
```
Tunable with `--rpm` or `REQUESTS_PER_MINUTE` in `src/config.py`.

---

## COMPLETENESS BREAKDOWN

```
Stage 1 (Cleaning)          ████████████████████████ complete
Stage 2 (Classification)    ███████████████████████░ complete, accuracy unmeasured
Stage 3 (Web Fetcher)       ████████████████████░░░░ working, search API is the upgrade
Stage 4 (AI Extraction)     ████████████████████████ complete, rate-limited
Stage 5 (Normalization)     ████████████████████████ complete
Stage 6 (Validation)        ████████████████████████ complete
Stage 7 (Export)            ████████████████████████ complete, 252 columns

Remaining work: production search API, headless rendering, full-scale run,
                accuracy measurement, database backend
```

---

## FOR YOUR PRESENTATION/APPROVAL

### What you can show (working now)
- End-to-end pipeline, all 7 stages, run live on demand with `--limit`
- Genuine 252-column delivery-format output with populated attribute triplets
- Real document retrieval: found the manufacturer's own page for a sample part
- Traffic-light quality routing with per-product reasons
- Full audit trail: source URL → snippet → confidence → normalized value → published column
- 23 passing tests, including regression tests for the bugs fixed

### What to state plainly
- Search discovery uses a scraped endpoint today; production wants a paid search API
- Some manufacturer pages need a headless browser to yield specs
- Numbers are from small live runs — do the full 1,000-product run before the demo
- Classification accuracy is not yet measured; don't quote a percentage

### Honest pitch
"All seven stages are implemented, including document retrieval and rate-limit handling.
The output is the client's real 252-column delivery format with facts mapped into the
attribute triplets. Small live runs and a 23-test suite verify it end to end; the full
1,000-product run and a production search API are the next steps."

---

## FILES

```
src/config.py            paths, model, rate limits, thresholds, vocabularies
src/logging_setup.py     console + per-run file logging                    NEW
src/cleaner.py           Stage 1
src/taxonomy.py          Stage 2  (+ classpath, product name)
src/fetcher.py           Stage 3  (discovery, download, text extraction)   IMPLEMENTED
src/extractor.py         Stage 4  (+ RateLimiter)
src/normalizer.py        Stage 5
src/validator.py         Stage 6  (3-tier routing)
src/mapper.py            Stage 7a — 252-column schema mapping              NEW
src/exporter.py          Stage 7b — catalog assembly and write
main.py                  orchestrator with CLI flags
tests/test_cleaner.py    7 tests
tests/test_pipeline.py   16 tests                                          NEW
requirements.txt         + google-genai, + pypdf
.env                     GEMINI_API_KEY
```

**Outputs**
```
data/intermediate/extracted_facts.csv      evidence graph
data/intermediate/normalized_facts.csv     before/after every value
data/intermediate/validation_report.csv    quality dashboard
data/intermediate/retrieval_log.csv        Stage 3 source audit
data/retrieved_documents/<part>/           fetched PDFs and HTML
data/output/FINAL_MASTER_CATALOG.csv       252 columns, one row per input product
logs/pipeline_<timestamp>.log              structured run log
```

---

## NEXT IMMEDIATE ACTIONS

1. Run the full catalog once end-to-end: `python main.py` (~67 min) and record the real
   🟢/🟡/🔴 split for the presentation
2. Decide on a production search API for Stage 3 and swap `search_candidates()`
3. Label ~100 products by hand to measure classification accuracy
4. Optional: `python main.py --fetch --limit 50` overnight to gauge retrieval hit rate at scale
