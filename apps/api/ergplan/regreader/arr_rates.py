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
    if simple and (simple[0][:2], simple[1][:2]) != (out[0][:2], out[1][:2]):
        return None  # two readings of the same text disagree: not read
    return start, end


def rate_under(text: str, heading: str) -> Rate | None:
    """A cell that repeats part of its column's unit: 'Rs. 400 per kVA' under 'Fixed Charge
    (Rs./kVA/ Month)' is 400 in the heading's unit. Read only when every unit word of the cell is
    also in the heading and the heading names a unit this module knows; otherwise None."""
    if is_blank(text):
        return None
    unit = unit_of(heading)
    if unit is None or unit == Unit.PERCENT:
        return None
    t = _norm(text)
    words = set(re.findall(r"[a-z]+", t)) - {"rs", "inr", "rupee", "rupees", "per"}
    if not words or words & _OTHER_BASIS or not words <= set(re.findall(r"[a-z]+", _norm(heading))):
        return None
    v = number(t)
    return None if v is None else Rate(v, unit)


_MONTH_NAMES = (
    ("jan", "january"), ("feb", "february"), ("mar", "march"), ("apr", "april"), ("may",),
    ("jun", "june"), ("jul", "july"), ("aug", "august"), ("sep", "sept", "september"),
    ("oct", "october"), ("nov", "november"), ("dec", "december"),
)  # fmt: skip
_MONTH_NUMBER = {name: i + 1 for i, names in enumerate(_MONTH_NAMES) for name in names}
_SEASON_PART = re.compile(r"([+-]?\s*\d[\d.]*\s*%)\s*\(\s*([a-z]+)\.?\s*(?:to|-)\s*([a-z]+)\.?\s*\)")


def month_span(first: str, last: str) -> list[int] | None:
    """'Apr', 'Sept' → [4, 5, 6, 7, 8, 9]; 'October', 'March' → [10, 11, 12, 1, 2, 3]."""
    a, b = _MONTH_NUMBER.get(first.lower().strip(".")), _MONTH_NUMBER.get(last.lower().strip("."))
    if a is None or b is None:
        return None
    return list(range(a, b + 1)) if a <= b else [*range(a, 13), *range(1, b + 1)]


def seasonal_adjustments(text: str) -> list[tuple[Decimal, list[int]]] | None:
    """A time-of-day adjustment that changes with the season, printed in one cell:
    '-15% (April to September) -25% (October to March)' → [(-15, [4 … 9]), (-25, [10, 11, 12, 1, 2, 3])].
    None unless the cell is nothing but signed percentages, each with its months, and the seasons
    together are the twelve months, none of them twice."""
    t = _norm(text)
    parts = list(_SEASON_PART.finditer(t))
    if len(parts) < 2 or _SEASON_PART.sub("", t).strip():
        return None
    out: list[tuple[Decimal, list[int]]] = []
    for m in parts:
        pct = adjustment_pct(m.group(1).replace(" ", ""))
        months = month_span(m.group(2), m.group(3))
        if pct is None or months is None:
            return None
        out.append((pct, months))
    if sorted(mo for _, months in out for mo in months) != list(range(1, 13)):
        return None
    return out


# ------------------------------------------------------------------ bands of load, slabs, joined cells

_LOAD = r"(\d[\d,]*(?:\.\d+)?)\s*(kva|kw)(?![a-z])"
_LOAD_FIELD = {"kva": "contract_demand_kva", "kw": "load_kw"}
_LIST_MARK = re.compile(r"^\(?([ivx]{1,4}|[a-z]|\d{1,2})\)\s*")
_SLAB = re.compile(
    r"^(\d[\d,]*\s*-\s*\d[\d,]*|(above|up\s*to|upto|first|next|beyond|balance|>)\s*\d[\d,]*)"
    r"\s*(units?|kwh|kvah)?(\s*(per|/)\s*month)?$"
)


def load_band(text: str) -> tuple[str, dict[str, int | float | bool]] | None:
    """A band of contract demand or load as a schedule prints it against a rate: 'Above 100 kVA and
    upto 1000 kVA' → ('contract_demand_kva', {min: 100, min_exclusive: true, max: 1000}); 'Upto 10 KW'
    → ('load_kw', {max: 10}); 'Above 2500 kVA' → (…, {min: 2500, min_exclusive: true}). None unless the
    text is nothing but such a band in one unit (a list mark such as 'ii)' before it is allowed)."""
    t = _LIST_MARK.sub("", _norm(text))
    t = re.sub(r"(?<=[a-z])(?=\d)", " ", t)  # 'upto1000 kVA', as a text layer runs it together
    m = re.fullmatch(
        rf"(?:(above|exceeding|more than)\s*{_LOAD})?\s*(?:and|&|but)?\s*"
        rf"(?:(up\s*to|upto|not exceeding)\s*{_LOAD})?",
        t,
    )
    if not m or not (m.group(1) or m.group(4)):
        return None
    units = {u for u in (m.group(3), m.group(6)) if u}
    if len(units) != 1:
        return None

    def num(s: str) -> int | float:
        v = float(s.replace(",", ""))
        return int(v) if v == int(v) else v

    band: dict[str, int | float | bool] = {}
    if m.group(1):
        band["min"] = num(m.group(2))
        band["min_exclusive"] = True
    if m.group(4):
        band["max"] = num(m.group(5))
    return _LOAD_FIELD[units.pop()], band


def is_slab(text: str) -> bool:
    """A row of a consumption slab ('0-50', '101-200', 'Above 500 units', 'ii) Upto 300 kWh'): its
    rate applies to part of the month's units, which a one-rate row cannot hold."""
    return bool(_SLAB.match(_LIST_MARK.sub("", _norm(text))))


def joined(text: str, heading: str) -> tuple[Rate, Rate] | None:
    """An energy charge and a fixed charge printed in one cell, as a schedule set side by side for
    two years prints them: '695/kVAh Rs. 290/kVA /Month Nil' under a heading that says the energy
    charge is in paise → (695 paise/kVAh, Rs. 290/kVA/month). None unless the cell states exactly
    one of each and no other number; rupees or paise for the energy charge come from the heading."""
    t, h = _norm(text), _norm(heading)
    energy = re.findall(r"(?<![\w./])(\d[\d,]*(?:\.\d+)?)\s*/\s*(kwh|kvah)(?![a-z])", t)
    fixed = re.findall(r"rs\.?\s*(\d[\d,]*(?:\.\d+)?)\s*/\s*(kva|kw)\s*/\s*month", t)
    if len(energy) != 1 or len(fixed) != 1 or len(_NUMBER.findall(t)) != 2:
        return None
    said = re.search(r"energy\s+charges?\s*\(([^)]*)\)", h)
    if not said:
        return None
    paise, rupee = bool(_PAISE.search(said.group(1))), bool(_RUPEE.search(said.group(1)))
    if paise == rupee:
        return None
    kvah = energy[0][1] == "kvah"
    if paise:
        e_unit = Unit.PAISE_PER_KVAH if kvah else Unit.PAISE_PER_KWH
    else:
        e_unit = Unit.INR_PER_KVAH if kvah else Unit.INR_PER_KWH
    f_unit = Unit.INR_PER_KVA_MONTH if fixed[0][1] == "kva" else Unit.INR_PER_KW_MONTH
    return (
        Rate(Decimal(energy[0][0].replace(",", "")), e_unit),
        Rate(Decimal(fixed[0][0].replace(",", "")), f_unit),
    )
