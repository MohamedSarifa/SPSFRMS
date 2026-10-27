from flask import Flask, render_template, request, redirect, url_for, session, flash, abort, g
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from datetime import datetime, timezone
from functools import wraps
from dotenv import load_dotenv
from secrets import token_urlsafe
from types import SimpleNamespace
from hmac import compare_digest
from zoneinfo import ZoneInfo
from sqlalchemy import and_, or_, inspect as sqlalchemy_inspect
from database_config import environment_database_url
from workflow_routing import (
    CATEGORY_DEPARTMENTS,
    CLASSIFICATION_RULES,
    HIGH_PRIORITY_WORDS,
    LEGACY_DEPARTMENT_NAMES,
    MEDIUM_PRIORITY_WORDS,
    MULTI_DEPARTMENT_LEGACY_CATEGORY,
    analyze_complaint,
    contains_word,
    matched_rule_terms,
)
from password_policy import require_unique_passwords
from password_policy import (
    MIN_NEW_ACCOUNT_PASSWORD_LENGTH,
    individual_passwords_for,
    parse_password_map_json,
    password_meets_policy,
)
from account_identity import normalize_email
from staff_recommendation import load_model_artifact, recommend_staff
from assignment_audit_models import (
    application_tables_excluding_audit,
    register_assignment_audit_models,
)
from migration_runner import LATEST_REQUIRED_VERSION, schema_fingerprint
import json
import os

load_dotenv()

def image_uploads_enabled_from_environment(environ=None):
    """Require an explicit opt-in before saving uploads to local disk."""
    environment = os.environ if environ is None else environ
    if environment.get("VERCEL") == "1":
        return False
    configured = environment.get("SPSFRMS_IMAGE_UPLOADS_ENABLED")
    return bool(configured and configured.strip().lower() in {"1", "true", "yes", "on"})


app = Flask(__name__)

secret_key = os.getenv("SECRET_KEY")
if not secret_key or len(secret_key.strip()) < 32:
    raise RuntimeError(
        "SECRET_KEY must be explicitly configured with at least 32 non-whitespace characters."
    )
app.config["SECRET_KEY"] = secret_key.strip()

database_url = environment_database_url()
if database_url:
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url
else:
    app.config["SQLALCHEMY_DATABASE_URI"] = (
        "postgresql+psycopg://postgres:YOUR_PASSWORD@localhost:5432/spsfrms_db"
    )

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
    "pool_pre_ping": True,
    "pool_recycle": 300,
}
app.config["UPLOAD_FOLDER"] = "uploads"
app.config["IMAGE_UPLOADS_ENABLED"] = image_uploads_enabled_from_environment()
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024
app.jinja_env.globals["csrf_token"] = lambda: csrf_token()


@app.context_processor
def inject_image_upload_setting():
    return {"image_uploads_enabled": app.config["IMAGE_UPLOADS_ENABLED"]}


db = SQLAlchemy(app)
StaffEligibilityPeriod, AssignmentEvent = register_assignment_audit_models(db)


def utcnow():
    """Return naive UTC timestamps to match existing database DateTime columns."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def format_kolkata_datetime(value):
    """Format a stored UTC datetime as Asia/Kolkata without double conversion."""
    if not isinstance(value, datetime):
        return "Date/time unavailable"
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(ZoneInfo("Asia/Kolkata")).strftime("%d %b %Y, %I:%M %p %Z")


app.jinja_env.filters["kolkata_datetime"] = format_kolkata_datetime

ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png"}
CATEGORIES = [
    "Road Damage", "Garbage", "Water Leakage", "Street Light",
    "Drainage", "Water Supply", "Electricity", "Traffic", "Fire", "Public Health", "Other"
]
STATUSES = ["Submitted", "Under Review", "Assigned", "In Progress", "Resolved"]
ROLES = {"citizen", "department_head", "staff", "admin"}
DEPARTMENT_NAMES = sorted(set(CATEGORY_DEPARTMENTS.values()))
MAX_ACTIVE_STAFF_ASSIGNMENTS = 2
TERMINAL_ASSIGNMENT_STATUSES = ("Resolved", "Ignored")


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    phone = db.Column(db.String(20), nullable=False)
    password = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), default="citizen")
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    registered_date = db.Column(db.DateTime, default=utcnow)
    department_id = db.Column(db.Integer, db.ForeignKey("department.id", ondelete="SET NULL"))

    complaints = db.relationship("Complaint", backref="citizen", lazy=True, foreign_keys="Complaint.citizen_id")
    department = db.relationship("Department", foreign_keys=[department_id], back_populates="members")
    headed_department = db.relationship("Department", foreign_keys="Department.head_id", back_populates="head", uselist=False)
    assigned_complaints = db.relationship("Complaint", back_populates="assigned_staff", foreign_keys="Complaint.assigned_staff_id")
    department_assignments = db.relationship(
        "ComplaintDepartment", back_populates="assigned_staff", foreign_keys="ComplaintDepartment.assigned_staff_id"
    )


def find_user_by_email(email):
    """Find an account using the application's normalized email identity."""
    normalized_email = normalize_email(email)
    if not normalized_email:
        return None
    return User.query.filter(
        db.func.lower(db.func.trim(User.email)) == normalized_email
    ).one_or_none()


class Department(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    email = db.Column(db.String(120))
    head_id = db.Column(db.Integer, db.ForeignKey(
        "user.id", ondelete="SET NULL", use_alter=True, name="fk_department_head_user"
    ))

    head = db.relationship("User", foreign_keys=[head_id], back_populates="headed_department")
    members = db.relationship("User", foreign_keys=[User.department_id], back_populates="department")
    complaints = db.relationship("Complaint", back_populates="department", foreign_keys="Complaint.department_id")


class ComplaintDepartment(db.Model):
    __tablename__ = "complaint_department"
    __table_args__ = (
        db.UniqueConstraint("complaint_id", "department_id", name="uq_complaint_department"),
        db.CheckConstraint(
            "status IN ('Submitted', 'Under Review', 'Assigned', 'In Progress', 'Resolved', 'Ignored')",
            name="ck_cd_status",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    complaint_id = db.Column(db.Integer, db.ForeignKey("complaint.id", ondelete="CASCADE"), nullable=False)
    department_id = db.Column(db.Integer, db.ForeignKey("department.id", ondelete="CASCADE"), nullable=False)
    assigned_date = db.Column(db.DateTime, default=utcnow)
    assigned_staff_id = db.Column(
        db.Integer, db.ForeignKey("user.id", ondelete="RESTRICT", name="fk_cd_assigned_staff"), index=True
    )
    assigned_by = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL", name="fk_cd_assigned_by"))
    staff_assigned_date = db.Column(db.DateTime)
    status = db.Column(db.String(30), nullable=False, default="Submitted")
    resolution_note = db.Column(db.Text)

    complaint = db.relationship("Complaint", back_populates="department_assignments")
    department = db.relationship("Department")
    assigned_staff = db.relationship("User", back_populates="department_assignments", foreign_keys=[assigned_staff_id])
    assigner = db.relationship("User", foreign_keys=[assigned_by])


class Complaint(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    complaint_text = db.Column(db.Text, nullable=False)
    category = db.Column(db.String(100), nullable=False)
    location = db.Column(db.String(255), nullable=False)
    image = db.Column(db.String(255))
    keywords = db.Column(db.String(255))
    priority = db.Column(db.String(20), default="Low")
    status = db.Column(db.String(30), default="Submitted")
    created_date = db.Column(db.DateTime, default=utcnow)
    updated_date = db.Column(
        db.DateTime, default=utcnow, onupdate=utcnow
    )

    citizen_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    department_id = db.Column(db.Integer, db.ForeignKey("department.id"))
    assigned_staff_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"))
    resolution_note = db.Column(db.Text)
    department = db.relationship("Department", back_populates="complaints")
    assigned_staff = db.relationship("User", back_populates="assigned_complaints", foreign_keys=[assigned_staff_id])
    department_assignments = db.relationship("ComplaintDepartment", back_populates="complaint", cascade="all, delete-orphan")
    assignment_history = db.relationship("ComplaintAssignment", back_populates="complaint", cascade="all, delete-orphan", order_by="ComplaintAssignment.assigned_date")
    status_history = db.relationship(
        "ComplaintStatusHistory", back_populates="complaint", cascade="all, delete-orphan",
        order_by=lambda: (ComplaintStatusHistory.timestamp, ComplaintStatusHistory.id),
    )


class ComplaintAssignment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    complaint_id = db.Column(db.Integer, db.ForeignKey("complaint.id", ondelete="CASCADE"), nullable=False, index=True)
    department_id = db.Column(
        db.Integer, db.ForeignKey("department.id", ondelete="RESTRICT", name="fk_ca_department"), index=True
    )
    staff_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="RESTRICT"), nullable=False, index=True)
    assigned_by = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="RESTRICT"))
    assigned_date = db.Column(db.DateTime, nullable=False, default=utcnow)
    remarks = db.Column(db.Text)

    complaint = db.relationship("Complaint", back_populates="assignment_history")
    staff = db.relationship("User", foreign_keys=[staff_id])
    assigner = db.relationship("User", foreign_keys=[assigned_by])
    department = db.relationship("Department")


class ComplaintStatusHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    complaint_id = db.Column(db.Integer, db.ForeignKey("complaint.id", ondelete="CASCADE"), nullable=False, index=True)
    department_id = db.Column(
        db.Integer, db.ForeignKey("department.id", ondelete="RESTRICT", name="fk_csh_department"), index=True
    )
    old_status = db.Column(db.String(30))
    new_status = db.Column(db.String(30), nullable=False)
    updated_by = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"))
    timestamp = db.Column(db.DateTime, nullable=False, default=utcnow)
    remarks = db.Column(db.Text)

    complaint = db.relationship("Complaint", back_populates="status_history")
    updater = db.relationship("User")
    department = db.relationship("Department")


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def analyze_text(text):
    """Compatibility wrapper for callers that need the original three values."""
    result = analyze_complaint(text)
    return result["keywords"], result["category"], result["priority"]


def priority_order_key(complaint):
    """Emergency first, then priority, then oldest submission, then ID."""
    analysis = analyze_complaint(complaint.complaint_text)
    rank = {"High": 3, "Medium": 2, "Low": 1}.get(complaint.priority, 0)
    created = complaint.created_date or datetime.max
    return (not analysis["emergency"], -rank, created, complaint.id or 0)


def complaint_age_label(created_date, now=None):
    seconds = max(0, int(((now or utcnow()) - created_date).total_seconds()))
    if seconds < 3600:
        return f"{max(1, seconds // 60)}m waiting"
    if seconds < 86400:
        return f"{seconds // 3600}h {seconds % 3600 // 60}m waiting"
    return f"{seconds // 86400}d {seconds % 86400 // 3600}h waiting"


def department_label(category):
    return f"{CATEGORY_DEPARTMENTS.get(category, 'Public Health')} Department"


def login_required():
    return current_user() is not None


def normalize_role(role):
    return "department_head" if role == "officer" else role


def csrf_token():
    """Return a session-bound CSRF token for sensitive admin forms."""
    if "csrf_token" not in session:
        session["csrf_token"] = token_urlsafe(32)
    return session["csrf_token"]


def valid_csrf_token(token):
    expected = session.get("csrf_token")
    return bool(expected and token and compare_digest(expected, token))


def current_user():
    if not session.get("user_id"):
        return None
    if not hasattr(g, "current_user"):
        g.current_user = db.session.get(User, session["user_id"])
        if g.current_user:
            session["role"] = normalize_role(g.current_user.role)
            session["user_name"] = g.current_user.name
    return g.current_user


def roles_required(*roles):
    allowed_roles = set(roles)

    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            user = current_user()
            if user is None:
                session.clear()
                return redirect(url_for("login"))
            if normalize_role(user.role) not in allowed_roles:
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator


def redirect_by_role():
    user = current_user()
    role = normalize_role(user.role) if user else session.get("role")
    if role == "citizen":
        return redirect(url_for("citizen_dashboard"))
    if role == "department_head":
        return redirect(url_for("officer_dashboard"))
    if role == "staff":
        return redirect(url_for("staff_dashboard"))
    if role == "admin":
        return redirect(url_for("admin_dashboard"))
    return redirect(url_for("home"))


def department_complaints_query(department):
    legacy_names = LEGACY_DEPARTMENT_NAMES.get(department.name, set())
    legacy_ids = [row.id for row in Department.query.filter(Department.name.in_(legacy_names)).all()] if legacy_names else []
    clauses = [Complaint.department_id == department.id]
    # The association table can identify departments linked to a complaint.
    # It does not provide independent ownership or workflow state per department.
    clauses.append(Complaint.department_assignments.any(ComplaintDepartment.department_id == department.id))
    if legacy_ids:
        clauses.extend([
            Complaint.department_id.in_(legacy_ids),
            Complaint.department_assignments.any(ComplaintDepartment.department_id.in_(legacy_ids)),
        ])
    categories = [category for category, name in CATEGORY_DEPARTMENTS.items() if name == department.name]
    if categories:
        clauses.append(Complaint.category.in_(categories))
    return Complaint.query.filter(db.or_(*clauses))


def resolve_analysis_departments(analysis):
    """Resolve every analyzer route to one unambiguous configured DB record."""
    primary_name = analysis.get("department")
    requested = analysis.get("departments")
    if not isinstance(primary_name, str) or not primary_name.strip():
        return None
    if not isinstance(requested, (list, tuple)):
        requested = [primary_name]
    names = []
    for name in [primary_name, *requested]:
        if not isinstance(name, str) or not name.strip():
            return None
        normalized = name.strip().casefold()
        if normalized not in names:
            names.append(normalized)
    if primary_name.strip().casefold() not in names:
        return None

    records = Department.query.filter(db.func.lower(Department.name).in_(names)).all()
    by_name = {}
    for record in records:
        by_name.setdefault(record.name.strip().casefold(), []).append(record)
    if any(len(by_name.get(name, [])) != 1 for name in names):
        return None
    primary = by_name[primary_name.strip().casefold()][0]
    secondary = [by_name[name][0] for name in names if name != primary.name.strip().casefold()]
    return primary, secondary


def department_can_view_complaint(department, complaint):
    """Authorize read access through canonical routing or explicit department links."""
    if not department:
        return False
    if complaint.category == MULTI_DEPARTMENT_LEGACY_CATEGORY:
        return bool(department_complaints_query(department).filter_by(id=complaint.id).first())
    canonical_name = CATEGORY_DEPARTMENTS.get(complaint.category)
    if (canonical_name and department.name.casefold() != canonical_name.casefold()
            and not ComplaintDepartment.query.filter_by(
                complaint_id=complaint.id, department_id=department.id,
            ).first()):
        return False
    return bool(department_complaints_query(department).filter_by(id=complaint.id).first())


def department_can_manage_complaint(department, complaint):
    """Authorize workflow changes only where one department owns the workflow."""
    if complaint.category == MULTI_DEPARTMENT_LEGACY_CATEGORY:
        return False
    canonical_name = CATEGORY_DEPARTMENTS.get(complaint.category)
    if not department or not canonical_name or department.name.casefold() != canonical_name.casefold():
        return False
    return department_can_view_complaint(department, complaint)


def complaint_department_states(complaint):
    """Return one display/state row per linked department, including legacy primary rows."""
    records = {row.department_id: row for row in complaint.department_assignments}
    department_ids = set(records)
    if complaint.department_id:
        department_ids.add(complaint.department_id)
    departments = Department.query.filter(Department.id.in_(department_ids)).all() if department_ids else []
    states = []
    for department in sorted(departments, key=lambda row: row.name.casefold()):
        record = records.get(department.id)
        if record is not None and department.id == complaint.department_id:
            # Preserve the legacy current assignee for display without
            # manufacturing department-specific assignment provenance/time.
            if record.assigned_staff_id is None and complaint.assigned_staff_id:
                record.assigned_staff_id = complaint.assigned_staff_id
                record.assigned_staff = complaint.assigned_staff
            if record.resolution_note is None:
                record.resolution_note = complaint.resolution_note
        if record is None:
            is_primary = department.id == complaint.department_id
            assigned_staff_id = complaint.assigned_staff_id if is_primary else None
            record_status = complaint.status if is_primary else "Submitted"
            record = SimpleNamespace(
                complaint_id=complaint.id,
                department_id=department.id,
                assigned_staff_id=assigned_staff_id,
                assigned_staff=complaint.assigned_staff if is_primary else None,
                assigned_by=None,
                staff_assigned_date=None,
                status=record_status,
                resolution_note=complaint.resolution_note if is_primary else None,
            )
            if assigned_staff_id:
                latest_assignment = next(
                    (event for event in reversed(complaint.assignment_history)
                     if event.staff_id == assigned_staff_id
                     and event.department_id in (None, department.id)),
                    None,
                )
                if latest_assignment:
                    record.staff_assigned_date = latest_assignment.assigned_date
                    record.assigned_by = latest_assignment.assigned_by
        record.department = department
        states.append(record)
    return states


def overall_department_status(complaint, department_states=None):
    """Summarize department work without reporting completion while any work remains."""
    if complaint.status == "Ignored":
        return "Ignored"
    states = [row.status for row in (department_states or complaint_department_states(complaint))]
    if not states:
        return complaint.status
    if all(status == "Resolved" for status in states):
        return "Resolved"
    for status in ("In Progress", "Assigned", "Under Review", "Submitted"):
        if status in states:
            return status
    return complaint.status


