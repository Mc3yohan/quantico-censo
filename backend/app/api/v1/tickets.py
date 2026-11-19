"""Field-to-HQ tactical support tickets with strict RBAC/IDOR guards and server-derived sender identity."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.rbac import AuthContext, Role, apply_scope_filter, assert_record_in_scope
from app.core.redis_bus import redis_bus
from app.core.security import get_current_user, require_roles
from app.db.models import FieldAudit, SupportTicket, Territory, TicketMessage, User
from app.db.session import get_db
from app.schemas.domain import (
    SupportTicketResponse,
    TicketCloseRequest,
    TicketCreateRequest,
    TicketMessageCreateRequest,
    TicketMessageResponse,
)

router = APIRouter()


@router.get("", response_model=list[SupportTicketResponse])
def list_tickets(
    status_filter: str | None = Query(default=None, alias="status", max_length=24),
    limit: int = Query(default=50, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    auth: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[SupportTicketResponse]:
    """List support tickets scoped to region/territory (agents see only tickets they opened)."""
    stmt = apply_scope_filter(
        select(SupportTicket),
        SupportTicket,
        auth,
        owner_column=SupportTicket.created_by_id,
        restrict_agent_to_owner=True,
    )
    if status_filter:
        stmt = stmt.where(SupportTicket.status == status_filter)
    rows = db.execute(stmt.order_by(SupportTicket.updated_at.desc()).offset(offset).limit(limit)).scalars().all()
    return [SupportTicketResponse.model_validate(r) for r in rows]


@router.post("", response_model=SupportTicketResponse, status_code=status.HTTP_201_CREATED)
def create_ticket(
    payload: TicketCreateRequest,
    auth: AuthContext = Depends(
        require_roles(
            Role.SUPER_ADMIN,
            Role.REGIONAL_MANAGER,
            Role.SUPERVISOR,
            Role.FIELD_AGENT,
            is_write=True,
        )
    ),
    db: Session = Depends(get_db),
) -> SupportTicketResponse:
    """Create a field support ticket strictly inside the caller's authorized region and territory."""
    audit: FieldAudit | None = None
    if payload.audit_id is not None:
        audit = db.get(FieldAudit, payload.audit_id)
        if audit is None:
            raise HTTPException(status_code=404, detail="Referenced audit not found.")
        assert_record_in_scope(
            auth,
            region_id=audit.region_id,
            territory_id=audit.territory_id,
            owner_user_id=audit.agent_id,
            require_owner_for_agent=True,
        )

    if auth.region_id is not None and auth.territory_id is not None:
        region_id, territory_id = auth.region_id, auth.territory_id
    elif audit is not None:
        region_id, territory_id = audit.region_id, audit.territory_id
    elif auth.role in (Role.SUPER_ADMIN, Role.REGIONAL_MANAGER):
        ter: Territory | None = None
        if payload.territory_id is not None:
            ter = db.get(Territory, payload.territory_id)
            if ter is None or not ter.is_active:
                raise HTTPException(status_code=404, detail="Active territory not found.")
            assert_record_in_scope(auth, region_id=ter.region_id, territory_id=ter.id)
        else:
            stmt = select(Territory).where(Territory.is_active.is_(True)).order_by(Territory.id.asc()).limit(1)
            if auth.role == Role.REGIONAL_MANAGER:
                if auth.region_id is None:
                    raise HTTPException(status_code=403, detail="Regional manager missing region assignment.")
                stmt = stmt.where(Territory.region_id == auth.region_id)
            ter = db.execute(stmt).scalar_one_or_none()
        if ter is None:
            raise HTTPException(status_code=400, detail="No active territory available to open a ticket.")
        region_id, territory_id = ter.region_id, ter.id
    else:
        raise HTTPException(status_code=400, detail="Operator must belong to a territory to open a ticket.")

    ticket = SupportTicket(
        region_id=region_id,
        territory_id=territory_id,
        created_by_id=auth.user_id,
        audit_id=payload.audit_id,
        subject=payload.subject,
        status="open",
    )
    db.add(ticket)
    db.flush()
    db.add(TicketMessage(ticket_id=ticket.id, sender_id=auth.user_id, body=payload.body))
    db.commit()
    db.refresh(ticket)
    return SupportTicketResponse.model_validate(ticket)


