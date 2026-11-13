"""FastAPI service (FR-08, FR-10).

    uvicorn src.api.main:app --reload        # http://127.0.0.1:8000/docs
"""
from __future__ import annotations

from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.delivery.mailer import SendBlocked
from src.schemas import CustomerProfile
from src.service import InvalidTransition, Service


class CreateDraftRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    customer_id: str | None = None
    profile: CustomerProfile | None = None

    @model_validator(mode="after")
    def _one_of(self):
        if (self.customer_id is None) == (self.profile is None):
            raise ValueError("send exactly one of customer_id or profile")
        return self


class Rubric(BaseModel):
    model_config = ConfigDict(extra="forbid")
    relevance: int = Field(ge=1, le=5)
    clarity: int = Field(ge=1, le=5)
    personalisation: int = Field(ge=1, le=5)
    call_to_action: int = Field(ge=1, le=5)
    tone: int = Field(ge=1, le=5)


class ReviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["approve", "edit", "reject", "escalate"]
    reviewer: str = "reviewer"
    edited_subject: str | None = None
    edited_body: str | None = None
    rubric: Rubric | None = None
    comment: str | None = None


class SendRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor: str = "reviewer"


def create_app(service: Service | None = None) -> FastAPI:
    app = FastAPI(title="Customer Loyalty and Engagement Assistant",
                  description="Segments customers, drafts grounded emails, checks them, and sends nothing "
                              "without human approval (mock delivery only).")
    state = {"svc": service}

    def svc() -> Service:
        if state["svc"] is None:
            state["svc"] = Service()
        return state["svc"]

    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, exc: RequestValidationError):
        fields = sorted({".".join(str(x) for x in e.get("loc", [])[1:]) for e in exc.errors()})
        kinds = sorted({e.get("type", "") for e in exc.errors()})
        svc().log_rejected_input(f"{request.method} {request.url.path} fields={fields} errors={kinds}")
        return JSONResponse(status_code=422, content={"detail": "invalid or unexpected fields", "fields": fields})

    @app.post("/drafts")
    def create_draft(req: CreateDraftRequest):
        try:
            return svc().create_draft(customer_id=req.customer_id, profile=req.profile, actor="api")
        except KeyError as e:
            raise HTTPException(404, f"unknown customer {e}") from e

    @app.get("/drafts")
    def list_drafts(status: str | None = None, segment: str | None = None):
        return svc().list(status, segment)

    @app.get("/drafts/{draft_id}")
    def get_draft(draft_id: str):
        try:
            return svc().get(draft_id)
        except KeyError as e:
            raise HTTPException(404, "draft not found") from e

    @app.post("/drafts/{draft_id}/review")
    def review(draft_id: str, req: ReviewRequest):
        try:
            return svc().review(draft_id, req.action, req.reviewer, req.edited_subject, req.edited_body,
                                req.rubric.model_dump() if req.rubric else None, req.comment)
        except KeyError as e:
            raise HTTPException(404, "draft not found") from e
        except InvalidTransition as e:
            raise HTTPException(409, str(e)) from e

    @app.post("/drafts/{draft_id}/send")
    def send(draft_id: str, req: SendRequest | None = None):
        try:
            return svc().send(draft_id, actor=(req.actor if req else "reviewer"))
        except KeyError as e:
            raise HTTPException(404, "draft not found") from e
        except SendBlocked as e:
            raise HTTPException(409, str(e)) from e

    @app.get("/audit/{draft_id}")
    def audit(draft_id: str):
        return svc().audit(draft_id)

    return app


app = create_app()
