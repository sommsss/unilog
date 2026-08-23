"""UniLog Industrial PIM Engine — 7-stage enrichment pipeline orchestrator.

Usage:
    python main.py                      # full run over the input catalog
    python main.py --limit 25           # smoke test on the first 25 products
    python main.py --fetch              # enable Stage 3 web document retrieval
    python main.py --no-ai              # plumbing-only run, skips Stage 4 API calls
    python main.py --rpm 15             # override the extraction rate limit
"""

import argparse
import logging
import sys
import time
from typing import Any, Dict, List

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


def run_stage_1_2(limit: int = 0) -> pd.DataFrame:
    log.info("🚀 Stage 1 & 2: Cleaning & Classification...")
    input_df = pd.read_csv(INPUT_CSV_PATH)
    if limit:
        input_df = input_df.head(limit)

    clean_df = pd.DataFrame([clean_input_row(row.to_dict()) for _, row in input_df.iterrows()])
    classified_df = TaxonomyEngine().process_catalog(clean_df)
    log.info("✅ Cleaned & classified %d products", len(classified_df))
    return classified_df


def run_stage_3(classified_df: pd.DataFrame) -> pd.DataFrame:
    """Retrieve manufacturer documentation for each product (opt-in, network-bound)."""
    from src.fetcher import AutonomousFetcher

    log.info("🚀 Stage 3: Autonomous Document Retrieval...")
    fetcher = AutonomousFetcher()
    entries: List[Dict[str, Any]] = []

    for _, row in tqdm(classified_df.iterrows(), total=len(classified_df), desc="Fetching Documents"):
        record = row.to_dict()
        product_id = str(record.get('Mfg_Part_Num') or '')
        if not product_id:
            continue
        entries.extend(fetcher.fetch_product_sources(product_id, record))

    log_df = pd.DataFrame(entries)
    if not log_df.empty:
        log_df.to_csv(RETRIEVAL_LOG_PATH, index=False)
        succeeded = int((log_df['retrieval_status'] == 'success').sum())
        log.info("✅ Retrieved %d/%d documents → %s", succeeded, len(log_df), RETRIEVAL_LOG_PATH)
    else:
        log.warning("⚠️  Stage 3 retrieved no documents")
    return log_df


def run_stage_4(
    classified_df: pd.DataFrame,
    retrieval_log_df: pd.DataFrame,
    rpm: int,
    use_ai: bool,
) -> pd.DataFrame:
    log.info("🚀 Stage 4: AI Fact Extraction...")
    if not use_ai:
        log.warning("⚠️  --no-ai set: skipping extraction, downstream stages run on zero facts")
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
    started = time.monotonic()

    for _, row in tqdm(classified_df.iterrows(), total=len(classified_df), desc="Extracting Facts"):
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

    elapsed = time.monotonic() - started
    log.info(
        "✅ Extracted %d facts from %d products in %.1f min (%d API calls, %d rate-limit pauses, %d failures)",
        len(evidence), len(classified_df), elapsed / 60,
        extractor.stats['calls'], extractor.stats['rate_limited'], extractor.stats['failures'],
    )

    facts_df = pd.DataFrame(evidence) if evidence else pd.DataFrame(columns=FACT_COLUMNS)
    facts_df.to_csv(EXTRACTED_FACTS_PATH, index=False)
    log.info("   Evidence graph saved → %s", EXTRACTED_FACTS_PATH)
    return facts_df


def run_stage_5(facts_df: pd.DataFrame) -> pd.DataFrame:
    log.info("🚀 Stage 5: Deterministic Normalization...")
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

    normalized_df.to_csv(NORMALIZED_FACTS_PATH, index=False)
    log.info("   Saved → %s", NORMALIZED_FACTS_PATH)
    return normalized_df


def run_stage_6(classified_df: pd.DataFrame, normalized_df: pd.DataFrame) -> pd.DataFrame:
    log.info("🚀 Stage 6: Validation & Quality Scoring...")
    dashboard = ValidationEngine().process_catalog_validation(classified_df, normalized_df)
    dashboard.to_csv(VALIDATION_REPORT_PATH, index=False)

    counts = dashboard['status'].value_counts().to_dict() if not dashboard.empty else {}
    log.info(
        "✅ 🟢 %d AUTO_APPROVED  |  🟡 %d HUMAN_REVIEW  |  🔴 %d FAILED",
        counts.get('AUTO_APPROVED', 0), counts.get('HUMAN_REVIEW', 0), counts.get('FAILED', 0),
    )
    log.info("   Quality dashboard saved → %s", VALIDATION_REPORT_PATH)
    return dashboard


def run_stage_7(
    classified_df: pd.DataFrame,
    dashboard: pd.DataFrame,
    normalized_df: pd.DataFrame,
    retrieval_log_df: pd.DataFrame,
) -> pd.DataFrame:
    log.info("🚀 Stage 7: Final Master Export & Schema Mapping...")
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
    exporter.write(catalog_df, FINAL_CATALOG_PATH)
    return catalog_df


def run_pipeline(limit: int = 0, fetch: bool = FETCH_ENABLED, use_ai: bool = True,
                 rpm: int = REQUESTS_PER_MINUTE) -> pd.DataFrame:
    ensure_directories()
    log_path = setup_logging()
    log.info("=" * 70)
    log.info("UniLog Industrial PIM Engine — pipeline start (log: %s)", log_path)
    log.info("=" * 70)

    classified_df = run_stage_1_2(limit)

    retrieval_log_df = run_stage_3(classified_df) if fetch else pd.DataFrame()
    if not fetch:
        log.info("⏭️  Stage 3 skipped (pass --fetch to retrieve manufacturer documents)")

    facts_df = run_stage_4(classified_df, retrieval_log_df, rpm, use_ai)
    normalized_df = run_stage_5(facts_df)
    dashboard = run_stage_6(classified_df, normalized_df)
    catalog_df = run_stage_7(classified_df, dashboard, normalized_df, retrieval_log_df)

    log.info("🎉 Pipeline complete — %d rows × %d columns → %s",
             len(catalog_df), len(catalog_df.columns), FINAL_CATALOG_PATH)
    return catalog_df


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="UniLog Industrial PIM Engine")
    parser.add_argument('--limit', type=int, default=0, help="process only the first N products")
    parser.add_argument('--fetch', action='store_true', help="enable Stage 3 document retrieval")
    parser.add_argument('--no-ai', dest='use_ai', action='store_false',
                        help="skip Stage 4 API calls (plumbing test only)")
    parser.add_argument('--rpm', type=int, default=REQUESTS_PER_MINUTE,
                        help=f"extraction rate limit in requests/minute (default {REQUESTS_PER_MINUTE})")
    parser.add_argument('--verbose', action='store_true', help="print DEBUG output to the console")
    return parser.parse_args(argv)


if __name__ == "__main__":
    args = parse_args()
    setup_logging(logging.DEBUG if args.verbose else logging.INFO)
    try:
        run_pipeline(limit=args.limit, fetch=args.fetch, use_ai=args.use_ai, rpm=args.rpm)
    except KeyboardInterrupt:
        log.warning("Interrupted by user — partial results remain in data/intermediate/")
        sys.exit(130)
