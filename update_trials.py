#!/usr/bin/env python3
"""
Clinical Catalyst Tracker
-------------------------
Fetches industry-sponsored Phase 2 / Phase 3 interventional trials from
ClinicalTrials.gov whose primary completion date is near the current date.

It then merges optional human-verified catalyst information from
manual_catalysts.csv and writes site/trials.json for the dashboard.

No API key is required.
"""

from __future__ import annotations

import calendar
import csv
import json
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests


API_URL = "https://clinicaltrials.gov/api/v2/studies"
ROOT = Path(__file__).resolve().parent
MANUAL_FILE = ROOT / "manual_catalysts.csv"
OUTPUT_FILE = ROOT / "site" / "trials.json"

# How far around today to look for primary-completion dates.
# We include some recently completed studies because topline results often
# arrive after primary completion.
DAYS_BACK = 60
DAYS_FORWARD = 240

# Avoid trials that are clearly dead. Completed studies are included because
# results may still be upcoming.
ALLOWED_STATUSES = {
    "NOT_YET_RECRUITING",
    "RECRUITING",
    "ENROLLING_BY_INVITATION",
    "ACTIVE_NOT_RECRUITING",
    "COMPLETED",
}

REQUEST_TIMEOUT = 60
MAX_RETRIES = 4


@dataclass(frozen=True)
class DateInterval:
    start: date
    end: date
    precision: str


def first_day_of_month(d: date) -> date:
    return d.replace(day=1)


def last_day_of_month(d: date) -> date:
    return d.replace(day=calendar.monthrange(d.year, d.month)[1])


def parse_partial_date(value: str | None) -> DateInterval | None:
    """
    ClinicalTrials.gov dates can be YYYY-MM-DD, YYYY-MM or occasionally YYYY.
    Convert each form into an interval so month/year precision isn't treated
    as a falsely exact day.
    """
    if not value:
        return None

    value = value.strip()

    try:
        if len(value) >= 10:
            d = datetime.strptime(value[:10], "%Y-%m-%d").date()
            return DateInterval(d, d, "DAY")

        if len(value) == 7:
            year, month = map(int, value.split("-"))
            start = date(year, month, 1)
            end = date(year, month, calendar.monthrange(year, month)[1])
            return DateInterval(start, end, "MONTH")

        if len(value) == 4:
            year = int(value)
            return DateInterval(date(year, 1, 1), date(year, 12, 31), "YEAR")
    except (ValueError, TypeError):
        return None

    return None


def overlaps_window(interval: DateInterval, start: date, end: date) -> bool:
    return interval.end >= start and interval.start <= end


def days_until_interval(interval: DateInterval, today: date) -> int:
    """
    Use the midpoint for month/year precision so dashboard sorting doesn't
    pretend a month-only date occurs on the first of that month.
    """
    midpoint = interval.start + (interval.end - interval.start) / 2
    return (midpoint - today).days


