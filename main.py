"""UniLog Industrial PIM Engine — 7-stage enrichment pipeline orchestrator.

Usage:
    python main.py                      # full run over the input catalog
    python main.py --limit 25           # smoke test on the first 25 products
    python main.py --fetch              # enable Stage 3 web document retrieval
    python main.py --no-ai              # plumbing-only run, skips Stage 4 API calls
    python main.py --rpm 15             # override the extraction rate limit

The same entry point backs the web UI: `run_pipeline()` accepts an input path, a
per-job work directory and a progress callback so a browser can drive a run and
watch it advance. See server.py.
"""

import argparse
import logging
import os
import sys
import time
from typing import Any, Callable, Dict, List, Optional

import pandas as pd
from tqdm import tqdm

from src.cleaner import clean_input_row
from src.config import (
    EXTRACTED_FACTS_PATH,
    FETCH_ENABLED,
    FINAL_CATALOG_PATH,
    INPUT_CSV_PATH,
    NORMALIZED_FACTS_PATH,
    REQUESTS_PER_MINUTE,
    RETRIEVAL_LOG_PATH,
    VALIDATION_REPORT_PATH,
    ensure_directories,
)
from src.exporter import MasterExporter
from src.logging_setup import get_logger, setup_logging
from src.normalizer import DeterministicNormalizer
from src.taxonomy import TaxonomyEngine
from src.validator import ValidationEngine

log = get_logger("pipeline")

FACT_COLUMNS = [
    'product_id', 'attribute_name', 'extracted_value', 'normalized_value',
    'uom', 'normalized_uom', 'normalization_flag', 'confidence',
    'source_context', 'page_number', 'source_document',
]

EXPECTED_INPUT_COLUMNS = [
    'Mfg_Part_Num', 'Part_Desc', 'E1_Brand', 'Unilog_Brand', 'DIB_Brand', 'Part_Manuf',
]

# A progress reporter: stage number, stage name, message, items done, items total.
Progress = Callable[[int, str, str, int, int], None]


def _noop(stage: int, name: str, message: str, done: int = 0, total: int = 0) -> None:
    """Default progress sink for CLI runs, where tqdm already shows the bar."""


class Paths:
    """Where one run reads and writes. Defaults to the repository layout; the web
    server hands each job its own directory so concurrent runs cannot collide."""

    def __init__(self, input_path: Optional[str] = None, work_dir: Optional[str] = None,
                 output_path: Optional[str] = None):
        self.input = input_path or INPUT_CSV_PATH
        if work_dir:
            os.makedirs(work_dir, exist_ok=True)
            self.extracted = os.path.join(work_dir, "extracted_facts.csv")
            self.normalized = os.path.join(work_dir, "normalized_facts.csv")
            self.validation = os.path.join(work_dir, "validation_report.csv")
            self.retrieval = os.path.join(work_dir, "retrieval_log.csv")
            self.output = output_path or os.path.join(work_dir, "FINAL_MASTER_CATALOG.csv")
        else:
            self.extracted = EXTRACTED_FACTS_PATH
            self.normalized = NORMALIZED_FACTS_PATH
            self.validation = VALIDATION_REPORT_PATH
            self.retrieval = RETRIEVAL_LOG_PATH
            self.output = output_path or FINAL_CATALOG_PATH


def validate_input_columns(df: pd.DataFrame) -> List[str]:
    """Return the expected columns this file is missing, if any."""
    present = {c.strip().lower() for c in df.columns}
    return [c for c in EXPECTED_INPUT_COLUMNS if c.lower() not in present]


def build_supplier_text(record: Dict[str, Any]) -> str:
    """Assemble every scrap of supplier-side text we hold for a product."""
    product_id = str(record.get('Mfg_Part_Num') or '')
    fields = [
        record.get('Part_Desc'),
        record.get('Resolved_Brand'),
        record.get('Resolved_MFR'),
        record.get('Assigned_Category'),
    ]
    lines = [
        str(value).strip()
        for value in fields
        if value is not None and str(value).strip().lower() not in ('', 'nan', 'none', 'unknown')
    ]
    return f"Product ID: {product_id}\n" + "\n".join(lines)


