from __future__ import annotations

import csv
import io
import os
import sqlite3
import math
import re
from functools import wraps
from werkzeug.security import check_password_hash, generate_password_hash
from flask import session
from datetime import date, datetime
from pathlib import Path

from flask import Flask, abort, make_response, redirect, render_template_string, request, send_file, url_for
from contextlib import contextmanager

app = Flask(__name__)
app.secret_key = os.environ.get("GRIDGUARD_SECRET_KEY", "gridguard-local-dev-key-change-before-deployment")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
PROJECT_DIR = Path(__file__).resolve().parent
app.config.setdefault("DATABASE", os.environ.get("GRIDGUARD_DATABASE", str(PROJECT_DIR / "gridguard.db")))

DEFAULT_TARIFFS = [
    {"slab_units": 100, "rate": 3.0},
    {"slab_units": 100, "rate": 5.0},
    {"slab_units": 100, "rate": 7.0},
    {"slab_units": None, "rate": 8.0},
]
# Backwards-compatible constant for existing tests and project code.
TARIFF_SLABS = [(100, 3), (100, 5), (100, 7), (float("inf"), 8)]


@contextmanager
def get_db():
    """Open a database connection and always close it."""
    db_path = Path(app.config["DATABASE"])
    db_path.parent.mkdir(parents=True, exist_ok=True)

    connection = sqlite3.connect(str(db_path), timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")

    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def init_db():
    with get_db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS consumers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            consumer_id TEXT UNIQUE,
            name TEXT NOT NULL,
            address TEXT NOT NULL DEFAULT '',
            phone TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS bills (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bill_number TEXT UNIQUE,
            consumer_pk INTEGER NOT NULL,
            previous_reading INTEGER NOT NULL,
            current_reading INTEGER NOT NULL,
            units INTEGER NOT NULL,
            amount REAL NOT NULL,
            breakdown_json TEXT NOT NULL,
            tariff_snapshot TEXT NOT NULL,
            billing_month TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (consumer_pk) REFERENCES consumers(id)
        );
        CREATE TABLE IF NOT EXISTS tariffs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slab_order INTEGER NOT NULL UNIQUE,
            slab_units INTEGER,
            rate REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL UNIQUE,
            password_hash TEXT NOT NULL,
            role TEXT NOT NULL CHECK(role IN ('admin','customer')),
            consumer_pk INTEGER UNIQUE,
            created_at TEXT NOT NULL,
            FOREIGN KEY (consumer_pk) REFERENCES consumers(id)
        );
        """)
        if con.execute("SELECT COUNT(*) FROM tariffs").fetchone()[0] == 0:
            con.executemany(
                "INSERT INTO tariffs(slab_order, slab_units, rate) VALUES (?, ?, ?)",
                [(i + 1, item["slab_units"], item["rate"]) for i, item in enumerate(DEFAULT_TARIFFS)]
            )
        admin_name = os.environ.get("GRIDGUARD_ADMIN_USERNAME", "admin")
        admin_password = os.environ.get("GRIDGUARD_ADMIN_PASSWORD", "GridGuard@123")
        if not con.execute("SELECT 1 FROM users WHERE username=?", (admin_name,)).fetchone():
            con.execute("INSERT INTO users(username,password_hash,role,consumer_pk,created_at) VALUES(?,?,?,?,?)",
                (admin_name, generate_password_hash(admin_password), "admin", None, datetime.now().isoformat(timespec="seconds")))


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(url_for("login"))
        if session.get("role") != "admin":
            abort(403)
        return view(*args, **kwargs)
    return wrapped


def get_tariffs():
    with get_db() as con:
        rows = con.execute("SELECT slab_units, rate FROM tariffs ORDER BY slab_order").fetchall()
    if not rows:
        return DEFAULT_TARIFFS
    tariffs = [{"slab_units": row["slab_units"], "rate": float(row["rate"])} for row in rows]
    # A final unlimited slab is required for every unit to be billed.
    if tariffs[-1]["slab_units"] is not None:
        tariffs.append({"slab_units": None, "rate": tariffs[-1]["rate"]})
    return tariffs


def calculate_bill(units, slabs=None):
    """Return (amount, breakdown) using progressive, configurable tariff slabs."""
    if not isinstance(units, int) or units < 0:
        raise ValueError("Units must be a non-negative integer.")
    if slabs is None:
        slabs = [{"slab_units": size if size != float("inf") else None, "rate": rate}
                 for size, rate in TARIFF_SLABS]
    remaining = units
    total = 0.0
    breakdown = []
    lower_limit = 0
    for slab in slabs:
        if remaining <= 0:
            break
        limit = slab["slab_units"]
        slab_units = remaining if limit is None else min(remaining, int(limit))
        if slab_units <= 0:
            continue
        rate = float(slab["rate"])
        amount = slab_units * rate
        breakdown.append({
            "from_unit": lower_limit + 1,
            "to_unit": lower_limit + slab_units,
            "units": slab_units,
            "rate": rate,
            "amount": round(amount, 2),
        })
        total += amount
        remaining -= slab_units
        lower_limit += slab_units
    if remaining > 0:
        raise ValueError("Tariff configuration does not cover all consumed units.")
    return round(total, 2), breakdown


def _consumer_by_name(con, name):
    return con.execute("SELECT * FROM consumers WHERE lower(name)=lower(?) ORDER BY id LIMIT 1", (name,)).fetchone()


def _get_or_create_consumer(name, consumer_id=""):
    with get_db() as con:
        if consumer_id:
            row = con.execute("SELECT * FROM consumers WHERE consumer_id=?", (consumer_id.strip(),)).fetchone()
            if row:
                return row
            raise ValueError("Selected consumer ID was not found. Register the consumer first.")
        row = _consumer_by_name(con, name)
        if row:
            return row
        next_id = con.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM consumers").fetchone()[0]
        generated_id = f"GG-{next_id:05d}"
        now = datetime.now().isoformat(timespec="seconds")
        con.execute(
            "INSERT INTO consumers(consumer_id, name, created_at) VALUES (?, ?, ?)",
            (generated_id, name, now)
        )
        return con.execute("SELECT * FROM consumers WHERE consumer_id=?", (generated_id,)).fetchone()


def _save_bill(consumer, previous, current, units, amount, breakdown, tariffs):
    now = datetime.now()
    billing_month = now.strftime("%Y-%m")
    snapshot = [{"slab_units": t["slab_units"], "rate": t["rate"]} for t in tariffs]
    with get_db() as con:
        cur = con.execute(
            """INSERT INTO bills
            (bill_number, consumer_pk, previous_reading, current_reading, units, amount,
             breakdown_json, tariff_snapshot, billing_month, created_at)
            VALUES ('PENDING', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (consumer["id"], previous, current, units, amount,
             __import__("json").dumps(breakdown), __import__("json").dumps(snapshot),
             billing_month, now.isoformat(timespec="seconds"))
        )
        bill_id = cur.lastrowid
        bill_number = f"GB-{now.strftime('%Y%m')}-{bill_id:05d}"
        con.execute("UPDATE bills SET bill_number=? WHERE id=?", (bill_number, bill_id))
        return bill_id, bill_number


