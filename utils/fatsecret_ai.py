"""
FatSecret AI Image Recognition & NLP Proxy

Provides a unified interface to:
  • FatSecret Image Recognition API
  • FatSecret Natural Language Processing API

Reuses the existing OAuth 2.0 token manager.
Responses are normalized into a common schema so the Flutter app
never needs to know which FatSecret endpoint produced the data.
"""

import logging
from typing import List, Dict, Any, Optional

import httpx

from utils.fatsecret_auth import get_access_token

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Normalization helpers
# ---------------------------------------------------------------------------

def _normalize_ai_food(raw: Dict[str, Any], source: str) -> Dict[str, Any]:
    """Convert a raw FatSecret AI response item into the app's standard schema.

    Handles both the nested 'food_response' structure from AI endpoints
    and the flatter structure from other endpoints.
    """
    eaten = raw.get("eaten") or {}
    nutrition = eaten.get("total_nutritional_content") or {}
    serving = raw.get("suggested_serving") or {}

    logger.debug("[AI] normalize input keys: %s, eaten keys: %s, nutrition keys: %s",
                 list(raw.keys()), list(eaten.keys()), list(nutrition.keys()))

    result = {
        "food_id": str(raw.get("food_id", "")),
        "food_name": raw.get("food_entry_name") or raw.get("food_name") or "Unknown Food",
        "calories": float(nutrition.get("calories", 0) or raw.get("calories", 0) or 0),
        "protein": float(nutrition.get("protein", 0) or raw.get("protein", 0) or 0),
        "carbs": float(nutrition.get("carbohydrate", 0) or raw.get("carbs", 0) or 0),
        "fat": float(nutrition.get("fat", 0) or raw.get("fat", 0) or 0),
        "serving_description": serving.get("serving_description") or raw.get("serving_description") or "per serving",
        "confidence": float(raw.get("confidence", 0) or 0),
        "source": source,
    }
    logger.debug("[AI] normalize output: %s", result)
    return result


# ---------------------------------------------------------------------------
# Image Recognition
# ---------------------------------------------------------------------------

async def image_recognize(
    base64_image: str,
    eaten_foods: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """
    Send a Base64-encoded image to FatSecret Image Recognition.
    """
    logger.info("[FatSecret AI] Image recognize called (eaten_foods=%s)", bool(eaten_foods))

    try:
        token = await get_access_token()
    except Exception as exc:
        logger.error("[FatSecret AI] Token fetch failed: %s", exc)
        raise httpx.HTTPStatusError(
            "Unable to authenticate with FatSecret.",
            request=None,
            response=httpx.Response(status_code=401),
        )

    payload: Dict[str, Any] = {
        "image_b64": base64_image,
        "include_food_data": True,
    }
    if eaten_foods:
        payload["eaten_foods"] = eaten_foods

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                "https://platform.fatsecret.com/rest/image/recognition/v1",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            logger.info("[FatSecret AI] image/recognition HTTP %s", resp.status_code)
            if resp.status_code >= 400:
                logger.error("[FatSecret AI] image/recognition error: %s", resp.text[:500])
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        logger.error("[FatSecret AI] image/recognize HTTP error: %s", exc)
        raise
    except Exception as exc:
        logger.error("[FatSecret AI] image/recognize failed: %s", exc)
        raise httpx.HTTPStatusError(
            "AI image recognition request failed.",
            request=None,
            response=httpx.Response(status_code=503),
        )

    foods_raw = data.get("food_response") or data.get("foods") or data.get("results") or []
    if isinstance(foods_raw, dict):
        foods_raw = [foods_raw]
    return [_normalize_ai_food(item, "ai_image") for item in foods_raw]


# ---------------------------------------------------------------------------
# NLP Text Parsing
# ---------------------------------------------------------------------------

async def nlp_parse(
    text: str,
    eaten_foods: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """
    Send unstructured meal text to FatSecret NLP.
    """
    logger.info("[FatSecret AI] NLP parse called for text: %.40s...", text)

    try:
        token = await get_access_token()
    except Exception as exc:
        logger.error("[FatSecret AI] Token fetch failed: %s", exc)
        raise httpx.HTTPStatusError(
            "Unable to authenticate with FatSecret.",
            request=None,
            response=httpx.Response(status_code=401),
        )

    payload: Dict[str, Any] = {
        "user_input": text,
        "include_food_data": True,
    }
    if eaten_foods:
        payload["eaten_foods"] = eaten_foods

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                "https://platform.fatsecret.com/rest/natural-language-processing/v1",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
            logger.info("[FatSecret AI] nlp HTTP %s", resp.status_code)
            if resp.status_code >= 400:
                logger.error("[FatSecret AI] nlp error: %s", resp.text[:500])
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        logger.error("[FatSecret AI] nlp HTTP error: %s", exc)
        raise
    except Exception as exc:
        logger.error("[FatSecret AI] nlp failed: %s", exc)
        raise httpx.HTTPStatusError(
            "AI text parsing request failed.",
            request=None,
            response=httpx.Response(status_code=503),
        )

    foods_raw = data.get("food_response") or data.get("foods") or data.get("results") or []
    if isinstance(foods_raw, dict):
        foods_raw = [foods_raw]
    return [_normalize_ai_food(item, "ai_nlp") for item in foods_raw]
