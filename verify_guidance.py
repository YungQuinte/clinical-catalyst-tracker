#!/usr/bin/env python3
"""
Automatic company-guidance verification layer.

Reads site/trials.json, maps trial sponsors to public companies when possible,
scans recent SEC filings plus optional company IR/news pages for clinical
readout-guidance language, and writes candidate matches back into trials.json.

This script intentionally DOES NOT auto-confirm a readout date. Keyword hits
are flags for human review. Confirmed timing still belongs in
manual_catalysts.csv.
"""

from __future__ import annotations

import csv
import difflib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
TRIALS_FILE = ROOT / "site" / "trials.json"
COMPANY_MAP_FILE = ROOT / "company_map.csv"

SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"
SEC_ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"

# Keep the scan focused on catalysts that are actually approaching.
SCAN_DAYS_BACK = 45
SCAN_DAYS_FORWARD = 180
SEC_LOOKBACK_DAYS = 210
MAX_SEC_COMPANIES_PER_RUN = 40
MAX_SEC_FILINGS_PER_COMPANY = 8
MAX_IR_LINKS_PER_COMPANY = 12
REQUEST_TIMEOUT = 35
MAX_RETRIES = 3

# SEC's published ceiling is 10 req/s. We intentionally stay far below it.
SEC_MIN_SECONDS_BETWEEN_REQUESTS = 0.22

SEC_FORMS = {
    "8-K", "8-K/A", "10-Q", "10-Q/A", "10-K", "10-K/A",
    "6-K", "6-K/A", "20-F", "20-F/A", "40-F", "40-F/A",
}

# The phrase itself plus nearby clinical vocabulary is required for generic
# phrases such as "results expected" to reduce financial-results false hits.
GUIDANCE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("topline data expected", re.compile(r"\btop[- ]?line\s+(?:clinical\s+)?data\s+(?:are\s+|is\s+)?expected\b", re.I)),
    ("topline results expected", re.compile(r"\btop[- ]?line\s+(?:clinical\s+)?results?\s+(?:are\s+|is\s+)?expected\b", re.I)),
    ("expects topline data/results", re.compile(r"\bexpects?\s+(?:to\s+)?(?:report|announce|present|release)\s+(?:[^.]{0,80}\s)?top[- ]?line\s+(?:data|results?)\b", re.I)),
    ("readout expected", re.compile(r"\b(?:data\s+)?read[- ]?out\s+(?:is\s+|are\s+)?expected\b", re.I)),
    ("expects a readout", re.compile(r"\bexpects?\s+(?:a\s+|the\s+)?(?:data\s+)?read[- ]?out\b", re.I)),
    ("data readout", re.compile(r"\b(?:clinical\s+)?data\s+read[- ]?out\b", re.I)),
    ("clinical data expected", re.compile(r"\b(?:clinical|interim|pivotal)\s+data\s+(?:are\s+|is\s+)?expected\b", re.I)),
    ("results expected", re.compile(r"\b(?:clinical|trial|study|pivotal|interim|top[- ]?line)?\s*results?\s+(?:are\s+|is\s+)?expected\b", re.I)),
    ("expects to report data/results", re.compile(r"\bexpects?\s+(?:to\s+)?(?:report|announce|present|release)\s+(?:[^.]{0,100}\s)?(?:clinical\s+)?(?:data|results?)\b", re.I)),
    ("data expected", re.compile(r"\b(?:clinical|trial|study|pivotal|interim|top[- ]?line)?\s*data\s+(?:are\s+|is\s+)?expected\b", re.I)),
]

CLINICAL_CONTEXT_RE = re.compile(
    r"\b(phase\s*[123]|clinical|trial|study|patient|endpoint|cohort|dose|dosing|enrollment|"
    r"randomi[sz]ed|placebo|treatment|efficacy|safety|pivotal|interim|top[- ]?line|read[- ]?out)\b",
    re.I,
)