def _get_bill(bill_id):
    with get_db() as con:
        row = con.execute("""
            SELECT b.*, c.consumer_id, c.name AS consumer_name, c.address, c.phone
            FROM bills b JOIN consumers c ON c.id=b.consumer_pk WHERE b.id=?
        """, (bill_id,)).fetchone()
    if row is None:
        abort(404)
    bill = dict(row)
    bill["breakdown"] = __import__("json").loads(bill["breakdown_json"])
    return bill


BASE_STYLE = """
<style>
:root{--navy:#12304a;--teal:#0f766e;--ink:#172b3a;--muted:#64748b;--line:#dce5ed;--bg:#f3f7fb}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font-family:Inter,Segoe UI,Arial,sans-serif}
header{background:linear-gradient(120deg,#12304a,#0f766e);color:white;padding:22px max(5vw,20px)}
header h1{margin:0;font-size:26px}header p{margin:6px 0 0;color:#dbeafe}
nav{display:flex;gap:8px;flex-wrap:wrap;margin-top:16px}nav a{color:#fff;text-decoration:none;border:1px solid #ffffff55;padding:8px 12px;border-radius:8px;font-size:14px}
main{max-width:1120px;margin:26px auto;padding:0 18px 40px}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px}
.card{background:white;border:1px solid var(--line);border-radius:14px;padding:20px;box-shadow:0 4px 14px #12304a08;margin-bottom:16px}
h2{margin-top:0;color:var(--navy)}h3{color:var(--navy)}.muted{color:var(--muted)}label{display:block;font-weight:600;margin:12px 0 6px}
input,select,textarea{width:100%;padding:10px;border:1px solid #cbd5e1;border-radius:8px;background:white;font:inherit}
button,.btn{display:inline-block;border:0;background:var(--teal);color:white;padding:10px 15px;border-radius:8px;text-decoration:none;cursor:pointer;font:inherit;margin-top:12px}
.btn.secondary,button.secondary{background:#e2e8f0;color:#12304a}.btn.dark{background:#12304a}
table{width:100%;border-collapse:collapse;margin-top:12px;background:white}th,td{text-align:left;border-bottom:1px solid var(--line);padding:10px;vertical-align:top}th{background:#f1f5f9}
.metric{font-size:27px;font-weight:750;color:var(--teal)}.alert{padding:12px;border-radius:8px;margin:12px 0}.error{background:#fee2e2;color:#991b1b}.success{background:#dcfce7;color:#166534}
.badge{display:inline-block;border-radius:999px;padding:4px 8px;background:#e2e8f0;font-size:12px}.pass{background:#dcfce7;color:#166534}.fail{background:#fee2e2;color:#991b1b}
.row2{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px}.small{font-size:13px}.right{text-align:right}
@media print{header nav,.no-print{display:none}body{background:white}.card{box-shadow:none}}
</style>
"""
NAV = """
<header><h1>GridGuard AI</h1><p>Electricity billing and explainable quality assurance</p>
<nav>
{% if session.get('role') == 'admin' %}
<a href="/admin">Admin home</a><a href="/consumers">Manage consumers</a><a href="/history">Billing history</a>
<a href="/tariffs">Tariff settings</a><a href="http://127.0.0.1:5002/" target="_blank" rel="noopener">Test generator</a>
<a href="http://127.0.0.1:5001/" target="_blank" rel="noopener">QA dashboard</a>
{% elif session.get('role') == 'customer' %}<a href="/">My account</a><a href="/my-bills">My bills</a>
{% else %}<a href="/login">Login</a><a href="/register">Customer registration</a>{% endif %}
{% if session.get('user_id') %}<a href="/logout">Logout ({{ session.get('username') }})</a>{% endif %}
</nav></header>
"""

