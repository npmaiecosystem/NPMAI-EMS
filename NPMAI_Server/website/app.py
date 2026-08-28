"""
NPMAI-PEMS website backend. Serves the marketing page and a dashboard
backed by REAL data.

IMPORTANT — this connects to the SAME Postgres database Layers 2-4
already write to (devices, joint_risk_score - see BuildPrompts.md and
migrations/001-005). This is not a separate app with its own parallel
data model: the `users` table added here is the ONLY new table.
Everything the dashboard shows about a building's electrical health
comes from the exact tables the physics (Layer 3) and feature/risk
(Layer 4) services populate. If those services aren't running yet for a
given building, the dashboard correctly shows "no data yet" - it does
not fabricate readings to fill the screen.

Floor number is NOT a column anywhere in the real schema - panel_id is
a free-text identifier (see Layer 1's config.h: "panel-2F-main",
"panel-GF-incomer"). We parse the floor out of that naming convention
here (see parse_floor_from_panel_id) rather than inventing a new column
that the rest of the system doesn't have - if your site's panel_id
naming differs, adjust that one function only.
"""
import os
import re
from datetime import datetime
from functools import wraps

import psycopg2
import psycopg2.extras
from flask import Flask, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "dev-secret-change-this-in-production")

# Same DATABASE_URL convention as Layer 2/3/4's app/config.py - point
# this at the real deployment's Postgres/TimescaleDB instance.
DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/npmai")


def get_db():
    if "db" not in g:
        g.db = psycopg2.connect(DATABASE_URL, cursor_factory=psycopg2.extras.RealDictCursor)
    return g.db


