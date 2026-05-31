from pydantic import BaseModel, ConfigDict
from datetime import date, datetime
from typing import Optional


class FoodLogCreate(BaseModel):
    user_id: str
    food_id: str
    food_name: str
    calories: float
    protein: float
    carbs: float
    fat: float
    quantity: float = 1.0
    meal_type: str
    log_date: date


class FoodLogResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: str
    food_id: str
    food_name: str
    calories: float
    protein: float
    carbs: float
    fat: float
    quantity: float
    meal_type: str
    log_date: date
    created_at: datetime


class MacroSummary(BaseModel):
    total_calories: float
    total_protein: float
    total_carbs: float
    total_fat: float


class MacroRatio(BaseModel):
    protein_pct: float
    carbs_pct: float
    fat_pct: float


class DailyLogResponse(BaseModel):
    log_date: date
    total_calories: float
    total_protein: float
    total_carbs: float
    total_fat: float
    entries: list[FoodLogResponse]


class WeeklyLogResponse(BaseModel):
    start_date: date
    end_date: date
    total_calories: float
    total_protein: float
    total_carbs: float
    total_fat: float
    macro_ratios: MacroRatio
    entries: list[FoodLogResponse]


class MonthlyLogResponse(BaseModel):
    month: str
    total_calories: float
    total_protein: float
    total_carbs: float
    total_fat: float
    macro_ratios: MacroRatio
    entries: list[FoodLogResponse]


class YearlyLogResponse(BaseModel):
    year: int
    total_calories: float
    total_protein: float
    total_carbs: float
    total_fat: float
    macro_ratios: MacroRatio
    entries: list[FoodLogResponse]