def get_json_with_retries(session: requests.Session, params: dict[str, Any]) -> dict[str, Any]:
    last_error: Exception | None = None

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = session.get(API_URL, params=params, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as exc:
            last_error = exc
            if attempt == MAX_RETRIES:
                break
            wait = 2 ** (attempt - 1)
            print(f"Request failed (attempt {attempt}/{MAX_RETRIES}); retrying in {wait}s: {exc}")
            time.sleep(wait)

    raise RuntimeError(f"ClinicalTrials.gov request failed after {MAX_RETRIES} attempts: {last_error}")


def fetch_trials(start_window: date, end_window: date) -> tuple[list[dict[str, Any]], int | None]:
    """
    Fetch Phase 2 / Phase 3 industry-sponsored interventional studies.

    Important: some ClinicalTrials.gov dates have only month precision. The
    API's date RANGE filtering can effectively place month-only values on the
    first day of the month. To avoid losing those records, the server-side
    search starts on the first day of start_window's month and ends on the
    last day of end_window's month. We then filter precisely in Python.
    """
    api_start = first_day_of_month(start_window)
    api_end = last_day_of_month(end_window)

    advanced_filter = (
        "AREA[StudyType]INTERVENTIONAL AND "
        "(AREA[Phase]PHASE2 OR AREA[Phase]PHASE3) AND "
        "AREA[LeadSponsorClass]INDUSTRY AND "
        f"AREA[PrimaryCompletionDate]RANGE[{api_start.isoformat()},{api_end.isoformat()}]"
    )

    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": "clinical-catalyst-tracker/1.0 (GitHub Actions; public ClinicalTrials.gov data)"
        }
    )

    all_studies: list[dict[str, Any]] = []
    page_token: str | None = None
    total_count: int | None = None

    while True:
        params: dict[str, Any] = {
            "format": "json",
            "pageSize": 1000,
            "countTotal": "true",
            "filter.advanced": advanced_filter,
            "filter.overallStatus": ",".join(sorted(ALLOWED_STATUSES)),
        }

        if page_token:
            params["pageToken"] = page_token

        payload = get_json_with_retries(session, params)

        if total_count is None:
            total_count = payload.get("totalCount")

        page = payload.get("studies", [])
        all_studies.extend(page)

        page_token = payload.get("nextPageToken")
        print(
            f"Fetched {len(page)} studies "
            f"({len(all_studies)} total so far"
            + (f" / API total {total_count}" if total_count is not None else "")
            + ")."
        )

        if not page_token:
            break

    return all_studies, total_count


def load_manual_catalysts() -> dict[str, dict[str, str]]:
    if not MANUAL_FILE.exists():
        return {}

    manual: dict[str, dict[str, str]] = {}

    with MANUAL_FILE.open("r", newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)

        required = {"nct"}
        missing = required - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"manual_catalysts.csv is missing required column(s): {', '.join(sorted(missing))}")

        for raw in reader:
            nct = (raw.get("nct") or "").strip().upper()
            if not nct:
                continue

            cleaned = {key: (value or "").strip() for key, value in raw.items()}
            cleaned["nct"] = nct
            manual[nct] = cleaned

    return manual


def get_nested(obj: dict[str, Any], *keys: str, default: Any = None) -> Any:
    current: Any = obj
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def clean_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    return [str(v).strip() for v in values if str(v).strip()]


def extract_trial(
    study: dict[str, Any],
    today: date,
    start_window: date,
    end_window: date,
    manual: dict[str, dict[str, str]],
) -> dict[str, Any] | None:
    protocol = study.get("protocolSection", {})

    ident = protocol.get("identificationModule", {})
    status_mod = protocol.get("statusModule", {})
    design = protocol.get("designModule", {})
    sponsor_mod = protocol.get("sponsorCollaboratorsModule", {})
    cond_mod = protocol.get("conditionsModule", {})
    arms_mod = protocol.get("armsInterventionsModule", {})
    outcomes_mod = protocol.get("outcomesModule", {})

    nct = str(ident.get("nctId") or "").strip().upper()
    if not nct:
        return None

    status = str(status_mod.get("overallStatus") or "")
    if status and status not in ALLOWED_STATUSES:
        return None

    completion = status_mod.get("primaryCompletionDateStruct") or {}
    completion_value = str(completion.get("date") or "").strip()
    interval = parse_partial_date(completion_value)

    if not interval or not overlaps_window(interval, start_window, end_window):
        return None

    lead_sponsor = sponsor_mod.get("leadSponsor") or {}
    interventions = arms_mod.get("interventions") or []

    intervention_rows = []
    for item in interventions:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        intervention_rows.append(
            {
                "name": name,
                "type": str(item.get("type") or "").strip(),
                "description": str(item.get("description") or "").strip(),
            }
        )

    primary_outcomes = []
    for outcome in outcomes_mod.get("primaryOutcomes") or []:
        if not isinstance(outcome, dict):
            continue
        primary_outcomes.append(
            {
                "measure": str(outcome.get("measure") or "").strip(),
                "description": str(outcome.get("description") or "").strip(),
                "timeframe": str(outcome.get("timeFrame") or "").strip(),
            }
        )

    design_info = design.get("designInfo") or {}
    masking_info = design_info.get("maskingInfo") or {}
    enrollment = design.get("enrollmentInfo") or {}

    override = manual.get(nct, {})

    return {
        "nct": nct,
        "title": str(ident.get("briefTitle") or "").strip(),
        "official_title": str(ident.get("officialTitle") or "").strip(),
        "sponsor": str(lead_sponsor.get("name") or "").strip(),
        "sponsor_class": str(lead_sponsor.get("class") or "").strip(),
        "phase": clean_list(design.get("phases")),
        "status": status,
        "enrollment": enrollment.get("count"),
        "enrollment_type": str(enrollment.get("type") or "").strip(),
        "conditions": clean_list(cond_mod.get("conditions")),
        "interventions": intervention_rows,
        "primary_outcomes": primary_outcomes,
        "allocation": str(design_info.get("allocation") or "").strip(),
        "intervention_model": str(design_info.get("interventionModel") or "").strip(),
        "primary_purpose": str(design_info.get("primaryPurpose") or "").strip(),
        "masking": str(masking_info.get("masking") or "").strip(),
        "primary_completion": completion_value,
        "primary_completion_type": str(completion.get("type") or "").strip(),
        "primary_completion_precision": interval.precision,
        "primary_completion_start": interval.start.isoformat(),
        "primary_completion_end": interval.end.isoformat(),
        "days_to_primary_completion": days_until_interval(interval, today),
        "last_update_posted": str(
            get_nested(status_mod, "studyLastUpdatePostDateStruct", "date", default="") or ""
        ),
        "has_results": bool(study.get("hasResults", False)),
        "url": f"https://clinicaltrials.gov/study/{nct}",
        # Human-verified / manually maintained fields:
        "ticker": override.get("ticker", ""),
        "company": override.get("company", ""),
        "expected_readout": override.get("expected_readout", ""),
        "expected_readout_date": override.get("expected_readout_date", ""),
        "readout_confidence": override.get("readout_confidence", ""),
        "source_url": override.get("source_url", ""),
        "notes": override.get("notes", ""),
        "verified_readout": bool(override.get("expected_readout", "")),
    }


