"""Rewrite landed_hand/<state>.yaml from the state tests' hand figures (the tests fail when the file
and the hand figures differ): `python -m tests.e2e.landed_hand.write` from apps/api."""

from __future__ import annotations

from pathlib import Path

import yaml

from tests.e2e import test_ka_assumed, test_mh_assumed

HEAD = {
    "mh": "Maharashtra (MSEDCL HT I (A), 22 kV), November 2026",
    "ka": "Karnataka (BESCOM HT-2(a), 11 kV), November 2026",
}


def main() -> None:
    here = Path(__file__).parent
    for state, mod in (("mh", test_mh_assumed), ("ka", test_ka_assumed)):
        text = (
            f"# Hand-worked landed cost per kWh at the meter, {HEAD[state]}.\n"
            "# approved_rulebook: built from the figures the approved rule book carries (what the app shows);\n"
            "# order: from the order's own figures. total includes the banking charge as if every unit were\n"
            "# banked (the landed-cost screen); total_excluding_banking_upper_bound is the rule book's figure.\n"
            f"# Written by tests/e2e/landed_hand/write.py from tests/e2e/test_{state}_assumed.py::landed_yaml.\n"
        )
        doc = mod.landed_yaml()
        (here / f"{state}.yaml").write_text(
            text + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=110)
        )


if __name__ == "__main__":
    main()
