import re
from typing import Optional, Dict, Any
from src.config import PLACEHOLDERS

def sanitize_value(val: Any) -> Optional[str]:
    """Strip spaces and remove standard placeholder strings."""
    if val is None:
        return None
    s = str(val).strip()
    if s.lower() in PLACEHOLDERS:
        return None
    return s

def clean_input_row(row: Dict[str, Any]) -> Dict[str, Optional[str]]:
    """Clean supplier row fields and extract clean manufacturer names."""
    mfg_part_num = sanitize_value(row.get('Mfg_Part_Num'))
    part_desc = sanitize_value(row.get('Part_Desc'))
    
    # Strip internal ERP suffix codes like (2435) or (JAMIN) from manufacturer names
    raw_mfr = sanitize_value(row.get('Part_Manuf')) or ''
    mfr_clean = re.sub(r'\s*\([A-Z0-9]+\)$', '', raw_mfr).strip()
    
    # Resolve brand with fallback chain
    brand_clean = (
        sanitize_value(row.get('E1_Brand')) or
        sanitize_value(row.get('Unilog_Brand')) or
        sanitize_value(row.get('DIB_Brand')) or
        mfr_clean or
        None
    )

    return {
        'Mfg_Part_Num': mfg_part_num,
        'Part_Desc': part_desc,
        'E1_Brand': sanitize_value(row.get('E1_Brand')),
        'Unilog_Brand': sanitize_value(row.get('Unilog_Brand')),
        'DIB_Brand': sanitize_value(row.get('DIB_Brand')),
        'Part_Manuf': raw_mfr,
        'Resolved_MFR': mfr_clean if mfr_clean else 'UNKNOWN',
        'Resolved_Brand': brand_clean if brand_clean else 'UNKNOWN',
    }