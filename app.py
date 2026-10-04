from flask import Flask, render_template, request, redirect, url_for, session, flash, abort, g
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from datetime import datetime
from functools import wraps
from dotenv import load_dotenv
import json
import os
import re

load_dotenv()

app = Flask(__name__)

app.config["SECRET_KEY"] = os.getenv("SECRET_KEY", "spsfrms-local-secret-key")

database_url = os.getenv("DATABASE_URL")
if database_url:
    if database_url.startswith("postgres://"):
        database_url = database_url.replace("postgres://", "postgresql://", 1)
    if database_url.startswith("postgresql://"):
        database_url = database_url.replace("postgresql://", "postgresql+psycopg://", 1)
    app.config["SQLALCHEMY_DATABASE_URI"] = database_url
else:
    app.config["SQLALCHEMY_DATABASE_URI"] = (
        "postgresql+psycopg://postgres:YOUR_PASSWORD@localhost:5432/spsfrms_db"
    )

app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.config["UPLOAD_FOLDER"] = "uploads"
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024


db = SQLAlchemy(app)

ALLOWED_EXTENSIONS = {"jpg", "jpeg", "png"}
CATEGORIES = [
    "Road Damage", "Garbage", "Water Leakage", "Street Light",
    "Drainage", "Electricity", "Traffic", "Fire", "Other"
]
STATUSES = ["Submitted", "Under Review", "Assigned", "In Progress", "Resolved"]
ROLES = {"citizen", "department_head", "staff", "admin"}
CATEGORY_DEPARTMENTS = {
    "Road Damage": "Road Maintenance",
    "Garbage": "Waste Management",
    "Water Leakage": "Water Supply",
    "Street Light": "Street Light",
    "Drainage": "Public Health",
    "Electricity": "Electricity",
    "Traffic": "Police",
    "Fire": "Fire Department",
    "Other": "Public Health",
}
LEGACY_DEPARTMENT_NAMES = {
    "Road Maintenance": {"Road Damage"},
    "Waste Management": {"Garbage"},
    "Water Supply": {"Water Leakage"},
    "Street Light": set(),
    "Public Health": {"Drainage", "Other"},
    "Electricity": set(),
    "Police": {"Traffic"},
    "Fire Department": {"Fire"},
}
DEPARTMENT_NAMES = sorted(set(CATEGORY_DEPARTMENTS.values()))


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    phone = db.Column(db.String(20), nullable=False)
    password = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), default="citizen")
    registered_date = db.Column(db.DateTime, default=datetime.utcnow)
    department_id = db.Column(db.Integer, db.ForeignKey("department.id", ondelete="SET NULL"))

    complaints = db.relationship("Complaint", backref="citizen", lazy=True, foreign_keys="Complaint.citizen_id")
    department = db.relationship("Department", foreign_keys=[department_id], back_populates="members")
    headed_department = db.relationship("Department", foreign_keys="Department.head_id", back_populates="head", uselist=False)
    assigned_complaints = db.relationship("Complaint", back_populates="assigned_staff", foreign_keys="Complaint.assigned_staff_id")