TIMING_PATTERNS = [
    re.compile(r"\bQ[1-4]\s*(?:20\d{2})\b", re.I),
    re.compile(r"\b(?:first|second|third|fourth)\s+quarter\s+(?:of\s+)?20\d{2}\b", re.I),
    re.compile(r"\bH[12]\s*(?:20\d{2})\b", re.I),
    re.compile(r"\b(?:first|second)\s+half\s+(?:of\s+)?20\d{2}\b", re.I),
    re.compile(r"\b(?:early|mid|late)[- ]20\d{2}\b", re.I),
    re.compile(r"\b(?:by|before)\s+(?:year[- ]?end|the\s+end\s+of)\s+20\d{2}\b", re.I),
    re.compile(r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+20\d{2}\b", re.I),
    re.compile(r"\b20\d{2}\b"),
]

# Generic names that should not be treated as program-specific evidence.
GENERIC_INTERVENTION_TERMS = {
    "placebo", "drug", "treatment", "therapy", "standard of care", "soc",
    "saline", "vehicle", "control", "best supportive care", "bsc",
}

COMPANY_SUFFIXES = {
    "inc", "incorporated", "corp", "corporation", "company", "co", "ltd", "limited",
    "llc", "plc", "sa", "nv", "ag", "se", "holdings", "holding", "group",
}

IR_LINK_HINTS = (
    "press", "release", "news", "investor", "financial", "quarter", "results",
    "event", "presentation", "pipeline", "clinical", "update",
)


@dataclass
class PublicCompany:
    sponsor: str
    ticker: str = ""
    company: str = ""
    cik: str = ""
    exchange: str = ""
    ir_url: str = ""
    method: str = ""
    score: float = 0.0


class RateLimitedSession:
    def __init__(self, user_agent: str, min_interval: float = 0.0):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": user_agent,
            "Accept-Encoding": "gzip, deflate",
        })
        self.min_interval = min_interval
        self._last_request = 0.0

    def get(self, url: str, **kwargs: Any) -> requests.Response:
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)

        last_error: Exception | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                response = self.session.get(url, timeout=REQUEST_TIMEOUT, **kwargs)
                self._last_request = time.monotonic()
                response.raise_for_status()
                return response
            except requests.RequestException as exc:
                last_error = exc
                self._last_request = time.monotonic()
                if attempt == MAX_RETRIES:
                    break
                time.sleep(2 ** (attempt - 1))
        raise RuntimeError(f"GET failed after {MAX_RETRIES} attempts: {url}: {last_error}")


def normalize_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text or " ").strip()