def run_stage_1_2(paths: Paths, limit: int = 0, progress: Progress = _noop) -> pd.DataFrame:
    progress(1, "Cleaning & classification", "Reading input catalog", 0, 0)
    log.info("🚀 Stage 1 & 2: Cleaning & Classification...")

    input_df = pd.read_csv(paths.input)
    missing = validate_input_columns(input_df)
    if missing:
        raise ValueError(
            "Input file is missing required column(s): %s. "
            "uniLog reads a supplier export with %s."
            % (", ".join(missing), ", ".join(EXPECTED_INPUT_COLUMNS))
        )

    if limit:
        input_df = input_df.head(limit)

    clean_df = pd.DataFrame([clean_input_row(row.to_dict()) for _, row in input_df.iterrows()])
    classified_df = TaxonomyEngine().process_catalog(clean_df)

    log.info("✅ Cleaned & classified %d products", len(classified_df))
    progress(2, "Cleaning & classification", "Classified %d products" % len(classified_df),
             len(classified_df), len(classified_df))
    return classified_df


def run_stage_3(paths: Paths, classified_df: pd.DataFrame, progress: Progress = _noop) -> pd.DataFrame:
    """Retrieve manufacturer documentation for each product (opt-in, network-bound)."""
    from src.fetcher import AutonomousFetcher

    log.info("🚀 Stage 3: Autonomous Document Retrieval...")
    fetcher = AutonomousFetcher()
    entries: List[Dict[str, Any]] = []
    total = len(classified_df)

    for i, (_, row) in enumerate(tqdm(classified_df.iterrows(), total=total, desc="Fetching Documents"), 1):
        record = row.to_dict()
        product_id = str(record.get('Mfg_Part_Num') or '')
        if not product_id:
            continue
        entries.extend(fetcher.fetch_product_sources(product_id, record))
        progress(3, "Document retrieval", "Searching manufacturer sources", i, total)

    log_df = pd.DataFrame(entries)
    if not log_df.empty:
        log_df.to_csv(paths.retrieval, index=False)
        succeeded = int((log_df['retrieval_status'] == 'success').sum())
        log.info("✅ Retrieved %d/%d documents → %s", succeeded, len(log_df), paths.retrieval)
    else:
        log.warning("⚠️  Stage 3 retrieved no documents")
    return log_df


def run_stage_4(
    paths: Paths,
    classified_df: pd.DataFrame,
    retrieval_log_df: pd.DataFrame,
    rpm: int,
    use_ai: bool,
    progress: Progress = _noop,
) -> pd.DataFrame:
    log.info("🚀 Stage 4: AI Fact Extraction...")
    if not use_ai:
        log.warning("⚠️  --no-ai set: skipping extraction, downstream stages run on zero facts")
        progress(4, "Fact extraction", "Skipped (--no-ai)", 0, 0)
        return pd.DataFrame(columns=FACT_COLUMNS)

    from src.extractor import AIFactExtractor
    from src.fetcher import AutonomousFetcher

    extractor = AIFactExtractor(requests_per_minute=rpm)
    doc_helper = AutonomousFetcher() if not retrieval_log_df.empty else None
    logs_by_product: Dict[str, List[Dict[str, Any]]] = {}
    if not retrieval_log_df.empty:
        logs_by_product = {
            str(pid): chunk.to_dict('records')
            for pid, chunk in retrieval_log_df.groupby('product_id')
        }

    evidence: List[Dict[str, Any]] = []
    total = len(classified_df)
    started = time.monotonic()

    for i, (_, row) in enumerate(tqdm(classified_df.iterrows(), total=total, desc="Extracting Facts"), 1):
        record = row.to_dict()
        product_id = str(record.get('Mfg_Part_Num') or '')
        target_attrs = record.get('Target_Attributes') or []

        # Prefer retrieved manufacturer documents; fall back to supplier text.
        document_text, source = "", "supplier_description"
        if doc_helper and product_id in logs_by_product:
            document_text = doc_helper.build_document_text(logs_by_product[product_id])
            if document_text:
                source = "retrieved_document"
        if not document_text:
            document_text = build_supplier_text(record)

        facts = extractor.process_document(product_id, document_text, target_attrs, source)
        evidence.extend(facts)
        progress(4, "Fact extraction", "%s — %d facts so far" % (product_id, len(evidence)), i, total)

    elapsed = time.monotonic() - started
    log.info(
        "✅ Extracted %d facts from %d products in %.1f min (%d API calls, %d rate-limit pauses, %d failures)",
        len(evidence), total, elapsed / 60,
        extractor.stats['calls'], extractor.stats['rate_limited'], extractor.stats['failures'],
    )

    facts_df = pd.DataFrame(evidence) if evidence else pd.DataFrame(columns=FACT_COLUMNS)
    facts_df.to_csv(paths.extracted, index=False)
    log.info("   Evidence graph saved → %s", paths.extracted)
    return facts_df


