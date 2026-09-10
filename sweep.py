#!/usr/bin/env python3
"""
Daily ACN sweep — finds Australian PE acquisition vehicles the day they are registered.

Two windows, both of which matter:

  FORWARD    from the last run's high-water mark to today's issuance frontier.
  TRAILING   below that: first any slot never visited at all (a gap left by a run
             that was cut short), then slots that were visited and came back empty.

The trailing pass is not belt-and-braces. ACNs are allocated ahead of use, so a
company registered today can carry a number issued days ago, sitting in a slot an
earlier run already looked at and found empty. Measured against the ASIC register
over 1 Jul - 6 Sep 2026 that cost 11 of 211 role-named vehicles (5.2%), including
three complete stacks: TRIDENT, DANONE and PULSE.

Everything is checkpointed as it goes. A killed run resumes; it never restarts.
"""
from __future__ import annotations

import gzip
import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"

API = "https://businessapi.com.au/api/v2/lookup/acn/"
# An honest, descriptive UA. Cloudflare rule 1010 rejects urllib's default one.
# This identifies the client; it is not an attempt to look like a browser.
_contact = os.environ.get("CONTACT_EMAIL", "").strip()
UA = f"bidco-watch/1.0 (+{_contact})" if _contact else "bidco-watch/1.0"

# The vendor throttles at roughly 50 requests/minute regardless of plan — ten
# concurrent requests all return 429. Serial, paced, single-threaded. Do not add
# a thread pool: it yields nothing but 429s.
RATE_PER_MIN = 48
GAP = 60.0 / RATE_PER_MIN

W = [8, 7, 6, 5, 4, 3, 2, 1]
ROLE = re.compile(
    r"\b(BIDCO|TOPCO|MIDCO|HOLDCO|FINCO|MEZZCO|PIKCO|BONDCO|DEBTCO|"
    r"NEWCO|INTERMEDIATECO|BUYCO|PARENTCO|ACQUISITIONCO)\b", re.I)
BID = re.compile(r"\bBIDCO\b", re.I)
# "A.C.N. 698 332 240 PTY LTD" - a shelf company registered without a name yet.
# ZELORA TOPCO/MEZZCO/HOLDCO spent thirteen days looking exactly like this before
# being renamed, which is why these get re-queried rather than filed away.
SHELF = re.compile(r"^A\.?C\.?N\.?[\s\d]{9,}", re.I)
DEBT = {"FINCO", "MEZZCO", "PIKCO", "BONDCO", "DEBTCO"}
SUF = re.compile(r"\b(PTY|LTD|LIMITED|PROPRIETARY|CO|AUSTRALIA|AU|AUS|HOLDINGS?|"
                 r"HOLDING|GROUP|NO\.?\s*\d+)\b\.?", re.I)
NOISE = re.compile(r"\b(SMSF|SUPER|SUPERFUND|SUPERANNUATION|FAMILY|CUSTODIAN|BARE)\b", re.I)
ORDER = ["TOPCO", "PARENTCO", "HOLDCO", "INTERMEDIATECO", "MIDCO", "MEZZCO", "PIKCO",
         "FINCO", "BONDCO", "DEBTCO", "NEWCO", "BUYCO", "ACQUISITIONCO", "BIDCO"]

FRONTIER_LO, FRONTIER_HI = 70_000_000, 71_000_000
TRAILING_SLOTS = int(os.environ.get("TRAILING_SLOTS", "20000"))
SHELF_DAYS = int(os.environ.get("SHELF_DAYS", "120"))
# Bank the checkpoint this often. A run cut short then loses at most this much
# work instead of everything since it started.
SAVE_EVERY_MIN = int(os.environ.get("SAVE_EVERY_MIN", "15"))
MAX_FORWARD_SLOTS = int(os.environ.get("MAX_FORWARD_SLOTS", "12000"))
# GitHub caps a hosted Actions job at 6 hours. That is a platform limit, not a
# choice - so run right up to it, and when the queue is still not empty, ask for
# a continuation job rather than dropping the remainder.
BUDGET_MIN = int(os.environ.get("BUDGET_MIN", "335"))

