"""
Unified Food Search Router

Exposes:
  GET /api/nutrition/search?query=...   -> Searches FatSecret + OpenFoodFacts in parallel
  GET /api/nutrition/fatsecret/{food_id} -> FatSecret food detail with serving sizes

Returns clean macros + source attribution. FatSecret provides portion-based data
(e.g. "1 cup rice"); OpenFoodFacts provides packaged product data (per 100g).
"""

import asyncio
import logging
import re
from typing import List, Dict, Any

import httpx
from fastapi import APIRouter, Query, HTTPException, status

from utils.fatsecret_auth import get_access_token

logger = logging.getLogger(__name__)
router = APIRouter()

FATSECRET_API_URL = "https://platform.fatsecret.com/rest/server.api"
OFF_SEARCH_URL = "https://world.openfoodfacts.org/cgi/search.pl"

# ---------------------------------------------------------------------------
# FatSecret helpers
# ---------------------------------------------------------------------------


def _parse_fatsecret_description(description: str) -> Dict[str, Any]:
    """
    Parse FatSecret food_description into structured macros.
    Example input:
      "Per 1 cup - Calories: 242kcal | Fat: 0.40g | Carbs: 53.20g | Protein: 4.40g"
    Returns:
      {
        "serving_description": "1 cup",
        "calories": 242.0,
        "fat": 0.40,
        "carbs": 53.20,
        "protein": 4.40,
      }
    """
    result = {
        "serving_description": "per serving",
        "calories": 0.0,
        "fat": 0.0,
        "carbs": 0.0,
        "protein": 0.0,
    }
    if not description:
        return result

    # Extract serving description from "Per X - ..."
    serving_match = re.search(r"Per\s+(.+?)\s*[-–—]", description)
    if serving_match:
        result["serving_description"] = serving_match.group(1).strip()

    # Extract numeric values
    cal_match = re.search(r"Calories:\s*([0-9.]+)", description)
    if cal_match:
        result["calories"] = float(cal_match.group(1))

    fat_match = re.search(r"Fat:\s*([0-9.]+)", description)
    if fat_match:
        result["fat"] = float(fat_match.group(1))

    carb_match = re.search(r"Carbs:\s*([0-9.]+)", description)
    if carb_match:
        result["carbs"] = float(carb_match.group(1))

    protein_match = re.search(r"Protein:\s*([0-9.]+)", description)
    if protein_match:
        result["protein"] = float(protein_match.group(1))

    return result


