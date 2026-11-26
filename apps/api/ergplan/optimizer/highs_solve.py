"""Solve a built model with HiGHS and read the solution back. The Pyomo model is compiled to its
matrix once and handed to HiGHS directly (highspy). A model with few integer columns (the banking
guards and the like) is searched here, branch and bound over HiGHS's simplex; one with many (a
thermal fleet's commitment) goes to HiGHS's own MIP search. Deterministic for the same problem and
options (one thread, a fixed seed, a fixed order of rows, columns and nodes), which the replay test
relies on."""

from __future__ import annotations

import heapq
import math
import time
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import date
from typing import Any

import highspy
import numpy as np
import pyomo
import pyomo.environ as pyo
from pyomo.repn.linear import LinearRepnVisitor
from pyomo.repn.util import VarRecorder

from .model import (
    Fixed,
    Netting,
    Problem,
    Start,
    build,
    build_settlement,
    chunk,
    coarsen,
    months_of,
    periods_of,
)

_HIGHS = getattr(highspy, "__version__", "1.15.1")
# "search 3": the matrix handed to HiGHS directly and the search of few-integer models here
# (25 Nov 2026); "search 2" was HiGHS's own search with the TUNING options, through Pyomo's interface
SOLVER_VERSION = f"highspy {_HIGHS} / pyomo {pyomo.__version__} / search 3"


class SolveFailed(RuntimeError):
    pass


class NoSolutionInTime(SolveFailed):
    """The search ended on its time limit before it found any solution."""


# `Solution.status`: proven within the gap; the best plan found when the time limit ended the search;
# no plan found in time, so today's supply (nothing built, the contract demand as it is) stands in
OPTIMAL = "optimal"
TIME_LIMIT = "time_limit"
STATUS_QUO = "status_quo_time_limit"


@dataclass
class Solution:
    status: str
    objective: float
    gap: float | None
    seconds: float
    solver: str
    cap_kw: dict[str, float]
    bess_kw: dict[str, float]
    bess_kwh: dict[str, float]
    cd_kva: float
    delivered: dict[str, list[float]]  # OA candidate → kWh at the meter per block (must-take)
    gen: dict[str, list[float]]  # behind-the-meter candidate → kWh used per block
    charge: dict[str, list[float]]
    discharge: dict[str, list[float]]
    soc: dict[str, list[float]]
    thermal: dict[str, list[float]]
    metered: list[float]  # load net of behind-the-meter supply, per block
    md_kva: dict[date, float]
    settled: dict[date, dict[str, float]]  # the solver's own settlement, per month
    binding: list[str] = field(default_factory=list)
    # open-access candidate → kWh per block a battery at the plant took before delivery (the energy
    # was generated and is paid for; `delivered` is net of it)
    oa_charged: dict[str, list[float]] = field(default_factory=dict)
    # where the time went, pass by pass (sizing, each month's dispatch, each period's settlement)
    passes: list[Pass] = field(default_factory=list)


@dataclass
class Pass:
    """One solve: its size, how it was searched and where its time went."""

    name: str = ""
    rows: int = 0
    cols: int = 0
    integers: int = 0
    search: str = "lp"  # lp | tree (here) | highs (HiGHS's MIP search)
    nodes: int = 0
    lp_iterations: int = 0
    gap: float | None = None
    optimal: bool = True
    build_s: float = 0.0  # the Pyomo model
    load_s: float = 0.0  # its matrix, into HiGHS
    held_s: float = 0.0  # tree: the search with storage held at zero
    held_nodes: int = 0
    root_s: float = 0.0  # the whole problem's first LP (tree) or the whole run (lp, highs)
    tree_s: float = 0.0  # tree: the whole problem's nodes
    read_s: float = 0.0  # values back into the model