LOGIN_HTML = """<!doctype html><html><head><title>Login | GridGuard AI</title>""" + BASE_STYLE + """</head><body>""" + NAV + """
<main><section class="card" style="max-width:520px;margin:auto"><h2>Sign in</h2>
{% if message %}<div class="alert error">{{ message }}</div>{% endif %}
<form method="post"><label>Username</label><input name="username" required maxlength="80" autocomplete="username">
<label>Password</label><input type="password" name="password" required autocomplete="current-password">
<button>Login</button></form><p class="small muted">Customers must activate an account linked to a registered consumer ID.</p></section></main></body></html>"""

REGISTER_HTML = """<!doctype html><html><head><title>Customer registration | GridGuard AI</title>""" + BASE_STYLE + """</head><body>""" + NAV + """
<main><section class="card" style="max-width:600px;margin:auto"><h2>Activate customer account</h2>
<p class="muted">An administrator must register your consumer record first. Enter the matching consumer ID and registered 10-digit phone number.</p>
{% if message %}<div class="alert {{ 'success' if ok else 'error' }}">{{ message }}</div>{% endif %}
<form method="post"><label>Consumer ID *</label><input name="consumer_id" required maxlength="20">
<label>Registered Indian phone number *</label><input name="phone" required inputmode="numeric" pattern="[0-9]{10}" minlength="10" maxlength="10" title="Enter exactly 10 digits">
<label>Choose username *</label><input name="username" required minlength="3" maxlength="80" autocomplete="username">
<label>Password *</label><input type="password" name="password" required minlength="8" autocomplete="new-password">
<label>Confirm password *</label><input type="password" name="confirm_password" required minlength="8" autocomplete="new-password">
<button>Create customer account</button></form></section></main></body></html>"""

CUSTOMER_HOME_HTML = """<!doctype html><html><head><title>My account | GridGuard AI</title>""" + BASE_STYLE + """</head><body>""" + NAV + """
<main><section class="card"><h2>Welcome, {{ consumer.name }}</h2><p>Your consumer ID: <strong>{{ consumer.consumer_id }}</strong></p>
<p>Address: {{ consumer.address or 'Not provided' }}</p><p>Phone: {{ consumer.phone or 'Not provided' }}</p>
<a class="btn" href="/my-bills">View my bills and receipts</a></section>
<section class="card"><h2>Recent bills</h2><table><thead><tr><th>Bill number</th><th>Month</th><th>Units</th><th>Amount</th><th>Receipt</th></tr></thead><tbody>
{% for b in bills %}<tr><td>{{ b.bill_number }}</td><td>{{ b.billing_month }}</td><td>{{ b.units }}</td><td>₹{{ "%.2f"|format(b.amount) }}</td><td><a href="/receipt/{{ b.id }}">View</a></td></tr>{% else %}<tr><td colspan="5">No bills have been generated for your account yet.</td></tr>{% endfor %}
</tbody></table></section></main></body></html>"""

@app.route("/login", methods=["GET", "POST"])
def login():
    message = None
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        with get_db() as con:
            user = con.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        if user and check_password_hash(user["password_hash"], password):
            session.clear()
            session["user_id"] = user["id"]
            session["username"] = user["username"]
            session["role"] = user["role"]
            session["consumer_pk"] = user["consumer_pk"]
            return redirect(url_for("home"))
        message = "Invalid username or password."
    return render_template_string(LOGIN_HTML, message=message)


