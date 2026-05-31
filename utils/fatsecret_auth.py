"""
FatSecret OAuth 2.0 Token Manager

Handles client-credentials flow and caches the access token in memory
for its full lifetime (24h) to avoid requesting a new token on every call.
"""

import os
import logging
from datetime import datetime, timedelta

import httpx
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)

FATSECRET_CLIENT_ID = os.getenv("FATSECRET_CLIENT_ID")
FATSECRET_CLIENT_SECRET = os.getenv("FATSECRET_CLIENT_SECRET")
TOKEN_URL = "https://oauth.fatsecret.com/connect/token"

_token_cache = {
    "access_token": None,
    "expires_at": None,
}


async def get_access_token() -> str:
    """Return a valid access token, refreshing from FatSecret only when expired."""
    global _token_cache
    now = datetime.utcnow()

    # Return cached token if still valid (with 60-second buffer)
    if (
        _token_cache["access_token"]
        and _token_cache["expires_at"]
        and _token_cache["expires_at"] > now + timedelta(seconds=60)
    ):
        return _token_cache["access_token"]

    logger.info("[FatSecret] Fetching new OAuth token...")
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "client_credentials",
                "scope": "basic",
            },
            auth=(FATSECRET_CLIENT_ID, FATSECRET_CLIENT_SECRET),
        )
        resp.raise_for_status()
        data = resp.json()

    _token_cache["access_token"] = data["access_token"]
    expires_in = data.get("expires_in", 86400)
    _token_cache["expires_at"] = now + timedelta(seconds=expires_in)
    logger.info("[FatSecret] Token cached, expires in %s seconds", expires_in)
    return _token_cache["access_token"]
