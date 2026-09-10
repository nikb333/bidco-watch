#!/usr/bin/env python3
"""
Weekly reconciliation against the published ASIC company register.

The register is the ground truth: every company, every name, current and
historical. Its only fault is that the cut is six to eight days old when it
lands. This job pulls it, extracts every role-named vehicle in the window, and
compares that against what the daily sweep already had.

Anything in the register the daily sweep did not have is a MISS, and gets
recorded as one. That is what turns "nothing falls through the cracks" from a
claim into a measurement.
"""
from __future__ import annotations

import csv
import io
import json
import re
import sys
import urllib.request
import zipfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
UA = __import__("os").environ.get("CONTACT_EMAIL","").strip()
UA = f"bidco-watch/1.0 (+{UA})" if UA else "bidco-watch/1.0"
CKAN = "https://data.gov.au/data/api/3/action/package_search?q=ASIC+company+register&rows=5"

sys.path.insert(0, str(ROOT))
from sweep import ROLE, BID, NOISE, stem_of, role_of  # noqa: E402

WINDOW_DAYS = int(__import__("os").environ.get("WEEKLY_WINDOW_DAYS", "180"))


def get(url, timeout=120):
    return urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": UA}), timeout=timeout)


def find_zip() -> tuple[str, str]:
    d = json.load(get(CKAN, 60))
    for p in d["result"]["results"]:
        if "company" not in p["title"].lower():
            continue
        for r in p.get("resources", []):
            if (r.get("format") or "").upper() == "ZIP" and "company_" in (r.get("url") or ""):
                return r["url"], r["url"].rsplit("/", 1)[-1]
    raise RuntimeError("could not locate the company register ZIP on data.gov.au")


def main() -> int:
    url, fname = find_zip()
    print(f"register: {fname}")
    blob = get(url, 600).read()
    print(f"downloaded {len(blob)/1e6:.0f} MB")

    z = zipfile.ZipFile(io.BytesIO(blob))
    member = z.namelist()[0]
    since = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=WINDOW_DAYS)

    by_acn: dict[str, dict] = {}
    renamed: list[dict] = []
    scanned = 0
    with z.open(member) as f:
        rd = csv.DictReader(io.TextIOWrapper(f, encoding="utf-8-sig"), delimiter="\t")
        for r in rd:
            scanned += 1
            reg = (r.get("Date of Registration") or "").strip()
            if not reg:
                continue
            try:
                d = datetime.strptime(reg, "%d/%m/%Y")
            except ValueError:
                continue
            if d < since:
                continue
            name = (r.get("Company Name") or "").strip()
            cur = (r.get("Current Name") or "").strip()
            if not (ROLE.search(name) or ROLE.search(cur)):
                continue
            acn = (r.get("ACN") or "").strip()
            if not acn.isdigit():
                continue
            is_current = (r.get("Current Name Indicator") or "").strip() == "Y"
            if is_current or acn not in by_acn:
                by_acn[acn] = {
                    "acn": acn, "name": name if is_current else cur or name,
                    "date": reg, "role": role_of(name if is_current else cur or name),
                    "state": (r.get("Previous State of Registration") or "").strip(),
                    "abn": bool((r.get("ABN") or "").strip()),
                    "status": (r.get("Status") or "").strip(),
                }
            if not is_current and cur:
                # A historical name row: the company was renamed into (or out of)
                # a role name. This is the shelf-company path, and the register is
                # the only place it is visible.
                renamed.append({"acn": acn, "was": name, "now": cur,
                                "since": (r.get("Current Name Start Date") or "").strip()})

    print(f"scanned {scanned:,} register rows -> {len(by_acn)} role-named vehicles "
          f"in the last {WINDOW_DAYS} days")

    # Same family rule as the daily sweep: two or more role-named companies sharing
    # a stem is a stack, with or without a Bidco among them. Over six months of the
    # register, 24 of 58 families carried no Bidco at all.
    counts = defaultdict(int)
    for e in by_acn.values():
        st = stem_of(e["name"])
        if st and len(st) > 3 and not NOISE.search(st):
            counts[st] += 1
    stack_has_bidco = defaultdict(bool)
    for e in by_acn.values():
        st = stem_of(e["name"])
        if counts.get(st, 0) >= 2 and BID.search(e["name"]):
            stack_has_bidco[st] = True

    entities = []
    for e in sorted(by_acn.values(),
                    key=lambda x: (datetime.strptime(x["date"], "%d/%m/%Y"), x["acn"]),
                    reverse=True):
        st = stem_of(e["name"])
        in_stack = counts.get(st, 0) >= 2
        entities.append({**e, "stem": st, "in_stack": in_stack,
                         "stack_has_bidco": bool(stack_has_bidco.get(st)),
                         "is_bidco": bool(BID.search(e["name"]))})

    # ---- reconciliation: what did the daily sweep already have?
    seen_acns: set[str] = set()
    dp = DATA / "daily.json"
    if dp.exists():
        dj = json.loads(dp.read_text())
        for st in dj.get("stacks", []):
            seen_acns |= {m["acn"] for m in st["members"]}
        seen_acns |= {m["acn"] for m in dj.get("singles", [])}
        seen_acns |= {m["acn"] for m in dj.get("role_only", [])}

    # Only judge the sweep on ground it actually covered.
    state = json.loads((DATA / "state.json").read_text()) if (DATA / "state.json").exists() else {}
    covered_lo = None
    hist = state.get("history") or []
    if hist:
        lows = [h["window"][0] for h in hist if h.get("window") and h["window"][0]]
        if lows:
            covered_lo = min(lows)

    judged = [e for e in entities if covered_lo and e["acn"] >= covered_lo]
    missed = [e for e in judged if e["acn"] not in seen_acns]
    recall = (1 - len(missed) / len(judged)) * 100 if judged else None

    out = {
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "dataset": fname, "window_days": WINDOW_DAYS,
        "entities": entities,
        "bidcos": sum(1 for e in entities if e["is_bidco"]),
        "stacks": len({e["stem"] for e in entities if e["in_stack"]}),
        "stacks_without_bidco": len({e["stem"] for e in entities
                                     if e["in_stack"] and not e["stack_has_bidco"]}),
        "renamed": renamed,
        "reconciliation": {
            "covered_from_acn": covered_lo,
            "judged": len(judged),
            "found_by_daily": len(judged) - len(missed),
            "missed_by_daily": [{"acn": e["acn"], "name": e["name"], "date": e["date"]}
                                for e in missed],
            "recall_pct": round(recall, 1) if recall is not None else None,
        },
    }
    DATA.mkdir(exist_ok=True)
    (DATA / "weekly.json").write_text(json.dumps(out, indent=1))
    with (DATA / "weekly_vehicles.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["name", "acn", "role", "date", "state",
                                          "abn", "status", "stem", "in_stack",
                                          "stack_has_bidco", "is_bidco"])
        w.writeheader()
        w.writerows(entities)

    print(f"{out['bidcos']} Bidcos · {len(renamed)} renames seen")
    if recall is not None:
        print(f"daily-sweep recall over covered ground: {recall:.1f}% "
              f"({len(missed)} missed of {len(judged)})")
        for m in missed[:20]:
            print(f"   MISSED  {m['acn']}  {m['name']}  {m['date']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
