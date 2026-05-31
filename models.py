from sqlalchemy import Column, Integer, String, Float, Date, DateTime, func
from database import Base


class FoodLog(Base):
    __tablename__ = "food_logs"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(String, index=True, nullable=False)
    food_id = Column(String, nullable=False)
    food_name = Column(String, nullable=False)
    calories = Column(Float, default=0.0)
    protein = Column(Float, default=0.0)
    carbs = Column(Float, default=0.0)
    fat = Column(Float, default=0.0)
    quantity = Column(Float, default=1.0)
    meal_type = Column(String, nullable=False)
    log_date = Column(Date, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
