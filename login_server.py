"""
LinkedIn Login Server with noVNC.

Manages browser-based LinkedIn login sessions.
Each login request creates a VNC-accessible browser instance.
The user logs in via noVNC in their browser, then cookies are captured.

Endpoints:
  POST /api/login/start    - Start a login session, returns noVNC URL
  GET  /api/login/status   - Check if login succeeded, returns cookies
  POST /api/login/stop     - Close the login browser
"""

import asyncio
import json
import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
import uvicorn

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

API_KEY = os.environ.get("LINKEDIN_API_KEY", "")

# Active login sessions: session_id -> session info
_login_sessions: dict[str, dict[str, Any]] = {}


def verify_api_key(x_api_key: str = Header(default="")):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


class LoginStartRequest(BaseModel):
    session_id: str  # PsView user ID or unique session identifier
    callback_url: str | None = None  # URL to POST cookies to when login succeeds


class LoginStatusResponse(BaseModel):
    status: str  # "pending" | "logged_in" | "expired" | "error"
    cookies: dict[str, str] | None = None
    vnc_url: str | None = None


app = FastAPI(title="LinkedIn Login Server")


@app.post("/api/login/start")
async def start_login(req: LoginStartRequest, x_api_key: str = Header(default="")):
    verify_api_key(x_api_key)

    session_id = req.session_id

    # Kill existing session if any
    if session_id in _login_sessions:
        await _cleanup_session(session_id)

    logger.info(f"Starting login session for {session_id}")

    try:
        # Find available ports for VNC and noVNC
        vnc_port = 5900 + (hash(session_id) % 100)
        novnc_port = 6080 + (hash(session_id) % 100)

        # Create a temporary profile directory
        profile_dir = tempfile.mkdtemp(prefix=f"linkedin-login-{session_id}-")

        # Start Xvfb (virtual display)
        display = f":{vnc_port - 5900 + 99}"
        xvfb_proc = subprocess.Popen(
            ["Xvfb", display, "-screen", "0", "1280x800x24", "-ac"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

        # Start x11vnc on the display
        vnc_proc = subprocess.Popen(
            ["x11vnc", "-display", display, "-rfbport", str(vnc_port),
             "-nopw", "-forever", "-shared", "-noxdamage"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

        # Start noVNC websocket proxy
        novnc_proc = subprocess.Popen(
            ["websockify", "--web", "/usr/share/novnc",
             str(novnc_port), f"localhost:{vnc_port}"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

        # Wait for services to start
        await asyncio.sleep(2)

        # Launch Chromium on the virtual display
        env = os.environ.copy()
        env["DISPLAY"] = display

        chrome_proc = subprocess.Popen(
            ["chromium", "--no-sandbox", "--disable-gpu",
             "--window-size=1280,800", "--window-position=0,0",
             f"--user-data-dir={profile_dir}",
             "https://www.linkedin.com/login"],
            env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )

        # Build noVNC URL
        host = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "localhost")
        vnc_url = f"https://{host}/vnc/?port={novnc_port}&autoconnect=true&resize=scale"

        session = {
            "session_id": session_id,
            "status": "pending",
            "vnc_url": vnc_url,
            "novnc_port": novnc_port,
            "vnc_port": vnc_port,
            "profile_dir": profile_dir,
            "display": display,
            "procs": {
                "xvfb": xvfb_proc,
                "vnc": vnc_proc,
                "novnc": novnc_proc,
                "chrome": chrome_proc,
            },
            "callback_url": req.callback_url,
            "created_at": time.time(),
            "cookies": None,
        }

        _login_sessions[session_id] = session

        # Start background task to monitor login status
        asyncio.create_task(_monitor_login(session_id))

        logger.info(f"Login session started. noVNC at port {novnc_port}")

        return {
            "success": True,
            "vnc_url": vnc_url,
            "novnc_port": novnc_port,
            "session_id": session_id,
        }

    except Exception as e:
        logger.error(f"Failed to start login session: {e}")
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
        "vnc_url": session.get("vnc_url"),
    }


@app.post("/api/login/stop")
async def stop_login(session_id: str, x_api_key: str = Header(default="")):
    verify_api_key(x_api_key)

    if session_id in _login_sessions:
        await _cleanup_session(session_id)
        return {"success": True}
    return {"success": False, "error": "Session not found"}


async def _monitor_login(session_id: str):
    """Background task: poll the browser's cookies until LinkedIn login is detected."""
    session = _login_sessions.get(session_id)
    if not session:
        return

    profile_dir = session["profile_dir"]
    timeout = 300  # 5 minutes
    start_time = time.time()

    logger.info(f"Monitoring login for session {session_id} (timeout: {timeout}s)")

    while time.time() - start_time < timeout:
        await asyncio.sleep(3)

        # Check if session was cleaned up
        if session_id not in _login_sessions:
            return

        # Check Chrome's cookie file for li_at
        try:
            cookies = await _extract_cookies_from_profile(profile_dir)
            if cookies and cookies.get("li_at"):
                logger.info(f"Login detected for session {session_id}!")
                session["status"] = "logged_in"
                session["cookies"] = cookies

                # POST cookies to callback if configured
                if session.get("callback_url"):
                    await _send_cookies_callback(session["callback_url"], cookies, session_id)

                return
        except Exception as e:
            logger.debug(f"Cookie check error: {e}")

    # Timeout
    logger.warning(f"Login session {session_id} timed out")
    session["status"] = "expired"
    await _cleanup_session(session_id)


async def _extract_cookies_from_profile(profile_dir: str) -> dict[str, str] | None:
    """Extract LinkedIn cookies from Chrome's cookie database."""
    import sqlite3

    cookie_db = Path(profile_dir) / "Default" / "Cookies"
    if not cookie_db.exists():
        # Try alternate path
        cookie_db = Path(profile_dir) / "Cookies"
        if not cookie_db.exists():
            return None

    # Chrome locks the DB, so copy it first
    tmp_db = Path(profile_dir) / "cookies_copy.db"
    import shutil
    shutil.copy2(str(cookie_db), str(tmp_db))

    try:
        conn = sqlite3.connect(str(tmp_db))
        cursor = conn.cursor()

        # Query for LinkedIn cookies
        cursor.execute(
            "SELECT name, value FROM cookies WHERE host_key LIKE '%linkedin.com' AND name IN ('li_at', 'JSESSIONID')"
        )
        rows = cursor.fetchall()
        conn.close()

        cookies = {}
        for name, value in rows:
            if value:  # Chrome may encrypt values, but Chromium on Linux usually doesn't
                cookies[name] = value

        return cookies if cookies.get("li_at") else None
    except Exception as e:
        logger.debug(f"SQLite error: {e}")
        return None
    finally:
        tmp_db.unlink(missing_ok=True)


async def _send_cookies_callback(callback_url: str, cookies: dict, session_id: str):
    """POST captured cookies to the callback URL (PsView Edge Function)."""
    import aiohttp

    try:
        async with aiohttp.ClientSession() as http_session:
            async with http_session.post(callback_url, json={
                "session_id": session_id,
                "li_at": cookies.get("li_at", ""),
                "jsessionid": cookies.get("JSESSIONID", ""),
            }) as resp:
                logger.info(f"Cookie callback response: {resp.status}")
    except Exception as e:
        logger.error(f"Cookie callback failed: {e}")


async def _cleanup_session(session_id: str):
    """Kill all processes and clean up a login session."""
    session = _login_sessions.pop(session_id, None)
    if not session:
        return

    logger.info(f"Cleaning up session {session_id}")

    for name, proc in session.get("procs", {}).items():
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    # Clean up profile directory
    profile_dir = session.get("profile_dir")
    if profile_dir:
        import shutil
        shutil.rmtree(profile_dir, ignore_errors=True)


if __name__ == "__main__":
    port = int(os.environ.get("LOGIN_PORT", 8001))
    uvicorn.run("login_server:app", host="0.0.0.0", port=port, log_level="info")
