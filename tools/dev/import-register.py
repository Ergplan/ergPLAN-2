#!/usr/bin/env python3
"""Import jouleWise's State Regulation Master (erg_plan 2.0) into the rulebook.

  apps/api/.venv/bin/python tools/dev/import-register.py            # propose + approve, then export
  apps/api/.venv/bin/python tools/dev/import-register.py --dry-run  # print what would be written

Reads packages/rulebooks/register/erg-plan-2.0.json (regulation data only) and writes, through
`rulebook.service` (the single writer, audited), one approved version per state and family the
rulebook does not already hold from a cited order:

  oa_charge         transmission, wheeling (by voltage), losses, CSS, additional surcharge, duty, SLDC …
  tod_slots         the state's ToD slots with their Peak / Normal / Off-Peak category
  settlement        the cycle settlement (method cycle_pooled): cycle, charge, drawal rule, limit
  regulatory_limit  renewable capacity as a multiple of contract demand
  tariff_schedule   only where the register holds a grid cost schedule and no order is cited (TN)

Every value carries a source_ref of kind `register`: the analyst's own citation where one was
entered, otherwise the register entry itself. Where a state's family is already cited to a stored
order page (UP, MH, KA …), that version stays; UP's and MH's settlement rules are re-issued with the
cycle settlement's fields added and every cited field kept.

Idempotent: a version whose title is already approved is left alone. After it runs,
`python -m ergplan.rulebook.cli export` writes the YAML under packages/rulebooks/states/.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "apps" / "api"))

REGISTER = ROOT / "packages" / "rulebooks" / "register" / "erg-plan-2.0.json"
ACTOR = "register-import@ergplan"
ORIGIN = "register"
# what the rulebook already holds from cited orders, by state: these families are not imported
CITED = {
    "UP": {"oa_charge", "tod_slots", "tariff_schedule", "settlement"},
    "MH": {"oa_charge", "tod_slots", "tariff_schedule", "settlement"},
    "KA": {"oa_charge", "tod_slots", "tariff_schedule"},
    "AP": {"tod_slots", "tariff_schedule"},
    "GJ": {"tod_slots", "tariff_schedule"},
    "PB": {"oa_charge", "tariff_schedule"},
}
# a state with several register versions: the one to read (KA: BESCOM's; GESCOM's is the same rule)
PREFER = {"KA": "8"}
# cited settlement rules re-issued with the cycle settlement's fields: jurisdiction → register state
AMEND = {"UP": "UP", "msedcl": "MH"}
CAT = {"Peak": "peak", "Normal": "normal", "Off-Peak": "off_peak"}


def dec(x: Any) -> Decimal:
    return Decimal(str(x))


def s(x: Decimal) -> str:
    """A decimal as the rulebook stores it: plain, no trailing zeros."""
    t = format(x.normalize(), "f")
    return t if t != "-0" else "0"


def ref(note: str, fy_from: date) -> dict[str, Any]:
    return {"kind": "register", "note": note[:500], "effective_from": fy_from.isoformat()}


class Register:
    def __init__(self) -> None:
        self.t = json.loads(REGISTER.read_text())["tables"]
        self.state = {x["state_id"]: x for x in self.t["states"]}
        self.discom = {x["discom_id"]: x for x in self.t["discom"]}
        self.oa = {x["oa_type_id"]: x["oa_type_name"] for x in self.t["oa_type_master"]}
        self.settle = {
            x["settlement_type_id"]: x["settlement_type_name"] for x in self.t["settlement_type_master"]
        }

    def versions(self) -> dict[str, dict[str, Any]]:
        """The active intra-state version to read for each state code."""
        out: dict[str, dict[str, Any]] = {}
        for v in sorted(self.t["regulation_version"], key=lambda v: int(v["regulation_id"])):
            code = self.state[v["state_id"]]["state_code"]
            if v["status"] != "ACTIVE" or self.oa[v["oa_type_id"]] != "Intrastate":
                continue
            if code in PREFER and v["regulation_id"] != PREFER[code]:
                continue
            out[code] = v
        return out

    def rows(self, table: str, rid: str) -> list[dict[str, Any]]:
        return [r for r in self.t[table] if r.get("regulation_id") == rid and r.get("is_active", "t") == "t"]


def fy_start(v: dict[str, Any]) -> date:
    return date(int(str(v["financial_year"])[:4]), 4, 1)


def what(reg: Register, v: dict[str, Any]) -> str:
    st = reg.state[v["state_id"]]["state_name"]
    return f"jouleWise State Regulation Master · {st} · FY {v['financial_year']} (register version {v['regulation_id']})"


# ---------------------------------------------------------------- oa_charge

COMPONENT = (
    ("cross-subsidy", "cross_subsidy_surcharge"),
    ("css", "cross_subsidy_surcharge"),
    ("additional surcharge", "additional_surcharge"),
    ("electricity duty", "electricity_duty_oa"),
    ("sldc", "sldc_operating"),
    ("standby", "standby_charge"),
    ("grid", "grid_support_charge"),
    ("reactive", "reactive_energy_charge"),
    ("transmission", "stu_transmission"),
    ("wheeling", "wheeling"),
)
BASIS = {"Rs/kWh": "inr_per_kwh", "Rs/MW/month": "inr_per_mw_month", "Rs/MW/day": "inr_per_mw_day"}
LABEL = {
    "stu_transmission": "State transmission charge",
    "wheeling": "Wheeling charge",
    "stu_losses": "State transmission loss",
    "wheeling_losses": "Wheeling loss",
    "cross_subsidy_surcharge": "Cross-subsidy surcharge",
    "additional_surcharge": "Additional surcharge",
    "electricity_duty_oa": "Electricity duty on open-access energy",
    "sldc_operating": "SLDC scheduling and operating charge",
    "standby_charge": "Standby charge",
    "grid_support_charge": "Grid-support (parallel operation) charge",
    "reactive_energy_charge": "Reactive-energy charge",
}


CAPTIVE_EXEMPT = {
    "cross_subsidy_surcharge": "captive users (26% equity and 51% of the output, Electricity Rules 2005, r. 3) pay no "
    "cross-subsidy surcharge: Electricity Act 2003, s. 42(2), fourth proviso; the register charges it to "
    "third-party open access only",
    "additional_surcharge": "captive users pay no additional surcharge under s. 42(4) of the Electricity Act 2003 "
    "(Supreme Court, MSEDCL v. JSW Steel, 2021); the register charges it to third-party open access only",
}


def component_of(name: str) -> str | None:
    n = name.lower()
    return next((c for key, c in COMPONENT if key in n), None)


def oa_charge_rows(reg: Register, v: dict[str, Any], skipped: list[str]) -> list[dict[str, Any]]:
    rid, fy, base = v["regulation_id"], fy_start(v), what(reg, v)
    waiver: dict[str, tuple[Decimal, str]] = {}
    for w in reg.rows("waivers", rid):
        if dec(w["waiver_value"]) > 0 and w["waiver_unit"] == "%":
            comp = component_of(w["waiver_type"])
            if comp:
                waiver[comp] = (dec(w["waiver_value"]), str(w["applies_to"] or "All"))
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for c in reg.rows("charges", rid):
        comp, basis = component_of(c["charge_type"]), BASIS.get(c["charge_unit"])
        value = dec(c["charge_value"])
        if "banking" in c["charge_type"].lower():
            continue  # the banking charge is part of the settlement rule
        if comp is None or basis is None:
            if value != 0:
                skipped.append(
                    f"{base}: {c['charge_type']} {c['charge_value']} {c['charge_unit']} (no rulebook basis)"
                )
            continue
        if value == 0 and not c.get("source_reference"):
            continue  # an unfilled register default, not a statement that the charge is nil
        groups[(comp, basis)].append(c)
    for loss in reg.rows("losses", rid):
        kind = loss["loss_type"].lower()
        if "transmission" in kind:
            comp = "stu_losses"
        elif "wheeling" in kind:
            comp = "wheeling_losses"
        else:  # a loss the rulebook has no component for: said, never guessed
            skipped.append(
                f"{base}: loss '{loss['loss_type']}' {loss['loss_percentage']}% (not transmission or wheeling)"
            )
            continue
        groups[(comp, "percent_in_kind")].append(
            {
                "charge_value": loss["loss_percentage"],
                "voltage_level_kv": loss["voltage_level_kv"],
                "source_reference": None,
            }
        )
    out: list[dict[str, Any]] = []
    for (comp, basis), items in sorted(groups.items()):
        by_kv = {i["voltage_level_kv"]: i for i in items if i["voltage_level_kv"] is not None}
        flat = [i for i in items if i["voltage_level_kv"] is None]
        body: dict[str, Any] = {"component": comp, "basis": basis}
        scope: dict[str, Any] = {"connection": ["intra_state"]}
        field_refs: dict[str, Any] = {}
        cite = next((i["source_reference"] for i in items if i.get("source_reference")), None)
        if by_kv:
            body["value_by_voltage"] = {
                s(dec(kv)): s(dec(i["charge_value"]))
                for kv, i in sorted(by_kv.items(), key=lambda x: dec(x[0]))
            }
            for kv, i in by_kv.items():
                if i.get("source_reference") and i["source_reference"] != cite:
                    field_refs[f"/value_by_voltage/{s(dec(kv))}"] = ref(
                        f"{i['source_reference']} — as entered in the {base}", fy
                    )
            body["notes"] = (
                "Tabulated by supply voltage (kV) in the register; a voltage between two rows takes the next higher row."
            )
        else:
            body["value"] = s(dec(flat[0]["charge_value"]))
            if comp in ("wheeling", "wheeling_losses"):
                # the register's rule: wheeling charges and losses apply below 66 kV
                scope["voltage_kv"] = {"max": 65}
                body["notes"] = "One figure in the register; wheeling applies to supply below 66 kV."
        if comp in CAPTIVE_EXEMPT:
            # the register holds these surcharges for third-party open access; erg_plan 2.0 does not
            # charge them to a captive user (Electricity Act 2003, s. 42; Electricity Rules 2005, r. 3)
            body["exemptions"] = [
                {"condition": cond, "waived_pct": "100", "note": CAPTIVE_EXEMPT[comp]}
                for cond in ("captive_26_51", "group_captive")
            ]
        if comp in waiver:
            pct, who = waiver[comp]
            body.setdefault("exemptions", [])
            body["exemptions"] += [
                {
                    "condition": "re_source_waiver",
                    "waived_pct": s(pct),
                    "note": f"waiver in the register, applies to: {who}",
                }
            ]
        for i, ex in enumerate(body.get("exemptions") or []):
            if ex["condition"] in ("captive_26_51", "group_captive"):
                field_refs[f"/exemptions/{i}/waived_pct"] = ref(CAPTIVE_EXEMPT[comp], fy)
        note = f"{cite} — as entered in the {base}" if cite else f"{base}: entered without a citation"
        row = {"label": f"{LABEL[comp]} · {reg.state[v['state_id']]['state_code']} · FY {v['financial_year']}", "scope": scope,
               "body": body, "source_ref": ref(note, fy)}  # fmt: skip
        if field_refs:
            row["field_refs"] = field_refs
        out.append(row)
    return out


# ---------------------------------------------------------------- tod_slots


def _hhmm(x: str) -> str:
    h = dec(x)
    return f"{int(h):02d}:{int((h - int(h)) * 60):02d}"


def _months(text: str | None) -> list[int] | None:
    if not text or text.strip() == "1-12":
        return None
    ms = sorted({int(p) for p in text.split(",") if p.strip()})
    return None if len(ms) == 12 else ms


def tod_rows(reg: Register, v: dict[str, Any]) -> list[dict[str, Any]]:
    rid, fy, base = v["regulation_id"], fy_start(v), what(reg, v)
    slots = []
    order = sorted(reg.rows("tod_slots", rid), key=lambda t: (t["season"] or "", dec(t["start_hour"] or 0)))
    for t in order:
        sl: dict[str, Any] = {"name": t["slot_name"], "start": _hhmm(t["start_hour"]), "end": _hhmm(t["end_hour"]),
                              "category": CAT[t["category"]]}  # fmt: skip
        ms = _months(t["month_applicability"])
        if ms:
            sl["months"] = ms
        if t["surcharge_pct"] and dec(t["surcharge_pct"]) != 0:
            sl["surcharge_pct"] = s(dec(t["surcharge_pct"]))
        if t["rebate_pct"] and dec(t["rebate_pct"]) != 0:
            sl["rebate_pct"] = s(dec(t["rebate_pct"]))
        slots.append(sl)
    if not slots:
        return []
    # slots are read by name within a month: a name used twice gets its hours
    seen: dict[tuple[str, tuple[int, ...]], int] = defaultdict(int)
    for sl in slots:
        seen[(sl["name"], tuple(sl.get("months") or ()))] += 1
    for sl in slots:
        if seen[(sl["name"], tuple(sl.get("months") or ()))] > 1:
            sl["name"] = f"{sl['name']} {sl['start']}–{sl['end']}"
    return [{"label": f"ToD slots · {reg.state[v['state_id']]['state_code']} · FY {v['financial_year']}",
             "scope": {}, "body": {"slots": slots}, "source_ref": ref(f"{base}: ToD slots as entered", fy)}]  # fmt: skip


# ---------------------------------------------------------------- settlement


def _slots(text: str | None) -> list[str] | None:
    if not text or not text.strip():
        return None
    out = []
    for part in text.replace(",", "+").replace("/", "+").split("+"):
        p = part.strip().lower().replace(" ", "").replace("-", "").replace("_", "")
        if p in ("peak", "normal"):
            out.append(p)
        elif p == "offpeak":
            out.append("off_peak")
    return out or None


def banking_fields(reg: Register, v: dict[str, Any]) -> dict[str, Any] | None:
    """The cycle settlement's banking fields of a register version (regulationBankingRules,
    regulationBankingLimit and regulationBankingRule of erg_plan 2.0's settlement-data.ts)."""
    rid = v["regulation_id"]
    rules = reg.rows("banking_rules", rid)
    if not rules:
        return None
    b = rules[0]
    out: dict[str, Any] = {"allowed": b["banking_allowed"] == "t"}
    out["period"] = "financial_year" if (b["banking_cycle"] or "Monthly") == "Annual" else "monthly"
    charge = next(
        (
            c
            for c in reg.rows("charges", rid)
            if "banking" in c["charge_type"].lower() and c["charge_unit"] == "%"
        ),
        None,
    )
    pct = (
        dec(b["banking_charge_pct"]) * 100
        if b["banking_charge_pct"]
        else (dec(charge["charge_value"]) if charge else None)
    )
    if pct is not None:
        out["charge"] = {"kind": "in_kind_percent", "value": s(pct)}
    label = (b["banked_energy_drawal_rule"] or "").strip().lower()
    rule = {"same tod": "same_tod", "higher to lower": "higher_to_lower", "custom": "custom"}.get(label)
    if rule is None:
        rule = "higher_to_lower" if b["higher_to_lower_tod"] == "t" else "same_tod"
    out["drawal_rule"] = rule
    if rule == "custom":
        m = {}
        for cat, col in (
            ("peak", "peak_banked_drawal_slots"),
            ("normal", "normal_banked_drawal_slots"),
            ("off_peak", "off_peak_banked_drawal_slots"),
        ):
            got = _slots(b[col])
            if got:
                m[cat] = got
        out["matrix"] = m
    cap = next(
        (
            c
            for c in reg.t["banking_cap_rules"]
            if c["banking_id"] == b["banking_id"] and c["is_active"] == "t"
        ),
        None,
    )
    if cap is not None:
        lim: dict[str, Any] = {}
        drawal, gen = cap["max_discom_drawal_percentage"], cap["max_injected_energy_percentage"]
        combine = (cap["capping_combine"] or "").strip().lower()
        lim["combine"] = "min" if combine == "min" else "drawal" if combine.startswith("drawal") else "max"
        if drawal is not None:
            lim["drawal_pct"] = s(dec(drawal))
        if gen is not None and lim["combine"] != "drawal":
            lim["generation_pct"] = s(dec(gen))
        if cap["cap_pct_of_consumption"] is not None:
            lim["consumption_pct"] = s(dec(cap["cap_pct_of_consumption"]) * 100)
        out["limit"] = lim
    treat = (b["unutilised_treatment"] or "Lapse").lower()
    out["lapse"] = {"treatment": "paid" if treat.startswith("sold") else "lapses"}
    return out


