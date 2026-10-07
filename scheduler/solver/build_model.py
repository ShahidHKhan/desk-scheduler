"""
Builds the CP-SAT model for the service desk schedule.

Per-slot boolean variables rather than interval variables: every rule is
phrased per half-hour slot, so booleans keep each constraint a direct
translation of the rule. Rule numbers below match the README.
"""

from itertools import pairwise

from ortools.sat.python import cp_model

from scheduler.solver.model_input import (
    DAYS,
    OPERATING_SLOTS,
    ROLES,
    WEEKDAYS,
    WEEKEND_DAYS,
    SolverInput,
    role_capacity,
)


def build_model(data: SolverInput) -> tuple[cp_model.CpModel, dict]:
    model = cp_model.CpModel()
    people = data.people

    # --- Variables ---
    # x[person_id, day, slot, role] = 1 if that person works that slot in that role
    x = {}
    for p in people:
        for day in DAYS:
            for slot in OPERATING_SLOTS[day]:
                for role in ROLES:
                    x[p.id, day, slot, role] = model.NewBoolVar(f"x_{p.id}_{day}_{slot}_{role}")

    # --- Hard constraint: manual locks always win ---
    # Locks are pre-validated by locks.validate_locks() before this point - not
    # re-validated here, that's locks.py's job. A lock referencing a day/slot/role
    # combo that isn't in `x` (i.e. outside operating hours) raises KeyError, which
    # is intentional: a defensive check against a caller that skipped validation.
    for lock in data.locked_assignments:
        model.Add(x[lock.person_id, lock.day, lock.slot, lock.role] == (1 if lock.value else 0))

    # work[person_id, day, slot] = 1 if working ANY role that slot
    # (derived variable - a person can't be assistant AND tech in the same slot)
    work = {}
    for p in people:
        for day in DAYS:
            for slot in OPERATING_SLOTS[day]:
                roles_here = [x[p.id, day, slot, r] for r in ROLES]
                model.Add(sum(roles_here) <= 1)  # at most one role per slot
                w = model.NewBoolVar(f"work_{p.id}_{day}_{slot}")
                model.Add(w == sum(roles_here))
                work[p.id, day, slot] = w

    # --- Hard constraint: availability (Rule 3) ---
    for p in people:
        for day in DAYS:
            for slot in OPERATING_SLOTS[day]:
                if not p.is_available(day, slot):
                    model.Add(work[p.id, day, slot] == 0)

    # --- Hard constraint: capability boundary (Rule 2) ---
    for p in people:
        if not p.can_work_tech():
            for day in DAYS:
                for slot in OPERATING_SLOTS[day]:
                    model.Add(x[p.id, day, slot, "tech"] == 0)

    # --- Soft constraint: weekday coverage - target 2 assistant + 2 tech ---
    # (1 + 1 on Mon-Thu evenings - see model_input.role_capacity)
    # Was a hard "== 2" until real rosters showed it was too rigid: a
    # single understaffed slot (commonly the opening/closing slot, where
    # fewer people mark themselves available) made the ENTIRE schedule
    # infeasible, even when every other slot was fully covered. Now it's
    # a capped target - `<= target` still holds (so the solver can't stuff
    # extra people into a slot just to burn hours), but falling short is
    # allowed, at a heavy penalty in the objective below, so the solver
    # only accepts a shortfall when it truly can't do better.
    # Weekend coverage (below) stays hard - the desk must have someone
    # there every weekend slot, no exceptions.
    coverage_shortfalls = []  # (role, day, slot, shortfall_var)
    for day in [d for d in DAYS if d in WEEKDAYS]:
        for slot in OPERATING_SLOTS[day]:
            for role in ROLES:
                target = role_capacity(day, slot, role)
                in_role = [x[p.id, day, slot, role] for p in people]
                model.Add(sum(in_role) <= target)

                shortfall = model.NewIntVar(0, target, f"{role}_shortfall_{day}_{slot}")
                model.Add(shortfall >= target - sum(in_role))
                coverage_shortfalls.append((role, day, slot, shortfall))

    # --- Hard constraint: weekend coverage - exactly 1 tech-role person ---
    for day in [d for d in DAYS if d in WEEKEND_DAYS]:
        for slot in OPERATING_SLOTS[day]:
            techs = [x[p.id, day, slot, "tech"] for p in people]
            assistants = [x[p.id, day, slot, "assistant"] for p in people]
            model.Add(sum(techs) == 1)
            model.Add(sum(assistants) == 0)  # weekends are tech-only, no assistant role at all

    # --- Hard constraint: hours ceiling (Rule 1) ---
    # each slot = 0.5 hr, so sum of slots <= hours_requested * 2
    for p in people:
        total_slots = [work[p.id, day, slot] for day in DAYS for slot in OPERATING_SLOTS[day]]
        model.Add(sum(total_slots) <= p.hours_requested * 2)

    # --- Hard constraint: no two rating-1 people together on weekdays (Rule 4) ---
    rating_1_people = [p for p in people if p.experience_rating == 1]
    for day in [d for d in DAYS if d in WEEKDAYS]:
        for slot in OPERATING_SLOTS[day]:
            if len(rating_1_people) > 1:
                model.Add(sum(work[p.id, day, slot] for p in rating_1_people) <= 1)

    # --- Hard constraint: max block length 6 hrs = 12 slots (Rule 7) ---
    # Sliding window: no 13 consecutive slots can all be worked by the same person.
    for p in people:
        for day in DAYS:
            slots = list(OPERATING_SLOTS[day])
            window = data.max_block_slots + 1
            for i in range(len(slots) - window + 1):
                window_slots = slots[i : i + window]
                model.Add(sum(work[p.id, day, s] for s in window_slots) <= data.max_block_slots)

    # --- Hard constraint: min block length 2 hrs = 4 slots (Rule 7) ---
    # A block "starts" at slot s if work[s]=1 and work[s-1]=0 (or s is the
    # first operating slot of the day). If a block starts, force the next
    # min_block_slots consecutive slots to also be worked - UNLESS fewer
    # than min_block_slots slots remain in the day's operating window, in
    # which case a full-length block literally can't fit. That's the
    # "trailing sliver" case: allowed, not
    # forbidden, but tracked in sliver_starts below so the objective can
    # discourage it instead of silently accepting it for free.
    sliver_starts = []
    starts = {}  # (person_id, day) -> that day's start_vars
    for p in people:
        for day in DAYS:
            slots = list(OPERATING_SLOTS[day])
            starts[p.id, day] = []
            for idx, s in enumerate(slots):
                is_first = idx == 0
                prev_s = None if is_first else slots[idx - 1]

                start_var = model.NewBoolVar(f"start_{p.id}_{day}_{s}")
                if is_first:
                    model.Add(start_var == work[p.id, day, s])
                else:
                    # start_var == work[s] AND NOT work[prev_s]
                    model.AddBoolAnd([work[p.id, day, s], work[p.id, day, prev_s].Not()]).OnlyEnforceIf(start_var)
                    model.AddBoolOr([work[p.id, day, s].Not(), work[p.id, day, prev_s]]).OnlyEnforceIf(start_var.Not())
                starts[p.id, day].append(start_var)

                remaining = len(slots) - idx
                if remaining >= data.min_block_slots:
                    required_slots = slots[idx : idx + data.min_block_slots]
                    for req_s in required_slots:
                        model.AddImplication(start_var, work[p.id, day, req_s])
                else:
                    sliver_starts.append(start_var)

    # --- Objective: minimize the worst-off person's shortfall ratio (Rule 6) ---
    # Proportional fairness, first pass: minimize the maximum, across all
    # people, of (hours_requested - hours_assigned) / hours_requested.
    # Scaled to integers (CP-SAT requirement) as per-mille (0-1000).
    # NOTE: role-weighting ratio targets and proximity tiebreaking (the
    # Rule 2/Rule 5 soft components) are NOT yet in this objective.
    max_shortfall_permille = model.NewIntVar(0, 1000, "max_shortfall_permille")
    for p in people:
        total_slots = [work[p.id, day, slot] for day in DAYS for slot in OPERATING_SLOTS[day]]
        assigned_slots = sum(total_slots)  # linear expression, slots (0.5hr units)
        requested_slots = p.hours_requested * 2
        if requested_slots > 0:
            # (requested - assigned) * 1000 <= max_shortfall_permille * requested
            model.Add(
                (requested_slots - assigned_slots) * 1000 <= max_shortfall_permille * requested_slots
            )

    # Soft penalty on trailing slivers: each block that starts too close to closing to reach the
    # full min_block_slots length adds SLIVER_PENALTY_WEIGHT to the
    # objective. Deliberately small relative to max_shortfall_permille's
    # 0-1000 range - hours fairness is the primary business rule, this is
    # only meant to act as a tiebreaker between otherwise-equally-fair
    # schedules, not override fairness to avoid a sliver. Uncalibrated
    # placeholder, like MIN_POSITIONED_WORDS in pdf_parser.py - tune once
    # there's a real schedule to see how often slivers actually occur.
    SLIVER_PENALTY_WEIGHT = 10

    # Coverage shortfall dominates everything else in the objective: its
    # per-unit weight is set above max_shortfall_permille's entire 0-1000
    # range, so the solver always prefers covering one more weekday
    # slot/role over any possible fairness gain - it only accepts a
    # shortfall when no feasible assignment can avoid it. This keeps
    # weekday coverage "hard in practice, soft on paper": always solvable,
    # but never traded away cheaply.
    COVERAGE_SHORTFALL_WEIGHT = 2000
    total_coverage_shortfall = sum(var for _, _, _, var in coverage_shortfalls)

    model.Minimize(
        COVERAGE_SHORTFALL_WEIGHT * total_coverage_shortfall
        + max_shortfall_permille
        + SLIVER_PENALTY_WEIGHT * sum(sliver_starts)
    )

    # --- Shift shape: what makes a schedule read like a human wrote it ---
    # The objective above is indifferent between a clean 4-hour tech shift
    # and the same hours flipping tech/assistant every half hour, or split
    # into two pieces of one day. solve.py runs a second pass that holds
    # coverage and fairness at the first pass's values and minimizes this,
    # so a tidier schedule never costs a covered slot or anyone's hours.
    # All soft: a mid-shift role switch is still allowed when it's the only
    # way to cover a slot, it just has to earn its place.
    # Weights only compete with each other, in this order:
    ROLE_SWITCH_WEIGHT = 50  # tech <-> assistant between back-to-back half hours
    EXTRA_SHIFT_WEIGHT = 20  # a second (third...) shift for one person on one day
    SHIFT_START_WEIGHT = 5  # every shift: fewer, longer ones, as on the paper schedule

    role_switches = []
    for p in people:
        if not p.can_work_tech():
            continue  # one role only, so never switches
        for day in [d for d in DAYS if d in WEEKDAYS]:
            slots = list(OPERATING_SLOTS[day])
            for s, next_s in pairwise(slots):
                switch = model.NewBoolVar(f"switch_{p.id}_{day}_{s}")
                model.Add(switch >= x[p.id, day, s, "assistant"] + x[p.id, day, next_s, "tech"] - 1)
                model.Add(switch >= x[p.id, day, s, "tech"] + x[p.id, day, next_s, "assistant"] - 1)
                role_switches.append(switch)

    extra_shifts = []
    for (person_id, day), day_starts in starts.items():
        extra = model.NewIntVar(0, len(day_starts), f"extra_shifts_{person_id}_{day}")
        model.Add(extra >= sum(day_starts) - 1)
        extra_shifts.append(extra)

    shape_penalty = (
        ROLE_SWITCH_WEIGHT * sum(role_switches)
        + EXTRA_SHIFT_WEIGHT * sum(extra_shifts)
        + SHIFT_START_WEIGHT * sum(v for day_starts in starts.values() for v in day_starts)
        + SLIVER_PENALTY_WEIGHT * sum(sliver_starts)
    )

    variables = {
        "x": x,
        "work": work,
        "max_shortfall_permille": max_shortfall_permille,
        "sliver_starts": sliver_starts,
        "coverage_shortfalls": coverage_shortfalls,
        "total_coverage_shortfall": total_coverage_shortfall,
        "role_switches": role_switches,
        "shape_penalty": shape_penalty,
    }
    return model, variables
