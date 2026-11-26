"""Shared steps of the Stage 17 state tests (test_gj_assumed, test_pb_assumed, test_ts_assumed): an
assumed consumer's twelve hand-worked bills go in as billx readings from a stand-in reader, then every
step of the product runs through the API. The figures each test asserts are worked in the test file
from the state's own order; nothing here computes a tariff. When the environment variable named by
`out_env` is set, every response is also written to that folder for checking by hand."""

from __future__ import annotations

import copy
import csv
import io
import json
import os
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal as D
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ergplan.core.settings import get_settings
from ergplan.intake import bills as bill_store
from ergplan.platform.app import app
from tests.engine.helpers import restore_committed
from tests.intake.helpers import FakeBillx, result

client = TestClient(app)
AN = {"X-Ergplan-Actor": "analyst@joulewise.in", "X-Ergplan-Role": "analyst"}


@dataclass
class Line:
    """One charge line of a hand-worked bill, as billx would read it."""

    code: str
    label: str
    quantity: D
    rate: D
    amount: D


@dataclass
class HandBill:
    first: date  # first day of the supply month
    kwh: D
    kvah: D
    tod: list[tuple[str, D]]  # (slot label with its hours, kWh)
    max_kva: D
    contract_kva: D
    category: str  # as the bill prints it
    lines: list[Line] = field(default_factory=list)

    @property
    def last(self) -> date:
        return (self.first.replace(day=28) + timedelta(days=4)).replace(day=1) - timedelta(days=1)

    @property
    def total(self) -> D:
        return sum((ln.amount for ln in self.lines), D(0))

    def amount(self, code: str) -> D:
        return sum((ln.amount for ln in self.lines if ln.code == code), D(0))


def billx_reading(h: HandBill, profile: str, utility: str, state_name: str) -> dict[str, Any]:
    """billx's reading of a hand-worked bill, in the shape of the synthetic fixture (contract 1.1)."""
    res = copy.deepcopy(result("pvvnl_passed"))
    b = res["bills"][0]
    b["profile"] = {"id": profile, "version": "assumed", "status": "draft"}
    b["utility"] = {"id": profile, "name_as_printed": utility, "state": state_name}
    fl = b["fields"]
    fl["utility"]["name"]["value"] = fl["utility"]["name"]["printed"] = utility
    fl["connection"]["tariff_category"]["value"] = h.category
    fl["tariff"]["category"]["value"] = h.category
    nxt = h.last + timedelta(days=3)
    p = fl["period"]
    p["from"]["value"], p["to"]["value"] = h.first.isoformat(), h.last.isoformat()
    p["bill_date"]["value"], p["bill_month"]["value"] = nxt.isoformat(), nxt.strftime("%Y-%m")
    p["due_date"]["value"] = (nxt + timedelta(days=15)).isoformat()
    fl["readings"] = []
    fl["energy"]["kwh"]["value"], fl["energy"]["kvah"]["value"] = str(h.kwh), str(h.kvah)
    proto = fl["energy"]["tod"][0]
    fl["energy"]["tod"] = []
    for name, kwh in h.tod:
        row = copy.deepcopy(proto)
        row["slot"] = name
        row["kwh"]["value"] = str(kwh)
        fl["energy"]["tod"].append(row)
    fl["demand"]["max_kva"]["value"] = str(h.max_kva)
    fl["demand"]["contract_kva"]["value"] = str(h.contract_kva)
    fl["charges"]["energy"]["value"] = str(h.amount("energy_charge"))
    fl["charges"]["demand"]["value"] = str(h.amount("demand_charge"))
    proto_charge = fl["charges"]["by_code"][0]
    fl["charges"]["by_code"] = []
    proto_el = b["elements"][1]
    els = []
    for i, ln in enumerate(h.lines):
        c = copy.deepcopy(proto_charge)
        c["code"] = ln.code
        c["amount"]["value"] = str(ln.amount)
        fl["charges"]["by_code"].append(c)
        el = copy.deepcopy(proto_el)
        el.update(index=i, label=ln.label, code=ln.code, quantity=str(ln.quantity), rate=str(ln.rate))
        el["unit"] = None
        el["amount"]["value"] = str(ln.amount)
        el["amount"]["printed"] = f"{ln.amount:.2f}"
        el["formula_recheck"].update(computed=str(abs(ln.amount)), billed=str(abs(ln.amount)), difference="0")
        els.append(el)
    sub = copy.deepcopy(b["elements"][6])
    sub["index"], sub["amount"]["value"] = len(els), str(h.total)
    els.append(sub)
    arr = copy.deepcopy(b["elements"][7])
    arr["index"], arr["amount"]["value"] = len(els), "0"
    els.append(arr)
    net = copy.deepcopy(b["elements"][8])
    net["index"], net["amount"]["value"] = len(els), str(h.total)
    els.append(net)
    b["elements"] = els
    t = fl["totals"]
    t["current_charges"]["value"], t["arrears"]["value"] = str(h.total), "0"
    t["net_payable"]["value"] = str(h.total)
    res["file"]["sha256"] = f"assumed-{profile}-{h.first.isoformat()}"
    return res


