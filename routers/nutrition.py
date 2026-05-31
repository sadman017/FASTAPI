"""
FatSecret REST API Router

Exposes:
  GET /api/nutrition/search?query=...
"""

import logging
from typing import List, Dict, Any

import httpx
from fastapi import APIRouter, Query, HTTPException, status

from utils.fatsecret_auth import get_access_token

logger = logging.getLogger(__name__)
router = APIRouter()

FATSECRET_REST_URL = "https://platform.fatsecret.com/rest/server.api"


async def _call_fatsecret_search(query: str) -> Dict[str, Any]:
    token = await get_access_token()
    async with httpx.AsyncClient(timeout=60.0) as client:
        resp = await client.get(
            FATSECRET_REST_URL,
            params={
                "method": "foods.search.v2",
                "search_expression": query,
                "format": "json",
                "max_results": 20,
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        resp.raise_for_status()
        return resp.json()


def _extract_macros(food: Dict[str, Any]) -> Dict[str, Any]:
    """Pull the first serving's macros from a FatSecret food object."""
    servings = food.get("servings", {}).get("serving", [])
    if not isinstance(servings, list):
        servings = [servings]
    if not servings:
        return {
            "calories": 0.0,
            "protein": 0.0,
            "carbs": 0.0,
            "fat": 0.0,
        }
    s = servings[0]
    return {
        "calories": float(s.get("calories", 0) or 0),
        "protein": float(s.get("protein", 0) or 0),
        "carbs": float(s.get("carbohydrate", 0) or 0),
        "fat": float(s.get("fat", 0) or 0),
    }


@router.get("/api/nutrition/search", tags=["Nutrition"])
async def search_foods(
    query: str = Query(..., min_length=1, description="Food name to search")
):
    """
    Search FatSecret for foods matching the query.
    Returns a clean list with food_id, food_name, calories, protein, carbs, fat.
    """
    try:
        data = await _call_fatsecret_search(query)
    except httpx.HTTPStatusError as exc:
        logger.error("FatSecret HTTP error: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="FatSecret API returned an error. Please retry later.",
        )
    except Exception as exc:
        logger.error("FatSecret search failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to query FatSecret.",
        )

    results: List[Dict[str, Any]] = []
    foods = data.get("foods", {}).get("food", [])
    if not isinstance(foods, list):
        foods = [foods]

    for food in foods:
        macros = _extract_macros(food)
        results.append({
            "food_id": food.get("food_id"),
            "food_name": food.get("food_name"),
            **macros,
        })

    return {"success": True, "count": len(results), "results": results}
