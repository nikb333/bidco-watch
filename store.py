#!/usr/bin/env python3
"""
Encrypted data store.

The repository can be public — that is what makes GitHub Pages free — so nothing
readable may be committed to it. Everything the sweep produces (the checkpoint,
the run state, the stacks it found, the register extract) is bundled into a single
AES-256-GCM blob at data/store.enc under the same passcode that unlocks the site.
The plaintext files exist only inside the running job and are gitignored.

    python store.py unlock    # store.enc -> data/*.json, *.csv, lookups.jsonl.gz
    python store.py lock      # those files -> store.enc

Both are no-ops if there is nothing to do, so a first run works with no store.
"""
from __future__ import annotations

import base64
import io
import os
import secrets
import sys
import tarfile
from pathlib import Path

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
ENC = DATA / "store.enc"
ITERS = 600_000
MEMBERS = ["state.json", "daily.json", "weekly.json",
           "lookups.jsonl.gz", "companies.csv", "weekly_vehicles.csv"]


def passcode() -> str:
    p = os.environ.get("SITE_PASSCODE")
    if not p:
        print("SITE_PASSCODE is not set", file=sys.stderr)
        sys.exit(2)
    return p


def key_for(salt: bytes, p: str) -> bytes:
    return PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt,
                      iterations=ITERS).derive(p.encode())


def lock() -> None:
    DATA.mkdir(exist_ok=True)
    present = [m for m in MEMBERS if (DATA / m).exists()]
    if not present:
        print("nothing to lock")
        return
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for m in present:
            tar.add(DATA / m, arcname=m)
    salt, iv = secrets.token_bytes(16), secrets.token_bytes(12)
    ct = AESGCM(key_for(salt, passcode())).encrypt(iv, buf.getvalue(), None)
    ENC.write_text(base64.b64encode(salt + iv + ct).decode())
    print(f"locked {len(present)} files -> data/store.enc ({ENC.stat().st_size:,} bytes)")


def unlock() -> None:
    if not ENC.exists():
        print("no store yet — starting fresh")
        return
    raw = base64.b64decode(ENC.read_text())
    salt, iv, ct = raw[:16], raw[16:28], raw[28:]
    try:
        blob = AESGCM(key_for(salt, passcode())).decrypt(iv, ct, None)
    except Exception:
        print("could not decrypt data/store.enc — SITE_PASSCODE does not match the "
              "one it was written with", file=sys.stderr)
        sys.exit(3)
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as tar:
        names = tar.getnames()
        tar.extractall(DATA)
    print(f"unlocked {len(names)} files from data/store.enc: {', '.join(names)}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    if cmd == "lock":
        lock()
    elif cmd == "unlock":
        unlock()
    else:
        print(__doc__)
        sys.exit(1)
