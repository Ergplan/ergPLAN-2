"""Shared plumbing for the Stage 17 state tests (tests/e2e/test_<state>_assumed.py): a hand-worked bill
turned into billx's reading of it, the twelve bills uploaded through the API with a stand-in billx, and
the API calls of each step with their answers written to a folder when one is named.

Nothing here uses the app's code to work a figure the tests compare against: the hand bills are worked
in each state's test file from that state's order; the procedures here (a normative curve summed into
ToD slots, a month of ToD-slot banking, the landed-cost build-up, the grid bill from a dispatch file)
are written from the regulations' and the data files' words, with the state's figures passed in."""

from __future__ import annotations

import copy
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ergplan.intake import bills as bill_store
from ergplan.platform.app import app
from tests.engine.helpers import restore_committed
from tests.intake.helpers import FakeBillx, result

client = TestClient(app)
AN = {"X-Ergplan-Actor": "analyst@joulewise.in", "X-Ergplan-Role": "analyst"}


@dataclass(frozen=True)
class Line:
    """One printed bill line: billx's code, the label as printed, quantity × rate = amount."""

    code: str
    label: str
    quantity: D | None
    rate: D | None
    unit: str | None
    amount: D


@dataclass
class HandBill:
    first: date  # first day of the supply month
    kwh: D
    kvah: D
    max_kva: D
    contract_kva: D
    tod: list[tuple[str, D]]  # (slot as printed, kWh)
    lines: list[Line] = field(default_factory=list)

    @property
    def last(self) -> date:
        return (self.first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)

    @property
    def total(self) -> D:
        return sum((ln.amount for ln in self.lines), D(0))

    def amount(self, code: str) -> D:
        return sum((ln.amount for ln in self.lines if ln.code == code), D(0))


def billx_reading(h: HandBill, utility: str, category: str) -> dict[str, Any]:
    """billx's reading of a hand-worked bill, in the shape of the synthetic PVVNL fixture: every field
    verified, every line's own arithmetic checked."""
    res = copy.deepcopy(result("pvvnl_passed"))
    b = res["bills"][0]
    b["profile"] = None  # no layout profile: the DISCOM is read from its printed name
    b["utility"] = {"id": None, "name_as_printed": utility, "state": None}
    fl = b["fields"]
    fl["utility"]["name"]["value"] = fl["utility"]["name"]["printed"] = utility
    for path in (("connection", "tariff_category"), ("tariff", "category")):
        node = fl[path[0]][path[1]]
        node["value"] = node["printed"] = category
    p = fl["period"]
    nxt = h.last + timedelta(days=3)
    p["from"]["value"], p["to"]["value"] = h.first.isoformat(), h.last.isoformat()
    p["bill_date"]["value"], p["bill_month"]["value"] = nxt.isoformat(), nxt.strftime("%Y-%m")
    p["due_date"]["value"] = (nxt + timedelta(days=15)).isoformat()
    fl["energy"]["kwh"]["value"], fl["energy"]["kvah"]["value"] = str(h.kwh), str(h.kvah)
    fl["demand"]["max_kva"]["value"], fl["demand"]["contract_kva"]["value"] = (
        str(h.max_kva),
        str(h.contract_kva),
    )
    proto = fl["energy"]["tod"][0]
    fl["energy"]["tod"] = []
    for name, kwh in h.tod:
        row = copy.deepcopy(proto)
        row["slot"] = name
        row["kwh"]["value"] = str(kwh)
        fl["energy"]["tod"].append(row)
    by_code_proto = fl["charges"]["by_code"][0]
    fl["charges"]["by_code"] = []
    el_proto = b["elements"][0]
    elements = []
    for i, ln in enumerate(h.lines):
        bc = copy.deepcopy(by_code_proto)
        bc["code"], bc["amount"]["value"] = ln.code, str(ln.amount)
        fl["charges"]["by_code"].append(bc)
        el = copy.deepcopy(el_proto)
        el.update(index=i, label=ln.label, code=ln.code, unit=ln.unit)
        el["quantity"] = None if ln.quantity is None else str(ln.quantity)
        el["rate"] = None if ln.rate is None else str(ln.rate)
        el["amount"]["value"] = el["amount"]["printed"] = str(ln.amount)
        el["formula_recheck"].update(computed=str(abs(ln.amount)), billed=str(abs(ln.amount)), difference="0")
        elements.append(el)
    tail = copy.deepcopy(b["elements"][6:9])
    for j, el in enumerate(tail):
        el["index"] = len(elements) + j
    tail[0]["amount"]["value"] = str(h.total)
    tail[1]["amount"]["value"] = "0"
    tail[2]["amount"]["value"] = str(h.total)
    b["elements"] = elements + tail
    fl["charges"]["energy"]["value"] = str(h.amount("energy_charge"))
    fl["charges"]["demand"]["value"] = str(h.amount("demand_charge"))
    t = fl["totals"]
    t["current_charges"]["value"], t["arrears"]["value"], t["net_payable"]["value"] = (
        str(h.total),
        "0",
        str(h.total),
    )
    res["file"]["sha256"] = f"assumed-{utility}-{h.first.isoformat()}"
    return res