def active_department_staff(department_id):
    if not department_id:
        return []
    return User.query.filter_by(
        role="staff", department_id=department_id, is_active=True,
    ).order_by(User.name).all()


def dashboard_workload_staff_ids(complaints, staff_by_department):
    """Include current assignees in workload summaries even if now inactive."""
    staff_ids = {
        staff.id
        for staff_list in (staff_by_department or {}).values()
        for staff in staff_list
    }
    for complaint in complaints:
        if complaint.assigned_staff_id is not None:
            staff_ids.add(complaint.assigned_staff_id)
        staff_ids.update(
            state.assigned_staff_id
            for state in complaint.department_assignments
            if state.assigned_staff_id is not None
        )
    return staff_ids


def build_department_assignment_views(complaints, actor, staff_by_department=None,
                                      staff_workloads=None, recommendations_by_key=None):
    """Build per-department dashboard rows with permissions derived server-side."""
    staff_by_department = staff_by_department or {}
    staff_workloads = staff_workloads or {}
    recommendations_by_key = recommendations_by_key or {}
    views_by_complaint = {}
    overall_status_by_complaint = {}
    for complaint in complaints:
        states = complaint_department_states(complaint)
        overall_status_by_complaint[complaint.id] = overall_department_status(complaint, states)
        views = []
        for state in states:
            department_id = state.department_id
            is_linked = department_id == complaint.department_id or any(
                row.department_id == department_id for row in complaint.department_assignments
            )
            may_manage = False
            if is_linked:
                if normalize_role(actor.role) == "admin":
                    may_manage = True
                elif (normalize_role(actor.role) == "department_head"
                      and actor.department_id == department_id):
                    may_manage = True
            if normalize_role(actor.role) == "staff" and state.assigned_staff_id != actor.id:
                continue
            views.append(SimpleNamespace(
                department=state.department,
                department_id=department_id,
                status=state.status,
                assigned_staff=state.assigned_staff,
                assigned_staff_id=state.assigned_staff_id,
                staff_assigned_date=state.staff_assigned_date,
                resolution_note=state.resolution_note,
                can_manage=may_manage,
                staff_options=staff_by_department.get(department_id, []),
                has_available_staff=any(
                    staff_workloads.get(staff.id, 0) < MAX_ACTIVE_STAFF_ASSIGNMENTS
                    or staff.id == state.assigned_staff_id
                    for staff in staff_by_department.get(department_id, [])
                ),
                has_additional_staff_capacity=any(
                    staff_workloads.get(staff.id, 0) < MAX_ACTIVE_STAFF_ASSIGNMENTS
                    for staff in staff_by_department.get(department_id, [])
                ),
                max_active_assignments=MAX_ACTIVE_STAFF_ASSIGNMENTS,
                recommendation=recommendations_by_key.get((complaint.id, department_id)),
                workload_by_staff=staff_workloads,
            ))
        views_by_complaint[complaint.id] = views
    return views_by_complaint, overall_status_by_complaint


def get_department_assignment(complaint, department_id, create=False):
    """Fetch or compatibility-materialize the state row for a linked department."""
    department = db.session.get(Department, department_id) if department_id else None
    if not department:
        return None
    linked = department.id == complaint.department_id or ComplaintDepartment.query.filter_by(
        complaint_id=complaint.id, department_id=department.id,
    ).first() is not None
    if not linked:
        return None
    assignment = ComplaintDepartment.query.filter_by(
        complaint_id=complaint.id, department_id=department.id,
    ).first()
    if assignment or not create:
        return assignment
    assignment = ComplaintDepartment(
        complaint_id=complaint.id,
        department_id=department.id,
        status=complaint.status if department.id == complaint.department_id else "Submitted",
    )
    if department.id == complaint.department_id:
        assignment.assigned_staff_id = complaint.assigned_staff_id
        assignment.resolution_note = complaint.resolution_note
        if complaint.assigned_staff_id:
            latest = next(
                (event for event in reversed(complaint.assignment_history)
                 if event.staff_id == complaint.assigned_staff_id
                 and event.department_id in (None, department.id)),
                None,
            )
            if latest:
                assignment.staff_assigned_date = latest.assigned_date
                assignment.assigned_by = latest.assigned_by
    db.session.add(assignment)
    return assignment


def get_locked_complaint_or_404(complaint_id):
    """Lock the complaint row before a workflow mutation to serialize its state."""
    complaint = db.session.execute(
        db.select(Complaint)
        .where(Complaint.id == complaint_id)
        .with_for_update()
    ).scalar_one_or_none()
    if complaint is None:
        abort(404)
    return complaint


def active_assignment_workloads(staff_ids):
    """Count active department work plus unmaterialized legacy primary assignments once.

    Resolved and Ignored are terminal. A complaint-level legacy assignee is
    counted only when no corresponding primary-department row has an assignee.
    Ignored complaints do not consume active workload while they are paused.
    """
    staff_ids = set(staff_ids or [])
    workloads = {staff_id: 0 for staff_id in staff_ids}
    if not staff_ids:
        return workloads

    active_department_status = or_(
        ComplaintDepartment.status.is_(None),
        ComplaintDepartment.status.notin_(TERMINAL_ASSIGNMENT_STATUSES),
    )
    department_counts = db.session.query(
        ComplaintDepartment.assigned_staff_id, db.func.count(ComplaintDepartment.id)
    ).join(Complaint, Complaint.id == ComplaintDepartment.complaint_id).filter(
        ComplaintDepartment.assigned_staff_id.in_(staff_ids),
        active_department_status,
        or_(Complaint.status.is_(None), Complaint.status != "Ignored"),
    ).group_by(ComplaintDepartment.assigned_staff_id).all()
    for staff_id, count in department_counts:
        workloads[staff_id] += count

    legacy_has_assigned_department_row = Complaint.department_assignments.any(and_(
        ComplaintDepartment.department_id == Complaint.department_id,
        ComplaintDepartment.assigned_staff_id.isnot(None),
    ))
    active_complaint_status = or_(
        Complaint.status.is_(None),
        Complaint.status.notin_(TERMINAL_ASSIGNMENT_STATUSES),
    )
    legacy_counts = db.session.query(
        Complaint.assigned_staff_id, db.func.count(Complaint.id)
    ).filter(
        Complaint.assigned_staff_id.in_(staff_ids),
        active_complaint_status,
        ~legacy_has_assigned_department_row,
    ).group_by(Complaint.assigned_staff_id).all()
    for staff_id, count in legacy_counts:
        workloads[staff_id] += count
    return workloads


def active_assignment_workload(staff_id, *, exclude_complaint_id=None, exclude_department_id=None):
    """Count one candidate's current workload, optionally excluding the edited row."""
    workloads = active_assignment_workloads([staff_id])
    if exclude_complaint_id is None or exclude_department_id is None:
        return workloads.get(staff_id, 0)

    excluded_department_row = ComplaintDepartment.query.filter_by(
        complaint_id=exclude_complaint_id,
        department_id=exclude_department_id,
        assigned_staff_id=staff_id,
    ).filter(
        or_(ComplaintDepartment.status.is_(None),
            ComplaintDepartment.status.notin_(TERMINAL_ASSIGNMENT_STATUSES))
    ).first()
    if excluded_department_row:
        return max(0, workloads.get(staff_id, 0) - 1)

    excluded_legacy_row = Complaint.query.filter_by(
        id=exclude_complaint_id, assigned_staff_id=staff_id,
    ).filter(
        or_(Complaint.status.is_(None), Complaint.status.notin_(TERMINAL_ASSIGNMENT_STATUSES)),
        ~Complaint.department_assignments.any(and_(
            ComplaintDepartment.department_id == Complaint.department_id,
            ComplaintDepartment.assigned_staff_id.isnot(None),
        )),
    ).first()
    if excluded_legacy_row:
        return max(0, workloads.get(staff_id, 0) - 1)
    return workloads.get(staff_id, 0)


