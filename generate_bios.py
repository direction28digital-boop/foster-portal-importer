#!/usr/bin/env python3
"""
Write a bio for every priority dog that does not have one yet.

Source of truth is data/priority-dogs.json, produced by mcacc_priority.py.
Output is data/bios.json, keyed by animal id, read directly by the public site
(tcdp-web) and by daily_digest.py.

GROUND RULES, carried over from the hand-written batches and NOT negotiable:

1. Nothing is invented. Every claim traces to a county record. If the record is
   thin, the bio is short and says so.
2. Staff notes (behavior evaluations, medical history, bite history) are INTERNAL.
   They inform the bio; they are never reproduced in it.
3. The `needs` line is where safety lives. If a dog has bitten, has reacted when
   startled, cannot live with other dogs, or is not a beginner's dog, the needs
   line says so plainly enough that nobody unprepared raises their hand.
   Omitting that would be both dangerous and dishonest.
4. Never make the commitment sound smaller than it is. Months, not weeks. These
   rescues are foster based and have no facility.
5. Warm, dog lover to dog lover. Never corporate, never cute about a dog in
   trouble.

Usage:
    ANTHROPIC_API_KEY=... python generate_bios.py [--limit N] [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import requests

DATA = Path(__file__).parent / "data"
DOGS_FILE = DATA / "priority-dogs.json"
BIOS_FILE = DATA / "bios.json"
NEW_FILE = DATA / "bios-new.json"

API_URL = "https://api.anthropic.com/v1/messages"
MODEL = "claude-sonnet-4-5"
MAX_PER_RUN = 25  # a catch-up guard; normal days add a handful

SYSTEM = """You write foster-recruitment bios for The CrAZy Dog People, an Arizona \
rescue advocacy group, from Maricopa County (MCACC) shelter records.

Voice: warm, direct, dog lover to dog lover. Never corporate. Never cute about a dog \
in danger. Short sentences. No em dashes. No exclamation marks.

HARD RULES:
- Invent nothing. Every claim must trace to the record you are given. Thin record, \
short bio. Never guess at history, temperament, or how a dog ended up there.
- The staff notes you are given are INTERNAL. Never quote or paraphrase them as \
shelter documentation. Never mention euthanasia, kill dates, bite reports, medical \
treatment history, or behavior evaluations as such.
- The "needs" line is a safety line. If the record shows a bite, a reaction when \
startled, dog aggression, fear so deep the dog shuts down, or anything that means \
this is not a beginner's dog, the needs line must say so plainly, in plain words, so \
nobody unprepared raises their hand. Do not soften it. Do not bury it. You may write \
"this is not a beginner's dog and we will not pretend otherwise."
- Never imply a short-term commitment. These rescues are foster based and have no \
facility, so a foster IS the placement, usually for months. Never say "just a few \
days" or "a couple of weeks."
- If the dog is New Hope Only (nho true), it must be pulled by a partner rescue. Say \
so in needs.

Return ONLY a JSON object, no prose around it, with exactly these keys:
  "bullets": 3 to 5 short strings, concrete observed things (what handlers saw the dog \
             actually do). No adjectives without evidence.
  "story":   1 to 3 sentences. The honest shape of this dog's situation and who they \
             are underneath it.
  "needs":   1 to 2 sentences. What kind of home, plus the safety line, plus the \
             rescue-pull requirement if applicable."""


def load(path: Path, default):
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def is_cat(dog: dict) -> bool:
    """The county's priority list is not dogs only. Breed is the reliable signal."""
    breed = (dog.get("breed") or "").upper()
    return "DOMESTIC SH" in breed or "DOMESTIC MH" in breed or "DOMESTIC LH" in breed


def title(name: str | None, animal_id: str) -> str:
    if not name or name.strip().lower() == "name unknown":
        return f"Dog {animal_id}"
    return " ".join(w.capitalize() for w in name.split())


def tidy_breed(breed: str | None) -> str:
    if not breed:
        return "Mixed breed"
    # County format: "BROWN/WHITE AM PIT BULL TER" -> drop the colour prefix.
    parts = breed.split()
    words = [w for w in parts if "/" not in w] or parts
    out = " ".join(w.capitalize() for w in words)
    return out.replace("Ter", "Terrier").replace("Am Pit Bull", "Pit Bull")


def tidy_location(dog: dict) -> str:
    kennel = dog.get("kennel") or ""
    shelter = dog.get("shelter") or ""
    if kennel:
        return kennel.replace(" Kennel,", " Shelter,")
    return f"{shelter} Shelter".strip()


def tidy_age(age: str | None) -> str:
    if not age:
        return "Age unknown"
    # "5Y 0M" -> "5 years"; "0Y 3M" -> "3 months"
    years = months = 0
    for token in age.split():
        if token.endswith("Y"):
            years = int(token[:-1] or 0)
        elif token.endswith("M"):
            months = int(token[:-1] or 0)
    if years and months:
        return f"{years} years {months} months"
    if years:
        return f"{years} year" + ("s" if years != 1 else "")
    if months:
        return f"{months} month" + ("s" if months != 1 else "")
    return age