@app.teardown_appcontext
def close_db(exception=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def parse_floor_from_panel_id(panel_id: str) -> int:
    """
    "panel-2F-main" -> 2, "panel-GF-incomer" -> 0 (ground floor, sorts
    below floor 1). Returns -1 for any panel_id that doesn't match the
    Layer 1 naming convention, rather than crashing the dashboard.
    """
    m = re.search(r"panel-(\d+)F", panel_id, re.IGNORECASE)
    if m:
        return int(m.group(1))
    if re.search(r"panel-GF", panel_id, re.IGNORECASE):
        return 0
    return -1


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login_page"))
        return view(*args, **kwargs)
    return wrapped


@app.route("/")
def home():
    return render_template("index.html", logged_in=("user_id" in session))


@app.route("/login")
def login_page():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return render_template("auth.html", mode="login")


@app.route("/register")
def register_page():
    if "user_id" in session:
        return redirect(url_for("dashboard"))
    return render_template("auth.html", mode="register")


@app.route("/api/register", methods=["POST"])
def api_register():
    import random
    data = request.get_json(force=True)
    name = (data.get("name") or "").strip()
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""
    building_name = (data.get("building_name") or "My Building").strip()

    if not name or not email or len(password) < 6:
        return jsonify({"ok": False, "error": "Name, a valid email, and a password of at least 6 characters are required."}), 400

    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT id FROM users WHERE email = %s", (email,))
    if cur.fetchone():
        return jsonify({"ok": False, "error": "An account with that email already exists. Try logging in instead."}), 409

    building_id = f"bldg-{email.split('@')[0]}-{random.randint(1000,9999)}"
    password_hash = generate_password_hash(password)
    cur.execute(
        "INSERT INTO users (name, email, password_hash, building_id, building_name) VALUES (%s, %s, %s, %s, %s) RETURNING id",
        (name, email, password_hash, building_id, building_name),
    )
    user_id = cur.fetchone()["id"]
    db.commit()

    session["user_id"] = user_id
    session["building_id"] = building_id
    session["name"] = name
    return jsonify({"ok": True, "redirect": url_for("dashboard")})


@app.route("/api/login", methods=["POST"])
def api_login():
    data = request.get_json(force=True)
    email = (data.get("email") or "").strip().lower()
    password = data.get("password") or ""

    db = get_db()
    cur = db.cursor()
    cur.execute("SELECT * FROM users WHERE email = %s", (email,))
    user = cur.fetchone()
    if user is None or not check_password_hash(user["password_hash"], password):
        return jsonify({"ok": False, "error": "Incorrect email or password."}), 401

    session["user_id"] = user["id"]
    session["building_id"] = user["building_id"]
    session["name"] = user["name"]
    return jsonify({"ok": True, "redirect": url_for("dashboard")})


@app.route("/api/logout", methods=["POST"])
def api_logout():
    session.clear()
    return jsonify({"ok": True, "redirect": url_for("home")})


@app.route("/dashboard")
@login_required
def dashboard():
    return render_template("dashboard.html", name=session.get("name"))


@app.route("/api/dashboard-data")
@login_required
def api_dashboard_data():
    db = get_db()
    building_id = session["building_id"]
    cur = db.cursor()

    cur.execute("SELECT building_name FROM users WHERE building_id = %s LIMIT 1", (building_id,))
    user_row = cur.fetchone()
    building_name = user_row["building_name"] if user_row else "My Building"

    cur.execute(
        """
        SELECT DISTINCT ON (rs.panel_id, rs.breaker_id)
            rs.panel_id, rs.breaker_id, rs.severity_band, rs.cusum_value,
            rs.dr_dt_ohms_per_day, rs.projected_days_to_critical, rs.status, rs.ts
        FROM joint_risk_score rs
        JOIN devices d ON d.panel_id = rs.panel_id
        WHERE d.building_id = %s
        ORDER BY rs.panel_id, rs.breaker_id, rs.ts DESC
        """,
        (building_id,),
    )
    joints = cur.fetchall()

    if not joints:
        return jsonify({
            "ok": True,
            "building": {"name": building_name, "building_id": building_id, "has_data": False},
            "summary": {"total_joints": 0, "flagged_count": 0},
            "floors": [],
        })

    floors = {}
    for j in joints:
        floor_num = parse_floor_from_panel_id(j["panel_id"])
        floors.setdefault(floor_num, []).append({
            "panel_id": j["panel_id"], "breaker_id": j["breaker_id"],
            "severity": j["severity_band"], "cusum": j["cusum_value"],
            "dr_dt_ohms_per_day": j["dr_dt_ohms_per_day"],
            "projected_days_to_critical": j["projected_days_to_critical"],
            "ts": j["ts"].isoformat() if j["ts"] else None,
        })

    severity_rank = {"healthy": 0, "baselining": 0, "minor": 1, "intermediate": 2, "serious": 3, "emergency": 4}
    floor_list = []
    for floor_num in sorted(floors.keys(), reverse=True):
        joints_on_floor = floors[floor_num]
        worst = max(joints_on_floor, key=lambda x: severity_rank.get(x["severity"], 0))
        floor_list.append({"floor": floor_num, "worst_severity": worst["severity"], "joints": joints_on_floor})

    flagged = [j for j in joints if j["severity_band"] not in ("healthy", "baselining")]

    return jsonify({
        "ok": True,
        "building": {"name": building_name, "building_id": building_id, "has_data": True},
        "summary": {"total_joints": len(joints), "flagged_count": len(flagged)},
        "floors": floor_list,
    })


@app.route("/api/seed-demo", methods=["POST"])
@login_required
def api_seed_demo():
    import random
    building_id = session["building_id"]
    db = get_db()
    cur = db.cursor()

    cur.execute("SELECT 1 FROM devices WHERE building_id = %s LIMIT 1", (building_id,))
    if cur.fetchone():
        return jsonify({"ok": False, "error": "This building already has device data - demo seeding is only for brand-new accounts."}), 409

    now = datetime.utcnow()
    for floor in range(1, 5):
        panel_id = f"panel-{floor}F-main"
        device_id = f"esp32-demo-{floor}F"
        cur.execute(
            """INSERT INTO devices (device_id, building_id, panel_id, api_key_hash, mqtt_username, is_active)
               VALUES (%s, %s, %s, 'demo', %s, TRUE) ON CONFLICT (device_id) DO NOTHING""",
            (device_id, building_id, panel_id, device_id),
        )
        for b in range(1, 9):
            breaker_id = f"brk-{b:02d}"
            is_flagged = (floor == 2 and b == 4)
            severity = "serious" if is_flagged else "healthy"
            cusum = 842.6 if is_flagged else round(random.uniform(0, 3), 2)
            drift = 0.0000091 if is_flagged else 0.0
            days = 6.3 if is_flagged else None
            cur.execute(
                """INSERT INTO joint_risk_score (ts, panel_id, breaker_id, cusum_value, dr_dt_ohms_per_day,
                   projected_days_to_critical, severity_band, status)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, 'scored')""",
                (now, panel_id, breaker_id, cusum, drift, days, severity),
            )
    db.commit()
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(debug=True, port=5050)