@router.get("/{ticket_id}/messages", response_model=list[TicketMessageResponse])
def list_ticket_messages(
    ticket_id: int,
    auth: AuthContext = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[TicketMessageResponse]:
    """Read messages for a ticket after strict territorial & ownership verification."""
    ticket = db.get(SupportTicket, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found.")
    assert_record_in_scope(
        auth,
        region_id=ticket.region_id,
        territory_id=ticket.territory_id,
        owner_user_id=ticket.created_by_id,
        require_owner_for_agent=True,
    )

    msgs = db.execute(
        select(TicketMessage)
        .where(TicketMessage.ticket_id == ticket.id)
        .order_by(TicketMessage.created_at.asc())
        .limit(200)
    ).scalars().all()
    sender_ids = {m.sender_id for m in msgs}
    names: dict[int, str] = {}
    if sender_ids:
        for uid, fname in db.execute(select(User.id, User.full_name).where(User.id.in_(sender_ids))).all():
            names[int(uid)] = str(fname)

    out: list[TicketMessageResponse] = []
    for m in msgs:
        item = TicketMessageResponse.model_validate(m)
        item.sender_name = names.get(m.sender_id, "")
        out.append(item)
    return out


@router.post("/{ticket_id}/messages", response_model=TicketMessageResponse, status_code=status.HTTP_201_CREATED)
def post_ticket_message(
    ticket_id: int,
    payload: TicketMessageCreateRequest,
    auth: AuthContext = Depends(
        require_roles(
            Role.SUPER_ADMIN,
            Role.REGIONAL_MANAGER,
            Role.SUPERVISOR,
            Role.FIELD_AGENT,
            is_write=True,
        )
    ),
    db: Session = Depends(get_db),
) -> TicketMessageResponse:
    """Append a message using server-derived sender_id and publish via Redis Pub/Sub."""
    ticket = db.execute(
        select(SupportTicket).where(SupportTicket.id == ticket_id).with_for_update()
    ).scalar_one_or_none()
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found.")
    assert_record_in_scope(
        auth,
        region_id=ticket.region_id,
        territory_id=ticket.territory_id,
        owner_user_id=ticket.created_by_id,
        require_owner_for_agent=True,
    )
    if ticket.status == "closed":
        raise HTTPException(status_code=409, detail="Cannot post messages to a closed ticket.")

    msg = TicketMessage(ticket_id=ticket.id, sender_id=auth.user_id, body=payload.body)
    ticket.updated_at = datetime.now(timezone.utc)
    db.add(msg)
    db.commit()
    db.refresh(msg)

    redis_bus.publish_event(
        f"qc:ticket:{ticket.id}",
        {
            "event": "TICKET_MESSAGE",
            "ticket_id": ticket.id,
            "message_id": msg.id,
            "sender_id": auth.user_id,
            "sender_name": auth.full_name,
            "body": msg.body,
            "created_at": msg.created_at.isoformat(),
        },
    )

    out = TicketMessageResponse.model_validate(msg)
    out.sender_name = auth.full_name
    return out


@router.post("/{ticket_id}/claim", response_model=SupportTicketResponse)
def claim_ticket(
    ticket_id: int,
    auth: AuthContext = Depends(
        require_roles(Role.SUPER_ADMIN, Role.REGIONAL_MANAGER, Role.SUPERVISOR, is_write=True)
    ),
    db: Session = Depends(get_db),
) -> SupportTicketResponse:
    """Assign an open support ticket to an authorized supervisor or manager."""
    ticket = db.execute(
        select(SupportTicket).where(SupportTicket.id == ticket_id).with_for_update()
    ).scalar_one_or_none()
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found.")
    assert_record_in_scope(auth, region_id=ticket.region_id, territory_id=ticket.territory_id)
    if ticket.status == "closed":
        raise HTTPException(status_code=409, detail="Closed ticket must be reopened before claiming.")

    ticket.assigned_to_id = auth.user_id
    ticket.status = "claimed"
    db.commit()
    db.refresh(ticket)
    return SupportTicketResponse.model_validate(ticket)


@router.post("/{ticket_id}/close", response_model=SupportTicketResponse)
def close_ticket(
    ticket_id: int,
    payload: TicketCloseRequest,
    auth: AuthContext = Depends(
        require_roles(Role.SUPER_ADMIN, Role.REGIONAL_MANAGER, Role.SUPERVISOR, is_write=True)
    ),
    db: Session = Depends(get_db),
) -> SupportTicketResponse:
    """Close a support ticket within scope and preserve closure attribution."""
    ticket = db.execute(
        select(SupportTicket).where(SupportTicket.id == ticket_id).with_for_update()
    ).scalar_one_or_none()
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found.")
    assert_record_in_scope(auth, region_id=ticket.region_id, territory_id=ticket.territory_id)
    if ticket.status == "closed":
        raise HTTPException(status_code=409, detail="Ticket is already closed.")

    ticket.status = "closed"
    ticket.closed_by_id = auth.user_id
    ticket.resolution_note = payload.resolution_note
    ticket.closed_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(ticket)
    return SupportTicketResponse.model_validate(ticket)


@router.post("/{ticket_id}/reopen", response_model=SupportTicketResponse)
def reopen_ticket(
    ticket_id: int,
    auth: AuthContext = Depends(
        require_roles(Role.SUPER_ADMIN, Role.REGIONAL_MANAGER, Role.SUPERVISOR, is_write=True)
    ),
    db: Session = Depends(get_db),
) -> SupportTicketResponse:
    """Reopen a closed ticket while preserving previous audit trail metadata."""
    ticket = db.execute(
        select(SupportTicket).where(SupportTicket.id == ticket_id).with_for_update()
    ).scalar_one_or_none()
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found.")
    assert_record_in_scope(auth, region_id=ticket.region_id, territory_id=ticket.territory_id)
    if ticket.status != "closed":
        raise HTTPException(status_code=409, detail="Ticket is not closed.")

    ticket.status = "open"
    ticket.reopen_count += 1
    db.commit()
    db.refresh(ticket)
    return SupportTicketResponse.model_validate(ticket)