class Out:
    """Each answer written to the folder named by an environment variable, when it is set."""

    def __init__(self, env: str) -> None:
        self.dir = os.environ.get(env)
        if self.dir:
            Path(self.dir).mkdir(parents=True, exist_ok=True)

    def save(self, name: str, data: Any) -> None:
        if self.dir:
            Path(self.dir, f"{name}.json").write_text(json.dumps(data, indent=1, default=str))

    def text(self, name: str, text: str) -> None:
        if self.dir:
            Path(self.dir, name).write_text(text)

    def get(self, name: str, path: str, **kw: Any) -> Any:
        r = client.get(path, headers=AN, **kw)
        self.save(name, _body(r))
        assert r.status_code == 200, f"{path}: {r.status_code} {r.text[:300]}"
        return r.json()

    def post(self, name: str, path: str, ok: tuple[int, ...] = (200, 201, 202), **kw: Any) -> Any:
        r = client.post(path, headers=AN, **kw)
        self.save(name, _body(r))
        assert r.status_code in ok, f"{path}: {r.status_code} {r.text[:300]}"
        return r.json()


def _body(r: Any) -> Any:
    if r.headers.get("content-type", "").startswith("application/json"):
        return r.json()
    return {"status": r.status_code, "text": r.text[:4000]}


def landed_lines(
    *,
    generation: D,
    injection: D,
    keep: D,
    drawal: D,
    bank_pct: D | None,
    duty: D,
    equity: D = D(0),
) -> dict[str, D]:
    """The landed cost of a kWh at the meter, by hand, in the order of the build-up: the generation
    price and the charges paid on injected energy (wheeling, transmission), grossed up for the energy
    lost on the way (`keep` the share delivered); the charges at drawal (cross-subsidy and additional
    surcharge); the banking charge in kind as if every unit were banked (it costs bank/(100-bank) of
    the energy's cost to the drawal point: the generation, the injection charges and the losses; the
    energy lost in kind is never drawn, so the drawal charges, paid on units drawn, are not grossed
    up); the electricity duty; the equity cost of a 26%-equity plant (given per kWh generated, so per
    kWh at the meter it is grossed up for the energy lost like the price is).
    `calculator_total` is what the landed-cost calculator shows a visitor who has no bills: without
    the duty (no bill rate to take it from) and without the banking charge (shown beside it)."""
    lost = (generation + injection) * (1 / keep - 1)
    bank = D(0) if not bank_pct else (generation + injection + lost) * bank_pct / (100 - bank_pct)
    lines = {
        "generation": generation,
        "injection_charges": injection,
        "losses": lost,
        "drawal_charges": drawal,
        "banking_upper_bound": bank,
        "duty_assumed": duty,
        "equity_cost": (equity / keep).quantize(D("0.0001")),
    }
    lines["total"] = sum(lines.values(), D(0))
    # the rule book shows the banking charge apart, "on banked units only", and leaves it out of the
    # landed cost it prints; the landed-cost screen's total holds it (as the upper bound)
    lines["total_excluding_banking_upper_bound"] = lines["total"] - bank
    lines["calculator_total"] = lines["total_excluding_banking_upper_bound"] - duty
    return lines


