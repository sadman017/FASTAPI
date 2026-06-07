"""
AI Food Recognition Router

Endpoints:
  POST /api/ai/recognize-image  -> Accepts a photo, returns detected foods
  POST /api/ai/parse-text       -> Accepts natural language, returns parsed foods

Both endpoints normalize responses through utils.fatsecret_ai so the Flutter
app sees a single consistent schema regardless of which AI model produced the
data.
"""

import logging
import base64
from io import BytesIO
from typing import List, Dict, Any, Optional

from fastapi import APIRouter, HTTPException, status, UploadFile, File, Form
from pydantic import BaseModel
from PIL import Image

from utils.fatsecret_ai import image_recognize, nlp_parse

logger = logging.getLogger(__name__)
router = APIRouter()

# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------

class AiDetectedFood(BaseModel):
    food_id: str
    food_name: str
    calories: float
    protein: float
    carbs: float
    fat: float
    serving_description: str
    confidence: float
    source: str


class ParseTextRequest(BaseModel):
    text: str
    eaten_foods: Optional[List[Dict[str, Any]]] = None


class AiFoodsResponse(BaseModel):
    success: bool
    foods: List[AiDetectedFood]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _compress_image(upload: UploadFile, max_size: int = 512) -> str:
    """
    Read an uploaded image, resize so the longest edge is <= max_size,
    compress to JPEG quality 85, and return a Base64 string.
    Ensures final payload stays well under FatSecret's ~1 MB limit.
    """
    try:
        contents = await upload.read()
        img = Image.open(BytesIO(contents))

        # Convert to RGB if necessary (e.g. PNG with transparency)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")

        # Resize preserving aspect ratio
        img.thumbnail((max_size, max_size), Image.Resampling.LANCZOS)

        # Encode to JPEG
        buffer = BytesIO()
        img.save(buffer, format="JPEG", quality=85, optimize=True)
        b64 = base64.b64encode(buffer.getvalue()).decode("utf-8")

        logger.info(
            "[AI] Image compressed: original=%s, resized=%s, base64_len=%s",
            upload.content_type,
            img.size,
            len(b64),
        )
        return b64
    except Exception as exc:
        logger.error("[AI] Image compression failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid image file. Please upload a valid photo.",
        )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.post(
    "/api/ai/recognize-image",
    response_model=AiFoodsResponse,
    tags=["AI"],
    summary="Recognize foods from a photo",
)
async def recognize_image(
    image: UploadFile = File(..., description="Food photo (JPEG/PNG, max 1 MB recommended)"),
    eaten_foods: Optional[str] = Form(None, description="JSON array of previously eaten foods for context"),
):
    """
    Upload a photo of a meal and receive a list of detected foods with
    estimated portions and macronutrients.

    The image is compressed to 512 px on the longest edge before being
    forwarded to the AI provider.
    """
    # Validate content type loosely
    if image.content_type and not image.content_type.startswith(("image/", "application/octet")):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file is not an image.",
        )

    base64_img = await _compress_image(image)

    parsed_eaten: Optional[List[Dict[str, Any]]] = None
    if eaten_foods:
        import json
        try:
            parsed_eaten = json.loads(eaten_foods)
        except json.JSONDecodeError:
            logger.warning("[AI] Invalid eaten_foods JSON, ignoring context.")

    try:
        foods = await image_recognize(base64_img, eaten_foods=parsed_eaten)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("[AI] image_recognize failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI image recognition is temporarily unavailable. Please try again later.",
        )

    return {"success": True, "foods": foods}


@router.post(
    "/api/ai/parse-text",
    response_model=AiFoodsResponse,
    tags=["AI"],
    summary="Parse natural language meal description",
)
async def parse_text(payload: ParseTextRequest):
    """
    Send a free-form meal description (e.g. "two slices of pepperoni pizza
    and a diet coke") and receive structured food items with macros.

    Optionally pass `eaten_foods` (a list of `{food_id, food_name}` objects)
    to improve recognition accuracy via memory context.
    """
    if not payload.text or not payload.text.strip():
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Text description is required.",
        )

    try:
        # FatSecret NLP enforces a 1000-character hard limit on user_input
        truncated_text = payload.text.strip()
        if len(truncated_text) > 1000:
            truncated_text = truncated_text[:1000]
            logger.warning("[AI] NLP text truncated from %d to 1000 chars", len(payload.text.strip()))
        foods = await nlp_parse(truncated_text, eaten_foods=payload.eaten_foods)
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("[AI] nlp_parse failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="AI text parsing is temporarily unavailable. Please try again later.",
        )

    return {"success": True, "foods": foods}
