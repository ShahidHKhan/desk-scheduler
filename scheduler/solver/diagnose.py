"""
Pre-solve and post-solve coverage diagnosis.

The CP-SAT model reports a bare INFEASIBLE with no indication of why.
diagnose_coverage_gaps() catches the
clearest and most common pre-solve cause up front: a weekend slot where
zero tech-capable, under-hour-cap people are available at all - weekend
coverage is still a hard constraint (exactly 1 tech-role person, no
exceptions), so that really does guarantee INFEASIBLE before ever
building the model.

Weekday coverage is now a soft/penalized constraint instead (see
build_model.py) - a weekday headcount gap no longer guarantees
infeasibility, since the solver can still return a schedule that's
understaffed at that slot. summarize_coverage_shortfalls() reports those
after a feasible solve, from the shortfall variables build_model()
returns, so the boss still sees exactly which weekday slots came up
short even though the run "succeeded".

Neither function is a full feasibility prover - a slot can pass the
pre-solve check and the model can still end up INFEASIBLE for other
reasons (block-length constraints, the no-two-rating-1s rule, etc.
interacting badly). Treat a clean pre-solve result as "no obvious hard
staffing gap", not "guaranteed feasible".
"""

from dataclasses import dataclass

from ortools.sat.python import cp_model

from scheduler.solver.model_input import (
    DAYS,
    OPERATING_SLOTS,
    WEEKEND_DAYS,
    Person,
    SolverInput,
    slot_label,
    time_label,
)

WEEKEND_TECHS_REQUIRED = 1


@dataclass
class CandidateRuling:
    """Why one specific person was ruled out as a candidate for one
    specific unsatisfiable slot. `reason` is a stable machine-checkable
    code (evals/judge.py cross-references this against the judged
    explanation); `detail` is the human-readable phrase.
    """

    person_name: str
    reason: str  # "not_available" | "not_tech_capable" | "at_hours_cap"
    detail: str


@dataclass
class SlotDiagnosis:
    """One weekend slot the pre-solve check found unsatisfiable, and the
    specific reason every roster member was ruled out as a candidate for
    it."""

    day: str
    slot: int
    rulings: list[CandidateRuling]

    @property
    def time_label(self) -> str:
        return time_label(self.slot)

    @property
    def slot_label(self) -> str:
        return slot_label(self.day, self.slot)

    def to_message(self) -> str:
        checked = ", ".join(f"[{r.person_name} — {r.detail}]" for r in self.rulings)
        return f"{self.slot_label} has no valid assignment. Checked: {checked}"


def _rule_out(person: Person, day: str, slot: int) -> CandidateRuling:
    """Which single reason rules `person` out as a tech candidate for this
    slot. Checked in this order - availability, then capability, then
    hours-cap - since a slot only ever reaches this function once
    diagnose_coverage_gaps_detailed() has already confirmed nobody
    qualifies, so exactly one of these three is guaranteed to apply.
    """
    if not person.is_available(day, slot):
        return CandidateRuling(person.name, "not_available", "not available this slot")
    if not person.can_work_tech():
        return CandidateRuling(person.name, "not_tech_capable", "assistant-only, can't cover tech slot")
    return CandidateRuling(
        person.name, "at_hours_cap", f"already at their {person.hours_requested}-hour cap"
    )


def diagnose_coverage_gaps_detailed(data: SolverInput) -> list[SlotDiagnosis]:
    """Structured version of diagnose_coverage_gaps(): one SlotDiagnosis per
    unsatisfiable weekend slot, citing the specific reason (availability
    vs. capability vs. hours-cap) each roster member was ruled out as a
    tech candidate for that slot.

    A candidate qualifies only if available, tech-capable, AND has hours
    left to give (hours_requested > 0) - a static, solve-independent
    stand-in for "already at their cap" (a full "how many hours has this
    person already been assigned elsewhere this week" check isn't
    knowable before the model has been solved; requesting 0 hours is the
    one hours-cap condition that's true regardless of the rest of the
    schedule).
    """
    diagnoses = []
    for day in [d for d in DAYS if d in WEEKEND_DAYS]:
        for slot in OPERATING_SLOTS[day]:
            qualifying = [
                p
                for p in data.people
                if p.is_available(day, slot) and p.can_work_tech() and p.hours_requested > 0
            ]
            if len(qualifying) >= WEEKEND_TECHS_REQUIRED:
                continue
            rulings = [_rule_out(p, day, slot) for p in data.people]
            diagnoses.append(SlotDiagnosis(day=day, slot=slot, rulings=rulings))
    return diagnoses


def diagnose_coverage_gaps(data: SolverInput) -> list[str]:
    """Return human-readable gaps that guarantee INFEASIBLE before ever
    building the CP-SAT model. Weekend-only - see module docstring for why
    weekday gaps are no longer diagnosed here.
    """
    return [d.to_message() for d in diagnose_coverage_gaps_detailed(data)]


def summarize_coverage_shortfalls(solver: cp_model.CpSolver, variables: dict) -> list[str]:
    """Human-readable messages for weekday slots the solver accepted as
    understaffed (the soft coverage constraint in build_model.py). Only
    meaningful after a feasible solve - call with the CpSolver used to
    solve the model and the `variables` dict build_model() returned.
    Empty list means every weekday slot hit its target (2+2, or 1+1 on
    Mon-Thu evenings).
    """
    messages = []
    for role, day, slot, shortfall_var in variables.get("coverage_shortfalls", []):
        shortfall = solver.Value(shortfall_var)
        if shortfall > 0:
            messages.append(f"{slot_label(day, slot)}: understaffed by {shortfall} {role}(s)")
    return messages