# HiGHS's own MIP search, for models with many integer columns. The models have few binaries over
# tens of thousands of continuous variables, so each node's LP is the cost. HiGHS's sub-problem
# heuristics, symmetry detection and strong branching spent more than half of the sizing pass without
# changing the answer (measured on the 5 MVA Uttar Pradesh site at 15 minutes, 25 Nov 2026: 138 s to
# 84 s, the same optimum to the paisa).
TUNING: dict[str, Any] = {
    "mip_detect_symmetry": False,
    "mip_heuristic_run_rins": False,
    "mip_heuristic_run_rens": False,
    "mip_heuristic_run_root_reduced_cost": False,
    "mip_pscost_minreliable": 0,
    "mip_allow_cut_separation_at_nodes": False,
}
# Every LP: the dual simplex with Devex pricing. On these models (one row per block for each
# storage and settlement relation, nearly as many pivots as rows) steepest-edge pricing takes the
# same number of pivots at about twice the time; HiGHS's MIP search does not let it be chosen.
LP_OPTIONS: dict[str, Any] = {
    "threads": 1,
    "random_seed": 0,
    "output_flag": False,
    "solver": "simplex",
    "simplex_dual_edge_weight_strategy": 1,
}
# A model with no more integer columns than this is searched here: for a handful of binaries
# HiGHS's MIP search spends longer before its first branch (an interior-point solve for a rounding
# heuristic, with steepest-edge pivots throughout) than the whole tree takes with warm-started LPs.
TREE_MAX_INTEGERS = 40
HOLD_SHARE = 0.25  # of a solve's time, at most, for the search with storage held at zero
INTEGRAL = 1e-6  # an integer column is integral within this (HiGHS's mip_feasibility_tolerance)
_INF = float(highspy.kHighsInf)
_OPTIMAL = highspy.HighsModelStatus.kOptimal
_TIME = highspy.HighsModelStatus.kTimeLimit
_INFEASIBLE = highspy.HighsModelStatus.kInfeasible


@dataclass
class _Matrix:
    """A model as HiGHS takes it: rows in the order they were added, columns in the order the
    variables were declared."""

    cols: list[Any]  # the Pyomo variable of each column
    lower: Any
    upper: Any
    cost: Any
    offset: float
    integers: list[int]  # columns that must be integral
    start: Any  # row-wise matrix
    index: Any
    value: Any
    row_lower: Any
    row_upper: Any


def compile_model(m: Any) -> _Matrix:
    """The model's rows, bounds and objective as arrays (fixed variables folded into constants)."""
    var_map: dict[int, Any] = {}
    visitor = LinearRepnVisitor({}, var_recorder=VarRecorder(var_map, None))
    ids: list[int] = []
    vals: list[float] = []
    start = [0]
    lo: list[float] = []
    hi: list[float] = []
    for c in m.cons.values():
        lb, body, ub = c.to_bounded_expression(True)
        repn = visitor.walk_expression(body)
        if repn.nonlinear is not None:
            raise SolveFailed("a constraint is not linear")
        ids.extend(repn.linear)
        vals.extend(repn.linear.values())
        start.append(len(ids))
        lo.append(-_INF if lb is None else lb - repn.constant)
        hi.append(_INF if ub is None else ub - repn.constant)
    obj = visitor.walk_expression(m.obj.expr)
    if obj.nonlinear is not None:
        raise SolveFailed("the objective is not linear")
    cols = list(var_map.values())
    col = {vid: j for j, vid in enumerate(var_map)}
    cost = np.zeros(len(cols))
    for vid, coef in obj.linear.items():
        cost[col[vid]] = coef
    lower = np.empty(len(cols))
    upper = np.empty(len(cols))
    integers = []
    for j, v in enumerate(cols):
        lb, ub = v.bounds
        lower[j] = -_INF if lb is None else lb
        upper[j] = _INF if ub is None else ub
        if v.is_integer():
            integers.append(j)
    return _Matrix(
        cols=cols,
        lower=lower,
        upper=upper,
        cost=cost,
        offset=float(obj.constant),
        integers=integers,
        start=np.array(start[:-1], dtype=np.int32),
        index=np.fromiter((col[i] for i in ids), dtype=np.int32, count=len(ids)),
        value=np.array(vals, dtype=np.float64),
        row_lower=np.array(lo, dtype=np.float64),
        row_upper=np.array(hi, dtype=np.float64),
    )


