"""
Pre-solve and post-solve coverage diagnosis.

The CP-SAT model reports a bare INFEASIBLE with no indication of why
(PHASE3_HANDOFF.md finding #1). diagnose_coverage_gaps() catches the
clearest and most common pre-solve cause up front: a weekend slot where
zero tech-capable people are available at all - weekend coverage is still
a hard constraint (exactly 1 tech-role person, no exceptions), so that
really does guarantee INFEASIBLE before ever building the model.

Weekday coverage is now a soft/penalized constraint instead (see
build_model.py) - a weekday headcount gap no longer guarantees
infeasibility, since the solver can still return a schedule that's
understaffed at that slot. summarize_coverage_shortfalls() reports those
after a feasible solve, from the shortfall variables build_model()
returns, so the boss still sees exactly which weekday slots came up
short even though the run "succeeded".

Neither function is a full feasibility prover - a slot can pass the
pre-solve check and the model can still end up INFEASIBLE for other
reasons (hours ceilings, block-length constraints, the no-two-rating-1s
rule, etc. interacting badly). Treat a clean pre-solve result as "no
obvious hard staffing gap", not "guaranteed feasible".
"""

from ortools.sat.python import cp_model

from model_input import DAYS, OPERATING_SLOTS, WEEKEND_DAYS, SolverInput

WEEKEND_TECHS_REQUIRED = 1


def diagnose_coverage_gaps(data: SolverInput) -> list[str]:
    """Return human-readable gaps that guarantee INFEASIBLE before ever
    building the CP-SAT model. Weekend-only - see module docstring for why
    weekday gaps are no longer diagnosed here.
    """
    problems = []

    for day in [d for d in DAYS if d in WEEKEND_DAYS]:
        for slot in OPERATING_SLOTS[day]:
            tech_available = [
                p for p in data.people if p.is_available(day, slot) and p.can_work_tech()
            ]
            if len(tech_available) < WEEKEND_TECHS_REQUIRED:
                problems.append(
                    f"{day} slot {slot}: needs {WEEKEND_TECHS_REQUIRED} tech-capable "
                    f"person, none available"
                )

    return problems


def summarize_coverage_shortfalls(solver: cp_model.CpSolver, variables: dict) -> list[str]:
    """Human-readable messages for weekday slots the solver accepted as
    understaffed (the soft coverage constraint in build_model.py). Only
    meaningful after a feasible solve - call with the CpSolver used to
    solve the model and the `variables` dict build_model() returned.
    Empty list means every weekday slot hit its 2+2 target.
    """
    messages = []
    for role, day, slot, shortfall_var in variables.get("coverage_shortfalls", []):
        shortfall = solver.Value(shortfall_var)
        if shortfall > 0:
            messages.append(f"{day} slot {slot}: understaffed by {shortfall} {role}(s)")
    return messages
