"""Stage 7a: map cleaned identity data + validated facts onto the 252-column
delivery format defined by the client's Expected Output sheet."""

import csv
import re
from typing import Any, Dict, Iterable, List, Optional

from src.config import FIELD_CHAR_LIMITS, MAX_ATTRIBUTE_SLOTS, SCHEMA_CSV_PATH
from src.logging_setup import get_logger

log = get_logger("mapper")

# Facts whose attribute name matches one of these patterns are promoted into
# the dedicated dimension columns as well as the ATTRIBUTE_* triplets.
DIMENSION_FIELDS = {
    'LENGTH': ('length', 'overall length', 'belt length'),
    'WIDTH': ('width',),
    'HEIGHT': ('height', 'thickness'),
    'WEIGHT': ('weight',),
    'VOLUME': ('volume', 'capacity'),
}

CONFIDENCE_RANK = {'high': 0, 'medium': 1, 'low': 2}

# Facts that restate identity rather than describe a spec. They feed the dedicated
# identity columns instead of consuming one of the 50 ATTRIBUTE_* slots.
IDENTITY_ATTRIBUTES = {
    'brand', 'brand name', 'manufacturer', 'manufacturer name', 'part number',
    'part num', 'mfg part number', 'manufacturer part number', 'sku',
    'model', 'model number', 'product type', 'product name',
}


def _clip(text: Any, field: str) -> str:
    """Truncate to the delivery-format character limit at a word boundary."""
    if text is None:
        return ""
    s = str(text).strip()
    limit = FIELD_CHAR_LIMITS.get(field)
    if not limit or len(s) <= limit:
        return s
    clipped = s[:limit]
    if ' ' in clipped:
        clipped = clipped.rsplit(' ', 1)[0]
    return clipped.strip()


def _blank(value: Any) -> bool:
    if value is None:
        return True
    s = str(value).strip()
    return s == "" or s.lower() in {'nan', 'none', 'null', 'unknown'}


