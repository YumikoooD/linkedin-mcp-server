"""
Multi-User LinkedIn HTTP API Server.

Each request includes LinkedIn cookies (li_at + jsessionid).
A temporary browser context is created per request, cookies injected, action executed, context destroyed.
No persistent sessions on the server.

Usage:
  uv run python api_server.py

Environment:
  LINKEDIN_API_KEY - API key for auth (optional)
  PORT - Server port (default 8000)
"""

import logging
import os
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel, Field
import uvicorn

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

API_KEY = os.environ.get("LINKEDIN_API_KEY", "")


def verify_api_key(x_api_key: str = Header(default="")):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("LinkedIn API server starting...")
    # Pre-warm: ensure Patchright Chromium is installed
    try:
        from linkedin_mcp_server.bootstrap import ensure_browser_cache
        await ensure_browser_cache()
        logger.info("Browser cache ready.")
    except Exception as e:
        logger.warning(f"Browser cache pre-warm skipped: {e}")

    yield

    # Cleanup shared browser on shutdown
    from linkedin_mcp_server.drivers.stateless import close_shared_browser
    await close_shared_browser()
    logger.info("Shutdown complete.")


app = FastAPI(title="LinkedIn Multi-User API", lifespan=lifespan)


# --- Request models ---
# Every request includes LinkedIn cookies for the recruiter

class AuthenticatedRequest(BaseModel):
    li_at: str
    jsessionid: str

class ProfileRequest(AuthenticatedRequest):
    username: str
    sections: str | None = None

class SearchPeopleRequest(AuthenticatedRequest):
    keywords: str
    location: str | None = None
    limit: int = Field(default=10, ge=1, le=50)

class SendMessageRequest(AuthenticatedRequest):
    username: str
    message: str

class ConnectRequest(AuthenticatedRequest):
    username: str
    note: str | None = None

class InboxRequest(AuthenticatedRequest):
    limit: int = Field(default=20, ge=1, le=50)

class ConversationRequest(AuthenticatedRequest):
    username: str | None = None
    thread_id: str | None = None

class CompanyRequest(AuthenticatedRequest):
    company_name: str
    sections: str | None = None

class JobSearchRequest(AuthenticatedRequest):
    keywords: str
    location: str | None = None
    limit: int = Field(default=10, ge=1, le=25)

class SearchConversationsRequest(AuthenticatedRequest):
    keywords: str


# --- Endpoints ---

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/api/profile")
async def get_profile(req: ProfileRequest, x_api_key: str = Header(default="")):
    verify_api_key(x_api_key)
    from linkedin_mcp_server.drivers.stateless import create_linkedin_context
    try:
        async with create_linkedin_context(req.li_at, req.jsessionid) as (extractor, _):
            requested = set()
            if req.sections:
                requested = {s.strip() for s in req.sections.split(",")}
            result = await extractor.scrape_person(req.username, requested=requested)
            return {"success": True, "data": result}
    except Exception as e:
        logger.error(f"Profile error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/search")
async def search_people(req: SearchPeopleRequest, x_api_key: str = Header(default="")):
    verify_api_key(x_api_key)
    from linkedin_mcp_server.drivers.stateless import create_linkedin_context
    try:
        async with create_linkedin_context(req.li_at, req.jsessionid) as (extractor, _):
            result = await extractor.search_people(
                keywords=req.keywords,
                location=req.location,
                limit=req.limit
            )
            return {"success": True, "data": result}
    except Exception as e:
        logger.error(f"Search error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/message")
async def send_message(req: SendMessageRequest, x_api_key: str = Header(default="")):
    verify_api_key(x_api_key)
    from linkedin_mcp_server.drivers.stateless import create_linkedin_context
    try:
        async with create_linkedin_context(req.li_at, req.jsessionid) as (extractor, _):
            result = await extractor.send_message(
                req.username,
                req.message,
                confirm_send=True
            )
            return {"success": True, "data": result}
    except Exception as e:
        logger.error(f"Message error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/connect")
async def connect_with_person(req: ConnectRequest, x_api_key: str = Header(default="")):
    verify_api_key(x_api_key)
    from linkedin_mcp_server.drivers.stateless import create_linkedin_context
    try:
        async with create_linkedin_context(req.li_at, req.jsessionid) as (extractor, _):
            result = await extractor.connect_with_person(req.username, note=req.note)
            return {"success": True, "data": result}
    except Exception as e:
        logger.error(f"Connect error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/inbox")
async def get_inbox(req: InboxRequest, x_api_key: str = Header(default="")):
    verify_api_key(x_api_key)
    from linkedin_mcp_server.drivers.stateless import create_linkedin_context
    try:
        async with create_linkedin_context(req.li_at, req.jsessionid) as (extractor, _):
            result = await extractor.get_inbox(limit=req.limit)
            return {"success": True, "data": result}
    except Exception as e:
        logger.error(f"Inbox error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/conversation")
async def get_conversation(req: ConversationRequest, x_api_key: str = Header(default="")):
    verify_api_key(x_api_key)
    from linkedin_mcp_server.drivers.stateless import create_linkedin_context
    try:
        async with create_linkedin_context(req.li_at, req.jsessionid) as (extractor, _):
            if req.thread_id:
                result = await extractor.get_conversation(thread_id=req.thread_id)
            elif req.username:
                result = await extractor.get_conversation(username=req.username)
            else:
                raise HTTPException(status_code=400, detail="username or thread_id required")
            return {"success": True, "data": result}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Conversation error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/search-conversations")
async def search_conversations(req: SearchConversationsRequest, x_api_key: str = Header(default="")):
    verify_api_key(x_api_key)
    from linkedin_mcp_server.drivers.stateless import create_linkedin_context
    try:
        async with create_linkedin_context(req.li_at, req.jsessionid) as (extractor, _):
            result = await extractor.search_conversations(keywords=req.keywords)
            return {"success": True, "data": result}
    except Exception as e:
        logger.error(f"Search conversations error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/company")
async def get_company(req: CompanyRequest, x_api_key: str = Header(default="")):
    verify_api_key(x_api_key)
    from linkedin_mcp_server.drivers.stateless import create_linkedin_context
    try:
        async with create_linkedin_context(req.li_at, req.jsessionid) as (extractor, _):
            requested = set()
            if req.sections:
                requested = {s.strip() for s in req.sections.split(",")}
            result = await extractor.scrape_company(req.company_name, requested=requested)
            return {"success": True, "data": result}
    except Exception as e:
        logger.error(f"Company error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/jobs")
async def search_jobs(req: JobSearchRequest, x_api_key: str = Header(default="")):
    verify_api_key(x_api_key)
    from linkedin_mcp_server.drivers.stateless import create_linkedin_context
    try:
        async with create_linkedin_context(req.li_at, req.jsessionid) as (extractor, _):
            result = await extractor.search_jobs(
                keywords=req.keywords,
                location=req.location,
                limit=req.limit
            )
            return {"success": True, "data": result}
    except Exception as e:
        logger.error(f"Job search error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("api_server:app", host="0.0.0.0", port=port, log_level="info")