KEY = os.environ.get("BAPI_KEY", "")
DOCS = ROOT / "docs"
# Every five minutes. The page extrapolates between publishes from the known
# rate, so this only has to be often enough to keep the extrapolation honest -
# not often enough to look live on its own.
HEARTBEAT_SECS = int(os.environ.get("HEARTBEAT_SECS", "300"))


def _git(*args, **kw):
    return subprocess.run(("git",) + args, capture_output=True, text=True, timeout=120, **kw)


def heartbeat(phase, done, total, found, started_iso, force=False, _last=[0.0]):
    """Publish progress so the dashboard can show a live count.

    docs/progress.json is deliberately NOT encrypted: it holds counters and
    nothing else - no names, no ACNs, no dates. Anyone who finds the URL learns
    that a sweep is running and how far along it is, which is not worth hiding
    and is the whole point of the file.

    The push is best effort - a heartbeat must never take the sweep down - but it
    is NOT silent. An earlier version swallowed every failure, so a run could
    sweep for hours while the dashboard showed nothing and the log said nothing
    about why.
    """
    DOCS.mkdir(exist_ok=True)
    (DOCS / "progress.json").write_text(json.dumps({
        "phase": phase, "done": done, "total": total, "companies": found,
        "started_utc": started_iso,
        "updated_utc": datetime.now(timezone.utc).isoformat(
            timespec="seconds").replace("+00:00", "Z"),
        "rate_per_min": RATE_PER_MIN,
        "run_url": (f"{os.environ.get('GITHUB_SERVER_URL','https://github.com')}/"
                    f"{os.environ.get('GITHUB_REPOSITORY','')}/actions/runs/"
                    f"{os.environ.get('GITHUB_RUN_ID','')}")
                   if os.environ.get("GITHUB_RUN_ID") else "",
    }, indent=1))
    now = time.time()
    if not force and now - _last[0] < HEARTBEAT_SECS:
        return
    _last[0] = now
    if not os.environ.get("GITHUB_ACTIONS"):
        return
    try:
        _git("add", "docs/progress.json")
        c = _git("-c", "user.name=bidco-watch", "-c", "user.email=actions@github.com",
                 "commit", "-m", f"progress {done}/{total}")
        if c.returncode and "nothing to commit" not in (c.stdout + c.stderr):
            print(f"  [heartbeat] commit: {(c.stderr or c.stdout).strip()[:160]}")
            return
        pu = _git("push")
        if pu.returncode:
            # The branch moved under us. Rebase onto it and try once more; if that
            # still fails, say so and carry on sweeping.
            _git("fetch", "origin", "main")
            rb = _git("rebase", "-X", "ours", "origin/main")
            if rb.returncode:
                _git("rebase", "--abort")
                print(f"  [heartbeat] rebase failed, progress not published: "
                      f"{(rb.stderr or rb.stdout).strip()[:160]}")
                return
            pu = _git("push")
            if pu.returncode:
                print(f"  [heartbeat] push failed: {(pu.stderr or pu.stdout).strip()[:160]}")
                return
        print(f"  [heartbeat] published {done:,}/{total:,}")
    except Exception as ex:
        print(f"  [heartbeat] {type(ex).__name__}: {ex}")


# ----------------------------------------------------------------- ACN arithmetic
def check_digit(base: int) -> int:
    d = [int(c) for c in f"{base:08d}"]
    return (10 - (sum(w * x for w, x in zip(W, d)) % 10)) % 10


def acn_for(base: int) -> str:
    return f"{base:08d}{check_digit(base)}"


def base_of(acn: str) -> int:
    return int(str(acn)[:8])


# ----------------------------------------------------------------- HTTP
class Blocked(RuntimeError):
    """The vendor refused us. Stop and report — never try to route around it."""


# Set when GitHub cancels the run, or the runner is being reclaimed. The queue
# loop checks it and stops cleanly so the work so far can be banked.
STOPPING = {"flag": False}


def _on_signal(signum, _frame):
    STOPPING["flag"] = True
    print(f"\n:: signal {signum} - finishing the current slot and banking the "
          f"checkpoint before exiting", flush=True)


