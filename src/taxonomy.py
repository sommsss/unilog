import re
from typing import Any, Dict, List, Tuple

import pandas as pd

from src.logging_setup import get_logger

log = get_logger("taxonomy")


class TaxonomyEngine:
    def __init__(self):
        # The Category-to-Schema Matrix
        # Defines the mandatory facts we need the LLM to extract for each category.
        self.schema_matrix = {
            'Lighting & Electrical': [
                'Voltage', 'Wattage', 'Color Temperature', 'Lumens',
                'Base Type', 'Amperage', 'Material'
            ],
            'Building Materials & Decking': [
                'Material', 'Length', 'Width', 'Thickness',
                'Color/Finish', 'Profile Type'
            ],
            'Power Tools & Equipment': [
                'Voltage', 'Battery Type', 'Max RPM',
                'Motor Type (Brushless/Brushed)', 'Chuck Size'
            ],
            'Abrasives': [
                'Grit', 'Diameter', 'Arbor Size',
                'Abrasive Material', 'Max RPM'
            ],
            'Appliances': [
                'Capacity', 'Voltage', 'Dimensions (HxWxD)',
                'Energy Star Rated', 'Installation Type'
            ],
            'Tools & Accessories': [
                'Shank Size', 'Diameter', 'Overall Length',
                'Material Application', 'Drive Size'
            ],
            'General / Industrial': [
                'Material', 'Dimensions', 'Weight', 'Color'
            ]
        }

        # Category -> (Dept, Class, Fine) for the delivery format's Classpath columns.
        self.classpath_matrix = {
            'Lighting & Electrical': ('Electrical', 'Lighting & Electrical', 'Fixtures & Wiring'),
            'Building Materials & Decking': ('Building Materials', 'Decking & Railing', 'Boards & Trim'),
            'Power Tools & Equipment': ('Tools', 'Power Tools', 'Cordless & Corded Tools'),
            'Abrasives': ('Abrasives', 'Coated Abrasives', 'Discs & Belts'),
            'Appliances': ('Appliances & Consumer Electronics', 'Large Appliances', 'Kitchen Appliances'),
            'Tools & Accessories': ('Tools', 'Tool Accessories', 'Bits, Blades & Drivers'),
            'General / Industrial': ('Industrial Supplies', 'General Industrial', 'Miscellaneous'),
        }

        # Product-name nouns, checked longest-phrase-first against the description.
        self.product_nouns = [
            'sanding belt', 'sanding disc', 'hole saw', 'router bit', 'circular saw',
            'impact driver', 'impact wrench', 'post sleeve', 'post cap', 'light fixture',
            'cut off wheel', 'grinding wheel', 'flap disc', 'dishwasher', 'refrigerator',
            'washer', 'dryer', 'range', 'oven', 'microwave', 'decking', 'fascia',
            'baluster', 'railing', 'drill', 'grinder', 'saw', 'blade', 'bit', 'disc',
            'belt', 'wrench', 'screwdriver', 'hammer', 'wire', 'cable', 'switch',
            'receptacle', 'outlet', 'lamp', 'bulb', 'fixture', 'pad', 'brush',
        ]

    def classify_product(self, raw_desc: str, raw_mfr: str, part_num: str) -> str:
        """
        Determines the product category based on description and manufacturer signals.
        """
        desc = str(raw_desc).lower()
        mfr = str(raw_mfr).lower()
        pn = str(part_num).lower()

        # 1. Lighting & Electrical
        if any(k in desc for k in ['led', 'light', 'fixture', 'lamp', 'bulb', 'luminaire', 'wire', 'switch', 'receptacle', 'outlet', 'cable']) or \
           any(m in mfr for m in ['lighting', 'kichler', 'satco', 'southwire', 'leviton']):
            return 'Lighting & Electrical'

        # 2. Building Materials, Decking & Fencing
        if any(k in desc for k in ['decking', 'trex', 'azek', 'fascia', 'grooved', 'timber', 'rail', 'post sleeve', 'post trim', 'post cap', 'baluster', 'mortar', 'gate']) or \
           any(m in mfr for m in ['lumber', 'parksite', 'boise', 'rees cast']):
            return 'Building Materials & Decking'

        # 3. Power Tools & Equipment
        if any(k in desc for k in ['saw', 'drill', 'impact', 'grinder', 'router', 'bare tool', 'cordless', 'inflator']) and \
           any(brand in desc or brand in mfr for brand in ['milw', 'dewalt', 'makita', 'festool']):
            return 'Power Tools & Equipment'

        # 4. Abrasives
        if any(k in desc for k in ['disc', 'sanding belt', 'abrasive', 'cubitron', 'stikit', 'grinding', 'abranet']) or \
           any(m in mfr for m in ['mirka', 'abrasive']) or '3mabr' in pn:
            return 'Abrasives'

        # 5. Appliances
        if 'appliance' in mfr or 'ge ' in desc or 'whirlpool' in mfr or \
           any(k in desc for k in ['refrigerator', 'washer', 'dryer', 'dishwasher', 'oven', 'range']):
            return 'Appliances'

        # 6. Tools & Accessories
        if any(k in desc for k in ['bit', 'blade', 'cut off', 'hole saw', 'router bit', 'pad', 'wrench']) or \
           'wera tools' in mfr:
            return 'Tools & Accessories'

        # Fallback
        return 'General / Industrial'

    def get_target_attributes(self, category: str) -> List[str]:
        """Returns the specific list of attributes to extract for this category."""
        return self.schema_matrix.get(category, self.schema_matrix['General / Industrial'])

    def get_classpath(self, category: str) -> Tuple[str, str, str, str]:
        """Returns (Dept, Class, Fine, Classpath) for the delivery format."""
        dept, cls, fine = self.classpath_matrix.get(
            category, self.classpath_matrix['General / Industrial']
        )
        return dept, cls, fine, f"{dept}>{cls}>{fine}"

    def extract_product_name(self, raw_desc: str, category: str) -> str:
        """Derives the short product noun the delivery format expects (e.g. 'Sanding Belt')."""
        desc = str(raw_desc).lower()
        for noun in self.product_nouns:
            if re.search(rf'\b{re.escape(noun)}\b', desc):
                return noun.title()
        # Fall back to the leaf of the classpath (e.g. 'Discs & Belts').
        return self.get_classpath(category)[2]

    def process_catalog(self, df: pd.DataFrame) -> pd.DataFrame:
        """Applies classification, taxonomy paths and target attributes to the dataset."""
        df = df.copy()

        df['Assigned_Category'] = df.apply(
            lambda row: self.classify_product(
                row.get('Part_Desc', ''), row.get('Part_Manuf', ''), row.get('Mfg_Part_Num', '')
            ),
            axis=1
        )

        df['Target_Attributes'] = df['Assigned_Category'].apply(self.get_target_attributes)

        paths = df['Assigned_Category'].apply(self.get_classpath)
        df['Dept'] = paths.apply(lambda p: p[0])
        df['Class'] = paths.apply(lambda p: p[1])
        df['Fine'] = paths.apply(lambda p: p[2])
        df['Classpath'] = paths.apply(lambda p: p[3])

        df['Product_Name'] = df.apply(
            lambda row: self.extract_product_name(row.get('Part_Desc', ''), row['Assigned_Category']),
            axis=1
        )

        counts: Dict[str, Any] = df['Assigned_Category'].value_counts().to_dict()
        log.info("Category distribution: %s", counts)
        return df
