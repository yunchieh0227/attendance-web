from fastapi import FastAPI, HTTPException, Depends, Header
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
ADMIN_SECRET    = os.getenv("ADMIN_SECRET", "").strip()

app = FastAPI(title="Clock Backend", version="0.3.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[ALLOW_ORIGIN],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ─── 連線池 ──────────────────────────────────────────────────────
pool: asyncpg.Pool | None = None

@app.on_event("startup")
async def startup():
    global pool
    pool = await asyncpg.create_pool(DATABASE_URL, min_size=1, max_size=5, statement_cache_size=0)

@app.on_event("shutdown")
async def shutdown():
    if pool:
        await pool.close()

async def get_db():
    async with pool.acquire() as conn:
        yield conn

# ─── Models ──────────────────────────────────────────────────────

class ClockRequest(BaseModel):
    action: str
    idToken: str
    lineUserId: Optional[str] = None
    displayName: Optional[str] = None
    frontendTime: Optional[str] = None
    latitude: Optional[float] = None
    longitude: Optional[float] = None
    accuracy: Optional[float] = None
    address_text: Optional[str] = None

class EmployeeUpdate(BaseModel):
    daily_rate:       Optional[int]  = None
    overtime_rate:    Optional[int]  = None
    labor_insurance:  Optional[int]  = None
    health_insurance: Optional[int]  = None
    tax:              Optional[int]  = None
    agency_fee:       Optional[int]  = None
    is_active:        Optional[bool] = None
    payment_method:   Optional[str]  = None

class WorkDayUpdate(BaseModel):
    day_value:      Optional[float] = None
    overtime_hours: Optional[float] = None
    note:           Optional[str]   = None
    clock_in_time:  Optional[str]   = None  # "HH:MM" 24小時制
    clock_out_time: Optional[str]   = None  # "HH:MM" 24小時制

class LoanCreate(BaseModel):
    employee_id: int
    amount:      int
    note:        Optional[str] = None

class SalaryPeriodCreate(BaseModel):
    employee_id:     int
    period_label:    str
    period_start:    date
    period_end:      date
    settlement_date: Optional[date] = None
    loan_deduction:  int = 0
    note:            Optional[str] = None

class SalaryPeriodUpdate(BaseModel):
    note:   Optional[str] = None
    status: Optional[str] = None

# ─── 工具 ────────────────────────────────────────────────────────

def check_admin(secret: str):
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
    row = await conn.fetchrow("SELECT id FROM employees WHERE line_user_id=$1", line_user_id)
    if row:
        await conn.execute(
            "UPDATE employees SET display_name=$1 WHERE line_user_id=$2",
            display_name, line_user_id
        )
        return row["id"]
    row = await conn.fetchrow(
        "INSERT INTO employees (line_user_id, display_name) VALUES ($1,$2) RETURNING id",
        line_user_id, display_name
    )
    return row["id"]

# ─── 健康檢查 ─────────────────────────────────────────────────────

@app.api_route("/", methods=["GET","HEAD"])
async def root():
    return {"message": "Clock backend v0.3 is running"}

@app.api_route("/health", methods=["GET","HEAD"])
async def health():
    return {"ok": True}

# ─── 打卡 ────────────────────────────────────────────────────────

@app.post("/api/clock")
async def clock(payload: ClockRequest, conn=Depends(get_db)):
    if payload.action not in {"clock_in", "clock_out"}:
        raise HTTPException(status_code=400, detail="action 必須是 clock_in 或 clock_out")

    verified     = await verify_line_id_token(payload.idToken)
    line_user_id = verified.get("sub", "")
    display_name = payload.displayName or "未知使用者"
    now          = datetime.now(ZoneInfo("Asia/Taipei"))
    server_time  = now.strftime("%Y-%m-%d %H:%M:%S")

    location_text = "未提供定位"
    if payload.latitude is not None and payload.longitude is not None:
        acc = f" (±{round(payload.accuracy)}m)" if payload.accuracy is not None else ""
        if payload.address_text:
            location_text = f"{payload.address_text}{acc}"
        else:
            location_text = f"{payload.latitude:.6f}, {payload.longitude:.6f}{acc}"

    employee_id = await get_or_create_employee(conn, line_user_id, display_name)

    await conn.execute(
        """INSERT INTO clock_records
           (employee_id, line_user_id, action, server_time, frontend_time,
            latitude, longitude, accuracy, address_text)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9)""",
        employee_id, line_user_id, payload.action, now,
        payload.frontendTime, payload.latitude, payload.longitude, payload.accuracy,
        payload.address_text
    )

    today = now.date()
    if payload.action == "clock_in":
        await conn.execute(
            """INSERT INTO work_days (employee_id, work_date, clock_in_time, clock_in_address)
               VALUES ($1,$2,$3,$4)
               ON CONFLICT (employee_id, work_date)
               DO UPDATE SET clock_in_time    = EXCLUDED.clock_in_time,
                             clock_in_address = EXCLUDED.clock_in_address""",
            employee_id, today, now, payload.address_text
        )
    else:
        result = await conn.execute(
            "UPDATE work_days SET clock_out_time=$1, clock_out_address=$2 WHERE employee_id=$3 AND work_date=$4",
            now, payload.address_text, employee_id, today
        )
        if result == "UPDATE 0":
            await conn.execute(
                "INSERT INTO work_days (employee_id, work_date, clock_out_time, clock_out_address) VALUES ($1,$2,$3,$4)",
                employee_id, today, now, payload.address_text
            )

    return {
        "message":      f"{display_name}打卡{action_to_text(payload.action)}成功",
        "displayName":  display_name,
        "type":         payload.action,
        "time":         server_time,
        "locationText": location_text,
        "lineUserId":   line_user_id,
    }

# ─── 員工查詢自己的資料 ────────────────────────────────────────────

@app.post("/api/my/salary")
async def my_salary(payload: dict, conn=Depends(get_db)):
    id_token = payload.get("idToken")
    if not id_token:
        raise HTTPException(status_code=400, detail="缺少 idToken")
    verified     = await verify_line_id_token(id_token)
    line_user_id = verified.get("sub", "")
    rows = await conn.fetch(
        """SELECT sp.*, e.display_name FROM salary_periods sp
           JOIN employees e ON e.id = sp.employee_id
           WHERE e.line_user_id=$1 AND sp.status='confirmed'
           ORDER BY sp.period_start DESC""",
        line_user_id
    )
    return {"records": [dict(r) for r in rows]}

@app.post("/api/my/workdays")
async def my_workdays(payload: dict, conn=Depends(get_db)):
    id_token = payload.get("idToken")
    if not id_token:
        raise HTTPException(status_code=400, detail="缺少 idToken")
    verified     = await verify_line_id_token(id_token)
    line_user_id = verified.get("sub", "")
    rows = await conn.fetch(
        """SELECT wd.* FROM work_days wd
           JOIN employees e ON e.id = wd.employee_id
           WHERE e.line_user_id=$1
           ORDER BY wd.work_date DESC LIMIT 60""",
        line_user_id
    )
    return {"records": [dict(r) for r in rows]}

# ─── 管理員：員工 ─────────────────────────────────────────────────

@app.get("/api/admin/employees")
async def admin_list_employees(
    x_admin_secret: str = Header(default=""),
    conn=Depends(get_db)
):
    check_admin(x_admin_secret)
    rows = await conn.fetch("SELECT * FROM employees ORDER BY id")
    return {"employees": [dict(r) for r in rows]}

@app.patch("/api/admin/employees/{employee_id}")
async def admin_update_employee(
    employee_id: int,
    body: EmployeeUpdate,
    x_admin_secret: str = Header(default=""),
    conn=Depends(get_db)
):
    check_admin(x_admin_secret)
    updates = {k: v for k, v in body.dict().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="沒有要更新的欄位")
    set_clause = ", ".join(f"{k}=${i+2}" for i, k in enumerate(updates))
    await conn.execute(
        f"UPDATE employees SET {set_clause} WHERE id=$1",
        employee_id, *list(updates.values())
    )
    return {"message": "更新成功"}

@app.delete("/api/admin/employees/{employee_id}")
async def admin_delete_employee(
    employee_id: int,
    x_admin_secret: str = Header(default=""),
    conn=Depends(get_db)
):
    check_admin(x_admin_secret)
    emp = await conn.fetchrow("SELECT id, display_name FROM employees WHERE id=$1", employee_id)
    if not emp:
        raise HTTPException(status_code=404, detail="員工不存在")
    # 依序刪除關聯資料，再刪員工
    await conn.execute("DELETE FROM overtime_records WHERE employee_id=$1", employee_id)
    await conn.execute("DELETE FROM loans          WHERE employee_id=$1", employee_id)
    await conn.execute("DELETE FROM salary_periods WHERE employee_id=$1", employee_id)
    await conn.execute("DELETE FROM work_days      WHERE employee_id=$1", employee_id)
    await conn.execute("DELETE FROM clock_records  WHERE employee_id=$1", employee_id)
    await conn.execute("DELETE FROM employees      WHERE id=$1", employee_id)
    return {"message": f"員工 {emp['display_name']} 已刪除"}

# ─── 管理員：出工日 ────────────────────────────────────────────────

@app.get("/api/admin/workdays/{employee_id}")
async def admin_get_workdays(
    employee_id: int,
    x_admin_secret: str = Header(default=""),
    conn=Depends(get_db)
):
    check_admin(x_admin_secret)
    rows = await conn.fetch(
        "SELECT * FROM work_days WHERE employee_id=$1 ORDER BY work_date DESC",
        employee_id
    )
    return {"workdays": [dict(r) for r in rows]}

@app.patch("/api/admin/workdays/{workday_id}")
async def admin_update_workday(
    workday_id: int,
    body: WorkDayUpdate,
    x_admin_secret: str = Header(default=""),
    conn=Depends(get_db)
):
    check_admin(x_admin_secret)

    if body.day_value is not None or body.note is not None:
        await conn.execute(
            """UPDATE work_days
               SET day_value = COALESCE($1, day_value),
                   note      = COALESCE($2, note)
               WHERE id=$3""",
            body.day_value, body.note, workday_id
        )

    if body.clock_in_time is not None or body.clock_out_time is not None:
        wd_row = await conn.fetchrow("SELECT work_date FROM work_days WHERE id=$1", workday_id)
        if wd_row:
            tz = ZoneInfo("Asia/Taipei")
            if body.clock_in_time is not None:
                try:
                    h, m = map(int, body.clock_in_time.split(":"))
                    cin_dt = datetime(wd_row["work_date"].year, wd_row["work_date"].month,
                                      wd_row["work_date"].day, h, m, 0, tzinfo=tz)
                    await conn.execute("UPDATE work_days SET clock_in_time=$1 WHERE id=$2", cin_dt, workday_id)
                except ValueError:
                    raise HTTPException(status_code=400, detail="上班時間格式錯誤，請使用 HH:MM")
            if body.clock_out_time is not None:
                try:
                    h, m = map(int, body.clock_out_time.split(":"))
                    cout_dt = datetime(wd_row["work_date"].year, wd_row["work_date"].month,
                                       wd_row["work_date"].day, h, m, 0, tzinfo=tz)
                    await conn.execute("UPDATE work_days SET clock_out_time=$1 WHERE id=$2", cout_dt, workday_id)
                except ValueError:
                    raise HTTPException(status_code=400, detail="下班時間格式錯誤，請使用 HH:MM")

    if body.overtime_hours is not None:
        wd = await conn.fetchrow(
            "SELECT employee_id, work_date FROM work_days WHERE id=$1", workday_id
        )
        if wd:
            emp = await conn.fetchrow(
                "SELECT overtime_rate FROM employees WHERE id=$1", wd["employee_id"]
            )
            await conn.execute(
                "DELETE FROM overtime_records WHERE employee_id=$1 AND work_date=$2",
                wd["employee_id"], wd["work_date"]
            )
            if body.overtime_hours > 0:
                await conn.execute(
                    """INSERT INTO overtime_records
                       (employee_id, work_date, hours, rate_snapshot)
                       VALUES ($1,$2,$3,$4)""",
                    wd["employee_id"], wd["work_date"],
                    body.overtime_hours, emp["overtime_rate"]
                )

    return {"message": "出工日已更新"}

# ─── 管理員：加班紀錄 ──────────────────────────────────────────────

@app.get("/api/admin/overtime/{employee_id}")
async def admin_get_overtime(
    employee_id: int,
    x_admin_secret: str = Header(default=""),
    conn=Depends(get_db)
):
    check_admin(x_admin_secret)
    rows = await conn.fetch(
        "SELECT * FROM overtime_records WHERE employee_id=$1 ORDER BY work_date DESC",
        employee_id
    )
    return {"records": [dict(r) for r in rows]}

# ─── 管理員：借支 ──────────────────────────────────────────────────

@app.post("/api/admin/loans")
async def admin_add_loan(
    body: LoanCreate,
    x_admin_secret: str = Header(default=""),
    conn=Depends(get_db)
):
    check_admin(x_admin_secret)
    last = await conn.fetchrow(
        "SELECT remaining_balance FROM loans WHERE employee_id=$1 ORDER BY created_at DESC LIMIT 1",
        body.employee_id
    )
    prev = last["remaining_balance"] if last else 0
    new_balance = prev + body.amount
    await conn.execute(
        "INSERT INTO loans (employee_id, amount, loan_date, remaining_balance, note) VALUES ($1,$2,$3,$4,$5)",
        body.employee_id, body.amount, date.today(), new_balance, body.note
    )
    return {"message": "借支已新增", "remaining_balance": new_balance}

@app.get("/api/admin/loans/{employee_id}")
async def admin_get_loans(
    employee_id: int,
    x_admin_secret: str = Header(default=""),
    conn=Depends(get_db)
):
    check_admin(x_admin_secret)
    rows = await conn.fetch(
        "SELECT * FROM loans WHERE employee_id=$1 ORDER BY created_at DESC",
        employee_id
    )
    return {"loans": [dict(r) for r in rows]}

@app.get("/api/admin/loans")
async def admin_get_all_loans(
    x_admin_secret: str = Header(default=""),
    conn=Depends(get_db)
):
    check_admin(x_admin_secret)
    rows = await conn.fetch(
        """SELECT l.*, e.display_name FROM loans l
           JOIN employees e ON e.id = l.employee_id
           ORDER BY l.created_at DESC"""
    )
    return {"loans": [dict(r) for r in rows]}

# ─── 管理員：薪資結算 ──────────────────────────────────────────────

@app.post("/api/admin/salary_periods")
async def admin_create_salary_period(
    body: SalaryPeriodCreate,
    x_admin_secret: str = Header(default=""),
    conn=Depends(get_db)
):
    check_admin(x_admin_secret)

    emp = await conn.fetchrow("SELECT * FROM employees WHERE id=$1", body.employee_id)
    if not emp:
        raise HTTPException(status_code=404, detail="員工不存在")

    # 出工天數
    td_row = await conn.fetchrow(
        """SELECT COALESCE(SUM(day_value),0) AS total FROM work_days
           WHERE employee_id=$1 AND work_date BETWEEN $2 AND $3 AND day_value IS NOT NULL""",
        body.employee_id, body.period_start, body.period_end
    )
    total_days = float(td_row["total"])

    # 加班
    ot_row = await conn.fetchrow(
        """SELECT COALESCE(SUM(hours),0) AS total_hours,
                  COALESCE(SUM(hours * rate_snapshot),0) AS total_amount,
                  COALESCE(MAX(rate_snapshot),$4) AS rate
           FROM overtime_records
           WHERE employee_id=$1 AND work_date BETWEEN $2 AND $3""",
        body.employee_id, body.period_start, body.period_end, emp["overtime_rate"]
    )
    total_ot_hours = float(ot_row["total_hours"])
    ot_rate        = int(ot_row["rate"])
    ot_amount      = int(ot_row["total_amount"])

    # 快照
    daily_rate = emp["daily_rate"]
    labor_ins  = emp["labor_insurance"]
    health_ins = emp["health_insurance"]
    tax        = emp.get("tax", 0) or 0
    agency_fee = emp.get("agency_fee", 0) or 0

    gross = int(daily_rate * total_days) + ot_amount
    net   = gross - labor_ins - health_ins - tax - agency_fee - body.loan_deduction

    # 借支餘額
    last_loan = await conn.fetchrow(
        "SELECT remaining_balance FROM loans WHERE employee_id=$1 ORDER BY created_at DESC LIMIT 1",
        body.employee_id
    )
    prev_loan_balance    = last_loan["remaining_balance"] if last_loan else 0
    loan_remaining_after = prev_loan_balance - body.loan_deduction

    # 寫入結算單
    row = await conn.fetchrow(
        """INSERT INTO salary_periods
           (employee_id, period_label, period_start, period_end, settlement_date,
            daily_rate_snapshot, total_days,
            total_overtime_hours, overtime_rate_snapshot, overtime_amount,
            labor_insurance, health_insurance,
            tax_snapshot, agency_fee_snapshot,
            loan_deduction, loan_remaining_after,
            gross_salary, net_salary, note)
           VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19)
           RETURNING id""",
        body.employee_id, body.period_label, body.period_start, body.period_end,
        body.settlement_date, daily_rate, total_days,
        total_ot_hours, ot_rate, ot_amount,
        labor_ins, health_ins, tax, agency_fee,
        body.loan_deduction, loan_remaining_after,
        gross, net, body.note
    )
    period_id = row["id"]

    # 綁定
    await conn.execute(
        "UPDATE work_days SET period_id=$1 WHERE employee_id=$2 AND work_date BETWEEN $3 AND $4",
        period_id, body.employee_id, body.period_start, body.period_end
    )
    await conn.execute(
        "UPDATE overtime_records SET period_id=$1 WHERE employee_id=$2 AND work_date BETWEEN $3 AND $4",
        period_id, body.employee_id, body.period_start, body.period_end
    )

    # 寫入借支還款紀錄
    if body.loan_deduction > 0:
        await conn.execute(
            "INSERT INTO loans (employee_id, amount, loan_date, remaining_balance, note) VALUES ($1,$2,$3,$4,$5)",
            body.employee_id, -body.loan_deduction, date.today(),
            loan_remaining_after, f"薪資扣款（{body.period_label}）"
        )

    return {
        "message":            "薪資結算單已產生",
        "period_id":          period_id,
        "total_days":         total_days,
        "gross":              gross,
        "net":                net,
        "loan_remaining_after": loan_remaining_after,
    }

@app.get("/api/admin/salary_periods")
async def admin_get_salary_periods(
    employee_id:  Optional[int] = None,
    period_label: Optional[str] = None,
    status:       Optional[str] = None,
    x_admin_secret: str = Header(default=""),
    conn=Depends(get_db)
):
    check_admin(x_admin_secret)
    if status is not None and status not in {"draft", "confirmed"}:
        raise HTTPException(status_code=400, detail="status 只能是 draft 或 confirmed")

    conditions: list[str] = []
    params:     list      = []
    if employee_id is not None:
        params.append(employee_id);  conditions.append(f"sp.employee_id=${len(params)}")
    if period_label:
        params.append(period_label); conditions.append(f"sp.period_label=${len(params)}")
    if status:
        params.append(status);       conditions.append(f"sp.status=${len(params)}")

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
    rows = await conn.fetch(
        f"""SELECT sp.*, e.display_name FROM salary_periods sp
            JOIN employees e ON e.id = sp.employee_id
            {where} ORDER BY sp.period_start DESC""",
        *params
    )
    return {"periods": [dict(r) for r in rows]}

@app.patch("/api/admin/salary_periods/{period_id}")
async def admin_update_salary_period(
    period_id: int,
    body: SalaryPeriodUpdate,
    x_admin_secret: str = Header(default=""),
    conn=Depends(get_db)
):
    check_admin(x_admin_secret)
    updates = {}
    if body.note is not None:
        updates["note"] = body.note
    if body.status is not None:
        if body.status not in {"draft", "confirmed"}:
            raise HTTPException(status_code=400, detail="status 只能是 draft 或 confirmed")
        updates["status"] = body.status
    if not updates:
        raise HTTPException(status_code=400, detail="沒有要更新的欄位")
    updates["updated_at"] = datetime.now(ZoneInfo("Asia/Taipei"))
    set_clause = ", ".join(f"{k}=${i+2}" for i, k in enumerate(updates))
    await conn.execute(
        f"UPDATE salary_periods SET {set_clause} WHERE id=$1",
        period_id, *list(updates.values())
    )
    return {"message": "結算單已更新"}

@app.delete("/api/admin/salary_periods/{period_id}")
async def admin_delete_salary_period(
    period_id: int,
    x_admin_secret: str = Header(default=""),
    conn=Depends(get_db)
):
    check_admin(x_admin_secret)
    period = await conn.fetchrow(
        "SELECT employee_id, period_label, loan_deduction FROM salary_periods WHERE id=$1",
        period_id
    )
    await conn.execute("UPDATE work_days SET period_id=NULL WHERE period_id=$1", period_id)
    await conn.execute("UPDATE overtime_records SET period_id=NULL WHERE period_id=$1", period_id)
    await conn.execute("DELETE FROM salary_periods WHERE id=$1", period_id)
    if period and period["loan_deduction"] > 0:
        await conn.execute(
            "DELETE FROM loans WHERE employee_id=$1 AND amount=$2 AND note=$3",
            period["employee_id"], -period["loan_deduction"],
            f"薪資扣款（{period['period_label']}）"
        )
    return {"message": "結算單已刪除"}

# ─── 管理員：本期總計 ──────────────────────────────────────────────

@app.get("/api/admin/summary_labels")
async def admin_summary_labels(
    x_admin_secret: str = Header(default=""),
    conn=Depends(get_db)
):
    check_admin(x_admin_secret)
    rows = await conn.fetch(
        "SELECT DISTINCT period_label FROM salary_periods WHERE status='confirmed' ORDER BY period_label DESC"
    )
    return {"labels": [r["period_label"] for r in rows]}

@app.get("/api/admin/summary")
async def admin_period_summary(
    period_label: str,
    x_admin_secret: str = Header(default=""),
    conn=Depends(get_db)
):
    check_admin(x_admin_secret)
    rows = await conn.fetch(
        """SELECT sp.*, e.display_name, e.payment_method FROM salary_periods sp
           JOIN employees e ON e.id = sp.employee_id
           WHERE sp.period_label=$1 AND sp.status='confirmed'
           ORDER BY e.display_name""",
        period_label
    )
    data        = [dict(r) for r in rows]
    total_gross = sum(r["gross_salary"] for r in data)
    total_net   = sum(r["net_salary"]   for r in data)
    cash_net     = sum(r["net_salary"] for r in data if (r.get("payment_method") or "cash") == "cash")
    transfer_net = sum(r["net_salary"] for r in data if (r.get("payment_method") or "cash") == "transfer")
    return {"periods": data, "total_gross": total_gross, "total_net": total_net,
            "cash_net": cash_net, "transfer_net": transfer_net}
