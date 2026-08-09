#!/usr/bin/env python3
"""
Send the team's morning digest: what changed on the county priority list overnight.

Reads the importer's own output. Sends via Resend (HTTPS API).

Why Resend and not SES: SES is stuck in the sandbox, which only delivers to addresses
verified inside the AWS account. That is fine for the two staff mailboxes but useless
for anything applicant-facing, so the new stack sends through Resend. dogfoster.org's
WordPress notifications still go through SES and are untouched by this.

Env:
    RESEND_API_KEY   required
    DIGEST_TO        comma-separated recipients (default: deerommes@gmail.com)
    DIGEST_FROM      default: The CrAZy Dog People <digest@thecrazydogpeople.com>
    SITE_URL         default: https://thecrazydogpeople.com

Usage:
    python daily_digest.py [--dry-run]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

DATA = Path(__file__).parent / "data"
SITE = os.environ.get("SITE_URL", "https://thecrazydogpeople.com").rstrip("/")

# Phoenix does not observe daylight saving. The county clock is always UTC-7.
PHOENIX = timezone(timedelta(hours=-7))

INK = "#1f2421"
SAGE = "#4a5d4e"
RUST = "#a4501f"
CREAM = "#faf6ef"
MUTED = "#6b6f6c"


def load(path: Path, default):
    if not path.exists():
        return default
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def is_cat(dog: dict) -> bool:
    breed = (dog.get("breed") or "").upper()
    return any(k in breed for k in ("DOMESTIC SH", "DOMESTIC MH", "DOMESTIC LH"))


def title(name: str | None, animal_id: str) -> str:
    if not name or name.strip().lower() == "name unknown":
        return f"Dog {animal_id}"
    return " ".join(w.capitalize() for w in name.split())


def days_left(deadline: str | None) -> int | None:
    if not deadline:
        return None
    try:
        target = datetime.strptime(deadline, "%Y-%m-%d").date()
    except ValueError:
        return None
    return (target - datetime.now(PHOENIX).date()).days


def esc(text: str) -> str:
    return (text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def dog_row(dog: dict, bios: dict) -> str:
    animal_id = dog["animal_id"]
    name = title(dog.get("name"), animal_id)
    left = days_left(dog.get("deadline"))
    if left is None:
        when = "no deadline listed"
    elif left <= 0:
        when = "TODAY"
    elif left == 1:
        when = "1 day left"
    else:
        when = f"{left} days left"
    urgent = left is not None and left <= 2
    bio = bios.get(animal_id) or {}
    needs = bio.get("needs", "")
    route = "Needs a rescue to pull" if dog.get("nho") else "Can be adopted directly"

    return f"""
    <tr><td style="padding:14px 0;border-bottom:1px solid #e6e0d6;">
      <div style="font:600 16px/1.3 system-ui,sans-serif;color:{INK};">
        <a href="{SITE}/dogs/{animal_id}" style="color:{INK};text-decoration:none;">{esc(name)}</a>
        <span style="font-weight:400;color:{MUTED};">&nbsp;{animal_id}</span>
      </div>
      <div style="font:14px/1.4 system-ui,sans-serif;color:{RUST if urgent else MUTED};margin-top:3px;">
        {when} &middot; {route} &middot; {esc(dog.get('reason') or 'priority')}
      </div>
      {f'<div style="font:14px/1.5 system-ui,sans-serif;color:{SAGE};margin-top:6px;">{esc(needs)}</div>' if needs else ''}
    </td></tr>"""


def build(feed: dict, bios: dict, new_ids: list[str]) -> tuple[str, str]:
    active = [d for d in feed.get("active", []) if not is_cat(d)]
    resolved = feed.get("resolved", [])

    new_dogs = [d for d in active if d["animal_id"] in set(new_ids)]
    urgent = sorted(
        [d for d in active if (days_left(d.get("deadline")) or 99) <= 2],
        key=lambda d: d.get("deadline") or "9999",
    )
    saved = [
        d for d in resolved
        if (d.get("status") or "").upper() in ("TRANSFERRED", "ADOPTED")
    ]

    today = datetime.now(PHOENIX).strftime("%A %-d %B")
    subject = f"{len(active)} dogs waiting"
    if urgent:
        subject += f", {len(urgent)} out of time in 2 days"
    if new_dogs:
        subject += f" ({len(new_dogs)} new)"

    def section(heading: str, note: str, dogs: list[dict]) -> str:
        if not dogs:
            return ""
        rows = "".join(dog_row(d, bios) for d in dogs)
        return f"""
        <tr><td style="padding:26px 0 4px;">
          <div style="font:700 13px/1 system-ui,sans-serif;letter-spacing:.09em;
                      text-transform:uppercase;color:{SAGE};">{heading}</div>
          <div style="font:14px/1.5 system-ui,sans-serif;color:{MUTED};margin-top:5px;">{note}</div>
        </td></tr>
        <tr><td><table width="100%" cellpadding="0" cellspacing="0">{rows}</table></td></tr>"""

    html = f"""<!doctype html>