class Department(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    email = db.Column(db.String(120))
    head_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"))

    head = db.relationship("User", foreign_keys=[head_id], back_populates="headed_department")
    members = db.relationship("User", foreign_keys=[User.department_id], back_populates="department")
    complaints = db.relationship("Complaint", back_populates="department", foreign_keys="Complaint.department_id")


class ComplaintDepartment(db.Model):
    __tablename__ = "complaint_department"
    __table_args__ = (db.UniqueConstraint("complaint_id", "department_id", name="uq_complaint_department"),)

    id = db.Column(db.Integer, primary_key=True)
    complaint_id = db.Column(db.Integer, db.ForeignKey("complaint.id", ondelete="CASCADE"), nullable=False)
    department_id = db.Column(db.Integer, db.ForeignKey("department.id", ondelete="CASCADE"), nullable=False)
    assigned_date = db.Column(db.DateTime, default=datetime.utcnow)

    complaint = db.relationship("Complaint", back_populates="department_assignments")
    department = db.relationship("Department")


class Complaint(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    complaint_text = db.Column(db.Text, nullable=False)
    category = db.Column(db.String(100), nullable=False)
    location = db.Column(db.String(255), nullable=False)
    image = db.Column(db.String(255))
    keywords = db.Column(db.String(255))
    priority = db.Column(db.String(20), default="Low")
    status = db.Column(db.String(30), default="Submitted")
    created_date = db.Column(db.DateTime, default=datetime.utcnow)
    updated_date = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    citizen_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    department_id = db.Column(db.Integer, db.ForeignKey("department.id"))
    assigned_staff_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"))
    resolution_note = db.Column(db.Text)
    department = db.relationship("Department", back_populates="complaints")
    assigned_staff = db.relationship("User", back_populates="assigned_complaints", foreign_keys=[assigned_staff_id])
    department_assignments = db.relationship("ComplaintDepartment", back_populates="complaint", cascade="all, delete-orphan")
    assignment_history = db.relationship("ComplaintAssignment", back_populates="complaint", cascade="all, delete-orphan", order_by="ComplaintAssignment.assigned_date")
    status_history = db.relationship("ComplaintStatusHistory", back_populates="complaint", cascade="all, delete-orphan", order_by="ComplaintStatusHistory.timestamp")


class ComplaintAssignment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    complaint_id = db.Column(db.Integer, db.ForeignKey("complaint.id", ondelete="CASCADE"), nullable=False, index=True)
    staff_id = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="RESTRICT"), nullable=False, index=True)
    assigned_by = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="RESTRICT"))
    assigned_date = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    remarks = db.Column(db.Text)

    complaint = db.relationship("Complaint", back_populates="assignment_history")
    staff = db.relationship("User", foreign_keys=[staff_id])
    assigner = db.relationship("User", foreign_keys=[assigned_by])


class ComplaintStatusHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    complaint_id = db.Column(db.Integer, db.ForeignKey("complaint.id", ondelete="CASCADE"), nullable=False, index=True)
    old_status = db.Column(db.String(30))
    new_status = db.Column(db.String(30), nullable=False)
    updated_by = db.Column(db.Integer, db.ForeignKey("user.id", ondelete="SET NULL"))
    timestamp = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    remarks = db.Column(db.Text)

    complaint = db.relationship("Complaint", back_populates="status_history")
    updater = db.relationship("User")


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


KEYWORD_GROUPS = {
    "Road Damage": ["road", "pothole", "crack", "broken road", "footpath", "pavement", "speed breaker"],
    "Garbage": ["garbage", "waste", "trash", "dump", "litter", "rubbish"],
    "Water Leakage": ["water", "leak", "pipe", "leakage", "tap"],
    "Street Light": ["street light", "streetlight", "lamp", "light"],
    "Drainage": ["drain", "drainage", "sewage", "sewer", "manhole", "waterlogging"],
    "Electricity": ["electricity", "electric", "power", "wire", "current", "transformer"],
    "Traffic": ["traffic", "signal", "vehicle", "jam", "congestion"],
    "Fire": ["fire", "smoke", "burning", "flame"],
}

HIGH_PRIORITY_WORDS = [
    "fire", "accident", "danger", "emergency", "injury", "injured",
    "electric shock", "live wire", "collapse", "explosion", "gas leak",
]
MEDIUM_PRIORITY_WORDS = [
    "broken", "leak", "blocked", "damage", "traffic", "pothole", "overflow",
    "difficulty", "unsafe", "large", "sewage", "not working",
]


def contains_word(text, word):
    """Match a keyword at a word start so 'road' does not match 'broad'."""
    return re.search(r"\b" + re.escape(word), text) is not None


def analyze_text(text):
    """Simple predefined-keyword analysis; no AI model is used.

    The category with the most keyword matches wins; ties go to the
    category listed first in KEYWORD_GROUPS.
    """
    text = (text or "").lower()

    found_keywords = []
    best_category = "Other"
    best_score = 0

    for category, words in KEYWORD_GROUPS.items():
        matches = [word for word in words if contains_word(text, word)]
        found_keywords.extend(matches)
        if len(matches) > best_score:
            best_category = category
            best_score = len(matches)

    if any(contains_word(text, word) for word in HIGH_PRIORITY_WORDS):
        priority = "High"
    elif any(contains_word(text, word) for word in MEDIUM_PRIORITY_WORDS):
        priority = "Medium"
    else:
        priority = "Low"

    return list(dict.fromkeys(found_keywords)), best_category, priority


