"""Rates, hours and adjustments as tariff orders print them, read into a value and a unit of
core/units.py. Deterministic and deliberately narrow: a cell that states more than one number, a
unit this module does not know (per BHP, per day, per connection) or an adjustment without its sign
reads as None, and the caller lists the row as unmapped. Nothing is inferred."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from ..core.units import Unit, convert
from .tariff_oder import _band, _months

_NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?:\.\d+)?")
_RUPEE = re.compile(r"(?<![a-z])(rs|inr|rupees?)(?![a-z])|₹")
_PAISE = re.compile(r"(?<![a-z])(paise|paisa|ps)(?![a-z])")
_PERCENT = re.compile(r"%|(?<![a-z])(percent|per\s*cent)(?![a-z])")
_BLANK = re.compile(r"^\s*(-+|–|—|nil|n\.?a\.?|not applicable)?\s*$", re.I)

# what a rate is per, as the words printed after the amount; the first set that fits wins
_PER: list[tuple[tuple[str, ...], tuple[str, ...], Unit, Unit | None]] = [
    # (all of these words, none of these words, unit in rupees, unit in paise)
    (("kvah",), (), Unit.INR_PER_KVAH, Unit.PAISE_PER_KVAH),
    (("mwh",), (), Unit.INR_PER_MWH, None),
    (("kwh",), (), Unit.INR_PER_KWH, Unit.PAISE_PER_KWH),
    (("unit",), ("kva", "kw", "mw"), Unit.INR_PER_KWH, Unit.PAISE_PER_KWH),
    (("kva", "month"), (), Unit.INR_PER_KVA_MONTH, None),
    (("kw", "month"), (), Unit.INR_PER_KW_MONTH, None),
    (("mw", "month"), (), Unit.INR_PER_MW_MONTH, None),
    (("mw", "day"), (), Unit.INR_PER_MW_DAY, None),
]
# words that make a rate something else than the units above (per horsepower, per day, per connection …)
_OTHER_BASIS = {"bhp", "hp", "day", "shop", "connection", "consumer", "point", "lamp", "year", "annum"}
_RUPEE_OF = {
    Unit.PAISE_PER_KWH: Unit.INR_PER_KWH,
    Unit.PAISE_PER_KVAH: Unit.INR_PER_KVAH,
}


@dataclass(frozen=True)
class Rate:
    value: Decimal  # the number as printed (signed for an adjustment)
    unit: Unit  # the unit as printed

    def in_rupees(self) -> Rate:
        """Paise as rupees (core/units.py does the arithmetic); anything else unchanged."""
        to = _RUPEE_OF.get(self.unit)
        return self if to is None else Rate(convert(self.value, self.unit, to), to)

    def text(self) -> str:
        return format(self.value, "f")


def _norm(text: str) -> str:
    t = unicodedata.normalize("NFKC", text).lower()
    return re.sub(r"\s+", " ", re.sub(r"[‐-―−]", "-", t)).strip()


def is_blank(text: str) -> bool:
    """A cell that prints nothing: empty, a dash, 'Nil', 'NA'."""
    return bool(_BLANK.match(unicodedata.normalize("NFKC", text)))


def number(text: str) -> Decimal | None:
    """The one number a cell prints ('1,43,013.60' → 143013.60; '(12.29%)' → -12.29). None when the
    cell prints no number or more than one."""
    t = _norm(text)
    found = _NUMBER.findall(t)
    if len(found) != 1:
        return None
    try:
        v = Decimal(found[0].replace(",", ""))
    except InvalidOperation:
        return None
    negative = bool(re.search(r"\(\s*-\s*\)|(?<![\w)])-\s*\d", t)) or bool(
        re.fullmatch(r"\(\s*[\d,.]+\s*%?\s*\)", t)
    )
    return -v if negative else v


def unit_of(text: str) -> Unit | None:
    """The unit a cell or a column heading names: 'Rs. /kWh', 'Rs. 300.00 / kVA / month',
    'Paise per unit', '% of Energy Charges'. None when it names none of core/units.py's rates."""
    t = _norm(text)
    words = set(re.findall(r"[a-z]+", t))
    paise, rupee = bool(_PAISE.search(t)), bool(_RUPEE.search(t))
    if paise or rupee:
        if words & _OTHER_BASIS:
            return None
        for needs, excludes, in_rupees, in_paise in _PER:
            if all(w in words for w in needs) and not any(w in words for w in excludes):
                return in_paise if paise and not rupee else in_rupees
        return None
    if _PERCENT.search(t):
        return Unit.PERCENT
    return None


def rate(text: str, heading: str | None = None) -> Rate | None:
    """A printed rate: 'Rs. 300.00 / kVA / month', 'Rs. 7.10 / kVAh', '650 paise/kWh', '1.00%'.
    A bare number takes its unit from `heading` (the column heading or the row's units cell:
    '0.9318' under 'Rs. /kWh'). None when the cell does not state exactly one number with a unit
    this module knows."""
    if is_blank(text):
        return None
    t = _norm(text)
    unit = unit_of(t)
    if unit is None:
        has_words = bool(re.search(r"[a-z]{2,}", re.sub(r"\b(rs|inr)\b", "", t)))
        if has_words or heading is None:
            return None  # the cell names a basis of its own that is not a known unit
        unit = unit_of(heading)
        if unit is None:
            return None
    v = number(t)
    if v is None:
        return None
    return Rate(v, unit)