def build_prompt(dog: dict) -> str:
    sections = dog.get("sections") or {}
    lines = [
        "PUBLIC RECORD (safe to draw on):",
        f"  Name: {title(dog.get('name'), dog['animal_id'])}",
        f"  ID: {dog['animal_id']}",
        f"  Age: {tidy_age(dog.get('age'))}",
        f"  Sex: {dog.get('sex') or 'unknown'}",
        f"  Breed as recorded by staff (a visual guess, not DNA): {dog.get('breed')}",
        f"  Weight: {dog.get('weight')} lb",
        f"  Shelter: {tidy_location(dog)}",
        f"  Deadline: {dog.get('deadline')} ({dog.get('days_left')} days left)",
        f"  Listed as priority for: {dog.get('reason')}",
        f"  New Hope Only (needs a partner rescue to pull): {bool(dog.get('nho'))}",
        "",
        "INTERNAL STAFF NOTES (inform the bio, never reproduce):",
    ]
    for key in ("memo", "evaluation_comments", "medical", "bite_history", "intake"):
        val = sections.get(key)
        if val:
            lines.append(f"  [{key}] {val.strip()[:2500]}")
    lines.append("")
    lines.append("Write the JSON object now.")
    return "\n".join(lines)


def call_api(api_key: str, prompt: str) -> dict:
    resp = requests.post(
        API_URL,
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": MODEL,
            "max_tokens": 1200,
            "system": SYSTEM,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=120,
    )
    resp.raise_for_status()
    text = "".join(
        block.get("text", "") for block in resp.json().get("content", [])
    ).strip()
    # The model is told to return bare JSON, but tolerate a fenced block.
    if text.startswith("```"):
        text = text.split("```")[1]
        text = text[4:] if text.startswith("json") else text
    return json.loads(text)


def validate(payload: dict) -> tuple[bool, str]:
    bullets = payload.get("bullets")
    if not isinstance(bullets, list) or not 3 <= len(bullets) <= 5:
        return False, "bullets must be a list of 3 to 5 strings"
    if not all(isinstance(b, str) and b.strip() for b in bullets):
        return False, "bullets must all be non-empty strings"
    for key in ("story", "needs"):
        if not isinstance(payload.get(key), str) or not payload[key].strip():
            return False, f"{key} must be a non-empty string"
    return True, ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=MAX_PER_RUN)
    ap.add_argument("--dry-run", action="store_true",
                    help="report which dogs need bios, call no API")
    args = ap.parse_args()

    feed = load(DOGS_FILE, {})
    bios = load(BIOS_FILE, {})

    active = [d for d in feed.get("active", []) if not is_cat(d)]
    missing = [d for d in active if d.get("animal_id") not in bios]

    print(f"{len(active)} active dogs, {len(bios)} bios on file, "
          f"{len(missing)} missing")

    if not missing:
        NEW_FILE.write_text(json.dumps({"generated": [], "count": 0}, indent=2))
        print("Nothing to write.")
        return 0

    if args.dry_run:
        for d in missing:
            print(f"  would write: {d['animal_id']} {d.get('name')} "
                  f"({d.get('reason')}, deadline {d.get('deadline')})")
        return 0

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ANTHROPIC_API_KEY is not set", file=sys.stderr)
        return 1

    # Soonest deadline first: if a run is cut short, the dogs with the least time
    # are the ones that got written.
    missing.sort(key=lambda d: (d.get("deadline") or "9999-99-99"))

    written = []
    for dog in missing[: args.limit]:
        animal_id = dog["animal_id"]
        try:
            payload = call_api(api_key, build_prompt(dog))
            ok, why = validate(payload)
            if not ok:
                print(f"  SKIP {animal_id}: {why}", file=sys.stderr)
                continue
        except Exception as exc:  # never let one dog kill the run
            print(f"  SKIP {animal_id}: {exc}", file=sys.stderr)
            continue

        bios[animal_id] = {
            "animal_id": animal_id,
            "name": title(dog.get("name"), animal_id),
            "age": tidy_age(dog.get("age")),
            "breed": tidy_breed(dog.get("breed")),
            "location": tidy_location(dog),
            "bullets": [b.strip() for b in payload["bullets"]],
            "story": payload["story"].strip(),
            "needs": payload["needs"].strip(),
        }
        written.append(animal_id)
        print(f"  wrote {animal_id} {bios[animal_id]['name']}")
        time.sleep(1)  # be unhurried; there is no rush and no rate limit worth hitting

    BIOS_FILE.write_text(json.dumps(bios, indent=2, ensure_ascii=False) + "\n",
                         encoding="utf-8")
    NEW_FILE.write_text(
        json.dumps({"generated": written, "count": len(written)}, indent=2) + "\n",
        encoding="utf-8",
    )

    skipped = len(missing) - len(written)
    print(f"Wrote {len(written)} bios. {len(bios)} total on file.")
    if len(missing) > args.limit:
        # No silent caps.
        print(f"NOTE: {len(missing) - args.limit} dogs were over the per-run limit "
              f"of {args.limit} and will be picked up on the next run.")
    if skipped > 0 and len(missing) <= args.limit:
        print(f"NOTE: {skipped} dogs failed and were skipped, see errors above.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
