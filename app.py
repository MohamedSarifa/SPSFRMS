from flask import Flask, render_template, request, redirect, url_for, session, flash
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from datetime import datetime
from dotenv import load_dotenv
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
STATUSES = ["Submitted", "Under Review", "In Progress", "Resolved"]


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    phone = db.Column(db.String(20), nullable=False)
    password = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), default="citizen")
    registered_date = db.Column(db.DateTime, default=datetime.utcnow)

    complaints = db.relationship("Complaint", backref="citizen", lazy=True)


class Department(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    email = db.Column(db.String(120))


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
    department = db.relationship("Department")


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
    return f"{category} Department"


def login_required():
    return "user_id" in session


def redirect_by_role():
    role = session.get("role")
    if role == "citizen":
        return redirect(url_for("citizen_dashboard"))
    if role == "officer":
        return redirect(url_for("officer_dashboard"))
    if role == "admin":
        return redirect(url_for("admin_dashboard"))
    return redirect(url_for("home"))


def initialize_database():
    """Create tables and basic departments automatically on first start."""
    with app.app_context():
        db.create_all()

        department_names = [
            "Road Damage", "Garbage", "Water Leakage", "Street Light",
            "Drainage", "Electricity", "Traffic", "Fire", "Other"
        ]

        for name in department_names:
            if not Department.query.filter_by(name=name).first():
                db.session.add(
                    Department(
                        name=name,
                        email=f"{name.lower().replace(' ', '')}@spsfrms.local"
                    )
                )
        db.session.commit()


def create_demo_users():
    """Create simple demo accounts if they do not already exist."""
    with app.app_context():
        demo_users = [
            ("System Administrator", "admin@spsfrms.com", "9876543210", "admin123", "admin"),
            ("Safety Officer", "officer@spsfrms.com", "9876543211", "officer123", "officer"),
        ]
        changed = False
        for name, email, phone, password, role in demo_users:
            if not User.query.filter_by(email=email).first():
                db.session.add(User(
                    name=name,
                    email=email,
                    phone=phone,
                    password=generate_password_hash(password),
                    role=role,
                ))
                changed = True
        if changed:
            db.session.commit()


@app.route("/")
def home():
    demo_text = (
        "There is a large pothole near the college entrance and "
        "vehicles are having difficulty passing."
    )
    keywords, category, priority = analyze_text(demo_text)
    demo = {
        "text": demo_text,
        "keywords": keywords,
        "category": category,
        "priority": priority,
        "department": department_label(category),
    }
    return render_template("index.html", demo=demo, categories=CATEGORIES)


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
        selected_role = request.form.get("role", "citizen")

        user = User.query.filter_by(email=email).first()

        if user and check_password_hash(user.password, password):
            if user.role != selected_role:
                flash("Selected role does not match this account.", "danger")
                return render_template("login.html")

            session["user_id"] = user.id
            session["user_name"] = user.name
            session["role"] = user.role
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
def citizen_dashboard():
    if not login_required() or session.get("role") != "citizen":
        return redirect(url_for("login"))

    complaints = Complaint.query.filter_by(
        citizen_id=session["user_id"]
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
def submit_complaint():
    if not login_required() or session.get("role") != "citizen":
        return redirect(url_for("login"))

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

        department = Department.query.filter_by(name=final_category).first()

        complaint = Complaint(
            complaint_text=complaint_text,
            category=final_category,
            location=location,
            image=image_name,
            keywords=", ".join(keywords),
            priority=priority,
            status="Submitted",
            citizen_id=session["user_id"],
            department_id=department.id if department else None,
        )

        db.session.add(complaint)
        db.session.commit()

        flash(
            f"Complaint CMP{complaint.id:03d} submitted. Category: {final_category} · "
            f"Priority: {priority} · Routed to {department_label(final_category)}.",
            "success",
        )
        return redirect(url_for("citizen_dashboard"))

    return render_template("submit_complaint.html", form={})


@app.route("/analyze-preview", methods=["POST"])
def analyze_preview():
    """Live preview of the same keyword analysis used on submission."""
    if not login_required() or session.get("role") != "citizen":
        return {"error": "unauthorized"}, 401

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
def track_complaint(complaint_id):
    if not login_required():
        return redirect(url_for("login"))

    complaint = Complaint.query.get_or_404(complaint_id)

    if session.get("role") == "citizen" and complaint.citizen_id != session["user_id"]:
        flash("You cannot view this complaint.", "danger")
        return redirect(url_for("citizen_dashboard"))

    return render_template("track_complaint.html", complaint=complaint)


@app.route("/officer-dashboard")
def officer_dashboard():
    if not login_required() or session.get("role") != "officer":
        return redirect(url_for("login"))

    complaints = Complaint.query.order_by(Complaint.created_date.desc()).all()
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
    )


@app.route("/update-status/<int:complaint_id>", methods=["POST"])
def update_status(complaint_id):
    if not login_required() or session.get("role") not in ["officer", "admin"]:
        return redirect(url_for("login"))

    complaint = Complaint.query.get_or_404(complaint_id)
    new_status = request.form.get("status")

    if new_status in STATUSES:
        complaint.status = new_status
        db.session.commit()
        flash(f"CMP{complaint.id:03d} status updated to {new_status}.", "success")

    return redirect_by_role()


@app.route("/admin-dashboard")
def admin_dashboard():
    if not login_required() or session.get("role") != "admin":
        return redirect(url_for("login"))

    users = User.query.count()
    officers = User.query.filter_by(role="officer").count()
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
    )


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
