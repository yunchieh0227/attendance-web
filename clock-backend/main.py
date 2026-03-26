from fastapi import FastAPI, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from dotenv import load_dotenv
from zoneinfo import ZoneInfo
from datetime import datetime, date
from typing import Optional
import httpx
import os
import asyncpg

load_dotenv()

LINE_CHANNEL_ID = os.getenv("LINE_CHANNEL_ID", "").strip()
ALLOW_ORIGIN    = os.getenv("ALLOW_ORIGIN", "https://yunchieh0227.github.io").strip()
DATABASE_URL    = os.getenv("DATABASE_URL", "").strip()
ADMIN_SECRET    = os.getenv("ADMIN_SECRET", "").strip()   # 管理員 API 驗證用，自訂一組字串

app = FastAPI(title="Clock Backend", version="0.2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[ALLOW_ORIGIN],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── 資料庫連線池 ─────────────────────────────────────────────
pool: asyncpg.Pool | None = None

@app.on_event("startup")
async def startup():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5)

@app.on_event("shutdown")
async def shutdown():
    if pool:
        await pool.close()

async def get_db() -> asyncpg.Connection:
    async with pool.acquire() as conn:
        yield conn

# ─── Pydantic Models ──────────────────────────────────────────

class ClockRequest(BaseModel):
    action: str
    idToken: str
    lineUserId: Optional[str] = None
    displayName: Optional[str] = None
    frontendTime: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    accuracy: Optional[float] = None

class EmployeeUpdate(BaseModel):
    daily_rate: Optional[int] = None
    overtime_rate: Optional[int] = None
    labor_insurance: Optional[int] = None
    health_insurance: Optional[int] = None
    is_active: Optional[bool] = None

class WorkDayUpdate(BaseModel):
    day_value: Optional[float] = None   # 1.0 / 0.5 / None(取消)
    note: Optional[str] = None

class OvertimeCreate(BaseModel):
    employee_id: int
    work_date: date
    hours: float
    note: Optional[str] = None

class LoanCreate(BaseModel):
    employee_id: int
    amount: int
    loan_date: Optional[date] = None
    note: Optional[str] = None

class SalaryPeriodCreate(BaseModel):
    employee_id: int
    period_label: str       # e.g. "2025-03"
    period_start: date
    period_end: date
    settlement_date: Optional[date] = None
    loan_deduction: int = 0
    expenses: int = 0
    note: Optional[str] = None

class SalaryPeriodConfirm(BaseModel):
    status: str             # "confirmed" or "draft"

# ─── 工具函式 ─────────────────────────────────────────────────

def check_admin(secret: str):
    """簡單的管理員驗證，Header 帶 X-Admin-Secret"""
    if not ADMIN_SECRET or secret != ADMIN_SECRET:
        raise HTTPException(status_code=403, detail="管理員權限不足")

def action_to_text(action: str) -> str:
    return "上班" if action == "clock_in" else "下班"

async def verify_line_id_token(id_token: str) -> dict:
    if not LINE_CHANNEL_ID:
        raise RuntimeError("LINE_CHANNEL_ID 尚未設定")
    async with httpx.AsyncClient(timeout=15.0) as client:
        resp = await client.post(
            "https://api.line.me/oauth2/v2.1/verify",
            data={"id_token": id_token, "client_id": LINE_CHANNEL_ID},
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=401, detail="LINE idToken 驗證失敗")
    return resp.json()

async def get_or_create_employee(conn, line_user_id: str, display_name: str) -> int:
    """用 line_user_id 找員工，找不到就自動建立（日薪預設 0，待管理員設定）"""
    row = await conn.fetchrow(
        "SELECT id FROM employees WHERE line_user_id = $1", line_user_id
    )
    if row:
        # 順便更新 display_name（LINE 名稱可能改過）
        await conn.execute(
            "UPDATE employees SET display_name = $1 WHERE line_user_id = $2",
            display_name, line_user_id
        )
        return row["id"]
    else:
        row = await conn.fetchrow(
            """INSERT INTO employees (line_user_id, display_name)
               VALUES ($1, $2) RETURNING id""",
            line_user_id, display_name
        )
        return row["id"]

# ─── 健康檢查 ─────────────────────────────────────────────────

