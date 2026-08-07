# foster-portal-importer

Imports every dog on the MCACC Priority Placement Portal (the E-list)
into JSON for the TCDP foster portal at dogfoster.org.

## Why

The team cannot physically get to every dog, but wants to support them
all. This importer gives every priority dog a data record the moment
MCACC lists it: ID, name, NHO flag, reason, age, sex, shelter, kennel,
deadline, and (with `--details`) breed, level, weight, memo, staff
behavior evaluations, medical history, bite history, and the photo.

MCACC has no public API and has declined to build one. The portal is
public, server-rendered HTML, so this scrapes it politely: about once
an hour, ~1.5s between requests, an honest User-Agent with contact
info. The data serves MCACC's own stated goal of finding outlets for
at-risk animals.

## Ground rules

1. Staff notes (behavior evals, medical, bite history) feed the
   INTERNAL team dashboard only. Never republish them on public dog
   pages. Public pages get: name, photo, age, sex, shelter, deadline,
   NHO flag, and TCDP's own warm write-up.
2. Keep this importer isolated from the site. If MCACC redesigns the
   portal, the importer breaks; dogfoster.org must not.
3. NHO dogs must be pulled by a New Hope Partner rescue. Non-NHO
   deadline dogs can be adopted in person before the deadline, fees
   waived. Route CTAs accordingly.

## Usage

```
pip install requests beautifulsoup4
python mcacc_priority.py                     # grid only
python mcacc_priority.py --details           # + detail pages
python mcacc_priority.py --details --photos  # + photos to data/photos/
```

Output: `data/priority-dogs.json` with `active`, `resolved` (recent
TRANSFERRED/ADOPTED outcomes, useful for the wins feed), and
`parse_errors` (odd cards that need a parser tweak, never a crash).

`data/priority-dogs-2026-08-06.json` is the first snapshot, captured
2026-08-06 evening via browser extraction while building this.

## Endpoints (discovered 2026-08-06)

- Grid: `GET /priority/Home/AnimalGrid?submissionObj=<json>&pageNumber=N&env=...`
  returns an HTML fragment of `li.dogCard` elements, ~25 per page.
- Detail: `GET /priority/Details/<token>` returns the full record;
  the photo is an inline base64 JPEG.

## Status

Proof of concept. The parser is built against captured page structure
but has not yet run live from GitHub Actions. First run may need a
selector tweak: check `parse_errors` in the output.

## Note on the workflow file

Cowork's device bridge cannot write into `.github/workflows/` (protected
path), so the Action lives here as `github-workflow--mcacc-priority.yml`.
When creating the GitHub repo, move it to
`.github/workflows/mcacc-priority.yml`.
