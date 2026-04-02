"""
Multi-User LinkedIn HTTP API Server with Login via Screenshot Streaming.

Single port. Two modes:
1. Action API: per-request cookie injection for messaging/scraping
2. Login: opens a visible browser, streams screenshots via WebSocket,
   receives mouse/keyboard events, captures cookies on successful login

Usage:
  uv run python api_server.py

Environment:
  LINKEDIN_API_KEY - API key for auth (optional)
  PORT - Server port (default 8000)
"""

import asyncio
import base64
import json
import logging
import os
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Header, WebSocket, WebSocketDisconnect
from pydantic import BaseModel, Field
import uvicorn

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

API_KEY = os.environ.get("LINKEDIN_API_KEY", "")

# Active login sessions
_login_sessions: dict[str, dict[str, Any]] = {}


def verify_api_key(x_api_key: str = Header(default="")):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("LinkedIn API server starting...")
    try:
        from linkedin_mcp_server.bootstrap import ensure_browser_cache
        await ensure_browser_cache()
    except Exception as e:
        logger.warning(f"Browser cache pre-warm skipped: {e}")
    yield
    # Cleanup all login sessions
    for sid in list(_login_sessions.keys()):
        await _cleanup_login_session(sid)
    # Close shared browser for action API
    try:
        from linkedin_mcp_server.drivers.stateless import close_shared_browser
        await close_shared_browser()
    except Exception:
        pass
    logger.info("Shutdown complete.")


app = FastAPI(title="LinkedIn Multi-User API", lifespan=lifespan)


# ============================================================
# ACTION API: per-request cookie injection (messaging, scraping)
# ============================================================

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


@app.get("/health")
async def health():
    return {"status": "ok", "active_login_sessions": len(_login_sessions)}


@app.post("/api/profile")
async def get_profile(req: ProfileRequest, x_api_key: str = Header(default="")):
    verify_api_key(x_api_key)
    from linkedin_mcp_server.drivers.stateless import create_linkedin_context
    try:
        async with create_linkedin_context(req.li_at, req.jsessionid) as (extractor, _):
            requested = {s.strip() for s in req.sections.split(",")} if req.sections else set()
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
            result = await extractor.search_people(keywords=req.keywords, location=req.location, limit=req.limit)
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
            result = await extractor.send_message(req.username, req.message, confirm_send=True)
            return {"success": True, "data": result}
    except Exception as e:
        logger.error(f"Message error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/connect")
async def connect_with_person(req: ConnectRequest, x_api_key: str = Header(default="")):
    verify_api_key(x_api_key)
    from linkedin_mcp_server.drivers.stateless import create_linkedin_context
    try:
        async with create_linkedin_context(req.li_at, req.jsessionid) as (extractor, page):
            logger.info(f"Navigating to profile: {req.username}")
            result = await extractor.connect_with_person(req.username, note=req.note)
            logger.info(f"Connect result for {req.username}: {json.dumps(result, default=str)[:500]}")

            # Debug: take screenshot after connect attempt
            try:
                import base64 as b64mod
                screenshot = await page.screenshot(type="jpeg", quality=50)
                screenshot_b64 = b64mod.b64encode(screenshot).decode("ascii")
                result["debug_screenshot"] = screenshot_b64
                logger.info(f"Post-connect page URL: {page.url}")
                # Log visible buttons on the page
                buttons = await page.locator("main button").all_text_contents()
                logger.info(f"Visible buttons after connect: {buttons[:10]}")
            except Exception as e:
                logger.warning(f"Debug screenshot failed: {e}")

            return {"success": True, "data": result}
    except Exception as e:
        logger.error(f"Connect error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/debug-profile")
async def debug_profile(req: ConnectRequest, x_api_key: str = Header(default="")):
    """Debug: see what the browser sees on a LinkedIn profile page."""
    verify_api_key(x_api_key)
    from linkedin_mcp_server.drivers.stateless import create_linkedin_context
    from linkedin_mcp_server.scraping.connection import detect_connection_state
    try:
        async with create_linkedin_context(req.li_at, req.jsessionid) as (extractor, page):
            profile = await extractor.scrape_person(req.username, {"main_profile"})
            page_text = profile.get("sections", {}).get("main_profile", "")
            state = detect_connection_state(page_text)

            # Get the first 500 chars of the action area
            action_area = page_text[:500] if page_text else "EMPTY"

            # Take a screenshot
            screenshot = await page.screenshot(type="jpeg", quality=50)
            b64 = base64.b64encode(screenshot).decode("ascii")

            # Check for buttons
            buttons = await page.locator("main button").all_text_contents()

            return {
                "success": True,
                "username": req.username,
                "detected_state": state,
                "action_area_preview": action_area,
                "buttons_found": buttons[:20],
                "page_url": page.url,
                "screenshot_b64": b64[:100] + "...",  # just confirm it works
            }
    except Exception as e:
        logger.error(f"Debug error: {e}", exc_info=True)
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


