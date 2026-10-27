from __future__ import annotations

import csv
import io
import json
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from flask import Flask, abort, redirect, render_template_string, request, send_file, url_for, session

from ai_assistant import generate_test_cases, find_missing_scenarios, compare_with_manual_baseline
import app as billing_module
import uuid

app = Flask(__name__)
app.secret_key = os.environ.get("GRIDGUARD_SECRET_KEY", "gridguard-local-dev-key-change-before-deployment")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
PROJECT_DIR = Path(__file__).resolve().parent

@app.before_request
def require_admin_session():
    # This app shares the signed Flask session with the billing app on localhost.
    if session.get("role") != "admin":
        return redirect("http://127.0.0.1:5000/login")
REPORT_DIR = PROJECT_DIR / "reports"
REPORT_DIR.mkdir(exist_ok=True)
XML_REPORT = REPORT_DIR / "junit.xml"
QA_DB = REPORT_DIR / "qa_history.db"

STYLE = """
<style>
*{box-sizing:border-box}body{margin:0;background:#f3f7fb;color:#172b3a;font-family:Segoe UI,Arial,sans-serif}
header{background:linear-gradient(120deg,#12304a,#0f766e);color:#fff;padding:22px max(5vw,20px)}
header h1{margin:0;font-size:27px}header p{margin:5px 0 0;color:#dbeafe}
nav{display:flex;flex-wrap:wrap;gap:8px;margin-top:15px}nav a{color:white;text-decoration:none;border:1px solid #ffffff55;border-radius:8px;padding:8px 11px;font-size:14px}
main{max-width:1200px;margin:26px auto;padding:0 18px 45px}.card{background:#fff;border:1px solid #dce5ed;border-radius:14px;padding:20px;margin-bottom:16px;box-shadow:0 4px 16px #12304a08}
h2,h3{color:#12304a;margin-top:0}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(170px,1fr));gap:13px}.metric{font-size:28px;font-weight:750;color:#0f766e}.muted{color:#64748b}.small{font-size:13px}
label{display:block;font-weight:600;margin:10px 0 6px}select,input{width:100%;padding:10px;border:1px solid #cbd5e1;border-radius:8px;background:white}
button,.btn{display:inline-block;border:0;border-radius:8px;padding:10px 14px;background:#0f766e;color:white;text-decoration:none;cursor:pointer;font:inherit;margin-top:10px}.btn.alt{background:#e2e8f0;color:#12304a}
table{width:100%;border-collapse:collapse;margin-top:10px}th,td{padding:10px;border-bottom:1px solid #e2e8f0;text-align:left;vertical-align:top}th{background:#f1f5f9}.pass{color:#166534;font-weight:bold}.fail,.error{color:#b91c1c;font-weight:bold}.alert{padding:12px;border-radius:8px;background:#fef3c7;color:#92400e;white-space:pre-wrap}.bar{height:9px;background:#e2e8f0;border-radius:99px;overflow:hidden}.bar span{display:block;height:100%;background:#0f766e}
@media(max-width:600px){table{font-size:13px}td,th{padding:7px}}
</style>
"""
NAV = """<header><h1>GridGuard AI · Quality Assurance</h1><p>Execute tests, inspect failures, track defects and review coverage history</p>
<nav><a href="/">Overview</a><a href="/test-lab">Interactive Test Lab</a><a href="/run-tests-page">Full Pytest Suite</a><a href="/history">Execution History</a><a href="/download-history">Export QA History</a><a href="http://127.0.0.1:5000/">Billing App</a><a href="http://127.0.0.1:5002/">Test Generator</a></nav></header>"""


def qa_connect():
    import sqlite3
    con = sqlite3.connect(QA_DB, timeout=10)
    con.row_factory = sqlite3.Row
    return con


