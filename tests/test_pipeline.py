"""Stage 5 (normalization), 6 (validation) and 7 (schema mapping) unit tests."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

from src.exporter import MasterExporter
from src.extractor import RateLimiter, _parse_retry_delay
from src.fetcher import AutonomousFetcher
from src.mapper import SchemaMapper
from src.normalizer import DeterministicNormalizer
from src.validator import ValidationEngine


# --- Stage 5 --------------------------------------------------------------
def test_normalizer_handles_missing_and_nan_values():
    n = DeterministicNormalizer()
    fact = n.normalize_fact({'attribute_name': 'Brand', 'extracted_value': '3M', 'uom': float('nan')})
    assert fact['normalized_uom'] == ''
    assert fact['normalization_flag'] == 'SUCCESS'


def test_normalizer_maps_units_and_materials():
    n = DeterministicNormalizer()
    assert n.normalize_uom('inches') == ('IN', True)
    assert n.normalize_uom('Disc/Box') == ('BOX', True)
    assert n.normalize_value('Abrasive Material', 'Cubitron II') == ('CERAMIC', True)
    # Unknown vocabulary is upper-cased but flagged for review.
    assert n.normalize_value('Material', 'Unobtainium') == ('UNOBTAINIUM', False)


def test_normalizer_preserves_fractions_and_trims_decimals():
    n = DeterministicNormalizer()
    assert n.normalize_value('Width', '1/2') == ('1/2', True)
    assert n.normalize_value('Depth', '24-1/4') == ('24-1/4', True)
    assert n.normalize_value('Length', '18.00') == ('18', True)


def test_grit_scale_prefix_folds_into_value():
    n = DeterministicNormalizer()
    fact = n.normalize_fact({'attribute_name': 'Grit', 'extracted_value': '80', 'uom': 'P'})
    assert (fact['normalized_value'], fact['normalized_uom']) == ('P80', '')


def test_field_char_limits_enforced_on_word_boundary():
    n = DeterministicNormalizer()
    out = n.enforce_field_limit('INVOICE_DESC', 'SANDING BELT ' * 20)
    assert len(out) <= 40 and not out.endswith(' ')


# --- Stage 6 --------------------------------------------------------------
def _fact(name, value, confidence='High', flag='SUCCESS'):
    return {'attribute_name': name, 'normalized_value': value,
            'confidence': confidence, 'normalization_flag': flag}


def test_three_tier_routing():
    v = ValidationEngine()
    assert v.route(95) == 'AUTO_APPROVED'
    assert v.route(65) == 'HUMAN_REVIEW'
    assert v.route(20) == 'FAILED'


def test_no_facts_fails_closed():
    metrics = ValidationEngine().calculate_metrics([], ['Grit'])
    assert metrics['status'] == 'FAILED' and metrics['quality_score'] == 0


def test_conflicting_values_are_detected_and_penalized():
    v = ValidationEngine()
    targets = ['Grit', 'Diameter']
    clean = v.calculate_metrics([_fact('Grit', '150'), _fact('Diameter', '5')], targets)
    conflicted = v.calculate_metrics(
        [_fact('Grit', '150'), _fact('Diameter', '5'), _fact('Grit', '120', 'Low')], targets
    )
    assert conflicted['conflicts_detected'] == 'Grit'
    assert conflicted['quality_score'] < clean['quality_score']


# --- Stage 7 --------------------------------------------------------------
def test_mapper_produces_full_delivery_schema():
    m = SchemaMapper()
    assert len(m.columns) == 252
    assert m.attribute_slots == 50
    row = m.build_row({'Mfg_Part_Num': 'X1', 'Resolved_Brand': 'Diablo', 'Resolved_MFR': 'Freud Inc',
                       'Product_Name': 'Sanding Belt'},
                      [_fact('Grit', '150')])
    assert len(row) == 252
    assert row['ATTRIBUTE_LABEL 1'] == 'Grit' and row['ATTRIBUTE_VALUE 1'] == '150'


def test_mapper_keeps_highest_confidence_value_per_attribute():
    ranked = SchemaMapper.rank_facts([_fact('Grit', 'P150', 'Low'), _fact('Grit', '150', 'High')])
    assert len(ranked) == 1 and ranked[0]['normalized_value'] == '150'


def test_duplicate_attributes_do_not_break_export():
    """The old pivot()-based exporter raised ValueError on repeated attribute names."""
    classified = pd.DataFrame([{'Mfg_Part_Num': 'X1', 'Part_Desc': 'Belt', 'Resolved_Brand': 'Diablo',
                                'Resolved_MFR': 'Freud', 'Assigned_Category': 'Abrasives',
                                'Product_Name': 'Sanding Belt', 'Dept': 'Abrasives', 'Class': 'C',
                                'Fine': 'F', 'Classpath': 'Abrasives>C>F'}])
    facts = pd.DataFrame([
        {'product_id': 'X1', **_fact('Grit', '150')},
        {'product_id': 'X1', **_fact('Grit', 'P150', 'Low')},
    ])
    out = MasterExporter().export_master_catalog(classified, classified, ['X1'], facts)
    assert out.shape == (1, 252)


def test_unapproved_products_still_ship_identity_rows():
    classified = pd.DataFrame([
        {'Mfg_Part_Num': 'X1', 'Part_Desc': 'Belt', 'Resolved_Brand': 'Diablo', 'Resolved_MFR': 'Freud',
         'Assigned_Category': 'Abrasives', 'Product_Name': 'Sanding Belt'},
        {'Mfg_Part_Num': 'X2', 'Part_Desc': 'Disc', 'Resolved_Brand': '3M', 'Resolved_MFR': '3M',
         'Assigned_Category': 'Abrasives', 'Product_Name': 'Disc'},
    ])
    facts = pd.DataFrame([{'product_id': 'X1', **_fact('Grit', '150')},
                          {'product_id': 'X2', **_fact('Grit', '80')}])
    out = MasterExporter().export_master_catalog(classified, classified, ['X1'], facts)
    assert len(out) == 2                              # every input product ships
    assert out.iloc[0]['ATTRIBUTE_VALUE 1'] == '150'  # approved product carries specs
    assert out.iloc[1]['ATTRIBUTE_VALUE 1'] == ''     # unapproved carries identity only
    assert out.iloc[1]['Mfg_Part_Num'] == 'X2'


# --- Stage 3 & 4 infrastructure ------------------------------------------
def test_marketplace_domains_are_blocked():
    f = AutonomousFetcher()
    assert not f.is_allowed_domain('https://www.amazon.com/dp/X')
    assert not f.is_allowed_domain('https://grainger.com/x')
    assert f.is_allowed_domain('https://www.freudtools.com/spec.pdf')


def test_search_result_redirects_are_unwrapped():
    unwrapped = AutonomousFetcher._unwrap_redirect('//duckduckgo.com/l/?uddg=https%3A%2F%2Fx.com%2Fa.pdf&rut=1')
    assert unwrapped == 'https://x.com/a.pdf'


def test_rate_limiter_spaces_calls():
    import time
    limiter = RateLimiter(120)  # 0.5s apart
    start = time.monotonic()
    limiter.wait(); limiter.wait(); limiter.wait()
    assert time.monotonic() - start >= 1.0


def test_retry_delay_is_read_from_quota_error():
    assert _parse_retry_delay("429 RESOURCE_EXHAUSTED ... 'retryDelay': '31s'") == 31.0
    assert _parse_retry_delay("503 UNAVAILABLE") is None


if __name__ == '__main__':
    import traceback
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn()
                print(f'PASS {name}')
            except Exception:
                failures += 1
                print(f'FAIL {name}')
                traceback.print_exc()
    raise SystemExit(1 if failures else 0)