def lock_assignment_staff_rows(staff_ids):
    """Serialize workload decisions on staff rows in stable order.

    Under PostgreSQL READ COMMITTED, the workload query after this lock sees
    commits made by a competing assignment that held the same staff row lock.
    Other isolation levels fail closed because their snapshot may be stale.
    """
    staff_ids = sorted({staff_id for staff_id in staff_ids if staff_id is not None})
    if db.engine.dialect.name == "postgresql":
        isolation = db.session.execute(db.text("SHOW transaction_isolation")).scalar_one_or_none()
        if str(isolation).strip().lower() != "read committed":
            return None
    if not staff_ids:
        return {}
    rows = db.session.execute(
        db.select(User)
        .where(User.id.in_(staff_ids))
        .order_by(User.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).scalars().all()
    return {row.id: row for row in rows}


def staff_has_open_work(staff_id):
    """Check department state and legacy primary assignments before staff changes."""
    if ComplaintDepartment.query.filter_by(assigned_staff_id=staff_id) \
            .filter(or_(ComplaintDepartment.status.is_(None),
                        ComplaintDepartment.status.notin_(TERMINAL_ASSIGNMENT_STATUSES))) \
            .first() is not None:
        return True
    return Complaint.query.filter_by(assigned_staff_id=staff_id).filter(
        or_(Complaint.status.is_(None), Complaint.status != "Resolved")
    ).first() is not None


def initialize_database():
    """Create missing tables and apply additive, data-preserving schema changes."""
    with app.app_context():
        # Keep prospective audit tables out of startup create_all until a reviewed migration.
        db.metadata.create_all(
            bind=db.engine,
            tables=application_tables_excluding_audit(db.metadata),
        )
        migrate_schema()
        for name in DEPARTMENT_NAMES:
            Department.query.filter(db.func.lower(Department.name) == name.lower()).first() or db.session.add(
                Department(name=name, email=f"{name.lower().replace(' ', '')}@spsfrms.local")
            )
        db.session.flush()

        # Keep legacy department rows and multi-department links, but route each
        # existing categorized complaint through its canonical department too.
        for complaint in Complaint.query.filter(Complaint.category.in_(CATEGORY_DEPARTMENTS)).all():
            department_name = CATEGORY_DEPARTMENTS[complaint.category]
            department = Department.query.filter(db.func.lower(Department.name) == department_name.lower()).first()
            if department and complaint.department_id != department.id:
                complaint.department_id = department.id
        for complaint in Complaint.query.all():
            if (complaint.assigned_staff_id and (complaint.updated_date or complaint.created_date)
                    and not ComplaintAssignment.query.filter_by(complaint_id=complaint.id).first()):
                db.session.add(ComplaintAssignment(
                    complaint_id=complaint.id,
                    department_id=complaint.department_id,
                    staff_id=complaint.assigned_staff_id,
                    assigned_by=None,
                    assigned_date=complaint.updated_date or complaint.created_date,
                    remarks="Existing assignment recorded during workflow upgrade.",
                ))
            if complaint.created_date and not ComplaintStatusHistory.query.filter_by(complaint_id=complaint.id).first():
                db.session.add(ComplaintStatusHistory(
                    complaint_id=complaint.id,
                    old_status=None,
                    new_status=complaint.status or "Submitted",
                    updated_by=complaint.citizen_id,
                    timestamp=complaint.created_date,
                    remarks="Initial status recorded during workflow upgrade.",
                ))
        db.session.commit()


def migrate_schema():
    """Add nullable assignment columns and migrate legacy officer roles."""
    statements = [
        'ALTER TABLE "user" ADD COLUMN IF NOT EXISTS department_id INTEGER REFERENCES department(id) ON DELETE SET NULL',
        'ALTER TABLE department ADD COLUMN IF NOT EXISTS head_id INTEGER REFERENCES "user"(id) ON DELETE SET NULL',
        'ALTER TABLE complaint ADD COLUMN IF NOT EXISTS assigned_staff_id INTEGER REFERENCES "user"(id) ON DELETE SET NULL',
        'ALTER TABLE complaint ADD COLUMN IF NOT EXISTS resolution_note TEXT',
        'ALTER TABLE complaint_assignment ALTER COLUMN assigned_by DROP NOT NULL',
        'CREATE UNIQUE INDEX IF NOT EXISTS uq_department_head_id ON department(head_id) WHERE head_id IS NOT NULL',
        'CREATE INDEX IF NOT EXISTS ix_user_department_id ON "user"(department_id)',
        'CREATE INDEX IF NOT EXISTS ix_complaint_assigned_staff_id ON complaint(assigned_staff_id)',
        'UPDATE "user" SET role = \'department_head\' WHERE role = \'officer\'',
    ]
    with db.engine.begin() as connection:
        for statement in statements:
            connection.exec_driver_sql(statement)


def create_demo_users():
    """Optionally create configured demo accounts with individual passwords."""
    with app.app_context():
        pending_accounts = []
        admin_password = os.getenv("SPSFRMS_DEMO_ADMIN_PASSWORD")
        admin_email = normalize_email("admin@spsfrms.com")
        if (os.getenv("SPSFRMS_CREATE_DEMO_ADMIN") == "1"
                and not find_user_by_email(admin_email)):
            if not password_meets_policy(admin_password, MIN_NEW_ACCOUNT_PASSWORD_LENGTH):
                raise ValueError("An individual admin password of at least 12 characters is required.")
            pending_accounts.append((admin_email, admin_password))

        seed_data = {"departments": [], "users": []}
        if os.getenv("SPSFRMS_SEED_DEMO_DATA") == "1":
            seed_path = os.getenv(
                "SPSFRMS_SEED_FILE",
                os.path.join(os.path.dirname(__file__), "dev_seed.example.json"),
            )
            with open(seed_path, encoding="utf-8") as seed_file:
                seed_data = json.load(seed_file)
            seed_users = seed_data.get("users", [])
            seed_emails = [normalize_email(account.get("email")) for account in seed_users]
            if any(not email for email in seed_emails) or len(set(seed_emails)) != len(seed_emails):
                raise ValueError("Seed account email configuration is invalid or duplicated.")
            if admin_email in seed_emails:
                raise ValueError("The demo administrator address cannot also be a seeded account.")
            new_seed_emails = [
                email for email in seed_emails if not find_user_by_email(email)
            ]
            supplied_passwords = parse_password_map_json(
                os.getenv("SPSFRMS_SEED_PASSWORDS_JSON", "{}")
            )
            seed_passwords = individual_passwords_for(
                new_seed_emails, supplied_passwords,
                minimum_length=MIN_NEW_ACCOUNT_PASSWORD_LENGTH,
            )
            pending_accounts.extend(zip(new_seed_emails, seed_passwords))

        if pending_accounts:
            require_unique_passwords(
                [password for _, password in pending_accounts],
                (row[0] for row in User.query.with_entities(User.password).all()),
            )
        pending_passwords = dict(pending_accounts)
        if admin_email in pending_passwords:
            db.session.add(User(
                name="System Administrator",
                email=admin_email,
                phone="9876543210",
                password=generate_password_hash(pending_passwords[admin_email]),
                role="admin",
            ))

        if os.getenv("SPSFRMS_SEED_DEMO_DATA") == "1":
            for name in seed_data.get("departments", []):
                if not Department.query.filter(db.func.lower(Department.name) == name.lower()).first():
                    db.session.add(Department(name=name, email=f"{name.lower().replace(' ', '')}@spsfrms.local"))
            db.session.flush()
            for account in seed_data.get("users", []):
                name = account["name"]
                email = normalize_email(account["email"])
                role = account["role"]
                department_name = account["department"]
                password = pending_passwords.get(email)
                user = find_user_by_email(email)
                department = Department.query.filter(db.func.lower(Department.name) == department_name.lower()).first()
                created_user = False
                if not user and department:
                    user = User(
                        name=name,
                        email=email,
                        phone=account.get("phone", "0000000000"),
                        password=generate_password_hash(password),
                        role=role,
                        department_id=department.id,
                    )
                    db.session.add(user)
                    db.session.flush()
                    created_user = True
                if created_user and role == "department_head" and department.head_id is None:
                    department.head_id = user.id
            db.session.commit()
        else:
            db.session.commit()


@app.route("/")
def home():
    return render_template("index.html", categories=CATEGORIES)


@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        email = normalize_email(request.form.get("email", ""))
        phone = request.form.get("phone", "").strip()
        password = request.form.get("password", "")

        if not name or not email or not phone or not password:
            flash("Please fill all fields.", "danger")
            return render_template("register.html")
        if len(password) < 6:
            flash("Password must be at least 6 characters long.", "danger")
            return render_template("register.html")

        if find_user_by_email(email):
            flash("Email already registered. Please login.", "warning")
            return redirect(url_for("login"))

        user = User(
            name=name,
            email=email,
            phone=phone,
            password=generate_password_hash(password),
            role="citizen",
        )
        db.session.add(user)
        db.session.commit()

        flash("Registration successful. Please login.", "success")
        return redirect(url_for("login"))

    return render_template("register.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = normalize_email(request.form.get("email", ""))
        password = request.form.get("password", "")
        selected_role = normalize_role(request.form.get("role", "citizen"))

        user = find_user_by_email(email)

        if user and check_password_hash(user.password, password):
            if normalize_role(user.role) != selected_role:
                flash("Selected role does not match this account.", "danger")
                return render_template("login.html")

            session["user_id"] = user.id
            session["user_name"] = user.name
            session["role"] = normalize_role(user.role)
            flash(f"Welcome back, {user.name}.", "success")
            return redirect_by_role()

        flash("Invalid email, password or role.", "danger")

    return render_template("login.html")


@app.route("/change-password", methods=["GET", "POST"])
@roles_required("citizen", "staff", "department_head", "admin")
def change_password():
    user = current_user()
    if request.method == "POST":
        if not valid_csrf_token(request.form.get("csrf_token", "")):
            abort(400, description="Invalid or missing CSRF token.")

        current_password = request.form.get("current_password", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")

        if not check_password_hash(user.password, current_password):
            flash("The current password is incorrect.", "danger")
        elif not password_meets_policy(new_password, MIN_NEW_ACCOUNT_PASSWORD_LENGTH):
            flash("The new password must be at least 12 characters long.", "danger")
        elif new_password != confirm_password:
            flash("The new password entries do not match.", "danger")
        elif check_password_hash(user.password, new_password):
            flash("Choose a new password that differs from your current password.", "danger")
        else:
            user.password = generate_password_hash(new_password)
            db.session.commit()
            flash("Your password was changed successfully.", "success")
            return redirect(url_for("change_password"))

    return render_template("change_password.html")


@app.route("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("home"))


@app.route("/citizen-dashboard")
@roles_required("citizen")
def citizen_dashboard():
    user = current_user()
    complaints = Complaint.query.filter_by(
        citizen_id=user.id
    ).order_by(Complaint.created_date.desc()).all()

    total = len(complaints)
    status_by_id = {
        complaint.id: overall_department_status(complaint)
        for complaint in complaints
    }
    department_states_by_id = {
        complaint.id: complaint_department_states(complaint)
        for complaint in complaints
    }
    resolved = sum(status_by_id[c.id] == "Resolved" for c in complaints)
    in_progress = sum(status_by_id[c.id] in ["Under Review", "In Progress"] for c in complaints)
    submitted = sum(status_by_id[c.id] == "Submitted" for c in complaints)

    return render_template(
        "citizen_dashboard.html",
        complaints=complaints,
        total=total,
        resolved=resolved,
        in_progress=in_progress,
        submitted=submitted,
        overall_status_by_id=status_by_id,
        department_states_by_id=department_states_by_id,
    )


@app.route("/submit-complaint", methods=["GET", "POST"])
@roles_required("citizen")
def submit_complaint():
    user = current_user()
    if request.method == "POST":
        complaint_text = request.form.get("complaint_text", "").strip()
        location = request.form.get("location", "").strip()

        if not complaint_text or not location:
            flash("Please describe the issue and provide a location.", "danger")
            return render_template("submit_complaint.html", form=request.form)
        if len(complaint_text) > 5000:
            flash("Complaint descriptions must be 5,000 characters or fewer.", "danger")
            return render_template("submit_complaint.html", form=request.form)

        analysis = analyze_complaint(complaint_text)
        final_category = analysis["category"]
        priority = analysis["priority"]
        keywords = analysis["keywords"]

        resolved_departments = resolve_analysis_departments(analysis)
        if resolved_departments is None:
            flash("One or more responsible departments are unavailable. Please try again later.", "danger")
            return render_template("submit_complaint.html", form=request.form)
        department, secondary_departments = resolved_departments

        image_name = None
        file = request.files.get("image")
        if file and file.filename:
            if not app.config["IMAGE_UPLOADS_ENABLED"]:
                flash(
                    "Image uploads are disabled on this deployment. Remove the image and submit the complaint text again.",
                    "warning",
                )
                return render_template("submit_complaint.html", form=request.form)
            if not allowed_file(file.filename):
                flash("Only JPG, JPEG and PNG images are allowed.", "danger")
                return render_template("submit_complaint.html", form=request.form)

            image_name = secure_filename(file.filename)
            name, extension = os.path.splitext(image_name)
            image_name = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{name}{extension}"
            os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
            file.save(os.path.join(app.config["UPLOAD_FOLDER"], image_name))

        submitted_at = utcnow()
        complaint = Complaint(
            complaint_text=complaint_text,
            category=final_category,
            location=location,
            image=image_name,
            keywords=", ".join(keywords),
            priority=priority,
            status="Submitted",
            created_date=submitted_at,
            citizen_id=user.id,
            department_id=department.id,
        )

        db.session.add(complaint)
        db.session.flush()
        db.session.add_all([
            ComplaintDepartment(complaint_id=complaint.id, department_id=linked.id)
            for linked in [department, *secondary_departments]
        ])
        db.session.add(ComplaintStatusHistory(
            complaint_id=complaint.id,
            old_status=None,
            new_status="Submitted",
            updated_by=user.id,
            timestamp=submitted_at,
            remarks="Complaint submitted and text analysis completed.",
        ))
        db.session.commit()

        flash(
            f"Complaint CMP{complaint.id:03d} submitted. Category: {final_category} · "
            f"Priority: {priority} · Routed to {department_label(final_category)}. "
            f"{analysis['reason']}",
            "success",
        )
        return redirect(url_for("citizen_dashboard"))

    return render_template("submit_complaint.html", form={})


@app.route("/analyze-preview", methods=["POST"])
@roles_required("citizen")
def analyze_preview():
    """Live preview of the same keyword analysis used on submission."""
    data = request.get_json(silent=True) or {}
    text = str(data.get("text", ""))[:5000]
    analysis = analyze_complaint(text)
    return {
        "keywords": analysis["keywords"],
        "category": analysis["category"],
        "categories": analysis["categories"],
        "secondary_categories": analysis["secondary_categories"],
        "priority": analysis["priority"],
        "department": analysis["department"],
        "departments": analysis["departments"],
        "secondary_departments": analysis["secondary_departments"],
        "reason": analysis["reason"],
        "needs_review": analysis["needs_review"],
        "emergency": analysis["emergency"],
    }


@app.route("/track-complaint/<int:complaint_id>")
@roles_required("citizen", "department_head", "staff", "admin")
def track_complaint(complaint_id):
    user = current_user()
    complaint = db.get_or_404(Complaint, complaint_id)

    if user.role == "citizen" and complaint.citizen_id != user.id:
        abort(403)
    if normalize_role(user.role) == "department_head" and not department_can_view_complaint(user.department, complaint):
        abort(403)
    all_department_states = complaint_department_states(complaint)
    if user.role == "staff" and not any(
        row.assigned_staff_id == user.id and row.department_id == user.department_id
        for row in all_department_states
    ):
        abort(403)

    if user.role == "citizen" or user.role == "admin":
        visible_department_states = all_department_states
    elif normalize_role(user.role) == "department_head":
        visible_department_states = [
            row for row in all_department_states if row.department_id == user.department_id
        ]
    else:
        visible_department_states = [
            row for row in all_department_states
            if row.department_id == user.department_id and row.assigned_staff_id == user.id
        ]

    return render_template(
        "track_complaint.html",
        complaint=complaint,
        linked_departments=[row.department.name for row in visible_department_states],
        department_progress=visible_department_states,
        display_status=overall_department_status(complaint, all_department_states),
        multi_department_review=complaint.category == MULTI_DEPARTMENT_LEGACY_CATEGORY,
        status_history=complaint.status_history,
        analysis=analyze_complaint(complaint.complaint_text),
    )


@app.route("/officer-dashboard")
@roles_required("department_head")
def officer_dashboard():
    user = current_user()
    queue_filter = request.args.get("queue", "active")
    complaints_query = department_complaints_query(user.department) if user.department else Complaint.query.filter(False)
    if queue_filter == "ignored":
        complaints_query = complaints_query.filter_by(status="Ignored")
    else:
        complaints_query = complaints_query.filter(Complaint.status != "Ignored")
    complaints = complaints_query.all()
    if user.department:
        complaints = [
            complaint for complaint in complaints
            if department_can_view_complaint(user.department, complaint)
        ]
    staff_members = active_department_staff(user.department_id)
    staff_workloads = active_assignment_workloads(
        dashboard_workload_staff_ids(
            complaints, {user.department_id: staff_members} if user.department_id else {},
        )
    )
    staff_with_capacity = [
        staff for staff in staff_members
        if staff_workloads.get(staff.id, 0) < MAX_ACTIVE_STAFF_ASSIGNMENTS
    ]
    recommendation_artifact = load_model_artifact(
        os.getenv("SPSFRMS_STAFF_MODEL_PATH", "")
    )
    recommendations_by_key = {
        (complaint.id, user.department_id): recommend_staff(
            complaint.complaint_text,
            complaint.category,
            user.department_id,
            staff_with_capacity,
            staff_workloads,
            recommendation_artifact,
        )
        for complaint in complaints
        if (user.department and department_can_view_complaint(user.department, complaint)
            and complaint.category != MULTI_DEPARTMENT_LEGACY_CATEGORY
            and complaint.status in {"Submitted", "Assigned"})
    }
    return render_staff_dashboard(
        complaints,
        dashboard_role="department head",
        can_assign=True,
        department=user.department,
        staff_members=staff_members,
        staff_by_department={user.department_id: staff_members} if user.department_id else {},
        staff_workloads=staff_workloads,
        recommendations_by_key=recommendations_by_key,
        actor=user,
        queue_filter=queue_filter,
        can_manage_by_id={
            complaint.id: department_can_manage_complaint(user.department, complaint)
            for complaint in complaints
        },
    )


@app.route("/staff-dashboard")
@roles_required("staff")
def staff_dashboard():
    user = current_user()
    if not user.is_active:
        abort(403)
    complaints = Complaint.query.filter(
        Complaint.status != "Ignored",
        or_(
            Complaint.assigned_staff_id == user.id,
            Complaint.department_assignments.any(and_(
                ComplaintDepartment.assigned_staff_id == user.id,
                ComplaintDepartment.status != "Ignored",
            )),
        ),
    ).all()
    return render_staff_dashboard(
        complaints, dashboard_role="staff", can_assign=False, department=user.department,
        actor=user,
    )


def render_staff_dashboard(complaints, dashboard_role, can_assign, department=None, staff_members=None,
                          staff_workloads=None, queue_filter="active", dashboard_title="Complaint response desk",
                          can_manage_by_id=None, recommendations_by_id=None, actor=None,
                          staff_by_department=None, recommendations_by_key=None):
    complaints = sorted(complaints, key=priority_order_key)
    analysis_by_id = {c.id: analyze_complaint(c.complaint_text) for c in complaints}
    linked_departments_by_id = {}
    for complaint in complaints:
        names = {row.department.name for row in complaint.department_assignments if row.department}
        if complaint.department:
            names.add(complaint.department.name)
        linked_departments_by_id[complaint.id] = sorted(names)
    age_by_id = {c.id: complaint_age_label(c.created_date) for c in complaints}
    views_by_complaint, overall_status_by_complaint = build_department_assignment_views(
        complaints, actor or current_user(), staff_by_department,
        staff_workloads, recommendations_by_key,
    )
    new_count = sum(overall_status_by_complaint[c.id] == "Submitted" for c in complaints)
    in_progress = sum(overall_status_by_complaint[c.id] in ["Under Review", "In Progress"] for c in complaints)
    resolved = sum(overall_status_by_complaint[c.id] == "Resolved" for c in complaints)
    high_priority = sum(c.priority == "High" for c in complaints)

    return render_template(
        "officer_dashboard.html",
        complaints=complaints,
        new_count=new_count,
        in_progress=in_progress,
        resolved=resolved,
        high_priority=high_priority,
        dashboard_role=dashboard_role,
        dashboard_title=dashboard_title,
        department=department,
        can_assign=can_assign,
        can_manage_by_id=can_manage_by_id or {},
        multi_department_review_category=MULTI_DEPARTMENT_LEGACY_CATEGORY,
        staff_members=staff_members or [],
        staff_workloads=staff_workloads or {},
        recommendations_by_id=recommendations_by_id or {},
        analysis_by_id=analysis_by_id,
        linked_departments_by_id=linked_departments_by_id,
        department_assignment_views_by_complaint=views_by_complaint,
        overall_status_by_complaint=overall_status_by_complaint,
        age_by_id=age_by_id,
        queue_filter=queue_filter,
    )


@app.route("/update-status/<int:complaint_id>", methods=["POST"])
@roles_required("staff", "admin")
def update_status(complaint_id):
    user = current_user()
    if not valid_csrf_token(request.form.get("csrf_token", "")):
        abort(400, description="Invalid or missing CSRF token.")
    complaint = get_locked_complaint_or_404(complaint_id)
    department_id = request.form.get("department_id", type=int) or complaint.department_id
    department_assignment = get_department_assignment(complaint, department_id, create=True)
    if not department_assignment:
        abort(403)
    if user.role == "staff" and (
        not user.is_active or department_assignment.assigned_staff_id != user.id
        or department_assignment.department_id != user.department_id
    ):
        abort(403)
    if complaint.status == "Ignored":
        flash("This complaint is ignored. Restore it before changing its status.", "warning")
        return _status_redirect(complaint)
    new_status = request.form.get("status")

    if new_status == "In Progress" and department_assignment.status == "In Progress":
        flash(f"CMP{complaint.id:03d} is already In Progress for {department_assignment.department.name}.", "info")
        return _status_redirect(complaint)

    starting_statuses = {"Submitted", "Assigned"}
    if not (
        new_status == "In Progress" and department_assignment.status in starting_statuses
        or new_status == "Resolved" and department_assignment.status == "In Progress"
    ):
        flash("That status change is not available for this complaint.", "warning")
        return _status_redirect(complaint)

    remarks = request.form.get("remarks", "").strip()
    if new_status == "Resolved" and not remarks:
        flash("Add a resolution note before marking this complaint resolved.", "danger")
        return _status_redirect(complaint)

    old_status = department_assignment.status
    department_assignment.status = new_status
    if department_assignment.department_id == complaint.department_id:
        complaint.resolution_note = remarks if new_status == "Resolved" else complaint.resolution_note
    if new_status == "Resolved":
        department_assignment.resolution_note = remarks
    db.session.add(ComplaintStatusHistory(
        complaint_id=complaint.id,
        department_id=department_assignment.department_id,
        old_status=old_status,
        new_status=new_status,
        updated_by=user.id,
        timestamp=utcnow(),
        remarks=remarks or None,
    ))
    complaint.status = overall_department_status(
        complaint,
        [row for row in complaint_department_states(complaint)
         if row.department_id != department_assignment.department_id]
        + [department_assignment],
    )
    db.session.commit()
    flash(f"{department_assignment.department.name} status for CMP{complaint.id:03d} updated to {new_status}.", "success")

    return _status_redirect(complaint)


def _status_redirect(complaint):
    if request.form.get("return_to") == "track":
        return redirect(url_for("track_complaint", complaint_id=complaint.id))
    return redirect_by_role()


@app.route("/admin-dashboard")
@roles_required("admin")
def admin_dashboard():
    users = User.query.count()
    officers = User.query.filter(User.role.in_(["department_head", "staff", "officer"])).count()
    citizens = User.query.filter_by(role="citizen").count()
    departments = Department.query.count()
    complaints = Complaint.query.count()
    resolved = Complaint.query.filter_by(status="Resolved").count()
    pending = Complaint.query.filter(Complaint.status != "Resolved").count()
    recent_complaints = Complaint.query.order_by(Complaint.created_date.desc()).limit(8).all()

    return render_template(
        "admin_dashboard.html",
        users=users,
        officers=officers,
        citizens=citizens,
        departments=departments,
        complaints=complaints,
        resolved=resolved,
        pending=pending,
        recent_complaints=recent_complaints,
        all_users=User.query.order_by(User.name).all(),
        all_departments=Department.query.order_by(Department.name).all(),
    )


@app.route("/admin/complaints")
@roles_required("admin")
def admin_complaints():
    complaints = Complaint.query.order_by(Complaint.created_date.desc()).all()
    department_ids = {
        department_id
        for complaint in complaints
        for department_id in (
            [complaint.department_id] + [row.department_id for row in complaint.department_assignments]
        )
        if department_id
    }
    staff_by_department = {
        department_id: active_department_staff(department_id)
        for department_id in department_ids
    }
    staff_ids = dashboard_workload_staff_ids(complaints, staff_by_department)
    staff_workloads = active_assignment_workloads(staff_ids)
    return render_staff_dashboard(
        complaints,
        dashboard_role="administrator",
        can_assign=True,
        dashboard_title="All complaints",
        actor=current_user(),
        staff_by_department=staff_by_department,
        staff_workloads=staff_workloads,
    )


@app.route("/admin/manage", methods=["POST"])
@roles_required("admin")
def admin_manage():
    action = request.form.get("action")
    if action == "create_department":
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip() or None
        if not name:
            flash("Department name is required.", "danger")
        elif Department.query.filter(db.func.lower(Department.name) == name.lower()).first():
            flash("That department already exists.", "warning")
        else:
            db.session.add(Department(name=name, email=email))
            db.session.commit()
            flash(f"{name} department created.", "success")
        return redirect(url_for("admin_dashboard"))

    if action == "create_user":
        if not valid_csrf_token(request.form.get("csrf_token", "")):
            abort(400, description="Invalid or missing CSRF token.")
        name = request.form.get("name", "").strip()
        email = normalize_email(request.form.get("email", ""))
        phone = request.form.get("phone", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role", "")
        department = db.session.get(Department, request.form.get("department_id", type=int))
        if (not name or not email or not phone
                or not password_meets_policy(password, MIN_NEW_ACCOUNT_PASSWORD_LENGTH)
                or role not in {"department_head", "staff"} or not department):
            flash("Enter valid account details, a department, and a password of at least 12 characters.", "danger")
        elif find_user_by_email(email):
            flash("That email is already registered.", "warning")
        elif role == "department_head" and department.head_id:
            flash("This department already has a department head.", "warning")
        else:
            try:
                require_unique_passwords(
                    [password],
                    (row[0] for row in User.query.with_entities(User.password).all()),
                )
            except ValueError:
                flash("Use a unique password that is not assigned to another account.", "danger")
                return redirect(url_for("admin_dashboard"))
            user = User(
                name=name,
                email=email,
                phone=phone,
                password=generate_password_hash(password),
                role=role,
                department_id=department.id,
            )
            db.session.add(user)
            db.session.flush()
            if role == "department_head":
                department.head_id = user.id
            db.session.commit()
            flash(f"{role.replace('_', ' ').title()} account created.", "success")
        return redirect(url_for("admin_dashboard"))

    if action == "reset_password":
        if not valid_csrf_token(request.form.get("csrf_token", "")):
            abort(400, description="Invalid or missing CSRF token.")

        target_id = request.form.get("user_id", type=int)
        target = db.session.get(User, target_id) if target_id else None
        if not target or normalize_role(target.role) not in {"department_head", "staff"}:
            flash("Select a valid staff or department-head account.", "danger")
            return redirect(url_for("admin_dashboard"))

        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")
        if not password_meets_policy(new_password, MIN_NEW_ACCOUNT_PASSWORD_LENGTH):
            flash("Password must be at least 12 characters long.", "danger")
        elif new_password != confirm_password:
            flash("The password entries do not match.", "danger")
        else:
            target.password = generate_password_hash(new_password)
            db.session.commit()
            flash(f"Password updated for {target.name}.", "success")
        return redirect(url_for("admin_dashboard"))

    if action == "assign_user":
        user = db.session.get(User, request.form.get("user_id", type=int))
        role = request.form.get("role", "")
        department = db.session.get(Department, request.form.get("department_id", type=int))
        if user and (user.role == "staff" or role == "staff"):
            locked_staff = lock_assignment_staff_rows([user.id])
            if locked_staff is None:
                db.session.rollback()
                flash("Staff eligibility cannot be safely updated under the current database isolation level.", "danger")
                return redirect(url_for("admin_dashboard"))
            user = locked_staff.get(user.id)
        if not user or role not in ROLES or (role in {"department_head", "staff"} and not department):
            flash("Select a valid account, role, and department.", "danger")
        elif role == "department_head" and department.head_id not in (None, user.id):
            flash("This department already has a department head.", "warning")
        elif (user.role == "staff" and (role != "staff" or user.department_id != department.id)
              and staff_has_open_work(user.id)):
            flash("Reassign this staff member's active department work before changing their role or department.", "warning")
        else:
            if user.headed_department:
                user.headed_department.head_id = None
            user.role = role
            user.department_id = department.id if role in {"department_head", "staff"} else None
            if role == "department_head":
                department.head_id = user.id
            db.session.commit()
            flash(f"{user.name}'s role and department were updated.", "success")
        return redirect(url_for("admin_dashboard"))

    if action == "set_staff_active":
        if not valid_csrf_token(request.form.get("csrf_token", "")):
            abort(400, description="Invalid or missing CSRF token.")
        user = db.session.get(User, request.form.get("user_id", type=int))
        if user and user.role == "staff":
            locked_staff = lock_assignment_staff_rows([user.id])
            if locked_staff is None:
                db.session.rollback()
                flash("Staff eligibility cannot be safely updated under the current database isolation level.", "danger")
                return redirect(url_for("admin_dashboard"))
            user = locked_staff.get(user.id)
        active_value = request.form.get("is_active")
        if not user or user.role != "staff" or active_value not in {"true", "false"}:
            flash("Select a valid staff account and active-state action.", "danger")
        elif active_value == "false" and staff_has_open_work(user.id):
            flash("Reassign this staff member's active department work before deactivating the account.", "warning")
        else:
            user.is_active = active_value == "true"
            db.session.commit()
            flash(f"Staff account {'activated' if user.is_active else 'deactivated'}.", "success")
        return redirect(url_for("admin_dashboard"))

    flash("Choose a valid account-management action.", "warning")
    return redirect(url_for("admin_dashboard"))


@app.route("/assign-complaint/<int:complaint_id>", methods=["POST"])
@roles_required("department_head", "admin")
def assign_complaint(complaint_id):
    actor = current_user()
    if not valid_csrf_token(request.form.get("csrf_token", "")):
        abort(400, description="Invalid or missing CSRF token.")
    complaint = get_locked_complaint_or_404(complaint_id)
    department_id = request.form.get("department_id", type=int) or complaint.department_id
    department = db.session.get(Department, department_id) if department_id else None
    if not department:
        abort(403)
    if normalize_role(actor.role) == "department_head" and actor.department_id != department.id:
        abort(403)
    department_assignment = get_department_assignment(complaint, department.id, create=True)
    if not department_assignment:
        abort(403)
    if department_assignment.status == "Resolved":
        flash(f"Resolved {department.name} work cannot be assigned or reassigned.", "warning")
        return redirect_by_role()
    if complaint.status == "Ignored":
        flash("Restore this complaint before assigning staff.", "warning")
        return redirect_by_role()
    if department_assignment.status not in {"Submitted", "Assigned"}:
        flash("Staff can only be assigned before this department starts work.", "warning")
        return redirect_by_role()

    staff_id = request.form.get("staff_id", type=int)
    previous_staff_id = department_assignment.assigned_staff_id
    locked_staff = lock_assignment_staff_rows([previous_staff_id, staff_id])
    if locked_staff is None:
        db.session.rollback()
        flash("Assignments cannot be safely changed under the current database isolation level. Please retry.", "danger")
        return redirect_by_role()
    staff = locked_staff.get(staff_id) if staff_id is not None else None
    if not staff:
        flash("Select an available staff account.", "danger")
        return redirect_by_role()
    if staff.role != "staff" or not staff.is_active or staff.department_id != department.id:
        abort(403)

    if department_assignment.status == "Assigned" and department_assignment.assigned_staff_id == staff.id:
        flash(f"CMP{complaint.id:03d} is already assigned to {staff.name}.", "info")
        return redirect_by_role()

    workload = active_assignment_workload(
        staff.id,
        exclude_complaint_id=complaint.id,
        exclude_department_id=department.id,
    )
    if workload >= MAX_ACTIVE_STAFF_ASSIGNMENTS:
        db.session.rollback()
        flash(
            f"This staff member already has {workload} active assignments. "
            "Please select another eligible staff member.",
            "warning",
        )
        return redirect_by_role()

    previous_status = department_assignment.status
    assigned_at = utcnow()
    department_assignment.assigned_staff_id = staff.id
    department_assignment.assigned_by = actor.id
    department_assignment.staff_assigned_date = assigned_at
    department_assignment.status = "Assigned"
    department_assignment.resolution_note = None
    if department.id == complaint.department_id:
        complaint.assigned_staff_id = staff.id
        complaint.resolution_note = None
    db.session.add(ComplaintAssignment(
        complaint_id=complaint.id,
        department_id=department.id,
        staff_id=staff.id,
        assigned_by=actor.id,
        assigned_date=assigned_at,
        remarks=request.form.get("remarks", "").strip() or None,
    ))
    if previous_status != "Assigned":
        db.session.add(ComplaintStatusHistory(
            complaint_id=complaint.id,
            department_id=department.id,
            old_status=previous_status,
            new_status="Assigned",
            updated_by=actor.id,
            timestamp=assigned_at,
            remarks=request.form.get("remarks", "").strip() or f"Assigned to {staff.name}.",
        ))
    complaint.status = overall_department_status(
        complaint,
        [row for row in complaint_department_states(complaint)
         if row.department_id != department.id]
        + [department_assignment],
    )
    db.session.commit()
    flash(f"{department.name} assignment for CMP{complaint.id:03d} set to {staff.name}.", "success")
    return redirect_by_role()


@app.route("/ignore-complaint/<int:complaint_id>", methods=["POST"])
@roles_required("department_head")
def ignore_complaint(complaint_id):
    head = current_user()
    if not valid_csrf_token(request.form.get("csrf_token", "")):
        abort(400, description="Invalid or missing CSRF token.")
    complaint = get_locked_complaint_or_404(complaint_id)
    if not department_can_manage_complaint(head.department, complaint):
        abort(403)
    if complaint.status not in {"Submitted", "Under Review", "Assigned", "In Progress"}:
        flash("Only active complaints can be ignored.", "warning")
        return redirect(url_for("officer_dashboard"))
    reason = request.form.get("reason", "").strip()[:1000]
    if not reason:
        flash("Enter a reason before ignoring this complaint.", "danger")
        return redirect(url_for("officer_dashboard"))
    previous_status = complaint.status
    complaint.status = "Ignored"
    db.session.add(ComplaintStatusHistory(
        complaint_id=complaint.id,
        old_status=previous_status,
        new_status="Ignored",
        updated_by=head.id,
        timestamp=utcnow(),
        remarks=reason,
    ))
    db.session.commit()
    flash(f"CMP{complaint.id:03d} moved to the ignored queue.", "success")
    return redirect(url_for("officer_dashboard"))


@app.route("/restore-complaint/<int:complaint_id>", methods=["POST"])
@roles_required("department_head")
def restore_complaint(complaint_id):
    head = current_user()
    if not valid_csrf_token(request.form.get("csrf_token", "")):
        abort(400, description="Invalid or missing CSRF token.")
    complaint = get_locked_complaint_or_404(complaint_id)
    if not department_can_manage_complaint(head.department, complaint):
        abort(403)
    if complaint.status != "Ignored":
        flash("This complaint is not in the ignored queue.", "warning")
        return redirect(url_for("officer_dashboard"))
    ignored_event = ComplaintStatusHistory.query.filter_by(
        complaint_id=complaint.id, new_status="Ignored"
    ).order_by(ComplaintStatusHistory.timestamp.desc(), ComplaintStatusHistory.id.desc()).first()
    valid_restore_statuses = {"Submitted", "Under Review", "Assigned", "In Progress"}
    restored_status = (
        ignored_event.old_status
        if ignored_event and ignored_event.old_status in valid_restore_statuses
        else "Submitted"
    )

    # Restoring an ignored complaint makes its existing assignments active
    # again. Serialize on every affected staff row before checking capacity.
    department_states = ComplaintDepartment.query.filter_by(complaint_id=complaint.id).all()
    returning_work = {}
    required_departments = {}
    for state in department_states:
        if (state.assigned_staff_id is not None
                and (state.status is None or state.status not in TERMINAL_ASSIGNMENT_STATUSES)):
            staff_id = state.assigned_staff_id
            returning_work[staff_id] = returning_work.get(staff_id, 0) + 1
            required_departments.setdefault(staff_id, set()).add(state.department_id)

    primary_has_department_assignee = any(
        state.department_id == complaint.department_id and state.assigned_staff_id is not None
        for state in department_states
    )
    if complaint.assigned_staff_id is not None and not primary_has_department_assignee:
        if complaint.department_id is None:
            db.session.rollback()
            flash("This legacy assignment has no department and cannot be safely restored.", "danger")
            return redirect(url_for("officer_dashboard"))
        staff_id = complaint.assigned_staff_id
        returning_work[staff_id] = returning_work.get(staff_id, 0) + 1
        required_departments.setdefault(staff_id, set()).add(complaint.department_id)

    locked_staff = lock_assignment_staff_rows(returning_work)
    if locked_staff is None:
        db.session.rollback()
        flash("This complaint cannot be safely restored under the current database isolation level.", "danger")
        return redirect(url_for("officer_dashboard"))
    for staff_id, department_ids in required_departments.items():
        staff = locked_staff.get(staff_id)
        if (len(department_ids) != 1 or not staff or staff.role != "staff"
                or not staff.is_active or staff.department_id not in department_ids):
            db.session.rollback()
            flash("This complaint cannot be restored until its assigned staff eligibility is valid.", "danger")
            return redirect(url_for("officer_dashboard"))
    current_work = active_assignment_workloads(returning_work)
    if any(
        current_work.get(staff_id, 0) + returning_count > MAX_ACTIVE_STAFF_ASSIGNMENTS
        for staff_id, returning_count in returning_work.items()
    ):
        db.session.rollback()
        flash(
            "This complaint cannot be restored because one or more assigned staff members are at capacity. "
            "Free capacity or reassign the affected work before restoring it.",
            "warning",
        )
        return redirect(url_for("officer_dashboard"))

    complaint.status = restored_status
    db.session.add(ComplaintStatusHistory(
        complaint_id=complaint.id,
        old_status="Ignored",
        new_status=restored_status,
        updated_by=head.id,
        timestamp=utcnow(),
        remarks="Restored by department head.",
    ))
    db.session.commit()
    flash(f"CMP{complaint.id:03d} restored to the active queue.", "success")
    return redirect(url_for("officer_dashboard"))


def inspect_database_schema_readiness():
    """Return schema state and version without creating or changing objects."""
    if db.engine.dialect.name != "postgresql":
        return "unsupported_dialect", None

    inspector = sqlalchemy_inspect(db.engine)
    actual_tables = set(inspector.get_table_names())
    application_tables = application_tables_excluding_audit(db.metadata)
    missing_tables = [table.name for table in application_tables if table.name not in actual_tables]
    for table in application_tables:
        if table.name not in actual_tables:
            continue
        actual_columns = {column["name"] for column in inspector.get_columns(table.name)}
        if any(column.name not in actual_columns for column in table.columns):
            return "missing_required_objects", None
    if missing_tables:
        return "missing_required_objects", None

    raw_connection = db.engine.raw_connection()
    try:
        with raw_connection.cursor() as cursor:
            cursor.execute(
                "SELECT version, schema_fingerprint "
                "FROM public.spsfrms_schema_migration ORDER BY version DESC LIMIT 1"
            )
            migration_row = cursor.fetchone()
        if migration_row is None:
            return "unversioned", None
        current_fingerprint = schema_fingerprint(raw_connection)
    finally:
        try:
            raw_connection.rollback()
        finally:
            raw_connection.close()

    version, recorded_fingerprint = migration_row
    if version != LATEST_REQUIRED_VERSION:
        return "migration_required", version
    if recorded_fingerprint.strip() != current_fingerprint:
        return "fingerprint_mismatch", version
    return "current", version


@app.route("/health")
def health():
    try:
        db.session.execute(db.text("SELECT 1"))
    except Exception as health_error:
        app.logger.error("Database health check failed (%s).", type(health_error).__name__)
        return {
            "status": "error",
            "process": "running",
            "database": "unavailable",
            "schema": "unknown",
            "ready": False,
        }, 503

    try:
        schema_state, migration_version = inspect_database_schema_readiness()
    except Exception as health_error:
        app.logger.error("Schema readiness check failed (%s).", type(health_error).__name__)
        return {
            "status": "degraded",
            "process": "running",
            "database": "connected",
            "schema": "unknown",
            "ready": False,
        }, 503

    if schema_state == "current":
        return {
            "status": "ok",
            "process": "running",
            "database": "connected",
            "schema": "current",
            "migration_version": migration_version,
            "ready": True,
        }, 200
    return {
        "status": "degraded",
        "process": "running",
        "database": "connected",
        "schema": schema_state,
        "ready": False,
    }, 503


@app.route("/livez")
def livez():
    """Report process liveness without requiring database availability."""
    return {"process": "running"}, 200


def startup_database_mode():
    """Read explicit startup mode; default to no database initialization."""
    configured_mode = os.getenv("SPSFRMS_STARTUP_MODE")
    if configured_mode is not None:
        return configured_mode.strip().lower()
    if os.getenv("SPSFRMS_SKIP_STARTUP_DB_INIT") == "1":
        return "skip"
    return "skip"


def run_startup_database_setup():
    """Run only when explicitly invoked with SPSFRMS_STARTUP_MODE=initialize."""
    mode = startup_database_mode()
    if mode == "skip":
        app.config["DATABASE_INITIALIZATION_STATE"] = "not_run"
        return
    if mode != "initialize":
        app.config["DATABASE_INITIALIZATION_STATE"] = "configuration_error"
        app.logger.error("Invalid SPSFRMS_STARTUP_MODE; expected 'initialize' or 'skip'.")
        return

    app.config["DATABASE_INITIALIZATION_STATE"] = "pending"
    with app.app_context():
        try:
            initialize_database()
            create_demo_users()
            app.config["DATABASE_INITIALIZATION_STATE"] = "initialized"
        except Exception as startup_error:
            app.config["DATABASE_INITIALIZATION_STATE"] = "failed"
            app.logger.error("Database startup setup failed (%s).", type(startup_error).__name__)
            try:
                db.session.rollback()
            except Exception as rollback_error:
                app.logger.error("Database startup rollback failed (%s).", type(rollback_error).__name__)


if __name__ == "__main__":
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    app.run(debug=True)
