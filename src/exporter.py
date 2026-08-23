"""Stage 7: assemble the final master catalog in the 252-column delivery format."""

import os
from typing import Any, Dict, Iterable, List, Optional

import pandas as pd

from src.config import FINAL_CATALOG_PATH
from src.logging_setup import get_logger
from src.mapper import SchemaMapper

log = get_logger("exporter")


class MasterExporter:
    def __init__(self, mapper: Optional[SchemaMapper] = None):
        self.mapper = mapper or SchemaMapper()
        # The full delivery schema, loaded from the client's Expected Output sheet.
        self.master_columns = self.mapper.columns

    @staticmethod
    def _facts_by_product(normalized_facts_df: pd.DataFrame) -> Dict[str, List[Dict[str, Any]]]:
        if normalized_facts_df is None or normalized_facts_df.empty:
            return {}
        grouped: Dict[str, List[Dict[str, Any]]] = {}
        for product_id, chunk in normalized_facts_df.groupby('product_id'):
            grouped[str(product_id)] = chunk.to_dict('records')
        return grouped

    @staticmethod
    def _urls_by_product(retrieval_log_df: Optional[pd.DataFrame]) -> Dict[str, List[str]]:
        if retrieval_log_df is None or retrieval_log_df.empty:
            return {}
        successful = retrieval_log_df[retrieval_log_df['retrieval_status'] == 'success']
        return {
            str(pid): chunk['source_url'].tolist()
            for pid, chunk in successful.groupby('product_id')
        }

    def export_master_catalog(
        self,
        clean_df: pd.DataFrame,
        classified_df: pd.DataFrame,
        approved_products: Iterable[str],
        normalized_facts_df: pd.DataFrame,
        retrieval_log_df: Optional[pd.DataFrame] = None,
    ) -> pd.DataFrame:
        """Build one delivery-format row per input product.

        Every input product is carried through so the output stays row-aligned with
        the source file. Only facts belonging to products that cleared validation
        are published into the ATTRIBUTE_* columns; everything else ships with
        identity and taxonomy data only, ready for the human-review queue.
        """
        approved = {str(p) for p in approved_products}
        facts_map = self._facts_by_product(normalized_facts_df)
        urls_map = self._urls_by_product(retrieval_log_df)

        # classified_df already carries the cleaned identity columns; fall back to
        # clean_df if the caller passed a classification frame built elsewhere.
        source_df = classified_df if 'Assigned_Category' in classified_df.columns else clean_df

        rows: List[Dict[str, Any]] = []
        enriched = 0
        for _, record in source_df.iterrows():
            product = record.to_dict()
            product_id = str(product.get('Mfg_Part_Num', ''))

            product_facts = facts_map.get(product_id, []) if product_id in approved else []
            if product_facts:
                enriched += 1

            rows.append(self.mapper.build_row(
                product,
                facts=product_facts,
                source_urls=urls_map.get(product_id, []),
            ))

        final_df = pd.DataFrame(rows, columns=self.master_columns)
        log.info(
            "Master catalog assembled: %d rows x %d columns (%d rows carry published specs)",
            len(final_df), len(final_df.columns), enriched,
        )
        return final_df

    def write(self, catalog_df: pd.DataFrame, path: str = FINAL_CATALOG_PATH) -> str:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        catalog_df.to_csv(path, index=False)
        log.info("Wrote %s", path)
        return path
