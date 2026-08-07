#!/usr/bin/env python3
"""MCACC Priority Placement Portal importer for the TCDP foster portal.

Scrapes the public Priority Placement Portal at
https://apps.pets.maricopa.gov/Priority/ (Maricopa County Animal Care
and Control). No API key needed: the portal is server-rendered and the
grid loads from a plain GET endpoint that returns an HTML fragment.

Usage:
    python mcacc_priority.py                  # grid only -> data/priority-dogs.json
    python mcacc_priority.py --details        # also fetch each dog's detail page
    python mcacc_priority.py --details --photos  # also save photos to data/photos/

Be a good citizen: this script sleeps between requests, identifies
itself in the User-Agent, and is meant to run about once an hour from a
scheduled GitHub Action. The data supports MCACC's own placement goal:
finding outlets for at-risk dogs.
"""

import argparse
import base64
import json
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = "https://apps.pets.maricopa.gov/priority"
GRID_URL = f"{BASE}/Home/AnimalGrid"
DETAILS_URL = f"{BASE}/Details/"

# Default filters exactly as the portal's own front-end sends them.
FILTERS = {
    "TypeChoice": "ANY",
    "SizeChoice": "Any Size",
    "BreedChoice": "Any Breed",
    "AgeChoice": "1",
    "GenderChoice": "Any Gender",
    "ReasonChoice": "Any Reason",
    "AnimalName": "Any Animal",
    "AnimalId": "Any ID",
    "KennelNumber": "Any Kennel",
    "ShelterChoice": "Both Shelters",
}

USER_AGENT = (
    "TCDP-FosterPortal/0.1 (+https://dogfoster.org; contact foster@dogfoster.org) "
    "volunteer foster-network importer"
)
REQUEST_DELAY_S = 1.5
TIMEOUT_S = 30
MAX_PAGES = 20  # hard safety stop; the portal is ~3 pages in practice

STATUS_WORDS = (
    "TRANSFER PENDING",
    "ADOPTION PENDING",
    "RTO PENDING",
    "TRANSFERRED",
    "ADOPTED",
    "RTO",
)

# The overlay label is title-case in markup ("Transferred") and only
# LOOKS uppercase via CSS, so status matching must be case-insensitive.
CARD_RE = re.compile(
    r"^(?:(?P<status>(?i:" + "|".join(STATUS_WORDS) + r"))\s+)?"
    r"(?P<id>A\d{7})\s+"
    r"(?P<name>.+?)\s+"
    r"(?P<nho>NHO\s+)?PRIORITY:\s*(?P<reason>[A-Za-z]+)\s+"
    r"(?P<age>.+?)\s+"
    r"(?P<sex>Neutered|Spayed|Male|Female)\s+"
    r"(?P<shelter>East|West)\s+Shelter,\s*"
    r"(?P<kennel>.+?)\s+"
    r"(?P<deadline>\d{2}/\d{2}/\d{2})"
    # The status overlay can also sit after the deadline in DOM order.
    r"(?:\s+(?P<status2>(?i:" + "|".join(STATUS_WORDS) + r")))?$"
)

RESOLVED_STATUSES = {"TRANSFERRED", "ADOPTED", "RTO"}


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


def fetch_grid_page(session: requests.Session, page: int) -> str:
    params = {
        "submissionObj": json.dumps(FILTERS),
        "pageNumber": page,
        "env": BASE + "/",
    }
    r = session.get(GRID_URL, params=params, timeout=TIMEOUT_S)
    r.raise_for_status()
    return r.text


def parse_cards(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "html.parser")
    dogs = []
    for li in soup.select("li.dogCard"):
        a = li.select_one('a[href*="Details"]')
        token = ""
        if a and a.get("href"):
            token = a["href"].rstrip("/").split("/")[-1]
        text = re.sub(r"\s+", " ", li.get_text(" ", strip=True)).strip()
        m = CARD_RE.match(text)
        if not m:
            # Never crash the run on one odd card; record it for review.
            dogs.append({"parse_error": text, "token": token})
            continue
        d = m.groupdict()
        status = (d["status"] or d.get("status2") or "").strip().upper()
        deadline = datetime.strptime(d["deadline"], "%m/%d/%y").date()
        dogs.append(
            {
                "animal_id": d["id"],
                "name": d["name"].title(),
                "nho": bool(d["nho"]),
                "reason": d["reason"],
                "age": d["age"],
                "sex": d["sex"],
                "shelter": d["shelter"],
                "kennel": d["kennel"],
                "deadline": deadline.isoformat(),
                "days_left": (deadline - date.today()).days,
                "status": status or None,
                "resolved": status in RESOLVED_STATUSES,
                "token": token,
                "detail_url": DETAILS_URL + token if token else None,
                "raw": text,
            }
        )
    return dogs


