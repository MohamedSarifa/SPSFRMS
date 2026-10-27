import os
import json
import tempfile
import subprocess
import sys
import unittest
from types import SimpleNamespace
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import BytesIO, StringIO
from unittest.mock import patch

os.environ["SPSFRMS_SKIP_STARTUP_DB_INIT"] = "1"
os.environ["SPSFRMS_STARTUP_MODE"] = "skip"
os.environ["DATABASE_URL"] = "sqlite://"
os.environ["SECRET_KEY"] = "synthetic-workflow-test-key-for-isolated-tests"

from app import (  # noqa: E402
    CATEGORY_DEPARTMENTS,
    DEPARTMENT_NAMES,
    Complaint,
    ComplaintAssignment,
    ComplaintDepartment,
    ComplaintStatusHistory,
    Department,
    User,
    active_assignment_workload,
    active_assignment_workloads,
    analyze_complaint,
    app,
    db,
    format_kolkata_datetime,
    image_uploads_enabled_from_environment,
    priority_order_key,
    run_startup_database_setup,
    utcnow,
    create_demo_users,
)
from werkzeug.security import check_password_hash, generate_password_hash  # noqa: E402
from password_policy import (  # noqa: E402
    individual_passwords_for,
    parse_password_map_json,
    require_unique_passwords,
)
from account_identity import normalize_email  # noqa: E402
from reset_all_demo_passwords import main as retired_bulk_reset_main  # noqa: E402
from staff_recommendation import (  # noqa: E402
    InsufficientTrainingData,
    chronological_group_split,
    recommend_staff,
    reconstruct_historical_workloads,
    train_staff_models,
    validate_assignment_records,
    workload_recommendation,
)
from sqlalchemy import inspect  # noqa: E402


class SPSFRMSWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.config.update(
            TESTING=True,
            SQLALCHEMY_DATABASE_URI="sqlite://",
            # Keep the in-memory SQLite database on one connection for the
            # full suite; recycling drops its schema after the app's 300s TTL.
            SQLALCHEMY_ENGINE_OPTIONS={"pool_recycle": -1},
            WTF_CSRF_ENABLED=False,
            SECRET_KEY="workflow-test-key-for-isolated-tests",
        )
        with app.app_context():
            if db.engine.dialect.name != "sqlite":
                raise RuntimeError("Workflow tests must use isolated SQLite, never the configured project database.")
            if app.config.get("DATABASE_INITIALIZATION_STATE", "not_run") != "not_run":
                raise RuntimeError("Workflow tests require database initialization to remain unrun on import.")
            if inspect(db.engine).get_table_names():
                raise RuntimeError("Startup skip must not create tables before isolated test setup.")
            db.create_all()

    @classmethod
    def tearDownClass(cls):
        with app.app_context():
            db.drop_all()
            db.session.remove()
            db.engine.dispose()

    def setUp(self):
        with app.app_context():
            db.drop_all()
            db.create_all()
            self.departments = {}
            for name in set(DEPARTMENT_NAMES) | {"Police", "Drainage", "Public Health"}:
                department = Department(name=name)
                db.session.add(department)
                self.departments[name] = department
            db.session.flush()
            self.citizen = User(name="Test Citizen", email="citizen@example.test", phone="100",
                                password=generate_password_hash("test-password"), role="citizen")
            self.other_citizen = User(name="Other Citizen", email="other-citizen@example.test", phone="108",
                                      password=generate_password_hash("test-password"), role="citizen")
            self.head = User(name="Test Head", email="head@example.test", phone="101",
                             password=generate_password_hash("test-password"), role="department_head",
                             department_id=self.departments["Drainage"].id)
            self.other_head = User(name="Other Head", email="other-head@example.test", phone="106",
                                   password=generate_password_hash("test-password"), role="department_head",
                                   department_id=self.departments["Police"].id)
            self.unlinked_head = User(name="Unlinked Head", email="unlinked-head@example.test", phone="107",
                                      password=generate_password_hash("test-password"), role="department_head",
                                      department_id=self.departments["Waste Management"].id)
            self.staff = User(name="Test Staff", email="staff@example.test", phone="102",
                              password=generate_password_hash("test-password"), role="staff",
                              department_id=self.departments["Drainage"].id)
            self.staff2 = User(name="Other Drainage Staff", email="staff2@example.test", phone="105",
                               password=generate_password_hash("test-password"), role="staff",
                               department_id=self.departments["Drainage"].id)
            self.other_staff = User(name="Other Staff", email="other@example.test", phone="103",
                                    password=generate_password_hash("test-password"), role="staff",
                                    department_id=self.departments["Police"].id)
            self.admin = User(name="Test Admin", email="admin@example.test", phone="104",
                              password=generate_password_hash("test-password"), role="admin")
            db.session.add_all([self.citizen, self.other_citizen, self.head, self.other_head, self.unlinked_head,
                                self.staff, self.staff2, self.other_staff, self.admin])
            db.session.flush()
            self.departments["Drainage"].head_id = self.head.id
            self.departments["Police"].head_id = self.other_head.id
            self.departments["Waste Management"].head_id = self.unlinked_head.id
            db.session.commit()
            self.department_ids = {name: department.id for name, department in self.departments.items()}
            self.ids = {"citizen": self.citizen.id, "other_citizen": self.other_citizen.id,
                        "head": self.head.id, "staff": self.staff.id,
                        "staff2": self.staff2.id, "other_staff": self.other_staff.id,
                        "other_head": self.other_head.id, "unlinked_head": self.unlinked_head.id,
                        "admin": self.admin.id,
                        "drainage": self.departments["Drainage"].id}
        self.client = app.test_client()

    def login_as(self, account_key):
        with self.client.session_transaction() as state:
            state["user_id"] = self.ids[account_key]
            state["role"] = {"citizen": "citizen", "other_citizen": "citizen",
                              "head": "department_head", "other_head": "department_head",
                              "unlinked_head": "department_head", "staff": "staff", "admin": "admin"}[account_key]
            state["csrf_token"] = "test-csrf"

    def make_complaint(self, status="Submitted", assigned_staff_id=None, created_date=None, text="drainage overflow"):
        with app.app_context():
            complaint = Complaint(
                complaint_text=text, category="Drainage", location="Test Road", priority="Medium",
                status=status, citizen_id=self.ids["citizen"], department_id=self.ids["drainage"],
                assigned_staff_id=assigned_staff_id, created_date=created_date or utcnow(),
            )
            complaint.department_assignments.append(ComplaintDepartment(
                department_id=self.ids["drainage"], assigned_staff_id=assigned_staff_id,
                staff_assigned_date=utcnow() if assigned_staff_id else None,
                status=status,
            ))
            db.session.add(complaint)
            db.session.commit()
            return complaint.id

    def make_other_department_complaint(self, status="Submitted"):
        with app.app_context():
            complaint = Complaint(
                complaint_text="traffic jam", category="Traffic", location="Other Road",
                priority="Low", status=status, citizen_id=self.ids["citizen"],
                department_id=self.department_ids["Police"],
            )
            complaint.department_assignments.append(ComplaintDepartment(
                department_id=self.department_ids["Police"], status=status,
            ))
            db.session.add(complaint)
            db.session.commit()
            return complaint.id

    def make_multi_department_complaint(self, status="Submitted", assigned_staff_id=None, citizen_id=None):
        with app.app_context():
            complaint = Complaint(
                complaint_text="private multi-department fixture", category="Multiple Departments",
                location="Fixture location", priority="Medium", status=status,
                citizen_id=citizen_id or self.ids["citizen"], department_id=self.department_ids["Drainage"],
                assigned_staff_id=assigned_staff_id,
            )
            complaint.department_assignments.append(ComplaintDepartment(
                department_id=self.department_ids["Police"],
            ))
            db.session.add(complaint)
            db.session.commit()
            return complaint.id

    def test_drainage_overflow_maps_to_drainage(self):
        result = analyze_complaint("The drainage water is overflowing onto the road near my house.")
        self.assertEqual(result["category"], "Drainage")
        self.assertEqual(result["department"], "Drainage")
        self.assertEqual(result["priority"], "Medium")

    def test_requested_category_examples(self):
        cases = [
            ("A blocked drain is causing waterlogging", "Drainage"),
            ("Sewage is overflowing from the sewer", "Drainage"),
            ("Garbage and trash are piling up", "Garbage"),
            ("A large road crack is damaging the pavement", "Road Damage"),
            ("Water is leaking from a burst pipe", "Water Leakage"),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(analyze_complaint(text)["category"], expected)

    def test_contaminated_drinking_water_maps_to_water_supply(self):
        result = analyze_complaint("The drinking water is contaminated and unsafe.")
        self.assertEqual(result["category"], "Water Supply")
        self.assertEqual(result["department"], "Water Supply")

    def test_road_damage_does_not_route_to_public_health(self):
        result = analyze_complaint(
            "The main road has several large potholes and the damaged road surface needs immediate repair."
        )
        self.assertIn("Road Damage", result["categories"])
        self.assertIn("Road Maintenance", result["departments"])
        self.assertNotIn("Public Health", result["departments"])

    def test_water_supply_only_does_not_route_to_road_maintenance(self):
        result = analyze_complaint(
            "Our area has not received municipal drinking water for four days. Please restore the water supply."
        )
        self.assertIn("Water Supply", result["categories"])
        self.assertEqual(result["departments"], ["Water Supply"])
        self.assertNotIn("Road Maintenance", result["departments"])

    def test_drainage_only_routes_to_drainage(self):
        result = analyze_complaint(
            "The drainage channel is blocked and sewage water is overflowing onto the street."
        )
        self.assertIn("Drainage", result["categories"])
        self.assertIn("Drainage", result["departments"])
        self.assertNotIn("Road Maintenance", result["departments"])

    def test_multi_issue_description_selects_all_supported_departments_once(self):
        result = analyze_complaint(
            "Sewage water is leaking near the drinking water pipeline. The water supplied to our houses "
            "has a bad smell, and dirty water is collecting on the street. Please repair the pipeline, "
            "clear the drainage and check the drinking water quality."
        )
        self.assertTrue(result["needs_review"])
        self.assertEqual(
            set(result["departments"]),
            {"Water Supply", "Drainage", "Public Health"},
        )
        self.assertEqual(len(result["departments"]), len(set(result["departments"])))
        self.assertEqual(result["department"], result["departments"][0])

    def test_road_damage_and_drainage_are_both_detected(self):
        result = analyze_complaint(
            "The road is full of potholes, and the drainage water is overflowing onto the damaged road."
        )
        self.assertIn("Road Maintenance", result["departments"])
        self.assertIn("Drainage", result["departments"])
        self.assertNotIn("Public Health", result["departments"])

    def test_public_health_issue_does_not_force_road_or_water_supply(self):
        result = analyze_complaint(
            "Garbage has accumulated near the public park and mosquitoes are breeding there, creating a health risk."
        )
        self.assertIn("Public Health", result["departments"])
        self.assertNotIn("Road Maintenance", result["departments"])
        self.assertNotIn("Water Supply", result["departments"])

    def test_streetlight_road_garbage_fire_and_public_health_rules(self):
        cases = [
            ("Street light is not working", "Street Light"),
            ("Large pothole and road damage", "Road Damage"),
            ("Garbage accumulation beside the market", "Garbage"),
            ("Fire emergency with smoke", "Fire"),
            ("Disease outbreak reported in the area", "Public Health"),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(analyze_complaint(text)["category"], expected)

    def test_ambiguous_description_is_flagged_for_review(self):
        result = analyze_complaint("There is a problem near my house.")
        self.assertTrue(result["needs_review"])
        self.assertEqual(result["category"], "Other")
        self.assertEqual(result["department"], "Public Health")

    def test_overlapping_category_evidence_is_flagged_for_review(self):
        result = analyze_complaint(
            "Drainage is overflowing and drinking water is contaminated near the road."
        )
        self.assertTrue(result["needs_review"])
        self.assertIn("Drainage", result["categories"])
        self.assertIn("Water Supply", result["categories"])
        self.assertIn("Drainage", result["departments"])
        self.assertIn("Water Supply", result["departments"])

    def test_priority_order_is_emergency_priority_then_age_then_id(self):
        older = Complaint(id=10, complaint_text="pothole", priority="High", created_date=utcnow() - timedelta(days=2))
        newer = Complaint(id=11, complaint_text="pothole", priority="High", created_date=utcnow() - timedelta(days=1))
        emergency = Complaint(id=12, complaint_text="fire emergency", priority="Low", created_date=utcnow())
        self.assertLess(priority_order_key(emergency), priority_order_key(older))
        self.assertLess(priority_order_key(older), priority_order_key(newer))

    def test_same_priority_order_uses_oldest_then_id(self):
        older = Complaint(id=20, complaint_text="pothole", priority="Medium", created_date=utcnow() - timedelta(days=1))
        same_time_larger_id = Complaint(id=22, complaint_text="pothole", priority="Medium", created_date=utcnow())
        same_time_smaller_id = Complaint(id=21, complaint_text="pothole", priority="Medium", created_date=same_time_larger_id.created_date)
        self.assertLess(priority_order_key(older), priority_order_key(same_time_smaller_id))
        self.assertLess(priority_order_key(same_time_smaller_id), priority_order_key(same_time_larger_id))

    def test_dashboard_renders_compact_assignment_interface_for_head(self):
        self.make_complaint()
        self.login_as("head")
        response = self.client.get("/officer-dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Assign for Drainage", response.data)
        self.assertIn(b"/2 active assignments", response.data)

    def test_multiple_departments_is_not_an_active_classification_or_route(self):
        self.assertNotIn("Multiple Departments", CATEGORY_DEPARTMENTS)
        result = analyze_complaint("Multiple departments need to inspect a damaged road.")
        self.assertNotEqual(result["category"], "Multiple Departments")

    def test_multi_department_legacy_record_is_manageable_only_by_linked_department_heads(self):
        complaint_id = self.make_multi_department_complaint()

        self.login_as("head")
        primary_response = self.client.get("/officer-dashboard")
        self.assertEqual(primary_response.status_code, 200)
        self.assertIn(f"CMP{complaint_id:03d}".encode(), primary_response.data)
        self.assertIn(b"Staff in Drainage", primary_response.data)
        self.assertNotIn(b"Staff in Police", primary_response.data)
        self.assertNotIn(f"/ignore-complaint/{complaint_id}".encode(), primary_response.data)
        cross_department_attempt = self.client.post(f"/assign-complaint/{complaint_id}", data={
            "csrf_token": "test-csrf", "department_id": str(self.department_ids["Police"]),
            "staff_id": str(self.ids["other_staff"]),
        })
        self.assertEqual(cross_department_attempt.status_code, 403)
        primary_assignment = self.client.post(f"/assign-complaint/{complaint_id}", data={
            "csrf_token": "test-csrf", "department_id": str(self.department_ids["Drainage"]),
            "staff_id": str(self.ids["staff"]),
        })
        self.assertEqual(primary_assignment.status_code, 302)

        self.login_as("other_head")
        linked_response = self.client.get("/officer-dashboard")
        self.assertEqual(linked_response.status_code, 200)
        self.assertIn(f"CMP{complaint_id:03d}".encode(), linked_response.data)
        self.assertIn(b"Staff in Police", linked_response.data)
        self.assertNotIn(b"Staff in Drainage", linked_response.data)
        linked_cross_department_attempt = self.client.post(f"/assign-complaint/{complaint_id}", data={
            "csrf_token": "test-csrf", "department_id": str(self.department_ids["Drainage"]),
            "staff_id": str(self.ids["staff"]),
        })
        self.assertEqual(linked_cross_department_attempt.status_code, 403)
        linked_assignment = self.client.post(f"/assign-complaint/{complaint_id}", data={
            "csrf_token": "test-csrf", "department_id": str(self.department_ids["Police"]),
            "staff_id": str(self.ids["other_staff"]),
        })
        self.assertEqual(linked_assignment.status_code, 302)
        tracked = self.client.get(f"/track-complaint/{complaint_id}")
        self.assertEqual(tracked.status_code, 200)
        self.assertIn(b"Police", tracked.data)
        self.assertNotIn(b"Drainage</strong>", tracked.data)
        self.assertIn(b"Legacy routing", tracked.data)

        self.login_as("unlinked_head")
        self.assertNotIn(f"CMP{complaint_id:03d}".encode(), self.client.get("/officer-dashboard").data)
        self.assertEqual(self.client.get(f"/track-complaint/{complaint_id}").status_code, 403)
        unlinked_assignment = self.client.post(f"/assign-complaint/{complaint_id}", data={
            "csrf_token": "test-csrf", "department_id": str(self.department_ids["Waste Management"]),
            "staff_id": str(self.ids["unlinked_head"]),
        })
        self.assertEqual(unlinked_assignment.status_code, 403)

    def test_linked_secondary_department_head_can_manage_only_their_own_department(self):
        with app.app_context():
            complaint = Complaint(
                complaint_text="Drainage overflow with a linked public safety issue",
                category="Drainage", location="Fixture location", priority="Medium",
                status="Submitted", citizen_id=self.ids["citizen"],
                department_id=self.department_ids["Drainage"],
            )
            complaint.department_assignments.append(ComplaintDepartment(
                department_id=self.department_ids["Police"],
            ))
            db.session.add(complaint)
            db.session.commit()
            complaint_id = complaint.id

        self.login_as("other_head")
        dashboard = self.client.get("/officer-dashboard")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn(f"CMP{complaint_id:03d}".encode(), dashboard.data)
        self.assertIn(b"Staff in Police", dashboard.data)
        self.assertNotIn(b"Staff in Drainage", dashboard.data)

        assignment_response = self.client.post(
            f"/assign-complaint/{complaint_id}",
            data={
                "csrf_token": "test-csrf", "department_id": str(self.department_ids["Police"]),
                "staff_id": str(self.ids["other_staff"]),
            },
        )
        self.assertEqual(assignment_response.status_code, 302)
        with app.app_context():
            complaint = db.session.get(Complaint, complaint_id)
            self.assertEqual(complaint.status, "Assigned")
            self.assertIsNone(complaint.assigned_staff_id)
            linked_assignment = ComplaintDepartment.query.filter_by(
                complaint_id=complaint_id, department_id=self.department_ids["Police"],
            ).one()
            self.assertEqual(linked_assignment.assigned_staff_id, self.ids["other_staff"])
            self.assertEqual(linked_assignment.status, "Assigned")

    def test_multi_department_legacy_assignment_is_allowed_but_global_ignore_restore_remain_restricted(self):
        active_id = self.make_multi_department_complaint()
        ignored_id = self.make_multi_department_complaint(status="Ignored")
        with app.app_context():
            db.session.add(ComplaintStatusHistory(
                complaint_id=ignored_id, old_status="Submitted", new_status="Ignored",
                updated_by=self.ids["head"], remarks="Existing ignore reason",
            ))
            db.session.commit()
            before_history = ComplaintStatusHistory.query.count()
            before_assignments = ComplaintAssignment.query.count()

        self.login_as("head")
        assignment_response = self.client.post(f"/assign-complaint/{active_id}", data={
            "department_id": str(self.department_ids["Drainage"]),
            "staff_id": self.ids["staff"], "csrf_token": "test-csrf",
        })
        responses = [
            self.client.post(f"/ignore-complaint/{active_id}", data={
                "reason": "Attempted action", "csrf_token": "test-csrf",
            }),
            self.client.post(f"/restore-complaint/{ignored_id}", data={"csrf_token": "test-csrf"}),
        ]
        self.assertEqual(assignment_response.status_code, 302)
        self.assertTrue(all(response.status_code == 403 for response in responses))
        with app.app_context():
            self.assertEqual(db.session.get(Complaint, active_id).status, "Assigned")
            self.assertEqual(db.session.get(Complaint, active_id).assigned_staff_id, self.ids["staff"])
            self.assertEqual(db.session.get(Complaint, ignored_id).status, "Ignored")
            self.assertEqual(ComplaintStatusHistory.query.count(), before_history + 1)
            self.assertEqual(ComplaintAssignment.query.count(), before_assignments + 1)

    def test_multi_department_assigned_staff_changes_only_their_department_status(self):
        complaint_id = self.make_multi_department_complaint(
            status="Assigned", assigned_staff_id=self.ids["staff"],
        )
        self.login_as("staff")
        dashboard = self.client.get("/staff-dashboard")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn(b"Start Drainage work", dashboard.data)
        response = self.client.post(f"/update-status/{complaint_id}", data={
            "department_id": str(self.department_ids["Drainage"]),
            "status": "In Progress", "csrf_token": "test-csrf",
        })
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            complaint = db.session.get(Complaint, complaint_id)
            self.assertEqual(complaint.status, "In Progress")
            primary = ComplaintDepartment.query.filter_by(
                complaint_id=complaint_id, department_id=self.department_ids["Drainage"],
            ).one()
            secondary = ComplaintDepartment.query.filter_by(
                complaint_id=complaint_id, department_id=self.department_ids["Police"],
            ).one()
            self.assertEqual(primary.status, "In Progress")
            self.assertEqual(secondary.status, "Submitted")
            self.assertEqual(ComplaintStatusHistory.query.filter_by(complaint_id=complaint_id).count(), 1)

    def test_multi_department_tracking_denies_unassigned_staff_and_other_citizens(self):
        complaint_id = self.make_multi_department_complaint(assigned_staff_id=self.ids["staff2"])
        self.login_as("staff")
        staff_response = self.client.get(f"/track-complaint/{complaint_id}")
        self.assertEqual(staff_response.status_code, 403)
        self.assertNotIn(b"private multi-department fixture", staff_response.data)

        self.login_as("other_citizen")
        citizen_response = self.client.get(f"/track-complaint/{complaint_id}")
        self.assertEqual(citizen_response.status_code, 403)
        self.assertNotIn(b"private multi-department fixture", citizen_response.data)

        self.login_as("citizen")
        own_response = self.client.get(f"/track-complaint/{complaint_id}")
        self.assertEqual(own_response.status_code, 200)

    def test_assignment_rejects_cross_department_staff(self):
        complaint_id = self.make_complaint(status="Assigned", assigned_staff_id=self.ids["staff"])
        self.login_as("head")
        response = self.client.post(f"/assign-complaint/{complaint_id}", data={
            "staff_id": self.ids["other_staff"], "csrf_token": "test-csrf",
        })
        self.assertEqual(response.status_code, 403)

    def test_assignment_accepts_same_department_staff_and_records_history(self):
        complaint_id = self.make_complaint()
        self.login_as("head")
        response = self.client.post(f"/assign-complaint/{complaint_id}", data={
            "staff_id": self.ids["staff"], "remarks": "Recommended by workload", "csrf_token": "test-csrf",
        })
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            complaint = db.session.get(Complaint, complaint_id)
            self.assertEqual(complaint.status, "Assigned")
            self.assertEqual(complaint.assigned_staff_id, self.ids["staff"])
            self.assertEqual(len(complaint.assignment_history), 1)

    def test_repeat_assignment_does_not_duplicate_history(self):
        complaint_id = self.make_complaint(status="Assigned", assigned_staff_id=self.ids["staff"])
        with app.app_context():
            db.session.add(ComplaintStatusHistory(complaint_id=complaint_id, old_status="Submitted", new_status="Assigned", updated_by=self.ids["head"]))
            db.session.add(ComplaintAssignment(complaint_id=complaint_id, staff_id=self.ids["staff"], assigned_by=self.ids["head"]))
            db.session.commit()
        self.login_as("head")
        response = self.client.post(f"/assign-complaint/{complaint_id}", data={
            "staff_id": self.ids["staff"], "csrf_token": "test-csrf",
        })
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            self.assertEqual(ComplaintStatusHistory.query.filter_by(complaint_id=complaint_id).count(), 1)
            self.assertEqual(ComplaintAssignment.query.filter_by(complaint_id=complaint_id).count(), 1)

    def test_department_head_sees_only_department_complaints(self):
        own_id = self.make_complaint()
        with app.app_context():
            outside = Complaint(
                complaint_text="traffic jam", category="Traffic", location="Elsewhere", priority="Low",
                status="Submitted", citizen_id=self.ids["citizen"],
                department_id=self.department_ids["Police"],
            )
            db.session.add(outside)
            db.session.commit()
            outside_id = outside.id
        self.login_as("head")
        response = self.client.get("/officer-dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertIn(f"CMP{own_id:03d}".encode(), response.data)
        self.assertNotIn(f"CMP{outside_id:03d}".encode(), response.data)

    def test_resolved_assignment_redirects_with_flash_instead_of_400(self):
        complaint_id = self.make_complaint(status="Resolved")
        self.login_as("head")
        response = self.client.post(f"/assign-complaint/{complaint_id}", data={
            "staff_id": self.ids["staff"], "csrf_token": "test-csrf",
        })
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            self.assertEqual(db.session.get(Complaint, complaint_id).status, "Resolved")

    def test_ignore_and_restore_keep_history_and_status(self):
        complaint_id = self.make_complaint(status="Assigned", assigned_staff_id=self.ids["staff"])
        self.login_as("head")
        ignored = self.client.post(f"/ignore-complaint/{complaint_id}", data={
            "reason": "Duplicate", "csrf_token": "test-csrf",
        })
        self.assertEqual(ignored.status_code, 302)
        with app.app_context():
            complaint = db.session.get(Complaint, complaint_id)
            self.assertEqual(complaint.status, "Ignored")
            self.assertEqual(complaint.status_history[-1].remarks, "Duplicate")
        restored = self.client.post(f"/restore-complaint/{complaint_id}", data={"csrf_token": "test-csrf"})
        self.assertEqual(restored.status_code, 302)
        with app.app_context():
            self.assertEqual(db.session.get(Complaint, complaint_id).status, "Assigned")
            self.assertEqual(ComplaintStatusHistory.query.filter_by(complaint_id=complaint_id).count(), 2)

    def test_ignore_requires_reason_and_cannot_ignore_resolved_or_repeat(self):
        active_id = self.make_complaint()
        resolved_id = self.make_complaint(status="Resolved")
        self.login_as("head")
        missing = self.client.post(f"/ignore-complaint/{active_id}", data={"csrf_token": "test-csrf"})
        self.assertEqual(missing.status_code, 302)
        with app.app_context():
            self.assertEqual(db.session.get(Complaint, active_id).status, "Submitted")
            self.assertEqual(ComplaintStatusHistory.query.filter_by(complaint_id=active_id).count(), 0)
        self.client.post(f"/ignore-complaint/{active_id}", data={"reason": "Out of scope", "csrf_token": "test-csrf"})
        self.client.get("/")
        repeated = self.client.post(f"/ignore-complaint/{active_id}", data={"reason": "Again", "csrf_token": "test-csrf"})
        resolved = self.client.post(f"/ignore-complaint/{resolved_id}", data={"reason": "Done", "csrf_token": "test-csrf"})
        self.assertEqual(repeated.status_code, 302)
        self.assertEqual(resolved.status_code, 302)
        with app.app_context():
            self.assertEqual(db.session.get(Complaint, resolved_id).status, "Resolved")
            self.assertEqual(ComplaintStatusHistory.query.filter_by(complaint_id=active_id).count(), 1)
            self.assertEqual(ComplaintStatusHistory.query.filter_by(complaint_id=resolved_id).count(), 0)
            self.assertEqual(ComplaintStatusHistory.query.filter_by(complaint_id=active_id, new_status="Ignored").count(), 1)

    def test_citizen_and_staff_cannot_ignore_or_restore(self):
        active_id = self.make_complaint()
        ignored_id = self.make_complaint(status="Ignored")
        with app.app_context():
            db.session.add(ComplaintStatusHistory(
                complaint_id=ignored_id, old_status="Assigned", new_status="Ignored",
                updated_by=self.ids["head"], remarks="Prior action",
            ))
            db.session.commit()
            before_history = ComplaintStatusHistory.query.count()

        for account in ("citizen", "staff"):
            with self.subTest(account=account):
                self.login_as(account)
                ignore = self.client.post(f"/ignore-complaint/{active_id}", data={
                    "reason": "Unauthorized attempt", "csrf_token": "test-csrf",
                })
                restore = self.client.post(f"/restore-complaint/{ignored_id}", data={
                    "csrf_token": "test-csrf",
                })
                self.assertEqual(ignore.status_code, 403)
                self.assertEqual(restore.status_code, 403)
                with app.app_context():
                    self.assertEqual(db.session.get(Complaint, active_id).status, "Submitted")
                    self.assertEqual(db.session.get(Complaint, ignored_id).status, "Ignored")
                    self.assertEqual(ComplaintStatusHistory.query.count(), before_history)

    def test_head_cannot_ignore_or_restore_another_department_complaint(self):
        active_id = self.make_other_department_complaint()
        ignored_id = self.make_other_department_complaint(status="Ignored")
        with app.app_context():
            db.session.add(ComplaintStatusHistory(
                complaint_id=ignored_id, old_status="In Progress", new_status="Ignored",
                updated_by=self.ids["admin"], remarks="Prior action",
            ))
            db.session.commit()
            before_history = ComplaintStatusHistory.query.count()
        self.login_as("head")
        ignore = self.client.post(f"/ignore-complaint/{active_id}", data={
            "reason": "Out of scope", "csrf_token": "test-csrf",
        })
        restore = self.client.post(f"/restore-complaint/{ignored_id}", data={"csrf_token": "test-csrf"})
        self.assertEqual(ignore.status_code, 403)
        self.assertEqual(restore.status_code, 403)
        with app.app_context():
            self.assertEqual(db.session.get(Complaint, active_id).status, "Submitted")
            self.assertEqual(db.session.get(Complaint, ignored_id).status, "Ignored")
            self.assertEqual(ComplaintStatusHistory.query.count(), before_history)

    def test_assignment_ignore_and_restore_reject_invalid_csrf_without_changes(self):
        assign_id = self.make_complaint()
        ignore_id = self.make_complaint()
        restore_id = self.make_complaint(status="Ignored")
        with app.app_context():
            db.session.add(ComplaintStatusHistory(
                complaint_id=restore_id, old_status="Assigned", new_status="Ignored",
                updated_by=self.ids["head"], remarks="Existing reason",
            ))
            db.session.commit()
            before_history = ComplaintStatusHistory.query.count()
        self.login_as("head")
        requests = [
            (f"/assign-complaint/{assign_id}", {"staff_id": self.ids["staff"]}),
            (f"/ignore-complaint/{ignore_id}", {"reason": "Needs review"}),
            (f"/restore-complaint/{restore_id}", {}),
        ]
        for route, data in requests:
            response = self.client.post(route, data={**data, "csrf_token": "incorrect-token"})
            self.assertEqual(response.status_code, 400)
        with app.app_context():
            self.assertEqual(db.session.get(Complaint, assign_id).status, "Submitted")
            self.assertEqual(db.session.get(Complaint, ignore_id).status, "Submitted")
            self.assertEqual(db.session.get(Complaint, restore_id).status, "Ignored")
            self.assertEqual(ComplaintStatusHistory.query.count(), before_history)

    def test_restore_rejects_active_complaint_and_sanitizes_invalid_prior_status(self):
        active_id = self.make_complaint(status="In Progress")
        ignored_id = self.make_complaint(status="Ignored")
        with app.app_context():
            db.session.add(ComplaintStatusHistory(
                complaint_id=ignored_id, old_status="Unexpected Legacy State", new_status="Ignored",
                updated_by=self.ids["head"], remarks="Legacy row",
            ))
            db.session.commit()
            before_active_history = ComplaintStatusHistory.query.filter_by(complaint_id=active_id).count()
        self.login_as("head")
        rejected = self.client.post(f"/restore-complaint/{active_id}", data={"csrf_token": "test-csrf"})
        restored = self.client.post(f"/restore-complaint/{ignored_id}", data={"csrf_token": "test-csrf"})
        self.assertEqual(rejected.status_code, 302)
        self.assertEqual(restored.status_code, 302)
        with app.app_context():
            self.assertEqual(db.session.get(Complaint, active_id).status, "In Progress")
            self.assertEqual(ComplaintStatusHistory.query.filter_by(complaint_id=active_id).count(), before_active_history)
            self.assertEqual(db.session.get(Complaint, ignored_id).status, "Submitted")
            self.assertEqual(ComplaintStatusHistory.query.filter_by(complaint_id=ignored_id).order_by(ComplaintStatusHistory.id.desc()).first().new_status, "Submitted")

    def test_preview_department_matches_saved_department(self):
        self.login_as("citizen")
        text = "A blocked drain is causing waterlogging"
        preview = self.client.post("/analyze-preview", json={"text": text})
        self.assertEqual(preview.status_code, 200)
        expected_department = preview.get_json()["department"]
        saved = self.client.post("/submit-complaint", data={"complaint_text": text, "location": "Test Road"})
        self.assertEqual(saved.status_code, 302)
        with app.app_context():
            complaint = Complaint.query.order_by(Complaint.id.desc()).first()
            self.assertEqual(complaint.department.name, expected_department)

    def test_multi_department_preview_matches_persisted_primary_and_links(self):
        self.login_as("citizen")
        text = (
            "Sewage water is leaking near the drinking water pipeline. The water supplied to our houses "
            "has a bad smell, and dirty water is collecting on the street. Please repair the pipeline, "
            "clear the drainage and check the drinking water quality."
        )
        preview = self.client.post("/analyze-preview", json={"text": text})
        self.assertEqual(preview.status_code, 200)
        preview_data = preview.get_json()
        saved = self.client.post("/submit-complaint", data={"complaint_text": text, "location": "Test Road"})
        self.assertEqual(saved.status_code, 302)
        with app.app_context():
            complaint = Complaint.query.order_by(Complaint.id.desc()).first()
            linked_names = {
                row.department.name for row in ComplaintDepartment.query.filter_by(complaint_id=complaint.id).all()
            }
            self.assertEqual(complaint.category, preview_data["category"])
            self.assertEqual(complaint.department.name, preview_data["department"])
            self.assertEqual(
                linked_names,
                set(preview_data["departments"]),
            )
            self.assertEqual(
                ComplaintDepartment.query.filter_by(
                    complaint_id=complaint.id, department_id=complaint.department_id,
                ).count(),
                1,
            )

    def _make_department_wise_fixture(self, category="Drainage"):
        with app.app_context():
            water_staff = User(
                name="Water Assignment Fixture", email="water-fixture@example.test", phone="301",
                password=generate_password_hash("test-password"), role="staff",
                department_id=self.department_ids["Water Supply"], is_active=True,
            )
            health_staff = User(
                name="Health Assignment Fixture", email="health-fixture@example.test", phone="302",
                password=generate_password_hash("test-password"), role="staff",
                department_id=self.department_ids["Public Health"], is_active=True,
            )
            db.session.add_all([water_staff, health_staff])
            db.session.flush()
            complaint = Complaint(
                complaint_text="Sewage and drinking water quality issue",
                category=category, location="Fixture location", priority="Medium",
                status="Submitted", citizen_id=self.ids["citizen"],
                department_id=self.department_ids["Drainage"],
            )
            complaint.department_assignments.extend([
                ComplaintDepartment(department_id=self.department_ids["Drainage"], status="Submitted"),
                ComplaintDepartment(department_id=self.department_ids["Water Supply"], status="Submitted"),
                ComplaintDepartment(department_id=self.department_ids["Public Health"], status="Submitted"),
            ])
            db.session.add(complaint)
            db.session.commit()
            return complaint.id, water_staff.id, health_staff.id

    def test_staff_with_zero_active_assignments_can_receive_first_assignment(self):
        complaint_id = self.make_complaint()
        self.login_as("head")
        response = self.client.post(f"/assign-complaint/{complaint_id}", data={
            "csrf_token": "test-csrf", "department_id": str(self.ids["drainage"]),
            "staff_id": str(self.ids["staff"]),
        })
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            self.assertEqual(active_assignment_workloads([self.ids["staff"]])[self.ids["staff"]], 1)

    def test_staff_with_one_active_assignment_can_receive_second(self):
        self.make_complaint(status="Assigned", assigned_staff_id=self.ids["staff"])
        complaint_id = self.make_complaint()
        self.login_as("head")
        response = self.client.post(f"/assign-complaint/{complaint_id}", data={
            "csrf_token": "test-csrf", "department_id": str(self.ids["drainage"]),
            "staff_id": str(self.ids["staff"]),
        })
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            self.assertEqual(active_assignment_workloads([self.ids["staff"]])[self.ids["staff"]], 2)

    def test_staff_with_two_active_assignments_cannot_receive_third(self):
        self.make_complaint(status="Assigned", assigned_staff_id=self.ids["staff"])
        self.make_complaint(status="Assigned", assigned_staff_id=self.ids["staff"])
        complaint_id = self.make_complaint()
        self.login_as("head")
        response = self.client.post(f"/assign-complaint/{complaint_id}", data={
            "csrf_token": "test-csrf", "department_id": str(self.ids["drainage"]),
            "staff_id": str(self.ids["staff"]),
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"already has 2 active assignments", response.data)
        self.assertIn(b"Please select another eligible staff member", response.data)
        with app.app_context():
            state = ComplaintDepartment.query.filter_by(
                complaint_id=complaint_id, department_id=self.ids["drainage"],
            ).one()
            self.assertIsNone(state.assigned_staff_id)
            self.assertEqual(ComplaintAssignment.query.filter_by(complaint_id=complaint_id).count(), 0)
            self.assertEqual(active_assignment_workloads([self.ids["staff"]])[self.ids["staff"]], 2)

    def test_assignment_fails_closed_when_staff_lock_protocol_is_unavailable(self):
        complaint_id = self.make_complaint()
        self.login_as("head")
        with patch("app.lock_assignment_staff_rows", return_value=None):
            response = self.client.post(f"/assign-complaint/{complaint_id}", data={
                "csrf_token": "test-csrf", "department_id": str(self.ids["drainage"]),
                "staff_id": str(self.ids["staff"]),
            }, follow_redirects=True)
        self.assertIn(b"cannot be safely changed under the current database isolation level", response.data)
        with app.app_context():
            state = ComplaintDepartment.query.filter_by(
                complaint_id=complaint_id, department_id=self.ids["drainage"],
            ).one()
            self.assertIsNone(state.assigned_staff_id)
            self.assertEqual(ComplaintAssignment.query.filter_by(complaint_id=complaint_id).count(), 0)

    def test_resolved_assignments_do_not_consume_workload_capacity(self):
        self.make_complaint(status="Resolved", assigned_staff_id=self.ids["staff"])
        complaint_id = self.make_complaint()
        self.login_as("head")
        response = self.client.post(f"/assign-complaint/{complaint_id}", data={
            "csrf_token": "test-csrf", "department_id": str(self.ids["drainage"]),
            "staff_id": str(self.ids["staff"]),
        })
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            self.assertEqual(active_assignment_workloads([self.ids["staff"]])[self.ids["staff"]], 1)

    def test_ignored_complaint_pauses_workload_and_restore_reactivates_it(self):
        complaint_id = self.make_complaint(status="Assigned", assigned_staff_id=self.ids["staff"])
        self.login_as("head")
        ignored = self.client.post(f"/ignore-complaint/{complaint_id}", data={
            "csrf_token": "test-csrf", "reason": "Synthetic test pause",
        })
        self.assertEqual(ignored.status_code, 302)
        with app.app_context():
            self.assertEqual(active_assignment_workloads([self.ids["staff"]])[self.ids["staff"]], 0)
        restored = self.client.post(f"/restore-complaint/{complaint_id}", data={
            "csrf_token": "test-csrf",
        })
        self.assertEqual(restored.status_code, 302)
        with app.app_context():
            self.assertEqual(active_assignment_workloads([self.ids["staff"]])[self.ids["staff"]], 1)

    def test_restore_is_blocked_when_reactivation_would_exceed_staff_limit(self):
        self.make_complaint(status="Assigned", assigned_staff_id=self.ids["staff"])
        self.make_complaint(status="Assigned", assigned_staff_id=self.ids["staff"])
        paused_id = self.make_complaint(status="Assigned", assigned_staff_id=self.ids["staff"])
        self.login_as("head")
        ignored = self.client.post(f"/ignore-complaint/{paused_id}", data={
            "csrf_token": "test-csrf", "reason": "Synthetic test pause",
        })
        self.assertEqual(ignored.status_code, 302)
        with app.app_context():
            history_count = ComplaintStatusHistory.query.filter_by(complaint_id=paused_id).count()
        restored = self.client.post(f"/restore-complaint/{paused_id}", data={
            "csrf_token": "test-csrf",
        }, follow_redirects=True)
        self.assertIn(b"one or more assigned staff members are at capacity", restored.data)
        with app.app_context():
            complaint = db.session.get(Complaint, paused_id)
            state = ComplaintDepartment.query.filter_by(
                complaint_id=paused_id, department_id=self.ids["drainage"],
            ).one()
            self.assertEqual(complaint.status, "Ignored")
            self.assertEqual(state.assigned_staff_id, self.ids["staff"])
            self.assertEqual(ComplaintStatusHistory.query.filter_by(complaint_id=paused_id).count(), history_count)
            self.assertEqual(active_assignment_workloads([self.ids["staff"]])[self.ids["staff"]], 2)

    def test_restore_is_rejected_when_assigned_staff_is_inactive(self):
        paused_id = self.make_complaint(status="Assigned", assigned_staff_id=self.ids["staff"])
        self.login_as("head")
        ignored = self.client.post(f"/ignore-complaint/{paused_id}", data={
            "csrf_token": "test-csrf", "reason": "Synthetic test pause",
        })
        self.assertEqual(ignored.status_code, 302)
        with app.app_context():
            staff = db.session.get(User, self.ids["staff"])
            staff.is_active = False
            db.session.commit()
            history_count = ComplaintStatusHistory.query.filter_by(complaint_id=paused_id).count()

        restored = self.client.post(f"/restore-complaint/{paused_id}", data={
            "csrf_token": "test-csrf",
        }, follow_redirects=True)
        self.assertIn(b"cannot be restored until its assigned staff eligibility is valid", restored.data)
        with app.app_context():
            complaint = db.session.get(Complaint, paused_id)
            state = ComplaintDepartment.query.filter_by(
                complaint_id=paused_id, department_id=self.ids["drainage"],
            ).one()
            self.assertEqual(complaint.status, "Ignored")
            self.assertEqual(state.status, "Assigned")
            self.assertEqual(state.assigned_staff_id, self.ids["staff"])
            self.assertEqual(ComplaintStatusHistory.query.filter_by(complaint_id=paused_id).count(), history_count)
            self.assertEqual(active_assignment_workloads([self.ids["staff"]])[self.ids["staff"]], 0)

    def test_same_staff_reassignment_does_not_double_count_current_department(self):
        existing_id = self.make_complaint(status="Assigned", assigned_staff_id=self.ids["staff"])
        self.make_complaint(status="Assigned", assigned_staff_id=self.ids["staff"])
        with app.app_context():
            self.assertEqual(
                active_assignment_workload(
                    self.ids["staff"], exclude_complaint_id=existing_id,
                    exclude_department_id=self.ids["drainage"],
                ),
                1,
            )
        self.login_as("head")
        response = self.client.post(f"/assign-complaint/{existing_id}", data={
            "csrf_token": "test-csrf", "department_id": str(self.ids["drainage"]),
            "staff_id": str(self.ids["staff"]),
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"is already assigned", response.data)

    def test_reassignment_to_full_staff_is_rejected_without_changing_history(self):
        complaint_id = self.make_complaint(status="Assigned", assigned_staff_id=self.ids["staff2"])
        self.make_complaint(status="Assigned", assigned_staff_id=self.ids["staff"])
        self.make_complaint(status="Assigned", assigned_staff_id=self.ids["staff"])
        with app.app_context():
            before_history = ComplaintAssignment.query.filter_by(complaint_id=complaint_id).count()
        self.login_as("admin")
        response = self.client.post(f"/assign-complaint/{complaint_id}", data={
            "csrf_token": "test-csrf", "department_id": str(self.ids["drainage"]),
            "staff_id": str(self.ids["staff"]),
        }, follow_redirects=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"already has 2 active assignments", response.data)
        with app.app_context():
            state = ComplaintDepartment.query.filter_by(
                complaint_id=complaint_id, department_id=self.ids["drainage"],
            ).one()
            self.assertEqual(state.assigned_staff_id, self.ids["staff2"])
            self.assertEqual(ComplaintAssignment.query.filter_by(complaint_id=complaint_id).count(), before_history)

    def test_legacy_primary_assignee_is_not_double_counted_with_department_row(self):
        self.make_complaint(status="Assigned", assigned_staff_id=self.ids["staff"])
        with app.app_context():
            self.assertEqual(active_assignment_workloads([self.ids["staff"]])[self.ids["staff"]], 1)

    def test_assignment_dropdown_shows_and_disables_at_capacity_candidate(self):
        self.make_complaint(status="Assigned", assigned_staff_id=self.ids["staff"])
        self.make_complaint(status="Assigned", assigned_staff_id=self.ids["staff"])
        self.make_complaint()
        self.login_as("admin")
        response = self.client.get("/admin/complaints")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Test Staff | 2/2 active assignments", response.data)
        self.assertIn(b"disabled>Test Staff | 2/2 active assignments", response.data)

    def test_admin_can_assign_distinct_active_staff_to_each_department(self):
        complaint_id, water_staff_id, health_staff_id = self._make_department_wise_fixture()
        self.login_as("admin")
        targets = [
            (self.department_ids["Drainage"], self.ids["staff"]),
            (self.department_ids["Water Supply"], water_staff_id),
            (self.department_ids["Public Health"], health_staff_id),
        ]
        for department_id, staff_id in targets:
            response = self.client.post(f"/assign-complaint/{complaint_id}", data={
                "csrf_token": "test-csrf", "department_id": str(department_id), "staff_id": str(staff_id),
            })
            self.assertEqual(response.status_code, 302)
        admin_dashboard = self.client.get("/admin/complaints")
        self.assertEqual(admin_dashboard.status_code, 200)
        for department_name in ("Drainage", "Water Supply", "Public Health"):
            self.assertIn(f"Staff in {department_name}".encode(), admin_dashboard.data)
        with app.app_context():
            states = ComplaintDepartment.query.filter_by(complaint_id=complaint_id).all()
            self.assertEqual({(row.department_id, row.assigned_staff_id) for row in states}, set(targets))
            events = ComplaintAssignment.query.filter_by(complaint_id=complaint_id).all()
            self.assertEqual({(row.department_id, row.staff_id) for row in events}, set(targets))
            self.assertEqual(len({row.department_id for row in events}), 3)
            workloads = active_assignment_workloads([staff_id for _, staff_id in targets])
            self.assertEqual([workloads[staff_id] for _, staff_id in targets], [1, 1, 1])

    def test_capacity_limit_applies_to_every_linked_department(self):
        complaint_id, _, _ = self._make_department_wise_fixture()
        with app.app_context():
            # Create two active Water Supply assignments for this eligible staff.
            department_id = self.department_ids["Water Supply"]
            staff = User(
                name="Capacity Water Fixture", email="capacity-water@example.test", phone="304",
                password=generate_password_hash("test-password"), role="staff",
                department_id=department_id, is_active=True,
            )
            db.session.add(staff)
            db.session.flush()
            capacity_staff_id = staff.id
            for index in range(2):
                complaint = Complaint(
                    complaint_text=f"Synthetic water workload {index}", category="Water Supply",
                    location="Fixture location", priority="Low", status="Assigned",
                    citizen_id=self.ids["citizen"], department_id=department_id,
                )
                complaint.department_assignments.append(ComplaintDepartment(
                    department_id=department_id, assigned_staff_id=capacity_staff_id,
                    status="Assigned", staff_assigned_date=utcnow(),
                ))
                db.session.add(complaint)
            db.session.commit()
        self.login_as("admin")
        response = self.client.post(f"/assign-complaint/{complaint_id}", data={
            "csrf_token": "test-csrf", "department_id": str(self.department_ids["Water Supply"]),
            "staff_id": str(capacity_staff_id),
        }, follow_redirects=True)
        self.assertIn(b"already has 2 active assignments", response.data)
        with app.app_context():
            water_state = ComplaintDepartment.query.filter_by(
                complaint_id=complaint_id, department_id=self.department_ids["Water Supply"],
            ).one()
            self.assertIsNone(water_state.assigned_staff_id)
            self.assertEqual(ComplaintAssignment.query.filter_by(complaint_id=complaint_id).count(), 0)

    def test_legacy_three_department_assignment_reassignment_status_and_tracking_are_independent(self):
        complaint_id, water_staff_id, health_staff_id = self._make_department_wise_fixture(
            category="Multiple Departments",
        )
        with app.app_context():
            replacement = User(
                name="Replacement Water Fixture", email="water-replacement@example.test", phone="303",
                password=generate_password_hash("test-password"), role="staff",
                department_id=self.department_ids["Water Supply"], is_active=True,
            )
            db.session.add(replacement)
            db.session.commit()
            replacement_staff_id = replacement.id

        targets = [
            ("Drainage", self.ids["staff"]),
            ("Water Supply", water_staff_id),
            ("Public Health", health_staff_id),
        ]
        self.login_as("admin")
        for department_name, staff_id in targets:
            response = self.client.post(f"/assign-complaint/{complaint_id}", data={
                "csrf_token": "test-csrf",
                "department_id": str(self.department_ids[department_name]),
                "staff_id": str(staff_id),
            })
            self.assertEqual(response.status_code, 302)

        invalid = self.client.post(f"/assign-complaint/{complaint_id}", data={
            "csrf_token": "test-csrf", "department_id": str(self.department_ids["Water Supply"]),
            "staff_id": str(health_staff_id),
        })
        self.assertEqual(invalid.status_code, 403)
        reassigned = self.client.post(f"/assign-complaint/{complaint_id}", data={
            "csrf_token": "test-csrf", "department_id": str(self.department_ids["Water Supply"]),
            "staff_id": str(replacement_staff_id),
        })
        self.assertEqual(reassigned.status_code, 302)

        selected_staff = {
            "Drainage": self.ids["staff"],
            "Water Supply": replacement_staff_id,
            "Public Health": health_staff_id,
        }
        for department_name, staff_id in selected_staff.items():
            department_id = self.department_ids[department_name]
            start = self.client.post(f"/update-status/{complaint_id}", data={
                "csrf_token": "test-csrf", "department_id": str(department_id),
                "status": "In Progress",
            })
            self.assertEqual(start.status_code, 302)
            resolve = self.client.post(f"/update-status/{complaint_id}", data={
                "csrf_token": "test-csrf", "department_id": str(department_id),
                "status": "Resolved", "remarks": f"{department_name} completed",
            })
            self.assertEqual(resolve.status_code, 302)
            with app.app_context():
                complaint = db.session.get(Complaint, complaint_id)
                state = ComplaintDepartment.query.filter_by(
                    complaint_id=complaint_id, department_id=department_id,
                ).one()
                self.assertEqual(state.assigned_staff_id, staff_id)
                self.assertEqual(state.status, "Resolved")
                self.assertEqual(state.resolution_note, f"{department_name} completed")
                if department_name != "Public Health":
                    self.assertNotEqual(complaint.status, "Resolved")
                else:
                    self.assertEqual(complaint.status, "Resolved")

            if department_name == "Drainage":
                # Citizen tracking must show the first department's completion
                # without summarizing the whole complaint as resolved.
                self.login_as("citizen")
                partial_tracking = self.client.get(f"/track-complaint/{complaint_id}")
                self.assertEqual(partial_tracking.status_code, 200)
                self.assertIn(b'class="status-badge lg assigned">Assigned', partial_tracking.data)
                self.assertIn(b"Drainage completed", partial_tracking.data)
                self.assertIn(b"Water Supply", partial_tracking.data)
                self.assertIn(b"Public Health", partial_tracking.data)
                self.login_as("admin")

        with app.app_context():
            events = ComplaintAssignment.query.filter_by(complaint_id=complaint_id).all()
            water_events = [row for row in events if row.department_id == self.department_ids["Water Supply"]]
            self.assertEqual([row.staff_id for row in water_events], [water_staff_id, replacement_staff_id])
            self.assertEqual(len(events), 4)
            states = ComplaintDepartment.query.filter_by(complaint_id=complaint_id).all()
            self.assertEqual(len(states), 3)

        self.login_as("citizen")
        tracking = self.client.get(f"/track-complaint/{complaint_id}")
        self.assertEqual(tracking.status_code, 200)
        for department_name in ("Drainage", "Water Supply", "Public Health"):
            self.assertIn(department_name.encode(), tracking.data)
        self.assertIn(b"Replacement Water Fixture", tracking.data)
        for department_name in ("Drainage", "Water Supply", "Public Health"):
            self.assertIn(f"Resolution note: {department_name} completed".encode(), tracking.data)
        self.assertIn(b'class="status-badge lg resolved">Resolved', tracking.data)

    def test_department_head_cannot_assign_another_department_or_its_staff(self):
        complaint_id, water_staff_id, _ = self._make_department_wise_fixture()
        self.login_as("head")
        response = self.client.post(f"/assign-complaint/{complaint_id}", data={
            "csrf_token": "test-csrf", "department_id": str(self.department_ids["Water Supply"]),
            "staff_id": str(water_staff_id),
        })
        self.assertEqual(response.status_code, 403)
        response = self.client.post(f"/assign-complaint/{complaint_id}", data={
            "csrf_token": "test-csrf", "department_id": str(self.department_ids["Drainage"]),
            "staff_id": str(water_staff_id),
        })
        self.assertEqual(response.status_code, 403)

    def test_inactive_staff_cannot_be_assigned(self):
        complaint_id = self.make_complaint()
        with app.app_context():
            staff = db.session.get(User, self.ids["staff"])
            staff.is_active = False
            db.session.commit()
        self.login_as("head")
        response = self.client.post(f"/assign-complaint/{complaint_id}", data={
            "csrf_token": "test-csrf", "department_id": str(self.department_ids["Drainage"]),
            "staff_id": str(self.ids["staff"]),
        })
        self.assertEqual(response.status_code, 403)
        with app.app_context():
            state = ComplaintDepartment.query.filter_by(
                complaint_id=complaint_id, department_id=self.department_ids["Drainage"],
            ).one()
            self.assertIsNone(state.assigned_staff_id)
            self.assertEqual(ComplaintAssignment.query.filter_by(complaint_id=complaint_id).count(), 0)

    def test_staff_with_open_department_work_cannot_be_deactivated_or_moved(self):
        complaint_id = self.make_complaint(status="Assigned", assigned_staff_id=self.ids["staff"])
        self.login_as("admin")
        deactivate = self.client.post("/admin/manage", data={
            "action": "set_staff_active", "csrf_token": "test-csrf",
            "user_id": str(self.ids["staff"]), "is_active": "false",
        })
        move = self.client.post("/admin/manage", data={
            "action": "assign_user", "user_id": str(self.ids["staff"]),
            "role": "staff", "department_id": str(self.department_ids["Police"]),
        })
        self.assertEqual(deactivate.status_code, 302)
        self.assertEqual(move.status_code, 302)
        with app.app_context():
            staff = db.session.get(User, self.ids["staff"])
            self.assertTrue(staff.is_active)
            self.assertEqual(staff.department_id, self.department_ids["Drainage"])
            state = ComplaintDepartment.query.filter_by(
                complaint_id=complaint_id, department_id=self.department_ids["Drainage"],
            ).one()
            self.assertEqual(state.assigned_staff_id, self.ids["staff"])

    def test_legacy_primary_assignment_without_department_state_protects_staff(self):
        complaint_id = self.make_complaint(status="Assigned", assigned_staff_id=self.ids["staff"])
        with app.app_context():
            ComplaintDepartment.query.filter_by(
                complaint_id=complaint_id,
                department_id=self.department_ids["Drainage"],
            ).delete()
            db.session.commit()

        self.login_as("admin")
        deactivate = self.client.post("/admin/manage", data={
            "action": "set_staff_active", "csrf_token": "test-csrf",
            "user_id": str(self.ids["staff"]), "is_active": "false",
        })
        move = self.client.post("/admin/manage", data={
            "action": "assign_user", "user_id": str(self.ids["staff"]),
            "role": "staff", "department_id": str(self.department_ids["Police"]),
        })
        self.assertEqual(deactivate.status_code, 302)
        self.assertEqual(move.status_code, 302)
        with app.app_context():
            staff = db.session.get(User, self.ids["staff"])
            complaint = db.session.get(Complaint, complaint_id)
            self.assertTrue(staff.is_active)
            self.assertEqual(staff.department_id, self.department_ids["Drainage"])
            self.assertEqual(complaint.assigned_staff_id, staff.id)

    def test_migrated_legacy_primary_state_keeps_direct_assignee_without_invented_time(self):
        with app.app_context():
            complaint = Complaint(
                complaint_text="Legacy assignment state", category="Drainage",
                location="Fixture location", priority="Low", status="Assigned",
                citizen_id=self.ids["citizen"], department_id=self.department_ids["Drainage"],
                assigned_staff_id=self.ids["staff"],
            )
            complaint.department_assignments.append(ComplaintDepartment(
                department_id=self.department_ids["Drainage"], status="Assigned",
            ))
            db.session.add(complaint)
            db.session.commit()
            complaint_id = complaint.id
        self.login_as("citizen")
        response = self.client.get(f"/track-complaint/{complaint_id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Assigned to Test Staff</small>", response.data)

    def test_department_status_update_does_not_change_other_department_state(self):
        complaint_id, water_staff_id, _ = self._make_department_wise_fixture()
        self.login_as("admin")
        for department_id, staff_id in [
            (self.department_ids["Drainage"], self.ids["staff"]),
            (self.department_ids["Water Supply"], water_staff_id),
        ]:
            self.client.post(f"/assign-complaint/{complaint_id}", data={
                "csrf_token": "test-csrf", "department_id": str(department_id), "staff_id": str(staff_id),
            })
        self.login_as("staff")
        response = self.client.post(f"/update-status/{complaint_id}", data={
            "csrf_token": "test-csrf", "department_id": str(self.department_ids["Drainage"]),
            "status": "In Progress",
        })
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            states = {
                row.department_id: row.status
                for row in ComplaintDepartment.query.filter_by(complaint_id=complaint_id).all()
            }
            self.assertEqual(states[self.department_ids["Drainage"]], "In Progress")
            self.assertEqual(states[self.department_ids["Water Supply"]], "Assigned")
            self.assertEqual(states[self.department_ids["Public Health"]], "Submitted")
            self.assertEqual(db.session.get(Complaint, complaint_id).status, "In Progress")

    def test_staff_dashboard_shows_only_the_staff_members_department_assignment(self):
        complaint_id, water_staff_id, _ = self._make_department_wise_fixture()
        self.login_as("admin")
        for department_id, staff_id in [
            (self.department_ids["Drainage"], self.ids["staff"]),
            (self.department_ids["Water Supply"], water_staff_id),
        ]:
            self.client.post(f"/assign-complaint/{complaint_id}", data={
                "csrf_token": "test-csrf", "department_id": str(department_id), "staff_id": str(staff_id),
            })
        self.login_as("staff")
        with self.client.session_transaction() as state:
            state.pop("_flashes", None)
        drainage_dashboard = self.client.get("/staff-dashboard")
        self.assertEqual(drainage_dashboard.status_code, 200)
        self.assertIn(f"CMP{complaint_id:03d}".encode(), drainage_dashboard.data)
        self.assertNotIn(b"Water Assignment Fixture", drainage_dashboard.data)
        with self.client.session_transaction() as state:
            state["user_id"] = water_staff_id
            state["role"] = "staff"
            state["csrf_token"] = "test-csrf"
            state.pop("_flashes", None)
        water_dashboard = self.client.get("/staff-dashboard")
        self.assertEqual(water_dashboard.status_code, 200)
        self.assertIn(f"CMP{complaint_id:03d}".encode(), water_dashboard.data)
        self.assertIn(b"Water Assignment Fixture", water_dashboard.data)
        self.assertNotIn(b"Assigned: Test Staff", water_dashboard.data)

    def test_citizen_tracking_shows_department_staff_and_assignment_times(self):
        complaint_id, water_staff_id, health_staff_id = self._make_department_wise_fixture()
        self.login_as("admin")
        for department_id, staff_id in [
            (self.department_ids["Drainage"], self.ids["staff"]),
            (self.department_ids["Water Supply"], water_staff_id),
            (self.department_ids["Public Health"], health_staff_id),
        ]:
            self.client.post(f"/assign-complaint/{complaint_id}", data={
                "csrf_token": "test-csrf", "department_id": str(department_id), "staff_id": str(staff_id),
            })
        self.login_as("citizen")
        response = self.client.get(f"/track-complaint/{complaint_id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Drainage", response.data)
        self.assertIn(b"Water Supply", response.data)
        self.assertIn(b"Public Health", response.data)
        self.assertIn(b"Test Staff", response.data)
        self.assertIn(b"Water Assignment Fixture", response.data)
        self.assertIn(b"Health Assignment Fixture", response.data)
        self.assertIn(b"Assigned", response.data)
        with app.app_context():
            self.assertEqual(ComplaintAssignment.query.filter_by(complaint_id=complaint_id).count(), 3)

    def test_legacy_single_department_complaint_remains_trackable_without_link_row(self):
        with app.app_context():
            complaint = Complaint(
                complaint_text="Legacy single-department complaint",
                category="Drainage", location="Legacy fixture", priority="Low",
                status="Assigned", citizen_id=self.ids["citizen"],
                department_id=self.department_ids["Drainage"],
                assigned_staff_id=self.ids["staff"],
            )
            db.session.add(complaint)
            db.session.commit()
            complaint_id = complaint.id
        self.login_as("citizen")
        response = self.client.get(f"/track-complaint/{complaint_id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Drainage", response.data)
        self.assertIn(b"Test Staff", response.data)
        self.assertIn(b"Test Staff", self.client.get("/citizen-dashboard").data)

    def test_missing_canonical_department_rejects_before_saving_upload_or_complaint(self):
        self.login_as("citizen")
        with app.app_context():
            before_count = Complaint.query.count()

        with app.app_context():
            with patch.object(Department, "query") as department_query:
                department_query.filter.return_value.all.return_value = []
                with patch("werkzeug.datastructures.FileStorage.save") as save_upload:
                    response = self.client.post("/submit-complaint", data={
                        "complaint_text": "A blocked drain is causing waterlogging",
                        "location": "Synthetic test location",
                        "image": (BytesIO(b"synthetic image bytes"), "synthetic.jpg"),
                    }, content_type="multipart/form-data")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"One or more responsible departments are unavailable", response.data)
        save_upload.assert_not_called()
        with app.app_context():
            self.assertEqual(Complaint.query.count(), before_count)

    def test_submission_ignores_client_supplied_category_and_department(self):
        self.login_as("citizen")
        response = self.client.post("/submit-complaint", data={
            "complaint_text": "A blocked drain is causing waterlogging",
            "location": "Synthetic test location",
            "category": "Fire",
            "department_id": str(self.departments["Fire Department"].id),
        })
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            complaint = Complaint.query.order_by(Complaint.id.desc()).first()
            self.assertEqual(complaint.category, "Drainage")
            self.assertEqual(complaint.department.name, "Drainage")

    def test_vercel_disables_local_image_storage_without_blocking_text_complaints(self):
        self.assertFalse(image_uploads_enabled_from_environment({
            "VERCEL": "1", "SPSFRMS_IMAGE_UPLOADS_ENABLED": "1",
        }))
        self.assertFalse(image_uploads_enabled_from_environment({}))
        self.assertTrue(image_uploads_enabled_from_environment({
            "SPSFRMS_IMAGE_UPLOADS_ENABLED": "1",
        }))
        self.assertFalse(image_uploads_enabled_from_environment({
            "SPSFRMS_IMAGE_UPLOADS_ENABLED": "0",
        }))

        self.login_as("citizen")
        with tempfile.TemporaryDirectory() as upload_directory:
            with patch.dict(app.config, {
                "IMAGE_UPLOADS_ENABLED": False,
                "UPLOAD_FOLDER": upload_directory,
            }):
                form = self.client.get("/submit-complaint")
                self.assertNotIn(b'name="image"', form.data)
                self.assertIn(b"Text-only complaint submission is available", form.data)

                with app.app_context():
                    before = Complaint.query.count()
                image_response = self.client.post("/submit-complaint", data={
                    "complaint_text": "A blocked drain is causing waterlogging",
                    "location": "Synthetic test location",
                    "image": (BytesIO(b"synthetic image bytes"), "synthetic.jpg"),
                }, content_type="multipart/form-data")
                self.assertEqual(image_response.status_code, 200)
                self.assertIn(b"Image uploads are disabled", image_response.data)
                with app.app_context():
                    self.assertEqual(Complaint.query.count(), before)
                self.assertEqual(os.listdir(upload_directory), [])

                text_response = self.client.post("/submit-complaint", data={
                    "complaint_text": "A blocked drain is causing waterlogging",
                    "location": "Synthetic test location",
                })
                self.assertEqual(text_response.status_code, 302)
                with app.app_context():
                    complaint = Complaint.query.order_by(Complaint.id.desc()).first()
                    self.assertIsNone(complaint.image)
                self.assertEqual(os.listdir(upload_directory), [])

    def test_submission_rejects_descriptions_longer_than_preview_limit(self):
        self.login_as("citizen")
        with app.app_context():
            before = Complaint.query.count()
        response = self.client.post("/submit-complaint", data={
            "complaint_text": "drainage " + ("x" * 5000),
            "location": "Test Road",
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"5,000 characters or fewer", response.data)
        with app.app_context():
            self.assertEqual(Complaint.query.count(), before)

    def test_staff_recommendation_favors_lower_workload(self):
        self.make_complaint(status="Assigned", assigned_staff_id=self.ids["staff"])
        self.make_complaint()
        self.login_as("head")
        with patch("app.load_model_artifact", return_value=None):
            response = self.client.get("/officer-dashboard")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Recommended</option>", response.data)
        self.assertIn(b"Lowest active workload (fallback)", response.data)

    def test_recommendation_fallback_obeys_department_role_and_active_eligibility(self):
        inactive_same_department = SimpleNamespace(
            id=999, name="Inactive Fixture", role="staff",
            department_id=self.ids["drainage"], is_active=False,
        )
        candidates = [self.staff, self.staff2, self.other_staff, self.head, inactive_same_department]
        recommendation = workload_recommendation(
            candidates, self.ids["drainage"],
            {self.ids["staff"]: 3, self.ids["staff2"]: 1, self.ids["other_staff"]: 0},
        )
        self.assertEqual(recommendation["staff_id"], self.ids["staff2"])
        self.assertEqual(recommendation["method"], "workload_fallback")
        self.assertEqual(recommendation["workload"], 1)

    def test_ml_ranking_filters_other_departments_and_inactive_staff_and_uses_workload(self):
        inactive_same_department = SimpleNamespace(
            id=999, name="Inactive Fixture", role="staff",
            department_id=self.ids["drainage"], is_active=False,
        )

        class FakeClassifier:
            classes_ = [self.ids["staff"], self.ids["staff2"], self.ids["other_staff"], 999]

        class FakeModel:
            named_steps = {"classifier": FakeClassifier()}

            def predict_proba(self, rows):
                self.assertion_rows = rows
                return [[0.90, 0.80, 0.99, 1.0]]

        artifact = {"models": {str(self.ids["drainage"]): FakeModel()}}
        recommendation = recommend_staff(
            "fixture complaint text", "Drainage", self.ids["drainage"],
            [self.staff, self.staff2, self.other_staff, inactive_same_department],
            {self.ids["staff"]: 10, self.ids["staff2"]: 0}, artifact,
        )
        self.assertEqual(recommendation["staff_id"], self.ids["staff2"])
        self.assertEqual(recommendation["method"], "ml_workload")
        self.assertEqual(recommendation["confidence"], 0.80)
        self.assertEqual(recommendation["workload"], 0)

    def test_ml_recommendation_failure_or_missing_eligible_model_uses_workload_fallback(self):
        class BrokenClassifier:
            classes_ = [self.ids["staff"]]

        class BrokenModel:
            named_steps = {"classifier": BrokenClassifier()}

            def predict_proba(self, rows):
                raise RuntimeError("synthetic model failure")

        staff = [self.staff, self.staff2]
        workloads = {self.ids["staff"]: 2, self.ids["staff2"]: 0}
        artifact = {"models": {str(self.ids["drainage"]): BrokenModel()}}
        broken = recommend_staff("fixture", "Drainage", self.ids["drainage"], staff, workloads, artifact)
        missing = recommend_staff("fixture", "Drainage", self.ids["drainage"], staff, workloads, {"models": {}})
        class OtherDepartmentClassifier:
            classes_ = [self.ids["other_staff"]]

        class OtherDepartmentModel:
            named_steps = {"classifier": OtherDepartmentClassifier()}

            def predict_proba(self, rows):
                return [[1.0]]

        ineligible_label = recommend_staff(
            "fixture", "Drainage", self.ids["drainage"], staff, workloads,
            {"models": {str(self.ids["drainage"]): OtherDepartmentModel()}},
        )
        self.assertEqual(broken["method"], "workload_fallback")
        self.assertEqual(missing["staff_id"], self.ids["staff2"])
        self.assertEqual(ineligible_label["method"], "workload_fallback")

    def test_historical_assignment_validation_ignores_current_staff_fields(self):
        base = {
            "assignment_id": 1, "complaint_id": 1, "staff_id": 1,
            "complaint_department_id": 1, "staff_department_id": 99,
            "staff_role": "former_staff", "staff_is_active": False,
            "complaint_department": "Drainage", "staff_department": "Other current unit",
            "record_provenance": "verified_assignment",
            "reconstruction_status": "not_reconstructed",
            "provenance_evidence_ref": "synthetic-provenance-evidence",
            "eligibility_verified": True,
            "eligibility_evidence_ref": "synthetic-eligibility-evidence",
            "label_kind": "historical_assignment_choice",
            "assigned_at": datetime(2026, 1, 1, tzinfo=timezone.utc),
            "complaint_text": "synthetic fixture", "category": "Drainage",
        }
        valid, rejected = validate_assignment_records([base])
        self.assertEqual([row["assignment_id"] for row in valid], [1])
        self.assertEqual(rejected, {})

        current_fields_without_historical_evidence = dict(
            base,
            staff_department_id=1,
            staff_role="officer",
            staff_is_active=True,
            eligibility_verified=False,
            eligibility_evidence_ref=None,
        )
        valid, rejected = validate_assignment_records(
            [current_fields_without_historical_evidence]
        )
        self.assertEqual(valid, [])
        self.assertEqual(rejected["historical_eligibility_unverified"], 1)

        invalid_timestamp = dict(base, assigned_at=None)
        valid, rejected = validate_assignment_records([invalid_timestamp])
        self.assertEqual(valid, [])
        self.assertEqual(rejected["invalid_assignment_timestamp"], 1)
        with self.assertRaises(InsufficientTrainingData):
            train_staff_models([base])
    def test_chronological_holdout_keeps_each_complaint_in_one_split(self):
        records = []
        for complaint_id, day in ((1, 1), (2, 2), (3, 3), (4, 4), (5, 5)):
            for offset in (0, 1):
                records.append({
                    "complaint_id": complaint_id,
                    "assigned_at": datetime(2026, 1, day, 10 + offset),
                })
        training, held_out = chronological_group_split(records, test_fraction=0.2)
        self.assertFalse({row["complaint_id"] for row in training} & {row["complaint_id"] for row in held_out})
        self.assertEqual({row["complaint_id"] for row in held_out}, {5})

    def test_historical_workload_baseline_requires_complete_ordered_history(self):
        assignments = [
            {"assignment_id": 1, "complaint_id": 10, "staff_id": 20, "assigned_at": datetime(2026, 1, 1, 10)},
            {"assignment_id": 2, "complaint_id": 11, "staff_id": 21, "assigned_at": datetime(2026, 1, 1, 11)},
        ]
        statuses = [{
            "history_id": 1, "complaint_id": 10, "new_status": "Resolved",
            "timestamp": datetime(2026, 1, 1, 10, 30),
        }]
        workloads, error = reconstruct_historical_workloads(
            assignments, statuses, {10: "Resolved", 11: "Assigned"},
        )
        self.assertIsNone(error)
        self.assertEqual(workloads[2].get(20, 0), 0)
        unavailable, reason = reconstruct_historical_workloads(
            assignments, [], {10: "Resolved", 11: "Assigned"},
        )
        self.assertIsNone(unavailable)
        self.assertEqual(reason, "terminal_status_history_gap")

    def test_offline_training_module_does_not_import_database_or_application(self):
        command = r"""
import builtins, sys
blocked = {'app', 'database_config', 'sqlalchemy', 'flask_sqlalchemy'}
original_import = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.split('.')[0] in blocked:
        raise AssertionError('database/application import attempted')
    return original_import(name, *args, **kwargs)
builtins.__import__ = guarded
import train_staff_recommendation as trainer
assert not any(name.split('.')[0] in blocked for name in sys.modules)
assert trainer.main(['--dataset', 'definitely-missing-local-training-data.csv']) == 1
assert not any(name.split('.')[0] in blocked for name in sys.modules)
assert trainer.main(['--validate-only', '--dataset', 'definitely-missing-local-training-data.csv']) == 1
assert not any(name.split('.')[0] in blocked for name in sys.modules)
"""
        environment = os.environ.copy()
        environment["DATABASE_URL"] = "this-value-must-never-be-read"
        result = subprocess.run(
            [sys.executable, "-c", command],
            cwd=os.path.dirname(os.path.dirname(__file__)),
            env=environment, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('"artifact_saved": false', result.stdout)
        self.assertIn('"status": "model_not_produced"', result.stdout)

    def test_missing_and_insufficient_local_datasets_do_not_create_artifacts(self):
        import train_staff_recommendation as trainer
        trainer_main = trainer.main
        with tempfile.TemporaryDirectory() as directory:
            artifact = os.path.join(directory, "staff-model.joblib")
            missing = os.path.join(directory, "missing.csv")
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(trainer_main(["--dataset", missing, "--artifact-path", artifact]), 1)
            self.assertIn('"status": "model_not_produced"', output.getvalue())
            self.assertFalse(os.path.exists(artifact))

            dataset = os.path.join(directory, "small.json")
            row = {
                "assignment_id": 1, "complaint_id": 11, "staff_id": 21,
                "assigned_at": "2026-01-01T10:00:00Z", "complaint_text": "synthetic test fixture",
                "category": "Drainage", "complaint_department_id": 2,
                "complaint_department": "Drainage", "staff_role": "staff",
                "staff_department_id": 2, "staff_department": "Drainage", "staff_is_active": True,
            }
            review = {"format_version": 2, "records": [{
                "assignment_id": 1, "complaint_id": 11, "staff_id": 21,
                "assigned_at": "2026-01-01T10:00:00Z", "provenance": "verified_assignment",
                "reconstruction_status": "not_reconstructed",
                "provenance_evidence_ref": "synthetic-provenance-evidence",
                "eligibility_verified": True, "eligibility_evidence_ref": "synthetic-eligibility-evidence",
                "label_kind": "historical_assignment_choice",
                "candidate_staff_ids": [21],
                "candidate_roster_department_id": row["complaint_department_id"],
                "candidate_roster_reviewed": True,
                "candidate_staff_eligibility_review": {str(candidate): True for candidate in [21]},
                "candidate_roster_at": "2026-01-01T10:00:00Z",
                "candidate_roster_evidence_ref": "synthetic-roster-evidence",
            }]}
            review['dataset_fingerprint'] = trainer.dataset_fingerprint([row])
            review['source_identity'] = 'synthetic-source'
            with open(dataset, "w", encoding="utf-8") as handle:
                json.dump({"format_version": 1, "assignments": [row], "review_manifest": review}, handle)
            output = StringIO()
            with redirect_stdout(output):
                self.assertEqual(trainer_main(["--dataset", dataset, "--source-identity", "synthetic-source", "--artifact-path", artifact]), 1)
            self.assertIn('"status": "insufficient_data"', output.getvalue())
            self.assertFalse(os.path.exists(artifact))

    def test_validation_only_summarizes_without_fitting_or_touching_artifact(self):
        import train_staff_recommendation as trainer
        with tempfile.TemporaryDirectory() as directory:
            dataset = os.path.join(directory, "validation-only.json")
            artifact = os.path.join(directory, "existing-model.joblib")
            private_fixture_text = "DO_NOT_PRINT_TEST_COMPLAINT_TEXT_94721"
            row = {
                "assignment_id": 1, "complaint_id": 11, "staff_id": 987654321,
                "assigned_at": "2026-01-01T10:00:00Z", "complaint_text": private_fixture_text,
                "category": "Drainage", "complaint_department_id": 2,
                "complaint_department": "Drainage", "staff_role": "staff",
                "staff_department_id": 2, "staff_department": "Drainage", "staff_is_active": True,
            }
            review = {"format_version": 2, "records": [{
                "assignment_id": 1, "complaint_id": 11, "staff_id": 987654321,
                "assigned_at": "2026-01-01T10:00:00Z", "provenance": "verified_assignment",
                "reconstruction_status": "not_reconstructed",
                "provenance_evidence_ref": "synthetic-provenance-evidence",
                "eligibility_verified": True, "eligibility_evidence_ref": "synthetic-eligibility-evidence",
                "label_kind": "historical_assignment_choice",
                "candidate_staff_ids": [987654321],
                "candidate_roster_department_id": row["complaint_department_id"],
                "candidate_roster_reviewed": True,
                "candidate_staff_eligibility_review": {str(candidate): True for candidate in [987654321]},
                "candidate_roster_at": "2026-01-01T10:00:00Z",
                "candidate_roster_evidence_ref": "synthetic-roster-evidence",
            }]}
            review['dataset_fingerprint'] = trainer.dataset_fingerprint([row])
            review['source_identity'] = 'synthetic-source'
            with open(dataset, "w", encoding="utf-8") as handle:
                json.dump({"format_version": 1, "assignments": [row], "review_manifest": review}, handle)

            output = StringIO()
            with patch.object(trainer, "train_staff_models") as fit_model, \
                    patch.object(trainer, "save_model_artifact") as save_artifact, \
                    redirect_stdout(output):
                result = trainer.main(["--validate-only", "--dataset", dataset, "--source-identity", "synthetic-source", "--artifact-path", artifact])
            report = json.loads(output.getvalue())
            self.assertEqual(result, 0)
            self.assertTrue(report["format_valid"])
            self.assertTrue(report["schema_valid"])
            self.assertTrue(report["validation_passed"])
            self.assertEqual(report["total_assignment_rows"], 1)
            self.assertEqual(report["distinct_complaint_count"], 1)
            self.assertEqual(report["assignment_counts_by_department"], {"Drainage": 1})
            self.assertEqual(report["departments"]["Drainage"]["assignment_counts_per_staff_label"], [1])
            self.assertFalse(report["data_sufficient_for_training_attempt"])
            self.assertTrue(report["validation_success_does_not_guarantee_model_accuracy"])
            self.assertNotIn(private_fixture_text, output.getvalue())
            self.assertNotIn("987654321", output.getvalue())
            fit_model.assert_not_called()
            save_artifact.assert_not_called()
            self.assertFalse(os.path.exists(artifact))

            sentinel = b"preexisting-artifact-must-remain-byte-for-byte"
            with open(artifact, "wb") as handle:
                handle.write(sentinel)
            output = StringIO()
            with patch.object(trainer, "train_staff_models") as fit_model, \
                    patch.object(trainer, "save_model_artifact") as save_artifact, \
                    redirect_stdout(output):
                result = trainer.main(["--validate-only", "--dataset", dataset, "--source-identity", "synthetic-source", "--artifact-path", artifact])
            self.assertEqual(result, 0)
            with open(artifact, "rb") as handle:
                self.assertEqual(handle.read(), sentinel)
            fit_model.assert_not_called()
            save_artifact.assert_not_called()

    def test_validation_only_reports_invalid_and_malformed_data_without_values(self):
        import train_staff_recommendation as trainer
        with tempfile.TemporaryDirectory() as directory:
            dataset = os.path.join(directory, "invalid.json")
            row = {
                "assignment_id": 1, "complaint_id": 11, "staff_id": 87654321,
                "assigned_at": "2026-01-01T10:00:00Z", "complaint_text": "PRIVATE_INVALID_FIXTURE_TEXT",
                "category": "unsupported-demo-category", "complaint_department_id": 2,
                "complaint_department": "Drainage", "staff_role": "not-a-staff-role",
                "staff_department_id": 2, "staff_department": "Drainage", "staff_is_active": True,
            }
            review = {"format_version": 2, "records": [{
                "assignment_id": 1, "complaint_id": 11, "staff_id": 87654321,
                "assigned_at": "2026-01-01T10:00:00Z", "provenance": "verified_assignment",
                "reconstruction_status": "not_reconstructed",
                "provenance_evidence_ref": "synthetic-provenance-evidence",
                "eligibility_verified": True, "eligibility_evidence_ref": "synthetic-eligibility-evidence",
                "label_kind": "historical_assignment_choice",
                "candidate_staff_ids": [87654321],
                "candidate_roster_department_id": row["complaint_department_id"],
                "candidate_roster_reviewed": True,
                "candidate_staff_eligibility_review": {str(candidate): True for candidate in [87654321]},
                "candidate_roster_at": "2026-01-01T10:00:00Z",
                "candidate_roster_evidence_ref": "synthetic-roster-evidence",
            }]}
            review['dataset_fingerprint'] = trainer.dataset_fingerprint([row])
            review['source_identity'] = 'synthetic-source'
            with open(dataset, "w", encoding="utf-8") as handle:
                json.dump({"format_version": 1, "assignments": [row], "review_manifest": review}, handle)
            output = StringIO()
            with redirect_stdout(output):
                result = trainer.main(["--validate-only", "--dataset", dataset, "--source-identity", "synthetic-source"])
            report = json.loads(output.getvalue())
            self.assertEqual(result, 1)
            self.assertFalse(report["validation_passed"])
            self.assertEqual(report["invalid_assignment_rows"], 1)
            self.assertIn("invalid_category", report["rejected_row_counts"])
            self.assertNotIn("PRIVATE_INVALID_FIXTURE_TEXT", output.getvalue())
            self.assertNotIn("87654321", output.getvalue())

            malformed = os.path.join(directory, "malformed.json")
            with open(malformed, "w", encoding="utf-8") as handle:
                handle.write('{"private_value":"DO_NOT_REPORT"')
            output = StringIO()
            with redirect_stdout(output):
                result = trainer.main(["--validate-only", "--dataset", malformed, "--source-identity", "synthetic-source"])
            self.assertEqual(result, 1)
            self.assertIn('"status": "dataset_invalid"', output.getvalue())
            self.assertNotIn("DO_NOT_REPORT", output.getvalue())

    def test_ignored_complaints_leave_active_and_staff_queues(self):
        complaint_id = self.make_complaint(status="In Progress", assigned_staff_id=self.ids["staff"])
        self.login_as("head")
        response = self.client.post(f"/ignore-complaint/{complaint_id}", data={"reason": "Duplicate report", "csrf_token": "test-csrf"})
        self.assertEqual(response.status_code, 302)
        self.client.get("/")  # Consume the success flash before checking queue contents.
        active = self.client.get("/officer-dashboard?queue=active")
        ignored = self.client.get("/officer-dashboard?queue=ignored")
        complaint_link = f'href="/track-complaint/{complaint_id}"'.encode()
        self.assertNotIn(complaint_link, active.data)
        self.assertIn(complaint_link, ignored.data)
        self.login_as("staff")
        staff_queue = self.client.get("/staff-dashboard")
        self.assertNotIn(complaint_link, staff_queue.data)

    def test_start_work_and_invalid_transitions_are_safe(self):
        complaint_id = self.make_complaint(status="Assigned", assigned_staff_id=self.ids["staff"])
        self.login_as("staff")
        started_at = datetime(2026, 3, 4, 3, 0)
        with patch("app.utcnow", return_value=started_at):
            started = self.client.post(f"/update-status/{complaint_id}", data={"status": "In Progress", "csrf_token": "test-csrf"})
        self.assertEqual(started.status_code, 302)
        with app.app_context():
            self.assertEqual(db.session.get(Complaint, complaint_id).status, "In Progress")
            started_event = ComplaintStatusHistory.query.filter_by(
                complaint_id=complaint_id, new_status="In Progress",
            ).one()
            self.assertEqual(started_event.timestamp, started_at)
        resolved_at = datetime(2026, 3, 4, 8, 15)
        with patch("app.utcnow", return_value=resolved_at):
            resolved = self.client.post(f"/update-status/{complaint_id}", data={"status": "Resolved", "remarks": "Done", "csrf_token": "test-csrf"})
        self.assertEqual(resolved.status_code, 302)
        with app.app_context():
            self.assertEqual(db.session.get(Complaint, complaint_id).status, "Resolved")
            resolved_event = ComplaintStatusHistory.query.filter_by(
                complaint_id=complaint_id, new_status="Resolved",
            ).one()
            self.assertEqual(resolved_event.timestamp, resolved_at)
        stale_start = self.client.post(f"/update-status/{complaint_id}", data={"status": "In Progress", "csrf_token": "test-csrf"})
        self.assertEqual(stale_start.status_code, 302)

    def test_resolved_complaint_cannot_start_work_again(self):
        complaint_id = self.make_complaint(status="Resolved", assigned_staff_id=self.ids["staff"])
        with app.app_context():
            db.session.add(ComplaintStatusHistory(complaint_id=complaint_id, old_status="In Progress", new_status="Resolved", updated_by=self.ids["staff"]))
            db.session.commit()
            before = ComplaintStatusHistory.query.filter_by(complaint_id=complaint_id).count()
        self.login_as("staff")
        response = self.client.post(f"/update-status/{complaint_id}", data={"status": "In Progress", "csrf_token": "test-csrf"})
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            self.assertEqual(db.session.get(Complaint, complaint_id).status, "Resolved")
            self.assertEqual(ComplaintStatusHistory.query.filter_by(complaint_id=complaint_id).count(), before)

    def test_wrong_role_cannot_open_department_head_dashboard(self):
        self.login_as("citizen")
        self.assertEqual(self.client.get("/officer-dashboard").status_code, 403)

    def test_login_role_value_and_password_verification_still_work(self):
        response = self.client.post("/login", data={
            "email": "head@example.test", "password": "test-password", "role": "department_head",
        })
        self.assertEqual(response.status_code, 302)
        with self.client.session_transaction() as state:
            self.assertEqual(state.get("role"), "department_head")

    def test_citizen_registration_enforces_server_minimum_and_hashes_password(self):
        with app.app_context():
            before = User.query.count()
        too_short = self.client.post("/register", data={
            "name": "New Citizen", "email": "new-citizen@example.test",
            "phone": "555", "password": "12345",
        })
        self.assertEqual(too_short.status_code, 200)
        with app.app_context():
            self.assertEqual(User.query.count(), before)

        created = self.client.post("/register", data={
            "name": "New Citizen", "email": "new-citizen@example.test",
            "phone": "555", "password": "six-ok",
        })
        self.assertEqual(created.status_code, 302)
        with app.app_context():
            account = User.query.filter_by(email="new-citizen@example.test").one()
            self.assertNotEqual(account.password, "six-ok")
            self.assertTrue(check_password_hash(account.password, "six-ok"))
        login = self.client.post("/login", data={
            "email": "new-citizen@example.test", "password": "six-ok", "role": "citizen",
        })
        self.assertEqual(login.status_code, 302)

    def test_admin_staff_creation_rejects_shared_password_and_hashes_unique_password(self):
        self.login_as("admin")
        data = {
            "action": "create_user", "name": "New Staff", "email": "new-staff@example.test",
            "phone": "555", "password": "test-password", "role": "staff",
            "department_id": str(self.department_ids["Drainage"]), "csrf_token": "test-csrf",
        }
        missing_csrf = dict(data)
        missing_csrf.pop("csrf_token")
        csrf_rejected = self.client.post("/admin/manage", data=missing_csrf)
        self.assertEqual(csrf_rejected.status_code, 400)
        rejected = self.client.post("/admin/manage", data=data)
        self.assertEqual(rejected.status_code, 302)
        with app.app_context():
            self.assertIsNone(User.query.filter_by(email="new-staff@example.test").first())

        data["password"] = "short7"
        self.client.post("/admin/manage", data=data)
        with app.app_context():
            self.assertIsNone(User.query.filter_by(email="new-staff@example.test").first())

        data["password"] = "eleven-char"
        self.client.post("/admin/manage", data=data)
        with app.app_context():
            self.assertIsNone(User.query.filter_by(email="new-staff@example.test").first())

        data["password"] = "fresh-individual-password"
        created = self.client.post("/admin/manage", data=data)
        self.assertEqual(created.status_code, 302)
        with app.app_context():
            account = User.query.filter_by(email="new-staff@example.test").one()
            self.assertTrue(check_password_hash(account.password, "fresh-individual-password"))

    def test_admin_password_reset_changes_only_explicit_selected_account(self):
        selected_id = self.ids["staff"]
        other_id = self.ids["staff2"]
        with app.app_context():
            original_selected = db.session.get(User, selected_id).password
            original_other = db.session.get(User, other_id).password
        self.login_as("admin")
        response = self.client.post("/admin/manage", data={
            "action": "reset_password", "csrf_token": "test-csrf",
            "user_id": str(selected_id), "new_password": "selected-account-new-pass",
            "confirm_password": "selected-account-new-pass",
        })
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            selected = db.session.get(User, selected_id)
            other = db.session.get(User, other_id)
            self.assertNotEqual(selected.password, original_selected)
            self.assertTrue(check_password_hash(selected.password, "selected-account-new-pass"))
            self.assertEqual(other.password, original_other)
            self.assertTrue(check_password_hash(other.password, "test-password"))

    def test_password_reset_requires_admin_and_csrf_and_rejects_other_roles(self):
        with app.app_context():
            citizen_hash = db.session.get(User, self.ids["citizen"]).password
        self.login_as("staff")
        forbidden = self.client.post("/admin/manage", data={
            "action": "reset_password", "csrf_token": "test-csrf",
            "user_id": str(self.ids["staff2"]), "new_password": "long-enough-password",
            "confirm_password": "long-enough-password",
        })
        self.assertEqual(forbidden.status_code, 403)
        self.login_as("admin")
        bad_csrf = self.client.post("/admin/manage", data={
            "action": "reset_password", "csrf_token": "wrong-token",
            "user_id": str(self.ids["citizen"]), "new_password": "long-enough-password",
            "confirm_password": "long-enough-password",
        })
        self.assertEqual(bad_csrf.status_code, 400)
        valid_csrf_wrong_role = self.client.post("/admin/manage", data={
            "action": "reset_password", "csrf_token": "test-csrf",
            "user_id": str(self.ids["citizen"]), "new_password": "long-enough-password",
            "confirm_password": "long-enough-password",
        })
        self.assertEqual(valid_csrf_wrong_role.status_code, 302)
        with app.app_context():
            self.assertEqual(db.session.get(User, self.ids["citizen"]).password, citizen_hash)

    def test_password_reset_keeps_twelve_character_minimum(self):
        with app.app_context():
            original = db.session.get(User, self.ids["staff"]).password
        self.login_as("admin")
        response = self.client.post("/admin/manage", data={
            "action": "reset_password", "csrf_token": "test-csrf",
            "user_id": str(self.ids["staff"]), "new_password": "too-short",
            "confirm_password": "too-short",
        })
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            self.assertEqual(db.session.get(User, self.ids["staff"]).password, original)

    def test_self_service_password_change_is_available_to_all_roles_and_targets_session_user(self):
        role_accounts = ["citizen", "staff", "head", "admin"]
        other_ids = {
            key: self.ids[key]
            for key in ("other_citizen", "other_head", "unlinked_head", "staff2", "other_staff")
        }
        with app.app_context():
            original_hashes = {key: db.session.get(User, self.ids[key]).password for key in role_accounts}
            protected_hashes = {key: db.session.get(User, value).password for key, value in other_ids.items()}

        changed_passwords = {}
        for index, account_key in enumerate(role_accounts):
            with self.subTest(role=account_key):
                self.login_as(account_key)
                response = self.client.get("/change-password")
                self.assertEqual(response.status_code, 200)
                self.assertIn(b"Change password", response.data)
                self.assertIn(b"name=\"csrf_token\"", response.data)

                new_password = f"new-role-password-{index:02d}"
                response = self.client.post("/change-password", data={
                    "csrf_token": "test-csrf",
                    "current_password": "test-password",
                    "new_password": new_password,
                    "confirm_password": new_password,
                    "user_id": str(self.ids["other_citizen"]),
                    "email": "other-citizen@example.test",
                    "role": "admin",
                })
                self.assertEqual(response.status_code, 302)
                with app.app_context():
                    changed = db.session.get(User, self.ids[account_key])
                    self.assertNotEqual(changed.password, original_hashes[account_key])
                    self.assertTrue(check_password_hash(changed.password, new_password))
                    changed_passwords[account_key] = new_password
                    for key, expected_hash in original_hashes.items():
                        if key in changed_passwords:
                            self.assertTrue(check_password_hash(
                                db.session.get(User, self.ids[key]).password,
                                changed_passwords[key],
                            ))
                        elif key != account_key:
                            self.assertEqual(db.session.get(User, self.ids[key]).password, expected_hash)
                    for key, expected_hash in protected_hashes.items():
                        self.assertEqual(db.session.get(User, other_ids[key]).password, expected_hash)

    def test_self_service_password_change_rejects_invalid_current_mismatch_short_and_unchanged(self):
        self.login_as("citizen")
        with app.app_context():
            original = db.session.get(User, self.ids["citizen"]).password

        cases = [
            ({"current_password": "incorrect", "new_password": "distinct-new-password", "confirm_password": "distinct-new-password"}, "current password"),
            ({"current_password": "test-password", "new_password": "distinct-new-password", "confirm_password": "different-new-password"}, "do not match"),
            ({"current_password": "test-password", "new_password": "short", "confirm_password": "short"}, "at least 12"),
            ({"current_password": "test-password", "new_password": "test-password", "confirm_password": "test-password"}, "differs from your current"),
        ]
        for fields, expected_message in cases:
            with self.subTest(case=expected_message):
                response = self.client.post("/change-password", data={"csrf_token": "test-csrf", **fields})
                self.assertEqual(response.status_code, 200)
                self.assertIn(expected_message.encode(), response.data)
                with app.app_context():
                    self.assertEqual(db.session.get(User, self.ids["citizen"]).password, original)

    def test_self_service_password_change_requires_csrf_and_authentication(self):
        with app.app_context():
            original = db.session.get(User, self.ids["citizen"]).password
        self.login_as("citizen")
        bad_csrf = self.client.post("/change-password", data={
            "csrf_token": "wrong-token", "current_password": "test-password",
            "new_password": "valid-new-password", "confirm_password": "valid-new-password",
        })
        self.assertEqual(bad_csrf.status_code, 400)
        with app.app_context():
            self.assertEqual(db.session.get(User, self.ids["citizen"]).password, original)

        self.client.get("/logout")
        unauthenticated_get = self.client.get("/change-password")
        unauthenticated_post = self.client.post("/change-password", data={
            "csrf_token": "test-csrf", "current_password": "test-password",
            "new_password": "valid-new-password", "confirm_password": "valid-new-password",
        })
        self.assertEqual(unauthenticated_get.status_code, 302)
        self.assertEqual(unauthenticated_post.status_code, 302)
        with app.app_context():
            self.assertEqual(db.session.get(User, self.ids["citizen"]).password, original)

    def test_self_service_password_change_keeps_admin_targeted_reset_separate(self):
        self.login_as("citizen")
        response = self.client.post("/change-password", data={
            "csrf_token": "test-csrf", "current_password": "test-password",
            "new_password": "citizen-own-new-password", "confirm_password": "citizen-own-new-password",
            "user_id": str(self.ids["staff"]),
        })
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            self.assertTrue(check_password_hash(db.session.get(User, self.ids["citizen"]).password, "citizen-own-new-password"))
            self.assertTrue(check_password_hash(db.session.get(User, self.ids["staff"]).password, "test-password"))

        self.login_as("admin")
        denied = self.client.post("/admin/manage", data={
            "action": "reset_password", "csrf_token": "test-csrf",
            "user_id": str(self.ids["citizen"]), "new_password": "another-password-12",
            "confirm_password": "another-password-12",
        })
        self.assertEqual(denied.status_code, 302)
        with app.app_context():
            self.assertTrue(check_password_hash(db.session.get(User, self.ids["citizen"]).password, "citizen-own-new-password"))

    def test_password_policy_rejects_reuse_without_disclosing_candidate(self):
        stored_hash = generate_password_hash("already-used-secret")
        with self.assertRaises(ValueError) as error:
            require_unique_passwords(["already-used-secret"], [stored_hash])
        self.assertNotIn("already-used-secret", str(error.exception))
        with self.assertRaises(ValueError):
            require_unique_passwords(["distinct-secret", "distinct-secret"])
        with self.assertRaises(ValueError):
            individual_passwords_for(["a@example.test", "b@example.test"], {
                "a@example.test": "distinct-1", "b@example.test": "distinct-1",
            })
        self.assertEqual(
            individual_passwords_for(["a@example.test"], {" A@EXAMPLE.TEST ": "unique-password-12"}),
            ["unique-password-12"],
        )

    def test_password_reuse_detection_handles_independently_salted_hashes(self):
        reused = "reused-password-for-test"
        first_hash = generate_password_hash(reused)
        second_hash = generate_password_hash(reused)
        self.assertNotEqual(first_hash, second_hash)
        with self.assertRaises(ValueError):
            require_unique_passwords([reused], [first_hash, second_hash])
        self.assertTrue(require_unique_passwords(["different-password-value"], [first_hash, second_hash]))

    def test_password_map_json_validation_is_bounded_exact_and_secret_safe(self):
        self.assertEqual(parse_password_map_json('{"a@example.test":"valid-value"}'), {
            "a@example.test": "valid-value",
        })
        invalid_inputs = [
            "{bad-json", "[]", '"scalar"',
            '{"a@example.test":"one","a@example.test":"two"}',
            " " * (64 * 1024 + 1),
        ]
        for invalid in invalid_inputs:
            with self.subTest(input_size=len(invalid)):
                with self.assertRaises(ValueError) as error:
                    parse_password_map_json(invalid)
                self.assertNotIn("one", str(error.exception))

        cases = [
            ({}, ["a@example.test"]),
            ({"a@example.test": "twelve-chars", "unused@example.test": "another-password"}, ["a@example.test"]),
            ({"a@example.test": 123456789012}, ["a@example.test"]),
            ({"a@example.test": "eleven-char"}, ["a@example.test"]),
        ]
        for password_map, emails in cases:
            with self.subTest(email_count=len(emails)):
                with self.assertRaises(ValueError):
                    individual_passwords_for(emails, password_map)

    def test_email_normalization_is_case_insensitive_and_trims_whitespace(self):
        self.assertEqual(normalize_email("  Mixed.Case@Example.Test "), "mixed.case@example.test")

    def test_demo_admin_case_variant_is_reused_without_password_change(self):
        existing_hash = generate_password_hash("existing-admin-only-value")
        with app.app_context():
            db.session.add(User(
                name="Existing Demo Admin", email="ADMIN@SPSFRMS.COM", phone="555",
                password=existing_hash, role="admin",
            ))
            db.session.commit()

        with patch.dict(os.environ, {
            "SPSFRMS_CREATE_DEMO_ADMIN": "1",
            "SPSFRMS_DEMO_ADMIN_PASSWORD": "individual-admin-password-12",
            "SPSFRMS_SEED_DEMO_DATA": "0",
        }, clear=False):
            create_demo_users()

        with app.app_context():
            matches = User.query.filter(
                db.func.lower(db.func.trim(User.email)) == "admin@spsfrms.com"
            ).all()
            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0].password, existing_hash)

    def test_seed_case_variant_preserves_existing_account_password_and_role(self):
        existing_hash = generate_password_hash("existing-seed-user-value")
        with app.app_context():
            account = User(
                name="Existing Seed User", email="Seed.User@Example.Test", phone="555",
                password=existing_hash, role="staff", department_id=self.department_ids["Drainage"],
            )
            db.session.add(account)
            db.session.commit()

        seed_data = {
            "departments": ["Drainage"],
            "users": [{
                "name": "Seed User", "email": "seed.user@example.test", "phone": "555",
                "role": "department_head", "department": "Drainage",
            }],
        }
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".json", delete=False) as seed_file:
            json.dump(seed_data, seed_file)
            seed_path = seed_file.name
        self.addCleanup(lambda: os.path.exists(seed_path) and os.unlink(seed_path))
        with patch.dict(os.environ, {
            "SPSFRMS_CREATE_DEMO_ADMIN": "0",
            "SPSFRMS_SEED_DEMO_DATA": "1",
            "SPSFRMS_SEED_FILE": seed_path,
            "SPSFRMS_SEED_PASSWORDS_JSON": "{}",
        }, clear=False):
            create_demo_users()

        with app.app_context():
            matches = User.query.filter(
                db.func.lower(db.func.trim(User.email)) == "seed.user@example.test"
            ).all()
            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0].password, existing_hash)
            self.assertEqual(matches[0].role, "staff")

    def test_bulk_password_reset_utility_is_retired_without_database_access(self):
        output = StringIO()
        with redirect_stdout(output):
            result = retired_bulk_reset_main()
        self.assertEqual(result, 2)
        self.assertIn("retired", output.getvalue().lower())
        self.assertIn("one explicitly selected account", output.getvalue())

    def test_demo_admin_creation_requires_explicit_individual_password(self):
        with patch.dict(os.environ, {
            "SPSFRMS_CREATE_DEMO_ADMIN": "1",
            "SPSFRMS_DEMO_ADMIN_PASSWORD": "individual-demo-admin-1",
            "SPSFRMS_SEED_DEMO_DATA": "0",
        }, clear=False):
            create_demo_users()
        with app.app_context():
            account = User.query.filter_by(email="admin@spsfrms.com").one()
            self.assertTrue(check_password_hash(account.password, "individual-demo-admin-1"))

    def test_demo_seed_without_per_account_passwords_fails_before_writes(self):
        with app.app_context():
            before_users = User.query.count()
            before_departments = Department.query.count()
        with patch.dict(os.environ, {
            "SPSFRMS_CREATE_DEMO_ADMIN": "0",
            "SPSFRMS_SEED_DEMO_DATA": "1",
            "SPSFRMS_SEED_PASSWORDS_JSON": "{}",
        }, clear=False):
            with self.assertRaises(ValueError):
                create_demo_users()
        with app.app_context():
            self.assertEqual(User.query.count(), before_users)
            self.assertEqual(Department.query.count(), before_departments)

    def test_citizen_can_track_own_complaint_with_analysis_details(self):
        complaint_id = self.make_complaint()
        self.login_as("citizen")
        response = self.client.get(f"/track-complaint/{complaint_id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Classification explanation", response.data)

    def test_invalid_admin_action_redirects_without_bad_request(self):
        self.login_as("admin")
        response = self.client.post("/admin/manage", data={"action": "unknown"})
        self.assertEqual(response.status_code, 302)

    def test_missing_assignment_staff_and_invalid_status_redirect_helpfully(self):
        complaint_id = self.make_complaint(status="Assigned", assigned_staff_id=self.ids["staff"])
        self.login_as("head")
        assignment = self.client.post(f"/assign-complaint/{complaint_id}", data={"csrf_token": "test-csrf"})
        self.assertEqual(assignment.status_code, 302)
        self.login_as("staff")
        invalid_status = self.client.post(f"/update-status/{complaint_id}", data={"status": "Invented", "csrf_token": "test-csrf"})
        self.assertEqual(invalid_status.status_code, 302)

    def test_health_reports_database_failure_without_exposing_error_details(self):
        previous_state = app.config.get("DATABASE_INITIALIZATION_STATE")
        self.addCleanup(app.config.__setitem__, "DATABASE_INITIALIZATION_STATE", previous_state)
        app.config["DATABASE_INITIALIZATION_STATE"] = "not_run"
        with patch.object(db.session, "execute", side_effect=RuntimeError("private connection detail")):
            response = self.client.get("/health")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json()["status"], "error")
        self.assertEqual(response.get_json()["process"], "running")
        self.assertEqual(response.get_json()["database"], "unavailable")
        self.assertEqual(response.get_json()["schema"], "unknown")
        self.assertFalse(response.get_json()["ready"])
        self.assertNotIn(b"private connection detail", response.data)

    def test_startup_skip_option_never_calls_initializer_or_demo_creation(self):
        with (
            patch.dict(os.environ, {"SPSFRMS_STARTUP_MODE": "skip"}),
            patch("app.initialize_database") as initialize,
            patch("app.create_demo_users") as create_demo,
        ):
            run_startup_database_setup()
        initialize.assert_not_called()
        create_demo.assert_not_called()
        self.assertEqual(app.config["DATABASE_INITIALIZATION_STATE"], "not_run")

    def test_health_reports_connected_but_schema_is_not_ready(self):
        with patch.object(db.session, "execute", return_value=None):
            response = self.client.get("/health")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json(), {
            "status": "degraded", "process": "running", "database": "connected",
            "schema": "unsupported_dialect", "ready": False,
        })

    def test_health_requires_current_fingerprinted_schema_before_readiness(self):
        with (
            patch.object(db.session, "execute", return_value=None),
            patch("app.inspect_database_schema_readiness", return_value=("current", "0002")),
        ):
            response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {
            "status": "ok", "process": "running", "database": "connected",
            "schema": "current", "migration_version": "0002", "ready": True,
        })

    def test_liveness_does_not_depend_on_database(self):
        with patch.object(db.session, "execute", side_effect=AssertionError("liveness queried DB")):
            response = self.client.get("/livez")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"process": "running"})

    def test_startup_failure_is_distinct_and_health_does_not_claim_success(self):
        previous_state = app.config.get("DATABASE_INITIALIZATION_STATE")
        self.addCleanup(app.config.__setitem__, "DATABASE_INITIALIZATION_STATE", previous_state)
        with (
            patch.dict(os.environ, {"SPSFRMS_STARTUP_MODE": "initialize"}),
            patch("app.initialize_database", side_effect=RuntimeError("private database detail")),
            patch("app.create_demo_users") as create_demo,
            patch.object(db.session, "rollback", side_effect=RuntimeError("private rollback detail")),
            patch.object(app.logger, "error") as log_error,
        ):
            run_startup_database_setup()
        create_demo.assert_not_called()
        self.assertEqual(app.config["DATABASE_INITIALIZATION_STATE"], "failed")
        self.assertGreaterEqual(log_error.call_count, 2)
        self.assertNotIn("private database detail", str(log_error.call_args_list))
        self.assertNotIn("private rollback detail", str(log_error.call_args_list))

        with patch.object(db.session, "execute", return_value=None):
            response = self.client.get("/health")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.get_json(), {
            "status": "degraded", "process": "running", "database": "connected",
            "schema": "unsupported_dialect", "ready": False,
        })

    def test_citizen_submission_stores_drainage_department_and_analysis(self):
        self.login_as("citizen")
        submitted_at = datetime(2026, 2, 10, 5, 0)
        with patch("app.utcnow", return_value=submitted_at):
            response = self.client.post("/submit-complaint", data={
                "complaint_text": "The drainage water is overflowing onto the road near my house.",
                "location": "Test Road",
            })
        self.assertEqual(response.status_code, 302)
        with app.app_context():
            complaint = Complaint.query.order_by(Complaint.id.desc()).first()
            self.assertEqual(complaint.category, "Drainage")
            self.assertEqual(complaint.department.name, "Drainage")
            self.assertEqual(complaint.created_date, submitted_at)
            submitted_event = ComplaintStatusHistory.query.filter_by(
                complaint_id=complaint.id, new_status="Submitted",
            ).one()
            self.assertEqual(submitted_event.timestamp, submitted_at)
            complaint_id = complaint.id
        receipt = self.client.get(f"/track-complaint/{complaint_id}")
        self.assertIn(b"10 Feb 2026, 10:30 AM IST", receipt.data)

    def test_tracking_shows_submission_and_history_in_kolkata_chronological_order(self):
        submitted_at = datetime(2026, 1, 1, 18, 30)
        in_progress_at = datetime(2026, 1, 2, 2, 30)
        resolved_at = datetime(2026, 1, 2, 10, 30)
        complaint_id = self.make_complaint(
            status="Resolved", assigned_staff_id=self.ids["staff"], created_date=submitted_at,
        )
        with app.app_context():
            db.session.add_all([
                ComplaintStatusHistory(
                    complaint_id=complaint_id, old_status=None, new_status="Submitted",
                    updated_by=self.ids["citizen"], timestamp=submitted_at,
                ),
                # Insert out of order to exercise timestamp then ID ordering.
                ComplaintStatusHistory(
                    complaint_id=complaint_id, old_status="In Progress", new_status="Resolved",
                    updated_by=self.ids["staff"], timestamp=resolved_at,
                ),
                ComplaintStatusHistory(
                    complaint_id=complaint_id, old_status="Assigned", new_status="In Progress",
                    updated_by=self.ids["staff"], timestamp=in_progress_at,
                ),
            ])
            db.session.commit()
        self.login_as("citizen")
        response = self.client.get(f"/track-complaint/{complaint_id}")
        self.assertEqual(response.status_code, 200)
        expected_times = [
            b"02 Jan 2026, 12:00 AM IST",
            b"02 Jan 2026, 08:00 AM IST",
            b"02 Jan 2026, 04:00 PM IST",
        ]
        for value in expected_times:
            self.assertIn(value, response.data)
        positions = [response.data.index(value) for value in expected_times]
        self.assertEqual(positions, sorted(positions))

    def test_datetime_formatter_handles_naive_aware_and_missing_timestamps(self):
        utc_naive = datetime(2026, 1, 1, 18, 30)
        utc_aware = utc_naive.replace(tzinfo=timezone.utc)
        kolkata_aware = datetime(2026, 1, 2, 0, 0, tzinfo=timezone(timedelta(hours=5, minutes=30)))
        expected = "02 Jan 2026, 12:00 AM IST"
        self.assertEqual(format_kolkata_datetime(utc_naive), expected)
        self.assertEqual(format_kolkata_datetime(utc_aware), expected)
        self.assertEqual(format_kolkata_datetime(kolkata_aware), expected)
        self.assertEqual(format_kolkata_datetime(None), "Date/time unavailable")
        with app.app_context():
            rendered_missing = app.jinja_env.from_string(
                "{{ timestamp|kolkata_datetime }}"
            ).render(timestamp=None)
        self.assertEqual(rendered_missing, "Date/time unavailable")

    def test_initializer_does_not_invent_history_dates_when_submission_time_is_missing(self):
        with app.app_context():
            complaint = Complaint(
                complaint_text="fixture", category="Drainage", location="fixture",
                status="Assigned", citizen_id=self.ids["citizen"],
                department_id=self.ids["drainage"], assigned_staff_id=self.ids["staff"],
            )
            db.session.add(complaint)
            db.session.commit()
            complaint_id = complaint.id
            complaint.created_date = None
            complaint.updated_date = None
            db.session.commit()
        with patch("app.migrate_schema"):
            from app import initialize_database
            initialize_database()
        with app.app_context():
            self.assertEqual(ComplaintStatusHistory.query.filter_by(complaint_id=complaint_id).count(), 0)
            self.assertEqual(ComplaintAssignment.query.filter_by(complaint_id=complaint_id).count(), 0)


if __name__ == "__main__":
    unittest.main()