@app.api_route("/", methods=["GET", "HEAD"])
async def root():
    return {"message": "Clock backend v0.2 is running"}

# ─── 打卡 API ─────────────────────────────────────────────────

@app.post("/api/clock")
async def clock(payload: ClockRequest, conn=Depends(get_db)):
    if payload.action not in {"clock_in", "clock_out"}:
        raise HTTPException(status_code=400, detail="action 必須是 clock_in 或 clock_out")

    verified    = await verify_line_id_token(payload.idToken)
    line_user_id = verified.get("sub", "")
    display_name = payload.displayName or "未知使用者"

    now         = datetime.now(ZoneInfo("Asia/Taipei"))
    server_time = now.strftime("%Y-%m-%d %H:%M:%S")

    location_text = "未提供定位"
    if payload.latitude is not None and payload.longitude is not None:
        acc = f" (±{round(payload.accuracy)}m)" if payload.accuracy is not None else ""
        location_text = f"{payload.latitude:.6f}, {payload.longitude:.6f}{acc}"

    # 取得或建立員工
    employee_id = await get_or_create_employee(conn, line_user_id, display_name)

    # 寫入打卡紀錄
    await conn.execute(
        """INSERT INTO clock_records
           (employee_id, line_user_id, action, server_time, frontend_time,
            latitude, longitude, accuracy)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8)""",
        employee_id, line_user_id, payload.action, now,
        payload.frontendTime, payload.latitude, payload.longitude, payload.accuracy
    )

    # 自動更新 work_days（有上班打卡才建出工日，day_value 留 NULL 待管理員確認）
    if payload.action == "clock_in":
        today = now.date()
        await conn.execute(
            """INSERT INTO work_days (employee_id, work_date, clock_in_time)
               VALUES ($1, $2, $3)
               ON CONFLICT (employee_id, work_date)
               DO UPDATE SET clock_in_time = EXCLUDED.clock_in_time""",
            employee_id, today, now
        )
    elif payload.action == "clock_out":
        today = now.date()
        await conn.execute(
            """UPDATE work_days SET clock_out_time = $1
               WHERE employee_id = $2 AND work_date = $3""",
            now, employee_id, today
        )

    return {
        "message":      f"{display_name}打卡{action_to_text(payload.action)}成功",
        "displayName":  display_name,
        "type":         payload.action,
        "time":         server_time,
        "locationText": location_text,
        "lineUserId":   line_user_id,
    }

# ─── 員工查詢自己的薪資 ───────────────────────────────────────

@app.post("/api/my/salary")
async def my_salary(payload: dict, conn=Depends(get_db)):
    """員工用 idToken 查自己 confirmed 的薪資結算單"""
    id_token = payload.get("idToken")
    if not id_token:
        raise HTTPException(status_code=400, detail="缺少 idToken")

    verified     = await verify_line_id_token(id_token)
    line_user_id = verified.get("sub", "")

    rows = await conn.fetch(
        """SELECT sp.*, e.display_name
           FROM salary_periods sp
           JOIN employees e ON e.id = sp.employee_id
           WHERE e.line_user_id = $1 AND sp.status = 'confirmed'
           ORDER BY sp.period_start DESC""",
        line_user_id
    )
    return {"records": [dict(r) for r in rows]}

@app.post("/api/my/workdays")
async def my_workdays(payload: dict, conn=Depends(get_db)):
    """員工查自己的出工日紀錄"""
    id_token = payload.get("idToken")
    if not id_token:
        raise HTTPException(status_code=400, detail="缺少 idToken")

    verified     = await verify_line_id_token(id_token)
    line_user_id = verified.get("sub", "")

    rows = await conn.fetch(
        """SELECT wd.*
           FROM work_days wd
           JOIN employees e ON e.id = wd.employee_id
           WHERE e.line_user_id = $1
           ORDER BY wd.work_date DESC
           LIMIT 60""",
        line_user_id
    )
    return {"records": [dict(r) for r in rows]}

# ─── 管理員 API（需帶 Header: X-Admin-Secret） ────────────────

def get_admin_secret(request: "Request"):
    from fastapi import Request
    return request.headers.get("X-Admin-Secret", "")

