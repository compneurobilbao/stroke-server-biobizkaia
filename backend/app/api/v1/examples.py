"""Synthetic example inputs offered by a deployment that enables them (card MVP-03)."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.core.config import Settings, get_settings
from app.models.user import User
from app.services.auth import get_current_user
from app.services.example_inputs import load_examples

router = APIRouter(tags=["example-inputs"])


class ExampleInputRead(BaseModel):
    id: str
    role: str
    label: str
    description: str
    licence: str
    synthetic: bool = True


@router.get("/example-inputs", response_model=list[ExampleInputRead])
def list_example_inputs(
    current_user: User = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
) -> list[ExampleInputRead]:
    """Synthetic example files the deployment offers; an empty list when the feature is
    off (the default). Attach one with ``POST /cases/{id}/inputs/{role}/example``."""
    return [
        ExampleInputRead(
            id=e.id,
            role=e.role,
            label=e.label,
            description=e.description,
            licence=e.licence,
        )
        for e in load_examples(settings)
    ]
