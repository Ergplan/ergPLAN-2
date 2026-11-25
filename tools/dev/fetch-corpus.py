#!/usr/bin/env python3
"""Fetch the regulation library from the regulators' own sites (Stage 11: the PDFs live on the Mac
mini, not on the public VM).

  python3 tools/dev/fetch-corpus.py            # download into data/corpus/India_DISCOM_Tariff_Orders_Final_2026/<the index's path>
  python3 tools/dev/fetch-corpus.py --list     # what the approved rulebooks cite, and what is here

For every entry of packages/rulebooks/corpus/tariff-library.json that has a regulator URL, the file
is downloaded and its document id computed from its SHA-256 (core/ids.py: the same bytes have the
same id on every machine). A file is kept only when that id is one the committed rulebooks cite
(packages/rulebooks/states/**), so a regulator's re-issued or re-compressed copy can never stand in
for the page a rule quotes. Kept files are registered with

  python -m ergplan.documents.corpus --root /data/corpus/India_DISCOM_Tariff_Orders_Final_2026

Anything cited and not fetched has to come from the Mac mini (docs/stage-reports/stage-11.md).
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
INDEX = ROOT / "packages" / "rulebooks" / "corpus" / "tariff-library.json"
STATES = ROOT / "packages" / "rulebooks" / "states"
DEST = ROOT / "data" / "corpus" / "India_DISCOM_Tariff_Orders_Final_2026"  # the Mac mini's folder name
NAMESPACE = uuid.UUID("6f1b2a0e-5d6c-4c2b-9a51-6a2f1d3e0b77")  # ergplan/core/ids.py DOCUMENT_NAMESPACE
AGENT = "Mozilla/5.0 (X11; Linux x86_64) regulation-library-fetch"


def document_id(sha256: str) -> str:
    return str(uuid.uuid5(NAMESPACE, sha256.lower()))


def cited() -> dict[str, set[str]]:
    """document id → the rulebook files that cite it."""
    out: dict[str, set[str]] = {}
    for p in sorted(STATES.rglob("*.yaml")):
        for m in re.finditer(r"document_id:\s*([0-9a-f-]{36})", p.read_text(encoding="utf-8")):
            out.setdefault(m.group(1), set()).add(str(p.relative_to(STATES)))
    return out


def main() -> int:
    want = cited()
    entries = json.loads(INDEX.read_text(encoding="utf-8"))["entries"]
    have: dict[str, Path] = {}
    for p in DEST.rglob("*.pdf") if DEST.exists() else []:
        have[document_id(hashlib.sha256(p.read_bytes()).hexdigest())] = p
    if "--list" not in sys.argv:
        for e in entries:
            url, target = e.get("provenance_url"), DEST / e["path"]
            if not url or target.exists():
                continue
            try:
                req = urllib.request.Request(url, headers={"User-Agent": AGENT})
                with urllib.request.urlopen(req, timeout=120) as r:  # noqa: S310 (regulator URLs from the index)
                    data = r.read()
            except Exception as err:  # a regulator's site being down is reported, not fatal
                print(f"not fetched  {e['path']}: {type(err).__name__}: {err}")
                continue
            doc = document_id(hashlib.sha256(data).hexdigest())
            if not data.startswith(b"%PDF"):
                print(f"not a PDF    {e['path']} ({url})")
            elif doc not in want:
                print(f"not the cited file (id {doc[:8]}…, not kept)  {e['path']}")
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(data)
                have[doc] = target
                print(f"kept         {e['path']} → {doc}")
    print(f"\n{len(want)} documents are cited by the committed rulebooks; {len(set(want) & set(have))} are in {DEST}:")
    for doc, files in sorted(want.items(), key=lambda kv: sorted(kv[1])[0]):
        where = have[doc].relative_to(DEST) if doc in have else "MISSING"
        print(f"  {doc}  {where}\n      cited by {', '.join(sorted(files)[:4])}{' …' if len(files) > 4 else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
