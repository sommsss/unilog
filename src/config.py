import os

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# --- Data locations -------------------------------------------------------
DATA_DIR = os.path.join(BASE_DIR, "data")
INPUT_CSV_PATH = os.path.join(DATA_DIR, "input", "Unihack_ Sample Dataset - Input.csv")
SCHEMA_CSV_PATH = os.path.join(DATA_DIR, "schema", "Unihack_ Expected Output - Delivery Format.csv")

INTERMEDIATE_DIR = os.path.join(DATA_DIR, "intermediate")
OUTPUT_DIR = os.path.join(DATA_DIR, "output")
DOCUMENT_DIR = os.path.join(DATA_DIR, "retrieved_documents")
LOG_DIR = os.path.join(BASE_DIR, "logs")

# Stage-by-stage evidence trail (Stage 4 -> 5 -> 6)
EXTRACTED_FACTS_PATH = os.path.join(INTERMEDIATE_DIR, "extracted_facts.csv")
NORMALIZED_FACTS_PATH = os.path.join(INTERMEDIATE_DIR, "normalized_facts.csv")
VALIDATION_REPORT_PATH = os.path.join(INTERMEDIATE_DIR, "validation_report.csv")
RETRIEVAL_LOG_PATH = os.path.join(INTERMEDIATE_DIR, "retrieval_log.csv")

OUTPUT_CSV_PATH = os.path.join(OUTPUT_DIR, "enriched_catalog_output.csv")
FINAL_CATALOG_PATH = os.path.join(OUTPUT_DIR, "FINAL_MASTER_CATALOG.csv")


def ensure_directories() -> None:
    """Create every directory the pipeline writes to."""
    for path in (INTERMEDIATE_DIR, OUTPUT_DIR, DOCUMENT_DIR, LOG_DIR):
        os.makedirs(path, exist_ok=True)


# --- LLM configuration ----------------------------------------------------
# gemini-3.5-flash: structured extraction, low latency, available on the free tier.
# Swap for a larger model (or Claude Sonnet) in production by changing this line only.
MODEL_NAME = "gemini-3.5-flash"

# Free-tier quota is ~15 requests/minute. The extractor paces itself to stay
# under REQUESTS_PER_MINUTE so a 1,000-product run never trips 429.
REQUESTS_PER_MINUTE = 15
MAX_RETRIES = 5
# Wait applied when the API reports a quota exhaustion and gives no retry hint.
RATE_LIMIT_COOLDOWN_SECONDS = 60

# --- Stage 3 (document retrieval) ----------------------------------------
FETCH_ENABLED = False          # opt-in: main.py --fetch
FETCH_DELAY_SECONDS = 2.0      # politeness delay between outbound requests
FETCH_TIMEOUT_SECONDS = 15
MAX_DOCS_PER_PRODUCT = 2

# --- Stage 6 (routing thresholds) ----------------------------------------
AUTO_APPROVE_SCORE = 80        # >= 80  -> AUTO_APPROVED  (green)
REVIEW_SCORE = 50              # 50-79  -> HUMAN_REVIEW   (yellow)
                               # < 50   -> FAILED         (red)
MIN_COMPLETENESS = 0.30
MIN_CONFIDENCE = 0.50
CONFLICT_PENALTY = 10          # points deducted per conflicting attribute

# Placeholders to ignore across all supplier fields
PLACEHOLDERS = {
    '-- unbranded --', '-- no unilog brand --', '-- no dib brand --',
    'n/a', 'null', 'none', 'unknown', '', 'nan'
}

# Unit of Measure normalization map
UOM_MAP = {
    'in': 'IN', 'inch': 'IN', 'inches': 'IN', '"': 'IN',
    'ft': 'FT', 'foot': 'FT', 'feet': 'FT', "'": 'FT',
    'mm': 'MM', 'millimeter': 'MM', 'millimeters': 'MM',
    'lbs': 'LB', 'lb': 'LB', 'pound': 'LB', 'pounds': 'LB',
    'oz': 'OZ', 'ounce': 'OZ',
    'pc': 'EA', 'piece': 'EA', 'ea': 'EA', 'each': 'EA',
    'pk': 'PK', 'pack': 'PK', 'box': 'BOX', 'bx': 'BOX',
    'v': 'V', 'volt': 'V', 'volts': 'V',
    'a': 'A', 'amp': 'A', 'amps': 'A', 'ampere': 'A',
    'w': 'W', 'watt': 'W', 'watts': 'W',
    'kw': 'KW', 'kilowatt': 'KW', 'kilowatts': 'KW',
    'rpm': 'RPM', 'hp': 'HP', 'horsepower': 'HP'
}

# Material standardization map
MATERIAL_MAP = {
    'aluminum': 'ALU', 'aluminium': 'ALU', 'al': 'ALU',
    'steel': 'STL', 'stainless': 'SS',
    'iron': 'IR', 'cast iron': 'CI',
    'copper': 'CU', 'brass': 'BR',
    'plastic': 'PLS', 'nylon': 'NYL', 'rubber': 'RBR'
}

# Rating/Standard enumerations (common electrical standards)
RATINGS = {
    'ip': ['IP20', 'IP44', 'IP54', 'IP55', 'IP65', 'IP67'],
    'insulation_class': ['A', 'B', 'F', 'H'],
    'cooling': ['IC01', 'IC06', 'IC81W', 'IC86', 'IC401', 'IC611']
}

# Character limits for the delivery-format description fields.
# Values follow the filled sample rows in the Delivery Format sheet, where
# INVOICE_DESC is a short uppercase ERP string and LONG_DESC1 carries detail.
FIELD_CHAR_LIMITS = {
    'INVOICE_DESC': 40,
    'MOBILE_DESC': 100,
    'SHORT_DESC': 120,
    'LONG_DESC1': 250,
    'RETAIL_DESC': 200,
    'MARKETING_DESCRIPTION': 500,
    'BRAND_NAME': 50,
    'MANUFACTURER_NAME': 50,
    'Mfg_Part_Num': 50
}

# The delivery format carries 50 ATTRIBUTE_LABEL/VALUE/UOM triplets per row.
MAX_ATTRIBUTE_SLOTS = 50

# Default target attributes to extract if not specified by category
DEFAULT_TARGET_ATTRIBUTES = [
    'Power', 'Voltage', 'Amperage', 'Weight', 'Dimensions',
    'Material', 'Color', 'Operating Temperature', 'IP Rating',
    'Insulation Class', 'Cooling Method', 'Speed', 'Torque'
]