@app.route("/register", methods=["GET", "POST"])
def register():
    message = None
    ok = False
    if request.method == "POST":
        consumer_id = request.form.get("consumer_id", "").strip()
        phone = request.form.get("phone", "").strip()
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirm = request.form.get("confirm_password", "")
        try:
            if not re.fullmatch(r"[0-9]{10}", phone):
                raise ValueError("Enter an Indian phone number containing exactly 10 digits.")
            if not re.fullmatch(r"[A-Za-z0-9_.-]{3,80}", username):
                raise ValueError("Username must be 3–80 characters and use letters, digits, dot, underscore or hyphen.")
            if len(password) < 8:
                raise ValueError("Password must contain at least 8 characters.")
            if password != confirm:
                raise ValueError("Passwords do not match.")
            with get_db() as con:
                consumer = con.execute("SELECT * FROM consumers WHERE consumer_id=?", (consumer_id,)).fetchone()
                if not consumer:
                    raise ValueError("Consumer ID was not found. Contact the administrator to register your consumer record.")
                if consumer["phone"] != phone:
                    raise ValueError("The phone number does not match the registered consumer record.")
                if con.execute("SELECT 1 FROM users WHERE username=?", (username,)).fetchone():
                    raise ValueError("That username is already in use.")
                if con.execute("SELECT 1 FROM users WHERE consumer_pk=?", (consumer["id"],)).fetchone():
                    raise ValueError("An account is already linked to this consumer ID. Contact the administrator if you need help.")
                con.execute("INSERT INTO users(username,password_hash,role,consumer_pk,created_at) VALUES(?,?,?,?,?)",
                    (username, generate_password_hash(password), "customer", consumer["id"], datetime.now().isoformat(timespec="seconds")))
            message = "Customer account created. You can now log in."
            ok = True
        except ValueError as exc:
            message = str(exc)
        except sqlite3.IntegrityError:
            message = "Could not create account because the username or consumer account is already in use."
    return render_template_string(REGISTER_HTML, message=message, ok=ok)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/my-bills")
@login_required
def my_bills():
    if session.get("role") != "customer" or not session.get("consumer_pk"):
        abort(403)
    with get_db() as con:
        bills = [dict(r) for r in con.execute("SELECT * FROM bills WHERE consumer_pk=? ORDER BY id DESC", (session["consumer_pk"],)).fetchall()]
    return render_template_string(MY_BILLS_HTML, bills=bills)


MY_BILLS_HTML = """<!doctype html><html><head><title>My bills | GridGuard AI</title>""" + BASE_STYLE + """</head><body>""" + NAV + """
<main><section class="card"><h2>My billing history</h2><table><thead><tr><th>Bill number</th><th>Month</th><th>Previous</th><th>Current</th><th>Units</th><th>Amount</th><th>Receipt</th></tr></thead><tbody>
{% for b in bills %}<tr><td>{{ b.bill_number }}</td><td>{{ b.billing_month }}</td><td>{{ b.previous_reading }}</td><td>{{ b.current_reading }}</td><td>{{ b.units }}</td><td>₹{{ "%.2f"|format(b.amount) }}</td><td><a href="/receipt/{{ b.id }}">Open receipt</a></td></tr>{% else %}<tr><td colspan="7">No bills found.</td></tr>{% endfor %}
</tbody></table><a class="btn secondary" href="/">Back to account</a></section></main></body></html>"""

