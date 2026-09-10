# Bidco Watch

Finds Australian private-equity acquisition vehicles — Topco/Holdco/Midco/Bidco
families — in the days before a deal is announced, rather than the week after.

ASIC publishes the full company register weekly, and it is already several days
stale when it lands. Acquisition shells usually hold no ABN and no LEI, so no
free live source can see them in between. This walks the ACN number space
directly and closes that gap.

Everything runs on GitHub Actions. No server, no Claude in the loop at runtime.

---

## Setup (once)

1. **Secrets** — Settings → Secrets and variables → Actions → New repository secret:

   | Name | Value |
   |---|---|
   | `BAPI_KEY` | your Business API key (`bapi_sk_live_…`) |
   | `SITE_PASSCODE` | the passcode that unlocks the dashboard |

2. **Pages** — Settings → Pages → Source: *Deploy from a branch*, Branch: `main`,
   folder `/docs`.

3. **Actions write access** — Settings → Actions → General → Workflow permissions:
   *Read and write permissions*. The workflows commit results back to the repo.

4. Run **Actions → Daily ACN sweep → Run workflow** once to seed the data.

---

## What runs, and when

| Workflow | Schedule | Does |
|---|---|---|
| `daily.yml` | 5am Sydney, every day | Sweeps new ACN slots + re-checks the trailing window, rebuilds the site |
| `weekly.yml` | Wednesday ~8am Sydney | Downloads the published register, extracts every role-named vehicle, **reconciles it against what the daily sweep found** |

Cron cannot handle daylight saving, so `daily.yml` fires at both 18:00 and 19:00
UTC and the first step drops whichever one is not 5am in Sydney.

"Run now" on the dashboard links to the Actions page — press *Run workflow*
there. The button deliberately does not carry a token; a page that could start a
job would need one embedded in it.

---

## The method

An ACN is nine digits: eight base digits plus a check digit,
`check = (10 - Σ(wᵢdᵢ) mod 10) mod 10` with weights `8 7 6 5 4 3 2 1`.
Exactly **one integer in ten** is a valid ACN. That tenfold reduction is
arithmetic, done offline, before any request is made.

Each run covers two windows:

**Forward** — from the last run's high-water mark to today's issuance frontier,
found by binary search. Walked every-second-slot-first, then the gaps. That is an
ordering, not a sample: every slot is visited either way, but a run cut short
still spans the whole range, and you only need one member of a family to notice
the family.

**Trailing** — the previous 20,000 slots, re-querying only the ones that came
back *empty* before. This is not belt-and-braces. ACNs are allocated ahead of
use, so a company registered today can carry a number issued days ago, sitting in
a slot an earlier run already found empty. Measured against the register over
1 Jul – 6 Sep 2026 this cost **11 of 211 role-named vehicles (5.2%)**, including
three complete stacks: TRIDENT, DANONE and PULSE.

Resolved companies are grouped by name stem — role word and legal suffix stripped.
Two or more sharing a stem with at least one Bidco is a **stack**: a real
structure with a target behind it. A lone Bidco is kept separately.

---

## Things that are true and cost time to learn

- **The lookup API is throttled to ~50 requests/minute** regardless of plan. Ten
  concurrent requests all return 429. The sweep is deliberately serial and paced;
  adding threads produces nothing but errors. Budget ~100 minutes per 5,000 slots.
- **The User-Agent is mandatory.** Cloudflare rule 1010 rejects urllib's default.
  `bidco-watch/1.0 (+nik@withbureau.com)` is an honest identifier, not a disguise.
  If the vendor ever blocks us, the right response is to stop and ask them.
- **Never filter on ABN.** Excluding ABN-holders once discarded three real stacks
  (GELATO, CAPRA, VALLEY).
- **MEZZCO belongs in the role vocabulary.** Leaving it out made
  `AVOCA AUS MEZZCO` invisible.
- **The endpoint covers every ASIC register.** Business names are about two
  thirds of what comes back and are filtered out by entity type.
- **Every lookup is checkpointed as it returns.** Runs get killed. This one
  resumes; it never restarts.

---

## The passcode, honestly

`build_site.py` encrypts the whole data payload with AES-256-GCM under a key
derived from `SITE_PASSCODE` (PBKDF2-SHA256, 600,000 rounds). The page published
to Pages contains ciphertext and nothing else — no names, no ACNs, no dates. Open
it without the passcode and there is genuinely nothing to read.

**But a four-digit PIN is 10,000 possibilities.** Anyone who downloads the page
can grind through all of them offline; the slow key derivation makes that take
hours rather than seconds, not centuries. It keeps out people who wander past the
URL, which is what it is for. It is not a lock.

Set `SITE_PASSCODE` to a longer passphrase and the same machinery becomes
genuinely strong — nothing else changes. If you want real access control instead,
make the repo private and serve `docs/` through Cloudflare Pages with Cloudflare
Access in front of it; the free tier covers it.

Also worth saying plainly: the underlying data is public. It is the ASIC register.
What the passcode protects is *which vehicles you are watching*, which is the part
that is actually yours.

---

## Layout

```
sweep.py          the daily pipeline
weekly.py         register download + reconciliation
build_site.py     encrypts the payload, renders docs/index.html
template.html     the dashboard (Dashboard / Audit / Runs)
data/
  state.json          frontier, window, run history
  lookups.jsonl.gz    checkpoint, bounded to the last 80,000 slots
  daily.json          stacks, lone Bidcos, role matches
  weekly.json         register extract + reconciliation
  companies.csv       every company resolved in the current checkpoint
  weekly_vehicles.csv every role-named vehicle in the register window
docs/index.html   what Pages serves
```