# 取得所有員工
@app.get("/api/admin/employees")
async def admin_list_employees(
    x_admin_secret: str = "",
    conn=Depends(get_db)
):
    check_admin(x_admin_secret)
    rows = await conn.fetch(
        "SELECT * FROM employees ORDER BY id"
    )
    return {"employees": [dict(r) for r in rows]}

# 更新員工薪資設定（日薪、加班費率、勞健保）
@app.patch("/api/admin/employees/{employee_id}")
async def admin_update_employee(
    employee_id: int,
    body: EmployeeUpdate,
    x_admin_secret: str = "",
    conn=Depends(get_db)
):
    check_admin(x_admin_secret)
    updates = {k: v for k, v in body.dict().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="沒有要更新的欄位")

    set_clause = ", ".join(f"{k} = ${i+2}" for i, k in enumerate(updates))
    values     = list(updates.values())
    await conn.execute(
        f"UPDATE employees SET {set_clause} WHERE id = $1",
        employee_id, *values
    )
    return {"message": "更新成功"}

# 取得某員工的出工日（待確認列表）
@app.get("/api/admin/workdays/{employee_id}")
async def admin_get_workdays(
    employee_id: int,
    x_admin_secret: str = "",
    conn=Depends(get_db)
):
    check_admin(x_admin_secret)
    rows = await conn.fetch(
        """SELECT * FROM work_days
           WHERE employee_id = $1
           ORDER BY work_date DESC""",
        employee_id
    )
    return {"workdays": [dict(r) for r in rows]}

# 管理員確認出工日（填 1.0 / 0.5）
@app.patch("/api/admin/workdays/{workday_id}")
async def admin_update_workday(
    workday_id: int,
    body: WorkDayUpdate,
    x_admin_secret: str = "",
    conn=Depends(get_db)
):
    check_admin(x_admin_secret)
    await conn.execute(
        """UPDATE work_days
           SET day_value = $1, note = COALESCE($2, note)
           WHERE id = $3""",
        body.day_value, body.note, workday_id
    )
    return {"message": "出工日已更新"}

# 新增加班紀錄
@app.post("/api/admin/overtime")
async def admin_add_overtime(
    body: OvertimeCreate,
    x_admin_secret: str = "",
    conn=Depends(get_db)
):
    check_admin(x_admin_secret)
    emp = await conn.fetchrow(
        "SELECT overtime_rate FROM employees WHERE id = $1", body.employee_id
    )
    if not emp:
        raise HTTPException(status_code=404, detail="員工不存在")

    await conn.execute(
        """INSERT INTO overtime_records
           (employee_id, work_date, hours, rate_snapshot, note)
           VALUES ($1,$2,$3,$4,$5)""",
        body.employee_id, body.work_date, body.hours,
        emp["overtime_rate"], body.note
    )
    return {"message": "加班紀錄已新增"}

# 新增借支
@app.post("/api/admin/loans")
async def admin_add_loan(
    body: LoanCreate,
    x_admin_secret: str = "",
    conn=Depends(get_db)
):
    check_admin(x_admin_secret)
    # 查現有餘額
    last = await conn.fetchrow(
        """SELECT remaining_balance FROM loans
           WHERE employee_id = $1
           ORDER BY created_at DESC LIMIT 1""",
        body.employee_id
    )
    prev_balance = last["remaining_balance"] if last else 0
    new_balance  = prev_balance + body.amount

    await conn.execute(
        """INSERT INTO loans (employee_id, amount, loan_date, remaining_balance, note)
           VALUES ($1,$2,$3,$4,$5)""",
        body.employee_id, body.amount,
        body.loan_date or date.today(), new_balance, body.note
    )
    return {"message": "借支已新增", "remaining_balance": new_balance}