HOME_HTML = """<!doctype html><html><head><title>GridGuard AI Billing</title>""" + BASE_STYLE + """</head><body>""" + NAV + """
<main><div class="grid">
<section class="card"><h2>Calculate a bill</h2><p class="muted">Register a consumer or enter a name. Existing names reuse their consumer record.</p>
<form method="post" action="/calculate">
<label>Consumer name *</label><input name="consumer_name" required value="{{ consumer_name or '' }}">
<label>Registered consumer ID (optional)</label>
<select name="consumer_id"><option value="">Find/create by name</option>{% for c in consumers %}<option value="{{ c.consumer_id }}">{{ c.consumer_id }} — {{ c.name }}</option>{% endfor %}</select>
<div class="row2"><div><label>Previous meter reading *</label><input type="number" name="previous_reading" min="0" step="1" required value="{{ previous_reading if previous_reading is not none else '' }}"></div>
<div><label>Current meter reading *</label><input type="number" name="current_reading" min="0" step="1" required value="{{ current_reading if current_reading is not none else '' }}"></div></div>
<button type="submit">Calculate Electricity Bill</button></form>
{% if error %}<div class="alert error">{{ error }}</div>{% endif %}
{% if bill is not none %}<div class="alert success"><strong>Bill saved successfully.</strong> Receipt: {{ bill_number }}</div>
<h3>Electricity Bill</h3><p><strong>Consumer:</strong> {{ consumer_name }} ({{ consumer_id }})</p>
<p><strong>Previous Reading:</strong> {{ previous_reading }} | <strong>Current Reading:</strong> {{ current_reading }}</p>
<p><strong>Units Consumed:</strong> {{ units }}</p>
<table><thead><tr><th>Units in slab</th><th>Rate per unit</th><th>Amount</th></tr></thead><tbody>
{% for item in breakdown %}<tr><td>{{ item.units }} ({{ item.from_unit }}–{{ item.to_unit }})</td><td>₹{{ "%.2f"|format(item.rate) }}</td><td>₹{{ "%.2f"|format(item.amount) }}</td></tr>{% endfor %}
</tbody></table><h2>Total: Rs. {{ "%.2f"|format(bill) }}</h2>
<a class="btn" href="/receipt/{{ bill_id }}">View / download receipt</a>{% endif %}
</section>
<section class="card"><h2>System overview</h2><div class="row2">
<div><div class="metric">{{ consumer_count }}</div><div class="muted">Registered consumers</div></div>
<div><div class="metric">{{ bill_count }}</div><div class="muted">Bills recorded</div></div></div>
<hr><p>Billing records are stored in SQLite. Tariff settings are configurable and each bill stores a snapshot of the rates used.</p>
<a class="btn secondary" href="/consumers">Manage consumers</a> <a class="btn secondary" href="/history">View history</a>
</section></div>
<section class="card"><h2>Current tariff slabs</h2><table><thead><tr><th>Slab</th><th>Units</th><th>Rate / unit</th></tr></thead><tbody>
{% for t in tariffs %}<tr><td>{{ loop.index }}</td><td>{{ t.slab_units if t.slab_units else "Above previous slabs" }}</td><td>₹{{ "%.2f"|format(t.rate) }}</td></tr>{% endfor %}
</tbody></table><a class="btn secondary" href="/tariffs">Edit tariff settings</a></section></main></body></html>"""


@app.route("/")
@login_required
def home():
    if session.get("role") == "admin":
        return redirect(url_for("admin_dashboard"))
    if not session.get("consumer_pk"):
        abort(403)
    with get_db() as con:
        consumer = con.execute("SELECT * FROM consumers WHERE id=?", (session["consumer_pk"],)).fetchone()
        bills = [dict(r) for r in con.execute("SELECT * FROM bills WHERE consumer_pk=? ORDER BY id DESC LIMIT 5", (session["consumer_pk"],)).fetchall()]
    if consumer is None:
        abort(403)
    return render_template_string(CUSTOMER_HOME_HTML, consumer=dict(consumer), bills=bills)


@app.route("/admin")
@admin_required
def admin_dashboard():
    with get_db() as con:
        consumers = [dict(r) for r in con.execute("SELECT * FROM consumers ORDER BY name").fetchall()]
        consumer_count = con.execute("SELECT COUNT(*) FROM consumers").fetchone()[0]
        bill_count = con.execute("SELECT COUNT(*) FROM bills").fetchone()[0]
    return render_template_string(HOME_HTML, consumers=consumers, consumer_count=consumer_count,
        bill_count=bill_count, tariffs=get_tariffs(), error=None, bill=None, bill_number=None,
        bill_id=None, breakdown=[], consumer_name="", consumer_id="", previous_reading="",
        current_reading="", units=0)


@app.route("/calculate", methods=["POST"])
@admin_required
def calculate():
    name = request.form.get("consumer_name", "").strip()
    prev_text = request.form.get("previous_reading", "").strip()
    curr_text = request.form.get("current_reading", "").strip()
    selected_id = request.form.get("consumer_id", "").strip()
    error = None
    bill = bill_number = bill_id = None
    breakdown = []
    units = 0
    previous_reading = prev_text
    current_reading = curr_text
    consumer_id = ""
    try:
        if not name:
            raise ValueError("Consumer name is required.")
        if len(name) > 80 or not re.fullmatch(r"[A-Za-z][A-Za-z .'-]{0,79}", name):
            raise ValueError("Name must be 1–80 characters and contain letters, spaces, apostrophes, dots or hyphens.")
        if not prev_text or not curr_text:
            raise ValueError("Both meter readings are required.")

# Allow signed whole numbers temporarily so negative values
# receive the correct negative-reading validation message.
        if (
            not re.fullmatch(r"[+-]?\d+", prev_text)
    or not re.fullmatch(r"[+-]?\d+", curr_text)
):
            raise ValueError(
        "Meter readings must be whole numbers without letters or decimal points."
    )

        previous_reading = int(prev_text)
        current_reading = int(curr_text)

