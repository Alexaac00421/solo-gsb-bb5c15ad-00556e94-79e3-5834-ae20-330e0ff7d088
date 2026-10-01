"""Pydantic 请求 / 响应模型。"""
from typing import Optional

from pydantic import BaseModel, Field, model_validator

from . import config


# ---- 节点 / 航段 ----------------------------------------------------------------

class NodeIn(BaseModel):
    node_id: str = Field(min_length=1, description="节点编号，如 A")
    name: Optional[str] = None


class NodeOut(BaseModel):
    node_id: str
    name: Optional[str] = None


class SegmentIn(BaseModel):
    origin: str = Field(min_length=1)
    destination: str = Field(min_length=1)
    travel_time: int = Field(gt=0, description="航时（正整数分钟）")
    depth: float = Field(ge=0, description="静水深度（米）")
    clearance: Optional[float] = Field(
        default=None, ge=0, description="桥底净空（米），不填表示无桥"
    )


class SegmentOut(BaseModel):
    seg_id: int
    origin: str
    destination: str
    travel_time: int
    depth: float
    clearance: Optional[float] = None


# ---- 潮位 ----------------------------------------------------------------------

class TideCorrection(BaseModel):
    corrections: dict[int, float] = Field(
        description="要覆盖的 {分钟: 潮位(米)}；其余分钟继承所据修订"
    )
    base_revision: Optional[int] = Field(
        default=None, description="所基于的修订号；不填则取最新修订"
    )
    note: Optional[str] = None


class RevisionOut(BaseModel):
    revision: int
    note: Optional[str] = None
    created_at: str


# ---- 规划 / 确认 ----------------------------------------------------------------

class PlanRequest(BaseModel):
    origin: str
    destination: str
    start_minute: int = Field(ge=0, description="规划窗口起点（绝对分钟，>=0）")
    window_minutes: int = Field(gt=0, le=config.MAX_WINDOW, description="窗口长度，<=1440")
    draft: float = Field(ge=0, description="船舶吃水（米）")
    height: float = Field(ge=0, description="水面以上高度（米）")
    margin: float = Field(ge=0, description="安全余量（米）")
    revision: Optional[int] = Field(default=None, description="潮位修订号；不填用最新")


class LegOut(BaseModel):
    seg_id: int
    origin: str
    destination: str
    start_minute: int
    end_minute: int
    travel_time: int


class PlanResponse(BaseModel):
    revision: int
    origin: str
    destination: str
    start_minute: int
    window_minutes: int
    draft: float
    height: float
    margin: float
    arrival_minute: int
    total_wait_minutes: int
    itinerary: list[LegOut]


class ConfirmRequest(PlanRequest):
    itinerary: list[LegOut] = Field(description="按规划结果原样回填的行程（含 seg_id/起止分钟）")


class TripOut(BaseModel):
    trip_id: int
    revision: int
    origin: str
    destination: str
    start_minute: int
    window_minutes: int
    draft: float
    height: float
    margin: float
    arrival_minute: int
    itinerary: list[LegOut]
    confirmed_at: str