def _load(mx: _Matrix) -> Any:
    h = highspy.Highs()  # type: ignore[no-untyped-call]
    for k, v in LP_OPTIONS.items():
        h.setOptionValue(k, v)
    n, rows = len(mx.cols), len(mx.row_lower)
    bad = highspy.HighsStatus.kError
    if n and h.addVars(n, mx.lower, mx.upper) == bad:
        raise SolveFailed("HiGHS refused the columns")
    if n and h.changeColsCost(n, np.arange(n, dtype=np.int32), mx.cost) == bad:
        raise SolveFailed("HiGHS refused the objective")
    h.changeObjectiveOffset(mx.offset)
    if (
        rows
        and h.addRows(rows, mx.row_lower, mx.row_upper, len(mx.index), mx.start, mx.index, mx.value) == bad
    ):
        raise SolveFailed("HiGHS refused the rows")
    return h


def _lp(h: Any, deadline: float) -> Any:
    """Run the LP as it stands (warm from the basis HiGHS holds) up to the deadline. HiGHS counts
    its time limit from its first run, not from this one."""
    h.setOptionValue("time_limit", h.getRunTime() + max(1.0, deadline - time.perf_counter()))
    h.run()
    return h.getModelStatus()


def _slack(bound: float) -> float:
    return 1e-7 * max(1.0, abs(bound)) if math.isfinite(bound) else 0.0


def _fail(h: Any, status: Any) -> SolveFailed:
    return SolveFailed(f"solver ended with {h.modelStatusToString(status)}")