# Check negatives before checking the maximum permitted reading.
        if previous_reading < 0 or current_reading < 0:
            raise ValueError("Meter readings cannot be negative.")

        if previous_reading > 1_000_000_000 or current_reading > 1_000_000_000:
            raise ValueError("Meter readings cannot exceed 1,000,000,000.")
        if current_reading < previous_reading:
            raise ValueError("Current reading cannot be less than previous reading.")
        consumer = _get_or_create_consumer(name, selected_id)
        name = consumer["name"]
        consumer_id = consumer["consumer_id"]
        units = current_reading - previous_reading
        tariffs = get_tariffs()
        bill, breakdown = calculate_bill(units, tariffs)
        bill_id, bill_number = _save_bill(consumer, previous_reading, current_reading, units, bill, breakdown, tariffs)
    except (ValueError, TypeError) as exc:
        error = str(exc)
    with get_db() as con:
        consumers = [dict(r) for r in con.execute("SELECT * FROM consumers ORDER BY name").fetchall()]
        consumer_count = con.execute("SELECT COUNT(*) FROM consumers").fetchone()[0]
        bill_count = con.execute("SELECT COUNT(*) FROM bills").fetchone()[0]
    return render_template_string(HOME_HTML, consumers=consumers, consumer_count=consumer_count,
        bill_count=bill_count, tariffs=get_tariffs(), error=error, bill=bill, bill_number=bill_number,
        bill_id=bill_id, breakdown=breakdown, consumer_name=name, consumer_id=consumer_id,
        previous_reading=previous_reading, current_reading=current_reading, units=units)


CONSUMERS_HTML = """<!doctype html><html><head><title>Consumers | GridGuard AI</title>""" + BASE_STYLE + """</head><body>""" + NAV + """
<main><section class="card"><h2>Register a consumer</h2>{% if message %}<div class="alert {{ 'success' if ok else 'error' }}">{{ message }}</div>{% endif %}
<form method="post"><div class="row2"><div><label>Full name *</label><input name="name" required></div><div><label>Phone</label><input name="phone" pattern="[0-9]{10}" minlength="10" maxlength="10" inputmode="numeric" title="Enter exactly 10 digits"></div></div>
<label>Address</label><textarea name="address" rows="2" maxlength="250"></textarea><button>Register consumer</button></form></section>
<section class="card"><h2>Consumer register</h2><table><thead><tr><th>Consumer ID</th><th>Name</th><th>Phone</th><th>Address</th><th>Registered</th></tr></thead><tbody>
{% for c in consumers %}<tr><td><strong>{{ c.consumer_id }}</strong></td><td>{{ c.name }}</td><td>{{ c.phone or '—' }}</td><td>{{ c.address or '—' }}</td><td>{{ c.created_at }}</td></tr>{% else %}<tr><td colspan="5">No consumers registered yet.</td></tr>{% endfor %}
</tbody></table></section></main></body></html>"""


@app.route("/consumers", methods=["GET", "POST"])
@admin_required
def consumers_page():
    message = None
    ok = False
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        phone = request.form.get("phone", "").strip()
        address = request.form.get("address", "").strip()
        if not name:
            message = "Consumer name is required."
        elif len(name) > 80 or not re.fullmatch(r"[A-Za-z][A-Za-z .'-]{0,79}", name):
            message = "Name must be 1–80 characters and contain letters, spaces, apostrophes, dots or hyphens."
        elif phone and not re.fullmatch(r"[0-9]{10}", phone):
            message = "Phone number must contain exactly 10 digits (India)."
        elif len(address) > 250:
            message = "Address cannot exceed 250 characters."
        else:
            with get_db() as con:
                existing = _consumer_by_name(con, name)
                if existing:
                    message = f"A consumer named {name} is already registered ({existing['consumer_id']})."
                else:
                    next_id = con.execute("SELECT COALESCE(MAX(id),0)+1 FROM consumers").fetchone()[0]
                    consumer_id = f"GG-{next_id:05d}"
                    con.execute("INSERT INTO consumers(consumer_id,name,address,phone,created_at) VALUES(?,?,?,?,?)",
                        (consumer_id, name, address, phone, datetime.now().isoformat(timespec="seconds")))
                    message = f"Consumer registered successfully. Assigned ID: {consumer_id}"
                    ok = True
    with get_db() as con:
        rows = [dict(r) for r in con.execute("SELECT * FROM consumers ORDER BY id DESC").fetchall()]
    return render_template_string(CONSUMERS_HTML, consumers=rows, message=message, ok=ok)


