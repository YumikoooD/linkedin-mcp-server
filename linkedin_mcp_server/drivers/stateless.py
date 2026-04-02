"""
Multi-user browser sessions with persistent profiles stored in Supabase Storage.

Login: creates a persistent browser profile, tars it, uploads to Supabase.
Action: downloads the profile, launches browser with it, executes action, cleans up.
"""

import asyncio
import logging
import os
import shutil
import tarfile
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator

from patchright.async_api import async_playwright, BrowserContext, Page

from linkedin_mcp_server.scraping.extractor import LinkedInExtractor

logger = logging.getLogger(__name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
PROFILE_BUCKET = "linkedin-profiles"


async def _ensure_bucket():
    """Create the Supabase Storage bucket if it doesn't exist."""
    import httpx
    try:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{SUPABASE_URL}/storage/v1/bucket",
                headers={
                    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                    "apikey": SUPABASE_SERVICE_KEY,
                },
                json={"id": PROFILE_BUCKET, "name": PROFILE_BUCKET, "public": False},
            )
    except Exception as e:
        logger.debug(f"Bucket creation (may already exist): {e}")


async def upload_profile(user_id: str, profile_dir: str):
    """Tar.gz a browser profile directory and upload to Supabase Storage."""
    import httpx

    await _ensure_bucket()

    tar_path = f"/tmp/{user_id}-profile.tar.gz"
    logger.info(f"Compressing profile for {user_id}...")

    # Tar.gz the profile directory
    with tarfile.open(tar_path, "w:gz") as tar:
        tar.add(profile_dir, arcname="profile")

    file_size = os.path.getsize(tar_path)
    logger.info(f"Profile compressed: {file_size / 1024 / 1024:.1f} MB")

    # Upload to Supabase Storage
    logger.info(f"Uploading profile for {user_id} to Supabase Storage...")
    async with httpx.AsyncClient(timeout=120) as client:
        with open(tar_path, "rb") as f:
            resp = await client.put(
                f"{SUPABASE_URL}/storage/v1/object/{PROFILE_BUCKET}/{user_id}.tar.gz",
                headers={
                    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                    "apikey": SUPABASE_SERVICE_KEY,
                    "Content-Type": "application/gzip",
                    "x-upsert": "true",
                },
                content=f.read(),
            )
        if resp.status_code not in (200, 201):
            logger.error(f"Upload failed: {resp.status_code} {resp.text}")
            raise Exception(f"Profile upload failed: {resp.status_code}")

    logger.info(f"Profile uploaded for {user_id}")
    os.unlink(tar_path)


async def download_profile(user_id: str) -> str | None:
    """Download and extract a browser profile from Supabase Storage. Returns profile dir path."""
    import httpx

    tar_path = f"/tmp/{user_id}-profile.tar.gz"
    profile_dir = f"/tmp/linkedin-profiles/{user_id}/profile"

    # Clean up any existing profile
    if os.path.exists(f"/tmp/linkedin-profiles/{user_id}"):
        shutil.rmtree(f"/tmp/linkedin-profiles/{user_id}")

    logger.info(f"Downloading profile for {user_id}...")
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.get(
            f"{SUPABASE_URL}/storage/v1/object/{PROFILE_BUCKET}/{user_id}.tar.gz",
            headers={
                "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
                "apikey": SUPABASE_SERVICE_KEY,
            },
        )
        if resp.status_code == 404:
            logger.info(f"No profile found for {user_id}")
            return None
        if resp.status_code != 200:
            logger.error(f"Download failed: {resp.status_code}")
            return None

        with open(tar_path, "wb") as f:
            f.write(resp.content)

    # Extract
    os.makedirs(f"/tmp/linkedin-profiles/{user_id}", exist_ok=True)
    with tarfile.open(tar_path, "r:gz") as tar:
        tar.extractall(f"/tmp/linkedin-profiles/{user_id}")

    os.unlink(tar_path)
    logger.info(f"Profile extracted to {profile_dir}")
    return profile_dir


@asynccontextmanager
async def create_login_context(user_id: str) -> AsyncGenerator[tuple[Page, str], None]:
    """
    Create a persistent browser context for login.
    After login, call upload_profile() to save it.
    Returns (page, profile_dir).
    """
    profile_dir = f"/tmp/linkedin-profiles/{user_id}/profile"
    os.makedirs(profile_dir, exist_ok=True)

    pw = await async_playwright().start()

    try:
        context = await pw.chromium.launch_persistent_context(
            profile_dir,
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage"],
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            locale="en-US",
        )

        page = context.pages[0] if context.pages else await context.new_page()
        yield page, profile_dir

    finally:
        try:
            await context.close()
        except Exception:
            pass
        await pw.stop()


@asynccontextmanager
async def create_linkedin_context(user_id: str) -> AsyncGenerator[tuple[LinkedInExtractor, Page], None]:
    """
    Download user's profile from Supabase, launch browser with it, execute action.
    """
    profile_dir = await download_profile(user_id)
    if not profile_dir:
        raise Exception("No LinkedIn profile found. Please connect LinkedIn first.")

    pw = await async_playwright().start()

    try:
        context = await pw.chromium.launch_persistent_context(
            profile_dir,
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage"],
            viewport={"width": 1280, "height": 800},
            user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            locale="en-US",
        )

        page = context.pages[0] if context.pages else await context.new_page()

        # Validate session
        logger.info("Validating LinkedIn session...")
        await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=30000)

        current_url = page.url
        if any(x in current_url for x in ["/login", "/authwall", "/checkpoint"]):
            raise Exception("LinkedIn session expired. Please reconnect via Settings.")

        logger.info("LinkedIn session valid.")

        extractor = LinkedInExtractor(page)
        yield extractor, page

    finally:
        try:
            await context.close()
        except Exception:
            pass
        await pw.stop()
        # Clean up downloaded profile
        user_dir = f"/tmp/linkedin-profiles/{user_id}"
        if os.path.exists(user_dir):
            shutil.rmtree(user_dir, ignore_errors=True)
