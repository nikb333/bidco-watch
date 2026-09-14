#!/usr/bin/env python3
"""Decide whether the weekly register job should do any work.

Writes skip=true/false to $GITHUB_OUTPUT. The question it asks is deliberately
about content rather than time: is the file published on data.gov.au newer than
the one already recorded in data/weekly.json?

That is the only fact that matters, and it has no timezone. ASIC publishes the
company register shortly after midnight Sydney on a Tuesday - the 15 September
file landed at 00:53 - but the exact moment is not guaranteed, and a job pinned
to a clock either fires before the file lands or hours after it. Asking the
publisher directly costs one API call, survives daylight saving, recovers from a
dropped firing on its own, and picks up an off-schedule republication too.

Falls through to running when anything is unclear: a missing weekly.json, an
extract more than eight days old, or an API that will not answer. A needless run
costs five minutes; a skipped one costs a week of staleness.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CKAN = ("https://data.gov.au/data/api/3/action/package_search"
        "?q=ASIC+company+register&rows=5")
UA = "bidco-watch/1.0" + (f" (+{os.environ['CONTACT_EMAIL']})"
                          if os.environ.get("CONTACT_EMAIL") else "")
LOCAL = Path("data/weekly.json")
MAX_AGE_DAYS = 8


def decide() -> tuple[bool, str]:
    """(run?, reason)"""
    if os.environ.get("EVENT_NAME") != "schedule":
        return True, "manual run"

    have, have_stamp = None, ""
    try:
        have = json.loads(LOCAL.read_text())
        have_stamp = have.get("source_modified") or ""
    except Exception:
        return True, "no register extract on file yet"

    age = None
    try:
        t = datetime.fromisoformat(have["generated_utc"].replace("Z", "+00:00"))
        age = (datetime.now(timezone.utc) - t).days
    except Exception:
        pass
    if age is None or age >= MAX_AGE_DAYS:
        return True, f"extract on file is {age} days old (limit {MAX_AGE_DAYS})"

    try:
        req = urllib.request.Request(CKAN, headers={"User-Agent": UA})
        d = json.load(urllib.request.urlopen(req, timeout=60))
    except Exception as ex:
        # Cannot tell. Run: five wasted minutes beats a week of stale data.
        return True, f"could not reach data.gov.au ({type(ex).__name__}), running anyway"

    published, fname = "", ""
    for p in d["result"]["results"]:
        if "company" not in (p.get("title") or "").lower():
            continue
        for r in p.get("resources", []):
            if (r.get("format") or "").upper() == "ZIP" and "company_" in (r.get("url") or ""):
                published = r.get("last_modified") or r.get("metadata_modified") or ""
                fname = (r.get("url") or "").rsplit("/", 1)[-1]
                break
        if published:
            break
    if not published:
        return True, "could not read the publication stamp, running anyway"

    print(f"published: {fname}  {published}")
    print(f"on file:   {have.get('dataset','?')}  {have_stamp or '(no stamp recorded)'}")
    if not have_stamp:
        return True, "the extract on file predates stamp tracking"
    if published != have_stamp:
        return True, "ASIC has published a newer register"
    return False, "the register on file is the current one"


def main() -> int:
    run, why = decide()
    print(("RUN:  " if run else "SKIP: ") + why)
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write(f"skip={'false' if run else 'true'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