def _tree(
    h: Any, mx: _Matrix, deadline: float, mip_gap: float, out: Pass, hold: list[int]
) -> list[float] | None:
    """Branch and bound over the integer columns, HiGHS solving each node's LP from its parent's
    basis. Depth first from each node taken (the child on the side the LP leans to), then the open
    node with the lowest bound; it stops when the best solution is within `mip_gap` of the lowest
    bound still open. Returns the best solution's column values (None: none found in time).

    `hold` (storage sizes that may be zero): the same search is first run with those columns held
    at zero. That is the problem without storage, several times smaller; every solution of it is a
    solution of the whole problem, so its best one starts the whole search as the solution to beat
    (often already within the gap of the whole problem's first bound), and its first basis starts
    the whole problem's first LP (fewer pivots than from nothing on every site measured). Bounds
    come only from the whole problem."""
    ints = mx.integers
    home = {j: (float(mx.lower[j]), float(mx.upper[j])) for j in ints}
    Bounds = dict[int, tuple[float, float]]
    t0 = time.perf_counter()
    # the rows each integer column stands in, for `rounded`
    ends = np.append(mx.start[1:], len(mx.index))
    at_int = np.nonzero(np.isin(mx.index, ints))[0]
    row_of = np.searchsorted(mx.start, at_int, side="right") - 1
    rows_of: dict[int, list[tuple[int, float]]] = {j: [] for j in ints}
    for k, r in zip(at_int.tolist(), row_of.tolist(), strict=True):
        rows_of[int(mx.index[k])].append((r, float(mx.value[k])))

    def rounded(x: list[float]) -> Bounds | None:
        """Whole values for the integer columns that leave every row they stand in satisfied with
        the other columns where the LP put them (None: no such values). An LP often leaves a
        binary fractional that it does not need: a guard relaxed although the row it guards holds."""
        xs = np.asarray(x)
        act: dict[int, float] = {}
        out_: Bounds = {}
        for j in ints:
            for r, _ in rows_of[j]:
                if r not in act:
                    act[r] = float(mx.value[mx.start[r] : ends[r]] @ xs[mx.index[mx.start[r] : ends[r]]])
            lo, hi = home[j]
            near = float(round(x[j]))
            tries = [near] if abs(x[j] - near) <= INTEGRAL else [near, near + (1.0 if x[j] > near else -1.0)]
            for v in tries:
                if lo <= v <= hi and all(
                    mx.row_lower[r] - _slack(mx.row_lower[r])
                    <= act[r] + a * (v - x[j])
                    <= mx.row_upper[r] + _slack(mx.row_upper[r])
                    for r, a in rows_of[j]
                ):
                    for r, a in rows_of[j]:
                        act[r] += a * (v - x[j])
                    out_[j] = (v, v)
                    break
            else:
                return None
        return out_

    first_done: list[float] = []  # when the search under way finished its first LP

    def solve_node(bounds: Bounds, basis: Any, until: float) -> Any:
        for j in ints:
            lo, hi = bounds.get(j, home[j])
            h.changeColBounds(j, lo, hi)
        if basis is not None:
            h.setBasis(basis)
        status = _lp(h, until)
        out.nodes += 1
        out.lp_iterations += int(h.getInfo().simplex_iteration_count)
        return status

    def search(
        best: float, best_x: list[float] | None, root: Any, until: float
    ) -> tuple[float, list[float] | None, float, bool, Any]:
        """→ best value, its columns, the lowest bound left, whether the search ended within the
        gap, and the first LP's basis."""
        closed = math.inf  # the lowest LP value among nodes closed as integral
        heap: list[tuple[float, int, Bounds, Any]] = []  # open: bound, order, bounds, parent's basis
        seq = 0
        cur: tuple[float, Bounds, Any] | None = (-math.inf, {}, root)
        first: Any = None

        def lowest() -> float:
            return min(best, closed, heap[0][0]) if heap else min(best, closed)

        def within() -> bool:
            return best < math.inf and best - lowest() <= mip_gap * max(1.0, abs(best))

        while True:
            if cur is None:
                if not heap or within() or time.perf_counter() >= until:
                    break
                bound, _, bounds, basis = heapq.heappop(heap)
                cur = (bound, bounds, basis)
            bound, bounds, basis = cur
            cur = None
            status = solve_node(bounds, basis, until)
            if status == _TIME:
                seq += 1
                heapq.heappush(heap, (bound, -seq, bounds, basis))  # still open: its bound stands
                break
            if status == _INFEASIBLE:
                continue
            if status != _OPTIMAL:
                raise _fail(h, status)
            if first is None:
                first = h.getBasis()
                first_done.append(time.perf_counter())
            value = float(h.getInfo().objective_function_value)
            if value >= best:
                continue
            x = h.getSolution().col_value
            off, at = max((min(abs(x[j] - round(x[j])), 1.0), -j) for j in ints)
            if off <= INTEGRAL:
                # integral within the tolerance: the node is closed at its LP value. The solution
                # kept is the one with every integer column held at its whole value, where that
                # solves.
                closed = min(closed, value)
                whole = {j: (float(round(x[j])), float(round(x[j]))) for j in ints}
                moved = any(whole[j] != bounds.get(j) for j in ints)
                if moved and solve_node(whole, None, until) == _OPTIMAL:
                    value = float(h.getInfo().objective_function_value)
                    x = h.getSolution().col_value
                if value < best:
                    best, best_x = value, x
                    heap = [node for node in heap if node[0] < best]
                    heapq.heapify(heap)
                continue
            j = -at
            here = h.getBasis()
            # whole values the LP's own solution allows: a solution at (or near) this node's bound
            whole = rounded(x) or {}
            if whole and solve_node(whole, None, until) == _OPTIMAL:
                found = float(h.getInfo().objective_function_value)
                if found < best:
                    best, best_x = found, h.getSolution().col_value
                    heap = [node for node in heap if node[0] < best]
                    heapq.heapify(heap)
                if found <= value + 1e-9 * max(1.0, abs(value)):
                    closed = min(closed, value)  # nothing below it in this node
                    continue
            lo, hi = bounds.get(j, home[j])
            down = {**bounds, j: (lo, float(math.floor(x[j])))}
            up = {**bounds, j: (float(math.ceil(x[j])), hi)}
            lean, other = (up, down) if x[j] - math.floor(x[j]) >= 0.5 else (down, up)
            seq += 2
            heapq.heappush(heap, (value, seq, other, here))
            if best_x is None:
                cur = (value, lean, here)  # no solution yet: dive for one
            else:
                heapq.heappush(heap, (value, seq - 1, lean, here))
        return best, best_x, lowest(), (not heap or within()), first

    best, best_x, start = math.inf, None, None
    if hold:
        for j in hold:
            h.changeColBounds(j, 0.0, 0.0)
        best, best_x, _, _, start = search(best, best_x, None, t0 + HOLD_SHARE * (deadline - t0))
        for j in hold:
            h.changeColBounds(j, float(mx.lower[j]), float(mx.upper[j]))
        out.held_s = time.perf_counter() - t0
        out.held_nodes = out.nodes
    # the whole problem, its first LP from the held problem's first basis (a vertex of it too)
    t1 = time.perf_counter()
    first_done.clear()
    best, best_x, low, ended, _ = search(best, best_x, start, deadline)
    out.root_s = (first_done[0] if first_done else time.perf_counter()) - t1
    out.tree_s = time.perf_counter() - t1 - out.root_s
    if best_x is not None:
        # no bound at all (the whole problem's first LP did not finish): the gap is not known
        out.gap = abs(best - low) / max(1.0, abs(best)) if math.isfinite(low) else None
        out.optimal = ended
    return best_x