HISTORY_HTML = """<!doctype html><html><head><title>Billing History | GridGuard AI</title>""" + BASE_STYLE + """</head><body>""" + NAV + """
<main><section class="card"><h2>Billing history</h2><p class="muted">All saved bills, grouped by recent billing activity.</p>
<form method="get" class="row2"><div><label>Search consumer</label><input name="q" value="{{ q }}" placeholder="Name or consumer ID"></div><div><label>Billing month</label><input type="month" name="month" value="{{ month }}"></div><div><button>Filter history</button></div></form>
<table><thead><tr><th>Bill no.</th><th>Consumer</th><th>Month</th><th>Previous</th><th>Current</th><th>Units</th><th>Amount</th><th>Receipt</th></tr></thead><tbody>
{% for b in bills %}<tr><td>{{ b.bill_number }}</td><td>{{ b.consumer_name }}<div class="small muted">{{ b.consumer_id }}</div></td><td>{{ b.billing_month }}</td><td>{{ b.previous_reading }}</td><td>{{ b.current_reading }}</td><td>{{ b.units }}</td><td>₹{{ "%.2f"|format(b.amount) }}</td><td><a href="/receipt/{{ b.id }}">Open</a></td></tr>{% else %}<tr><td colspan="8">No matching bills found.</td></tr>{% endfor %}
</tbody></table><a class="btn secondary" href="/history/export">Export history CSV</a></section></main></body></html>"""


@app.route("/history")
@admin_required
def history():
    q = request.args.get("q", "").strip()
    month = request.args.get("month", "").strip()
    sql = """SELECT b.*, c.name AS consumer_name, c.consumer_id FROM bills b JOIN consumers c ON c.id=b.consumer_pk WHERE 1=1"""
    args = []
    if q:
        sql += " AND (lower(c.name) LIKE lower(?) OR c.consumer_id LIKE ?)"
        args.extend([f"%{q}%", f"%{q}%"])
    if month:
        sql += " AND b.billing_month=?"
        args.append(month)
    sql += " ORDER BY b.id DESC"
    with get_db() as con:
        rows = [dict(r) for r in con.execute(sql, args).fetchall()]
    return render_template_string(HISTORY_HTML, bills=rows, q=q, month=month)


@app.route("/history/export")
@admin_required
def export_history():
    with get_db() as con:
        rows = con.execute("""SELECT b.bill_number,c.consumer_id,c.name,b.billing_month,b.previous_reading,
        b.current_reading,b.units,b.amount,b.created_at FROM bills b JOIN consumers c ON c.id=b.consumer_pk ORDER BY b.id DESC""").fetchall()
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Bill Number","Consumer ID","Consumer Name","Billing Month","Previous Reading","Current Reading","Units","Amount INR","Created At"])
    writer.writerows([tuple(r) for r in rows])
    return send_file(io.BytesIO(output.getvalue().encode("utf-8-sig")), mimetype="text/csv",
                     as_attachment=True, download_name="GridGuardAI_Billing_History.csv")


RECEIPT_HTML = """<!doctype html><html><head><title>Receipt {{ bill.bill_number }}</title>""" + BASE_STYLE + """</head><body>""" + NAV + """
<main><section class="card" style="max-width:800px;margin:auto"><div class="no-print right"><button onclick="window.print()">Print receipt</button> <a class="btn" href="/receipt/{{ bill.id }}/download">Download CSV</a></div>
<h2>Electricity Bill Receipt</h2><p><strong>GridGuard AI</strong></p><hr>
<div class="row2"><div><p><span class="muted">Bill number</span><br><strong>{{ bill.bill_number }}</strong></p><p><span class="muted">Billing month</span><br>{{ bill.billing_month }}</p></div>
<div><p><span class="muted">Bill date</span><br>{{ bill.created_at }}</p><p><span class="muted">Consumer ID</span><br><strong>{{ bill.consumer_id }}</strong></p></div></div>
<h3>Consumer details</h3><p>{{ bill.consumer_name }}<br>{{ bill.address or '' }}<br>{{ bill.phone or '' }}</p>
<table><tbody><tr><th>Previous meter reading</th><td>{{ bill.previous_reading }}</td></tr><tr><th>Current meter reading</th><td>{{ bill.current_reading }}</td></tr><tr><th>Units consumed</th><td>{{ bill.units }}</td></tr></tbody></table>
<h3>Tariff breakdown</h3><table><thead><tr><th>Unit range</th><th>Units</th><th>Rate</th><th>Amount</th></tr></thead><tbody>
{% for item in bill.breakdown %}<tr><td>{{ item.from_unit }}–{{ item.to_unit }}</td><td>{{ item.units }}</td><td>₹{{ "%.2f"|format(item.rate) }}</td><td>₹{{ "%.2f"|format(item.amount) }}</td></tr>{% endfor %}
</tbody></table><h2 class="right">Total due: ₹{{ "%.2f"|format(bill.amount) }}</h2>
<p class="muted small">This receipt stores the tariff rates applied when the bill was generated.</p><a class="btn secondary no-print" href="/history">Back to history</a></section></main></body></html>"""


