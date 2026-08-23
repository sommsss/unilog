import json
import os
import random
import re
import threading
import time
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from google import genai

from src.config import (
    MAX_RETRIES,
    MODEL_NAME,
    RATE_LIMIT_COOLDOWN_SECONDS,
    REQUESTS_PER_MINUTE,
)
from src.logging_setup import get_logger

load_dotenv()
log = get_logger("extractor")


class RateLimiter:
    """Paces outbound calls so the pipeline never exceeds a requests-per-minute quota.

    The free Gemini tier allows ~15 RPM. Firing requests as fast as pandas can
    iterate produced 429 RESOURCE_EXHAUSTED around product 130; spacing calls
    60/RPM seconds apart keeps a full 1,000-product run inside quota.
    """

    def __init__(self, requests_per_minute: int):
        self.min_interval = 60.0 / requests_per_minute if requests_per_minute > 0 else 0.0
        self._lock = threading.Lock()
        self._last_call = 0.0

    def wait(self) -> float:
        """Block until the next call is allowed. Returns seconds actually slept."""
        if self.min_interval <= 0:
            return 0.0
        with self._lock:
            elapsed = time.monotonic() - self._last_call
            sleep_for = self.min_interval - elapsed
            if sleep_for > 0:
                time.sleep(sleep_for)
            else:
                sleep_for = 0.0
            self._last_call = time.monotonic()
        return sleep_for

    def pause(self, seconds: float) -> None:
        """Hard cooldown after a quota error — blocks every subsequent caller too."""
        with self._lock:
            time.sleep(seconds)
            self._last_call = time.monotonic()


def _parse_retry_delay(error_text: str) -> Optional[float]:
    """Pull the server-suggested retry delay out of a 429 payload, if present."""
    match = re.search(r"retry(?:Delay|_delay)['\"]?[:\s]+['\"]?(\d+(?:\.\d+)?)s?", error_text)
    if match:
        return float(match.group(1))
    return None


class AIFactExtractor:
    def __init__(self, requests_per_minute: int = REQUESTS_PER_MINUTE):
        api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
        if not api_key:
            raise ValueError(
                "API key not found. Set GEMINI_API_KEY (or GOOGLE_API_KEY) in .env"
            )

        self.client = genai.Client(api_key=api_key)
        self.model_name = MODEL_NAME
        self.limiter = RateLimiter(requests_per_minute)
        self.stats = {"calls": 0, "facts": 0, "rate_limited": 0, "failures": 0}

    def build_extraction_prompt(self, document_text: str, target_attributes: List[str]) -> str:
        attrs_str = ', '.join(target_attributes) if target_attributes else "Grit, Diameter, Material, Voltage, Size"

        return f"""You are an industrial data parsing engine. Extract technical specifications and product attributes from the text.

TARGET ATTRIBUTES PREFERRED:
{attrs_str}

RULES:
1. Extract values for the target attributes listed above whenever present or implied.
2. If primary target attributes are not present, extract any other clear technical attributes found in the text (e.g. Size, Material, Grit, Type, Brand, Quantity).
3. Do not return an empty list if any product specification or dimension can be parsed from the text.
4. Put the numeric magnitude in extracted_value and the unit in uom. Never repeat the unit inside extracted_value.

OUTPUT FORMAT: Return ONLY a valid JSON array of objects with these keys:
- attribute_name (string)
- extracted_value (string)
- uom (string, e.g. "IN", "MM", "V", or "")
- confidence (string: "High", "Medium", or "Low")
- source_context (string: short text snippet)
- page_number (string: "1")

PRODUCT TEXT:
---
{document_text[:15000]}
---

Return ONLY the raw JSON array:"""

    @staticmethod
    def _parse_response(raw_response: str) -> List[Dict[str, Any]]:
        """Strip markdown fencing and decode the model's JSON array."""
        text = raw_response.strip()
        if not text or text == "[]":
            return []

        if text.startswith("```"):
            parts = text.split("```")
            text = parts[1] if len(parts) > 1 else parts[0]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()

        facts = json.loads(text)
        if isinstance(facts, dict):
            facts = [facts]
        return [f for f in facts if isinstance(f, dict)]

    def process_document(
        self,
        product_id: str,
        document_text: str,
        target_attributes: List[str],
        source_document: str = "supplier_description",
    ) -> List[Dict[str, Any]]:
        """Extract facts for one product, pacing calls and backing off on 429s."""
        prompt = self.build_extraction_prompt(document_text, target_attributes)

        for attempt in range(MAX_RETRIES):
            self.limiter.wait()
            try:
                self.stats["calls"] += 1
                response = self.client.models.generate_content(
                    model=self.model_name,
                    contents=prompt,
                )
                facts = self._parse_response(response.text or "")

                for fact in facts:
                    fact['product_id'] = product_id
                    fact.setdefault('uom', '')
                    fact.setdefault('confidence', 'Low')
                    fact.setdefault('source_context', '')
                    fact.setdefault('page_number', '1')
                    fact['source_document'] = source_document

                self.stats["facts"] += len(facts)
                log.debug("%s: extracted %d facts", product_id, len(facts))
                return facts

            except json.JSONDecodeError as exc:
                log.warning("%s: unparseable JSON response (%s)", product_id, exc)
                self.stats["failures"] += 1
                return []

            except Exception as exc:  # noqa: BLE001 - SDK raises a wide range of errors
                error_str = str(exc)
                is_quota = "429" in error_str or "RESOURCE_EXHAUSTED" in error_str
                is_transient = is_quota or any(
                    code in error_str for code in ("503", "UNAVAILABLE", "500", "INTERNAL", "504")
                )

                if is_transient and attempt < MAX_RETRIES - 1:
                    if is_quota:
                        self.stats["rate_limited"] += 1
                        cooldown = _parse_retry_delay(error_str) or (
                            RATE_LIMIT_COOLDOWN_SECONDS * (attempt + 1)
                        )
                        log.warning(
                            "%s: quota exhausted, cooling down %.0fs (attempt %d/%d)",
                            product_id, cooldown, attempt + 1, MAX_RETRIES,
                        )
                        self.limiter.pause(cooldown)
                    else:
                        backoff = (2 ** attempt) + random.uniform(0.1, 0.5)
                        log.warning(
                            "%s: transient error, retrying in %.1fs (attempt %d/%d)",
                            product_id, backoff, attempt + 1, MAX_RETRIES,
                        )
                        time.sleep(backoff)
                    continue

                self.stats["failures"] += 1
                log.error("%s: extraction failed - %s", product_id, error_str[:200])
                return []

        self.stats["failures"] += 1
        log.error("%s: extraction failed after %d attempts", product_id, MAX_RETRIES)
        return []