# 產生薪資結算單（自動計算）
@app.post("/api/admin/salary_periods")
async def admin_create_salary_period(
    body: SalaryPeriodCreate,
    x_admin_secret: str = "",
    conn=Depends(get_db)
):
    check_admin(x_admin_secret)

    emp = await conn.fetchrow(
        "SELECT * FROM employees WHERE id = $1", body.employee_id
    )
    if not emp:
        raise HTTPException(status_code=404, detail="員工不存在")

    # 加總出工天數（只算有 day_value 的）
    total_days_row = await conn.fetchrow(
        """SELECT COALESCE(SUM(day_value), 0) as total
           FROM work_days
           WHERE employee_id = $1
             AND work_date BETWEEN $2 AND $3
             AND day_value IS NOT NULL""",
        body.employee_id, body.period_start, body.period_end
    )
    total_days = float(total_days_row["total"])

    # 加總加班時數
    ot_row = await conn.fetchrow(
        """SELECT COALESCE(SUM(hours), 0) as total,
                  COALESCE(MAX(rate_snapshot), $4) as rate
           FROM overtime_records
           WHERE employee_id = $1
             AND work_date BETWEEN $2 AND $3""",
        body.employee_id, body.period_start, body.period_end, emp["overtime_rate"]
    )
    total_ot_hours = float(ot_row["total"])
    ot_rate        = int(ot_row["rate"])

    # 計算薪資
    gross  = int(emp["daily_rate"] * total_days + ot_rate * total_ot_hours)
    net    = gross - emp["labor_insurance"] - emp["health_insurance"] \
             - body.loan_deduction - body.expenses

    row = await conn.fetchrow(
        """INSERT INTO salary_periods
           (employee_id, period_label, period_start, period_end, settlement_date,
            daily_rate_snapshot, total_days, total_overtime_hours, overtime_rate_snapshot,
            labor_insurance, health_insurance, loan_deduction, expenses,
            gross_salary, net_salary, note)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16)
           RETURNING id""",
        body.employee_id, body.period_label, body.period_start, body.period_end,
        body.settlement_date, emp["daily_rate"], total_days, total_ot_hours, ot_rate,
        emp["labor_insurance"], emp["health_insurance"],
        body.loan_deduction, body.expenses, gross, net, body.note
    )

    # 把出工日和加班紀錄綁定到這張結算單
    period_id = row["id"]
    await conn.execute(
        """UPDATE work_days SET period_id = $1
           WHERE employee_id = $2 AND work_date BETWEEN $3 AND $4""",
        period_id, body.employee_id, body.period_start, body.period_end
    )
    await conn.execute(
        """UPDATE overtime_records SET period_id = $1
           WHERE employee_id = $2 AND work_date BETWEEN $3 AND $4""",
        period_id, body.employee_id, body.period_start, body.period_end
    )

    return {
        "message":    "薪資結算單已產生",
        "period_id":  period_id,
        "total_days": total_days,
        "gross":      gross,
        "net":        net,
    }

# 查看某員工的所有結算單
@app.get("/api/admin/salary_periods/{employee_id}")
async def admin_get_salary_periods(
    employee_id: int,
    x_admin_secret: str = "",
    conn=Depends(get_db)
):
    check_admin(x_admin_secret)
    rows = await conn.fetch(
        """SELECT sp.*, e.display_name
           FROM salary_periods sp
           JOIN employees e ON e.id = sp.employee_id
           WHERE sp.employee_id = $1
           ORDER BY sp.period_start DESC""",
        employee_id
    )
    return {"periods": [dict(r) for r in rows]}

# 確認 / 退回結算單
@app.patch("/api/admin/salary_periods/{period_id}/status")
async def admin_confirm_salary_period(
    period_id: int,
    body: SalaryPeriodConfirm,
    x_admin_secret: str = "",
    conn=Depends(get_db)
):
    check_admin(x_admin_secret)
    if body.status not in {"draft", "confirmed"}:
        raise HTTPException(status_code=400, detail="status 只能是 draft 或 confirmed")
    await conn.execute(
        """UPDATE salary_periods
           SET status = $1, updated_at = NOW()
           WHERE id = $2""",
        body.status, period_id
    )
    return {"message": f"結算單已更新為 {body.status}"}

# 借支餘額查詢
@app.get("/api/admin/loans/{employee_id}")
async def admin_get_loans(
    employee_id: int,
    x_admin_secret: str = "",
    conn=Depends(get_db)
):
    check_admin(x_admin_secret)
    rows = await conn.fetch(
        """SELECT * FROM loans
           WHERE employee_id = $1
           ORDER BY created_at DESC""",
        employee_id
    )
    return {"loans": [dict(r) for r in rows]}