def department_label(category):
    return f"{CATEGORY_DEPARTMENTS.get(category, 'Public Health')} Department"


def login_required():
    return current_user() is not None


def normalize_role(role):
    return "department_head" if role == "officer" else role


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
    if legacy_ids:
        clauses.extend([
            Complaint.department_id.in_(legacy_ids),
            Complaint.department_assignments.any(ComplaintDepartment.department_id.in_(legacy_ids)),
        ])
    categories = [category for category, name in CATEGORY_DEPARTMENTS.items() if name == department.name]
    if categories:
        clauses.append(Complaint.category.in_(categories))
    return Complaint.query.filter(db.or_(*clauses))


def initialize_database():
    """Create missing tables and apply additive, data-preserving schema changes."""
    with app.app_context():
        db.create_all()
        migrate_schema()
        for name in DEPARTMENT_NAMES:
            Department.query.filter_by(name=name).first() or db.session.add(
                Department(name=name, email=f"{name.lower().replace(' ', '')}@spsfrms.local")
            )
        db.session.flush()

        # Keep legacy department rows and multi-department links, but route each
        # existing categorized complaint through its canonical department too.
        for complaint in Complaint.query.filter(Complaint.category.in_(CATEGORY_DEPARTMENTS)).all():
            department_name = CATEGORY_DEPARTMENTS[complaint.category]
            department = Department.query.filter_by(name=department_name).first()
            if department and complaint.department_id != department.id:
                complaint.department_id = department.id
        for complaint in Complaint.query.all():
            if complaint.assigned_staff_id and not ComplaintAssignment.query.filter_by(complaint_id=complaint.id).first():
                db.session.add(ComplaintAssignment(
                    complaint_id=complaint.id,
                    staff_id=complaint.assigned_staff_id,
                    assigned_by=None,
                    assigned_date=complaint.updated_date or complaint.created_date or datetime.utcnow(),
                    remarks="Existing assignment recorded during workflow upgrade.",
                ))
            if not ComplaintStatusHistory.query.filter_by(complaint_id=complaint.id).first():
                db.session.add(ComplaintStatusHistory(
                    complaint_id=complaint.id,
                    old_status=None,
                    new_status=complaint.status or "Submitted",
                    updated_by=complaint.citizen_id,
                    timestamp=complaint.created_date or datetime.utcnow(),
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
    """Create the existing admin demo and optionally seed development staff."""
    with app.app_context():
        if not User.query.filter_by(email="admin@spsfrms.com").first():
            db.session.add(User(
                name="System Administrator",
                email="admin@spsfrms.com",
                phone="9876543210",
                password=generate_password_hash("admin123"),
                role="admin",
            ))

        if os.getenv("SPSFRMS_SEED_DEMO_DATA") == "1":
            seed_path = os.getenv(
                "SPSFRMS_SEED_FILE",
                os.path.join(os.path.dirname(__file__), "dev_seed.example.json"),
            )
            with open(seed_path, encoding="utf-8") as seed_file:
                seed_data = json.load(seed_file)
            for name in seed_data.get("departments", []):
                if not Department.query.filter_by(name=name).first():
                    db.session.add(Department(name=name, email=f"{name.lower().replace(' ', '')}@spsfrms.local"))
            db.session.flush()
            for account in seed_data.get("users", []):
                name = account["name"]
                email = account["email"].lower()
                role = account["role"]
                department_name = account["department"]
                password = account["password"]
                user = User.query.filter_by(email=email).first()
                department = Department.query.filter_by(name=department_name).first()
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
                if user and department and role == "department_head" and department.head_id is None:
                    user.role = role
                    user.department_id = department.id
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
        email = request.form.get("email", "").strip().lower()
        phone = request.form.get("phone", "").strip()
        password = request.form.get("password", "")

        if not name or not email or not phone or not password:
            flash("Please fill all fields.", "danger")
            return render_template("register.html")

        if User.query.filter_by(email=email).first():
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
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        selected_role = normalize_role(request.form.get("role", "citizen"))

        user = User.query.filter_by(email=email).first()

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
    resolved = sum(c.status == "Resolved" for c in complaints)
    in_progress = sum(c.status in ["Under Review", "In Progress"] for c in complaints)
    submitted = sum(c.status == "Submitted" for c in complaints)

    return render_template(
        "citizen_dashboard.html",
        complaints=complaints,
        total=total,
        resolved=resolved,
        in_progress=in_progress,
        submitted=submitted,
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

        keywords, final_category, priority = analyze_text(complaint_text)

        image_name = None
        file = request.files.get("image")
        if file and file.filename:
            if not allowed_file(file.filename):
                flash("Only JPG, JPEG and PNG images are allowed.", "danger")
                return render_template("submit_complaint.html", form=request.form)

            image_name = secure_filename(file.filename)
            name, extension = os.path.splitext(image_name)
            image_name = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{name}{extension}"
            os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
            file.save(os.path.join(app.config["UPLOAD_FOLDER"], image_name))

        department = Department.query.filter_by(name=CATEGORY_DEPARTMENTS.get(final_category, "Public Health")).first()

        complaint = Complaint(
            complaint_text=complaint_text,
            category=final_category,
            location=location,
            image=image_name,
            keywords=", ".join(keywords),
            priority=priority,
            status="Submitted",
            citizen_id=user.id,
            department_id=department.id if department else None,
        )

        db.session.add(complaint)
        db.session.flush()
        db.session.add(ComplaintStatusHistory(
            complaint_id=complaint.id,
            old_status=None,
            new_status="Submitted",
            updated_by=user.id,
            remarks="Complaint submitted and text analysis completed.",
        ))
        db.session.commit()

        flash(
            f"Complaint CMP{complaint.id:03d} submitted. Category: {final_category} · "
            f"Priority: {priority} · Routed to {department_label(final_category)}.",
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
    keywords, category, priority = analyze_text(text)
    return {
        "keywords": keywords,
        "category": category,
        "priority": priority,
        "department": department_label(category),
    }


@app.route("/track-complaint/<int:complaint_id>")
@roles_required("citizen", "department_head", "staff", "admin")
def track_complaint(complaint_id):
    user = current_user()
    complaint = Complaint.query.get_or_404(complaint_id)

    if user.role == "citizen" and complaint.citizen_id != user.id:
        abort(403)
    if user.role == "department_head" and (
        not user.department or not department_complaints_query(user.department).filter_by(id=complaint.id).first()
    ):
        abort(403)
    if user.role == "staff" and complaint.assigned_staff_id != user.id:
        abort(403)

    return render_template("track_complaint.html", complaint=complaint, status_history=complaint.status_history)


@app.route("/officer-dashboard")
@roles_required("department_head")
def officer_dashboard():
    user = current_user()
    complaints = department_complaints_query(user.department).order_by(Complaint.created_date.desc()).all() if user.department else []
    return render_staff_dashboard(
        complaints,
        dashboard_role="department head",
        can_assign=True,
        department=user.department,
        staff_members=User.query.filter_by(role="staff", department_id=user.department_id).order_by(User.name).all() if user.department_id else [],
    )


@app.route("/staff-dashboard")
@roles_required("staff")
def staff_dashboard():
    user = current_user()
    complaints = Complaint.query.filter_by(assigned_staff_id=user.id).order_by(Complaint.created_date.desc()).all()
    return render_staff_dashboard(complaints, dashboard_role="staff", can_assign=False, department=user.department)


def render_staff_dashboard(complaints, dashboard_role, can_assign, department=None, staff_members=None, dashboard_title="Complaint response desk"):
    new_count = sum(c.status == "Submitted" for c in complaints)
    in_progress = sum(c.status in ["Under Review", "In Progress"] for c in complaints)
    resolved = sum(c.status == "Resolved" for c in complaints)
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
        staff_members=staff_members or [],
    )


@app.route("/update-status/<int:complaint_id>", methods=["POST"])
@roles_required("staff", "admin")
def update_status(complaint_id):
    user = current_user()
    complaint = Complaint.query.get_or_404(complaint_id)
    if user.role == "staff" and complaint.assigned_staff_id != user.id:
        abort(403)
    new_status = request.form.get("status")

    if new_status == "In Progress" and complaint.status == "In Progress":
        flash(f"CMP{complaint.id:03d} is already In Progress.", "info")
        return redirect_by_role()

    starting_statuses = {"Submitted", "Assigned"}
    if not (
        new_status == "In Progress" and complaint.status in starting_statuses
        or new_status == "Resolved" and complaint.status == "In Progress"
    ):
        flash("That status change is not available for this complaint.", "warning")
        return redirect_by_role()

    remarks = request.form.get("remarks", "").strip()
    if new_status == "Resolved" and not remarks:
        flash("Add a resolution note before marking this complaint resolved.", "danger")
        return redirect(url_for("track_complaint", complaint_id=complaint.id) if request.form.get("return_to") == "track" else url_for("staff_dashboard"))

    old_status = complaint.status
    complaint.status = new_status
    if new_status == "Resolved":
        complaint.resolution_note = remarks
    db.session.add(ComplaintStatusHistory(
        complaint_id=complaint.id,
        old_status=old_status,
        new_status=new_status,
        updated_by=user.id,
        remarks=remarks or None,
    ))
    db.session.commit()
    flash(f"CMP{complaint.id:03d} status updated to {new_status}.", "success")

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
    return render_staff_dashboard(
        complaints,
        dashboard_role="administrator",
        can_assign=False,
        dashboard_title="All complaints",
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
        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        phone = request.form.get("phone", "").strip()
        password = request.form.get("password", "")
        role = request.form.get("role", "")
        department = db.session.get(Department, request.form.get("department_id", type=int))
        if not name or not email or not phone or len(password) < 8 or role not in {"department_head", "staff"} or not department:
            flash("Enter valid account details, a department, and a password of at least 8 characters.", "danger")
        elif User.query.filter_by(email=email).first():
            flash("That email is already registered.", "warning")
        elif role == "department_head" and department.head_id:
            flash("This department already has a department head.", "warning")
        else:
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

    if action == "assign_user":
        user = db.session.get(User, request.form.get("user_id", type=int))
        role = request.form.get("role", "")
        department = db.session.get(Department, request.form.get("department_id", type=int))
        if not user or role not in ROLES or (role in {"department_head", "staff"} and not department):
            flash("Select a valid account, role, and department.", "danger")
        elif role == "department_head" and department.head_id not in (None, user.id):
            flash("This department already has a department head.", "warning")
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

    abort(400)


@app.route("/assign-complaint/<int:complaint_id>", methods=["POST"])
@roles_required("department_head")
def assign_complaint(complaint_id):
    head = current_user()
    complaint = Complaint.query.get_or_404(complaint_id)
    if not head.department or not department_complaints_query(head.department).filter_by(id=complaint.id).first():
        abort(403)
    if complaint.status == "Resolved":
        abort(400)

    staff = db.session.get(User, request.form.get("staff_id", type=int))
    if not staff or staff.role != "staff" or staff.department_id != head.department_id:
        abort(403)

    previous_status = complaint.status
    complaint.assigned_staff_id = staff.id
    complaint.status = "Assigned"
    complaint.resolution_note = None
    db.session.add(ComplaintAssignment(
        complaint_id=complaint.id,
        staff_id=staff.id,
        assigned_by=head.id,
        remarks=request.form.get("remarks", "").strip() or None,
    ))
    if previous_status != "Assigned":
        db.session.add(ComplaintStatusHistory(
            complaint_id=complaint.id,
            old_status=previous_status,
            new_status="Assigned",
            updated_by=head.id,
            remarks=request.form.get("remarks", "").strip() or f"Assigned to {staff.name}.",
        ))
    db.session.commit()
    flash(f"CMP{complaint.id:03d} assigned to {staff.name}.", "success")
    return redirect(url_for("officer_dashboard"))


@app.route("/health")
def health():
    try:
        db.session.execute(db.text("SELECT 1"))
        return {"status": "ok", "database": "connected"}, 200
    except Exception as error:
        return {"status": "error", "database": "not connected", "message": str(error)}, 500


with app.app_context():
    try:
        initialize_database()
        create_demo_users()
    except Exception as startup_error:
        print("Database setup skipped:", startup_error)


if __name__ == "__main__":
    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    app.run(debug=True)
