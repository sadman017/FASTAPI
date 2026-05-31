"""
Food Log Router

Endpoints:
  POST /api/food-log
  GET  /api/food-log/daily
  GET  /api/food-log/weekly
  GET  /api/food-log/monthly
  GET  /api/food-log/yearly
  DELETE /api/food-log/{log_id}
"""

import logging
from datetime import date, timedelta
from calendar import monthrange
from typing import List

from fastapi import APIRouter, Depends, Query, HTTPException, status
from sqlalchemy.orm import Session

from database import get_db
from models import FoodLog
from schemas import (
    FoodLogCreate,
    FoodLogResponse,
    DailyLogResponse,
    WeeklyLogResponse,
    MonthlyLogResponse,
    YearlyLogResponse,
    MacroRatio,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def _summarize_logs(logs: List[FoodLog]) -> dict:
    total_calories = sum(l.calories * l.quantity for l in logs)
    total_protein = sum(l.protein * l.quantity for l in logs)
    total_carbs = sum(l.carbs * l.quantity for l in logs)
    total_fat = sum(l.fat * l.quantity for l in logs)
    return {
        "total_calories": round(total_calories, 2),
        "total_protein": round(total_protein, 2),
        "total_carbs": round(total_carbs, 2),
        "total_fat": round(total_fat, 2),
    }


def _calculate_ratios(summary: dict) -> MacroRatio:
    total_cals = summary["total_calories"]
    if total_cals == 0:
        return MacroRatio(protein_pct=0.0, carbs_pct=0.0, fat_pct=0.0)
    protein_cals = summary["total_protein"] * 4
    carbs_cals = summary["total_carbs"] * 4
    fat_cals = summary["total_fat"] * 9
    return MacroRatio(
        protein_pct=round(protein_cals / total_cals * 100, 1),
        carbs_pct=round(carbs_cals / total_cals * 100, 1),
        fat_pct=round(fat_cals / total_cals * 100, 1),
    )


@router.post("/api/food-log", response_model=FoodLogResponse, tags=["Food Log"])
async def create_food_log(log: FoodLogCreate, db: Session = Depends(get_db)):
    """Log a food item for a user."""
    db_log = FoodLog(**log.model_dump())
    db.add(db_log)
    db.commit()
    db.refresh(db_log)
    return db_log


@router.get("/api/food-log/daily", response_model=DailyLogResponse, tags=["Food Log"])
async def get_daily_log(
    user_id: str = Query(..., description="Firebase user UID"),
    log_date: date = Query(..., description="Date in YYYY-MM-DD"),
    db: Session = Depends(get_db),
):
    """Get all food logs for a specific day."""
    logs = (
        db.query(FoodLog)
        .filter(FoodLog.user_id == user_id, FoodLog.log_date == log_date)
        .all()
    )
    summary = _summarize_logs(logs)
    return DailyLogResponse(log_date=log_date, entries=logs, **summary)


@router.get("/api/food-log/weekly", response_model=WeeklyLogResponse, tags=["Food Log"])
async def get_weekly_log(
    user_id: str = Query(...),
    start_date: date = Query(..., description="Week start date (Monday)"),
    db: Session = Depends(get_db),
):
    """Get food logs for a 7-day week."""
    end_date = start_date + timedelta(days=6)
    logs = (
        db.query(FoodLog)
        .filter(
            FoodLog.user_id == user_id,
            FoodLog.log_date >= start_date,
            FoodLog.log_date <= end_date,
        )
        .all()
    )
    summary = _summarize_logs(logs)
    ratios = _calculate_ratios(summary)
    return WeeklyLogResponse(
        start_date=start_date,
        end_date=end_date,
        entries=logs,
        macro_ratios=ratios,
        **summary,
    )


@router.get("/api/food-log/monthly", response_model=MonthlyLogResponse, tags=["Food Log"])
async def get_monthly_log(
    user_id: str = Query(...),
    year: int = Query(...),
    month: int = Query(..., ge=1, le=12),
    db: Session = Depends(get_db),
):
    """Get food logs for a full month."""
    _, last_day = monthrange(year, month)
    start = date(year, month, 1)
    end = date(year, month, last_day)
    logs = (
        db.query(FoodLog)
        .filter(
            FoodLog.user_id == user_id,
            FoodLog.log_date >= start,
            FoodLog.log_date <= end,
        )
        .all()
    )
    summary = _summarize_logs(logs)
    ratios = _calculate_ratios(summary)
    return MonthlyLogResponse(
        month=f"{year}-{month:02d}",
        entries=logs,
        macro_ratios=ratios,
        **summary,
    )


@router.get("/api/food-log/yearly", response_model=YearlyLogResponse, tags=["Food Log"])
async def get_yearly_log(
    user_id: str = Query(...),
    year: int = Query(...),
    db: Session = Depends(get_db),
):
    """Get food logs for a full year."""
    start = date(year, 1, 1)
    end = date(year, 12, 31)
    logs = (
        db.query(FoodLog)
        .filter(
            FoodLog.user_id == user_id,
            FoodLog.log_date >= start,
            FoodLog.log_date <= end,
        )
        .all()
    )
    summary = _summarize_logs(logs)
    ratios = _calculate_ratios(summary)
    return YearlyLogResponse(
        year=year,
        entries=logs,
        macro_ratios=ratios,
        **summary,
    )


@router.delete("/api/food-log/{log_id}", tags=["Food Log"])
async def delete_food_log(log_id: int, db: Session = Depends(get_db)):
    """Remove a logged food entry."""
    log = db.query(FoodLog).filter(FoodLog.id == log_id).first()
    if not log:
        raise HTTPException(status_code=404, detail="Log not found")
    db.delete(log)
    db.commit()
    return {"success": True, "detail": f"Deleted log {log_id}"}