def init_qa_db():
    with qa_connect() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS qa_runs(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_type TEXT NOT NULL,
            total INTEGER NOT NULL,
            passed INTEGER NOT NULL,
            failed INTEGER NOT NULL,
            errors INTEGER NOT NULL,
            coverage REAL,
            duration REAL NOT NULL,
            created_at TEXT NOT NULL,
            notes TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS qa_results(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            case_id TEXT NOT NULL,
            technique TEXT NOT NULL,
            input_data TEXT NOT NULL,
            expected TEXT NOT NULL,
            actual TEXT NOT NULL,
            status TEXT NOT NULL,
            risk TEXT NOT NULL DEFAULT 'Medium',
            details TEXT NOT NULL DEFAULT '',
            FOREIGN KEY(run_id) REFERENCES qa_runs(id) ON DELETE CASCADE
        );
        CREATE TABLE IF NOT EXISTS qa_events(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            description TEXT NOT NULL,
            detected INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );
        """)


def save_run(run_type, results, duration, coverage=None, notes=""):
    total = len(results)
    passed = sum(r["status"] == "PASS" for r in results)
    failed = sum(r["status"] == "FAIL" for r in results)
    errors = sum(r["status"] == "ERROR" for r in results)
    now = datetime.now().isoformat(timespec="seconds")
    with qa_connect() as con:
        cur = con.execute("""INSERT INTO qa_runs(run_type,total,passed,failed,errors,coverage,duration,created_at,notes)
            VALUES(?,?,?,?,?,?,?,?,?)""", (run_type,total,passed,failed,errors,coverage,duration,now,notes))
        run_id = cur.lastrowid
        con.executemany("""INSERT INTO qa_results(run_id,case_id,technique,input_data,expected,actual,status,risk,details)
            VALUES(?,?,?,?,?,?,?,?,?)""", [
            (run_id, r.get("id",""), r.get("technique",""), str(r.get("input","")),
             str(r.get("expected","")), str(r.get("actual","")), r["status"],
             r.get("risk","Medium"), str(r.get("details","")))
            for r in results
        ])
    return run_id, {"total":total,"passed":passed,"failed":failed,"errors":errors,
                    "pass_rate":round(passed*100/total,1) if total else 0,
                    "duration":round(duration,3),"coverage":coverage}


def latest_coverage():
    try:
        result = subprocess.run([sys.executable,"-m","coverage","report","--format=total"],
            cwd=PROJECT_DIR,capture_output=True,text=True,timeout=15)
        if result.returncode == 0:
            return float(result.stdout.strip().splitlines()[-1].strip("%"))
    except Exception:
        pass
    with qa_connect() as con:
        row = con.execute("SELECT coverage FROM qa_runs WHERE coverage IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
    return row["coverage"] if row else None


def get_dashboard_data():
    with qa_connect() as con:
        counts = con.execute("SELECT COUNT(*) runs, COALESCE(SUM(total),0) total, COALESCE(SUM(passed),0) passed, COALESCE(SUM(failed),0) failed, COALESCE(SUM(errors),0) errors FROM qa_runs").fetchone()
        recent = [dict(r) for r in con.execute("SELECT * FROM qa_runs ORDER BY id DESC LIMIT 8").fetchall()]
        trend = [dict(r) for r in con.execute("SELECT id,created_at,coverage,run_type FROM qa_runs WHERE coverage IS NOT NULL ORDER BY id DESC LIMIT 10").fetchall()]
        defects = con.execute("SELECT COUNT(*) FROM qa_results WHERE status IN ('FAIL','ERROR')").fetchone()[0]
        demo_events = con.execute("SELECT COUNT(*) FROM qa_events WHERE detected=1").fetchone()[0]
    return dict(counts=dict(counts), recent=recent, trend=list(reversed(trend)),
                failed_cases=defects, defect_demos=demo_events)


DASH_HTML = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>GridGuard AI QA Dashboard</title>""" + STYLE + """</head><body>""" + NAV + """
<main><div class="grid">
<div class="card"><div class="muted">Execution runs</div><div class="metric">{{ data.counts.runs }}</div></div>
<div class="card"><div class="muted">Test cases executed</div><div class="metric">{{ data.counts.total }}</div></div>
<div class="card"><div class="muted">Passed cases</div><div class="metric">{{ data.counts.passed }}</div></div>
<div class="card"><div class="muted">Failed / error cases</div><div class="metric">{{ data.failed_cases }}</div><div class="small muted">Across recorded QA runs</div></div>
<div class="card"><div class="muted">Defect demos detected</div><div class="metric">{{ data.defect_demos }}</div><div class="small muted">Intentional defect experiments caught</div></div>
<div class="card"><div class="muted">Current code coverage</div><div class="metric">{{ coverage if coverage is not none else '—' }}{% if coverage is not none %}%{% endif %}</div><div class="small muted">Latest local coverage.py report when available</div></div>
</div>
<div class="card"><h2>Start a QA activity</h2><p>Run generated black-box cases against the Flask billing application or execute the complete pytest suite.</p>
<a class="btn" href="/test-lab">Open interactive test lab</a> <a class="btn alt" href="/run-tests-page">Run full pytest suite</a>
<form method="post" action="/run-defect-demo" style="display:inline"><button class="btn alt">Run intentional defect demo</button></form></div>
<div class="card"><h2>Recent execution history</h2><table><thead><tr><th>Run</th><th>Type</th><th>Time</th><th>Total</th><th>Pass</th><th>Fail</th><th>Errors</th><th>Coverage</th><th>Duration</th></tr></thead><tbody>
{% for r in data.recent %}<tr><td><a href="/run/{{ r.id }}">#{{ r.id }}</a></td><td>{{ r.run_type }}</td><td>{{ r.created_at }}</td><td>{{ r.total }}</td><td>{{ r.passed }}</td><td>{{ r.failed }}</td><td>{{ r.errors }}</td><td>{{ r.coverage if r.coverage is not none else '—' }}{% if r.coverage is not none %}%{% endif %}</td><td>{{ "%.2f"|format(r.duration) }}s</td></tr>{% else %}<tr><td colspan="9">No runs recorded yet. Start with the interactive test lab.</td></tr>{% endfor %}
</tbody></table></div>
<div class="card"><h2>Coverage trend</h2>{% if data.trend %}<table><thead><tr><th>Run</th><th>Time</th><th>Type</th><th>Coverage</th><th>Visual</th></tr></thead><tbody>{% for t in data.trend %}<tr><td>#{{ t.id }}</td><td>{{ t.created_at }}</td><td>{{ t.run_type }}</td><td>{{ t.coverage }}%</td><td><div class="bar"><span style="width:{{ t.coverage }}%"></span></div></td></tr>{% endfor %}</tbody></table>{% else %}<p class="muted">Coverage history appears after running the full pytest suite under coverage.</p>{% endif %}</div>
<div class="card"><h2>How to interpret metrics</h2><p>Pass rate is calculated from actual recorded test results. Failed-case count is a historical count of failures/errors, not a count of unique root-cause defects. Coverage is Python statement coverage for the configured source modules.</p></div></main></body></html>"""


@app.route("/")
def home():
    init_qa_db()
    return render_template_string(DASH_HTML, data=get_dashboard_data(), coverage=latest_coverage())


TEST_LAB_HTML = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Interactive Test Lab</title>""" + STYLE + """</head><body>""" + NAV + """
<main><div class="card"><h2>Interactive black-box test lab</h2><p class="muted">Generated cases are executed through the billing app's Flask test client. Test records use an isolated database so they do not pollute real consumer billing history.</p>
<form method="post" action="/run-generated-tests"><label>Test technique</label><select name="category"><option value="all">All techniques</option><option value="boundary">Boundary Value Analysis</option><option value="equivalence">Equivalence Partitioning</option><option value="decision">Decision Table</option></select><button>Generate and execute suite</button></form></div>
<div class="card"><h2>Candidate inventory</h2><table><thead><tr><th>ID</th><th>Technique</th><th>Scenario</th><th>Expected</th><th>Risk</th><th>Reason</th></tr></thead><tbody>{% for c in cases %}<tr><td>{{ c.id }}</td><td>{{ c.technique }}</td><td>{{ c.scenario }}</td><td>{{ c.expected }}</td><td>{{ c.risk }} / P{{ c.priority }}</td><td>{{ c.reason }}</td></tr>{% endfor %}</tbody></table>
<h3>Missing scenario catalog for all techniques</h3>{% if missing %}<ul>{% for m in missing %}<li><strong>{{ m.scenario }}</strong>: {{ m.description }}</li>{% endfor %}</ul>{% else %}<p>All scenarios represented.</p>{% endif %}
<h3>Manual baseline comparison</h3><p>{{ comparison.overlap_count }} / {{ comparison.manual_count }} scenarios match the manual baseline ({{ comparison.overlap_percent }}%).</p></div></main></body></html>"""


def _expected_bill_for_case(case, tariffs):
    previous = case.get("previous_reading")
    current = case.get("current_reading")
    if isinstance(previous, int) and isinstance(current, int) and current >= previous:
        amount, _ = billing_module.calculate_bill(current - previous, tariffs)
        return amount
    return None



def _execute_generated_cases(category):
    cases = generate_test_cases(category)

    # Use a unique database for every test run.
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    sandbox = REPORT_DIR / f"testlab_sandbox_{uuid.uuid4().hex}.db"

    # Read tariffs from the real database before switching databases.
    active_tariffs = billing_module.get_tariffs()
    original_db = billing_module.app.config["DATABASE"]

    try:
        billing_module.app.config["DATABASE"] = str(sandbox)
        billing_module.init_db()

        # Copy the active tariff configuration into the sandbox.
        with billing_module.get_db() as con:
            con.execute("DELETE FROM tariffs")
            con.executemany(
                "INSERT INTO tariffs(slab_order, slab_units, rate) VALUES (?, ?, ?)",
                [
                    (i + 1, tariff["slab_units"], tariff["rate"])
                    for i, tariff in enumerate(active_tariffs)
                ],
            )

        client = billing_module.app.test_client()

        # The secured billing app requires an admin session for bill testing.
        with client.session_transaction() as sess:
            with billing_module.get_db() as con:
                admin = con.execute(
                    "SELECT id, username FROM users WHERE role = 'admin' LIMIT 1"
                ).fetchone()

            if admin is None:
                raise RuntimeError(
                    "No admin account was initialized in the test database."
                )

            sess["user_id"] = admin["id"]
            sess["username"] = admin["username"]
            sess["role"] = "admin"
            sess["consumer_pk"] = None

        results = []

        for case in cases:
            payload = {
                "consumer_name": case.get("consumer_name", ""),
                "previous_reading": (
                    "" if case.get("previous_reading") is None
                    else str(case["previous_reading"])
                ),
                "current_reading": (
                    "" if case.get("current_reading") is None
                    else str(case["current_reading"])
                ),
            }

            response = client.post("/calculate", data=payload)
            body = response.get_data(as_text=True)

            expected_error = case.get("expected_error")
            expected_bill = _expected_bill_for_case(case, active_tariffs)

            if expected_error:
                ok = expected_error in body
                expected_text = f"Validation error: {expected_error}"

                if ok:
                    actual_text = expected_error
                else:
                    start = body.find('alert error')
                    end = body.find('</div>', start) if start != -1 else -1
                    actual_text = (
                        body[start:end + 6]
                        if start != -1 and end != -1
                        else "Expected validation error was not found."
                    )

            elif expected_bill is not None:
                expected_text = f"Total: Rs. {expected_bill:.2f}"
                ok = expected_text in body
                actual_text = (
                    expected_text if ok
                    else "Expected bill total not present in response."
                )

            else:
                expected_text = case.get("expected", "No expected result specified")
                ok = False
                actual_text = "Case does not contain executable input values."

            results.append({
                **case,
                "expected": expected_text,
                "actual": actual_text,
                "status": "PASS" if ok else "FAIL",
                "details": (
                    "Observed result matched the oracle."
                    if ok else
                    "Actual response differed from expected result; "
                    "inspect input and response."
                ),
                "input": json.dumps(payload, ensure_ascii=False),
            })

        return results

    finally:
        # Restore the real database before cleaning up the sandbox.
        billing_module.app.config["DATABASE"] = original_db

        # Remove the database and SQLite sidecar files if possible.
        for path in (
            sandbox,
            Path(str(sandbox) + "-wal"),
            Path(str(sandbox) + "-shm"),
        ):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                # A cleanup failure must not hide the test result.
                pass



@app.route("/test-lab")
def test_lab():
    cases = generate_test_cases("all")
    return render_template_string(TEST_LAB_HTML, cases=cases,
        missing=find_missing_scenarios(cases), comparison=compare_with_manual_baseline(cases))


RESULTS_HTML = """<!doctype html><html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>QA Run Results</title>""" + STYLE + """</head><body>""" + NAV + """
<main><div class="card"><h2>Execution results — Run #{{ run_id }}</h2><div class="grid">
<div><div class="muted">Total</div><div class="metric">{{ summary.total }}</div></div><div><div class="muted">Passed</div><div class="metric">{{ summary.passed }}</div></div>
<div><div class="muted">Failed</div><div class="metric">{{ summary.failed }}</div></div><div><div class="muted">Pass rate</div><div class="metric">{{ summary.pass_rate }}%</div></div>
<div><div class="muted">Duration</div><div class="metric">{{ summary.duration }}s</div></div></div>
<p class="muted">Results are saved in QA history. Every row includes the input, expected result, observed result, risk and status.</p>
<a class="btn" href="/run/{{ run_id }}">Open saved run details</a> <a class="btn alt" href="/test-lab">Run another suite</a></div>
<div class="card"><h2>Individual test results</h2><table><thead><tr><th>ID</th><th>Technique</th><th>Input</th><th>Expected</th><th>Actual</th><th>Status</th><th>Details</th></tr></thead><tbody>
{% for r in results %}<tr><td>{{ r.id }}</td><td>{{ r.technique }}</td><td><code>{{ r.input }}</code></td><td>{{ r.expected }}</td><td>{{ r.actual }}</td><td class="{{ 'pass' if r.status=='PASS' else 'fail' }}">{{ r.status }}</td><td>{{ r.details }}</td></tr>{% endfor %}
</tbody></table></div></main></body></html>"""


@app.route("/run-generated-tests", methods=["POST"])
def run_generated_tests():
    category = request.form.get("category", "all")
    if category not in ("all","boundary","equivalence","decision"):
        category = "all"
    start = time.perf_counter()
    results = _execute_generated_cases(category)
    duration = time.perf_counter() - start
    run_id, summary = save_run("Generated: " + category, results, duration, latest_coverage(),
        notes="Executed through Flask test client in an isolated SQLite database.")
    return render_template_string(RESULTS_HTML, run_id=run_id, summary=summary, results=results)


RUN_PAGE = """<!doctype html><html><head><title>Full Pytest Suite</title>""" + STYLE + """</head><body>""" + NAV + """
<main><div class="card"><h2>Full automated test suite</h2><p>Runs pytest under coverage and records individual test cases. Browser tests require the billing server to be running on port 5000.</p>
<form method="post" action="/run-tests"><button>Run full pytest suite</button></form>{% if message %}<div class="alert">{{ message }}</div>{% endif %}</div></main></body></html>"""


@app.route("/run-tests-page")
def run_tests_page():
    return render_template_string(RUN_PAGE, message=None)


@app.route("/run-tests", methods=["POST"])
def run_tests():
    if XML_REPORT.exists():
        XML_REPORT.unlink()
    command = [sys.executable, "-m", "coverage", "run", "--source=app,ai_assistant",
        "-m", "pytest", str(PROJECT_DIR / "tests"),
        "--ignore=" + str(PROJECT_DIR / "tests" / "test_defect_demo.py"),
        "-v", f"--junitxml={XML_REPORT}"]
    start = time.perf_counter()
    try:
        result = subprocess.run(command, cwd=PROJECT_DIR, capture_output=True, text=True, timeout=180)
        duration = time.perf_counter() - start
        coverage = None
        cov = subprocess.run([sys.executable,"-m","coverage","report","--format=total"],
            cwd=PROJECT_DIR,capture_output=True,text=True,timeout=20)
        if cov.returncode == 0 and cov.stdout.strip():
            try: coverage = float(cov.stdout.strip().splitlines()[-1].strip("%"))
            except ValueError: pass
        results = []
        if XML_REPORT.exists():
            root = ET.parse(XML_REPORT).getroot()
            for tc in root.iter("testcase"):
                failure, error, skipped = tc.find("failure"), tc.find("error"), tc.find("skipped")
                if failure is not None:
                    status, details = "FAIL", ((failure.get("message") or "") + "\n" + (failure.text or "")).strip()
                elif error is not None:
                    status, details = "ERROR", ((error.get("message") or "") + "\n" + (error.text or "")).strip()
                elif skipped is not None:
                    status, details = "SKIPPED", skipped.get("message","Test skipped")
                else:
                    status, details = "PASS", "All assertions passed"
                results.append({"id":tc.get("name","unnamed"),"technique":"Pytest suite",
                    "input":tc.get("classname",""),"expected":"Test assertions pass",
                    "actual":details if status != "PASS" else "Assertions passed",
                    "status":status,"risk":"Medium","details":details})
        run_id, summary = save_run("Full pytest", results, duration, coverage,
            notes=f"pytest exit code {result.returncode}; output saved in reports/junit.xml")
        return render_template_string(RESULTS_HTML, run_id=run_id, summary=summary, results=results)
    except subprocess.TimeoutExpired:
        return render_template_string(RUN_PAGE, message="Pytest timed out. Check the app server and Playwright browser setup."), 500


@app.route("/run/<int:run_id>")
def run_detail(run_id):
    with qa_connect() as con:
        run = con.execute("SELECT * FROM qa_runs WHERE id=?", (run_id,)).fetchone()
        results = [dict(r) for r in con.execute("SELECT * FROM qa_results WHERE run_id=? ORDER BY id", (run_id,)).fetchall()]
    if run is None: abort(404)
    summary = {"total":run["total"],"passed":run["passed"],"failed":run["failed"],"errors":run["errors"],
        "pass_rate":round(run["passed"]*100/run["total"],1) if run["total"] else 0,"duration":round(run["duration"],3)}
    for r in results:
        r["id"] = r["case_id"]
        r["input"] = r["input_data"]
        r["actual"] = r["actual"]
    return render_template_string(RESULTS_HTML, run_id=run_id, summary=summary, results=results)


HISTORY_HTML = """<!doctype html><html><head><title>Execution History</title>""" + STYLE + """</head><body>""" + NAV + """
<main><div class="card"><h2>Persistent execution history</h2><p>Every saved run remains available after restarting the dashboard.</p><table><thead><tr><th>Run</th><th>Type</th><th>Time</th><th>Total</th><th>Pass</th><th>Fail</th><th>Errors</th><th>Coverage</th><th>Duration</th><th>Details</th></tr></thead><tbody>
{% for r in runs %}<tr><td>{{ r.id }}</td><td>{{ r.run_type }}</td><td>{{ r.created_at }}</td><td>{{ r.total }}</td><td>{{ r.passed }}</td><td>{{ r.failed }}</td><td>{{ r.errors }}</td><td>{{ r.coverage if r.coverage is not none else '—' }}{% if r.coverage is not none %}%{% endif %}</td><td>{{ "%.2f"|format(r.duration) }}s</td><td><a href="/run/{{ r.id }}">Open</a></td></tr>{% else %}<tr><td colspan="10">No execution history yet.</td></tr>{% endfor %}
</tbody></table></div></main></body></html>"""


@app.route("/history")
def history():
    with qa_connect() as con:
        runs = [dict(r) for r in con.execute("SELECT * FROM qa_runs ORDER BY id DESC").fetchall()]
    return render_template_string(HISTORY_HTML, runs=runs)


@app.route("/download-history")
def download_history():
    with qa_connect() as con:
        runs = [dict(r) for r in con.execute("SELECT * FROM qa_runs ORDER BY id DESC").fetchall()]
        results = [dict(r) for r in con.execute("SELECT * FROM qa_results ORDER BY run_id,id").fetchall()]
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["Run ID","Run Type","Timestamp","Total","Passed","Failed","Errors","Coverage","Duration","Notes"])
    for r in runs:
        writer.writerow([r["id"],r["run_type"],r["created_at"],r["total"],r["passed"],r["failed"],r["errors"],r["coverage"],r["duration"],r["notes"]])
    writer.writerow([])
    writer.writerow(["Run ID","Case ID","Technique","Input","Expected","Actual","Status","Risk","Details"])
    for r in results:
        writer.writerow([r["run_id"],r["case_id"],r["technique"],r["input_data"],r["expected"],r["actual"],r["status"],r["risk"],r["details"]])
    return send_file(io.BytesIO(output.getvalue().encode("utf-8-sig")),mimetype="text/csv",as_attachment=True,download_name="GridGuardAI_QA_History.csv")


@app.route("/run-defect-demo", methods=["POST"])
def run_defect_demo():
    command = [sys.executable,"-m","pytest",str(PROJECT_DIR/"tests"/"test_defect_demo.py"),"-v"]
    start = time.perf_counter()
    result = subprocess.run(command,cwd=PROJECT_DIR,capture_output=True,text=True,timeout=30)
    output = result.stdout + "\n" + result.stderr
    detected = result.returncode != 0 and "expected Rs. 550, but got Rs. 750" in output
    with qa_connect() as con:
        con.execute("INSERT INTO qa_events(event_type,description,detected,created_at) VALUES(?,?,?,?)",
            ("Intentional defect demo","Expected ₹550, defective function returned ₹750",int(detected),datetime.now().isoformat(timespec="seconds")))
    return render_template_string("""<!doctype html><html><head><title>Defect Demo</title>""" + STYLE + """</head><body>""" + NAV + """
<main><div class="card"><h2>Intentional defect demonstration</h2><div class="alert">{{ 'Defect detected successfully: expected ₹550, actual ₹750 (overcharge ₹200).' if detected else 'Expected defect was not confirmed.' }}</div>
<p>This is a deliberately faulty demonstration function, separate from the production billing calculation.</p><pre>{{ output }}</pre><a class="btn" href="/">Back to dashboard</a></div></main></body></html>""", detected=detected, output=output[-8000:])


init_qa_db()

if __name__ == "__main__":
    app.run(port=5001, debug=False)
