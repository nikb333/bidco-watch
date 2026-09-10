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
   | `BAPI_KEY` | your Business API key |
   | `SITE_PASSCODE` | the passcode that unlocks the dashboard **and encrypts the data store** |
   | `CONTACT_EMAIL` | *(optional)* an address to put in the User-Agent |

2. **Pages** — Settings → Pages → Source: *Deploy from a branch*, Branch: `main`,
   folder `/docs`.

3. **Actions write access** — Settings → Actions → General → Workflow permissions:
   *Read and write permissions*. The workflows commit results back to the repo.

4. Run **Actions → Daily ACN sweep → Run workflow** once to seed the data.

---

## What runs, and when

| Workflow | Schedule | Does |
|---|---|---|
| `daily.yml` | **10pm Sydney, every day** | Sweeps new ACN slots + the trailing window, rebuilds the site. Runs overnight so the morning starts with a finished sweep. |
| `weekly.yml` | Wednesday ~8am Sydney | Downloads the published register, extracts every role-named vehicle, **reconciles it against what the daily sweep found** |

Cron cannot handle daylight saving, so `daily.yml` fires at both UTC candidates
for each Sydney hour and the first step drops the wrong one.

**There is no artificial time cap.** GitHub caps a hosted job at six hours and
that is not ours to raise. A job also cannot dispatch itself to get around it —
events raised with the built-in token deliberately do not start new runs. So the
workflow is scheduled three times a night instead: **10pm, 2am and 5am Sydney**.
The sweep is resumable by construction, so each start simply continues the last,
and if the 10pm run finished everything the other two find an empty queue and
exit in minutes. That is up to sixteen hours of sweeping available per night
against a typical need of two to five.

While a sweep is running the dashboard shows a live count: how many slots are
done, how many companies have come back, and roughly how long is left. That comes
from `docs/progress.json`, which the sweep writes every 50 lookups and pushes
every ten minutes, cross-checked against the Actions API so a crashed run cannot
leave a stale bar creeping along. `progress.json` is **not** encrypted — it holds
counters and nothing else, no names or ACNs, and hiding them would defeat its
purpose.

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

**Trailing** — the previous 20,000 slots, in two priorities. First **gaps**:
slots never visited at all, which is what a run cut short leaves behind. Then
**re-checks**: slots that were visited and came back empty. Gaps first, because a
company in a never-visited slot has been seen by nothing at all, and because a
run that runs out of time should sacrifice re-checks rather than gaps.

The re-check half is not belt-and-braces. ACNs are allocated ahead of
use, so a company registered today can carry a number issued days ago, sitting in
a slot an earlier run already found empty. Measured against the register over
1 Jul – 6 Sep 2026 this cost **11 of 211 role-named vehicles (5.2%)**, including
three complete stacks: TRIDENT, DANONE and PULSE.

Resolved companies are grouped by name stem — role word and legal suffix stripped.
**Two or more role-named companies sharing a stem is a stack**, whether or not a
Bidco is among them: over six months of the register, 24 of 58 such families had
no Bidco in any name, including ZELORA (TOPCO/MEZZCO/HOLDCO), GANZ, EPTEC INFRA
and TUGUN BUYER. Requiring the word discarded 41% of the structures. Stacks that
do contain a Bidco are flagged, since they remain the strongest single signal.

The dashboard shows stacks and Bidcos only. Lone HOLDCOs and stray FINCOs are
real but weak on their own, so they sit in a collapsed section rather than on the
front page.

**Shelf companies** are re-queried for 120 days. A company registered as
`A.C.N. 698 332 240 PTY LTD` and renamed weeks later is invisible to a sweep that
only reads the name on registration day — that is exactly how ZELORA was missed.
About six are registered per business day, so the rolling window costs roughly
eight minutes a night.

---

## Things that are true and cost time to learn

- **The lookup API is throttled to ~50 requests/minute** regardless of plan. Ten
  concurrent requests all return 429. The sweep is deliberately serial and paced;
  adding threads produces nothing but errors. Budget ~100 minutes per 5,000 slots.
- **The User-Agent is mandatory.** Cloudflare rule 1010 rejects urllib's default.
  `bidco-watch/1.0` is an honest identifier, not a disguise. Set `CONTACT_EMAIL`
  and it is appended, so the vendor can reach you rather than just blocking you.
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

Two things are encrypted under `SITE_PASSCODE`, both AES-256-GCM with a
PBKDF2-SHA256 key at 600,000 rounds:

- **`docs/index.html`** — the whole data payload. The published page carries
  ciphertext and nothing else: no names, no ACNs, no dates.
- **`data/store.enc`** — the checkpoint, run state, findings and register
  extract, bundled and encrypted before every commit. The plaintext versions are
  gitignored and exist only inside a running job.

That is what lets the repo be **public**, which is what keeps GitHub Pages free.
Nothing in it is readable without the passcode.

**But a four-digit PIN is 10,000 possibilities.** Anyone who clones the repo can
grind through all of them offline; the slow key derivation makes that hours
rather than seconds, not centuries. It reliably keeps out anyone who wanders past
the URL, which is what it is for. It is not a lock.

Set `SITE_PASSCODE` to a longer passphrase and the same machinery becomes
genuinely strong — no code changes. **If you change it, run the daily workflow
by hand once straight afterwards**: the store is re-encrypted under the new
passcode on the next successful run, and until then the old `store.enc` cannot be
opened. If you ever lose the passcode, delete `data/store.enc` and the next run
rebuilds from scratch.

Worth saying plainly: the underlying data is public — it is the ASIC register.
What the passcode protects is *which vehicles you are watching*, which is the
part that is actually yours.

## Layout

```
sweep.py          the daily pipeline
weekly.py         register download + reconciliation
build_site.py     encrypts the payload, renders docs/index.html
template.html     the dashboard (Dashboard / Audit / Runs)
store.py          encrypts/decrypts the data bundle around each run
data/
  store.enc         THE ONLY DATA FILE COMMITTED — encrypted bundle of:
      state.json          frontier, window, run history
      lookups.jsonl.gz    checkpoint, bounded to the last 80,000 slots
      daily.json          stacks, lone Bidcos, role matches
      weekly.json         register extract + reconciliation
docs/index.html   what Pages serves
```