<html><body style="margin:0;padding:0;background:{CREAM};">
<table width="100%" cellpadding="0" cellspacing="0" style="background:{CREAM};padding:28px 14px;">
<tr><td align="center">
<table width="100%" style="max-width:600px;" cellpadding="0" cellspacing="0">

  <tr><td style="padding-bottom:6px;">
    <div style="font:700 22px/1.2 system-ui,sans-serif;color:{INK};">The dogs this morning</div>
    <div style="font:15px/1.5 system-ui,sans-serif;color:{MUTED};margin-top:5px;">
      {today} &middot; {len(active)} dogs waiting &middot; {len(saved)} recently out
    </div>
  </td></tr>

  {section("New since yesterday",
           "Bios are already written and live on their pages. Correct anything that reads wrong.",
           new_dogs)}
  {section("Out of time within 2 days",
           "These are the ones worth a phone call today.",
           urgent)}

  <tr><td style="padding:26px 0 0;border-top:1px solid #e6e0d6;">
    <div style="font:14px/1.6 system-ui,sans-serif;color:{MUTED};">
      Every dog is at <a href="{SITE}/dogs" style="color:{SAGE};">{SITE.replace('https://','')}/dogs</a>.
      Nobody typed this email. The importer reads the county's own priority list every
      hour and this goes out on its own each morning.
    </div>
    <div style="font:13px/1.6 system-ui,sans-serif;color:{MUTED};margin-top:12px;">
      Applications still arrive through dogfoster.org exactly as they always have.
    </div>
  </td></tr>

</table></td></tr></table></body></html>"""

    lines = [f"The dogs this morning - {today}",
             f"{len(active)} waiting, {len(saved)} recently out", ""]
    for label, dogs in (("NEW SINCE YESTERDAY", new_dogs),
                        ("OUT OF TIME WITHIN 2 DAYS", urgent)):
        if dogs:
            lines.append(label)
            for d in dogs:
                left = days_left(d.get("deadline"))
                lines.append(
                    f"  {title(d.get('name'), d['animal_id'])} {d['animal_id']} - "
                    f"{'TODAY' if (left or 0) <= 0 else f'{left} days left'} - "
                    f"{SITE}/dogs/{d['animal_id']}")
            lines.append("")
    lines.append(f"Every dog: {SITE}/dogs")
    return subject, html


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true",
                    help="print the subject and save the HTML, send nothing")
    args = ap.parse_args()

    feed = load(DATA / "priority-dogs.json", {})
    bios = load(DATA / "bios.json", {})
    new_ids = load(DATA / "bios-new.json", {}).get("generated", [])

    if not feed.get("active"):
        # An empty feed means the scrape failed, not that no dogs are waiting.
        # Sending "0 dogs waiting" would be a lie with real consequences.
        print("Feed is empty. Refusing to send.", file=sys.stderr)
        return 1

    subject, html = build(feed, bios, new_ids)

    if args.dry_run:
        Path("/tmp/digest-preview.html").write_text(html, encoding="utf-8")
        print(f"Subject: {subject}")
        print("Preview written to /tmp/digest-preview.html")
        return 0

    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        print("RESEND_API_KEY is not set", file=sys.stderr)
        return 1

    to = [a.strip() for a in
          os.environ.get("DIGEST_TO", "deerommes@gmail.com").split(",") if a.strip()]
    sender = os.environ.get(
        "DIGEST_FROM", "The CrAZy Dog People <digest@thecrazydogpeople.com>")

    resp = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        json={"from": sender, "to": to, "subject": subject, "html": html},
        timeout=60,
    )
    if resp.status_code >= 300:
        print(f"Resend rejected the send: {resp.status_code} {resp.text}",
              file=sys.stderr)
        return 1

    print(f"Sent to {', '.join(to)} - {subject}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