def fetch_all(session: requests.Session) -> list[dict]:
    """Walk grid pages until they run dry.

    Quirk found on the first live run: the portal serves the SAME first
    page for pageNumber 0 and 1, so a single no-new-cards page must not
    end the walk. Stop only after two consecutive pages add nothing, or
    a page comes back empty.
    """
    seen_tokens: set[str] = set()
    seen_ids: set[str] = set()
    all_dogs: list[dict] = []
    consecutive_stale = 0
    for page in range(MAX_PAGES):
        html = fetch_grid_page(session, page)
        cards = parse_cards(html)
        if not cards:
            break
        new = [
            c
            for c in cards
            if c.get("token") not in seen_tokens
            or (not c.get("token") and c.get("animal_id") not in seen_ids)
        ]
        if not new:
            consecutive_stale += 1
            if consecutive_stale >= 2:
                break
            time.sleep(REQUEST_DELAY_S)
            continue
        consecutive_stale = 0
        for c in new:
            if c.get("token"):
                seen_tokens.add(c["token"])
            if c.get("animal_id"):
                seen_ids.add(c["animal_id"])
        all_dogs.extend(new)
        time.sleep(REQUEST_DELAY_S)
    return all_dogs


LABELED_FIELDS = [
    "Name",
    "Animal ID",
    "Breed",
    "Level",
    "Weight",
    "Age",
    "Sex",
    "Kennel",
    "Intake Date",
    "Due Out",
    "Deadline Date",
]

SECTION_HEADERS = [
    "Memo",
    "Intake",
    "Evaluation Comments",
    "Medical Treatments",
    "In-Kennel Behavior Rounds",
    "Bite History",
]


def fetch_detail(session: requests.Session, dog: dict, save_photo_dir: Path | None) -> dict:
    """Fetch a dog's detail page. Returns a dict of extra fields.

    The detail page is rich: labeled facts, shelter memo, intake
    questionnaires, staff behavior evaluations, medical treatments,
    in-kennel behavior rounds, bite history, and the photo embedded as a
    base64 data URL. Staff notes are for the INTERNAL team dashboard
    only; never republish them on public pages.
    """
    if not dog.get("token"):
        return {}
    r = session.get(DETAILS_URL + dog["token"], timeout=TIMEOUT_S)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    text = soup.get_text("\n", strip=True)
    lines = [ln.strip() for ln in text.split("\n") if ln.strip()]

    detail: dict = {}
    # Labeled fields appear as "Label" on one line, value on the next.
    for i, ln in enumerate(lines[:-1]):
        if ln in LABELED_FIELDS:
            key = ln.lower().replace(" ", "_")
            if key not in detail:
                detail[key] = lines[i + 1]

    # Carve the page text into sections for the team dashboard.
    sections: dict[str, str] = {}
    positions = []
    for header in SECTION_HEADERS:
        for i, ln in enumerate(lines):
            if ln == header or ln.startswith(header):
                positions.append((i, header))
                break
    positions.sort()
    for idx, (start, header) in enumerate(positions):
        end = positions[idx + 1][0] if idx + 1 < len(positions) else len(lines)
        sections[header.lower().replace(" ", "_").replace("-", "_")] = "\n".join(
            lines[start + 1 : end]
        )
    if sections:
        detail["sections"] = sections

    # Photo: first inline base64 JPEG on the page.
    if save_photo_dir is not None:
        img = soup.select_one('img[src^="data:image"]')
        if img:
            src = img["src"]
            try:
                _, b64 = src.split(",", 1)
                save_photo_dir.mkdir(parents=True, exist_ok=True)
                photo_path = save_photo_dir / f"{dog['animal_id']}.jpg"
                photo_path.write_bytes(base64.b64decode(b64))
                detail["photo_file"] = str(photo_path)
            except (ValueError, OSError) as e:
                detail["photo_error"] = str(e)
    return detail


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--details", action="store_true", help="fetch each dog's detail page")
    ap.add_argument("--photos", action="store_true", help="save photos (implies --details)")
    ap.add_argument("--out", default="data/priority-dogs.json", help="output JSON path")
    args = ap.parse_args()
    if args.photos:
        args.details = True

    session = make_session()
    dogs = fetch_all(session)

    if args.details:
        photo_dir = Path(args.out).parent / "photos" if args.photos else None
        for dog in dogs:
            if dog.get("parse_error") or dog.get("resolved"):
                continue
            try:
                dog.update(fetch_detail(session, dog, photo_dir))
            except requests.RequestException as e:
                dog["detail_error"] = str(e)
            time.sleep(REQUEST_DELAY_S)

    active = [d for d in dogs if not d.get("resolved") and not d.get("parse_error")]
    resolved = [d for d in dogs if d.get("resolved")]
    errors = [d for d in dogs if d.get("parse_error")]

    out = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": "https://apps.pets.maricopa.gov/Priority/ (MCACC Priority Placement Portal)",
        "counts": {"active": len(active), "resolved": len(resolved), "parse_errors": len(errors)},
        "active": sorted(active, key=lambda d: (d["deadline"], d["name"])),
        "resolved": resolved,
        "parse_errors": errors,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, indent=2))
    print(
        f"{len(active)} active, {len(resolved)} resolved, {len(errors)} parse errors -> {out_path}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