def _extract_fatsecret_foods(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Extract food list from either v1 or v2 FatSecret response."""
    # Try v2 structure first
    foods_data = data.get("foods_search", {}).get("results", {}).get("food", [])
    if foods_data is None:
        foods_data = []
    if isinstance(foods_data, dict):
        foods_data = [foods_data]
    if foods_data:
        return foods_data

    # Fall back to v1 structure
    foods_data = data.get("foods", {}).get("food", [])
    if foods_data is None:
        foods_data = []
    if isinstance(foods_data, dict):
        foods_data = [foods_data]
    return foods_data


async def _call_fatsecret_search(
    client: httpx.AsyncClient,
    token: str,
    query: str,
    method: str,
) -> Dict[str, Any]:
    """Call FatSecret search and return parsed JSON."""
    resp = await client.get(
        FATSECRET_API_URL,
        headers={"Authorization": f"Bearer {token}"},
        params={
            "method": method,
            "search_expression": query,
            "format": "json",
            "max_results": 10,
        },
    )
    logger.info("[FatSecret] %s HTTP %s for query '%s'", method, resp.status_code, query)
    if resp.status_code >= 400:
        logger.error("[FatSecret] %s error body: %s", method, resp.text[:500])
    resp.raise_for_status()
    return resp.json()


async def _search_fatsecret(query: str) -> List[Dict[str, Any]]:
    """Search FatSecret. Tries Premier v2 first, falls back to v1."""
    try:
        token = await get_access_token()
    except Exception as exc:
        logger.error("FatSecret token fetch failed: %s", exc)
        return []

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            # Try Premier v2 first
            data = await _call_fatsecret_search(client, token, query, "foods.search.v2")

            # Check if v2 returned an error (e.g., premier scope missing)
            if "error" in data:
                logger.warning(
                    "[FatSecret] v2 error code %s: %s. Falling back to v1.",
                    data["error"].get("code"),
                    data["error"].get("message"),
                )
                data = await _call_fatsecret_search(client, token, query, "foods.search")
    except httpx.HTTPStatusError as exc:
        logger.error("FatSecret HTTP error: %s", exc)
        return []
    except Exception as exc:
        logger.error("FatSecret search failed: %s", exc)
        return []

    results: List[Dict[str, Any]] = []
    foods_data = _extract_fatsecret_foods(data)

    for food in foods_data:
        parsed = _parse_fatsecret_description(food.get("food_description", ""))
        results.append({
            "food_id": str(food.get("food_id", "")),
            "food_name": food.get("food_name") or "Unknown Food",
            "calories": parsed["calories"],
            "protein": parsed["protein"],
            "carbs": parsed["carbs"],
            "fat": parsed["fat"],
            "serving_description": parsed["serving_description"],
            "source": "fatsecret",
            "ingredients_text": "",
            "additives_tags": [],
        })

    logger.info("[FatSecret] Found %d results for '%s'", len(results), query)
    return results


# ---------------------------------------------------------------------------
# OpenFoodFacts helpers
# ---------------------------------------------------------------------------


async def _search_openfoodfacts(query: str) -> List[Dict[str, Any]]:
    """Search OpenFoodFacts and return structured results."""
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                OFF_SEARCH_URL,
                headers={"User-Agent": "SmartBiteApp/1.0 (contact@smartbite.app)"},
                params={
                    "search_terms": query,
                    "search_simple": 1,
                    "action": "process",
                    "json": 1,
                    "page_size": 10,
                    "fields": "code,product_name,nutriments,ingredients_text,additives_tags,allergens_tags",
                },
            )
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        logger.error("OpenFoodFacts HTTP error: %s", exc)
        return []
    except Exception as exc:
        logger.error("OpenFoodFacts search failed: %s", exc)
        return []

    results: List[Dict[str, Any]] = []
    products = data.get("products", [])

    for product in products:
        nutriments = product.get("nutriments", {})
        results.append({
            "food_id": product.get("code", ""),
            "food_name": product.get("product_name") or product.get("product_name_en") or "Unknown Product",
            "calories": float(nutriments.get("energy-kcal_100g", nutriments.get("energy-kcal", 0)) or 0),
            "protein": float(nutriments.get("proteins_100g", nutriments.get("proteins", 0)) or 0),
            "carbs": float(nutriments.get("carbohydrates_100g", nutriments.get("carbohydrates", 0)) or 0),
            "fat": float(nutriments.get("fat_100g", nutriments.get("fat", 0)) or 0),
            "serving_description": "per 100g",
            "source": "openfoodfacts",
            "ingredients_text": product.get("ingredients_text", ""),
            "additives_tags": product.get("additives_tags", []),
            "allergens_tags": product.get("allergens_tags", []),
        })

    logger.info("[OpenFoodFacts] Found %d results for '%s'", len(results), query)
    return results


# ---------------------------------------------------------------------------
# Unified search endpoint
# ---------------------------------------------------------------------------


@router.get("/api/nutrition/search", tags=["Nutrition"])
async def search_foods(
    query: str = Query(..., min_length=1, description="Food name to search")
):
    """
    Search both FatSecret and OpenFoodFacts in parallel.
    Returns a unified list of foods with source attribution.
    """
    fatsecret_task = _search_fatsecret(query)
    off_task = _search_openfoodfacts(query)

    fatsecret_results, off_results = await asyncio.gather(
        fatsecret_task, off_task, return_exceptions=True
    )

    # Handle exceptions
    if isinstance(fatsecret_results, Exception):
        logger.error("FatSecret search exception: %s", fatsecret_results)
        fatsecret_results = []
    if isinstance(off_results, Exception):
        logger.error("OpenFoodFacts search exception: %s", off_results)
        off_results = []

    # If both failed, return 503
    if not fatsecret_results and not off_results:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Both FatSecret and OpenFoodFacts are temporarily unreachable. Please retry later.",
        )

    merged = fatsecret_results + off_results
    merged = merged[:20]  # cap total results

    return {"success": True, "count": len(merged), "results": merged}


# ---------------------------------------------------------------------------
# FatSecret food detail endpoint
# ---------------------------------------------------------------------------


@router.get("/api/nutrition/fatsecret/{food_id}", tags=["Nutrition"])
async def get_fatsecret_food(food_id: str):
    """
    Get detailed FatSecret food info including available serving sizes.
    Returns: food_id, food_name, servings[] with macros per serving.
    """
    try:
        token = await get_access_token()
    except Exception as exc:
        logger.error("FatSecret token fetch failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to authenticate with FatSecret.",
        )

    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(
                FATSECRET_API_URL,
                headers={"Authorization": f"Bearer {token}"},
                params={
                    "method": "food.get",
                    "food_id": food_id,
                    "format": "json",
                },
            )
            logger.info("[FatSecret] detail HTTP %s for food_id %s", resp.status_code, food_id)
            if resp.status_code >= 400:
                logger.error("[FatSecret] detail error body: %s", resp.text[:500])
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPStatusError as exc:
        logger.error("FatSecret detail HTTP error: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="FatSecret is temporarily unreachable.",
        )
    except Exception as exc:
        logger.error("FatSecret detail fetch failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to fetch FatSecret food details.",
        )

    food = data.get("food", {})
    if not food:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Food ID {food_id} not found in FatSecret.",
        )

    # Parse servings
    servings_raw = food.get("servings", {}).get("serving", [])
    if servings_raw is None:
        servings_raw = []
    if isinstance(servings_raw, dict):
        servings_raw = [servings_raw]

    servings = []
    for s in servings_raw:
        servings.append({
            "serving_id": str(s.get("serving_id", "")),
            "serving_description": s.get("serving_description", "serving"),
            "calories": float(s.get("calories", 0) or 0),
            "protein": float(s.get("protein", 0) or 0),
            "carbs": float(s.get("carbohydrate", 0) or 0),
            "fat": float(s.get("fat", 0) or 0),
        })

    return {
        "success": True,
        "food_id": str(food.get("food_id", "")),
        "food_name": food.get("food_name") or "Unknown",
        "servings": servings,
    }