def calculator_input(plant: dict[str, dict[str, Any]], ppa: dict[str, D], equity_pct: D) -> dict[str, Any]:
    """The landed-cost calculator's input (report.calculator.CalculatorIn) that the hand figures rest
    on: the PPA tariffs and the plant figures stated, the equity share; intra-state."""
    out: dict[str, Any] = {}
    for source, p in plant.items():
        out[source] = {
            "ppa_tariff_inr_per_kwh": str(ppa[source]),
            "capex_inr_per_kw": str(p["capex"]),
            "om_pct_of_capex": str(p["om"]),
            "cuf_pct": str(p["cuf"]),
            "degradation_pct": str(p["deg"]),
            "life_years": p["life"],
        }
    out["equity_share_pct"] = str(equity_pct)
    out["connection"] = "intra_state"
    return out


def route_entry(route: str, source: str, approved: dict[str, D], order: dict[str, D]) -> dict[str, Any]:
    """One route of a landed_hand file: what the calculator must show (`total` without the banking
    charge and without the bills' duty, `banking` on a banked unit), and the state test's figures
    for the site with its bills (`site_approved_rulebook`) and from the order's own figures (`order`)."""
    q4 = D("0.0001")
    return {
        "route": route,
        "source": source,
        "total": str(approved["calculator_total"].quantize(q4)),
        "banking": str(approved["banking_upper_bound"].quantize(q4)),
        "site_approved_rulebook": {k: str(x.quantize(q4)) for k, x in approved.items()},
        "order": {k: str(x.quantize(q4)) for k, x in order.items()},
    }


CUF_DIR = Path(__file__).resolve().parents[4] / "packages" / "profiles" / "cuf"


