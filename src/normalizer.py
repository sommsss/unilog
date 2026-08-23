"""Stage 5: deterministic normalization. No AI — pure rule-based standardization."""

import math
import re
from typing import Any, Dict, Tuple

from src.config import FIELD_CHAR_LIMITS


def _as_text(value: Any) -> str:
    """Coerce anything (including pandas NaN read back from CSV) to a clean string."""
    if value is None:
        return ""
    if isinstance(value, float) and math.isnan(value):
        return ""
    text = str(value).strip()
    return "" if text.lower() in ('nan', 'none', 'null') else text


class DeterministicNormalizer:
    def __init__(self):
        # 1. Extended UOM Mapping Table
        self.uom_map = {
            'in': 'IN', 'inch': 'IN', 'inches': 'IN', '"': 'IN', 'in.': 'IN',
            'ft': 'FT', 'foot': 'FT', 'feet': 'FT', "'": 'FT', 'ft.': 'FT',
            'mm': 'MM', 'millimeter': 'MM', 'millimeters': 'MM',
            'cm': 'CM', 'm': 'M', 'micron': 'UM', 'microns': 'UM',
            'lbs': 'LB', 'lb': 'LB', 'pound': 'LB', 'pounds': 'LB',
            'oz': 'OZ', 'ounce': 'OZ', 'kg': 'KG', 'g': 'G',
            'v': 'V', 'volt': 'V', 'volts': 'V', 'vac': 'VAC', 'vdc': 'VDC',
            'a': 'A', 'amp': 'A', 'amps': 'A', 'ampere': 'A',
            'w': 'W', 'watt': 'W', 'watts': 'W', 'kw': 'KW', 'hp': 'HP',
            'rpm': 'RPM', 'ah': 'AH', 'mah': 'MAH', 'hz': 'HZ',
            'db': 'DB', 'dba': 'DBA', 'k': 'K', 'lumens': 'LM', 'lm': 'LM',
            'pc': 'EA', 'pcs': 'EA', 'piece': 'EA', 'pieces': 'EA', 'ea': 'EA', 'each': 'EA',
            'pk': 'PK', 'pack': 'PK', 'box': 'BOX', 'bx': 'BOX',
            'disc/box': 'BOX', 'pc/box': 'BOX', 'per box': 'BOX', 'roll': 'ROLL',
            'set': 'SET', 'kit': 'KIT', 'pair': 'PR', 'grit': '',
        }

        # 2. Material Controlled Vocabulary Lookup
        self.material_map = {
            'aluminum': 'ALUMINUM', 'alum': 'ALUMINUM', 'al': 'ALUMINUM',
            'aluminum oxide': 'ALUMINUM OXIDE', 'alox': 'ALUMINUM OXIDE',
            'stainless steel': 'STAINLESS STEEL', 'ss': 'STAINLESS STEEL',
            'carbon steel': 'CARBON STEEL', 'steel': 'STEEL',
            'ceramic': 'CERAMIC', 'ceramic aluminum oxide': 'CERAMIC ALUMINUM OXIDE',
            'cubitron': 'CERAMIC', 'cubitron ii': 'CERAMIC',
            'silicon carbide': 'SILICON CARBIDE', 'zirconia': 'ZIRCONIA',
            'zirconia alumina': 'ZIRCONIA ALUMINA', 'diamond': 'DIAMOND',
            'film': 'FILM', 'cloth': 'CLOTH', 'paper': 'PAPER', 'mesh': 'MESH',
            'pvc': 'PVC', 'vinyl': 'VINYL', 'plastic': 'PLASTIC',
            'brass': 'BRASS', 'copper': 'COPPER', 'cast iron': 'CAST IRON',
            'wood': 'WOOD', 'composite': 'COMPOSITE', 'rubber': 'RUBBER',
            'nylon': 'NYLON', 'carbide': 'CARBIDE', 'hss': 'HIGH SPEED STEEL',
        }

        # 3. Boolean / Binary Status Map
        self.boolean_map = {
            'yes': 'YES', 'true': 'YES', 'y': 'YES', '1': 'YES',
            'no': 'NO', 'false': 'NO', 'n': 'NO', '0': 'NO'
        }

    def normalize_uom(self, raw_uom: Any) -> Tuple[str, bool]:
        """Standardize raw unit strings into standard UOM codes."""
        cleaned = _as_text(raw_uom).lower()
        if not cleaned:
            return "", True
        if cleaned in self.uom_map:
            return self.uom_map[cleaned], True
        return _as_text(raw_uom).upper(), False

    def normalize_value(self, attr_name: Any, raw_value: Any) -> Tuple[str, bool]:
        """Applies ENUM maps and numerical cleanups to raw values."""
        val_clean = _as_text(raw_value)
        if not val_clean:
            return "", True

        val_lower = val_clean.lower()
        attr_lower = _as_text(attr_name).lower()

        # Material Normalization (covers 'Material', 'Backing Material', 'Abrasive Material')
        if 'material' in attr_lower:
            if val_lower in self.material_map:
                return self.material_map[val_lower], True
            return val_clean.upper(), False

        # Grit designations keep their P-prefix and carry no unit.
        if 'grit' in attr_lower:
            match = re.match(r'^p?\s*(\d+)$', val_lower)
            if match:
                prefix = 'P' if val_lower.startswith('p') else ''
                return f"{prefix}{match.group(1)}", True
            return val_clean.upper(), False

        # Boolean Field Normalization
        if any(term in attr_lower for term in ['energy star', 'brushless', 'cordless', 'discontinued']):
            if val_lower in self.boolean_map:
                return self.boolean_map[val_lower], True
            return val_clean, False

        # Fractional measurements stay as written ("1/2", "24-1/4") — the delivery
        # format keeps imperial fractions rather than converting to decimals.
        if re.match(r'^\d+(-\d+)?/\d+$', val_clean) or re.match(r'^\d+-\d+/\d+$', val_clean):
            return val_clean, True

        # Numerical Precision Clean-up (e.g., "18.00" -> "18")
        if re.match(r'^\d+(\.\d+)?$', val_clean):
            num = float(val_clean)
            if num.is_integer():
                return str(int(num)), True
            return f"{num:.2f}", True

        return val_clean, True

    def truncate_text(self, text: Any, max_chars: int) -> str:
        """Truncates text safely at word boundaries without breaking words."""
        text = _as_text(text)
        if not text or len(text) <= max_chars:
            return text
        return text[:max_chars].rsplit(' ', 1)[0]

    def enforce_field_limit(self, field_name: str, text: Any) -> str:
        """Applies the delivery-format character limit for a named output field."""
        limit = FIELD_CHAR_LIMITS.get(field_name)
        text = _as_text(text)
        return self.truncate_text(text, limit) if limit else text

    def normalize_fact(self, fact_record: Dict[str, Any]) -> Dict[str, Any]:
        """Applies normalization rules to a raw fact dict from Stage 4."""
        fact_record = dict(fact_record)
        attr_name = fact_record.get('attribute_name', '')
        raw_val = fact_record.get('extracted_value', '')
        raw_uom = fact_record.get('uom', '')

        # Grit is a scale designation, not a measurement: fold a stray "P" unit
        # back into the value (P + 80 -> P80) and leave the UOM empty.
        if 'grit' in _as_text(attr_name).lower() and _as_text(raw_uom).upper() == 'P':
            raw_val = f"P{_as_text(raw_val)}"
            raw_uom = ''

        norm_val, val_flag = self.normalize_value(attr_name, raw_val)
        norm_uom, uom_flag = self.normalize_uom(raw_uom)

        fact_record['normalized_value'] = norm_val
        fact_record['normalized_uom'] = norm_uom
        fact_record['normalization_flag'] = 'SUCCESS' if (val_flag and uom_flag) else 'UNMAPPED_ENUM'

        return fact_record
