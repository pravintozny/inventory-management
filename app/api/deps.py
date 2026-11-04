from typing import Annotated

from fastapi import Depends, Header
from sqlalchemy.orm import Session

from app.database import get_db

DB = Annotated[Session, Depends(get_db)]


def actor(x_user: Annotated[str | None, Header()] = None) -> str:
    """Caller identity for audit fields. Authentication is out of scope: a gateway should set X-User."""
    return (x_user or "system").strip()[:100] or "system"


Actor = Annotated[str, Depends(actor)]


def exact(obj):
    """Recursively render Decimals as 4-dp strings so JSON payloads never carry floats for money."""
    from decimal import Decimal
    if isinstance(obj, Decimal):
        return format(obj.quantize(Decimal("0.0001")), "f")
    if isinstance(obj, dict):
        return {k: exact(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [exact(v) for v in obj]
    return obj
