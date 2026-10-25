import datetime as dt
import uuid
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field


class AnalyticsKpis(BaseModel):
    sales_volume: Decimal
    orders: int
    average_order_value: Decimal | None
    completion_rate: float | None = Field(description="0 to 1; null without closed orders.")
    dispute_rate: float | None = Field(description="0 to 1; null without orders.")
    average_dispatch_seconds: int | None


class SalesPoint(BaseModel):
    date: dt.date = Field(description="EAT day, or the Monday of the week at 90 days.")
    amount: Decimal
    orders: int


class OutcomeWeek(BaseModel):
    week_start: dt.date
    released: int
    refunded: int
    disputed: int


class DisputeCategoryCount(BaseModel):
    category: str
    count: int


class TopListingRow(BaseModel):
    listing_id: uuid.UUID
    title: str
    photo_url: str | None
    orders: int
    sales: Decimal
    completion_rate: float | None


class SellerAnalyticsResponse(BaseModel):
    range_days: int
    completed_sales: int = Field(description="All-time released orders; S12 unlocks at 5.")
    current: AnalyticsKpis
    previous: AnalyticsKpis = Field(description="The period of the same length just before.")
    bucket: Literal["day", "week"]
    sales_series: list[SalesPoint]
    outcomes: list[OutcomeWeek]
    dispute_categories: list[DisputeCategoryCount]
    top_listings: list[TopListingRow]
    server_time: dt.datetime
