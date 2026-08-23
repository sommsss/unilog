"""Stage 1 (cleaning) and Stage 2 (classification) unit tests."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.cleaner import clean_input_row, sanitize_value
from src.taxonomy import TaxonomyEngine


def test_sanitize_strips_placeholders():
    assert sanitize_value('-- Unbranded --') is None
    assert sanitize_value('-- No Unilog Brand --') is None
    assert sanitize_value('N/A') is None
    assert sanitize_value('  Diablo  ') == 'Diablo'


def test_erp_suffix_is_stripped_from_manufacturer():
    row = clean_input_row({'Mfg_Part_Num': 'X1', 'Part_Manuf': 'Freud Inc (2435)'})
    assert row['Resolved_MFR'] == 'Freud Inc'
    # The raw supplier value is preserved for the audit trail.
    assert row['Part_Manuf'] == 'Freud Inc (2435)'


def test_brand_fallback_chain_prefers_first_real_value():
    row = clean_input_row({
        'Mfg_Part_Num': 'X1',
        'E1_Brand': '-- Unbranded --',
        'Unilog_Brand': '-- No Unilog Brand --',
        'DIB_Brand': 'Diablo',
        'Part_Manuf': 'Freud Inc (2435)',
    })
    assert row['Resolved_Brand'] == 'Diablo'


def test_brand_falls_back_to_manufacturer_then_unknown():
    with_mfr = clean_input_row({'Mfg_Part_Num': 'X1', 'E1_Brand': 'N/A', 'Part_Manuf': 'Freud Inc (2435)'})
    assert with_mfr['Resolved_Brand'] == 'Freud Inc'

    bare = clean_input_row({'Mfg_Part_Num': 'X1'})
    assert bare['Resolved_Brand'] == 'UNKNOWN'
    assert bare['Resolved_MFR'] == 'UNKNOWN'


def test_classification_and_classpath():
    engine = TaxonomyEngine()
    assert engine.classify_product('3M Stikit Film P150 Disc/Box', 'Jam Industrial', '3MABR-1') == 'Abrasives'
    assert engine.classify_product('LED Wall Light Fixture', 'Kichler', 'K-1') == 'Lighting & Electrical'

    dept, cls, fine, classpath = engine.get_classpath('Abrasives')
    assert classpath == f"{dept}>{cls}>{fine}"


def test_target_attributes_always_returned():
    engine = TaxonomyEngine()
    assert engine.get_target_attributes('Abrasives')
    # Unknown categories fall back rather than returning nothing.
    assert engine.get_target_attributes('Nonexistent Category')


def test_product_name_extraction():
    engine = TaxonomyEngine()
    assert engine.extract_product_name('Diablo 1/2"x18" - Sanding Belt 6pc', 'Abrasives') == 'Sanding Belt'
    # Falls back to the taxonomy leaf when no known noun appears.
    assert engine.extract_product_name('XYZ-9000 assembly', 'Abrasives') == 'Discs & Belts'


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
