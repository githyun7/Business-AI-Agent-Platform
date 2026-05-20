# FastAPI routes
import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from database import get_db, init_db
from models import Session as AgentSession, Message
from session_manager import (
    spawn_session_container,
    stop_session_container,
    wait_for_agent_ready,
)

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")
# Map session_id -> set of connected WebSockets for streaming
active_websockets: dict[str, set[WebSocket]] = {}
# Map session_id -> message history (in-memory for active sessions)
session_messages: dict[str, list] = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    # Background task: cleanup idle containers every 5 minutes
    asyncio.create_task(cleanup_idle_sessions())
    yield


app = FastAPI(title="AI Agent Platform", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class CreateSessionResponse(BaseModel):
    session_id: str
    vnc_url: str
    status: str


class SendMessageRequest(BaseModel):
    message: str


# ── Routes ────────────────────────────────────────────────────────────────────

@app.post("/sessions", response_model=CreateSessionResponse)
async def create_session(db: AsyncSession = Depends(get_db)):
    """Spawn a new agent container and create a session record."""
    session_id = str(uuid.uuid4())

    # Persist session immediately so frontend can poll status
    db_session = AgentSession(id=session_id, status="starting")
    db.add(db_session)
    await db.commit()

    # Spawn container in background — returns quickly, container starts async
    asyncio.create_task(_start_container(session_id))

    return CreateSessionResponse(
        session_id=session_id,
        vnc_url="",   # Will be populated once container is ready
        status="starting",
    )


async def _start_container(session_id: str):
    """Background task: spawn container, wait for ready, update DB."""
    async with (await _get_db_session()) as db:
        try:
            info = await spawn_session_container(session_id)

            # Wait for agent HTTP server to be up inside container
            ready = await wait_for_agent_ready(info["agent_port"])
            status = "ready" if ready else "error"

            await db.execute(
                update(AgentSession)
                .where(AgentSession.id == session_id)
                .values(
                    container_id=info["container_id"],
                    vnc_port=info["vnc_port"],
                    agent_port=info["agent_port"],
                    status=status,
                )
            )
            await db.commit()

            # Notify any connected WebSocket clients
            await _broadcast(session_id, {
                "type": "session_ready",
                "vnc_url": f"http://localhost:{info['vnc_port']}/vnc.html"
                           "?autoconnect=1&resize=scale&view_only=1",
                "status": status,
            })

        except Exception as e:
            await db.execute(
                update(AgentSession)
                .where(AgentSession.id == session_id)
                .values(status="error")
            )
            await db.commit()
            await _broadcast(session_id, {"type": "error", "error": str(e)})


@app.get("/sessions/{session_id}")
async def get_session(session_id: str, db: AsyncSession = Depends(get_db)):
    """Get session status and metadata."""
    result = await db.execute(
        select(AgentSession)
        .where(AgentSession.id == session_id)
        .options(selectinload(AgentSession.messages))
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    return {
        "session_id": session.id,
        "status": session.status,
        "vnc_url": (
            f"http://localhost:{session.vnc_port}/vnc.html"
            "?autoconnect=1&resize=scale&view_only=1"
            if session.vnc_port else None
        ),
        "created_at": session.created_at.isoformat(),
        "message_count": len(session.messages),
    }


@app.get("/sessions")
async def list_sessions(db: AsyncSession = Depends(get_db)):
    """List all sessions for the history sidebar."""
    result = await db.execute(
        select(AgentSession).order_by(AgentSession.created_at.desc()).limit(50)
    )
    sessions = result.scalars().all()
    return [
        {
            "session_id": s.id,
            "status": s.status,
            "created_at": s.created_at.isoformat(),
        }
        for s in sessions
    ]


@app.post("/sessions/{session_id}/message")
async def send_message(
    session_id: str,
    req: SendMessageRequest,
    db: AsyncSession = Depends(get_db),
):
    """Send a user message to the agent in a session."""
    result = await db.execute(
        select(AgentSession).where(AgentSession.id == session_id)
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.status != "ready":
        raise HTTPException(status_code=400, detail=f"Session not ready: {session.status}")

    # Build the message in Anthropic format
    user_msg = {"role": "user", "content": req.message}

    # Persist user message
    db_msg = Message(
        session_id=session_id,
        role="user",
        content={"text": req.message},
    )
    db.add(db_msg)

    # Update session history in memory
    if session_id not in session_messages:
        session_messages[session_id] = []
    session_messages[session_id].append(user_msg)

    # Update last_active
    await db.execute(
        update(AgentSession)
        .where(AgentSession.id == session_id)
        .values(last_active=datetime.now(timezone.utc))
    )
    await db.commit()

    # Send task to agent container
    callback_url = f"http://backend:8000/internal/stream/{session_id}"
    async with httpx.AsyncClient() as client:
        await client.post(
            f"http://localhost:{session.agent_port}/task",
            json={
                "messages": session_messages[session_id],
                "api_key": ANTHROPIC_API_KEY,
                "callback_url": callback_url,
            },
            timeout=5.0,
        )

    return {"status": "queued"}


@app.post("/internal/stream/{session_id}")
async def internal_stream(session_id: str, payload: dict):
    """
    Internal endpoint — receives streaming events from agent containers
    and broadcasts them to connected WebSocket clients.
    Not exposed to frontend directly.
    """
    # Persist assistant messages to DB
    if payload.get("type") in ("output", "tool_result"):
        async with (await _get_db_session()) as db:
            db_msg = Message(
                session_id=session_id,
                role="assistant",
                content=payload,
            )
            db.add(db_msg)
            await db.commit()

    await _broadcast(session_id, payload)
    return {"ok": True}


@app.delete("/sessions/{session_id}")
async def delete_session(session_id: str, db: AsyncSession = Depends(get_db)):
    """Stop and remove a session's container."""
    result = await db.execute(
        select(AgentSession).where(AgentSession.id == session_id)
    )
    session = result.scalar_one_or_none()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if session.container_id:
        await stop_session_container(session.container_id)

    await db.execute(
        update(AgentSession)
        .where(AgentSession.id == session_id)
        .values(status="stopped")
    )
    await db.commit()
    session_messages.pop(session_id, None)
    return {"status": "stopped"}


@app.websocket("/ws/{session_id}")
async def websocket_endpoint(websocket: WebSocket, session_id: str):
    """WebSocket for real-time streaming to frontend."""
    await websocket.accept()

    if session_id not in active_websockets:
        active_websockets[session_id] = set()
    active_websockets[session_id].add(websocket)

    try:
        # Send current session status immediately on connect
        async for db in get_db():
            result = await db.execute(
                select(AgentSession).where(AgentSession.id == session_id)
            )
            session = result.scalar_one_or_none()
            if session:
                await websocket.send_json({
                    "type": "session_status",
                    "status": session.status,
                    "vnc_url": (
                        f"http://localhost:{session.vnc_port}/vnc.html"
                        "?autoconnect=1&resize=scale&view_only=1"
                        if session.vnc_port else None
                    ),
                })

        # Keep alive until client disconnects
        while True:
            await websocket.receive_text()

    except WebSocketDisconnect:
        active_websockets.get(session_id, set()).discard(websocket)


# ── Helpers ───────────────────────────────────────────────────────────────────

async def _broadcast(session_id: str, data: dict):
    """Send a message to all WebSocket clients watching this session."""
    sockets = active_websockets.get(session_id, set())
    dead = set()
    for ws in sockets:
        try:
            await ws.send_json(data)
        except Exception:
            dead.add(ws)
    sockets -= dead


async def _get_db_session():
    """Helper to get a DB session outside of a request context."""
    from database import AsyncSessionLocal
    return AsyncSessionLocal()


async def cleanup_idle_sessions():
    """
    Every 5 minutes: stop containers idle for more than 30 minutes.
    Keeps t2.micro RAM free.
    """
    while True:
        await asyncio.sleep(300)
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
        async with (await _get_db_session()) as db:
            result = await db.execute(
                select(AgentSession).where(
                    AgentSession.status == "ready",
                    AgentSession.last_active < cutoff,
                )
            )
            idle = result.scalars().all()
            for session in idle:
                if session.container_id:
                    await stop_session_container(session.container_id)
                await db.execute(
                    update(AgentSession)
                    .where(AgentSession.id == session.id)
                    .values(status="stopped")
                )
            await db.commit()