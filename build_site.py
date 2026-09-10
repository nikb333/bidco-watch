#!/usr/bin/env python3
"""
Render docs/index.html for GitHub Pages.

The data payload is encrypted with a passcode before it is written, so the page
that ships to Pages contains ciphertext and nothing else. Someone who opens the
page or reads its source without the passcode sees no company names, no ACNs and
no dates.

Be clear-eyed about what that buys. A four-digit PIN is 10,000 possibilities;
somebody who downloads the page can grind through all of them offline. The
600,000-round key derivation makes that slow rather than impossible. It keeps out
anyone who wanders past the URL, which is the stated goal. Set SITE_PASSCODE in
the repo to a longer passphrase and the same machinery becomes genuinely strong —
nothing else has to change.
"""
from __future__ import annotations

import base64
import json
import os
import secrets
import sys
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DOCS = ROOT / "docs"
ITERS = 600_000
PASSCODE = os.environ.get("SITE_PASSCODE", "9090")


def encrypt(plaintext: str, passcode: str) -> str:
    salt = secrets.token_bytes(16)
    iv = secrets.token_bytes(12)
    key = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                     iterations=ITERS).derive(passcode.encode())
    ct = AESGCM(key).encrypt(iv, plaintext.encode("utf-8"), None)
    return base64.b64encode(salt + iv + ct).decode()


def load(name, default):
    p = DATA / name
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def main() -> int:
    daily = load("daily.json", {"run": {}, "stacks": [], "singles": [], "role_only": []})
    weekly = load("weekly.json", {"entities": [], "bidcos": 0, "renamed": [],
                                  "reconciliation": {}})
    state = load("state.json", {"history": []})

    run = daily.get("run") or state.get("last_run") or {}
    payload = {
        "built_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "run": run,
        "history": (state.get("history") or [])[-14:],
        "stacks": daily.get("stacks", []),
        "singles": daily.get("singles", []),
        "role_only": daily.get("role_only", []),
        "weekly": {
            "generated_utc": weekly.get("generated_utc", ""),
            "dataset": weekly.get("dataset", ""),
            "window_days": weekly.get("window_days"),
            "entities": weekly.get("entities", []),
            "bidcos": weekly.get("bidcos", 0),
            "max_acn": weekly.get("max_acn", ""),
            "stacks": weekly.get("stacks", 0),
            "stacks_without_bidco": weekly.get("stacks_without_bidco", 0),
            "renamed": weekly.get("renamed", []),
            "reconciliation": weekly.get("reconciliation", {}),
        },
        "repo": os.environ.get("GITHUB_REPOSITORY", ""),
    }

    blob = encrypt(json.dumps(payload, separators=(",", ":")), PASSCODE)
    tpl = (ROOT / "template.html").read_text(encoding="utf-8")
    html = tpl.replace("__PAYLOAD__", blob).replace("__ITERS__", str(ITERS))
    DOCS.mkdir(exist_ok=True)
    (DOCS / "index.html").write_text(html, encoding="utf-8")
    (DOCS / ".nojekyll").write_text("")
    print(f"docs/index.html  {len(html):,} bytes  "
          f"({len(payload['stacks'])} stacks, {len(payload['weekly']['entities'])} weekly vehicles)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