def main() -> int:
    today = datetime.now(timezone.utc).date()
    start_window = today - timedelta(days=DAYS_BACK)
    end_window = today + timedelta(days=DAYS_FORWARD)

    print(f"Searching ClinicalTrials.gov for primary completion from {start_window} through {end_window}.")

    manual = load_manual_catalysts()
    studies, api_total = fetch_trials(start_window, end_window)

    trials: list[dict[str, Any]] = []
    discovered_ids: set[str] = set()

    for study in studies:
        trial = extract_trial(study, today, start_window, end_window, manual)
        if trial:
            trials.append(trial)
            discovered_ids.add(trial["nct"])

    # Sort verified manual readouts by their optional ISO sort date first;
    # otherwise fall back to primary completion.
    def sort_key(row: dict[str, Any]) -> tuple[str, str, str]:
        manual_date = row.get("expected_readout_date") or ""
        if manual_date:
            return ("0", manual_date, row["nct"])
        return ("1", row.get("primary_completion_start") or "9999-12-31", row["nct"])

    trials.sort(key=sort_key)

    unmatched_manual = sorted(set(manual) - discovered_ids)
    if unmatched_manual:
        print(
            "NOTE: These manually entered NCT IDs are outside the current automatic window "
            "or did not match the automatic filters: " + ", ".join(unmatched_manual)
        )

    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    payload = {
        "meta": {
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "today_utc": today.isoformat(),
            "source": "ClinicalTrials.gov API v2",
            "automatic_window_start": start_window.isoformat(),
            "automatic_window_end": end_window.isoformat(),
            "api_total_before_local_date_filter": api_total,
            "visible_trial_count": len(trials),
            "manual_entry_count": len(manual),
            "unmatched_manual_ncts": unmatched_manual,
            "note": (
                "Primary completion is not the same as a public topline readout date. "
                "Use manual_catalysts.csv to add company-guided readout timing."
            ),
        },
        "trials": trials,
    }

    tmp_file = OUTPUT_FILE.with_suffix(".json.tmp")
    with tmp_file.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)

    tmp_file.replace(OUTPUT_FILE)

    print(f"Wrote {len(trials)} trials to {OUTPUT_FILE}.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
