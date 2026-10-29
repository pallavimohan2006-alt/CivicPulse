import os
import hmac
import math
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from functools import wraps

import imagehash
from PIL import Image, UnidentifiedImageError
from flask import (
    Flask, jsonify, render_template, request, send_from_directory,
    abort, session, redirect, url_for
)
from werkzeug.utils import secure_filename


# ============================================================
# APPLICATION CONFIGURATION
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"
UPLOAD_FOLDER = BASE_DIR / "uploads"
DATABASE = INSTANCE_DIR / "civicpulse.db"

INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
UPLOAD_FOLDER.mkdir(parents=True, exist_ok=True)

app = Flask(__name__)

app.config.update(
    UPLOAD_FOLDER=str(UPLOAD_FOLDER),
    MAX_CONTENT_LENGTH=5 * 1024 * 1024,
    SECRET_KEY=os.environ.get("CIVICPULSE_SECRET_KEY", ""),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
)

ADMIN_USERNAME = os.environ.get("CIVICPULSE_ADMIN_USERNAME", "")
ADMIN_PASSWORD = os.environ.get("CIVICPULSE_ADMIN_PASSWORD", "")

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "webp"}
HASH_DISTANCE_LIMIT = 8
DUPLICATE_LOCATION_METERS = 100

STATUS_OPTIONS = {
    "all": "All Complaints",
    "pending": "Pending",
    "in-progress": "In Progress",
    "solved": "Solved",
}


# ============================================================
# DATABASE AND MIGRATIONS
# ============================================================