def adjustment_pct(text: str) -> Decimal | None:
    """A time-of-day adjustment on the energy charge, signed: '(-) 15%' → -15, '(+) 15%' → 15,
    '0%' or '0' → 0. A percentage printed without its sign is not read (None)."""
    t = _norm(text)
    found = _NUMBER.findall(t)
    if len(found) != 1:
        return None
    v = Decimal(found[0].replace(",", ""))
    if re.search(r"[a-z]{2,}", t):
        return None  # words in the cell ('120% of normal rate'): not a plain adjustment
    if v == 0:
        return Decimal(0)
    if re.search(r"\(\s*-\s*\)|^-", t):
        return -v
    if re.search(r"\(\s*\+\s*\)|^\+", t):
        return v
    return None


_TIME = re.compile(
    r"(?<![\d:.])(\d{1,2})(?:\s*[:.]\s*(\d{2})|(\d{2}))?\s*(a\.?\s*m\.?|p\.?\s*m\.?)?(?![\d:])",
)


def hours(text: str) -> tuple[str, str] | None:
    """A time band as 'HH:MM' start and end: '05:00 hrs – 10:00 hrs', '0600 hrs to 1000 hrs',
    '6 AM to 10 PM', '22:00 – 24:00'. None unless the text states exactly two clock times."""
    simple = _band(text)
    t = _norm(text)
    clock = re.search(r"\d{1,2}\s*[:.]\s*\d{2}|\b\d{4}\b|\bh(ou)?rs?\b|\d\s*[ap]\.?\s*m\b", t)
    if not clock or _months(t):
        return None  # a date range ('1st April to 15th June') or no clock time at all
    found = [m for m in _TIME.finditer(t)]
    if len(found) != 2:
        return None
    out = []
    for m in found:
        h, mm = int(m.group(1)), int(m.group(2) or m.group(3) or 0)
        half = re.sub(r"[\s.]", "", m.group(4) or "")
        if half:
            if h < 1 or h > 12:
                return None
            h = (h % 12) + (12 if half == "pm" else 0)
        if h > 24 or mm > 59 or (h == 24 and mm):
            return None
        out.append(f"{h:02d}:{mm:02d}")
    start = "00:00" if out[0] == "24:00" else out[0]
    end = "24:00" if out[1] == "00:00" else out[1]
    meridiem = all(m.group(4) for m in found)  # '5.00 p.m.': the simple reading knows no a.m./p.m.
    if simple and not meridiem and (simple[0][:2], simple[1][:2]) != (out[0][:2], out[1][:2]):
        return None  # two readings of the same text disagree: not read
    return start, end


_FLAT = re.compile(r"^all\s+units\s*[-:]?\s*(\d[\d,]*(?:\.\d+)?)$")


def flat_rate(text: str, heading: str | None) -> Rate | None:
    """A rate printed for every unit under a heading that carries the unit: 'All units – 798' under
    'Energy charges (Paise / kVAh)'. The dash is punctuation, not a sign. None for anything else."""
    m = _FLAT.match(_norm(text))
    if not m or heading is None:
        return None
    unit = unit_of(heading)
    if unit is None or unit == Unit.PERCENT:
        return None
    return Rate(Decimal(m.group(1).replace(",", "")), unit)


_TRAILING = re.compile(
    r"^(.*?\D)\s*((?:rs\.?\s*)?\d[\d,]*(?:\.\d+)?\s*(?:/-)?\s*(?:paise|paisa|ps\.?|per)\b.*)$"
)


def trailing_rate(text: str) -> tuple[str, Rate] | None:
    """A line of a schedule printed as words and then the rate: '(a) Up to 500 kVA of billing demand
    400 Paise per unit' → ('(a) up to 500 kva of billing demand', 400 paise/kWh). The words come back
    normalised (lower case, single spaces). None unless the line ends in exactly one rate with a unit
    this module knows."""
    m = _TRAILING.match(_norm(text))
    if not m:
        return None
    got = rate(m.group(2))
    if got is None or got.unit == Unit.PERCENT:
        return None
    return m.group(1).strip(), got


_OF_NORMAL = re.compile(
    r"^(?:(\d+(?:\.\d+)?)\s*%\s*(?:of\s+)?)?normal\s+rate(?:\s+of\s+energy\s+charges?)?\.?$"
)


def of_normal_pct(text: str) -> Decimal | None:
    """A time-of-day rate printed as a share of the normal rate, as a signed adjustment:
    '80% normal rate of energy charges' → -20, '120% of normal rate of energy charges' → 20,
    'Normal rate of energy charges' → 0. None for anything else (two shares in one cell included)."""
    m = _OF_NORMAL.match(_norm(text))
    if not m:
        return None
    return Decimal(m.group(1)) - Decimal(100) if m.group(1) else Decimal(0)
