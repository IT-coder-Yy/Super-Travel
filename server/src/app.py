"""Compose the local API and its conversation routes."""
import os
from pathlib import Path
from typing import Any
from uuid import UUID

from fastapi import FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from supertravel_server.access import ALLOWED_ORIGINS, LocalAccessMiddleware
from supertravel_server.agent_service import AgentService
from supertravel_server.conversations import (
    Conversation, ConversationInput, ConversationStore, ConversationSummary,
    ConversationUpdate, RecoveryInput, RecoveryOption, StoreError,
)
from supertravel_server.exports import export_router
from supertravel_server.model import ModelError
from supertravel_server.preferences import preference_router
from supertravel_server.trip_api import trip_router

VERSION = os.environ.get("STA_APP_VERSION", "0.1.0")


def create_app(data_dir: Path, token: str) -> FastAPI:
    if not token:
        raise ValueError("STA_BACKEND_TOKEN must be supplied by the desktop process")
    store = ConversationStore(data_dir)
    agents = AgentService(store, StoreError)
    app = FastAPI(title="SuperTravel Desktop", version=VERSION, docs_url=None, redoc_url=None, openapi_url=None, lifespan=agents.lifespan)
    app.state.store = store
    app.state.agents = agents
    app.include_router(agents.router())
    app.include_router(trip_router(agents, store, StoreError))
    app.include_router(export_router(store, StoreError))
    app.include_router(preference_router(agents.preferences))
    app.add_middleware(LocalAccessMiddleware, token=token)
    app.add_middleware(CORSMiddleware, allow_origins=ALLOWED_ORIGINS, allow_methods=["GET", "POST", "PUT"], allow_headers=["Authorization", "Content-Type"])

    @app.exception_handler(StoreError)
    async def storage_error(_request: Any, exc: StoreError) -> JSONResponse:
        return JSONResponse(status_code=exc.status, content={"detail": exc.detail})

    @app.exception_handler(ModelError)
    async def model_error(_request: Any, exc: ModelError) -> JSONResponse:
        return JSONResponse(status_code=502, content={"detail": {"code": exc.code, "message": str(exc)}})

    @app.exception_handler(RequestValidationError)
    async def invalid_request(_request: Any, exc: RequestValidationError) -> JSONResponse:
        fields = [".".join(map(str, error["loc"])) for error in exc.errors()]
        return JSONResponse(status_code=422, content={"detail": {"code": "invalid_request", "message": "请求字段格式不正确。", "fields": fields}})

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "version": VERSION}

    @app.get("/api/conversations", response_model=list[ConversationSummary])
    def list_conversations() -> list[ConversationSummary]:
        return store.summaries()

    @app.post("/api/conversations", response_model=Conversation, status_code=201)
    def create_conversation(payload: ConversationInput) -> Conversation:
        return store.create(payload)

    @app.get("/api/conversations/recovery", response_model=list[RecoveryOption])
    def recovery_options() -> list[RecoveryOption]:
        return store.recovery_options()

    @app.post("/api/conversations/{conversation_id}/recover", response_model=Conversation)
    async def recover_conversation(conversation_id: UUID, payload: RecoveryInput) -> Conversation:
        await agents.discard(conversation_id)
        return store.recover(conversation_id, payload.token)

    @app.get("/api/conversations/{conversation_id}", response_model=Conversation)
    def get_conversation(conversation_id: UUID) -> Conversation:
        return agents.read(conversation_id)

    @app.put("/api/conversations/{conversation_id}", response_model=Conversation)
    def update_conversation(conversation_id: UUID, payload: ConversationUpdate) -> Conversation:
        return store.update(conversation_id, payload)

    return app