@app.post("/api/company")
async def get_company(req: CompanyRequest, x_api_key: str = Header(default="")):
    verify_api_key(x_api_key)
    from linkedin_mcp_server.drivers.stateless import create_linkedin_context
    try:
        async with create_linkedin_context(req.li_at, req.jsessionid) as (extractor, _):
            requested = {s.strip() for s in req.sections.split(",")} if req.sections else set()
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
            result = await extractor.search_jobs(keywords=req.keywords, location=req.location, limit=req.limit)
            return {"success": True, "data": result}
    except Exception as e:
        logger.error(f"Job search error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# LOGIN API: browser-based login with screenshot streaming
# ============================================================

class LoginStartRequest(BaseModel):
    session_id: str

class LoginStartResponse(BaseModel):
    success: bool
    session_id: str
    ws_url: str  # WebSocket URL for screenshot stream


@app.post("/api/login/start")
async def start_login(req: LoginStartRequest, x_api_key: str = Header(default="")):
    verify_api_key(x_api_key)
    session_id = req.session_id

    # Kill existing session
    if session_id in _login_sessions:
        await _cleanup_login_session(session_id)

    logger.info(f"Starting login session for {session_id}")

    try:
        from patchright.async_api import async_playwright

        pw = await async_playwright().start()
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            locale="en-US",
        )
        page = await context.new_page()
        await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")

        _login_sessions[session_id] = {
            "playwright": pw,
            "browser": browser,
            "context": context,
            "page": page,
            "status": "pending",
            "cookies": None,
            "created_at": time.time(),
        }

        # Start background monitor
        asyncio.create_task(_monitor_login(session_id))

        host = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "localhost:8000")
        ws_url = f"wss://{host}/ws/login/{session_id}"

        return {"success": True, "session_id": session_id, "ws_url": ws_url}

    except Exception as e:
        logger.error(f"Login start error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/login/status")
async def login_status(session_id: str, x_api_key: str = Header(default="")):
    verify_api_key(x_api_key)
    session = _login_sessions.get(session_id)
    if not session:
        return {"status": "not_found"}
    return {
        "status": session["status"],
        "cookies": session.get("cookies"),
    }


@app.websocket("/ws/login/{session_id}")
async def login_websocket(ws: WebSocket, session_id: str):
    """WebSocket: streams screenshots + receives mouse/keyboard events."""
    await ws.accept()

    session = _login_sessions.get(session_id)
    if not session:
        await ws.send_json({"type": "error", "message": "Session not found"})
        await ws.close()
        return

    page = session["page"]
    logger.info(f"WebSocket connected for login session {session_id}")

    # Start screenshot streaming task
    streaming = True

    async def stream_screenshots():
        while streaming and session_id in _login_sessions:
            try:
                # Check if login succeeded
                if session.get("status") == "logged_in":
                    try:
                        await ws.send_json({"type": "login_success", "message": "LinkedIn connected!"})
                    except Exception:
                        pass
                    break

                screenshot = await page.screenshot(type="jpeg", quality=60)
                b64 = base64.b64encode(screenshot).decode("ascii")
                await ws.send_json({"type": "screenshot", "data": b64})
                await asyncio.sleep(0.4)  # ~2.5 fps
            except Exception as e:
                if "closed" in str(e).lower():
                    break
                logger.debug(f"Screenshot error: {e}")
                await asyncio.sleep(1)

    screenshot_task = asyncio.create_task(stream_screenshots())

    try:
        while True:
            msg = await ws.receive_json()

            if msg["type"] == "click":
                await page.mouse.click(msg["x"], msg["y"])

            elif msg["type"] == "type":
                await page.keyboard.type(msg["text"])

            elif msg["type"] == "key":
                await page.keyboard.press(msg["key"])

            elif msg["type"] == "scroll":
                await page.mouse.wheel(msg.get("deltaX", 0), msg.get("deltaY", 0))

            # Check if login completed after each interaction
            if session.get("status") == "logged_in":
                await ws.send_json({"type": "login_success", "message": "LinkedIn connected!"})
                break

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected for {session_id}")
    except Exception as e:
        logger.error(f"WebSocket error: {e}")
    finally:
        streaming = False
        screenshot_task.cancel()


async def _monitor_login(session_id: str):
    """Background: check if LinkedIn login succeeded by monitoring cookies."""
    session = _login_sessions.get(session_id)
    if not session:
        return

    timeout = 300  # 5 minutes
    start = time.time()

    while time.time() - start < timeout:
        await asyncio.sleep(3)

        if session_id not in _login_sessions:
            return

        try:
            page = session["page"]
            url = page.url

            # Check if we're past the login page
            if any(x in url for x in ["/feed", "/mynetwork", "/messaging", "/in/"]):
                # Extract cookies
                cookies = await session["context"].cookies()
                li_at = next((c["value"] for c in cookies if c["name"] == "li_at"), None)
                jsessionid = next((c["value"] for c in cookies if c["name"] == "JSESSIONID"), None)

                if li_at:
                    logger.info(f"Login successful for {session_id}!")
                    session["status"] = "logged_in"
                    session["cookies"] = {"li_at": li_at, "JSESSIONID": jsessionid or ""}
                    return
        except Exception as e:
            logger.debug(f"Monitor check error: {e}")

    # Timeout
    logger.warning(f"Login session {session_id} timed out")
    session["status"] = "expired"
    await _cleanup_login_session(session_id)


async def _cleanup_login_session(session_id: str):
    """Close browser and clean up a login session."""
    session = _login_sessions.pop(session_id, None)
    if not session:
        return

    logger.info(f"Cleaning up login session {session_id}")
    try:
        await session["context"].close()
    except Exception:
        pass
    try:
        await session["browser"].close()
    except Exception:
        pass
    try:
        await session["playwright"].stop()
    except Exception:
        pass


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run("api_server:app", host="0.0.0.0", port=port, log_level="info")
