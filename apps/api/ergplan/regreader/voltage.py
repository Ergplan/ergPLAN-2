"""Supply-voltage phrases as tariff orders print them → scope voltage bands (kV)."""

from __future__ import annotations

import re
from typing import Any

_KV = r"(\d+(?:\.\d+)?)\s*k\s*v(?![a-z])"  # kV, never kVA or kVAh


def parse_band(text: str | None) -> dict[str, Any] | None:
    """'up to 11 kV' → {max: 11}; 'at 11kV' / '11 kV' → {min: 11, max: 11};
    'above 11 kV and up to 66 kV' → {min: 11, min_exclusive: true, max: 66};
    'above 132 kV' → {min: 132, min_exclusive: true}; '11 kV & above' → {min: 11};
    'at 11 kV up to 66 kV' / 'from 11 kV to 66 kV' → {min: 11, max: 66} (whether 11 kV itself is in
    the band is settled against the table's other rows by `disjoint`).
    None when the text names no voltage."""
    if not text:
        return None
    t = text.lower().replace("–", "-")
    nums = [float(x) for x in re.findall(_KV, t)]
    if not nums:
        return None
    band: dict[str, Any] = {}

    def num(x: float) -> float | int:
        return int(x) if x == int(x) else x

    above = re.search(r"(above|more than|exceeding|beyond)\s*" + _KV, t)
    upto = re.search(r"(up\s*to|upto|below|not exceeding|less than)\s*(and including\s*)?" + _KV, t)
    and_above = re.search(_KV + r"\s*(&|and)\s*above", t)
    if above:
        band["min"] = num(float(above.group(2)))
        band["min_exclusive"] = True
    if and_above:
        band["min"] = num(float(and_above.group(1)))
    if upto:
        band["max"] = num(float(upto.group(3)))
        if upto.group(1).startswith(("below", "less")):
            band["max_exclusive"] = True
    span = re.search(r"(at|from|between)\s*" + _KV + r"\s*(and\s*)?(up\s*to|upto|to|and|-)\s*" + _KV, t)
    if span and not above and not and_above:
        lo, hi = float(span.group(2)), float(span.group(5))
        if lo < hi:
            band = {"min": num(lo), "max": num(hi)}
    if not band and len(nums) == 1:
        band = {"min": num(nums[0]), "max": num(nums[0])}
    return band or None


def band_key(band: dict[str, Any] | None) -> str | None:
    """A voltage key for value_by_voltage: the band's single or upper voltage ('11', '33')."""
    if not band:
        return None
    v = band.get("max", band.get("min"))
    return None if v is None else str(v)


def disjoint(bands: list[dict[str, Any] | None]) -> list[dict[str, Any] | None]:
    """The bands of one table's rows, made not to overlap where two of them share an edge: a row
    printed for one voltage ('at 11 kV') keeps it, and the neighbouring range ('at 11 kV up to
    66 kV') is read as starting above or ending below it; between two ranges ('up to 66 kV',
    'at 66 kV up to 132 kV') the lower one keeps the shared voltage, as 'up to' says. Nothing else
    is changed: bands that overlap by more than an edge are left for a person."""
    out = [dict(b) if b else None for b in bands]

    def point(b: dict[str, Any]) -> bool:
        return "min" in b and b.get("min") == b.get("max")

    for a in out:
        for b in out:
            if a is None or b is None or a is b:
                continue
            if point(a) and not point(b):
                if b.get("min") == a["min"] and not b.get("min_exclusive"):
                    b["min_exclusive"] = True
                if b.get("max") == a["min"] and not b.get("max_exclusive"):
                    b["max_exclusive"] = True
            elif not point(a) and not point(b):
                shared = a.get("max") is not None and a.get("max") == b.get("min")
                if shared and not a.get("max_exclusive") and not b.get("min_exclusive"):
                    b["min_exclusive"] = True
    return out