def bank(seen, note, frontier=None):
    """Persist and publish the checkpoint from inside the sweep.

    Normally the workflow does this after the sweep returns. On a cancellation
    those later steps are skipped entirely, so an interrupted run would throw
    away everything it had done - which is exactly what made "pause and resume"
    impossible. Doing it here means a cancelled run still banks its work and the
    next start carries on from where it stopped.
    """
    save_checkpoint(seen, frontier)
    if not os.environ.get("GITHUB_ACTIONS"):
        print(f"[bank] {note}: checkpoint written ({len(seen):,} slots)")
        return
    try:
        _git("add", "data", "docs/progress.json")
        c = _git("-c", "user.name=bidco-watch", "-c", "user.email=actions@github.com",
                 "commit", "-m", f"checkpoint ({note}) - {len(seen):,} slots")
        if c.returncode and "nothing to commit" not in (c.stdout + c.stderr):
            print(f"[bank] commit: {(c.stderr or c.stdout).strip()[:200]}")
            return
        for _ in range(3):
            if _git("push").returncode == 0:
                print(f"[bank] {note}: banked {len(seen):,} slots")
                return
            _git("fetch", "origin", "main")
            if _git("rebase", "-X", "ours", "origin/main").returncode:
                _git("rebase", "--abort")
                break
        print("[bank] could not push the checkpoint")
    except Exception as ex:
        print(f"[bank] {type(ex).__name__}: {ex}")