@app.route("/receipt/<int:bill_id>")
@login_required
def receipt(bill_id):
    bill = _get_bill(bill_id)
    if session.get("role") == "customer" and bill["consumer_pk"] != session.get("consumer_pk"):
        abort(403)
    return render_template_string(RECEIPT_HTML, bill=bill)


@app.route("/receipt/<int:bill_id>/download")
@login_required
def download_receipt(bill_id):
    bill = _get_bill(bill_id)
    if session.get("role") == "customer" and bill["consumer_pk"] != session.get("consumer_pk"):
        abort(403)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Field", "Value"])
    for key, value in [
        ("Bill number", bill["bill_number"]), ("Consumer ID", bill["consumer_id"]),
        ("Consumer name", bill["consumer_name"]), ("Billing month", bill["billing_month"]),
        ("Previous reading", bill["previous_reading"]), ("Current reading", bill["current_reading"]),
        ("Units consumed", bill["units"]), ("Amount INR", f"{bill['amount']:.2f}"),
        ("Created at", bill["created_at"])
    ]:
        writer.writerow([key, value])
    return send_file(io.BytesIO(output.getvalue().encode("utf-8-sig")), mimetype="text/csv",
        as_attachment=True, download_name=f"{bill['bill_number']}_receipt.csv")


TARIFF_HTML = """<!doctype html><html><head><title>Tariff Settings</title>""" + BASE_STYLE + """</head><body>""" + NAV + """
<main><section class="card"><h2>Tariff configuration</h2><p class="muted">Edit progressive slab sizes and per-unit rates. Leave the final slab size blank to make it unlimited.</p>
{% if message %}<div class="alert {{ 'success' if ok else 'error' }}">{{ message }}</div>{% endif %}
<form method="post"><table><thead><tr><th>Slab</th><th>Maximum units in slab (blank = unlimited)</th><th>Rate per unit (₹)</th></tr></thead><tbody>
{% for t in tariffs %}<tr><td>{{ loop.index }}</td><td><input type="number" name="slab_units_{{ loop.index0 }}" min="1" value="{{ t.slab_units if t.slab_units else '' }}" {{ 'placeholder=Unlimited' if not t.slab_units else '' }}></td><td><input type="number" name="rate_{{ loop.index0 }}" min="0" step="0.01" required value="{{ t.rate }}"></td></tr>{% endfor %}
</tbody></table><button>Save tariff configuration</button></form><p class="small muted">Previously issued bills are not changed when rates are edited.</p></section></main></body></html>"""


@app.route("/tariffs", methods=["GET", "POST"])
@admin_required
def tariffs_page():
    message = None
    ok = False
    if request.method == "POST":
        try:
            new = []
            for i in range(4):
                raw_size = request.form.get(f"slab_units_{i}", "").strip()
                rate = float(request.form.get(f"rate_{i}", ""))
                if not math.isfinite(rate) or rate < 0 or rate > 1_000_000:
                    raise ValueError("Rates must be finite numbers between 0 and 1,000,000.")
                if raw_size and not re.fullmatch(r"[0-9]+", raw_size):
                    raise ValueError("Slab sizes must be whole numbers.")
                size = None if raw_size == "" else int(raw_size)
                if size is not None and (size <= 0 or size > 1_000_000_000):
                    raise ValueError("Finite slab sizes must be between 1 and 1,000,000,000 units.")
                if i < 3 and size is None:
                    raise ValueError("Only the final slab can be unlimited.")
                if i == 3 and size is not None:
                    raise ValueError("The final slab must be unlimited; leave its size blank.")
                new.append({"slab_units": size, "rate": rate})
            with get_db() as con:
                con.execute("DELETE FROM tariffs")
                con.executemany("INSERT INTO tariffs(slab_order,slab_units,rate) VALUES(?,?,?)",
                    [(i+1, t["slab_units"], t["rate"]) for i,t in enumerate(new)])
            message = "Tariff configuration saved. New bills will use these rates."
            ok = True
        except (ValueError, TypeError) as exc:
            message = f"Tariff configuration was not saved: {exc}"
    return render_template_string(TARIFF_HTML, tariffs=get_tariffs(), message=message, ok=ok)


@app.errorhandler(404)
def not_found(_error):
    return "Requested record was not found.", 404


init_db()

if __name__ == "__main__":
    app.run(debug=False)