def html_to_text(content: bytes | str) -> str:
    if isinstance(content, bytes):
        content = content.decode("utf-8", errors="ignore")
    soup = BeautifulSoup(content, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    return normalize_ws(soup.get_text(" ", strip=True))


def normalize_company_name(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower())
    tokens = [t for t in cleaned.split() if t and t not in COMPANY_SUFFIXES]
    return " ".join(tokens)


def load_company_overrides() -> dict[str, PublicCompany]:
    if not COMPANY_MAP_FILE.exists():
        return {}

    output: dict[str, PublicCompany] = {}
    with COMPANY_MAP_FILE.open("r", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            sponsor = (row.get("sponsor") or "").strip()
            if not sponsor:
                continue
            output[normalize_company_name(sponsor)] = PublicCompany(
                sponsor=sponsor,
                ticker=(row.get("ticker") or "").strip().upper(),
                company=(row.get("company") or "").strip(),
                cik=re.sub(r"\D", "", row.get("cik") or ""),
                ir_url=(row.get("ir_url") or "").strip(),
                method="company_map.csv",
                score=1.0,
            )
    return output


def sec_user_agent() -> str:
    return os.environ.get("SEC_USER_AGENT", "").strip()


def sec_enabled() -> bool:
    ua = sec_user_agent()
    return bool(ua and "@" in ua and "YOUR_" not in ua.upper())


def load_sec_companies(sec: RateLimitedSession) -> list[dict[str, Any]]:
    payload = sec.get(SEC_TICKERS_URL).json()

    if isinstance(payload, dict) and "fields" in payload and "data" in payload:
        fields = payload["fields"]
        rows = []
        for values in payload["data"]:
            row = dict(zip(fields, values))
            rows.append({
                "cik": str(row.get("cik") or row.get("cik_str") or ""),
                "name": str(row.get("name") or row.get("title") or ""),
                "ticker": str(row.get("ticker") or "").upper(),
                "exchange": str(row.get("exchange") or ""),
            })
        return rows

    # Compatibility with company_tickers.json-like dictionary structure.
    rows = []
    if isinstance(payload, dict):
        for value in payload.values():
            if not isinstance(value, dict):
                continue
            rows.append({
                "cik": str(value.get("cik_str") or value.get("cik") or ""),
                "name": str(value.get("title") or value.get("name") or ""),
                "ticker": str(value.get("ticker") or "").upper(),
                "exchange": str(value.get("exchange") or ""),
            })
    return rows


def build_sec_indexes(rows: list[dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    ticker_index: dict[str, dict[str, Any]] = {}
    normalized_names: dict[str, dict[str, Any]] = {}

    for row in rows:
        ticker = (row.get("ticker") or "").upper()
        if ticker:
            ticker_index[ticker] = row
        normalized = normalize_company_name(row.get("name") or "")
        if normalized:
            normalized_names[normalized] = row

    return ticker_index, normalized_names


def map_company(
    trial: dict[str, Any],
    overrides: dict[str, PublicCompany],
    ticker_index: dict[str, dict[str, Any]],
    sec_names: dict[str, dict[str, Any]],
) -> PublicCompany | None:
    sponsor = (trial.get("sponsor") or "").strip()
    sponsor_norm = normalize_company_name(sponsor)

    # 1) Explicit mapping wins.
    override = overrides.get(sponsor_norm)
    if override:
        candidate = PublicCompany(**override.__dict__)
        if candidate.ticker and candidate.ticker in ticker_index:
            sec_row = ticker_index[candidate.ticker]
            candidate.cik = candidate.cik or str(sec_row.get("cik") or "")
            candidate.company = candidate.company or str(sec_row.get("name") or "")
            candidate.exchange = str(sec_row.get("exchange") or "")
        return candidate

    # 2) If manual_catalysts.csv already supplied a ticker, trust that ticker
    #    and use SEC's association to add CIK/company metadata.
    ticker = (trial.get("ticker") or "").strip().upper()
    if ticker and ticker in ticker_index:
        row = ticker_index[ticker]
        return PublicCompany(
            sponsor=sponsor,
            ticker=ticker,
            company=(trial.get("company") or row.get("name") or "").strip(),
            cik=str(row.get("cik") or ""),
            exchange=str(row.get("exchange") or ""),
            method="manual ticker → SEC",
            score=1.0,
        )

    # 3) Exact normalized sponsor-name match.
    if sponsor_norm in sec_names:
        row = sec_names[sponsor_norm]
        return PublicCompany(
            sponsor=sponsor,
            ticker=str(row.get("ticker") or ""),
            company=str(row.get("name") or sponsor),
            cik=str(row.get("cik") or ""),
            exchange=str(row.get("exchange") or ""),
            method="SEC exact name",
            score=1.0,
        )

    # 4) Conservative fuzzy match. We intentionally require a very high score
    #    and meaningful token overlap to reduce bad public-company mappings.
    if not sponsor_norm or len(sponsor_norm) < 5:
        return None

    sponsor_tokens = set(sponsor_norm.split())
    best: tuple[float, str, dict[str, Any] | None] = (0.0, "", None)

    for sec_norm, row in sec_names.items():
        if not sec_norm:
            continue
        sec_tokens = set(sec_norm.split())
        if not sponsor_tokens.intersection(sec_tokens):
            continue
        score = difflib.SequenceMatcher(None, sponsor_norm, sec_norm).ratio()
        if score > best[0]:
            best = (score, sec_norm, row)

    score, _, row = best
    if row and score >= 0.93:
        return PublicCompany(
            sponsor=sponsor,
            ticker=str(row.get("ticker") or ""),
            company=str(row.get("name") or sponsor),
            cik=str(row.get("cik") or ""),
            exchange=str(row.get("exchange") or ""),
            method="SEC fuzzy name",
            score=round(score, 3),
        )

    return None


def timing_mentions(snippet: str) -> list[str]:
    found: list[str] = []
    for pattern in TIMING_PATTERNS:
        for match in pattern.finditer(snippet):
            value = normalize_ws(match.group(0))
            if value not in found:
                found.append(value)
    return found[:5]


def find_guidance_matches(text: str) -> list[dict[str, Any]]:
    text = normalize_ws(text)
    if not text:
        return []

    results: list[dict[str, Any]] = []
    seen: set[str] = set()

    for label, pattern in GUIDANCE_PATTERNS:
        for match in pattern.finditer(text):
            start = max(0, match.start() - 260)
            end = min(len(text), match.end() + 360)
            snippet = normalize_ws(text[start:end])

            # Generic phrases like "results expected" are only useful when the
            # surrounding paragraph is clearly clinical-development language.
            if label in {"results expected", "data expected", "expects to report data/results"}:
                if not CLINICAL_CONTEXT_RE.search(snippet):
                    continue

            signature = re.sub(r"\W+", " ", snippet.lower())[:220]
            if signature in seen:
                continue
            seen.add(signature)

            results.append({
                "phrase": label,
                "matched_text": normalize_ws(match.group(0)),
                "snippet": snippet,
                "timing_mentions": timing_mentions(snippet),
            })

    return results[:20]


def trial_terms(trial: dict[str, Any]) -> list[str]:
    terms: list[str] = []

    for item in trial.get("interventions") or []:
        name = normalize_ws(str(item.get("name") or ""))
        if not name or name.lower() in GENERIC_INTERVENTION_TERMS:
            continue
        if len(name) >= 3:
            terms.append(name)

        # Drug/program codes are often the most discriminating piece of text.
        for token in re.findall(r"\b[A-Za-z]{1,8}[-_]?[A-Za-z0-9]{1,10}\b", name):
            if len(token) >= 4 and token.lower() not in GENERIC_INTERVENTION_TERMS:
                terms.append(token)

    # Extract likely program/drug codes from title, e.g. ABC-123.
    title = str(trial.get("title") or "")
    terms.extend(re.findall(r"\b[A-Z]{2,8}[-_]?[0-9]{1,6}[A-Z0-9-]*\b", title))

    unique: list[str] = []
    for term in terms:
        if term.lower() not in {x.lower() for x in unique}:
            unique.append(term)
    return unique[:15]


def score_trial_relevance(match: dict[str, Any], trial: dict[str, Any]) -> tuple[str, list[str]]:
    snippet = (match.get("snippet") or "").lower()
    matched_terms = [term for term in trial_terms(trial) if term.lower() in snippet]
    if matched_terms:
        return "HIGH", matched_terms[:5]

    # Disease overlap is weaker evidence because a company can run several
    # programs in the same disease area.
    condition_terms: list[str] = []
    for condition in trial.get("conditions") or []:
        condition = normalize_ws(str(condition))
        if len(condition) >= 5 and condition.lower() in snippet:
            condition_terms.append(condition)
    if condition_terms:
        return "MEDIUM", condition_terms[:3]

    return "LOW", []


def recent_sec_filings(sec: RateLimitedSession, cik: str, today: date) -> tuple[list[dict[str, str]], dict[str, str]]:
    cik_digits = re.sub(r"\D", "", cik)
    if not cik_digits:
        return [], {}

    cik10 = cik_digits.zfill(10)
    payload = sec.get(SEC_SUBMISSIONS_URL.format(cik10=cik10)).json()
    recent = ((payload.get("filings") or {}).get("recent") or {})
    company_meta = {
        "website": str(payload.get("website") or "").strip(),
        "investor_website": str(payload.get("investorWebsite") or "").strip(),
    }

    forms = recent.get("form") or []
    accessions = recent.get("accessionNumber") or []
    filing_dates = recent.get("filingDate") or []
    primary_docs = recent.get("primaryDocument") or []

    cutoff = today - timedelta(days=SEC_LOOKBACK_DAYS)
    filings: list[dict[str, str]] = []

    for form, accession, filing_date, primary_doc in zip(forms, accessions, filing_dates, primary_docs):
        if form not in SEC_FORMS:
            continue
        try:
            filed = datetime.strptime(filing_date, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            continue
        if filed < cutoff:
            continue
        if not accession or not primary_doc:
            continue

        accession_nodash = str(accession).replace("-", "")
        url = f"{SEC_ARCHIVES_BASE}/{int(cik_digits)}/{accession_nodash}/{primary_doc}"
        filings.append({
            "form": str(form),
            "filed": str(filing_date),
            "accession": str(accession),
            "url": url,
        })

    filings.sort(key=lambda x: x["filed"], reverse=True)
    return filings[:MAX_SEC_FILINGS_PER_COMPANY], company_meta


def scan_sec_company(sec: RateLimitedSession, company: PublicCompany, today: date) -> tuple[list[dict[str, Any]], int, list[str], str]:
    matches: list[dict[str, Any]] = []
    checked = 0
    errors: list[str] = []

    try:
        filings, company_meta = recent_sec_filings(sec, company.cik, today)
    except Exception as exc:
        return [], 0, [f"SEC submissions {company.ticker or company.company}: {exc}"], ""

    discovered_ir = company_meta.get("investor_website") or company_meta.get("website") or ""

    for filing in filings:
        try:
            response = sec.get(filing["url"])
            text = html_to_text(response.content)
            checked += 1
            for raw in find_guidance_matches(text):
                matches.append({
                    **raw,
                    "source_type": "SEC",
                    "source_label": f"SEC {filing['form']}",
                    "form": filing["form"],
                    "source_date": filing["filed"],
                    "url": filing["url"],
                })
        except Exception as exc:
            errors.append(f"SEC filing {filing['url']}: {exc}")

    return matches, checked, errors, discovered_ir


def registrable_hint(hostname: str) -> str:
    parts = [p for p in hostname.lower().split(".") if p]
    return ".".join(parts[-2:]) if len(parts) >= 2 else hostname.lower()


def candidate_ir_links(base_url: str, html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    base = urlparse(base_url)
    base_domain = registrable_hint(base.hostname or "")
    scored: list[tuple[int, str]] = []
    seen: set[str] = set()

    for a in soup.find_all("a", href=True):
        href = urljoin(base_url, a.get("href"))
        parsed = urlparse(href)
        if parsed.scheme not in {"http", "https"}:
            continue
        if registrable_hint(parsed.hostname or "") != base_domain:
            continue
        if href in seen:
            continue
        seen.add(href)

        text = normalize_ws(a.get_text(" ", strip=True)).lower()
        haystack = f"{text} {parsed.path.lower()}"
        score = sum(1 for hint in IR_LINK_HINTS if hint in haystack)
        if score <= 0:
            continue
        if parsed.path.lower().endswith((".jpg", ".jpeg", ".png", ".gif", ".zip", ".mp4")):
            continue
        scored.append((score, href))

    scored.sort(key=lambda x: x[0], reverse=True)
    return [url for _, url in scored[:MAX_IR_LINKS_PER_COMPANY]]


def scan_ir_company(ir_url: str) -> tuple[list[dict[str, Any]], int, list[str]]:
    if not ir_url:
        return [], 0, []

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (compatible; ClinicalCatalystTracker/1.0; +https://github.com/)",
        "Accept": "text/html,application/xhtml+xml,application/pdf;q=0.8,*/*;q=0.5",
    })

    matches: list[dict[str, Any]] = []
    checked = 0
    errors: list[str] = []

    try:
        response = session.get(ir_url, timeout=REQUEST_TIMEOUT)
        response.raise_for_status()
        checked += 1
        html = response.text
        for raw in find_guidance_matches(html_to_text(html)):
            matches.append({
                **raw,
                "source_type": "IR",
                "source_label": "Company IR page",
                "form": "",
                "source_date": "",
                "url": ir_url,
            })
        links = candidate_ir_links(ir_url, html)
    except Exception as exc:
        return [], checked, [f"IR landing page {ir_url}: {exc}"]

    for url in links:
        try:
            r = session.get(url, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            content_type = (r.headers.get("content-type") or "").lower()
            if "pdf" in content_type or url.lower().endswith(".pdf"):
                # No OCR/PDF dependency in this lightweight layer. Link is
                # still discoverable manually from the IR page.
                continue
            checked += 1
            for raw in find_guidance_matches(html_to_text(r.content)):
                matches.append({
                    **raw,
                    "source_type": "IR",
                    "source_label": "Company release / IR",
                    "form": "",
                    "source_date": "",
                    "url": url,
                })
        except Exception as exc:
            errors.append(f"IR page {url}: {exc}")

    # Deduplicate same snippet/source combination.
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for m in matches:
        key = (m.get("url", ""), re.sub(r"\W+", " ", m.get("snippet", "").lower())[:180])
        if key in seen:
            continue
        seen.add(key)
        unique.append(m)

    return unique[:40], checked, errors


def within_scan_window(trial: dict[str, Any]) -> bool:
    try:
        d = int(trial.get("days_to_primary_completion"))
    except (TypeError, ValueError):
        return False
    return -SCAN_DAYS_BACK <= d <= SCAN_DAYS_FORWARD


def main() -> int:
    if not TRIALS_FILE.exists():
        raise FileNotFoundError(f"Run update_trials.py first; missing {TRIALS_FILE}")

    with TRIALS_FILE.open("r", encoding="utf-8") as f:
        payload = json.load(f)

    trials = payload.get("trials") or []
    if not isinstance(trials, list):
        raise ValueError("site/trials.json does not contain a trials list")

    today = datetime.now(timezone.utc).date()
    overrides = load_company_overrides()

    ua = sec_user_agent()
    use_sec = sec_enabled()
    sec_rows: list[dict[str, Any]] = []
    ticker_index: dict[str, dict[str, Any]] = {}
    sec_names: dict[str, dict[str, Any]] = {}
    sec: RateLimitedSession | None = None
    global_errors: list[str] = []

    if use_sec:
        sec = RateLimitedSession(ua, min_interval=SEC_MIN_SECONDS_BETWEEN_REQUESTS)
        try:
            sec_rows = load_sec_companies(sec)
            ticker_index, sec_names = build_sec_indexes(sec_rows)
            print(f"Loaded {len(sec_rows)} SEC ticker/company associations.")
        except Exception as exc:
            global_errors.append(f"SEC ticker mapping failed: {exc}")
            use_sec = False
            sec = None
    else:
        print(
            "SEC scanning is disabled because SEC_USER_AGENT is not configured. "
            "Set a GitHub Actions repository variable named SEC_USER_AGENT, e.g. "
            "'ClinicalCatalystTracker your-email@example.com'."
        )

    # Initialize fields for every row so the frontend has a stable schema.
    for trial in trials:
        trial["auto_company"] = None
        trial["guidance_candidates"] = []
        trial["guidance_candidate_count"] = 0
        trial["needs_guidance_review"] = False
        trial["guidance_scan_status"] = "outside_scan_window" if not within_scan_window(trial) else "pending"

    scan_trials = [t for t in trials if within_scan_window(t)]
    scan_trials.sort(key=lambda t: abs(int(t.get("days_to_primary_completion") or 99999)))

    # Map companies first, then cap the number of unique SEC companies scanned.
    mapped: list[tuple[dict[str, Any], PublicCompany]] = []
    for trial in scan_trials:
        company = map_company(trial, overrides, ticker_index, sec_names)
        if company:
            trial["auto_company"] = {
                "ticker": company.ticker,
                "company": company.company,
                "cik": company.cik,
                "exchange": company.exchange,
                "ir_url": company.ir_url,
                "method": company.method,
                "score": company.score,
            }
            mapped.append((trial, company))
        else:
            trial["guidance_scan_status"] = "company_not_mapped"

    # Company-level results are cached in memory so several trials from the
    # same sponsor do not cause duplicate network requests.
    sec_company_cache: dict[str, tuple[list[dict[str, Any]], int, list[str], str]] = {}
    ir_company_cache: dict[str, tuple[list[dict[str, Any]], int, list[str]]] = {}
    sec_companies_scanned = 0
    sources_checked = 0
    scan_errors: list[str] = []

    # Determine which CIKs are eligible for SEC scan, nearest catalysts first.
    sec_ciks_allowed: set[str] = set()
    if use_sec:
        for _, company in mapped:
            cik = re.sub(r"\D", "", company.cik)
            if not cik or cik in sec_ciks_allowed:
                continue
            if len(sec_ciks_allowed) >= MAX_SEC_COMPANIES_PER_RUN:
                break
            sec_ciks_allowed.add(cik)

    for trial, company in mapped:
        raw_matches: list[dict[str, Any]] = []

        cik = re.sub(r"\D", "", company.cik)
        if use_sec and sec and cik and cik in sec_ciks_allowed:
            if cik not in sec_company_cache:
                result = scan_sec_company(sec, company, today)
                sec_company_cache[cik] = result
                sec_companies_scanned += 1
            company_matches, checked, errors, sec_discovered_ir = sec_company_cache[cik]
            raw_matches.extend(company_matches)
        else:
            sec_discovered_ir = ""

        # Prefer an explicit company_map.csv IR URL. If none is supplied, use
        # investorWebsite/website metadata from the SEC submissions record when available.
        ir_url = company.ir_url or sec_discovered_ir
        if ir_url:
            if trial.get("auto_company") is not None:
                trial["auto_company"]["ir_url"] = ir_url
                trial["auto_company"]["ir_url_source"] = (
                    "company_map.csv" if company.ir_url else "SEC submissions metadata"
                )
            ir_key = ir_url
            if ir_key not in ir_company_cache:
                ir_company_cache[ir_key] = scan_ir_company(ir_key)
            ir_matches, _, _ = ir_company_cache[ir_key]
            raw_matches.extend(ir_matches)

        enriched: list[dict[str, Any]] = []
        for match in raw_matches:
            relevance, matched_terms = score_trial_relevance(match, trial)
            enriched.append({
                **match,
                "relevance": relevance,
                "matched_trial_terms": matched_terms,
            })

        # Rank program-specific hits first, then newest SEC dates.
        rank = {"HIGH": 0, "MEDIUM": 1, "LOW": 2}
        enriched.sort(
            key=lambda m: (
                rank.get(m.get("relevance", "LOW"), 9),
                -(int((m.get("source_date") or "0000-00-00").replace("-", "") or 0)),
            )
        )

        # Keep the dashboard compact. Low-relevance company-level hits are still
        # useful for review but cap them aggressively.
        high_med = [m for m in enriched if m.get("relevance") in {"HIGH", "MEDIUM"}]
        low = [m for m in enriched if m.get("relevance") == "LOW"]
        final_matches = (high_med[:8] + low[:3])[:10]

        trial["guidance_candidates"] = final_matches
        trial["guidance_candidate_count"] = len(final_matches)
        trial["needs_guidance_review"] = bool(final_matches) and not bool(trial.get("verified_readout"))
        if final_matches:
            trial["guidance_scan_status"] = "matches_found"
        elif company.cik or company.ir_url:
            trial["guidance_scan_status"] = "scanned_no_match"
        else:
            trial["guidance_scan_status"] = "mapped_but_no_source"

    # Aggregate request counts/errors from cached company scans.
    for _, checked, errors, _ in sec_company_cache.values():
        sources_checked += checked
        scan_errors.extend(errors)
    for _, checked, errors in ir_company_cache.values():
        sources_checked += checked
        scan_errors.extend(errors)

    matches_total = sum(int(t.get("guidance_candidate_count") or 0) for t in trials)
    review_trials = sum(1 for t in trials if t.get("needs_guidance_review"))

    meta = payload.setdefault("meta", {})
    meta["guidance_scan"] = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "sec_enabled": use_sec,
        "sec_user_agent_configured": sec_enabled(),
        "sec_company_association_count": len(sec_rows),
        "scan_window_days_back": SCAN_DAYS_BACK,
        "scan_window_days_forward": SCAN_DAYS_FORWARD,
        "trials_in_guidance_window": len(scan_trials),
        "trials_mapped_to_public_company": len(mapped),
        "sec_companies_scanned": sec_companies_scanned,
        "ir_sites_scanned": len(ir_company_cache),
        "source_documents_checked": sources_checked,
        "candidate_match_count": matches_total,
        "trials_needing_human_review": review_trials,
        "errors": (global_errors + scan_errors)[:30],
        "note": (
            "Guidance hits are automatic keyword candidates, not confirmed readout dates. "
            "Open the source, verify the program/timing, then add confirmed guidance to manual_catalysts.csv."
        ),
    }

    tmp = TRIALS_FILE.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    tmp.replace(TRIALS_FILE)

    print(
        f"Guidance scan complete: {len(mapped)} mapped trials, "
        f"{review_trials} trials need review, {matches_total} candidate matches."
    )
    if scan_errors:
        print(f"Completed with {len(scan_errors)} non-fatal source errors.")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