def _run(m: Any, time_limit_s: float, mip_gap: float, name: str = "", hold: Any = ()) -> Pass:
    """Solve the model and leave the solution in its variables. `hold`: variables (storage sizes)
    the tree search first holds at zero (see _tree)."""
    out = Pass(name=name)
    t0 = time.perf_counter()
    deadline = t0 + max(1.0, time_limit_s)
    mx = compile_model(m)
    h = _load(mx)
    out.rows, out.cols, out.integers = len(mx.row_lower), len(mx.cols), len(mx.integers)
    out.load_s = time.perf_counter() - t0
    t1 = time.perf_counter()
    x: list[float] | None
    if not mx.integers:
        # a plain LP is solved from nothing: starting it from the LP without storage is quicker
        # (15 s against 24 s on the third-party five-component site) but lands on another of two
        # equal-cost vertices there (the battery behind the meter where the one at the plant was)
        status = _lp(h, deadline)
        out.lp_iterations = int(h.getInfo().simplex_iteration_count)
        if status == _TIME:
            raise NoSolutionInTime("solver ended on its time limit without a solution")
        if status != _OPTIMAL:
            raise _fail(h, status)
        x = h.getSolution().col_value
        out.root_s = time.perf_counter() - t1
        out.gap = 0.0
    elif len(mx.integers) <= TREE_MAX_INTEGERS:
        out.search = "tree"
        col = {id(v): j for j, v in enumerate(mx.cols)}
        held = [col[id(v)] for v in hold if id(v) in col and mx.lower[col[id(v)]] <= 0.0]
        x = _tree(h, mx, deadline, mip_gap, out, held)
        if x is None:
            if time.perf_counter() >= deadline:
                raise NoSolutionInTime("solver ended on its time limit without a solution")
            raise SolveFailed("solver ended infeasible without a solution")
    else:
        out.search = "highs"
        x = _mip(h, mx, deadline, mip_gap, out)
        out.root_s = time.perf_counter() - t1
    t2 = time.perf_counter()
    whole = set(mx.integers)
    for j, v in enumerate(mx.cols):
        v.set_value(float(round(x[j])) if j in whole else x[j], skip_validation=True)
    out.read_s = time.perf_counter() - t2
    return out


def _mip(h: Any, mx: _Matrix, deadline: float, mip_gap: float, out: Pass) -> list[float]:
    """HiGHS's own MIP search (TUNING), for a model with many integer columns."""
    kind = highspy.HighsVarType.kInteger
    h.changeColsIntegrality(
        len(mx.integers), np.array(mx.integers, dtype=np.int32), [kind] * len(mx.integers)
    )
    h.setOptionValue("solver", "choose")
    h.setOptionValue("mip_rel_gap", mip_gap)
    for k, v in TUNING.items():
        h.setOptionValue(k, v)
    status = _lp(h, deadline)
    info = h.getInfo()
    feasible = info.primal_solution_status == highspy.SolutionStatus.kSolutionStatusFeasible
    if status == _TIME and not feasible:
        raise NoSolutionInTime("solver ended on its time limit without a solution")
    if status not in (_OPTIMAL, _TIME) or not feasible:
        raise _fail(h, status)
    out.nodes, out.lp_iterations = int(info.mip_node_count), int(info.simplex_iteration_count)
    inc, bnd = float(info.objective_function_value), float(info.mip_dual_bound)
    out.gap = abs(inc - bnd) / max(1.0, abs(inc))
    out.optimal = status == _OPTIMAL
    x: list[float] = h.getSolution().col_value
    return x