def get_db():
    conn = sqlite3.connect(str(DATABASE))
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS issues (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT,
                category TEXT NOT NULL DEFAULT 'Other',
                area TEXT DEFAULT '',
                address TEXT DEFAULT '',
                severity INTEGER DEFAULT 3,
                safety INTEGER DEFAULT 3,
                impact INTEGER DEFAULT 3,
                priority INTEGER DEFAULT 0,
                status TEXT DEFAULT 'Pending',
                photo TEXT DEFAULT '',
                image_hash TEXT DEFAULT '',
                duplicate_of INTEGER,
                reported_at TEXT,
                latitude REAL,
                longitude REAL
            )
        """)

        columns = {
            row["name"]
            for row in conn.execute(
                "PRAGMA table_info(issues)"
            ).fetchall()
        }

        additions = {
            "title": "TEXT",
            "category": "TEXT DEFAULT 'Other'",
            "area": "TEXT DEFAULT ''",
            "address": "TEXT DEFAULT ''",
            "severity": "INTEGER DEFAULT 3",
            "safety": "INTEGER DEFAULT 3",
            "impact": "INTEGER DEFAULT 3",
            "priority": "INTEGER DEFAULT 0",
            "status": "TEXT DEFAULT 'Pending'",
            "photo": "TEXT DEFAULT ''",
            "image_hash": "TEXT DEFAULT ''",
            "duplicate_of": "INTEGER",
            "reported_at": "TEXT",
            "latitude": "REAL",
            "longitude": "REAL",
        }

        for name, definition in additions.items():
            if name not in columns:
                conn.execute(
                    f"ALTER TABLE issues ADD COLUMN {name} {definition}"
                )

        conn.execute("""
            UPDATE issues SET category = 'Other'
            WHERE category IS NULL OR TRIM(category) = ''
        """)
        conn.execute("""
            UPDATE issues SET status = 'Pending'
            WHERE status IS NULL OR TRIM(status) = ''
        """)
        conn.execute("""
            UPDATE issues SET address = ''
            WHERE address IS NULL
        """)
        conn.execute("""
            UPDATE issues SET photo = ''
            WHERE photo IS NULL
        """)


# ============================================================
# ADMIN AUTHENTICATION
# ============================================================

def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("is_admin"):
            if request.path.startswith("/api/"):
                return jsonify({
                    "success": False,
                    "error": "Admin login required."
                }), 401

            return redirect(url_for("admin_login", next=request.path))

        return view(*args, **kwargs)

    return wrapped


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if (
        not app.config["SECRET_KEY"]
        or not ADMIN_USERNAME
        or not ADMIN_PASSWORD
    ):
        app.logger.error("Admin login credentials are not configured.")
        return (
            "Admin login is not configured. Set "
            "CIVICPULSE_SECRET_KEY, CIVICPULSE_ADMIN_USERNAME "
            "and CIVICPULSE_ADMIN_PASSWORD, then restart Flask.",
            503,
        )

    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")

        if (
            hmac.compare_digest(username, ADMIN_USERNAME)
            and hmac.compare_digest(password, ADMIN_PASSWORD)
        ):
            session.clear()
            session["is_admin"] = True

            target = request.args.get("next", "")
            if target.startswith("/") and not target.startswith("//"):
                return redirect(target)

            return redirect(url_for("dashboard"))

        return render_template(
            "admin_login.html",
            error="Invalid username or password."
        ), 401

    return render_template("admin_login.html", error=None)


@app.route("/admin/logout", methods=["POST"])
@admin_required
def admin_logout():
    session.clear()
    return redirect(url_for("home"))


# ============================================================
# PRIORITY CALCULATION
# ============================================================

def get_rating(value, field_name):
    try:
        rating = int(value)
    except (TypeError, ValueError):
        raise ValueError(
            f"{field_name} must be a number from 1 to 5."
        )

    if not 1 <= rating <= 5:
        raise ValueError(
            f"{field_name} must be between 1 and 5."
        )

    return rating


def calculate_priority(safety, impact, severity):
    score = (
        (int(safety) / 5) * 40
        + (int(impact) / 5) * 30
        + (int(severity) / 5) * 30
    )
    return int(round(max(0, min(100, score))))


# ============================================================
# IMAGE AND LOCATION DUPLICATE DETECTION
# ============================================================

def allowed_file(filename):
    return (
        "." in filename
        and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS
    )


def calculate_image_hash(file_path):
    with Image.open(file_path) as image:
        return str(imagehash.phash(image.convert("RGB")))


def parse_coordinate(value, field_name, minimum, maximum):
    if value is None or str(value).strip() == "":
        return None

    try:
        coordinate = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{field_name} must be a valid number.")

    if (
        not math.isfinite(coordinate)
        or not minimum <= coordinate <= maximum
    ):
        raise ValueError(f"{field_name} is outside the valid range.")

    return coordinate


def distance_in_meters(lat1, lon1, lat2, lon2):
    """Calculate the distance between two coordinates in metres."""
    earth_radius = 6_371_000

    phi1 = math.radians(lat1)
    phi2 = math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2)
        * math.sin(delta_lambda / 2) ** 2
    )

    a = max(0.0, min(1.0, a))
    return earth_radius * 2 * math.atan2(
        math.sqrt(a), math.sqrt(1 - a)
    )


def find_duplicate(image_hash, category, latitude, longitude):
    """
    Find a likely duplicate using image similarity and location.

    Rules:
    - Complaint categories must match.
    - Uploaded images must be visually similar.
    - When both reports have coordinates, they must be within
      DUPLICATE_LOCATION_METERS of each other.
    - If coordinates are missing, a similar image in the same
      category can still be flagged.
    - Location alone never rejects a complaint.
    """
    if not image_hash:
        return None, None, None

    try:
        new_hash = imagehash.hex_to_hash(image_hash)
    except (ValueError, TypeError):
        return None, None, None

    with get_db() as conn:
        rows = conn.execute("""
            SELECT id, category, image_hash, latitude, longitude, address
            FROM issues
            WHERE image_hash IS NOT NULL
              AND image_hash != ''
        """).fetchall()

    best_match = None
    best_hash_distance = None
    best_location_distance = None
    normalized_category = (category or "").strip().casefold()

    for row in rows:
        old_category = (row["category"] or "").strip().casefold()

        if old_category != normalized_category:
            continue

        try:
            old_hash = imagehash.hex_to_hash(row["image_hash"])
            hash_distance = int(new_hash - old_hash)
        except (ValueError, TypeError):
            continue

        if hash_distance > HASH_DISTANCE_LIMIT:
            continue

        old_lat = row["latitude"]
        old_lon = row["longitude"]
        location_distance = None

        if (
            latitude is not None
            and longitude is not None
            and old_lat is not None
            and old_lon is not None
        ):
            location_distance = distance_in_meters(
                latitude,
                longitude,
                float(old_lat),
                float(old_lon),
            )

            # Same-looking issue far away: don't reject it.
            if location_distance > DUPLICATE_LOCATION_METERS:
                continue

        if (
            best_hash_distance is None
            or hash_distance < best_hash_distance
        ):
            best_match = row
            best_hash_distance = hash_distance
            best_location_distance = location_distance

    if best_match is not None:
        return (
            int(best_match["id"]),
            best_hash_distance,
            best_location_distance,
        )

    return None, None, None


# ============================================================
# CONVERT DATABASE ROWS
# ============================================================

def issue_to_dict(row):
    item = dict(row)
    item["issue_id"] = f"CP-{int(item['id']):04d}"

    duplicate_of = item.get("duplicate_of")
    item["duplicate_issue_id"] = (
        f"CP-{int(duplicate_of):04d}"
        if duplicate_of is not None else None
    )

    for field, default in (
        ("priority", 0),
        ("severity", 3),
        ("safety", 3),
        ("impact", 3),
    ):
        item[field] = int(item.get(field) or default)

    return item


# ============================================================
# WEBSITE PAGES
# ============================================================

@app.route("/")
def home():
    return render_template("index.html")


@app.route("/report")
def report():
    return render_template("report.html")


@app.route("/dashboard")
def dashboard():
    try:
        with get_db() as conn:
            rows = conn.execute("""
                SELECT
                    LOWER(TRIM(COALESCE(status, 'pending')))
                    AS normalized_status,
                    COUNT(*) AS total
                FROM issues
                GROUP BY normalized_status
            """).fetchall()

            total = conn.execute(
                "SELECT COUNT(*) FROM issues"
            ).fetchone()[0]

        counts = {
            "total": int(total or 0),
            "pending": 0,
            "in_progress": 0,
            "solved": 0,
        }

        for row in rows:
            status = row["normalized_status"]
            amount = int(row["total"] or 0)

            if status == "pending":
                counts["pending"] += amount
            elif status in ("in progress", "in-progress", "inprogress"):
                counts["in_progress"] += amount
            elif status in ("solved", "resolved", "completed"):
                counts["solved"] += amount

        return render_template(
            "dashboard.html",
            counts=counts,
            is_admin=bool(session.get("is_admin")),
        )

    except sqlite3.Error:
        app.logger.exception("Dashboard database query failed")
        return (
            "Unable to load dashboard data. Check the VS Code terminal.",
            500,
        )


@app.route("/complaints")
def all_complaints():
    return render_template(
        "complaints.html",
        page_title="All Complaints",
        selected_status="all",
        status_slug="all",
        is_admin=bool(session.get("is_admin")),
    )


@app.route("/complaints/<status_slug>")
def complaints_by_status(status_slug):
    if status_slug not in STATUS_OPTIONS:
        abort(404)

    return render_template(
        "complaints.html",
        page_title=STATUS_OPTIONS[status_slug],
        selected_status=status_slug,
        status_slug=status_slug,
        is_admin=bool(session.get("is_admin")),
    )


# ============================================================
# HEALTH CHECK AND PHOTOS
# ============================================================

@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/uploads/<path:filename>")
def uploaded_file(filename):
    return send_from_directory(
        app.config["UPLOAD_FOLDER"],
        filename,
    )


# ============================================================
# FETCH COMPLAINTS
# ============================================================

@app.route("/api/issues", methods=["GET"])
def get_issues():
    with get_db() as conn:
        rows = conn.execute("""
            SELECT * FROM issues
            ORDER BY priority DESC, id DESC
        """).fetchall()

    return jsonify([issue_to_dict(row) for row in rows])


@app.route("/api/issues/status/<status_slug>", methods=["GET"])
def get_issues_by_status(status_slug):
    if status_slug not in STATUS_OPTIONS:
        return jsonify({"error": "Unknown complaint status."}), 400

    queries = {
        "pending": """
            SELECT * FROM issues
            WHERE LOWER(TRIM(status)) = 'pending'
            ORDER BY priority DESC, id DESC
        """,
        "in-progress": """
            SELECT * FROM issues
            WHERE LOWER(TRIM(status)) IN
            ('in progress', 'in-progress', 'inprogress')
            ORDER BY priority DESC, id DESC
        """,
        "solved": """
            SELECT * FROM issues
            WHERE LOWER(TRIM(status)) IN
            ('solved', 'resolved', 'completed')
            ORDER BY priority DESC, id DESC
        """,
    }

    with get_db() as conn:
        if status_slug == "all":
            rows = conn.execute("""
                SELECT * FROM issues
                ORDER BY priority DESC, id DESC
            """).fetchall()
        else:
            rows = conn.execute(queries[status_slug]).fetchall()

    return jsonify([issue_to_dict(row) for row in rows])


# ============================================================
# UPDATE COMPLAINT STATUS - ADMIN ONLY
# ============================================================

@app.route("/api/issues/<int:issue_id>/status", methods=["PATCH"])
@admin_required
def update_issue_status(issue_id):
    data = request.get_json(silent=True) or {}
    requested_status = str(data.get("status", "")).strip().lower()

    allowed_statuses = {
        "pending": "Pending",
        "in-progress": "In Progress",
        "solved": "Solved",
    }

    if requested_status not in allowed_statuses:
        return jsonify({
            "success": False,
            "error": "Choose Pending, In Progress, or Solved.",
        }), 400

    new_status = allowed_statuses[requested_status]

    with get_db() as conn:
        existing = conn.execute(
            "SELECT id FROM issues WHERE id = ?",
            (issue_id,),
        ).fetchone()

        if existing is None:
            return jsonify({
                "success": False,
                "error": "Complaint not found.",
            }), 404

        conn.execute(
            "UPDATE issues SET status = ? WHERE id = ?",
            (new_status, issue_id),
        )

        row = conn.execute(
            "SELECT * FROM issues WHERE id = ?",
            (issue_id,),
        ).fetchone()

    return jsonify({
        "success": True,
        "message": "Complaint status updated successfully.",
        "issue": issue_to_dict(row),
    })


# ============================================================
# SUBMIT A COMPLAINT
# ============================================================

@app.route("/api/issues", methods=["POST"])
def create_issue():
    photo_path = None
    saved_filename = ""

    try:
        category = (request.form.get("category") or "").strip()
        address = (request.form.get("address") or "").strip()
        area = (request.form.get("area") or address).strip()
        title = (request.form.get("title") or category).strip()

        if not category:
            return jsonify({
                "error": "Please select a complaint category."
            }), 400

        if not address:
            return jsonify({
                "error": "Please enter the complaint address."
            }), 400

        try:
            latitude = parse_coordinate(
                request.form.get("latitude"),
                "Latitude",
                -90,
                90,
            )
            longitude = parse_coordinate(
                request.form.get("longitude"),
                "Longitude",
                -180,
                180,
            )
        except ValueError as error:
            return jsonify({"error": str(error)}), 400

        if (latitude is None) != (longitude is None):
            return jsonify({
                "error": (
                    "Please select the location on the map again "
                    "so both coordinates are available."
                )
            }), 400

        try:
            severity = get_rating(
                request.form.get("severity", "3"), "Severity"
            )
            safety = get_rating(
                request.form.get("safety", "3"), "Safety"
            )
            impact = get_rating(
                request.form.get("impact", "3"), "Impact"
            )
        except ValueError as error:
            return jsonify({"error": str(error)}), 400

        priority = calculate_priority(safety, impact, severity)
        reported_at = datetime.now(timezone.utc).isoformat()

        image_hash = ""
        duplicate_id = None
        duplicate_distance = None
        location_distance = None

        uploaded_photo = request.files.get("photo")

        if uploaded_photo and uploaded_photo.filename:
            if not allowed_file(uploaded_photo.filename):
                return jsonify({
                    "error": "Upload a PNG, JPG, JPEG, or WEBP image."
                }), 400

            safe_name = secure_filename(uploaded_photo.filename)

            if not safe_name or "." not in safe_name:
                return jsonify({
                    "error": "Invalid photo filename."
                }), 400

            extension = safe_name.rsplit(".", 1)[1].lower()
            saved_filename = f"{uuid.uuid4().hex}.{extension}"
            photo_path = UPLOAD_FOLDER / saved_filename
            uploaded_photo.save(str(photo_path))

            try:
                image_hash = calculate_image_hash(photo_path)
            except (
                UnidentifiedImageError,
                OSError,
                ValueError,
            ):
                photo_path.unlink(missing_ok=True)
                return jsonify({
                    "error": "The uploaded file is not a valid image."
                }), 400

        # IMPORTANT: Check for duplicates BEFORE inserting into SQLite.
        if image_hash:
            duplicate_id, duplicate_distance, location_distance = (
                find_duplicate(
                    image_hash,
                    category,
                    latitude,
                    longitude,
                )
            )

        if duplicate_id is not None:
            # Delete the uploaded image because this report is rejected.
            if photo_path and photo_path.exists():
                photo_path.unlink(missing_ok=True)

            with get_db() as conn:
                existing = conn.execute("""
                    SELECT id, address, category, status
                    FROM issues
                    WHERE id = ?
                """, (duplicate_id,)).fetchone()

            return jsonify({
                "duplicate": True,
                "is_duplicate": True,
                "duplicate_issue_id": f"CP-{duplicate_id:04d}",
                "existing_issue_id": f"CP-{duplicate_id:04d}",
                "existing_address": (
                    existing["address"] if existing else ""
                ),
                "category": category,
                "message": (
                    "A similar civic issue has already been reported. "
                    "Your complaint was not saved."
                ),
                "image_distance": duplicate_distance,
                "location_distance_meters": (
                    round(location_distance, 1)
                    if location_distance is not None else None
                ),
            }), 409

        # Save only when the duplicate check passes.
        with get_db() as conn:
            cursor = conn.execute("""
                INSERT INTO issues (
                    title, category, area, address,
                    severity, safety, impact, priority,
                    status, photo, image_hash, duplicate_of,
                    reported_at, latitude, longitude
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                title,
                category,
                area,
                address,
                severity,
                safety,
                impact,
                priority,
                "Pending",
                saved_filename,
                image_hash,
                None,
                reported_at,
                latitude,
                longitude,
            ))

            new_id = int(cursor.lastrowid)

            row = conn.execute(
                "SELECT * FROM issues WHERE id = ?",
                (new_id,),
            ).fetchone()

            result = issue_to_dict(row)

        result["possible_duplicate"] = False
        result["duplicate_distance"] = None
        result["location_distance_meters"] = None

        return jsonify(result), 201

    except Exception:
        app.logger.exception("Failed to create complaint")

        if photo_path and photo_path.exists():
            try:
                photo_path.unlink()
            except OSError:
                app.logger.warning("Could not remove failed upload.")

        return jsonify({
            "error": "Unable to save the complaint. Please try again."
        }), 500


# ============================================================
# ERROR HANDLERS
# ============================================================

@app.errorhandler(404)
def not_found(error):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Not found."}), 404
    return "Page not found.", 404


@app.errorhandler(413)
def file_too_large(error):
    message = "File is too large. Maximum upload size is 5 MB."

    if request.path.startswith("/api/"):
        return jsonify({"error": message}), 413

    return message, 413


# ============================================================
# START APPLICATION
# ============================================================

init_db()

if __name__ == "__main__":
    app.run(debug=True)
 