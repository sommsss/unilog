"""Stage 3: autonomous retrieval of manufacturer documentation.

Finds candidate datasheet/manual URLs for a product, filters out marketplaces,
downloads what it finds into a per-product folder, and extracts plain text so
Stage 4 has something authoritative to read instead of the supplier blurb.
"""

import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from urllib.parse import unquote, urlparse

import requests
from bs4 import BeautifulSoup

from src.config import (
    DOCUMENT_DIR,
    FETCH_DELAY_SECONDS,
    FETCH_TIMEOUT_SECONDS,
    MAX_DOCS_PER_PRODUCT,
)
from src.logging_setup import get_logger

log = get_logger("fetcher")

SEARCH_ENDPOINT = "https://html.duckduckgo.com/html/"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


class AutonomousFetcher:
    def __init__(
        self,
        output_doc_dir: str = DOCUMENT_DIR,
        delay_seconds: float = FETCH_DELAY_SECONDS,
        max_docs_per_product: int = MAX_DOCS_PER_PRODUCT,
    ):
        self.output_doc_dir = output_doc_dir
        self.delay_seconds = delay_seconds
        self.max_docs_per_product = max_docs_per_product
        self._last_request = 0.0
        os.makedirs(self.output_doc_dir, exist_ok=True)

        # Domains explicitly forbidden (marketplaces, non-authoritative sources)
        self.blocked_domains = {
            'amazon.com', 'ebay.com', 'walmart.com', 'grainger.com',
            'mcmaster.com', 'homedepot.com', 'lowes.com', 'alibaba.com',
            'aliexpress.com', 'zoro.com', 'fastenal.com', 'pinterest.com',
            'facebook.com', 'youtube.com', 'reddit.com', 'wikipedia.org',
            'duckduckgo.com', 'google.com', 'bing.com', 'temu.com', 'etsy.com',
        }

        # User-agent header to avoid standard bot blocks on official sites
        self.headers = {
            'User-Agent': (
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                '(KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36'
            )
        }

    # --- politeness -------------------------------------------------------
    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.delay_seconds:
            time.sleep(self.delay_seconds - elapsed)
        self._last_request = time.monotonic()

    # --- domain policy ----------------------------------------------------
    def is_allowed_domain(self, url: str) -> bool:
        """Verify that the URL is not on the marketplace blocklist."""
        try:
            parsed = urlparse(url)
            if parsed.scheme not in ('http', 'https'):
                return False
            domain = parsed.netloc.lower()
            return not any(blocked in domain for blocked in self.blocked_domains)
        except Exception:
            return False

    # --- discovery --------------------------------------------------------
    def build_search_queries(self, record: Dict[str, Any]) -> List[str]:
        """Construct ranked search queries for official manufacturer documentation."""
        mpn = str(record.get('Mfg_Part_Num', '') or '').strip()
        mfr = str(record.get('Resolved_MFR', '') or '').strip()
        brand = str(record.get('Resolved_Brand', '') or '').strip()

        queries: List[str] = []
        if not mpn:
            return queries

        if mfr and mfr != 'UNKNOWN':
            queries.append(f'"{mpn}" "{mfr}" datasheet pdf')
            queries.append(f'"{mpn}" "{mfr}" specification sheet')
        if brand and brand != 'UNKNOWN' and brand != mfr:
            queries.append(f'"{mpn}" "{brand}" manual pdf')

        # Relaxed fallbacks: doubly-quoted queries often return nothing for
        # long industrial part numbers, so always keep a looser query in reserve.
        queries.append(f'"{mpn}" datasheet')
        queries.append(f'{mpn} {brand or mfr} specifications'.strip())

        return queries

    def search_candidates(self, query: str, limit: int = 5) -> List[str]:
        """Run one web search and return allowed result URLs, best first.

        Uses DuckDuckGo's no-API HTML endpoint so the pipeline needs no extra key.
        Returns an empty list (rather than raising) whenever search is unavailable.
        """
        self._throttle()
        try:
            response = requests.post(
                SEARCH_ENDPOINT,
                data={'q': query},
                headers=self.headers,
                timeout=FETCH_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
        except Exception as exc:  # noqa: BLE001 - search is best-effort
            log.warning("Search failed for %r: %s", query, str(exc)[:120])
            return []

        soup = BeautifulSoup(response.text, 'html.parser')
        urls: List[str] = []
        for anchor in soup.select('a.result__a, a.result__url'):
            href = anchor.get('href', '')
            href = self._unwrap_redirect(href)
            if href and self.is_allowed_domain(href) and href not in urls:
                urls.append(href)
            if len(urls) >= limit:
                break

        log.debug("Search %r -> %d allowed candidates", query, len(urls))
        return urls

    @staticmethod
    def _unwrap_redirect(href: str) -> str:
        """DuckDuckGo wraps results in /l/?uddg=<encoded-url>; unwrap to the target."""
        if not href:
            return ""
        if href.startswith('//'):
            href = 'https:' + href
        match = re.search(r'[?&]uddg=([^&]+)', href)
        if match:
            return unquote(match.group(1))
        return href

    def discover_documents(self, record: Dict[str, Any], per_query: int = 5) -> List[str]:
        """Search every ranked query for a product and return deduped candidate URLs."""
        candidates: List[str] = []
        for query in self.build_search_queries(record):
            for url in self.search_candidates(query, limit=per_query):
                if url not in candidates:
                    candidates.append(url)
            if len(candidates) >= self.max_docs_per_product * 3:
                break

        ranked = self.rank_by_authority(candidates, record)
        return ranked[: self.max_docs_per_product]

    @staticmethod
    def rank_by_authority(urls: List[str], record: Dict[str, Any]) -> List[str]:
        """Prefer the manufacturer's own domain, then PDFs, over third-party listings."""
        tokens = {
            re.sub(r'[^a-z0-9]', '', str(record.get(field, '') or '').lower().split(' ')[0])
            for field in ('Resolved_Brand', 'Resolved_MFR')
        }
        tokens = {t for t in tokens if len(t) >= 3}

        def score(url: str) -> tuple:
            domain = urlparse(url).netloc.lower()
            on_manufacturer_domain = any(token in domain for token in tokens)
            is_pdf = url.lower().endswith('.pdf')
            # Sort ascending, so negate the qualities we want first.
            return (not on_manufacturer_domain, not is_pdf, len(url))

        return sorted(urls, key=score)

    # --- retrieval --------------------------------------------------------
    def download_document(self, product_id: str, url: str, doc_type: str = "spec_sheet") -> Optional[Dict[str, Any]]:
        """Download a document (PDF/HTML) and store it locally with metadata."""
        if not self.is_allowed_domain(url):
            return None

        product_folder = os.path.join(self.output_doc_dir, str(product_id))
        os.makedirs(product_folder, exist_ok=True)

        self._throttle()
        try:
            response = requests.get(
                url, headers=self.headers, timeout=FETCH_TIMEOUT_SECONDS, stream=True
            )
            response.raise_for_status()

            content_type = response.headers.get('Content-Type', '').lower()
            ext = ".pdf" if "pdf" in content_type or url.lower().endswith(".pdf") else ".html"

            filename = f"{doc_type}_{abs(hash(url)) % (10 ** 8)}{ext}"
            filepath = os.path.join(product_folder, filename)

            with open(filepath, 'wb') as fh:
                for chunk in response.iter_content(chunk_size=8192):
                    fh.write(chunk)

            log.info("%s: retrieved %s", product_id, url)
            return {
                'product_id': product_id,
                'source_url': url,
                'local_filepath': filepath,
                'document_type': doc_type,
                'retrieval_status': 'success',
                'timestamp': _utcnow(),
            }
        except Exception as exc:  # noqa: BLE001 - any network failure is logged, not fatal
            log.warning("%s: failed %s (%s)", product_id, url, str(exc)[:120])
            return {
                'product_id': product_id,
                'source_url': url,
                'local_filepath': '',
                'document_type': doc_type,
                'retrieval_status': f'failed: {str(exc)[:120]}',
                'timestamp': _utcnow(),
            }

    def fetch_product_sources(
        self,
        product_id: str,
        record: Dict[str, Any],
        candidate_urls: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """Retrieve documents for a product and return the evidence log entries.

        When no candidate URLs are supplied, discovery runs a web search first.
        """
        if candidate_urls is None:
            candidate_urls = self.discover_documents(record)

        logs: List[Dict[str, Any]] = []
        for url in candidate_urls:
            if self.is_allowed_domain(url):
                entry = self.download_document(product_id, url)
                if entry:
                    logs.append(entry)
            else:
                logs.append({
                    'product_id': product_id,
                    'source_url': url,
                    'local_filepath': '',
                    'document_type': 'blocked',
                    'retrieval_status': 'blocked: marketplace domain',
                    'timestamp': _utcnow(),
                })
        return logs

    # --- text extraction --------------------------------------------------
    @staticmethod
    def extract_text(filepath: str, max_chars: int = 15000) -> str:
        """Read a retrieved PDF/HTML file into plain text for Stage 4."""
        if not filepath or not os.path.exists(filepath):
            return ""

        if filepath.lower().endswith('.pdf'):
            try:
                from pypdf import PdfReader
            except ImportError:
                log.warning("pypdf not installed - cannot read %s", filepath)
                return ""
            try:
                reader = PdfReader(filepath)
                pages = [page.extract_text() or "" for page in reader.pages[:20]]
                return "\n".join(pages)[:max_chars]
            except Exception as exc:  # noqa: BLE001
                log.warning("Unreadable PDF %s: %s", filepath, str(exc)[:120])
                return ""

        try:
            with open(filepath, 'r', encoding='utf-8', errors='replace') as fh:
                soup = BeautifulSoup(fh.read(), 'html.parser')
            for tag in soup(['script', 'style', 'nav', 'footer', 'header']):
                tag.decompose()
            text = re.sub(r'\n{3,}', '\n\n', soup.get_text('\n', strip=True))
            return text[:max_chars]
        except Exception as exc:  # noqa: BLE001
            log.warning("Unreadable HTML %s: %s", filepath, str(exc)[:120])
            return ""

    def build_document_text(self, retrieval_logs: List[Dict[str, Any]], max_chars: int = 15000) -> str:
        """Concatenate the text of every successfully retrieved document."""
        chunks = []
        for entry in retrieval_logs:
            if entry.get('retrieval_status') == 'success':
                text = self.extract_text(entry['local_filepath'], max_chars)
                if text:
                    chunks.append(f"[SOURCE: {entry['source_url']}]\n{text}")
        return "\n\n".join(chunks)[:max_chars]