def solve(p: Problem, time_limit_s: float = 120.0, mip_gap: float = 0.001) -> Solution:
    """Three passes.
    1. Sizing: capacities, contract demand and an hourly dispatch for the whole year (15-minute
       blocks are summed to hours), the bank guarded so it never takes energy the rule sets off first.
    2. Dispatch (15-minute problems only): sizes fixed, month by month at 15 minutes, carrying the
       battery's charge and the bank forward.
    3. Settlement: the dispatch fixed (read to the watt-hour, as the judge reads it), the solver
       settles it under the rule's order, banking period by banking period.
    The judge then settles the same dispatch again (optimizer.resettle)."""
    t0 = time.perf_counter()
    factor = 4 if p.hours == 0.25 else 1
    ps = coarsen(p, factor)
    passes: list[Pass] = []

    def run(make: Any, limit_s: float, gap_: float, name: str) -> Any:
        tb = time.perf_counter()
        model = make()
        built = time.perf_counter() - tb
        done = _run(model, limit_s, gap_, name, storage_sizes(model))
        done.build_s = built
        passes.append(done)
        return model

    fell_back = False
    try:
        m = run(lambda: build(ps), time_limit_s * 0.5, mip_gap, "sizing")
    except NoSolutionInTime:
        # no plan found in time: today's supply stands in (nothing built, the contract demand as it
        # is, no target or mandate, which only bind what is built), labelled as such, never as the
        # optimum. Its dispatch and settlement are solved as any plan's are
        fell_back = True
        passes.append(Pass(name="sizing", optimal=False))  # it found nothing
        ps = replace(ps, fixed=Fixed({}, {}, {}, ps.grid.cd_kva), limits=[])
        m = run(lambda: build(ps), max(30.0, time_limit_s * 0.5), mip_gap, "status quo")
    gap, optimal = passes[-1].gap, passes[-1].optimal
    size = read(m, ps)
    objective = float(pyo.value(m.obj))
    binding_names = size.binding
    if factor == 1:
        sol = size
    else:
        fixed = Fixed(size.cap_kw, size.bess_kw, size.bess_kwh, size.cd_kva)
        start = Start(soc_kwh={b: v[-1] for b, v in size.soc.items()})
        parts: list[Solution] = []
        for idx in months_of(p):
            cp = chunk(p, idx, start, fixed)
            # sizes fixed, a month is (nearly) an LP: a fair floor so a slower month still solves
            limit = max(30.0, (time_limit_s * 0.8 - (time.perf_counter() - t0)) / 6)
            mc = run(lambda cp=cp: build(cp), limit, mip_gap, f"dispatch {cp.blocks[0].month:%Y-%m}")
            part = read(mc, cp)
            parts.append(part)
            start = Start(
                soc_kwh={b: v[-1] for b, v in part.soc.items()},
                bank_kwh=_carried(mc),
                on={u: v[-1] > 0 for u, v in part.thermal.items()},
            )
        sol = _merge(size, parts)
    # proven only if the sizing and every month's dispatch were (the settlement passes are checked
    # by the judge)
    optimal = optimal and all(q.optimal for q in passes)
    sol.status = STATUS_QUO if fell_back else OPTIMAL if optimal else TIME_LIMIT
    if fell_back:
        gap = None
    sol.objective = objective
    sol.gap = gap
    sol.binding = binding_names
    settled: dict[date, dict[str, float]] = {}
    start = Start()
    for idx in periods_of(p):
        cp = chunk(p, idx, start, None)
        ms = run(
            lambda cp=cp, idx=idx: build_settlement(
                cp,
                [sol.metered[t] for t in idx],
                {k: [v[t] for t in idx] for k, v in sol.delivered.items()},
            ),
            max(10.0, time_limit_s - (time.perf_counter() - t0)),
            1e-9,
            f"settlement {cp.blocks[0].month:%Y-%m}",
        )
        for mo, row in read_settlement(ms, cp).items():
            settled[mo] = row
        start = Start(bank_kwh=_carried(ms))
    sol.settled = settled
    sol.passes = passes
    sol.seconds = time.perf_counter() - t0
    return sol


