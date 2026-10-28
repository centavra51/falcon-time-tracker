"""
Falcon Time Tracker — Employee Time Tracking Web Application
Built for the Falcon AI Developer Technical Assignment (Stack: Python 3 + FastAPI + SQLite3).

Key Engineering Features:
- Full SQLite persistence (WAL mode) across server restarts.
- Duplicate active timer prevention at both application level (HTTP 409 Conflict)
  and database level via a partial unique index (WHERE ended_at IS NULL).
- Accurate UTC ISO-8601 timestamps and inclusive date range filtering
  (00:00:00Z to 23:59:59Z).
- Department filtering and employee filtering for summary and detailed reports.
- CSV export (Detailed Time Log & Employee Summary) with UTF-8 BOM for Microsoft Excel,
  Google Sheets, and LibreOffice Calc compatibility.
- Safe empty report aggregation (returns 00:00:00 and 0 seconds instead of SQL NULL).
- Unicode case-insensitive matching via custom SQLite function (UNICODE_LOWER).
- Cross-version compatibility with both Pydantic v1 and Pydantic v2.
"""

from __future__ import annotations

import csv
import io
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock

from database import open_database, INTEGRITY_ERRORS
from typing import Generator, List, Optional

from fastapi import FastAPI, HTTPException, Query, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

try:
    from pydantic import field_validator as _pydantic_field_validator
    _HAS_PYDANTIC_V2 = True
except ImportError:
    from pydantic import validator as _pydantic_v1_validator
    _HAS_PYDANTIC_V2 = False

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"


def get_db_path() -> str:
    """Returns the SQLite database file path (configurable via TIME_TRACKER_DB env var for isolated tests)."""
    return os.environ.get("TIME_TRACKER_DB", str(BASE_DIR / "time_tracker.db"))


def utc_now() -> datetime:
    """Returns current UTC datetime."""
    return datetime.now(timezone.utc)