class Run:
    """The API steps of one state's test, with every response kept (and written when asked)."""

    def __init__(self, out_env: str) -> None:
        self.out = os.environ.get(out_env)
        if self.out:
            Path(self.out).mkdir(parents=True, exist_ok=True)
        self.site = ""
        self.conn = ""

    def save(self, name: str, data: Any) -> None:
        if self.out:
            p = Path(self.out, name if "." in name else f"{name}.json")
            p.write_text(data if isinstance(data, str) else json.dumps(data, indent=1, default=str))

    def get(self, name: str, path: str, ok: tuple[int, ...] = (200,), **kw: Any) -> Any:
        r = client.get(path, headers=AN, **kw)
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text
        self.save(name, body)
        assert r.status_code in ok, f"{path}: {r.status_code} {r.text[:400]}"
        return body

    def post(self, name: str, path: str, ok: tuple[int, ...] = (200, 201, 202), **kw: Any) -> Any:
        r = client.post(path, headers=AN, **kw)
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else r.text
        self.save(name, body)
        assert r.status_code in ok, f"{path}: {r.status_code} {r.text[:400]}"
        return body

    def make(
        self,
        bills: Sequence[HandBill],
        profile: str,
        utility: str,
        state_name: str,
        overrides: dict[str, Any],
    ) -> dict[str, Any]:
        """Upload the bills (confirming what the stand-in reader leaves outstanding), build the
        intake from them with the client's typed overrides, and create the site."""
        restore_committed()
        mp = pytest.MonkeyPatch()
        fake = FakeBillx()
        mp.setattr(bill_store, "BillxClient", fake.client)
        ids = []
        try:
            for h in bills:
                tag = f"{profile}{h.first:%Y%m}"
                pdf = fake.register(tag, billx_reading(h, profile, utility, state_name))
                r = client.post("/bills", files={"file": (f"{tag}.pdf", pdf, "application/pdf")})
                assert r.status_code in (200, 201), r.text
                [bill] = r.json()
                for path in list(bill["outstanding"]):
                    value = next(x["value"] for x in bill["fields"] if x["path"] == path)
                    bill = client.post(
                        f"/bills/{bill['id']}/confirm", json={"path": path, "value": value}
                    ).json()
                ids.append(bill["id"])
        finally:
            mp.undo()
        self.save("00_hand_bills", {h.first.isoformat(): h.__dict__ for h in bills})
        si = client.post("/intake/from-bills", json={"bill_ids": ids, "overrides": overrides})
        assert si.status_code == 200, si.text
        self.save("02_intake", si.json())
        out = client.post("/sites", json={"intake": si.json()})
        assert out.status_code == 201, out.text
        self.save("02_site", out.json())
        self.site, self.conn = out.json()["site_id"], out.json()["connection_id"]
        return dict(si.json())

    def profile_kw(self) -> list[D]:
        """The connection's 15-minute load (kW) from its profile file: the load the engines settle."""
        r = client.get(f"/connections/{self.conn}/profile.csv", headers=AN)
        assert r.status_code == 200, r.text[:300]
        rows = list(csv.reader(io.StringIO(r.text)))
        head = rows[0]
        col = next(i for i, h in enumerate(head) if "kw" in h.lower())
        return [D(x[col]) for x in rows[1:] if x]

    def dispatch_rows(self, run_id: str) -> list[dict[str, str]]:
        r = client.get(f"/runs/{run_id}/dispatch.csv", headers=AN)
        assert r.status_code == 200, r.text[:300]
        self.save(f"dispatch_{run_id[:8]}.csv", r.text)
        return list(csv.DictReader(io.StringIO(r.text)))


