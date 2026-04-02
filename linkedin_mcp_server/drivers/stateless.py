"""
Stateless browser context with cookie injection.
Creates a temporary browser context per request, injects LinkedIn cookies,
executes an action, and cleans up. No persistent state on the server.
"""

import asyncio
import logging
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncGenerator

from patchright.async_api import async_playwright, BrowserContext, Page

from linkedin_mcp_server.scraping.extractor import LinkedInExtractor

logger = logging.getLogger(__name__)

LINKEDIN_COOKIES = [
    {"name": "li_at", "domain": ".linkedin.com", "path": "/"},
    {"name": "JSESSIONID", "domain": ".linkedin.com", "path": "/"},
]

# Shared browser instance (reused across requests, only contexts are per-request)
_playwright = None
_browser = None
_browser_lock = asyncio.Lock()


async def _get_shared_browser():
    """Get or create a shared Chromium browser instance."""
    global _playwright, _browser
    async with _browser_lock:
        if _browser is None or not _browser.is_connected():
            logger.info("Launching shared Chromium browser...")
            _playwright = await async_playwright().start()
            _browser = await _playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-blink-features=AutomationControlled",
                    "--disable-dev-shm-usage",
                ],
            )
            logger.info("Shared browser ready.")
        return _browser


async def close_shared_browser():
    """Close the shared browser (call on shutdown)."""
    global _playwright, _browser
    if _browser:
        await _browser.close()
        _browser = None
    if _playwright:
        await _playwright.stop()
        _playwright = None


@asynccontextmanager
async def create_linkedin_context(
    li_at: str,
    jsessionid: str,
) -> AsyncGenerator[tuple[LinkedInExtractor, Page], None]:
    """
    Create a temporary browser context with LinkedIn cookies injected.

    Usage:
        async with create_linkedin_context(li_at, jsessionid) as (extractor, page):
            result = await extractor.send_message(...)

    The context is automatically cleaned up after the block exits.
    """
    browser = await _get_shared_browser()

    # Create isolated context (like incognito — cookies don't leak between users)
    # locale MUST be en-US — the connection detection parses English button text
    # ("Connect", "Follow", "Pending", "Accept")
    context: BrowserContext = await browser.new_context(
        viewport={"width": 1280, "height": 800},
        user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
        locale="en-US",
        timezone_id="Europe/Paris",
    )

    try:
        page = await context.new_page()

        # Step 1: Visit LinkedIn homepage to get base cookies (consent, lang, bcookie, etc.)
        logger.info("Loading LinkedIn base cookies...")
        await page.goto("https://www.linkedin.com/", wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(1)

        # Step 2: Inject auth cookies on top of the base cookies
        logger.info("Injecting auth cookies...")
        await context.add_cookies([
            {
                "name": "li_at",
                "value": li_at,
                "domain": ".linkedin.com",
                "path": "/",
                "httpOnly": True,
                "secure": True,
                "sameSite": "None",
            },
            {
                "name": "JSESSIONID",
                "value": jsessionid,
                "domain": ".linkedin.com",
                "path": "/",
                "httpOnly": False,
                "secure": True,
                "sameSite": "None",
            },
        ])

        # Step 3: Navigate to feed to verify auth
        logger.info("Validating LinkedIn session...")
        await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=30000)

        # Check if we're on a login page (session expired)
        current_url = page.url
        if any(x in current_url for x in ["/login", "/authwall", "/checkpoint"]):
            raise Exception("LinkedIn session expired. Recruiter needs to re-capture cookies.")

        logger.info("LinkedIn session valid.")

        extractor = LinkedInExtractor(page)
        yield extractor, page

    finally:
        await context.close()
        logger.info("Browser context closed.")
