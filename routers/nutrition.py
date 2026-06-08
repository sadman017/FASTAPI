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

        # Fallback: if food_description parsing yielded all zeros, try direct v2 fields
        if parsed["calories"] == 0 and parsed["protein"] == 0 and parsed["carbs"] == 0 and parsed["fat"] == 0:
            parsed["calories"] = float(food.get("calories", 0) or 0)
            parsed["protein"] = float(food.get("protein", 0) or 0)
            parsed["carbs"] = float(food.get("carbohydrate", 0) or 0)
            parsed["fat"] = float(food.get("fat", 0) or 0)
            if not parsed["serving_description"] or parsed["serving_description"] == "per serving":
                parsed["serving_description"] = food.get("serving_description") or "per serving"

        # Second fallback: try first serving if still zeros
        if parsed["calories"] == 0 and parsed["protein"] == 0 and parsed["carbs"] == 0 and parsed["fat"] == 0:
            servings_raw = food.get("servings", {}).get("serving", [])
            if servings_raw is None:
                servings_raw = []
            if isinstance(servings_raw, dict):
                servings_raw = [servings_raw]
            if servings_raw:
                first = servings_raw[0]
                parsed["calories"] = float(first.get("calories", 0) or 0)
                parsed["protein"] = float(first.get("protein", 0) or 0)
                parsed["carbs"] = float(first.get("carbohydrate", 0) or 0)
                parsed["fat"] = float(first.get("fat", 0) or 0)
                parsed["serving_description"] = first.get("serving_description") or "per serving"

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


# ---------------------------------------------------------------------------
# Helpers for new endpoints
# ---------------------------------------------------------------------------


def _format_gtin13(code: str) -> str:
    """Strip non-digits and zero-pad to GTIN-13 (13 digits)."""
    digits = re.sub(r"\D", "", code)
    return digits.zfill(13)


def _parse_fatsecret_food_detail(data: Dict[str, Any]) -> Dict[str, Any]:
    """Parse a FatSecret food.get or food.find_id_for_barcode.v2 response into unified shape."""
    food = data.get("food", {})
    if not food:
        return {}

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

    # Parse allergens if present (v2 barcode / food.get)
    allergens = []
    attrs = food.get("food_attributes", {})
    allergens_data = attrs.get("allergens", {}).get("allergen", []) if isinstance(attrs, dict) else []
    if allergens_data:
        if isinstance(allergens_data, dict):
            allergens_data = [allergens_data]
        for a in allergens_data:
            allergens.append({
                "id": str(a.get("id", "")),
                "name": a.get("name", ""),
                "value": a.get("value", ""),
            })

    first_serving = servings[0] if servings else {}
    return {
        "food_id": str(food.get("food_id", "")),
        "food_name": food.get("food_name") or "Unknown",
        "brand_name": food.get("brand_name", ""),
        "food_type": food.get("food_type", ""),
        "calories": float(first_serving.get("calories", 0) or 0),
        "protein": float(first_serving.get("protein", 0) or 0),
        "carbs": float(first_serving.get("carbs", 0) or 0),
        "fat": float(first_serving.get("fat", 0) or 0),
        "serving_description": first_serving.get("serving_description", "per serving"),
        "servings": servings,
        "allergens": allergens,
        "source": "fatsecret",
    }


# ---------------------------------------------------------------------------
# FatSecret barcode lookup endpoint
# ---------------------------------------------------------------------------


