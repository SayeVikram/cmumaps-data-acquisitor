#!/usr/bin/env python3

from __future__ import annotations

import json
import subprocess
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from dotenv import load_dotenv  
except ImportError:  
    load_dotenv = None  

import requests

if load_dotenv:
    load_dotenv()


BUILDING_IDS: List[str] = [
    "618852",  # 5th Ave Commons
    "583845",  # Athletics
    "583852",  # CUC
    "583841",  # Computer Labs
    "583837",  # HBH
    "583840",  # MI
    "586114",  # BH-PH
    "586120",  # DH-WEH
    "586116",  # HH-SH
    "586124",  # POS-MM
    "583865",  # TEP
]

COOKIE_FILE = Path(__file__).with_name("wssessionid_cookie.json")
SEARCH_COOKIES_FILE = Path(__file__).with_name("cmu_25live_search_cookies_today.json")
PREFERRED_COOKIE_KEY = "athletics"
COOKIE_MAX_AGE_HOURS = 12
REQUEST_DELAY_SECONDS = 0.5
DEFAULT_OUTPUT_FILE = Path(__file__).with_name("thisWeeksEvents.json")


def get_current_week_dates() -> List[str]:
    """Return ISO dates for Monday-Sunday of the current week."""
    today = date.today()
    monday = today - timedelta(days=today.weekday())
    return [(monday + timedelta(days=i)).isoformat() for i in range(7)]


def _parse_datetime(value: str) -> Optional[datetime]:
    """Parse ISO 8601-ish string into datetime."""
    try:
        if value.endswith("Z"):
            value = value[:-1] + "+00:00"
        return datetime.fromisoformat(value)
    except Exception:
        return None


def _find_cookie_script() -> Path:
    """Locate the script that can extract WSSESSIONID."""
    candidates = [
        Path(__file__).with_name("cmu25live_extractor.py"),
        Path(__file__).with_name("cmu_25live_search_cookie_extractor.py"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        "Unable to locate a cookie extractor script. "
        "Expected one of: cmu25live_extractor.py, cmu_25live_search_cookie_extractor.py"
    )


def _load_cookie_from_disk() -> Optional[str]:
    if not COOKIE_FILE.exists():
        return None

    try:
        data = json.loads(COOKIE_FILE.read_text())
    except json.JSONDecodeError:
        return None

    extracted_at = data.get("extracted_at")
    cookie = data.get("WSSESSIONID")
    if not cookie or not extracted_at:
        return None

    extracted_dt = _parse_datetime(extracted_at)
    if not extracted_dt:
        return None

    hours_diff = (datetime.now() - extracted_dt).total_seconds() / 3600
    if hours_diff < COOKIE_MAX_AGE_HOURS:
        print("Using existing cookie from file")
        return cookie

    return None


def _run_cookie_script() -> None:
    script_path = _find_cookie_script()
    print(f"Extracting new cookie using Python script: {script_path.name}")
    result = subprocess.run(
        [sys.executable, str(script_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.stdout:
        print(result.stdout.strip())
    if result.stderr:
        print(result.stderr.strip(), file=sys.stderr)
    if result.returncode != 0:
        raise RuntimeError(
            f"Cookie extractor {script_path.name} failed with code {result.returncode}"
        )


def get_wssessionid() -> str:
    """Load a WSSESSIONID from disk or extract a new one."""
    cookie = _load_cookie_from_disk()
    if cookie:
        return cookie
    cookie = _load_cookie_from_search_results()
    if cookie:
        return cookie

    _run_cookie_script()
    cookie = _load_cookie_from_disk()
    if not cookie:
        raise RuntimeError("Cookie file was not created by the extractor script")
    return cookie


def _load_cookie_from_search_results() -> Optional[str]:
    """Use a cookie captured for another building (e.g., athletics) for all requests."""
    if not SEARCH_COOKIES_FILE.exists():
        return None

    try:
        data = json.loads(SEARCH_COOKIES_FILE.read_text())
    except json.JSONDecodeError:
        return None

    preferred_entry = data.get(PREFERRED_COOKIE_KEY)
    if not preferred_entry:
        return None

    raw_cookie_string = preferred_entry.get("cookies")
    if not raw_cookie_string:
        return None

    for cookie_part in raw_cookie_string.split(";"):
        cookie_part = cookie_part.strip()
        if cookie_part.upper().startswith("WSSESSIONID="):
            value = cookie_part.split("=", 1)[1]
            if value:
                print(
                    f"Using WSSESSIONID from {SEARCH_COOKIES_FILE.name} entry "
                    f"'{PREFERRED_COOKIE_KEY}'"
                )
                return value
    return None


def get_url(building_id: str, day: str) -> str:
    return (
        "https://25live.collegenet.com/25live/data/cmu/run/home/calendar/"
        "calendardata.json?mode=pro&obj_cache_accl=0&space_query_id="
        f"{building_id}&start_dt={day}&end_dt={day}&page=1&comptype=home"
        "&sort=evdates_event_name&compsubject=location&last_id=-1"
        "&caller=pro-CalendarService.getCalendarDayPage"
    )


def fetch_building_events(building_id: str, day: str, wssessionid: str) -> List[Dict[str, Any]]:
    """Fetch events for a given building on a specific day."""
    url = get_url(building_id, day)
    try:
        response = requests.get(
            url,
            headers={"Cookie": f"WSSESSIONID={wssessionid}"},
            timeout=30,
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        print(f"Error fetching events for building {building_id} on {day}: {exc}")
        return []

    try:
        data = response.json()
    except ValueError as exc:
        print(f"Invalid JSON for building {building_id} on {day}: {exc}")
        return []

    rsrv_entries = data.get("root", {}).get("events", [{}])[0].get("rsrv")
    if not rsrv_entries:
        return []

    events: List[Dict[str, Any]] = []
    for event in rsrv_entries:
        for location in event.get("subject", []):
            events.append(
                {
                    "name": str(event.get("event_name", "")),
                    "location": location.get("itemName"),
                    "startTime": event.get("rsrv_start_dt"),
                    "endTime": event.get("rsrv_end_dt"),
                    "date": day,
                    "buildingID": building_id,
                }
            )
    return events


def get_current_week_events() -> List[Dict[str, Any]]:
    print("Getting WSSESSIONID cookie...")
    wssessionid = get_wssessionid()
    print(f"Using cookie: {wssessionid[:20]}...")

    week_dates = get_current_week_dates()
    print(f"Current week dates: {', '.join(week_dates)}")
    print(f"Fetching events for {len(BUILDING_IDS)} buildings across {len(week_dates)} days...")

    all_events: List[Dict[str, Any]] = []
    for building_id in BUILDING_IDS:
        for day in week_dates:
            print(f"Fetching events for building {building_id} on {day}...")
            all_events.extend(fetch_building_events(building_id, day, wssessionid))
            time.sleep(REQUEST_DELAY_SECONDS)

    print(f"\nTotal events found: {len(all_events)}")
    return all_events


def save_events_to_file(events: List[Dict[str, Any]], filename: Path = DEFAULT_OUTPUT_FILE) -> None:
    if not events:
        print("No events to save.")
        return
    filename.write_text(json.dumps(events, indent=2))
    print(f"Events saved to {filename}")


def main() -> None:
    try:
        events = get_current_week_events()
        if events:
            save_events_to_file(events)
        else:
            print("No events found for the current week.")
    except Exception as exc:  # pragma: no cover - CLI entry point
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()