def granularity(reg: Register, v: dict[str, Any]) -> str:
    return "tod_slot_monthly" if reg.settle[v["settlement_type_id"]] == "ToD" else "block_15min"


def settlement_rows(reg: Register, v: dict[str, Any]) -> list[dict[str, Any]]:
    fy, base = fy_start(v), what(reg, v)
    banking = banking_fields(reg, v)
    if banking is None:
        return []
    body = {"method": "cycle_pooled", "granularity": granularity(reg, v), "banking": banking,
            "notes": "Cycle settlement as jouleWise models it (erg_plan 2.0): real-time set-off, then banked energy drawn "
                     "within the banking cycle where the drawal rule allows, the highest-rate ToD category first."}  # fmt: skip
    return [{"label": f"Banking and settlement · {reg.state[v['state_id']]['state_code']} · solar, wind and hybrid, intra-state",
             "scope": {"connection": ["intra_state"], "source": ["solar", "wind", "hybrid"]},
             "body": body, "source_ref": ref(f"{base}: banking rule as entered", fy)}]  # fmt: skip


# ---------------------------------------------------------------- regulatory_limit, tariff_schedule


def limit_rows(reg: Register, v: dict[str, Any]) -> list[dict[str, Any]]:
    fy, base = fy_start(v), what(reg, v)
    b = reg.rows("banking_rules", v["regulation_id"])
    if not b:
        return []
    cap = next((c for c in reg.t["banking_capacity_rules"] if c["banking_id"] == b[0]["banking_id"] and c["is_active"] == "t"
                and c["capacity_restriction"] == "t" and c["max_cd_multiple"]), None)  # fmt: skip
    if cap is None:
        return []
    pct = dec(cap["max_cd_multiple"]) * 100
    return [{"label": f"Renewable open-access capacity up to {s(dec(cap['max_cd_multiple']))} × contract demand",
             "scope": {},
             "body": {"limit": "re_cap_pct_of_contract_demand", "value": s(pct), "unit": "percent",
                      "notes": "Capacity restriction with respect to contract demand, as entered in the register."},
             "source_ref": ref(f"{base}: capacity restriction as entered", fy)}]  # fmt: skip