def storage_sizes(m: Any) -> list[Any]:
    """The size variables of a model's storage: batteries behind the meter (power) and at the
    open-access plant (the capacity of the candidate that delivers their output)."""
    out = [m.bkw[k] for k in m.bkw] if hasattr(m, "bkw") else []
    if hasattr(m, "okwh"):
        out += [m.cap[k] for k in m.okwh]
    return [v for v in out if not v.fixed]


def _carried(m: Any) -> dict[str, float]:
    """Bank balance carried out of a chunk: the last step's inventory less what lapsed there."""
    net: Netting = m.netting
    if not net.buckets or net.steps == 0:
        return {}
    last = net.steps - 1
    out = {}
    for bk in net.buckets:
        v = _v(m.inv[bk, last])
        if last in net.step_period_end:
            v -= _v(m.lapse[bk, net.period_of_step[last]])
        out[bk] = max(0.0, v)
    return out


def _merge(size: Solution, parts: list[Solution]) -> Solution:
    def cat(get: Any) -> dict[str, list[float]]:
        keys = get(parts[0]).keys()
        return {k: [x for pt in parts for x in get(pt)[k]] for k in keys}

    md: dict[date, float] = {}
    for pt in parts:
        md.update(pt.md_kva)
    return Solution(
        status="",
        objective=0.0,
        gap=None,
        seconds=0.0,
        solver=size.solver,
        cap_kw=size.cap_kw,
        bess_kw=size.bess_kw,
        bess_kwh=size.bess_kwh,
        cd_kva=size.cd_kva,
        delivered=cat(lambda s: s.delivered),
        gen=cat(lambda s: s.gen),
        charge=cat(lambda s: s.charge),
        discharge=cat(lambda s: s.discharge),
        soc=cat(lambda s: s.soc),
        thermal=cat(lambda s: s.thermal),
        metered=[x for pt in parts for x in pt.metered],
        md_kva=md,
        settled={},
        oa_charged={
            k: [x for pt in parts for x in pt.oa_charged.get(k, [0.0] * len(pt.metered))]
            for k in {k for pt in parts for k in pt.oa_charged}
        },
    )


def _v(x: Any) -> float:
    v = pyo.value(x, exception=False)
    return 0.0 if v is None or abs(v) < 1e-9 else float(v)


def read(m: Any, p: Problem) -> Solution:
    n = p.n
    cap = {c.id: _v(m.cap[c.id]) for c in p.candidates}
    xv = m.x.extract_values()
    r3 = _r3
    delivered = {
        c.id: (
            [r3(_clean(xv.get((c.id, t)))) for t in range(n)]
            if c.dispatchable
            else [r3(cap[c.id] * c.shape[t]) for t in range(n)]
        )
        for c in p.candidates
        if not c.btm
    }
    oa_charged: dict[str, list[float]] = {}
    for (k, t), vs in getattr(m, "taken", {}).items():
        got = sum(_v(v) for v in vs)
        if got > 0:
            row = oa_charged.setdefault(k, [0.0] * n)
            row[t] = min(r3(got), delivered[k][t])
            delivered[k][t] = r3(delivered[k][t] - row[t])
    gvals = m.gen.extract_values()
    gen = {c.id: [_clean(gvals.get((c.id, t))) for t in range(n)] for c in p.candidates if c.btm}
    ch = {b.id: _series(m.ch, b.id, n) for b in p.batteries}
    dis = {b.id: _series(m.dis, b.id, n) for b in p.batteries}
    soc = {b.id: _series(m.soc, b.id, n) for b in p.batteries}
    soc.update({ob.id: _series(m.osoc, ob.id, n) for ob in p.oa_batteries if ob.id in m.okwh})
    th = {u.id: _series(m.out, u.id, n) for u in p.thermals}
    metered = []
    for t in range(n):
        v = p.load_kwh[t]
        v -= sum(g[t] for g in gen.values())
        v -= sum(d[t] for d in dis.values())
        v += sum(c[t] for c in ch.values())
        v -= sum(o[t] for o in th.values())
        metered.append(r3(max(0.0, v)))
    md = {mo: _v(m.md[mo]) for mo in m.md}
    sol = Solution(
        status="",
        objective=0.0,
        gap=None,
        seconds=0.0,
        solver=SOLVER_VERSION,
        cap_kw=cap,
        bess_kw={b.id: _v(m.bkw[b.id]) for b in p.batteries},
        bess_kwh={
            **{b.id: _v(m.bkwh[b.id]) for b in p.batteries},
            **{ob.id: _v(m.okwh[ob.id]) for ob in p.oa_batteries if ob.id in m.okwh},
        },
        cd_kva=float(pyo.value(m.cd_expr)),
        delivered=delivered,
        gen=gen,
        charge=ch,
        discharge=dis,
        soc=soc,
        thermal=th,
        metered=metered,
        md_kva=md,
        settled={},
    )
    sol.oa_charged = oa_charged
    sol.binding = binding(m, p)
    return sol


