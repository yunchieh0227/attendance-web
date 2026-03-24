from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
from zoneinfo import ZoneInfo
from datetime import datetime
import httpx
import json
import os
from pathlib import Path


load_dotenv()

LINE_CHANNEL_ID = os.getenv("LINE_CHANNEL_ID", "").strip()
ALLOW_ORIGIN = os.getenv("ALLOW_ORIGIN", "https://yunchieh0227.github.io").strip()
DATA_FILE = Path("clock_records.json")

app = FastAPI(title="Clock Backend", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[ALLOW_ORIGIN],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class ClockRequest(BaseModel):
    action: str               # clock_in / clock_out
    idToken: str
    lineUserId: str | None = None
    displayName: str | None = None
    frontendTime: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    accuracy: float | None = None


def load_records() -> list:
    if not DATA_FILE.exists():
        return []
    try:
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_records(records: list) -> None:
    DATA_FILE.write_text(
        json.dumps(records, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )


async def verify_line_id_token(id_token: str) -> dict:
    """
    向 LINE Verify ID token endpoint 驗證 idToken
    """
    if not LINE_CHANNEL_ID:
        raise RuntimeError("LINE_CHANNEL_ID 尚未設定")

    url = "https://api.line.me/oauth2/v2.1/verify"
    data = {
        "id_token": id_token,
        "client_id": LINE_CHANNEL_ID,
    }

    async with httpx.AsyncClient(timeout=15.0) as client:
        response = await client.post(url, data=data)

    if response.status_code != 200:
        raise HTTPException(status_code=401, detail="LINE idToken 驗證失敗")

    return response.json()


def action_to_text(action: str) -> str:
    if action == "clock_in":
        return "上班"
    if action == "clock_out":
        return "下班"
    return action


@app.api_route("/", methods=["GET", "HEAD"])
async def root():
    return {"message": "Clock backend is running"}


@app.post("/api/clock")
async def clock(payload: ClockRequest):
    if payload.action not in {"clock_in", "clock_out"}:
        raise HTTPException(status_code=400, detail="action 必須是 clock_in 或 clock_out")

    verified = await verify_line_id_token(payload.idToken)

    # 以 LINE 驗證後的資料為主
    line_user_id = verified.get("sub", "")
    display_name = payload.displayName or "未知使用者"

    # 後端正式時間：台灣時間
    now = datetime.now(ZoneInfo("Asia/Taipei"))
    server_time = now.strftime("%Y-%m-%d %H:%M:%S")

    location_text = "未提供定位"
    if payload.latitude is not None and payload.longitude is not None:
        if payload.accuracy is not None:
            location_text = (
                f"{payload.latitude:.6f}, {payload.longitude:.6f} "
                f"(±{round(payload.accuracy)}m)"
            )
        else:
            location_text = f"{payload.latitude:.6f}, {payload.longitude:.6f}"

    record = {
        "line_user_id": line_user_id,
        "display_name": display_name,
        "action": payload.action,
        "action_text": action_to_text(payload.action),
        "server_time": server_time,
        "frontend_time": payload.frontendTime,
        "latitude": payload.latitude,
        "longitude": payload.longitude,
        "accuracy": payload.accuracy,
        "location_text": location_text,
        "verified_payload": verified,
    }

    records = load_records()
    records.append(record)
    save_records(records)

    message = f"{display_name}打卡{action_to_text(payload.action)}成功"

    return {
        "message": message,
        "displayName": display_name,
        "type": payload.action,
        "time": server_time,
        "locationText": location_text,
        "lineUserId": line_user_id,
    }


@app.get("/api/records")
async def get_records():
    return {"records": load_records()}