def tariff_rows(reg: Register, v: dict[str, Any]) -> list[dict[str, Any]]:
    """A retail schedule from the register's grid cost rows: the Normal rate is the energy charge
    (the ToD surcharge and rebate come from the slots)."""
    fy, base = fy_start(v), what(reg, v)
    rows = [g for g in reg.rows("grid_cost_schedule", v["regulation_id"]) if g["tod_category"] == "Normal"]
    by_cat: dict[str, dict[str, Any]] = {}
    for g in rows:
        parts = [p.strip() for p in g["voltage_level"].split("·")]
        cat = next((p for p in parts if p.upper().startswith("HT")), None)
        if cat is None:
            continue
        by_cat.setdefault(cat, {"rates": set(), "demand": set(), "kvah": "kVAh" in g["voltage_level"]})
        by_cat[cat]["rates"].add(dec(g["energy_rate_rs_kwh"]))
        by_cat[cat]["demand"].add(dec(g["demand_charge_rs_kva_month"]))
    out = []
    for cat, x in sorted(by_cat.items()):
        if len(x["rates"]) != 1 or len(x["demand"]) != 1:
            continue  # varies by voltage or season: needs the order itself
        out.append({"label": f"{cat} · FY {v['financial_year']}", "scope": {"category": [cat]},
                    "body": {"category": cat, "billing_basis": "kvah" if x["kvah"] else "kwh",
                             "demand_charge": {"value": s(x["demand"].pop()), "basis": "inr_per_kva_month"},
                             "energy_charge": {"base": s(x["rates"].pop())},
                             "notes": "From the register's grid cost schedule (Normal rate; ToD surcharge and rebate are on the ToD slots)."},
                    "source_ref": ref(f"{base}: grid cost schedule as entered", fy)})  # fmt: skip
    return out


