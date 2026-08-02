"""
Pre-solve feasibility diagnosis.

The CP-SAT model reports a bare INFEASIBLE with no indication of why
(PHASE3_HANDOFF.md finding #1). This catches the clearest and most common
cause up front: a slot where zero people are available in a role the
coverage constraint hard-requires there - e.g. "Saturday 12:00 needs a
tech-capable person, none available" instead of just "no schedule found".

NOT a full feasibility prover. It only catches the "nobody at all
available" case. A slot can pass this check and the model can still end
up INFEASIBLE for other reasons (hours ceilings, block-length
constraints, the no-two-rating-1s rule, etc. interacting badly). Treat a
clean result as "no obvious staffing gap", not "guaranteed feasible".
"""

from model_input import DAYS, OPERATING_SLOTS, WEEKDAYS, WEEKEND_DAYS, SolverInput

WEEKDAY_ASSISTANTS_REQUIRED = 2
WEEKDAY_TECHS_REQUIRED = 2
WEEKEND_TECHS_REQUIRED = 1


def diagnose_coverage_gaps(data: SolverInput) -> list[str]:
    """Return a list of human-readable coverage gaps, one per understaffed slot/role.

    Empty list means no slot is guaranteed-infeasible by simple headcount -
    it does NOT mean the model is guaranteed feasible overall.
    """
    problems = []

    for day in DAYS:
        is_weekday = day in WEEKDAYS
        is_weekend = day in WEEKEND_DAYS
        for slot in OPERATING_SLOTS[day]:
            available = [p for p in data.people if p.is_available(day, slot)]
            tech_available = [p for p in available if p.can_work_tech()]

            if is_weekday:
                if len(available) < WEEKDAY_ASSISTANTS_REQUIRED:
                    problems.append(
                        f"{day} slot {slot}: needs {WEEKDAY_ASSISTANTS_REQUIRED} people "
                        f"for assistant coverage, only {len(available)} available at all"
                    )
                if len(tech_available) < WEEKDAY_TECHS_REQUIRED:
                    problems.append(
                        f"{day} slot {slot}: needs {WEEKDAY_TECHS_REQUIRED} tech-capable "
                        f"people, only {len(tech_available)} available"
                    )
            elif is_weekend:
                if len(tech_available) < WEEKEND_TECHS_REQUIRED:
                    problems.append(
                        f"{day} slot {slot}: needs {WEEKEND_TECHS_REQUIRED} tech-capable "
                        f"person, none available"
                    )

    return problems
