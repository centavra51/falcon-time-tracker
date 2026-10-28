"""
Automated Integration Test Suite for Falcon Time Tracker (Python + FastAPI + SQLite3).
Verifies all core requirements and edge cases specified in the Falcon AI assignment:
1. Adding employees and case-insensitive duplicate prevention.
2. Starting and stopping a task timer with a description.
3. Duplicate timer launch prevention (blocks a 2nd concurrent timer for the same employee with HTTP 409).
4. Viewing time entries and per-employee totals for a selected date range (including 23:59:59 end-of-day boundary).
5. Empty report handling for periods with zero entries (returns 00:00:00 and empty list without NULL errors).
6. Data persistence across application restarts (SQLite WAL).
7. Department filtering in reports.
8. CSV export (both detailed log and employee summary modes with UTF-8 BOM).
"""

import os
import tempfile
import unittest
from unittest.mock import patch
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from fastapi.testclient import TestClient

import app as tracker_app


class FalconTimeTrackerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        # Create an isolated temporary SQLite database file for each test
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        self.env_patch = patch.dict(os.environ, {
            "DATABASE_URL": os.environ.get("TEST_DATABASE_URL", ""),
            "TIME_TRACKER_DB": self.temp_db.name, "SEED_DEMO": "0",
        })
        self.env_patch.start()
        tracker_app._initialized_databases.clear()
        if os.environ.get("TEST_DATABASE_URL"):
            tracker_app.init_db()
            with tracker_app.get_db_connection() as conn:
                conn.execute("DELETE FROM time_entries")
                conn.execute("DELETE FROM employees")
        os.environ["TIME_TRACKER_DB"] = self.temp_db.name
        os.environ["SEED_DEMO"] = "0"  # Disable demo seeding for clean test assertions
        tracker_app.init_db()
        self.client = TestClient(tracker_app.app)

    def tearDown(self) -> None:
        self.client.close()
        self.env_patch.stop()
        if os.path.exists(self.temp_db.name):
            os.remove(self.temp_db.name)

    def test_1_add_and_list_employees(self) -> None:
        """Scenario 1: Add employees and prevent case-insensitive duplicates."""
        res = self.client.post(
            "/api/employees",
            json={
                "full_name": "Alex Morgan",
                "role": "Backend Engineer",
                "department": "Engineering",
            },
        )
        self.assertEqual(res.status_code, 201)
        emp = res.json()
        self.assertEqual(emp["full_name"], "Alex Morgan")
        self.assertIsNone(emp["active_timer"])

        # Attempting to add the same employee in lowercase must return 409 Conflict
        dup_res = self.client.post(
            "/api/employees",
            json={"full_name": "alex morgan", "role": "QA Engineer", "department": "Quality Assurance"},
        )
        self.assertEqual(dup_res.status_code, 409)

        list_res = self.client.get("/api/employees")
        self.assertEqual(list_res.status_code, 200)
        self.assertEqual(len(list_res.json()), 1)

    def test_2_start_and_stop_timer(self) -> None:
        """Scenario 2: Start and stop a task timer with description."""
        emp_id = self.client.post(
            "/api/employees",
            json={"full_name": "Maria Kowalski", "role": "AI Engineer", "department": "R&D / AI"},
        ).json()["id"]

        start_res = self.client.post(
            "/api/timers/start",
            json={"employee_id": emp_id, "task_description": "Integrate OpenAI API for resume parsing"},
        )
        self.assertEqual(start_res.status_code, 201)
        entry = start_res.json()
        self.assertTrue(entry["is_active"])
        self.assertIsNone(entry["ended_at"])
        self.assertEqual(entry["employee_department"], "R&D / AI")
        entry_id = entry["id"]

        stop_res = self.client.post(f"/api/timers/{entry_id}/stop")
        self.assertEqual(stop_res.status_code, 200)
        stopped = stop_res.json()
        self.assertFalse(stopped["is_active"])
        self.assertIsNotNone(stopped["ended_at"])
        self.assertGreaterEqual(stopped["duration_seconds"], 1)

        # Stopping an already stopped timer must return 409 Conflict
        stop_again = self.client.post(f"/api/timers/{entry_id}/stop")
        self.assertEqual(stop_again.status_code, 409)

    def test_3_duplicate_timer_start_blocked(self) -> None:
        """Edge Case from Spec: Prevent starting a second timer when one is already running."""
        emp_id = self.client.post(
            "/api/employees",
            json={"full_name": "David Brooks", "role": "Full-Stack Developer", "department": "Engineering"},
        ).json()["id"]

        first = self.client.post(
            "/api/timers/start",
            json={"employee_id": emp_id, "task_description": "First active task"},
        )
        self.assertEqual(first.status_code, 201)

        second = self.client.post(
            "/api/timers/start",
            json={"employee_id": emp_id, "task_description": "Second concurrent task"},
        )
        self.assertEqual(second.status_code, 409)
        self.assertIn("already has an active running timer", second.json()["detail"])

    def test_4_empty_report_scenario(self) -> None:
        """Edge Case from Spec: Empty report for a date range with zero entries."""
        self.client.post(
            "/api/employees",
            json={"full_name": "Elena Vance", "role": "Product Designer", "department": "Product & Design"},
        )

        rep_res = self.client.get(
            "/api/reports/summary",
            params={"date_from": "2020-01-01", "date_to": "2020-01-31"},
        )
        self.assertEqual(rep_res.status_code, 200)
        data = rep_res.json()
        self.assertEqual(data["total_entries"], 0)
        self.assertEqual(data["total_seconds"], 0)
        self.assertEqual(data["formatted_grand_total"], "00:00:00")
        self.assertEqual(data["entries"], [])
        self.assertEqual(len(data["employees_summary"]), 1)
        self.assertEqual(data["employees_summary"][0]["total_seconds"], 0)
        self.assertEqual(data["employees_summary"][0]["entries_count"], 0)

    def test_5_report_period_filtering_and_persistence_after_restart(self) -> None:
        """Scenarios 5 & 6: Date range filtering and persistence across app restarts."""
        emp_id = self.client.post(
            "/api/employees",
            json={"full_name": "Paul Carter", "role": "DevOps Engineer", "department": "Infrastructure"},
        ).json()["id"]

        entry_id = self.client.post(
            "/api/timers/start",
            json={"employee_id": emp_id, "task_description": "Configure Docker and CI/CD pipeline"},
        ).json()["id"]
        self.client.post(f"/api/timers/{entry_id}/stop")

        today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Simulate application restart by creating a new TestClient connected to the same SQLite DB file
        new_client = TestClient(tracker_app.app)
        report = new_client.get(
            "/api/reports/summary",
            params={"date_from": today_str, "date_to": today_str, "employee_id": emp_id},
        ).json()

        self.assertEqual(report["total_entries"], 1)
        self.assertGreaterEqual(report["total_seconds"], 1)
        self.assertEqual(report["entries"][0]["task_description"], "Configure Docker and CI/CD pipeline")
        self.assertEqual(report["employees_summary"][0]["full_name"], "Paul Carter")

    def test_6_department_filter_in_report(self) -> None:
        """Scenario 7: Filter report by department."""
        emp_eng = self.client.post(
            "/api/employees",
            json={"full_name": "Andrew Stone", "role": "Backend", "department": "Engineering"},
        ).json()["id"]
        emp_qa = self.client.post(
            "/api/employees",
            json={"full_name": "Natalie Bell", "role": "QA Lead", "department": "Quality Assurance"},
        ).json()["id"]

        e1 = self.client.post(
            "/api/timers/start",
            json={"employee_id": emp_eng, "task_description": "Optimize SQL queries"},
        ).json()["id"]
        self.client.post(f"/api/timers/{e1}/stop")

        e2 = self.client.post(
            "/api/timers/start",
            json={"employee_id": emp_qa, "task_description": "Run regression suite on reports"},
        ).json()["id"]
        self.client.post(f"/api/timers/{e2}/stop")

        qa_report = self.client.get(
            "/api/reports/summary",
            params={"department": "quality assurance"},
        ).json()

        self.assertEqual(qa_report["total_entries"], 1)
        self.assertEqual(len(qa_report["employees_summary"]), 1)
        self.assertEqual(qa_report["employees_summary"][0]["full_name"], "Natalie Bell")
        self.assertEqual(qa_report["entries"][0]["task_description"], "Run regression suite on reports")
        self.assertIn("Engineering", qa_report["available_departments"])
        self.assertIn("Quality Assurance", qa_report["available_departments"])

    def test_7_csv_export_detailed_and_summary(self) -> None:
        """Scenario 8: Export report as CSV in both detailed and summary modes."""
        emp_id = self.client.post(
            "/api/employees",
            json={"full_name": "Victor Reed", "role": "Data Engineer", "department": "Analytics"},
        ).json()["id"]

        entry_id = self.client.post(
            "/api/timers/start",
            json={"employee_id": emp_id, "task_description": "Build CSV export data pipeline"},
        ).json()["id"]
        self.client.post(f"/api/timers/{entry_id}/stop")

        # 1. Detailed CSV
        csv_det = self.client.get("/api/reports/export.csv", params={"mode": "detailed", "department": "Analytics"})
        self.assertEqual(csv_det.status_code, 200)
        self.assertIn("text/csv", csv_det.headers["content-type"])
        text_det = csv_det.content.decode("utf-8")
        self.assertTrue(text_det.startswith("\ufeff"))
        self.assertIn("Task Description", text_det)
        self.assertIn("Build CSV export data pipeline", text_det)
        self.assertIn("Victor Reed", text_det)

        # 2. Summary CSV
        csv_sum = self.client.get("/api/reports/export.csv", params={"mode": "summary", "department": "Analytics"})
        self.assertEqual(csv_sum.status_code, 200)
        text_sum = csv_sum.content.decode("utf-8")
        self.assertIn("Entries Count", text_sum)
        self.assertIn("Victor Reed", text_sum)
        self.assertIn("Analytics", text_sum)

    def test_8_cyrillic_name_and_department(self):
        employee = self.client.post("/api/employees", json={
            "full_name": "Иван Петров", "department": "Разработка", "role": "Инженер"
        })
        self.assertEqual(employee.status_code, 201)
        duplicate = self.client.post("/api/employees", json={"full_name": "иван петров"})
        self.assertEqual(duplicate.status_code, 409)
        report = self.client.get("/api/reports/summary", params={"department": "РАЗРАБОТКА"})
        self.assertEqual(report.status_code, 200)
        self.assertEqual(len(report.json()["employees_summary"]), 1)

    def test_9_inclusive_date_boundaries(self):
        emp = self.client.post("/api/employees", json={"full_name": "Boundary Tester"}).json()["id"]
        with tracker_app.get_db_connection() as conn:
            for timestamp in ["2026-01-31T00:00:00Z", "2026-01-31T23:59:59Z", "2026-02-01T00:00:00Z"]:
                conn.execute(
                    "INSERT INTO time_entries (employee_id, task_description, started_at, ended_at, duration_seconds) VALUES (?, ?, ?, ?, ?)",
                    (emp, "Boundary task", timestamp, timestamp, 0),
                )
        response = self.client.get("/api/reports/summary", params={"date_from": "2026-01-31", "date_to": "2026-01-31"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["total_entries"], 2)
        self.assertEqual(self.client.get("/api/reports/summary", params={"date_from": "2026-02-01", "date_to": "2026-01-01"}).status_code, 400)

    def test_10_concurrent_timer_start(self):
        emp = self.client.post("/api/employees", json={"full_name": "Concurrent Tester"}).json()["id"]
        def start(_):
            with TestClient(tracker_app.app) as client:
                return client.post("/api/timers/start", json={"employee_id": emp, "task_description": "Concurrent task"}).status_code
        with ThreadPoolExecutor(max_workers=4) as pool:
            codes = list(pool.map(start, range(4)))
        self.assertEqual(sorted(codes), [201, 409, 409, 409])

    def test_11_concurrent_timer_stop(self):
        emp = self.client.post("/api/employees", json={"full_name": "Stop Tester"}).json()["id"]
        entry = self.client.post("/api/timers/start", json={"employee_id": emp, "task_description": "Stop task"}).json()["id"]
        def stop(_):
            with TestClient(tracker_app.app) as client:
                return client.post(f"/api/timers/{entry}/stop").status_code
        with ThreadPoolExecutor(max_workers=4) as pool:
            codes = list(pool.map(stop, range(4)))
        self.assertEqual(sorted(codes), [200, 409, 409, 409])

    def test_12_unknown_employee_and_invalid_payload(self):
        self.assertEqual(self.client.post("/api/timers/start", json={"employee_id": 999999, "task_description": "Unknown employee"}).status_code, 404)
        self.assertEqual(self.client.post("/api/employees", json={"full_name": "  "}).status_code, 422)
        self.assertEqual(self.client.get("/api/reports/summary", params={"date_to": "invalid"}).status_code, 400)


if __name__ == "__main__":
    unittest.main(verbosity=2)
