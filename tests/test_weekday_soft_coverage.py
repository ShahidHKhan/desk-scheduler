"""
Step 3 (Tier 2) - weekday coverage (2 tech + 2 assistant) as currently
implemented: a soft, heavily-penalized target rather than a hard
constraint (Phase 3 decision - see build_model.py). Covers the two
things that decision needs to hold up under test:

1. When full 2+2 coverage is achievable, the solver actually achieves it
   (the penalty weight is high enough to prefer full coverage whenever
   possible, not just "allowed to").
2. When it's not achievable, a shortfall doesn't cause infeasibility -
   the solver still returns a feasible schedule, the shortfall is
   minimized to only the slot(s)/role(s) that genuinely can't be
   covered, and the shortfall is visible in solver output (not silent).
"""

import re

from helpers import full_week_availability, generous_roster, make_person
from model_input import DAYS, OPERATING_SLOTS, SolverInput, WEEKDAYS
from solve import solve


def test_full_weekday_coverage_is_achieved_when_possible():
    # 4+4 (generous_roster()'s own default scale, used by the weekend-coverage
    # test) has enough slack for weekend hard coverage but NOT enough total
    # tech/assistant hour-capacity to hit 2+2 at every one of the week's ~114
    # weekday slot-times (114 slots * 2 roles = 228 slot-units of demand per
    # role vs. 4 people * 20hrs * 2 slots/hr = 160 slot-units of capacity -
    # arithmetically impossible regardless of solver quality, same
    # observation as solve.py's own __main__ self-test). 10+10 matches
    # solve.py's realistic-scale example and has enough capacity to actually
    # exercise "does the solver prefer full coverage when it CAN reach it".
    people = generous_roster(tech_count=10, assistant_count=10, hours_requested=15)
    data = SolverInput(people=people)
    result = solve(data, time_limit_seconds=20)

    assert result["feasible"], f"expected a feasible solve, got {result['status']}"
    assert result["coverage_shortfalls"] == [], (
        f"expected full 2+2 weekday coverage with this much slack in the roster, "
        f"got shortfalls: {result['coverage_shortfalls']}"
    )


def _availability_minus_one_weekday_slot(day: str, slot: int) -> dict[str, list[bool]]:
    """Available every operating slot all week (see helpers.full_week_availability),
    except the one given weekday slot."""
    avail = full_week_availability()
    avail[day] = list(avail[day])
    avail[day][slot] = False
    return avail


def test_weekday_shortfall_is_minimized_not_infeasible_and_visible_in_output():
    # STUCK_DAY/STUCK_SLOT: every tech/assistant except one of each is
    # unavailable at this single weekday slot - only 1 tech + 1 assistant
    # can possibly cover it, so a shortfall of exactly 1 for each role
    # there is the best any solver could do. Every other weekday slot, and
    # both weekend days, are untouched (full availability), so nothing
    # else should come up short.
    STUCK_DAY, STUCK_SLOT = "Mon", 0
    reduced = _availability_minus_one_weekday_slot(STUCK_DAY, STUCK_SLOT)

    # Same 10+10/15hr scale as the achievable-coverage test above (enough
    # capacity to hit 2+2 everywhere in general) - except every tech and
    # every assistant except one of each is unavailable at STUCK_SLOT
    # specifically, so at most 1 tech + 1 assistant could ever cover it.
    people = [make_person(1, "Tech1", "tech_only", hours_requested=15)]
    people += [
        make_person(i, f"Tech{i}", "tech_only", hours_requested=15, availability=reduced)
        for i in range(2, 11)
    ]
    people.append(make_person(11, "Asst1", "assistant_only", hours_requested=15))
    people += [
        make_person(i, f"Asst{i - 10}", "assistant_only", hours_requested=15, availability=reduced)
        for i in range(12, 21)
    ]

    data = SolverInput(people=people)
    result = solve(data, time_limit_seconds=20)

    assert result["feasible"], (
        f"a weekday shortfall must not cause infeasibility, got {result['status']}"
    )

    shortfalls = result["coverage_shortfalls"]
    assert shortfalls, "expected the forced shortfall to be visible in solver output, got none"

    stuck_label = f"{STUCK_DAY} slot {STUCK_SLOT}"
    stuck_messages = [m for m in shortfalls if m.startswith(stuck_label)]
    other_messages = [m for m in shortfalls if not m.startswith(stuck_label)]

    assert other_messages == [], (
        f"only {stuck_label} should be short-staffed, but got shortfalls elsewhere too: {other_messages}"
    )
    # Exactly one assistant-shortfall message and one tech-shortfall message,
    # each short by exactly 1 - proves the solver minimized the shortfall to
    # what the availability constraint actually forces, not something worse.
    assert len(stuck_messages) == 2, f"expected exactly 2 shortfall messages at {stuck_label}, got {stuck_messages}"
    for message in stuck_messages:
        match = re.search(r"understaffed by (\d+)", message)
        assert match and int(match.group(1)) == 1, f"expected a shortfall of exactly 1, got: {message}"

    # Every other weekday slot on every other day hit the full 2+2 target.
    all_weekday_slots = {
        (day, slot) for day in DAYS if day in WEEKDAYS for slot in OPERATING_SLOTS[day]
    } - {(STUCK_DAY, STUCK_SLOT)}
    for day, slot in all_weekday_slots:
        label = f"{day} slot {slot}"
        assert not any(m.startswith(label) for m in shortfalls), f"unexpected shortfall at {label}"
