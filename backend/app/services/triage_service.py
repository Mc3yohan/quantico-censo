"""Deterministic Quarantine Gate and Triage Anomaly Scoring for retail field audits."""

from __future__ import annotations

from dataclasses import dataclass
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import FieldAudit, Outlet, TriageItem


@dataclass(frozen=True, slots=True)
class TriageDecision:
    requires_quarantine: bool
    review_status: str
    priority: str | None = None
    alert_type: str | None = None
    suggested_outlet_id: int | None = None
    similarity_score: float | None = None


def _find_closest_outlet_match(
    db: Session,
    *,
    region_id: int,
    territory_id: int,
    business_name: str,
) -> tuple[int | None, float | None]:
    """Bounded fuzzy match against existing territory outlets to spot duplicate prospects."""
    normalized = business_name.strip().lower()
    if len(normalized) < 3:
        return None, None

    candidates = db.execute(
        select(Outlet.id, Outlet.business_name)
        .where(Outlet.region_id == region_id, Outlet.territory_id == territory_id)
        .limit(500)
    ).all()

    best_id: int | None = None
    best_score = 0.0
    for outlet_id, candidate_name in candidates:
        ratio = SequenceMatcher(None, normalized, candidate_name.strip().lower()).ratio()
        if ratio > best_score:
            best_score = ratio
            best_id = int(outlet_id)

    if best_id is not None and best_score >= 0.72:
        return best_id, round(best_score, 3)
    return None, None


def evaluate_audit_for_triage(db: Session, audit: FieldAudit) -> TriageDecision:
    """Apply deterministic QA rules to decide between auto-approval and quarantine triage."""
    if audit.geofence_status == "outside_territory":
        return TriageDecision(
            requires_quarantine=True,
            review_status="quarantined",
            priority="critical",
            alert_type="out_of_territory",
            suggested_outlet_id=audit.outlet_id,
            similarity_score=1.0 if audit.outlet_id else None,
        )

    if audit.geofence_status == "gps_drift":
        return TriageDecision(
            requires_quarantine=True,
            review_status="quarantined",
            priority="high",
            alert_type="gps_drift",
            suggested_outlet_id=audit.outlet_id,
            similarity_score=1.0 if audit.outlet_id else None,
        )

    if audit.visit_status == "unmapped_prospect" or audit.outlet_id is None:
        suggested_id, score = _find_closest_outlet_match(
            db,
            region_id=audit.region_id,
            territory_id=audit.territory_id,
            business_name=audit.business_name,
        )
        alert_type = "duplicate_candidate" if suggested_id is not None else "unmapped_prospect"
        return TriageDecision(
            requires_quarantine=True,
            review_status="quarantined",
            priority="high",
            alert_type=alert_type,
            suggested_outlet_id=suggested_id,
            similarity_score=score,
        )

    if audit.stockout_alert or audit.visit_status == "relocated":
        alert_type = "stockout_critical" if audit.stockout_alert else "routine_review"
        return TriageDecision(
            requires_quarantine=True,
            review_status="quarantined",
            priority="high" if audit.stockout_alert else "medium",
            alert_type=alert_type,
            suggested_outlet_id=audit.outlet_id,
            similarity_score=1.0,
        )

    return TriageDecision(
        requires_quarantine=False,
        review_status="approved",
    )


def create_triage_if_needed(db: Session, audit: FieldAudit) -> TriageItem | None:
    """Evaluate an audit, update its review_status, and insert a TriageItem if quarantined."""
    decision = evaluate_audit_for_triage(db, audit)
    audit.review_status = decision.review_status
    if not decision.requires_quarantine or decision.priority is None or decision.alert_type is None:
        return None

    item = TriageItem(
        audit_id=audit.id,
        region_id=audit.region_id,
        territory_id=audit.territory_id,
        priority=decision.priority,
        alert_type=decision.alert_type,
        suggested_outlet_id=decision.suggested_outlet_id,
        similarity_score=decision.similarity_score,
        status="open",
    )
    db.add(item)
    db.flush()
    return item
