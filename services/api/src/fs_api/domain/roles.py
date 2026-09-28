"""RBAC: deny by default. Every route names ONE permission; a role has only the permissions listed here."""
from __future__ import annotations

from enum import StrEnum


class Role(StrEnum):
    PLATFORM_ADMIN = "platform_admin"
    FLEET_ADMIN = "fleet_admin"
    ANALYST = "analyst"
    VIEWER = "viewer"
    LENDER_AGENT = "lender_agent"


class Perm(StrEnum):
    ME = "me"
    FLEET_READ = "fleet.read"            # summary, vehicles, alerts, map
    ALERT_WRITE = "alert.write"          # ack / resolve
    DRIVER_READ = "driver.read"
    DRIVER_PII = "driver.pii"            # decrypt name (else pseudonymous id only)
    REPORT_READ = "report.read"
    INSIGHT_EXPORT = "insight.export"
    AGENT_CHAT = "agent.chat"
    AGENT_READ = "agent.read"
    AGENT_WRITE = "agent.write"          # create proposals / generate briefing
    AGENT_APPROVE = "agent.approve"      # approve / reject a proposal (fleet_admin only)
    PLATFORM_READ = "platform.read"      # mappings list, quality, dlq
    MAPPING_WRITE = "mapping.write"      # create draft / preview
    MAPPING_APPROVE = "mapping.approve"  # approve / activate / dlq replay (platform_admin only)
    AUDIT_READ = "audit.read"
    DSR = "dsr"
    STREAM = "stream"


_R = Role
PERMISSIONS: dict[Role, frozenset[Perm]] = {
    _R.PLATFORM_ADMIN: frozenset(Perm),  # everything, incl. mapping approval/activation
    _R.FLEET_ADMIN: frozenset({
        Perm.ME, Perm.FLEET_READ, Perm.ALERT_WRITE, Perm.DRIVER_READ, Perm.DRIVER_PII, Perm.REPORT_READ,
        Perm.INSIGHT_EXPORT, Perm.AGENT_CHAT, Perm.AGENT_READ, Perm.AGENT_WRITE, Perm.AGENT_APPROVE,
        Perm.PLATFORM_READ, Perm.MAPPING_WRITE, Perm.AUDIT_READ, Perm.DSR, Perm.STREAM,
    }),
    _R.ANALYST: frozenset({
        Perm.ME, Perm.FLEET_READ, Perm.ALERT_WRITE, Perm.DRIVER_READ, Perm.REPORT_READ, Perm.INSIGHT_EXPORT,
        Perm.AGENT_CHAT, Perm.AGENT_READ, Perm.AGENT_WRITE, Perm.STREAM,
    }),
    _R.VIEWER: frozenset({Perm.ME, Perm.FLEET_READ, Perm.DRIVER_READ, Perm.AGENT_READ, Perm.STREAM}),
    _R.LENDER_AGENT: frozenset({Perm.ME, Perm.FLEET_READ, Perm.STREAM}),
}

ROLE_ORDER = (_R.PLATFORM_ADMIN, _R.FLEET_ADMIN, _R.ANALYST, _R.VIEWER, _R.LENDER_AGENT)


def parse_roles(raw: object) -> frozenset[Role]:
    """Keep only known roles from a token claim (unknown strings are ignored, never trusted)."""
    out: set[Role] = set()
    if isinstance(raw, str):
        raw = [raw]
    if isinstance(raw, (list, tuple, set, frozenset)):
        for r in raw:
            try:
                out.add(Role(str(r)))
            except ValueError:
                continue
    return frozenset(out)


def permissions_of(roles: frozenset[Role]) -> frozenset[Perm]:
    perms: set[Perm] = set()
    for r in roles:
        perms |= PERMISSIONS.get(r, frozenset())
    return frozenset(perms)


def can(roles: frozenset[Role], perm: Perm) -> bool:
    return perm in permissions_of(roles)


# ---------------------------------------------------------------- purpose + location precision
class Purpose(StrEnum):
    OPERATIONS = "OPERATIONS"
    RECOVERY = "RECOVERY"
    SAFETY_REVIEW = "SAFETY_REVIEW"
    COMPLIANCE = "COMPLIANCE"


# Precision ladder: exact > geohash7 (~150 m) > geohash6 (~1.2 km) > geohash5 (~4.9 km)
PRECISION_RANK = {"exact": 3, "geohash7": 2, "geohash6": 1, "geohash5": 0}
ROLE_PRECISION = {
    _R.PLATFORM_ADMIN: "exact",
    _R.FLEET_ADMIN: "exact",
    _R.ANALYST: "geohash7",
    _R.VIEWER: "geohash6",
    _R.LENDER_AGENT: "geohash5",
}


def purpose_allowed(roles: frozenset[Role], purpose: Purpose) -> bool:
    """RECOVERY is reserved for lender_agent; everything else is open to any authenticated role."""
    if purpose is Purpose.RECOVERY:
        return Role.LENDER_AGENT in roles
    return True


def location_precision(roles: frozenset[Role], purpose: Purpose, vehicle_flagged: bool = False) -> str:
    """Role/purpose dependent precision: the best precision any held role grants, then purpose adjustments.

    * RECOVERY (lender_agent only) unlocks exact location, but ONLY for a vehicle flagged in default.
    * COMPLIANCE never needs a trail: capped at geohash5.
    * SAFETY_REVIEW lifts a viewer (geohash6) to geohash7 for a specific review, never higher.
    """
    best = "geohash5"
    for r in roles:
        p = ROLE_PRECISION.get(r, "geohash5")
        if PRECISION_RANK[p] > PRECISION_RANK[best]:
            best = p
    if purpose is Purpose.RECOVERY:
        return "exact" if (vehicle_flagged and Role.LENDER_AGENT in roles) else "geohash5"
    if purpose is Purpose.COMPLIANCE:
        return "geohash5"
    if purpose is Purpose.SAFETY_REVIEW and best == "geohash6":
        return "geohash7"
    return best