def run_stage_5(paths: Paths, facts_df: pd.DataFrame, progress: Progress = _noop) -> pd.DataFrame:
    log.info("🚀 Stage 5: Deterministic Normalization...")
    progress(5, "Normalization", "Standardizing units and vocabulary", 0, 0)

    if facts_df.empty:
        log.warning("⚠️  No facts to normalize")
        normalized_df = pd.DataFrame(columns=FACT_COLUMNS)
    else:
        normalizer = DeterministicNormalizer()
        normalized = [
            normalizer.normalize_fact(fact)
            for fact in tqdm(facts_df.to_dict('records'), desc="Normalizing Facts")
        ]
        normalized_df = pd.DataFrame(normalized)
        flags = normalized_df['normalization_flag'].value_counts().to_dict()
        log.info("✅ Normalized %d facts %s", len(normalized_df), flags)

    normalized_df.to_csv(paths.normalized, index=False)
    log.info("   Saved → %s", paths.normalized)
    progress(5, "Normalization", "Normalized %d facts" % len(normalized_df),
             len(normalized_df), len(normalized_df))
    return normalized_df


def run_stage_6(paths: Paths, classified_df: pd.DataFrame, normalized_df: pd.DataFrame,
                progress: Progress = _noop) -> pd.DataFrame:
    log.info("🚀 Stage 6: Validation & Quality Scoring...")
    progress(6, "Validation & scoring", "Scoring products", 0, 0)

    dashboard = ValidationEngine().process_catalog_validation(classified_df, normalized_df)
    dashboard.to_csv(paths.validation, index=False)

    counts = dashboard['status'].value_counts().to_dict() if not dashboard.empty else {}
    log.info(
        "✅ 🟢 %d AUTO_APPROVED  |  🟡 %d HUMAN_REVIEW  |  🔴 %d FAILED",
        counts.get('AUTO_APPROVED', 0), counts.get('HUMAN_REVIEW', 0), counts.get('FAILED', 0),
    )
    log.info("   Quality dashboard saved → %s", paths.validation)
    progress(6, "Validation & scoring", "Scored %d products" % len(dashboard),
             len(dashboard), len(dashboard))
    return dashboard


