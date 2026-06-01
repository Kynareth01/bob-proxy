"""
bob-proxy: OpenAI-compatible API proxy for IBM Bob Shell.

Wraps `bob` CLI (non-interactive mode) behind a standard
/v1/chat/completions endpoint so any OpenAI SDK can use it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sse_starlette.sse import EventSourceResponse

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BOBSHELL_API_KEY = os.environ.get("BOBSHELL_API_KEY", "")
PROXY_API_KEY = os.environ.get("PROXY_API_KEY", "")  # protect this proxy
BOB_BIN = os.environ.get("BOB_BIN", "bob")
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT", "4"))
BOB_TIMEOUT = int(os.environ.get("BOB_TIMEOUT", "120"))
DEFAULT_MODEL = os.environ.get("DEFAULT_MODEL", "ibm-bob")

log = logging.getLogger("bob-proxy")

# Semaphore to cap concurrent bob processes (each eats Bobcoins)
_sem: asyncio.Semaphore | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    global _sem
    _sem = asyncio.Semaphore(MAX_CONCURRENT)
    log.info("bob-proxy started  bob=%s  max_concurrent=%d", BOB_BIN, MAX_CONCURRENT)
    yield
    log.info("bob-proxy shutting down")


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="bob-proxy", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Auth middleware
# ---------------------------------------------------------------------------

@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    # Health endpoint is public
    if request.url.path in ("/health", "/v1/models"):
        return await call_next(request)
    if PROXY_API_KEY:
        auth = request.headers.get("authorization", "")
        if not auth.startswith("Bearer ") or auth[7:] != PROXY_API_KEY:
            return JSONResponse(
                status_code=401,
                content={"error": {"message": "Invalid API key", "type": "auth_error"}},
            )
    return await call_next(request)


# ---------------------------------------------------------------------------
# Pydantic models (OpenAI-compatible)
# ---------------------------------------------------------------------------

class ChatMessage(BaseModel):
    role: str
    content: str | list | None = None
    name: str | None = None


class ChatCompletionRequest(BaseModel):
    model: str = DEFAULT_MODEL
    messages: list[ChatMessage]
    temperature: float | None = None
    max_tokens: int | None = None
    stream: bool = False
    top_p: float | None = None
    stop: str | list[str] | None = None
    # Bob-specific extras (ignored by OpenAI spec, harmless)
    yolo: bool = False
    chat_mode: str | None = None  # plan, code, advanced, ask


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChoiceMessage(BaseModel):
    role: str = "assistant"
    content: str


class Choice(BaseModel):
    index: int = 0
    message: ChoiceMessage
    finish_reason: str = "stop"


class ChatCompletionResponse(BaseModel):
    id: str = Field(default_factory=lambda: f"chatcmpl-{uuid.uuid4().hex[:12]}")
    object: str = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str = DEFAULT_MODEL
    choices: list[Choice]
    usage: Usage = Field(default_factory=Usage)


class DeltaMessage(BaseModel):
    role: str | None = None
    content: str | None = None


class StreamChoice(BaseModel):
    index: int = 0
    delta: DeltaMessage
    finish_reason: str | None = None


class ChatCompletionChunk(BaseModel):
    id: str = Field(default_factory=lambda: f"chatcmpl-{uuid.uuid4().hex[:12]}")
    object: str = "chat.completion.chunk"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str = DEFAULT_MODEL
    choices: list[StreamChoice]


# ---------------------------------------------------------------------------
# Bob Shell runner
# ---------------------------------------------------------------------------

def _build_bob_command(req: ChatCompletionRequest, prompt: str) -> list[str]:
    """Build the bob CLI command."""
    cmd = [BOB_BIN, "--auth-method", "api-key"]

    if req.yolo:
        cmd.append("--yolo")

    if req.chat_mode:
        cmd.extend(["--chat-mode", req.chat_mode])

    # Hide intermediary output for cleaner parsing
    cmd.append("--hide-intermediary-output")
    cmd.extend(["-p", prompt])
    return cmd


def _extract_response(raw: str) -> str:
    """Extract the actual response text from Bob Shell output.

    Bob Shell output may contain:
    - <thinking>...</thinking> blocks
    - [using tool ...] lines
    - ---output--- delimited content
    - Raw text
    """
    text = raw.strip()

    # Try to extract from ---output--- delimiters first
    output_match = re.search(r"---output---\s*\n(.*?)\n\s*---output---", text, re.DOTALL)
    if output_match:
        return output_match.group(1).strip()

    # Remove <thinking> blocks
    text = re.sub(r"<thinking>.*?</thinking>", "", text, flags=re.DOTALL)

    # Remove [using tool ...] lines
    text = re.sub(r"\[using tool[^\]]*\]", "", text)

    # Remove cost lines
    text = re.sub(r"Cost:.*", "", text)

    return text.strip()


async def run_bob(req: ChatCompletionRequest) -> tuple[str, int]:
    """Run Bob Shell and return (response_text, cost_estimate)."""
    # Build prompt from messages
    prompt_parts: list[str] = []
    for msg in req.messages:
        if msg.role == "system":
            prompt_parts.append(f"[System instruction]: {msg.content}")
        elif msg.role == "user":
            content = msg.content if isinstance(msg.content, str) else json.dumps(msg.content)
            prompt_parts.append(content)
        elif msg.role == "assistant":
            prompt_parts.append(f"[Previous assistant response]: {msg.content}")

    prompt = "\n\n".join(prompt_parts)
    cmd = _build_bob_command(req, prompt)

    assert _sem is not None
    async with _sem:
        log.info("bob exec: %s", " ".join(cmd[:6]) + " ...")
        t0 = time.monotonic()

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env={**os.environ, "BOBSHELL_API_KEY": BOBSHELL_API_KEY},
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=BOB_TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise HTTPException(504, "Bob Shell timed out")

        elapsed = time.monotonic() - t0
        log.info("bob done in %.1fs  exit=%d", elapsed, proc.returncode)

        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip()
            log.error("bob error: %s", err[:300])
            raise HTTPException(502, f"Bob Shell error: {err[:200]}")

        raw = stdout.decode(errors="replace")
        response = _extract_response(raw)

        if not response:
            raise HTTPException(502, "Bob Shell returned empty response")

        # Rough token estimate (4 chars ≈ 1 token)
        est_tokens = len(response) // 4
        return response, est_tokens


async def run_bob_streaming(req: ChatCompletionRequest) -> AsyncIterator[str]:
    """Stream Bob Shell output as SSE chunks.

    Note: Bob Shell doesn't support true token-level streaming to stdout,
    so we run the full command and simulate streaming by chunking the output.
    """
    prompt_parts: list[str] = []
    for msg in req.messages:
        if msg.role == "system":
            prompt_parts.append(f"[System instruction]: {msg.content}")
        elif msg.role == "user":
            content = msg.content if isinstance(msg.content, str) else json.dumps(msg.content)
            prompt_parts.append(content)
        elif msg.role == "assistant":
            prompt_parts.append(f"[Previous assistant response]: {msg.content}")

    prompt = "\n\n".join(prompt_parts)
    cmd = _build_bob_command(req, prompt)

    assert _sem is not None
    async with _sem:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env={**os.environ, "BOBSHELL_API_KEY": BOBSHELL_API_KEY},
        )

        # Wait for completion (Bob Shell doesn't stream token-by-token to stdout)
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=BOB_TIMEOUT
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise HTTPException(504, "Bob Shell timed out")

        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip()
            raise HTTPException(502, f"Bob Shell error: {err[:200]}")

        raw = stdout.decode(errors="replace")
        response = _extract_response(raw)
        if not response:
            raise HTTPException(502, "Bob Shell returned empty response")

    # Simulate streaming by sending chunks
    chunk_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    # Send role delta first
    yield json.dumps({
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": req.model,
        "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
    })

    # Send content in ~20 char chunks to simulate streaming
    chunk_size = 20
    for i in range(0, len(response), chunk_size):
        chunk = response[i : i + chunk_size]
        yield json.dumps({
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": req.model,
            "choices": [{"index": 0, "delta": {"content": chunk}, "finish_reason": None}],
        })

    # Send finish
    yield json.dumps({
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": req.model,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    })


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {"status": "ok", "service": "bob-proxy"}


@app.get("/v1/models")
async def list_models():
    return {
        "object": "list",
        "data": [
            {
                "id": DEFAULT_MODEL,
                "object": "model",
                "created": 1700000000,
                "owned_by": "ibm",
                "permission": [],
            },
            {
                "id": "ibm-bob-code",
                "object": "model",
                "created": 1700000000,
                "owned_by": "ibm",
                "permission": [],
            },
            {
                "id": "ibm-bob-ask",
                "object": "model",
                "created": 1700000000,
                "owned_by": "ibm",
                "permission": [],
            },
        ],
    }


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatCompletionRequest):
    # Map model names to Bob modes
    if req.model in ("ibm-bob-code", "bob-code"):
        req.chat_mode = "code"
    elif req.model in ("ibm-bob-ask", "bob-ask"):
        req.chat_mode = "ask"
    elif req.model in ("ibm-bob-plan", "bob-plan"):
        req.chat_mode = "plan"

    if req.stream:
        return EventSourceResponse(run_bob_streaming(req), media_type="text/event-stream")

    response_text, est_tokens = await run_bob(req)

    return ChatCompletionResponse(
        model=req.model,
        choices=[
            Choice(
                message=ChoiceMessage(content=response_text),
                finish_reason="stop",
            )
        ],
        usage=Usage(
            prompt_tokens=est_tokens,
            completion_tokens=est_tokens,
            total_tokens=est_tokens * 2,
        ),
    )


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    uvicorn.run(
        "server:app",
        host=os.environ.get("HOST", "0.0.0.0"),
        port=int(os.environ.get("PORT", "8787")),
        log_level="info",
    )