YEAR_START = date(2026, 4, 1)
CATS = ("peak", "normal", "off_peak")  # value order of the ToD categories


def injection(peak_kw: D, blocks: int) -> list[D]:
    """A plain solar day, the same every day: the share of `peak_kw` injected in each hour from
    06:00 to 18:00 (a stand-in curve, so the settlement can be worked by hand from it)."""
    shape = [D(0)] * 6 + [D(x) for x in ("0.1", "0.3", "0.5", "0.7", "0.85", "0.95", "1", "0.95", "0.85", "0.7", "0.5", "0.3")] + [D(0)] * 6  # fmt: skip
    return [peak_kw * shape[(i % 96) // 4] for i in range(blocks)]


def month_of(i: int) -> date:
    d = YEAR_START + timedelta(days=i // 96)
    return date(d.year, d.month, 1)


def hand_settle(
    load_kw: Sequence[D],
    inj_kw: Sequence[D],
    delivered: D,
    cat_of: Any,  # (block index) -> peak | normal | off_peak, from the state's ToD windows
    basis: str,  # "block": set off in each 15 minutes; "tod": on each month's ToD-category totals
    banking: bool,
    limit_drawal_pct: D | None,  # banking up to this share of the month's drawal after set-off
    charge_pct: D,  # banking charge in kind, percent of the energy banked
) -> dict[date, dict[str, D]]:
    """A monthly settlement worked from the rule's words, independent of ergPLAN's engine: set-off,
    then the surplus banked (up to the limit, less the charge), drawn against the shortfall the
    highest-value category first (energy banked in a category may serve it and every lower one),
    and the rest lapses at the month's end. kWh per month."""
    cells: dict[tuple[date, str], dict[str, D]] = {}
    for i, (ld, g) in enumerate(zip(load_kw, inj_kw, strict=True)):
        c = cells.setdefault(
            (month_of(i), cat_of(i)), {"cons": D(0), "gen": D(0), "direct": D(0), "s": D(0), "n": D(0)}
        )
        lk, gk = ld / 4, g * delivered / 4
        c["cons"] += lk
        c["gen"] += gk
        if basis == "block":
            d = min(lk, gk)
            c["direct"] += d
            c["s"] += gk - d
            c["n"] += lk - d
    out: dict[date, dict[str, D]] = {}
    for m in sorted({k[0] for k in cells}):
        cs = {cat: cells.get((m, cat)) for cat in CATS}
        for c in cs.values():
            if c is not None and basis == "tod":
                c["direct"] = min(c["cons"], c["gen"])
                c["s"], c["n"] = c["gen"] - c["direct"], c["cons"] - c["direct"]
        s = {k: (c["s"] if c else D(0)) for k, c in cs.items()}
        n = {k: (c["n"] if c else D(0)) for k, c in cs.items()}
        total_s, total_n = sum(s.values(), D(0)), sum(n.values(), D(0))
        b = D(0)
        if banking:
            b = total_s if limit_drawal_pct is None else min(total_s, limit_drawal_pct / 100 * total_n)
        keep = 1 - charge_pct / 100
        cap = b * keep
        bank = {k: v * keep for k, v in s.items()}
        drawn = D(0)
        for to in CATS:  # peak, then normal, then off-peak
            want = n[to]
            for frm in reversed(CATS[: CATS.index(to) + 1]):  # its own bank first, then higher ones
                take = min(want, bank[frm], cap - drawn)
                bank[frm] -= take
                want -= take
                drawn += take
        direct = sum((c["direct"] for c in cs.values() if c), D(0))
        cons = sum((c["cons"] for c in cs.values() if c), D(0))
        out[m] = {
            "consumption": cons,
            "delivered": sum((c["gen"] for c in cs.values() if c), D(0)),
            "setoff": direct,
            "surplus": total_s,
            "banked_before": b,
            "charge": b - cap,
            "banked": cap,
            "withdrawn": drawn,
            "lapsed": cap - drawn,
            "unbanked": total_s - b,
            "grid": cons - direct - drawn,
        }
    return out


def replay_by_group(rep: list[dict[str, Any]]) -> dict[date, dict[str, tuple[D | None, D | None, str]]]:
    """The replay per supply month: group -> (billed, computed, status)."""
    out: dict[date, dict[str, tuple[D | None, D | None, str]]] = {}
    for r in rep:
        m = date.fromisoformat(r["evaluated_on"]).replace(day=1)
        out[m] = {
            ln["group"]: (
                dec(ln["billed"]) if ln.get("billed") else None,
                dec(ln["computed"]) if ln.get("computed") else None,
                ln["status"],
            )
            for ln in r["lines"]
        }
    return out


def landed_source(run: Run, name: str, scenario: dict[str, Any], source: str) -> dict[str, Any]:
    """One source's landed cost for the scenario: its lines by label (first slot) and every slot's total."""
    d = run.get(name, f"/connections/{run.conn}/landed-cost", params={"scenario": json.dumps(scenario)})
    s = next(x for x in d["sources"] if x["id"] == source)
    first = s["by_slot"][0]
    return {
        "lines": {str(ln["label"]): ln for ln in first["lines"]},
        "totals": {sl["slot"]: dec(sl["total"]) for sl in s["by_slot"]},
        "source": s,
        "gaps": d["gaps"],
    }


def settle(run: Run, name: str, kind: str, own: str, inj_kw: Sequence[D]) -> dict[str, Any]:
    body = {
        "sources": [
            {"id": "pv", "kind": kind, "capacity_mw": "1", "ownership": own, "kw": [str(x) for x in inj_kw]}
        ],
        "as_of": "2026-11-01",
    }
    return dict(run.post(name, f"/connections/{run.conn}/settle", json=body))


def compare_settlement(app: dict[str, Any], hand: dict[date, dict[str, D]]) -> list[str]:
    """Each month's figures, the app's against the hand's; the app prints kWh to three decimals."""
    bad = []
    for m in app["months"]:
        h = hand[date.fromisoformat(m["month"])]
        got = {
            "consumption": dec(m["consumption"]),
            "setoff": sum((dec(v) for v in m["setoff"].values()), D(0)),
            "banked": dec(m["banked"]),
            "charge": dec(m["charge"]),
            "withdrawn": dec(m["withdrawn"]),
            "lapsed": dec(m["lapsed"]),
            "grid": dec(m["grid"]),
            "unbanked": dec(m["unbanked"]),
        }
        for k, v in got.items():
            if abs(v - h[k]) > D("0.002"):
                bad.append(f"{m['month']} {k}: app {v}, by hand {h[k]:.3f}")
    return bad


def optimise(run: Run, candidates: list[dict[str, Any]], res: int, tag: str) -> dict[str, Any]:
    spec = {"candidates": candidates, "as_of": "2026-11-01", "resolution_min": res}
    sc = run.post(
        f"10_scenario_{tag}",
        f"/sites/{run.site}/scenarios",
        json={"connection_id": run.conn, "name": f"five components {tag}", "spec": spec},
    )
    r = run.post(f"11_run_{tag}", f"/scenarios/{sc['id']}/run", params={"wait": "true"})
    return dict(run.get(f"12_run_detail_{tag}", f"/runs/{r['id']}"))


FIVE = [
    {"kind": "rooftop_pv", "max_mw": "1"},
    {"kind": "oa_solar_intra", "ownership": "captive", "max_mw": "8"},
    {"kind": "oa_wind_intra", "ownership": "captive", "max_mw": "5"},
    {"kind": "btm_bess", "max_mw": "2"},
    {"kind": "oa_bess", "max_mw": "2"},
]


def dispatch_balance(rows: list[dict[str, str]], cell: Any = None) -> D:
    """The year's energy balance from the dispatch file alone: behind the meter, load = metered +
    rooftop + battery discharge − battery charge; at the meter, metered = open-access energy set off
    in its block (the delivered energy up to the metered load; the rest is surplus, banked or not) +
    withdrawn from the bank + grid. `cell` (a row -> key) nets on the rule's unit instead of the block
    (a month's ToD category). Returns the larger of the two residuals (kWh)."""
    behind = meter = D(0)
    if cell is not None:
        sums: dict[Any, list[D]] = {}
        for r in rows:
            c = sums.setdefault(cell(r), [D(0)] * 4)
            oa = sum(
                (D(v) for k, v in r.items() if k.startswith("supply:") and k != "supply:rooftop_pv_kwh"), D(0)
            )
            for j, v in enumerate((D(r["metered_kwh"]), oa, D(r["withdrawn_kwh"]), D(r["grid_kwh"]))):
                c[j] += v
        meter = sum((m - min(o, m) - w - g for m, o, w, g in sums.values()), D(0))
    for r in rows:
        load, metered = D(r["load_kwh"]), D(r["metered_kwh"])
        btm = D(r.get("supply:rooftop_pv_kwh") or 0)
        btm += sum((D(v) for k, v in r.items() if k.startswith("discharge:")), D(0))
        btm -= sum((D(v) for k, v in r.items() if k.startswith("charge:")), D(0))
        oa = sum(
            (D(v) for k, v in r.items() if k.startswith("supply:") and k != "supply:rooftop_pv_kwh"), D(0)
        )
        behind += load - metered - btm
        if cell is None:
            meter += metered - min(oa, metered) - D(r["withdrawn_kwh"]) - D(r["grid_kwh"])
    return max(abs(behind), abs(meter))


def monthly(rows: list[dict[str, str]], col: str) -> dict[str, dict[str, D]]:
    """A dispatch column per month and slot, and its highest hourly (or 15-minute) mean kW."""
    out: dict[str, dict[str, D]] = {}
    for r in rows:
        m = out.setdefault(r["time"][:7], {"__peak_kw": D(0)})
        v = D(r[col])
        m[r["slot"]] = m.get(r["slot"], D(0)) + v
        m["__peak_kw"] = max(m["__peak_kw"], v * 60 / minutes(rows))
    return out


def minutes(rows: list[dict[str, str]]) -> int:
    a, b = rows[0]["time"], rows[1]["time"]
    return (int(b[11:13]) * 60 + int(b[14:16]) - int(a[11:13]) * 60 - int(a[14:16])) % 1440


def assert_balance(hand: dict[date, dict[str, D]]) -> None:
    """The settlement's energy balance closed by hand, month by month."""
    tiny = D("0.000001")
    for m, h in hand.items():
        assert abs(h["consumption"] - (h["setoff"] + h["withdrawn"] + h["grid"])) < tiny, m
        assert abs(h["delivered"] - (h["setoff"] + h["banked_before"] + h["unbanked"])) < tiny, m
        assert abs(h["banked_before"] - (h["charge"] + h["withdrawn"] + h["lapsed"])) < tiny, m


def optimiser_checks(run: Run, grid_bill: Any, extra: Any = None, cell: Any = None) -> dict[str, Any]:
    """Five components at hourly blocks and at 15 minutes: each run done and re-settled, its dispatch
    file's energy balanced, the grid bill before and after recomputed from the file by the state's
    hand tariff (`grid_bill(energy by month and slot, peak kW by month)`) equal to the run's to the
    rupee (the file prints kWh to three decimals), and the hourly run replayed identically."""
    out: dict[str, Any] = {}
    # the machine these tests run on is shared: the solver's limit (300 s in production) is raised so a
    # run's result does not depend on the load; the seconds each run took are recorded and reported
    mp = pytest.MonkeyPatch()
    mp.setattr(get_settings(), "ergplan_optimizer_time_limit_s", OPTIMIZER_LIMIT_S)
    try:
        out.update(_optimiser_runs(run, grid_bill, extra, cell))
    finally:
        mp.undo()
    run.save("98_runs", out)
    return out


OPTIMIZER_LIMIT_S = 600.0


def _optimiser_runs(run: Run, grid_bill: Any, extra: Any, cell: Any) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for res, tag in ((60, "hourly"), (15, "15min")):
        r = optimise(run, FIVE, res, tag)
        assert r["status"] == "done", r["error"]
        assert r["analyst"]["resettle"]["agrees"] is True
        sol = r["solver"]
        out[tag] = {
            "seconds": sol["seconds"],
            "gap": sol["gap"],
            "status": sol["status"],
            "blocks": sol["blocks"],
        }
        rows = run.dispatch_rows(r["id"])
        out[tag]["balance_kwh"] = str(dispatch_balance(rows, cell))
        # each of the row's kWh columns is printed to 0.5 Wh: that is the most the balance can miss by
        b_tol = D("0.0005") * len(rows) * sum(1 for k in rows[0] if k.endswith("_kwh"))
        out[tag]["balance_print_bound_kwh"] = str(b_tol)
        assert dispatch_balance(rows, cell) <= b_tol, (dispatch_balance(rows, cell), b_tol)
        load, metered = monthly(rows, "load_kwh"), monthly(rows, "metered_kwh")
        before = grid_bill(load, {m: v["__peak_kw"] for m, v in load.items()})
        after = grid_bill(monthly(rows, "grid_kwh"), {m: v["__peak_kw"] for m, v in metered.items()})
        la, lb = r["analyst"]["lines_after"], r["analyst"]["lines_before"]
        # the file prints kWh to three decimals: each row is within 0.5 Wh of the run's own figure,
        # so the bill from the file is within 0.0005 kWh x the dearest rate x the rows (energy) and
        # 0.0005 kWh on each month's peak (demand) of the run's; the differences are recorded
        e_tol = D("0.0005") * len(rows) * before["max_rate"]
        d_tol = 12 * D("0.0005") * 60 / minutes(rows) * before["max_demand_rate"]
        diffs = {
            "energy_after": D(la["grid_energy"]) - after["energy"],
            "demand_after": D(la["demand"]) + D(la["excess_demand"]) - after["demand"],
            "energy_before": D(lb["grid_energy"]) - before["energy"],
            "demand_before": D(lb["demand"]) + D(lb["excess_demand"]) - before["demand"],
        }
        out[tag]["run_minus_bill_from_file"] = {k: str(q2(v)) for k, v in diffs.items()}
        out[tag]["print_precision_bound"] = {"energy": str(q2(e_tol)), "demand": str(q2(d_tol))}
        for k, v in diffs.items():
            assert abs(v) <= (e_tol if k.startswith("energy") else d_tol), (tag, k, v, e_tol, d_tol)
        out[tag]["grid_before"] = q2(before["energy"] + before["demand"])
        out[tag]["grid_after"] = q2(after["energy"] + after["demand"])
        out[tag]["annual"] = {k: r["summary"]["annual"][k]["value"] for k in ("before", "after", "saving")}
        out[tag]["sizes_kw"] = {x["id"]: x["kw"]["value"] for x in r["summary"]["sizes"]}
        out[tag]["settlement"] = {
            k: v["value"] for k, v in r["summary"]["settlement"].items() if isinstance(v, dict)
        }
        if extra is not None:
            out[tag]["extra"] = extra(rows, r)
        if tag == "hourly":
            again = run.post("13_replay", f"/runs/{r['id']}/replay", params={"wait": "true"})
            assert again["identical_to_original"] is True
    return out


def q4(x: D) -> D:
    return x.quantize(D("0.0001"))


def dec(x: Any) -> D:
    if isinstance(x, dict):
        x = x.get("value")
    return D(str(x))


def q2(x: D) -> D:
    return x.quantize(D("0.01"))
