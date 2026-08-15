"""Role targets, and the optional combined-DPS mode.

A raid's `caps` normally names all four roles: `{"tank": 2, "healer": 4,
"melee": 7, "ranged": 7}`. Supplying a single `dps` target instead collapses
melee and ranged into one number - `{"tank": 2, "healer": 4, "dps": 14}` - and
the board reads `2 / 4 / 14`.

Only the *target* collapses. The roster still separates melee from ranged
everywhere people are listed, because which one someone is remains the thing a
raid lead is reading for. What goes away is the pretence that the split is
fixed in advance, which is not how a raid night actually goes.

The mode is inferred from the presence of the `dps` key rather than stored in a
column of its own, so raids created before this existed keep their four
separate targets and behave exactly as they did.
"""

from __future__ import annotations

from dataclasses import dataclass

from .specs import ROLE_ORDER, Role

#: Cap key that marks a raid as using one combined DPS target.
DPS_KEY = "dps"

#: The roles that combined mode merges.
DPS_ROLES: tuple[Role, ...] = (Role.MELEE, Role.RANGED)

DPS_LABEL = "DPS"


@dataclass(frozen=True, slots=True)
class Target:
    """One number a raid lead is trying to hit, and the roles that feed it."""

    key: str
    label: str
    roles: tuple[Role, ...]
    cap: int

    def accepted(self, counts: dict[Role, int]) -> int:
        return sum(counts.get(role, 0) for role in self.roles)


def is_combined(caps: dict[str, int]) -> bool:
    return DPS_KEY in caps


def targets(caps: dict[str, int]) -> list[Target]:
    """The target groups for these caps, in display order."""
    if not is_combined(caps):
        return [
            Target(role.value, role.label, (role,), caps.get(role.value, 0))
            for role in ROLE_ORDER
        ]
    return [
        Target(Role.TANK.value, Role.TANK.label, (Role.TANK,), caps.get(Role.TANK.value, 0)),
        Target(
            Role.HEALER.value, Role.HEALER.label, (Role.HEALER,), caps.get(Role.HEALER.value, 0)
        ),
        Target(DPS_KEY, DPS_LABEL, DPS_ROLES, caps.get(DPS_KEY, 0)),
    ]


def role_cap(caps: dict[str, int], role: Role) -> int | None:
    """This role's own target, or None when it doesn't have one.

    None is not zero: it means "no target for this role specifically", so
    callers must show a bare count rather than `0/0` or an under-target warning.
    """
    if is_combined(caps) and role in DPS_ROLES:
        return None
    return caps.get(role.value, 0)


def raid_size(caps: dict[str, int]) -> int:
    """Total accepted players the raid is aiming for."""
    return sum(target.cap for target in targets(caps))


def role_counts(signup_roles: list[Role | None]) -> dict[Role, int]:
    counts = dict.fromkeys(ROLE_ORDER, 0)
    for role in signup_roles:
        if role is not None:
            counts[role] += 1
    return counts


def summary(caps: dict[str, int]) -> str:
    """Just the numbers, e.g. `2 / 4 / 14`."""
    return " / ".join(str(target.cap) for target in targets(caps))