@router.get("/api/nutrition/barcode", tags=["Nutrition"])
async def lookup_barcode(code: str = Query(..., min_length=1, description="Barcode value (any format)")):
    """
    Lookup a food by barcode via FatSecret.
    Tries Premier v2 first, falls back to v1 + food.get.
    Returns unified food detail with servings and allergens.
    """
    gtin = _format_gtin13(code)
    logger.info("[FatSecret] Barcode lookup for raw='%s' gtin='%s'", code, gtin)

    try:
        token = await get_access_token()
    except Exception as exc:
        logger.error("FatSecret token fetch failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Unable to authenticate with FatSecret.",
        )

    async with httpx.AsyncClient(timeout=30.0) as client:
        # --- Try v2 first (Premier, returns full food details) ---
        try:
            resp = await client.get(
                FATSECRET_API_URL,
                headers={"Authorization": f"Bearer {token}"},
                params={
                    "method": "food.find_id_for_barcode.v2",
                    "barcode": gtin,
                    "format": "json",
                },
            )
            logger.info("[FatSecret] barcode v2 HTTP %s for gtin %s", resp.status_code, gtin)
            if resp.status_code >= 400:
                logger.error("[FatSecret] barcode v2 error body: %s", resp.text[:500])
            resp.raise_for_status()
            data = resp.json()

            if "error" not in data and data.get("food"):
                parsed = _parse_fatsecret_food_detail(data)
                if parsed:
                    logger.info("[FatSecret] Barcode v2 hit for gtin %s -> %s", gtin, parsed["food_name"])
                    return {"success": True, **parsed}
        except Exception as exc:
            logger.warning("[FatSecret] Barcode v2 failed for gtin %s: %s", gtin, exc)

        # --- Fallback to v1 (returns food_id only) ---
        try:
            resp = await client.get(
                FATSECRET_API_URL,
                headers={"Authorization": f"Bearer {token}"},
                params={
                    "method": "food.find_id_for_barcode",
                    "barcode": gtin,
                    "format": "json",
                },
            )
            logger.info("[FatSecret] barcode v1 HTTP %s for gtin %s", resp.status_code, gtin)
            if resp.status_code >= 400:
                logger.error("[FatSecret] barcode v1 error body: %s", resp.text[:500])
            resp.raise_for_status()
            data = resp.json()

            if "error" in data:
                logger.warning("[FatSecret] Barcode v1 error for gtin %s: %s", gtin, data.get("error"))
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Barcode {gtin} not found in FatSecret.",
                )

            food_id = data.get("food_id")
            if not food_id:
                raise HTTPException(
                    status_code=status.HTTP_404_NOT_FOUND,
                    detail=f"Barcode {gtin} not found in FatSecret.",
                )

            # Fetch full details via food.get
            detail_resp = await client.get(
                FATSECRET_API_URL,
                headers={"Authorization": f"Bearer {token}"},
                params={
                    "method": "food.get",
                    "food_id": str(food_id),
                    "format": "json",
                },
            )
            detail_resp.raise_for_status()
            detail_data = detail_resp.json()
            parsed = _parse_fatsecret_food_detail(detail_data)
            if parsed:
                logger.info("[FatSecret] Barcode v1 hit for gtin %s -> food_id %s -> %s", gtin, food_id, parsed["food_name"])
                return {"success": True, **parsed}
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("[FatSecret] Barcode v1 failed for gtin %s: %s", gtin, exc)

    raise HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Barcode {gtin} not found in FatSecret.",
    )


# ---------------------------------------------------------------------------
# FatSecret auto-complete endpoint
# ---------------------------------------------------------------------------


@router.get("/api/nutrition/autocomplete", tags=["Nutrition"])
async def autocomplete_foods(
    query: str = Query(..., min_length=1, description="Partial food name to get suggestions for")
):
    """
    Return auto-complete suggestions from FatSecret Premier.
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
                    "method": "foods.autocomplete.v2",
                    "expression": query,
                    "format": "json",
                },
            )
            logger.info("[FatSecret] autocomplete HTTP %s for '%s'", resp.status_code, query)
            if resp.status_code >= 400:
                logger.error("[FatSecret] autocomplete error body: %s", resp.text[:500])
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.error("FatSecret autocomplete failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="FatSecret autocomplete temporarily unavailable.",
        )

    if "error" in data:
        logger.warning("[FatSecret] autocomplete error: %s", data.get("error"))
        return {"success": True, "suggestions": []}

    suggestions_data = data.get("suggestions", {}).get("suggestion", [])
    if suggestions_data is None:
        suggestions_data = []
    if isinstance(suggestions_data, str):
        suggestions_data = [suggestions_data]

    return {"success": True, "suggestions": suggestions_data}


# ---------------------------------------------------------------------------
# FatSecret food categories endpoint
# ---------------------------------------------------------------------------


@router.get("/api/nutrition/categories", tags=["Nutrition"])
async def get_food_categories():
    """
    Return FatSecret food categories list.
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
                    "method": "food_categories.get",
                    "format": "json",
                },
            )
            logger.info("[FatSecret] categories HTTP %s", resp.status_code)
            if resp.status_code >= 400:
                logger.error("[FatSecret] categories error body: %s", resp.text[:500])
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.error("FatSecret categories failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="FatSecret categories temporarily unavailable.",
        )

    if "error" in data:
        logger.warning("[FatSecret] categories error: %s", data.get("error"))
        return {"success": True, "categories": []}

    categories_raw = data.get("categories", {}).get("category", [])
    if categories_raw is None:
        categories_raw = []
    if isinstance(categories_raw, dict):
        categories_raw = [categories_raw]

    categories = []
    for cat in categories_raw:
        categories.append({
            "id": str(cat.get("food_category_id", "")),
            "name": cat.get("food_category_name", ""),
        })

    return {"success": True, "categories": categories}