def curve_energy(
    curve_id: str, capacity_kw: D, year_start: date, slot_of_hour: Any
) -> dict[tuple[date, str], D]:
    """A normative generation curve (packages/profiles/cuf/<id>.yaml, as its README defines it: the
    month factor × the hour factor, scaled to the curve's annual CUF over the year, no block above
    the capacity) summed into (month, slot) for the year from `year_start`; `slot_of_hour(month, h)`
    names the slot an hour falls in. Worked here from the data file, not by the app's code."""
    import calendar

    import yaml

    c = yaml.safe_load((CUF_DIR / f"{curve_id}.yaml").read_text())
    monthly = {int(k): D(str(v)) for k, v in c["monthly"].items()}
    hourly = {int(k): D(str(v)) for k, v in c["hourly"].items()}
    months, d = [], year_start
    for _ in range(12):
        months.append((d, calendar.monthrange(d.year, d.month)[1]))
        d = date(d.year + d.month // 12, d.month % 12 + 1, 1)
    # one day of each month in quarter hours, raw
    raw_day = {m: [monthly[m.month] * hourly.get(b // 4, D(0)) for b in range(96)] for m, _ in months}
    raw_year = sum((sum(raw_day[m], D(0)) * n for m, n in months), D(0))
    target = capacity_kw * D(8760) * D(str(c["annual_cuf_pct"])) / 100
    scale = target / (raw_year / 4)
    out: dict[tuple[date, str], D] = {}
    for m, n in months:
        for b, v in enumerate(raw_day[m]):
            key = (m, slot_of_hour(m.month, b // 4))
            out[key] = out.get(key, D(0)) + min(v * scale, capacity_kw) * D("0.25") * n
    return out


@dataclass
class HandMonth:
    setoff: D
    banked_before: D  # surplus banked, before the charge in kind
    charge: D
    withdrawn: D
    lapsed: D
    unbanked: D  # surplus over the banking limit
    grid: D


def hand_settle(
    cons: dict[str, D],
    gen: dict[str, D],
    *,
    charge_pct: D,
    limit: D | None,
    may_draw: dict[str, set[str]],
    sink_order: list[str],
) -> HandMonth:
    """One month of a ToD-slot settlement worked by hand: each slot's consumption against the energy
    delivered in it; the surplus banked up to `limit` (None: no limit) less the charge in kind; the
    banked energy drawn in the slots it may serve (`may_draw[banked in]`), the dearest slot first,
    each slot taking first the banked energy that can serve no dearer slot; what is not drawn
    lapses at the month's end. Slots are ToD categories (peak, normal, off_peak)."""
    direct = {s: min(cons.get(s, D(0)), gen.get(s, D(0))) for s in set(cons) | set(gen)}
    surplus = {s: gen.get(s, D(0)) - direct[s] for s in direct}
    short = {s: cons.get(s, D(0)) - direct[s] for s in direct}
    total_surplus = sum(surplus.values(), D(0))
    banked = total_surplus if limit is None else min(total_surplus, limit)
    keep = 1 - charge_pct / 100
    cap = banked * keep
    bank = {s: surplus[s] * keep for s in surplus}
    drawn = D(0)
    for sink in sink_order:
        need = short.get(sink, D(0))
        # the banks that may serve this sink, those with the fewest further uses first
        for src in sorted(
            (s for s in bank if sink in may_draw.get(s, {s})),
            key=lambda s: sum(
                1 for t in sink_order[sink_order.index(sink) + 1 :] if t in may_draw.get(s, {s})
            ),
        ):
            take = min(need, bank[src], cap - drawn)
            if take > 0:
                bank[src] -= take
                need -= take
                drawn += take
    return HandMonth(
        setoff=sum(direct.values(), D(0)),
        banked_before=banked,
        charge=banked - cap,
        withdrawn=drawn,
        lapsed=cap - drawn,
        unbanked=total_surplus - banked,
        grid=sum(short.values(), D(0)) - drawn,
    )


def make_site(
    out: Out, bills: list[HandBill], utility: str, category: str, overrides: dict[str, Any]
) -> dict[str, Any]:
    """The committed rule books restored, the twelve bills uploaded and accepted, the intake built
    from them with the overrides the client would type, and the site created."""
    restore_committed()
    mp = pytest.MonkeyPatch()
    fake = FakeBillx()
    mp.setattr(bill_store, "BillxClient", fake.client)
    ids, notes = [], []
    try:
        for h in bills:
            tag = f"{utility}-{h.first:%Y%m}"
            r = client.post(
                "/bills",
                files={
                    "file": (
                        f"{tag}.pdf",
                        fake.register(tag, billx_reading(h, utility, category)),
                        "application/pdf",
                    )
                },
            )
            assert r.status_code in (200, 201), r.text
            [bill] = r.json()
            notes.append(
                {
                    "month": f"{h.first:%Y-%m}",
                    "accepted": bill.get("accepted"),
                    "outstanding": bill["outstanding"],
                }
            )
            for path in list(bill["outstanding"]):
                value = next(x["value"] for x in bill["fields"] if x["path"] == path)
                bill = client.post(f"/bills/{bill['id']}/confirm", json={"path": path, "value": value}).json()
            ids.append(bill["id"])
    finally:
        mp.undo()
    si = client.post("/intake/from-bills", json={"bill_ids": ids, "overrides": overrides})
    assert si.status_code == 200, si.text
    out.save("01_bills", notes)
    out.save("02_intake", si.json())
    made = client.post("/sites", json={"intake": si.json()})
    assert made.status_code == 201, made.text
    return {
        "site": made.json()["site_id"],
        "conn": made.json()["connection_id"],
        "bills": ids,
        "notes": notes,
        "intake": si.json(),
    }


def sv(x: dict[str, Any]) -> D:
    """A SourcedValue's value as the API returns it."""
    return D(str(x["value"]))


def crf(rate_pct: D, years: int) -> D:
    """Capital recovery factor r(1+r)^n / ((1+r)^n - 1)."""
    r = rate_pct / 100
    f = (1 + r) ** years
    return r * f / (f - 1)


def energy_per_kw(cuf_pct: D, deg_pct: D, years: int) -> D:
    """Average kWh a year from a kW over the plant's life, its output falling by `deg_pct` a year."""
    d = deg_pct / 100
    return D(8760) * cuf_pct / 100 * sum(((1 - d) ** y for y in range(years)), D(0)) / years


def check_settle(res: dict[str, Any], hand: dict[date, HandMonth]) -> None:
    """Each month of the app's settlement against the hand settlement, to the kWh, and the energy
    balance closed by hand: consumption = set-off + withdrawn + grid."""
    assert len(res["months"]) == len(hand)
    for m in res["months"]:
        h = hand[date.fromisoformat(m["month"])]
        got = {
            "setoff": sum((D(v) for v in m["setoff"].values()), D(0)),
            "banked_after": D(m["banked"]),
            "charge": D(m["charge"]),
            "withdrawn": D(m["withdrawn"]),
            "lapsed": D(m["lapsed"]),
            "unbanked": D(m["unbanked"]),
            "grid": D(m["grid"]),
        }
        want = {
            "setoff": h.setoff,
            "banked_after": h.banked_before - h.charge,
            "charge": h.charge,
            "withdrawn": h.withdrawn,
            "lapsed": h.lapsed,
            "unbanked": h.unbanked,
            "grid": h.grid,
        }
        for k in got:
            assert abs(got[k] - want[k]) < 1, (m["month"], k, got[k], want[k])
        assert abs(D(m["consumption"]) - (want["setoff"] + want["withdrawn"] + want["grid"])) < 1


def run_scenario(
    out: Out, ctx: dict[str, Any], name: str, cands: list[dict[str, Any]], res: int, as_of: str
) -> dict[str, Any]:
    """A scenario run through the API to its end; its solver time, status and gap kept in ctx."""
    import time

    spec = {"candidates": cands, "as_of": as_of, "resolution_min": res}
    sc = out.post(
        f"10_scenario_{name}",
        f"/sites/{ctx['site']}/scenarios",
        json={"connection_id": ctx["conn"], "name": name, "spec": spec},
    )
    t0 = time.time()
    run = out.post(f"11_run_{name}", f"/scenarios/{sc['id']}/run", params={"wait": "true"})
    detail = dict(out.get(f"12_run_{name}", f"/runs/{run['id']}"))
    assert detail["status"] == "done", detail.get("error")
    s = detail["solver"]
    ctx["figures"][f"run:{name}"] = {
        "status": s["status"],
        "seconds": s["seconds"],
        "gap": s["gap"],
        "wall": round(time.time() - t0, 1),
        "blocks": s["blocks"],
    }
    assert s["status"] in ("optimal", "time_limit") and s["blocks"] == (8760 if res == 60 else 35040)
    return detail


def dispatch_rows(csv_text: str) -> list[dict[str, str]]:
    import csv
    import io

    return list(csv.DictReader(io.StringIO(csv_text)))


def balance_by_hand(rows: list[dict[str, str]], category: Callable[[int, str], str]) -> None:
    """Each month and ToD category of a dispatch file (`category(month, slot)`; slots of one
    category net together): metered drawal = open-access set-off + banked energy withdrawn + DISCOM,
    the set-off being the lesser of the category's drawal and the open-access energy delivered in it
    (a ToD-slot settlement nets the month's category)."""
    cells: dict[tuple[str, str], list[D]] = {}
    for row in rows:
        key = (row["time"][:7], category(int(row["time"][5:7]), row["slot"]))
        c = cells.setdefault(key, [D(0)] * 4)
        c[0] += D(row["metered_kwh"])
        c[1] += sum((D(v) for k, v in row.items() if k.startswith("supply:oa_")), D(0))
        c[2] += D(row["withdrawn_kwh"])
        c[3] += D(row["grid_kwh"])
    for key, (metered, oa, withdrawn, grid) in cells.items():
        assert abs(metered - (min(metered, oa) + withdrawn + grid)) < 1, (key, metered, oa, withdrawn, grid)


def grid_bill_by_hand(
    rows: list[dict[str, str]],
    minutes: int,
    rate: Callable[[int, str], D],
    kva_per_kwh: D,
    demand_rate: D,
    floor_kva: D = D(0),
) -> tuple[D, D]:
    """The grid bill for the year from a dispatch file, by hand: each block's DISCOM kWh at
    `rate(month, slot)` (INR per kWh, the levies in), and each month's demand charge on the highest
    metered drawal in kVA (the block's kWh over its hours, times kVAh per kWh), not below `floor_kva`."""
    energy, peak_kva = D(0), {}
    for row in rows:
        energy += D(row["grid_kwh"]) * rate(int(row["time"][5:7]), row["slot"])
        kva = D(row["metered_kwh"]) * 60 / minutes * kva_per_kwh
        peak_kva[row["time"][:7]] = max(peak_kva.get(row["time"][:7], floor_kva), kva)
    return energy, sum((v * demand_rate for v in peak_kva.values()), D(0))
