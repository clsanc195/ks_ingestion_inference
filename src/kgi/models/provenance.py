"""Provenance records (L11): every canonical element answers
'where did this come from and who approved it?'"""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class ExtractionRun(BaseModel):
    run_id: str
    doc_id: str
    model: str
    prompt_version: str
    schema_version: str  # induced-schema snapshot the run was guided by (L3)
    started_at: datetime
    token_cost: int = 0  # cost observability (NFR)


class ReviewAction(str, Enum):
    accept = "accept"
    edit = "edit"
    reject = "reject"
    force_merge = "force_merge"
    force_split = "force_split"
    defer = "defer"


class ReviewDecision(BaseModel):
    """One reviewer decision on one op. Feeds the active-learning loop (L15).

    reviewer_id is recorded from day one so multi-reviewer (post-v1) needs no
    migration (§7 decision).
    """

    decision_id: str
    patch_id: str
    op_id: str
    reviewer_id: str
    action: ReviewAction
    edited_op: dict | None = None  # replacement op payload when action == edit
    note: str = ""
    decided_at: datetime
    metadata: dict = Field(default_factory=dict)