class SchemaMapper:
    def __init__(self, schema_path: str = SCHEMA_CSV_PATH):
        self.schema_path = schema_path
        self.columns = self._load_columns(schema_path)
        self.attribute_slots = self._count_attribute_slots(self.columns)
        log.info(
            "Loaded delivery schema: %d columns, %d attribute slots",
            len(self.columns), self.attribute_slots,
        )

    @staticmethod
    def _load_columns(schema_path: str) -> List[str]:
        with open(schema_path, newline='', encoding='utf-8-sig', errors='replace') as fh:
            header = next(csv.reader(fh))
        return [h.strip() for h in header]

    @staticmethod
    def _count_attribute_slots(columns: Iterable[str]) -> int:
        slots = [
            int(m.group(1))
            for m in (re.match(r'ATTRIBUTE_LABEL (\d+)$', c) for c in columns)
            if m
        ]
        return min(max(slots) if slots else 0, MAX_ATTRIBUTE_SLOTS)

    # --- fact helpers -----------------------------------------------------
    @staticmethod
    def rank_facts(facts: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Dedupe by attribute name, keeping the highest-confidence value first."""
        ordered = sorted(
            facts,
            key=lambda f: CONFIDENCE_RANK.get(str(f.get('confidence', '')).lower().strip(), 3),
        )
        best: Dict[str, Dict[str, Any]] = {}
        for fact in ordered:
            name = str(fact.get('attribute_name', '')).strip()
            value = fact.get('normalized_value', fact.get('extracted_value', ''))
            if not name or _blank(value):
                continue
            best.setdefault(name.lower(), {**fact, 'attribute_name': name})
        return list(best.values())

    @staticmethod
    def _fact_value(fact: Dict[str, Any]) -> str:
        value = fact.get('normalized_value')
        if _blank(value):
            value = fact.get('extracted_value')
        return "" if _blank(value) else str(value).strip()

    @staticmethod
    def _fact_uom(fact: Dict[str, Any]) -> str:
        uom = fact.get('normalized_uom')
        if _blank(uom):
            uom = fact.get('uom')
        return "" if _blank(uom) else str(uom).strip()

    # --- description generation -------------------------------------------
    def build_descriptions(
        self,
        brand: str,
        mpn: str,
        product_name: str,
        facts: List[Dict[str, Any]],
    ) -> Dict[str, str]:
        """Compose the five delivery-format description fields from validated facts."""
        specs = [
            (f['attribute_name'], self._fact_value(f), self._fact_uom(f))
            for f in facts
        ]
        spec_phrases = [
            f"{value} {uom}".strip() if uom else value
            for _, value, uom in specs if value
        ]
        labelled_phrases = [
            f"{label} {value} {uom}".strip() if uom else f"{label} {value}"
            for label, value, uom in specs if value
        ]

        head = " ".join(p for p in [brand, product_name] if p).strip()

        mobile = ", ".join(p for p in [brand, product_name, mpn] if p)
        invoice = " ".join(
            [product_name.upper()] + [p.upper().replace(' ', '') for p in spec_phrases[:4]]
        ).strip()
        short = f"{head} {mpn} " + ", ".join(spec_phrases[:4])
        long_desc = f"{head}, " + ", ".join(labelled_phrases[:10])
        retail = f"{product_name}, " + ", ".join(spec_phrases[:6]) if product_name else ", ".join(spec_phrases[:6])

        return {
            'MOBILE_DESC': _clip(mobile, 'MOBILE_DESC'),
            'INVOICE_DESC': _clip(invoice, 'INVOICE_DESC'),
            'SHORT_DESC': _clip(short.strip().rstrip(','), 'SHORT_DESC'),
            'LONG_DESC1': _clip(long_desc.strip().rstrip(','), 'LONG_DESC1'),
            'RETAIL_DESC': _clip(retail.strip().rstrip(','), 'RETAIL_DESC'),
        }

    # --- row assembly -----------------------------------------------------
    def build_row(
        self,
        product: Dict[str, Any],
        facts: Optional[List[Dict[str, Any]]] = None,
        source_urls: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """Produce one fully-shaped delivery-format row for a single product."""
        facts = self.rank_facts(facts or [])
        row = {col: "" for col in self.columns}

        mpn = "" if _blank(product.get('Mfg_Part_Num')) else str(product['Mfg_Part_Num']).strip()
        brand = "" if _blank(product.get('Resolved_Brand')) else str(product['Resolved_Brand']).strip()
        manufacturer = "" if _blank(product.get('Resolved_MFR')) else str(product['Resolved_MFR']).strip()
        product_name = str(product.get('Product_Name', '') or '').strip()

        # Identity facts refine the resolved fields; spec facts fill ATTRIBUTE_* slots.
        identity_facts = {
            f['attribute_name'].lower(): self._fact_value(f)
            for f in facts if f['attribute_name'].lower() in IDENTITY_ATTRIBUTES
        }
        spec_facts = [f for f in facts if f['attribute_name'].lower() not in IDENTITY_ATTRIBUTES]

        # The supplier brand columns are usually placeholders, so the resolved brand
        # often falls back to the distributor's name. A brand parsed out of the
        # product text is the more accurate label when that happens.
        extracted_brand = identity_facts.get('brand') or identity_facts.get('brand name')
        if extracted_brand and (not brand or brand.lower() == manufacturer.lower()):
            brand = extracted_brand
        if not product_name:
            product_name = identity_facts.get('product name') or identity_facts.get('product type', '')

        # Raw supplier passthrough — preserves referential integrity with the input file.
        for field in ('Mfg_Part_Num', 'Part_Desc', 'E1_Brand', 'Unilog_Brand', 'DIB_Brand', 'Part_Manuf'):
            value = product.get(field)
            row[field] = "" if value is None else str(value)

        # Resolved identity
        manufacturer = identity_facts.get('manufacturer') or identity_facts.get('manufacturer name') or manufacturer
        row['MANUFACTURER_NAME'] = _clip(manufacturer, 'MANUFACTURER_NAME')
        row['BRAND_NAME'] = _clip(brand, 'BRAND_NAME')
        row['MANUFACTURER_PART_NUMBER'] = _clip(mpn, 'Mfg_Part_Num')
        row['PART_NUMBER'] = mpn
        row['Product Name'] = product_name

        # Taxonomy
        row['Dept'] = product.get('Dept', '') or ''
        row['Class'] = product.get('Class', '') or ''
        row['Fine'] = product.get('Fine', '') or ''
        row['Classpath'] = product.get('Classpath', '') or ''

        # Retrieved source documents (Stage 3 audit trail)
        urls = [u for u in (source_urls or []) if u][:6]
        if urls:
            row['MFR URL'] = urls[0]
            for i, url in enumerate(urls[1:], start=1):
                key = f'Ref URL {i}'
                if key in row:
                    row[key] = url

        # Descriptions
        row.update(self.build_descriptions(brand, mpn, product_name, spec_facts))

        # ATTRIBUTE_LABEL/VALUE/UOM triplets
        for slot, fact in enumerate(spec_facts[: self.attribute_slots], start=1):
            row[f'ATTRIBUTE_LABEL {slot}'] = fact['attribute_name']
            row[f'ATTRIBUTE_VALUE {slot}'] = self._fact_value(fact)
            row[f'ATTRIBUTE_UOM {slot}'] = self._fact_uom(fact)

        if len(spec_facts) > self.attribute_slots:
            log.warning(
                "%s: %d spec facts exceeded the %d attribute slots in the delivery "
                "format; the %d lowest-confidence facts were dropped",
                mpn, len(spec_facts), self.attribute_slots,
                len(spec_facts) - self.attribute_slots,
            )

        # Dedicated dimension columns
        for column, keywords in DIMENSION_FIELDS.items():
            for fact in spec_facts:
                name = fact['attribute_name'].lower()
                if any(name == kw or name.startswith(kw) or kw in name for kw in keywords):
                    row[column] = self._fact_value(fact)
                    row[f'{column}_UOM'] = self._fact_uom(fact)
                    break

        return row

    def build_catalog(self, rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """Reindex arbitrary row dicts onto the exact 252-column ordering."""
        return [{col: row.get(col, "") for col in self.columns} for row in rows]