def run_stage_7(
    paths: Paths,
    classified_df: pd.DataFrame,
    dashboard: pd.DataFrame,
    normalized_df: pd.DataFrame,
    retrieval_log_df: pd.DataFrame,
    progress: Progress = _noop,
) -> pd.DataFrame:
    log.info("🚀 Stage 7: Final Master Export & Schema Mapping...")
    progress(7, "Schema mapping & export", "Mapping into the 252-column format", 0, 0)

    approved = (
        dashboard[dashboard['status'] == 'AUTO_APPROVED']['product_id'].tolist()
        if not dashboard.empty else []
    )

    exporter = MasterExporter()
    catalog_df = exporter.export_master_catalog(
        clean_df=classified_df,
        classified_df=classified_df,
        approved_products=approved,
        normalized_facts_df=normalized_df,
        retrieval_log_df=retrieval_log_df,
    )
    exporter.write(catalog_df, paths.output)
    progress(7, "Schema mapping & export", "Wrote %d rows × %d columns"
             % (len(catalog_df), len(catalog_df.columns)), len(catalog_df), len(catalog_df))
    return catalog_df


def run_pipeline(
    limit: int = 0,
    fetch: bool = FETCH_ENABLED,
    use_ai: bool = True,
    rpm: int = REQUESTS_PER_MINUTE,
    input_path: Optional[str] = None,
    work_dir: Optional[str] = None,
    output_path: Optional[str] = None,
    progress: Progress = _noop,
) -> Dict[str, Any]:
    """Run all seven stages. Returns a summary dict describing the finished run."""
    ensure_directories()
    log_path = setup_logging()
    paths = Paths(input_path, work_dir, output_path)

    log.info("=" * 70)
    log.info("UniLog Industrial PIM Engine — pipeline start (log: %s)", log_path)
    log.info("=" * 70)

    classified_df = run_stage_1_2(paths, limit, progress)

    if fetch:
        retrieval_log_df = run_stage_3(paths, classified_df, progress)
    else:
        retrieval_log_df = pd.DataFrame()
        log.info("⏭️  Stage 3 skipped (pass --fetch to retrieve manufacturer documents)")

    facts_df = run_stage_4(paths, classified_df, retrieval_log_df, rpm, use_ai, progress)
    normalized_df = run_stage_5(paths, facts_df, progress)
    dashboard = run_stage_6(paths, classified_df, normalized_df, progress)
    catalog_df = run_stage_7(paths, classified_df, dashboard, normalized_df, retrieval_log_df, progress)

    log.info("🎉 Pipeline complete — %d rows × %d columns → %s",
             len(catalog_df), len(catalog_df.columns), paths.output)

    counts = dashboard['status'].value_counts().to_dict() if not dashboard.empty else {}
    return {
        "products": int(len(classified_df)),
        "facts": int(len(facts_df)),
        "rows": int(len(catalog_df)),
        "columns": int(len(catalog_df.columns)),
        "auto_approved": int(counts.get('AUTO_APPROVED', 0)),
        "human_review": int(counts.get('HUMAN_REVIEW', 0)),
        "failed": int(counts.get('FAILED', 0)),
        "output_path": paths.output,
        "validation_path": paths.validation,
        "log_path": log_path,
    }


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="UniLog Industrial PIM Engine")
    parser.add_argument('--limit', type=int, default=0, help="process only the first N products")
    parser.add_argument('--fetch', action='store_true', help="enable Stage 3 document retrieval")
    parser.add_argument('--no-ai', dest='use_ai', action='store_false',
                        help="skip Stage 4 API calls (plumbing test only)")
    parser.add_argument('--rpm', type=int, default=REQUESTS_PER_MINUTE,
                        help=f"extraction rate limit in requests/minute (default {REQUESTS_PER_MINUTE})")
    parser.add_argument('--input', dest='input_path', default=None, help="input CSV path")
    parser.add_argument('--verbose', action='store_true', help="print DEBUG output to the console")
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = parse_args()
    setup_logging(logging.DEBUG if args.verbose else logging.INFO)
    try:
        run_pipeline(limit=args.limit, fetch=args.fetch, use_ai=args.use_ai,
                     rpm=args.rpm, input_path=args.input_path)
    except KeyboardInterrupt:
        log.warning("Interrupted by user — partial results remain in data/intermediate/")
        sys.exit(130)