def lookup(acn: str, tries: int = 4):
    for n in range(tries):
        req = urllib.request.Request(
            API + acn,
            headers={"Authorization": f"Bearer {KEY}", "Accept": "application/json",
                     "User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
            return None if "errors" in d else d
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(20)
            elif e.code in (401, 403):
                raise Blocked(f"HTTP {e.code} from the lookup API — key or UA rejected")
            else:
                time.sleep(2 * (n + 1))
        except Exception:
            time.sleep(2 * (n + 1))
    return None


# ----------------------------------------------------------------- checkpoint
CKPT = DATA / "lookups.jsonl"


def load_checkpoint() -> dict:
    """acn -> record|None. None means the slot resolved to nothing when last seen.

    Plain JSON lines, not gzip. Gzip looks like the tidier choice and is exactly
    wrong here: compressed bytes change everywhere when the content changes, so
    git cannot delta them and every save costs a full copy. A plain file that
    mostly grows at one end deltas almost perfectly - measured at 0.4 MB for a
    whole night of fifteen-minute saves, against ~2 MB for a single compressed
    one. Cheap saves are what make a run resumable.
    """
    out = {}
    if CKPT.exists():
        for line in CKPT.open(encoding="utf-8"):
            try:
                r = json.loads(line)
                out[r["acn"]] = r["d"]
            except Exception:
                pass
    elif (DATA / "lookups.jsonl.gz").exists():          # one-time migration
        with gzip.open(DATA / "lookups.jsonl.gz", "rt", encoding="utf-8") as f:
            for line in f:
                try:
                    r = json.loads(line)
                    out[r["acn"]] = r["d"]
                except Exception:
                    pass
        print(f"migrated {len(out):,} slots from the old compressed checkpoint")
    return out


def save_checkpoint(seen: dict, frontier: int | None = None,
                    keep_slots: int = 80_000) -> None:
    """Keep a bounded tail so the repo does not grow without limit.

    Anchored on the issuance frontier, not on max(seen). Anchoring on the maximum
    meant one stray high ACN could carry the floor above every real slot and wipe
    the lot. Anything well above the frontier cannot be a registered company, so
    it is dropped rather than allowed to move the window.
    """
    if not seen:
        return
    anchor = frontier or max(base_of(a) for a in seen)
    floor, ceiling = anchor - keep_slots, anchor + 1_000
    DATA.mkdir(exist_ok=True)
    kept = 0
    tmp = CKPT.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for a in sorted(seen):
            b = base_of(a)
            if floor <= b <= ceiling:
                f.write(json.dumps({"acn": a, "d": seen[a]}) + "\n")
                kept += 1
    # Never replace a good checkpoint with an empty one.
    if kept == 0 and CKPT.exists():
        tmp.unlink(missing_ok=True)
        print("[checkpoint] refusing to write an empty checkpoint over an existing one")
        return
    tmp.replace(CKPT)


def load_state() -> dict:
    p = DATA / "state.json"
    if p.exists():
        return json.loads(p.read_text())
    return {"last_frontier_base": None, "history": []}


# ----------------------------------------------------------------- frontier
def find_frontier() -> int:
    """Binary-search the highest base that resolves to anything.

    Deliberately keeps its own cache instead of writing into the checkpoint.
    It probes bases anywhere between 70,000,000 and 71,000,000, and those probes
    are not observations of the register - they are scaffolding. Letting them
    into `seen` was catastrophic: save_checkpoint prunes relative to the highest
    base it can see, so a probe at 70.9m set the floor at 70.82m and silently
    deleted every real slot below it. Every run then started from nothing while
    appearing to work.

    Costs about 120 lookups.
    """
    probes: dict[str, object] = {}
    lo, hi = FRONTIER_LO, FRONTIER_HI
    while lo < hi - 1:
        mid = (lo + hi) // 2
        hit = False
        for k in range(6):
            a = acn_for(mid + k)
            if a not in probes:
                probes[a] = lookup(a)
                time.sleep(GAP)
            if probes[a]:
                hit = True
                break
        if hit:
            lo = mid
        else:
            hi = mid
    return lo


def stem_of(name: str) -> str:
    s = SUF.sub(" ", ROLE.sub(" ", (name or "").upper()))
    return re.sub(r"\s+", " ", re.sub(r"[^A-Z0-9 ]", " ", s)).strip()


def role_of(name: str) -> str:
    m = ROLE.search(name or "")
    return m.group(1).upper() if m else ""


LEDGER = DATA / "found.json"


def load_ledger() -> dict:
    """Every deal vehicle the daily sweep has ever found, keyed by ACN.

    The checkpoint is a rolling window - it holds the last 80,000 slots and
    prunes below that - so anything found more than a couple of weeks ago would
    quietly drop off the dashboard. This ledger is the opposite: it only ever
    grows, and an entry leaves only when the weekly job confirms the company has
    appeared in the published ASIC register. Found on the daily list, stays on
    the daily list, until it turns up on the weekly one.
    """
    if LEDGER.exists():
        try:
            return json.loads(LEDGER.read_text())
        except Exception:
            print("[ledger] unreadable, starting a new one")
    return {}


def merge_ledger(ledger: dict, rows: list[dict], now_iso: str) -> int:
    """Add anything role-named from this run. Returns how many are new."""
    added = 0
    for r in rows:
        role = role_of(r["name"])
        if not role:
            continue
        st = stem_of(r["name"])
        if not st or len(st) <= 3 or NOISE.search(st):
            continue
        if r["acn"] in ledger:
            ledger[r["acn"]].update({"name": r["name"], "role": role, "stem": st,
                                     "date": r["date"], "suburb": r["suburb"],
                                     "state": r["state"]})
            continue
        ledger[r["acn"]] = {
            "acn": r["acn"], "name": r["name"], "role": role, "stem": st,
            "date": r["date"], "suburb": r["suburb"], "state": r["state"],
            "first_seen": now_iso, "in_register": False, "via": "API",
        }
        added += 1
    return added


def company_rows(seen: dict) -> list[dict]:
    """Companies only. The endpoint covers every ASIC register and business names
    are about two thirds of what comes back."""
    out = []
    for a, d in seen.items():
        if not d or "Company" not in (d.get("type") or ""):
            continue
        ad = (d.get("addresses") or [{}])[0]
        out.append({
            "acn": a, "name": d.get("name", ""), "date": d.get("registrationDate", ""),
            "status": d.get("status", ""), "type": d.get("type", ""),
            "suburb": ad.get("suburb", ""),
            "state": ad.get("state", "") or d.get("incorporationState", ""),
            "postcode": ad.get("postcode", ""),
        })
    out.sort(key=lambda r: r["acn"])
    return out


def group(rows: list[dict]) -> tuple[list, list, list]:
    """Split resolved companies into what matters and what does not.

    A STACK is two or more companies sharing a name stem where at least two carry
    a vehicle role word. It deliberately does NOT require a Bidco: checked against
    six months of the register, 24 of 58 such families had no Bidco in any name -
    ZELORA (TOPCO/MEZZCO/HOLDCO), GANZ (TOPCO/MIDCO/PARENTCO), EPTEC INFRA,
    TUGUN BUYER among them. Requiring the word threw away 41% of the structures.
    Stacks that do contain a Bidco are flagged, because they remain the strongest
    signal.

    A BIDCO is any lone company with BIDCO in its name and no family found yet.

    Everything else - a single HOLDCO, a lone FINCO - is OTHER. Real, kept, but
    weak on its own and not worth the front page.
    """
    g = defaultdict(list)
    for r in rows:
        s = r.get("stem") or stem_of(r["name"])
        if s and len(s) > 3 and not NOISE.search(s):
            g[s].append(r)

    stacks, bidcos, other = [], [], []
    for s, ms in g.items():
        roled = [m for m in ms if role_of(m["name"])]
        if len(roled) >= 2:
            roled.sort(key=lambda m: ORDER.index(role_of(m["name"]))
                       if role_of(m["name"]) in ORDER else 99)
            roles = [role_of(m["name"]) for m in roled]
            dts = [m["date"] for m in roled if m["date"]]
            stacks.append({
                "stem": s, "first": min(dts) if dts else "",
                "has_bidco": any(BID.search(m["name"]) for m in roled),
                "finco": any(r in DEBT for r in roles),
                "phased": len(set(dts)) > 1,
                "members": [{"acn": m["acn"], "name": m["name"], "date": m["date"],
                             "role": role_of(m["name"]), "suburb": m["suburb"],
                             "state": m["state"], "via": "API",
                             "first_seen": m.get("first_seen", "")} for m in roled]})
            continue
        for m in ms:
            if not role_of(m["name"]):
                continue
            rec = {"acn": m["acn"], "name": m["name"], "date": m["date"],
                   "role": role_of(m["name"]), "suburb": m["suburb"],
                   "state": m["state"], "via": "API",
                   "first_seen": m.get("first_seen", "")}
            (bidcos if BID.search(m["name"]) else other).append(rec)

    # Bidco-bearing stacks first, then the rest, newest first within each.
    stacks.sort(key=lambda x: (x["has_bidco"], x["first"]), reverse=True)
    bidcos.sort(key=lambda m: m["date"], reverse=True)
    other.sort(key=lambda m: m["date"], reverse=True)
    return stacks, bidcos, other


# ----------------------------------------------------------------- main
def main() -> int:
    if not KEY:
        print("BAPI_KEY is not set", file=sys.stderr)
        return 2
    DATA.mkdir(exist_ok=True)
    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    started = time.time()
    now = datetime.now(timezone.utc)
    state = load_state()
    seen = load_checkpoint()
    print(f"checkpoint: {len(seen):,} slots known")

    try:
        frontier = find_frontier()
    except Blocked as e:
        write_run(state, now, started, error=str(e), status="failed")
        print(f"BLOCKED: {e}", file=sys.stderr)
        return 1
    print(f"frontier: base {frontier:,}  acn {acn_for(frontier)}")

    last = state.get("last_frontier_base") or (frontier - 5000)
    forward = [b for b in range(last + 1, frontier + 1)]
    if len(forward) > MAX_FORWARD_SLOTS:
        # Too big to finish: take the most recent chunk and leave a note.
        forward = forward[-MAX_FORWARD_SLOTS:]
    # Two-pass ordering. Every slot is still visited — this is ordering, not
    # sampling. It matters when a run is cut short: a stack shows up as soon as
    # any one member of the family does.
    plan = [acn_for(b) for b in forward[::2]] + [acn_for(b) for b in forward[1::2]]
    plan = [a for a in plan if a not in seen]

    # The trailing window has two jobs, and conflating them once let 12,000 slots
    # fall through silently.
    #
    #   GAPS     slots in the window this run has never looked at at all. They
    #            appear whenever an earlier run was cut short, or the high-water
    #            mark advanced over ground that was only partly covered. These are
    #            the highest-value slots in the queue — a company sitting in one
    #            has never been seen by anything.
    #   RECHECK  slots that WERE visited and came back empty. ACNs are allocated
    #            ahead of use, so an empty slot can fill in later. Lower yield, but
    #            it is what recovers the TRIDENT / DANONE / PULSE class of miss.
    #
    # Gaps go first. A run that runs out of time leaves rechecks undone, which is
    # the right thing to sacrifice.
    # Shelf companies: registered under a numeric placeholder and renamed days or
    # weeks later, once the deal firms up. The daily sweep sees the placeholder and
    # would never look again, so ZELORA - a TOPCO/MEZZCO/HOLDCO family, the most
    # leveraged-looking structure in six months of the register - was invisible to
    # it entirely. About six are registered per business day, so re-querying a
    # rolling window of them costs roughly eight minutes a night.
    cutoff = (datetime.now(timezone.utc) - timedelta(days=SHELF_DAYS)).date()
    shelf = []
    for a, d in seen.items():
        if not d or not SHELF.match(d.get("name", "") or ""):
            continue
        try:
            if datetime.strptime(d.get("registrationDate") or "", "%Y-%m-%d").date() >= cutoff:
                shelf.append(a)
        except (ValueError, TypeError):
            shelf.append(a)      # undated or malformed: cheap enough to keep checking
    shelf.sort(reverse=True)

    trail_hi = forward[0] if forward else frontier
    trail_lo = max(FRONTIER_LO, trail_hi - TRAILING_SLOTS)
    gaps, recheck = [], []
    for b in range(trail_lo, trail_hi):
        a = acn_for(b)
        if a not in seen:
            gaps.append(a)
        elif seen[a] is None:
            recheck.append(a)
    # Walk gaps every-second-first too, so a truncated run spans the whole hole
    # rather than covering the bottom of it.
    gaps = gaps[::2] + gaps[1::2]
    trailing = gaps + recheck
    print(f"forward: {len(plan):,} new slots   shelf re-checks: {len(shelf):,}   "
          f"trailing: {len(gaps):,} never-visited gaps + {len(recheck):,} empty re-checks")

    # Forward first, then shelf re-checks (cheap and high-yield), then the
    # trailing window. If the clock beats us, trailing re-checks are the right
    # thing to lose.
    queue = plan + shelf + trailing
    deadline = started + BUDGET_MIN * 60
    done = truncated = bad = 0
    found_n = sum(1 for v in seen.values() if v and "Company" in (v.get("type") or ""))
    last_save = time.time()
    err = ""
    try:
        for a in queue:
            if STOPPING["flag"]:
                truncated = len(queue) - done
                print(f"stopping early - {truncated:,} slots left for the next start")
                break
            if time.time() > deadline:
                truncated = len(queue) - done
                print(f"time budget reached — {truncated:,} slots left for next run")
                break
            try:
                seen[a] = lookup(a)
            except Blocked:
                raise
            except Exception as ex:
                # A single odd record is not a reason to lose the rest of the night.
                # Leave the slot unrecorded so a later run picks it up as a gap.
                bad += 1
                if bad <= 5:
                    print(f"  !! {a}: {type(ex).__name__}: {ex}")
                elif bad == 6:
                    print("  !! further per-slot errors suppressed")
                continue
            done += 1
            d = seen[a]
            if d and "Company" in (d.get("type") or ""):
                found_n += 1
            if done % 50 == 0:
                heartbeat("running", done, len(queue), found_n,
                          now.isoformat(timespec="seconds"))
            if time.time() - last_save > SAVE_EVERY_MIN * 60:
                last_save = time.time()
                bank(seen, f"{done:,}/{len(queue):,}", frontier)
            if d and ROLE.search(d.get("name", "") or ""):
                print(f"  *** {a}  {d['name']}  {d.get('registrationDate','')}")
            if done % 250 == 0:
                print(f"  {done:,}/{len(queue):,}", flush=True)
            time.sleep(GAP)
    except Blocked as e:
        err = str(e)
        print(f"::error::BLOCKED: {e}", file=sys.stderr)
    except Exception as e:
        import traceback
        err = f"{type(e).__name__}: {e}"
        traceback.print_exc()
        print(f"::error::sweep aborted - {err}", file=sys.stderr)
    finally:
        heartbeat("idle", done, len(queue), found_n,
                  now.isoformat(timespec="seconds"), force=True)
        # On a cancellation the workflow's remaining steps never run, so bank
        # from here. On a normal finish this is a cheap no-op that the workflow's
        # own commit step then supersedes.
        bank(seen, "cancelled" if STOPPING["flag"] else "end of sweep", frontier)

    # A hosted Actions job is capped at six hours by GitHub, which is not ours to
    # raise, and a job cannot dispatch itself to get around it. Instead the
    # workflow is scheduled three times a night and this run is resumable: the
    # leftovers below are simply queued again by the next start.
    if truncated and not err:
        print(f"::notice::{truncated:,} slots remain - the next scheduled start "
              f"({'2am or 5am Sydney'}) resumes from the checkpoint")

    if bad:
        print(f"{bad:,} slots errored and were left for a later run")
    rows = company_rows(seen)

    # Findings accumulate. Grouping runs over the ledger rather than over this
    # run's rows, so a family found across several nights - HAMILTON HOLDCO one
    # night, HAMILTON MIDCO the next - assembles into one stack instead of
    # appearing as two unrelated singles.
    ledger = load_ledger()
    added = merge_ledger(ledger, rows, now.isoformat(timespec="seconds"))
    LEDGER.write_text(json.dumps(ledger, indent=1, sort_keys=True))
    waiting = [v for v in ledger.values() if not v.get("in_register")]
    moved = sum(1 for v in ledger.values() if v.get("in_register"))
    print(f"ledger: {added} new, {len(waiting)} awaiting the register, "
          f"{moved} already in it")
    stacks, bidcos, other = group(waiting)

    # Only advance the high-water mark over ground we actually covered.
    if not truncated and not err and forward:
        state["last_frontier_base"] = frontier

    write_run(state, now, started,
              frontier=frontier, swept=done, queued=len(queue), truncated=truncated,
              gaps=len(gaps), recheck=len(recheck),
              companies=len(rows), stacks=stacks, singles=bidcos, role_only=other,
              bad=bad, ledger_new=added, ledger_waiting=len(waiting),
              ledger_in_register=moved,
              rows=rows, error=err, status="failed" if err else "ok",
              # When the forward pass is empty the window is the trailing one -
              # reporting a blank start made the page read "Window  -> ...".
              window=[acn_for(forward[0]) if forward else acn_for(trail_lo),
                      acn_for(frontier)])
    nb = sum(1 for x in stacks if x["has_bidco"])
    print(f"done: {done:,} slots, {len(rows):,} companies, {len(stacks)} stacks "
          f"({nb} with a Bidco), {len(bidcos)} lone Bidcos, {len(other)} other role "
          f"names, {(time.time()-started)/60:.0f} min")
    return 1 if err else 0


def write_run(state, now, started, **kw):
    run = {
        "finished_utc": now.isoformat(timespec="seconds"),
        "elapsed_min": round((time.time() - started) / 60, 1),
        "status": kw.get("status", "ok"),
        "error": kw.get("error", ""),
        "frontier_base": kw.get("frontier"),
        "window": kw.get("window", ["", ""]),
        "slots_swept": kw.get("swept", 0),
        "slots_queued": kw.get("queued", 0),
        "slots_left": kw.get("truncated", 0),
        "gaps_queued": kw.get("gaps", 0),
        "recheck_queued": kw.get("recheck", 0),
        "companies": kw.get("companies", 0),
        "trailing_slots": TRAILING_SLOTS,
        "slots_errored": kw.get("bad", 0),
        "ledger_new": kw.get("ledger_new", 0),
        "ledger_waiting": kw.get("ledger_waiting", 0),
        "ledger_in_register": kw.get("ledger_in_register", 0),
    }
    state.setdefault("history", []).append(run)
    state["history"] = state["history"][-60:]
    state["last_run"] = run
    (DATA / "state.json").write_text(json.dumps(state, indent=1))

    if "stacks" in kw:
        (DATA / "daily.json").write_text(json.dumps({
            "run": run,
            "stacks": kw["stacks"], "singles": kw["singles"],
            "role_only": kw["role_only"],
        }, indent=1))
        import csv
        with (DATA / "companies.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=["acn", "name", "date", "status", "type",
                                              "suburb", "state", "postcode"])
            w.writeheader()
            w.writerows(kw["rows"])


if __name__ == "__main__":
    sys.exit(main())
