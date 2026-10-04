"""
Pre-solve validation for manual lock overrides (see model_input.LockedAssignment).

Catches a contradictory or out-of-bounds lock before it reaches the
expensive CP-SAT solve, so the boss gets an immediate, specific error
instead of a generic INFEASIBLE with no explanation. Pure - no side
effects, doesn't touch the CP-SAT model - so it's cheap to call before
deciding whether to solve at all.
"""

from scheduler.solver.model_input import DAYS, OPERATING_SLOTS, ROLES, SolverInput, slot_label


def validate_locks(data: SolverInput) -> list[str]:
    """Return human-readable conflict descriptions; empty list means no conflicts."""
    conflicts: list[str] = []
    people_by_id = {p.id: p for p in data.people}

    for lock in data.locked_assignments:
        person = people_by_id.get(lock.person_id)
        if person is None:
            conflicts.append(f"Lock references person_id={lock.person_id}, who isn't on the roster")
            continue

        if lock.day not in DAYS:
            conflicts.append(f"Lock for {person.name}: {lock.day!r} is not a valid day")
            continue

        where = slot_label(lock.day, lock.slot)
        if lock.slot not in OPERATING_SLOTS[lock.day]:
            conflicts.append(f"Lock for {person.name} on {where}: outside that day's operating hours")
            continue

        if lock.value and not person.is_available(lock.day, lock.slot):
            conflicts.append(
                f"Lock for {person.name} on {where}: forced IN, but they "
                f"never marked themselves available then"
            )

        if lock.value and lock.role == "tech" and not person.can_work_tech():
            conflicts.append(
                f"Lock for {person.name} on {where}: forced into the tech "
                f"role, but role_weighting={person.role_weighting!r} doesn't allow tech work"
            )

        if lock.role not in ROLES:
            conflicts.append(f"Lock for {person.name} on {where}: {lock.role!r} is not a valid role")

    # Cross-lock: two different value=True locks for the same (person, day, slot)
    # but different roles - a person can only hold one role per slot (mirrors the
    # <=1-role-per-slot constraint in build_model.py).
    forced_in_roles: dict[tuple[int, str, int], set[str]] = {}
    for lock in data.locked_assignments:
        if lock.value and lock.person_id in people_by_id and lock.day in DAYS and lock.role in ROLES:
            forced_in_roles.setdefault((lock.person_id, lock.day, lock.slot), set()).add(lock.role)

    for (person_id, day, slot), roles in forced_in_roles.items():
        if len(roles) > 1:
            conflicts.append(
                f"Lock for {people_by_id[person_id].name} on {slot_label(day, slot)}: forced into "
                f"multiple roles at once ({', '.join(sorted(roles))})"
            )

    return conflicts