# ---------------------------------------------------------------- golden tests


def golden_tests(family: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Golden cases for an imported version: in a plain context, each row's field is the register's
    figure. They are written to packages/rulebooks/golden/ and gate the approval like any other."""
    ctx = {"connection": "intra_state", "source": "solar", "ownership": "captive"}
    out: list[dict[str, Any]] = []
    for r in rows:
        b = r["body"]
        if family == "oa_charge":
            kv = 11
            if "value_by_voltage" in b:
                keys = sorted(b["value_by_voltage"], key=Decimal)
                pick = next((k for k in keys if Decimal(kv) <= Decimal(k)), keys[-1])
                field, want = f"/value_by_voltage/{pick}", b["value_by_voltage"][pick]
            else:
                field, want = "/value", b["value"]
            out.append({"name": f"{b['component']} at {kv} kV: {want} ({b['basis']})",
                        "given": {"context": {**ctx, "voltage_kv": kv}, "where": {"/component": b["component"], "/basis": b["basis"]}, "field": field},
                        "expect": {"value": want}})  # fmt: skip
        elif family == "tod_slots":
            for i in (0, len(b["slots"]) - 1):
                out.append({"name": f"slot {i + 1}: {b['slots'][i]['name']} is {b['slots'][i]['category']}",
                            "given": {"context": {}, "field": f"/slots/{i}/category"}, "expect": {"value": b["slots"][i]["category"]}})  # fmt: skip
        elif family == "settlement":
            for field in ("/method", "/granularity", "/banking/period", "/banking/drawal_rule", "/banking/charge/value",
                          "/banking/limit/drawal_pct", "/banking/limit/combine"):  # fmt: skip
                cur: Any = b
                for part in field.strip("/").split("/"):
                    cur = cur.get(part) if isinstance(cur, dict) else None
                if cur is not None:
                    out.append(
                        {
                            "name": f"{field.strip('/')}: {cur}",
                            "given": {"context": ctx, "field": field},
                            "expect": {"value": cur},
                        }
                    )
        elif family == "regulatory_limit":
            out.append({"name": f"{b['limit']}: {b['value']} {b['unit']}",
                        "given": {"context": ctx, "where": {"/limit": b["limit"]}, "field": "/value"}, "expect": {"value": b["value"]}})  # fmt: skip
        elif family == "tariff_schedule":
            c = {"category": b["category"], "voltage_kv": 11}
            out.append({"name": f"{b['category']} energy charge {b['energy_charge']['base']}",
                        "given": {"context": c, "field": "/energy_charge/base"}, "expect": {"value": b["energy_charge"]["base"]}})  # fmt: skip
            out.append({"name": f"{b['category']} demand charge {b['demand_charge']['value']}",
                        "given": {"context": c, "field": "/demand_charge/value"}, "expect": {"value": b["demand_charge"]["value"]}})  # fmt: skip
    return out


def write_golden(code: str, family: str, title: str, rows: list[dict[str, Any]]) -> None:
    import yaml

    path = ROOT / "packages" / "rulebooks" / "golden" / code.lower() / f"{code}-{family}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    head = f"# {title}\n# Written by tools/dev/import-register.py from packages/rulebooks/register/erg-plan-2.0.json.\n"
    doc = {"jurisdiction": code, "family": family, "tests": golden_tests(family, rows)}
    path.write_text(head + yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=200))


# ---------------------------------------------------------------- write

BUILDERS = {
    "oa_charge": oa_charge_rows,
    "tod_slots": tod_rows,
    "settlement": settlement_rows,
    "regulatory_limit": limit_rows,
    "tariff_schedule": tariff_rows,
}


def plan(reg: Register) -> tuple[list[tuple[str, str, dict[str, Any], list[dict[str, Any]]]], list[str]]:
    out, skipped = [], []
    for code, v in sorted(reg.versions().items()):
        for family, build in BUILDERS.items():
            if family in CITED.get(code, set()):
                continue
            rows = build(reg, v, skipped) if family == "oa_charge" else build(reg, v)  # type: ignore[operator]
            if rows:
                out.append((code, family, v, rows))
    return out, skipped


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    reg = Register()
    todo, skipped = plan(reg)
    if a.dry_run:
        for code, family, _v, rows in todo:
            print(f"{code:3} {family:17} {len(rows)} rows")
        for x in skipped:
            print("skipped:", x)
        return 0

    from sqlalchemy import select

    from ergplan.db.base import session_scope
    from ergplan.db.models import Rule, RulebookVersion
    from ergplan.rulebook import golden, service

    for code, family, v, rows in todo:
        write_golden(code, family, f"{what(reg, v)} · {family}", rows)
    with session_scope(actor=ACTOR) as ses:
        service.sync_jurisdictions(ses)
        golden.sync_tests(ses)
    for code, family, v, rows in todo:
        title = f"{what(reg, v)} · {family}"
        with session_scope(actor=ACTOR, role="analyst") as ses:
            cur_id = ses.scalar(
                select(RulebookVersion.id).where(
                    RulebookVersion.title == title, RulebookVersion.status == "approved"
                )
            )
            if cur_id:
                have = [
                    (r.label, r.scope, r.body)
                    for r in ses.scalars(
                        select(Rule).where(Rule.rulebook_version_id == cur_id).order_by(Rule.position)
                    )
                ]
                if have == [(r["label"], r["scope"], r["body"]) for r in rows]:
                    print("present ", code, family)
                    continue
                print("changed ", code, family, "→ a new version")
            ver = service.propose(ses, family=family, jurisdiction_id=code, rows=rows, actor=ACTOR,
                                  effective_from=fy_start(v), title=title, origin=ORIGIN)  # fmt: skip
            service.approve(ses, ver.id, actor=ACTOR, role="analyst")
            print("approved", code, family, f"v{ver.version}", len(rows), "rows")
    # cited settlement rules: every cited field kept, the cycle settlement's fields added
    versions = reg.versions()
    for jid, code in AMEND.items():
        v = versions[code]
        add = banking_fields(reg, v)
        assert add is not None
        with session_scope(actor=ACTOR, role="analyst") as ses:
            cur = ses.scalar(select(RulebookVersion).where(RulebookVersion.family == "settlement",
                                                           RulebookVersion.jurisdiction_id == jid,
                                                           RulebookVersion.status == "approved"))  # fmt: skip
            if cur is None:
                print("no cited settlement rule for", jid)
                continue
            rules = list(
                ses.scalars(select(Rule).where(Rule.rulebook_version_id == cur.id).order_by(Rule.position))
            )
            done = all(
                r.body.get("method") == "cycle_pooled"
                and (
                    "charge" not in add
                    or (r.body.get("banking") or {}).get("charge")
                    or not (r.body.get("banking") or {}).get("allowed")
                )
                for r in rules
            )
            if done:
                print("present ", jid, "settlement (cycle settlement)")
                continue
            note = ref(
                f"{what(reg, v)}: the cycle settlement jouleWise applies (drawal rule and banking limit as entered)",
                fy_start(v),
            )
            rows = []
            for r in rules:
                body = json.loads(json.dumps(r.body))
                body["method"] = "cycle_pooled"
                charge_from_register = False
                bk = body.setdefault("banking", {})
                if bk.get("allowed"):
                    for key in ("drawal_rule", "matrix", "limit"):
                        if key in add:
                            bk[key] = add[key]
                    if not bk.get("charge") and "charge" in add:
                        # the cited rule states no banking charge: the register's applies, shown as such
                        bk["charge"] = add["charge"]
                        charge_from_register = True
                    if "limit" not in add:
                        # the register sets no cap for this state: erg_plan 2.0's default applies
                        bk["limit"] = {"drawal_pct": "30", "generation_pct": "25", "combine": "max"}
                    bk["period"] = (
                        "financial_year"
                        if bk.get("period") in ("financial_year", "calendar_year")
                        else "monthly"
                    )
                fr = dict(r.field_refs or {})
                for ptr in ("/method", "/banking/drawal_rule", "/banking/matrix", "/banking/limit"):
                    fr[ptr] = note
                if charge_from_register:
                    fr["/banking/charge/value"] = note
                rows.append({"label": r.label, "scope": r.scope, "body": body, "source_ref": r.source_ref,
                             "field_refs": fr, "position": r.position})  # fmt: skip
            ver = service.propose(ses, family="settlement", jurisdiction_id=jid, rows=rows, actor=ACTOR,
                                  effective_from=cur.effective_from, effective_to=cur.effective_to, title=cur.title,
                                  origin=cur.origin, notes="cycle settlement fields added from the State Regulation Master")  # fmt: skip
            service.approve(ses, ver.id, actor=ACTOR, role="analyst")
            print("re-issued", jid, "settlement", f"v{ver.version}")
    # cited open-access charges: a cross-subsidy or additional surcharge row that names no captive
    # exemption gets the statutory one, cited as such (not to the order's page)
    with session_scope(actor=ACTOR, role="analyst") as ses:
        cited = list(ses.scalars(select(RulebookVersion).where(RulebookVersion.family == "oa_charge",
                                                               RulebookVersion.status == "approved",
                                                               RulebookVersion.origin != ORIGIN)))  # fmt: skip
        todo_ids = [v.id for v in cited]
    for vid in todo_ids:
        with session_scope(actor=ACTOR, role="analyst") as ses:
            cur = ses.get(RulebookVersion, vid)
            assert cur is not None
            rules = list(
                ses.scalars(select(Rule).where(Rule.rulebook_version_id == vid).order_by(Rule.position))
            )
            rows, touched = [], 0
            for r in rules:
                body = json.loads(json.dumps(r.body))
                fr = dict(r.field_refs or {})
                comp = body.get("component")
                have = {e.get("condition") for e in body.get("exemptions") or []}
                if comp in CAPTIVE_EXEMPT and not have & {"captive_26_51", "group_captive"}:
                    exs = list(body.get("exemptions") or [])
                    for cond in ("captive_26_51", "group_captive"):
                        fr[f"/exemptions/{len(exs)}/waived_pct"] = ref(
                            CAPTIVE_EXEMPT[comp], cur.effective_from
                        )
                        exs.append({"condition": cond, "waived_pct": "100", "note": CAPTIVE_EXEMPT[comp]})
                    body["exemptions"] = exs
                    touched += 1
                rows.append({"label": r.label, "scope": r.scope, "body": body, "source_ref": r.source_ref,
                             "field_refs": fr, "position": r.position})  # fmt: skip
            if not touched:
                continue
            ver = service.propose(ses, family="oa_charge", jurisdiction_id=cur.jurisdiction_id, rows=rows, actor=ACTOR,
                                  effective_from=cur.effective_from, effective_to=cur.effective_to, title=cur.title,
                                  origin=cur.origin, notes="statutory captive exemption added to the surcharge rows")  # fmt: skip
            service.approve(ses, ver.id, actor=ACTOR, role="analyst")
            print("re-issued", cur.jurisdiction_id, "oa_charge", f"v{ver.version}", f"({touched} rows)")
    for x in skipped:
        print("skipped:", x)
    return 0


if __name__ == "__main__":
    sys.exit(main())