def to_iso_utc(dt: datetime) -> str:
    """Formats a datetime into an ISO-8601 UTC string with a trailing Z."""
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso_utc(value: str) -> datetime:
    """Parses an ISO-8601 string into a timezone-aware UTC datetime."""
    cleaned = value.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"
    dt = datetime.fromisoformat(cleaned)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def format_duration_hms(total_seconds: int) -> str:
    """Formats seconds into 'HH:MM:SS'."""
    seconds = max(0, int(total_seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


@contextmanager
def get_db_connection():
    with open_database(get_db_path()) as conn:
        yield conn


_init_lock = Lock()
_initialized_databases = set()


def init_db() -> None:
    key = os.environ.get("DATABASE_URL") or get_db_path()
    with _init_lock:
        if key not in _initialized_databases:
            _initialize_database()
            _initialized_databases.add(key)


def _initialize_database() -> None:
    """Initializes database tables, indexes, and optional fictional demo data."""
    with get_db_connection() as conn:
        if os.environ.get("DATABASE_URL"):
            conn.execute("SELECT pg_advisory_xact_lock(7063549001)")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS employees (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                full_name TEXT NOT NULL UNIQUE,
                role TEXT NOT NULL DEFAULT 'Software Engineer',
                department TEXT NOT NULL DEFAULT 'Engineering',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS time_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                employee_id INTEGER NOT NULL,
                task_description TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT NULL,
                duration_seconds INTEGER NULL,
                FOREIGN KEY (employee_id) REFERENCES employees(id) ON DELETE CASCADE
            );

            -- Database-level guard against multiple concurrent active timers per employee
            CREATE UNIQUE INDEX IF NOT EXISTS idx_one_active_timer_per_employee
                ON time_entries(employee_id)
                WHERE ended_at IS NULL;

            CREATE UNIQUE INDEX IF NOT EXISTS idx_employee_name_ci
                ON employees(UNICODE_LOWER(full_name));

            CREATE INDEX IF NOT EXISTS idx_time_entries_started_at
                ON time_entries(started_at);

            CREATE INDEX IF NOT EXISTS idx_time_entries_employee_id
                ON time_entries(employee_id);
            """
        )

        # Seed fictional demo records on first startup unless disabled via SEED_DEMO=0
        if os.environ.get("SEED_DEMO", "1") == "1":
            count = conn.execute("SELECT COUNT(*) AS count FROM employees").fetchone()["count"]
            if count == 0:
                _seed_fictional_data(conn)


def _seed_fictional_data(conn: sqlite3.Connection) -> None:
    """Populates fictional employees and sample time entries for demonstration."""
    now = utc_now()
    created_iso = to_iso_utc(now - timedelta(days=10))

    demo_employees = [
        ("Alex Morgan", "Backend Engineer", "Engineering"),
        ("Maria Kowalski", "AI Prompts & LLM Engineer", "R&D / AI"),
        ("David Brooks", "Full-Stack Developer", "Engineering"),
        ("Elena Vance", "QA Automation Engineer", "Quality Assurance"),
    ]

    emp_ids = []
    for full_name, role, dept in demo_employees:
        cur = conn.execute(
            "INSERT INTO employees (full_name, role, department, created_at) VALUES (?, ?, ?, ?) RETURNING id",
            (full_name, role, dept, created_iso),
        )
        emp_ids.append(cur.fetchone()["id"])

    today_start = now - timedelta(hours=3)
    yesterday_start = now - timedelta(days=1, hours=3)

    sample_entries = [
        (
            emp_ids[0],
            "Designed SQLite WAL schema and partial unique index for timer idempotency",
            yesterday_start,
            5400,  # 1h 30m
        ),
        (
            emp_ids[1],
            "Tuned LLM system prompts for automated candidate resume screening",
            yesterday_start + timedelta(hours=1, minutes=15),
            4200,  # 1h 10m
        ),
        (
            emp_ids[2],
            "Implemented REST API summary reports with date range and department filters",
            today_start,
            3900,  # 1h 05m
        ),
        (
            emp_ids[3],
            "Added automated tests for duplicate timer prevention and empty report periods",
            today_start + timedelta(hours=1),
            2700,  # 45m
        ),
    ]

    for emp_id, desc, start_dt, dur in sample_entries:
        end_dt = start_dt + timedelta(seconds=dur)
        conn.execute(
            """
            INSERT INTO time_entries (employee_id, task_description, started_at, ended_at, duration_seconds)
            VALUES (?, ?, ?, ?, ?)
            """,
            (emp_id, desc, to_iso_utc(start_dt), to_iso_utc(end_dt), dur),
        )


# ---------------------------------------------------------------------------
# Pydantic Request & Response Schemas (Pydantic v1 & v2 compatible)
# ---------------------------------------------------------------------------


def _clean_non_empty_text(value: str, field_label: str) -> str:
    cleaned = " ".join(str(value).strip().split())
    if len(cleaned) < 2:
        raise ValueError(f"{field_label} must contain at least 2 non-whitespace characters")
    return cleaned


class EmployeeCreate(BaseModel):
    full_name: str = Field(..., min_length=2, max_length=100, description="Employee full name (fictional data)")
    role: str = Field(default="Software Engineer", min_length=2, max_length=80, description="Job title / role")
    department: str = Field(default="Engineering", min_length=2, max_length=80, description="Department name")

    if _HAS_PYDANTIC_V2:
        @_pydantic_field_validator("full_name", "role", "department")
        @classmethod
        def _validate_fields_v2(cls, v: str) -> str:
            return _clean_non_empty_text(v, "Field")
    else:
        @_pydantic_v1_validator("full_name", "role", "department")
        def _validate_fields_v1(cls, v: str) -> str:
            return _clean_non_empty_text(v, "Field")


class ActiveTimerInfo(BaseModel):
    entry_id: int
    task_description: str
    started_at: str
    elapsed_seconds: int
    formatted_elapsed: str


class EmployeeOut(BaseModel):
    id: int
    full_name: str
    role: str
    department: str
    created_at: str
    active_timer: Optional[ActiveTimerInfo] = None


class TimerStartRequest(BaseModel):
    employee_id: int = Field(..., gt=0, description="Employee ID")
    task_description: str = Field(..., min_length=2, max_length=300, description="Task description")

    if _HAS_PYDANTIC_V2:
        @_pydantic_field_validator("task_description")
        @classmethod
        def _validate_task_v2(cls, v: str) -> str:
            return _clean_non_empty_text(v, "Task description")
    else:
        @_pydantic_v1_validator("task_description")
        def _validate_task_v1(cls, v: str) -> str:
            return _clean_non_empty_text(v, "Task description")


class TimeEntryOut(BaseModel):
    id: int
    employee_id: int
    employee_name: str
    employee_role: str
    employee_department: str
    task_description: str
    started_at: str
    ended_at: Optional[str] = None
    duration_seconds: int
    formatted_duration: str
    is_active: bool


class EmployeeReportSummary(BaseModel):
    employee_id: int
    full_name: str
    role: str
    department: str
    entries_count: int
    total_seconds: int
    formatted_total: str
    has_active_timer: bool


class ReportResponse(BaseModel):
    date_from: Optional[str] = None
    date_to: Optional[str] = None
    employee_id: Optional[int] = None
    department: Optional[str] = None
    available_departments: List[str] = Field(default_factory=list)
    total_entries: int
    total_seconds: int
    formatted_grand_total: str
    employees_summary: List[EmployeeReportSummary]
    entries: List[TimeEntryOut]


# ---------------------------------------------------------------------------
# FastAPI App Initialization
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Falcon Time Tracker API",
    description="Employee Time Tracking Web Application (Falcon AI Technical Assignment)",
    version="1.2.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup() -> None:
    init_db()


def _build_entry_out(row: sqlite3.Row, now: datetime) -> TimeEntryOut:
    """Converts a DB row into TimeEntryOut, dynamically computing elapsed time for active timers."""
    is_active = row["ended_at"] is None
    if is_active:
        started_dt = parse_iso_utc(row["started_at"])
        duration = max(0, int((now - started_dt).total_seconds()))
    else:
        duration = int(row["duration_seconds"] or 0)

    return TimeEntryOut(
        id=row["id"],
        employee_id=row["employee_id"],
        employee_name=row["full_name"],
        employee_role=row["role"],
        employee_department=row["department"],
        task_description=row["task_description"],
        started_at=row["started_at"],
        ended_at=row["ended_at"],
        duration_seconds=duration,
        formatted_duration=format_duration_hms(duration),
        is_active=is_active,
    )


def _parse_date_bounds(
    date_from: Optional[str], date_to: Optional[str]
) -> tuple[Optional[str], Optional[str]]:
    """
    Normalizes YYYY-MM-DD strings into full inclusive UTC day boundaries:
    date_from -> YYYY-MM-DDT00:00:00Z
    date_to   -> YYYY-MM-DDT23:59:59Z
    Prevents the common AI date-filtering bug where entries on the final day are excluded.
    """
    iso_from: Optional[str] = None
    iso_to: Optional[str] = None

    if date_from:
        try:
            dt_from = datetime.strptime(date_from.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
            iso_from = dt_from.strftime("%Y-%m-%dT00:00:00Z")
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid date_from format. Expected YYYY-MM-DD.",
            ) from exc

    if date_to:
        try:
            dt_to = datetime.strptime(date_to.strip(), "%Y-%m-%d").replace(tzinfo=timezone.utc)
            iso_to = dt_to.strftime("%Y-%m-%dT23:59:59Z")
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Invalid date_to format. Expected YYYY-MM-DD.",
            ) from exc

    if iso_from and iso_to and iso_from > iso_to:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Start date (date_from) cannot be later than end date (date_to).",
        )

    return iso_from, iso_to


def _compile_report_data(
    date_from: Optional[str],
    date_to: Optional[str],
    employee_id: Optional[int],
    department: Optional[str],
) -> ReportResponse:
    """Compiles summary and detailed report data with date range, employee, and department filters."""
    init_db()
    now = utc_now()
    iso_from, iso_to = _parse_date_bounds(date_from, date_to)
    clean_dept = department.strip() if department and department.strip() else None

    entry_where = []
    entry_params: list = []

    if iso_from:
        entry_where.append("te.started_at >= ?")
        entry_params.append(iso_from)
    if iso_to:
        entry_where.append("te.started_at <= ?")
        entry_params.append(iso_to)
    if employee_id is not None:
        entry_where.append("te.employee_id = ?")
        entry_params.append(employee_id)
    if clean_dept:
        entry_where.append("UNICODE_LOWER(e.department) = UNICODE_LOWER(?)")
        entry_params.append(clean_dept)

    where_sql = ("WHERE " + " AND ".join(entry_where)) if entry_where else ""

    emp_where = []
    emp_params: list = []
    if employee_id is not None:
        emp_where.append("id = ?")
        emp_params.append(employee_id)
    if clean_dept:
        emp_where.append("UNICODE_LOWER(department) = UNICODE_LOWER(?)")
        emp_params.append(clean_dept)

    emp_where_sql = ("WHERE " + " AND ".join(emp_where)) if emp_where else ""

    with get_db_connection() as conn:
        all_depts_rows = conn.execute(
            "SELECT DISTINCT department FROM employees ORDER BY department ASC"
        ).fetchall()
        available_departments = [r["department"] for r in all_depts_rows if r["department"]]

        emp_rows = conn.execute(
            f"SELECT id, full_name, role, department FROM employees {emp_where_sql} ORDER BY id ASC",
            emp_params,
        ).fetchall()

        entry_rows = conn.execute(
            f"""
            SELECT te.id, te.employee_id, te.task_description, te.started_at, te.ended_at, te.duration_seconds,
                   e.full_name, e.role, e.department
            FROM time_entries te
            JOIN employees e ON e.id = te.employee_id
            {where_sql}
            ORDER BY te.started_at DESC, te.id DESC
            """,
            entry_params,
        ).fetchall()

        active_emp_ids = {
            r["employee_id"]
            for r in conn.execute("SELECT employee_id FROM time_entries WHERE ended_at IS NULL").fetchall()
        }

    entries_out = [_build_entry_out(r, now) for r in entry_rows]

    totals_by_emp: dict[int, int] = {r["id"]: 0 for r in emp_rows}
    counts_by_emp: dict[int, int] = {r["id"]: 0 for r in emp_rows}

    for item in entries_out:
        if item.employee_id in totals_by_emp:
            totals_by_emp[item.employee_id] += item.duration_seconds
            counts_by_emp[item.employee_id] += 1

    employees_summary = [
        EmployeeReportSummary(
            employee_id=r["id"],
            full_name=r["full_name"],
            role=r["role"],
            department=r["department"],
            entries_count=counts_by_emp.get(r["id"], 0),
            total_seconds=totals_by_emp.get(r["id"], 0),
            formatted_total=format_duration_hms(totals_by_emp.get(r["id"], 0)),
            has_active_timer=(r["id"] in active_emp_ids),
        )
        for r in emp_rows
    ]

    grand_total_seconds = sum(item.duration_seconds for item in entries_out)

    return ReportResponse(
        date_from=date_from,
        date_to=date_to,
        employee_id=employee_id,
        department=clean_dept,
        available_departments=available_departments,
        total_entries=len(entries_out),
        total_seconds=grand_total_seconds,
        formatted_grand_total=format_duration_hms(grand_total_seconds),
        employees_summary=employees_summary,
        entries=entries_out,
    )


# ---------------------------------------------------------------------------
# API Routes
# ---------------------------------------------------------------------------


@app.get("/api/health", tags=["System"])
def health_check() -> dict:
    init_db()
    with get_db_connection() as conn:
        emp_count = conn.execute("SELECT COUNT(*) AS count FROM employees").fetchone()["count"]
        active_count = conn.execute("SELECT COUNT(*) AS count FROM time_entries WHERE ended_at IS NULL").fetchone()["count"]
    return {
        "status": "ok",
        "database": "PostgreSQL" if os.environ.get("DATABASE_URL") else "SQLite (WAL)",
        "employees_count": emp_count,
        "active_timers_count": active_count,
    }


@app.get("/api/employees", response_model=List[EmployeeOut], tags=["Employees"])
def list_employees() -> List[EmployeeOut]:
    init_db()
    now = utc_now()
    with get_db_connection() as conn:
        emp_rows = conn.execute(
            "SELECT id, full_name, role, department, created_at FROM employees ORDER BY id ASC"
        ).fetchall()
        active_rows = conn.execute(
            "SELECT id, employee_id, task_description, started_at FROM time_entries WHERE ended_at IS NULL"
        ).fetchall()

    active_by_emp = {}
    for a_row in active_rows:
        started_dt = parse_iso_utc(a_row["started_at"])
        elapsed = max(0, int((now - started_dt).total_seconds()))
        active_by_emp[a_row["employee_id"]] = ActiveTimerInfo(
            entry_id=a_row["id"],
            task_description=a_row["task_description"],
            started_at=a_row["started_at"],
            elapsed_seconds=elapsed,
            formatted_elapsed=format_duration_hms(elapsed),
        )

    return [
        EmployeeOut(
            id=row["id"],
            full_name=row["full_name"],
            role=row["role"],
            department=row["department"],
            created_at=row["created_at"],
            active_timer=active_by_emp.get(row["id"]),
        )
        for row in emp_rows
    ]


@app.post("/api/employees", response_model=EmployeeOut, status_code=status.HTTP_201_CREATED, tags=["Employees"])
def create_employee(payload: EmployeeCreate) -> EmployeeOut:
    init_db()
    created_at = to_iso_utc(utc_now())
    with get_db_connection() as conn:
        existing = conn.execute(
            "SELECT id FROM employees WHERE UNICODE_LOWER(full_name) = UNICODE_LOWER(?)",
            (payload.full_name,),
        ).fetchone()
        if existing:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Employee '{payload.full_name}' already exists in the system.",
            )

        try:
            cur = conn.execute(
                "INSERT INTO employees (full_name, role, department, created_at) VALUES (?, ?, ?, ?) RETURNING id",
                (payload.full_name, payload.role, payload.department, created_at),
            )
            emp_id = cur.fetchone()["id"]
        except INTEGRITY_ERRORS as exc:
            raise HTTPException(status_code=409, detail="Employee already exists in the system.") from exc

    return EmployeeOut(
        id=emp_id,
        full_name=payload.full_name,
        role=payload.role,
        department=payload.department,
        created_at=created_at,
        active_timer=None,
    )


@app.post("/api/timers/start", response_model=TimeEntryOut, status_code=status.HTTP_201_CREATED, tags=["Timers"])
def start_timer(payload: TimerStartRequest) -> TimeEntryOut:
    """
    Starts a new task timer for an employee.
    Returns HTTP 409 Conflict if the employee already has a running timer (duplicate timer protection).
    """
    init_db()
    now = utc_now()
    started_iso = to_iso_utc(now)

    with get_db_connection() as conn:
        emp = conn.execute(
            "SELECT id, full_name, role, department FROM employees WHERE id = ?",
            (payload.employee_id,),
        ).fetchone()
        if not emp:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Employee with ID={payload.employee_id} was not found.",
            )

        active = conn.execute(
            "SELECT id, task_description, started_at FROM time_entries WHERE employee_id = ? AND ended_at IS NULL",
            (payload.employee_id,),
        ).fetchone()
        if active:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"Employee '{emp['full_name']}' already has an active running timer "
                    f"for task '{active['task_description']}'. Please stop the current timer first."
                ),
            )

        try:
            cur = conn.execute(
                """
                INSERT INTO time_entries (employee_id, task_description, started_at, ended_at, duration_seconds)
                VALUES (?, ?, ?, NULL, NULL) RETURNING id
                """,
                (payload.employee_id, payload.task_description, started_iso),
            )
            entry_id = cur.fetchone()["id"]
        except INTEGRITY_ERRORS as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cannot start a second concurrent timer for the same employee.",
            ) from exc

        row = conn.execute(
            """
            SELECT te.id, te.employee_id, te.task_description, te.started_at, te.ended_at, te.duration_seconds,
                   e.full_name, e.role, e.department
            FROM time_entries te
            JOIN employees e ON e.id = te.employee_id
            WHERE te.id = ?
            """,
            (entry_id,),
        ).fetchone()

    return _build_entry_out(row, now)


@app.post("/api/timers/{entry_id}/stop", response_model=TimeEntryOut, tags=["Timers"])
def stop_timer(entry_id: int) -> TimeEntryOut:
    """Stops an active timer by entry ID and persists the final duration in seconds."""
    init_db()
    now = utc_now()
    ended_iso = to_iso_utc(now)

    with get_db_connection() as conn:
        row = conn.execute(
            """
            SELECT te.id, te.employee_id, te.task_description, te.started_at, te.ended_at, te.duration_seconds,
                   e.full_name, e.role, e.department
            FROM time_entries te
            JOIN employees e ON e.id = te.employee_id
            WHERE te.id = ?
            """,
            (entry_id,),
        ).fetchone()

        if not row:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Time entry with ID={entry_id} was not found.",
            )

        if row["ended_at"] is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="This timer has already been stopped.",
            )

        started_dt = parse_iso_utc(row["started_at"])
        # Ensure at least 1 second is recorded on rapid start/stop clicks during testing
        duration_seconds = max(1, int((now - started_dt).total_seconds()))

        result = conn.execute(
            "UPDATE time_entries SET ended_at = ?, duration_seconds = ? WHERE id = ? AND ended_at IS NULL",
            (ended_iso, duration_seconds, entry_id),
        )

        if result.rowcount != 1:
            raise HTTPException(status_code=409, detail="This timer has already been stopped.")

        updated = conn.execute(
            """
            SELECT te.id, te.employee_id, te.task_description, te.started_at, te.ended_at, te.duration_seconds,
                   e.full_name, e.role, e.department
            FROM time_entries te
            JOIN employees e ON e.id = te.employee_id
            WHERE te.id = ?
            """,
            (entry_id,),
        ).fetchone()

    return _build_entry_out(updated, now)


@app.get("/api/reports/summary", response_model=ReportResponse, tags=["Reports"])
def get_report_summary(
    date_from: Optional[str] = Query(default=None, description="Start date in YYYY-MM-DD format"),
    date_to: Optional[str] = Query(default=None, description="End date in YYYY-MM-DD format"),
    employee_id: Optional[int] = Query(default=None, description="Filter by employee ID"),
    department: Optional[str] = Query(default=None, description="Filter by department name"),
) -> ReportResponse:
    """
    Returns detailed time entries and per-employee totals for the selected period,
    with optional department and employee filters.
    """
    return _compile_report_data(
        date_from=date_from,
        date_to=date_to,
        employee_id=employee_id,
        department=department,
    )


@app.get("/api/reports/export.csv", tags=["Reports"])
def export_report_csv(
    date_from: Optional[str] = Query(default=None, description="Start date in YYYY-MM-DD format"),
    date_to: Optional[str] = Query(default=None, description="End date in YYYY-MM-DD format"),
    employee_id: Optional[int] = Query(default=None, description="Filter by employee ID"),
    department: Optional[str] = Query(default=None, description="Filter by department name"),
    mode: str = Query(default="detailed", description="CSV mode: 'detailed' (time entries log) or 'summary' (per-employee totals)"),
) -> Response:
    """
    Exports the filtered report as a CSV file (with UTF-8 BOM for direct compatibility with Excel).
    Supports two modes:
    - mode=detailed: exports individual time log records;
    - mode=summary: exports aggregated totals per employee.
    """
    report = _compile_report_data(
        date_from=date_from,
        date_to=date_to,
        employee_id=employee_id,
        department=department,
    )

    buffer = io.StringIO()
    # Include UTF-8 BOM so Microsoft Excel opens Unicode characters cleanly
    buffer.write("\ufeff")
    writer = csv.writer(buffer, delimiter=",", quoting=csv.QUOTE_MINIMAL)

    if mode.strip().lower() == "summary":
        writer.writerow([
            "Employee ID",
            "Employee Name",
            "Role",
            "Department",
            "Timer Status",
            "Entries Count",
            "Total Seconds",
            "Total Time (HH:MM:SS)",
        ])
        for row in report.employees_summary:
            writer.writerow([
                row.employee_id,
                row.full_name,
                row.role,
                row.department,
                "Running" if row.has_active_timer else "Idle",
                row.entries_count,
                row.total_seconds,
                row.formatted_total,
            ])
        filename_prefix = "time_report_summary"
    else:
        writer.writerow([
            "Entry ID",
            "Employee ID",
            "Employee Name",
            "Role",
            "Department",
            "Task Description",
            "Started At (UTC)",
            "Ended At (UTC)",
            "Duration (Seconds)",
            "Duration (HH:MM:SS)",
            "Status",
        ])
        for item in report.entries:
            writer.writerow([
                item.id,
                item.employee_id,
                item.employee_name,
                item.employee_role,
                item.employee_department,
                item.task_description,
                item.started_at,
                item.ended_at or "Running",
                item.duration_seconds,
                item.formatted_duration,
                "Running" if item.is_active else "Completed",
            ])
        filename_prefix = "time_report_detailed"

    period_suffix = f"{date_from or 'all'}_{date_to or 'all'}"
    filename = f"{filename_prefix}_{period_suffix}.csv"

    return Response(
        content=buffer.getvalue().encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
        },
    )


# ---------------------------------------------------------------------------
# Web UI Serving
# ---------------------------------------------------------------------------

if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def serve_index():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return FileResponse(str(index_file))
    return HTMLResponse("<h1>Falcon Time Tracker API is running. Visit /docs for Swagger UI.</h1>")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), reload=True)
