"""Every approved surcharge row carries the statutory captive exemption (Stage 10): the state rule
book's captive routes rest on it, so a state's open-access charges without it would price a captive
user as a third party."""

from __future__ import annotations

import yaml

from ergplan.rulebook import yaml_io

SURCHARGES = {"cross_subsidy_surcharge", "additional_surcharge"}


def test_every_surcharge_row_exempts_captive_and_group_captive() -> None:
    files = sorted(yaml_io.states_dir().glob("*/oa_charge/*.yaml"))
    assert len(files) >= 15
    seen = 0
    for path in files:
        doc = yaml.safe_load(path.read_text())
        for i, row in enumerate(doc["rows"]):
            body = row["body"]
            if body.get("component") not in SURCHARGES:
                continue
            seen += 1
            waived = {
                x["condition"]: x.get("waived_pct")
                for x in body.get("exemptions") or []
                if x.get("condition") in ("captive_26_51", "group_captive")
            }
            where = f"{path.name} row {i} ({body['component']})"
            assert waived == {"captive_26_51": "100", "group_captive": "100"}, where
            refs = row.get("field_refs") or {}
            for k, x in enumerate(body["exemptions"]):
                if x.get("condition") in waived:
                    assert f"/exemptions/{k}/waived_pct" in refs, where  # the exemption is itself sourced
    assert seen >= 20