def read_settlement(m: Any, p: Problem) -> dict[date, dict[str, float]]:
    """The solver's settlement per month, from a settlement-pass model. Used only to compare with the
    judge (optimizer.resettle); every cost anyone sees comes from settlement.simulate()."""
    net: Netting = m.netting
    # the solver's settlement per month
    settled: dict[date, dict[str, float]] = defaultdict(
        lambda: {"grid": 0.0, "setoff": 0.0, "withdrawn": 0.0, "banked": 0.0, "charge": 0.0, "lapsed": 0.0}
    )
    so, dep, w, grid = (
        m.so.extract_values(),
        m.dep.extract_values(),
        m.w.extract_values(),
        m.grid.extract_values(),
    )
    keep = 1 - net.charge_pct / 100
    for g, idx in enumerate(net.groups):
        mo = p.blocks[idx[0]].month
        settled[mo]["grid"] += _clean(grid.get(g))
    for (_k, g), v in so.items():
        settled[p.blocks[net.groups[g][0]].month]["setoff"] += _clean(v)
    for (_k, g), v in dep.items():
        mo = p.blocks[net.groups[g][0]].month
        settled[mo]["banked"] += keep * _clean(v)
        settled[mo]["charge"] += (1 - keep) * _clean(v)
    for (_b, g), v in w.items():
        settled[p.blocks[net.groups[g][0]].month]["withdrawn"] += _clean(v)
    step_month: dict[int, date] = {}
    for g, idx in enumerate(net.groups):
        step_month[net.group_step[g]] = p.blocks[idx[0]].month
    period_end_month = {net.period_of_step[s]: step_month[s] for s in net.step_period_end if s in step_month}
    for (_b, q), v in m.lapse.extract_values().items():
        if q in period_end_month:
            settled[period_end_month[q]]["lapsed"] += _clean(v)
    return dict(settled)


def _r3(x: float) -> float:
    return float(f"{x:.3f}")


def _clean(v: Any) -> float:
    if v is None:
        return 0.0
    f = float(v)
    return 0.0 if abs(f) < 1e-9 else f


def _series(var: Any, key: str, n: int) -> list[float]:
    vals = var.extract_values()
    return [_clean(vals.get((key, t))) for t in range(n)]


def binding(m: Any, p: Problem, tol: float = 1e-4) -> list[str]:
    """Names of constraints and bounds that hold the solution where it is."""
    out: list[str] = []
    for c in p.candidates:
        v = _v(m.cap[c.id])
        if c.max_kw > 0 and v >= c.max_kw * (1 - tol):
            out.append(f"max_kw:{c.id}")
        if c.min_size_kw > 0 and abs(v - c.min_size_kw) <= c.min_size_kw * tol:
            out.append(f"min_size:{c.id}")
    for b in p.batteries:
        if b.max_kw > 0 and _v(m.bkw[b.id]) >= b.max_kw * (1 - tol):
            out.append(f"max_kw:{b.id}")
    for i, name in m.cons_names.items():
        if not name.startswith(("limit:", "carry_cap", "cd_floor", "banking_limit")):
            continue
        c = m.cons[i]
        body = pyo.value(c.body, exception=False)
        if body is None:
            continue
        scale = max(1.0, abs(body))
        hit = (c.upper is not None and abs(pyo.value(c.upper) - body) <= tol * scale) or (
            c.lower is not None and abs(pyo.value(c.lower) - body) <= tol * scale
        )
        if hit:
            out.append(name)
    return sorted(set(out))
