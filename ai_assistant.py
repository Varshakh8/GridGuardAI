import os
from flask import Flask, render_template_string, request, session, redirect

app = Flask(__name__)
app.secret_key = os.environ.get("GRIDGUARD_SECRET_KEY", "gridguard-local-dev-key-change-before-deployment")
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# Documented baseline scenarios. This is a transparent rule engine, not an ML model.
@app.before_request
def require_admin_session():
    # This app shares the signed Flask session with the billing app on localhost.
    if session.get("role") != "admin":
        return redirect("http://127.0.0.1:5000/login")


SCENARIO_CATALOG = {
    "zero_consumption": "No consumption when readings are equal",
    "valid_reading": "A valid reading pair produces a bill",
    "boundary_100": "First tariff boundary",
    "boundary_101": "Transition above 100 units",
    "boundary_200": "Second tariff boundary",
    "boundary_201": "Transition above 200 units",
    "boundary_300": "Third tariff boundary",
    "boundary_300": "Exact end of the third tariff slab",
    "boundary_301": "Transition above 300 units",
    "negative_reading": "Negative readings are rejected",
    "negative_current": "Negative current reading is rejected",
    "reversed_readings": "Current reading below previous is rejected",
    "blank_name": "Blank consumer name is rejected",
    "missing_previous": "Missing previous reading is rejected",
    "missing_current": "Missing current reading is rejected",
    "non_numeric_reading": "Non-numeric readings are rejected",
    "custom_tariff": "Changed tariff configuration is respected",
}
MANUAL_BASELINE = [
    {"id": "MAN-01", "scenario": "valid_reading", "description": "Normal valid consumption"},
    {"id": "MAN-02", "scenario": "zero_consumption", "description": "Zero-unit consumption"},
    {"id": "MAN-03", "scenario": "boundary_100", "description": "100-unit boundary"},
    {"id": "MAN-04", "scenario": "negative_reading", "description": "Negative reading rejection"},
    {"id": "MAN-05", "scenario": "reversed_readings", "description": "Reversed readings rejection"},
    {"id": "MAN-06", "scenario": "blank_name", "description": "Missing consumer name"},
]


def _case(case_id, technique, category, previous, current, name, expected, reason,
          scenario, expected_bill=None, expected_error=None, risk="Medium", priority=2):
    return {
        "id": case_id, "technique": technique, "category": category,
        "previous_reading": previous, "current_reading": current,
        "consumer_name": name, "expected": expected, "reason": reason,
        "scenario": scenario, "expected_bill": expected_bill,
        "expected_error": expected_error, "risk": risk, "priority": priority,
        "input": f"Name={name!r}; Previous={previous!r}; Current={current!r}",
    }


def generate_test_cases(category="all"):
    """Generate explainable black-box tests from transparent QA rules."""
    cases = []
    if category in ("all", "boundary"):
        cases.extend([
            _case("BVA-01", "Boundary Value Analysis", "boundary", 0, 99, "Test Consumer",
                  "99 units; bill ₹297", "One unit below the 100-unit tariff boundary.",
                  "valid_reading", 297, risk="Medium", priority=2),
            _case("BVA-02", "Boundary Value Analysis", "boundary", 0, 100, "Test Consumer",
                  "100 units; bill ₹300", "Exact end of the first tariff slab.",
                  "boundary_100", 300, risk="High", priority=1),
            _case("BVA-03", "Boundary Value Analysis", "boundary", 0, 101, "Test Consumer",
                  "101 units; bill ₹305", "One unit above the first boundary catches slab-transition errors.",
                  "boundary_101", 305, risk="High", priority=1),
            _case("BVA-04", "Boundary Value Analysis", "boundary", 0, 200, "Test Consumer",
                  "200 units; bill ₹800", "Exact end of the second tariff slab.",
                  "boundary_200", 800, risk="High", priority=1),
            _case("BVA-05", "Boundary Value Analysis", "boundary", 0, 201, "Test Consumer",
                  "201 units; bill ₹807", "One unit above the second boundary.",
                  "boundary_201", 807, risk="High", priority=1),
            _case("BVA-06", "Boundary Value Analysis", "boundary", 0, 301, "Test Consumer",
                  "301 units; bill ₹1508", "Checks transition to the highest tariff slab.",
                  "boundary_301", 1508, risk="High", priority=1),
            _case("BVA-07", "Boundary Value Analysis", "boundary", 0, 300, "Test Consumer",
                  "300 units; bill ₹1500", "Exact end of the third tariff slab.",
                  "boundary_300", 1500, risk="High", priority=1),
        ])
    if category in ("all", "equivalence"):
        cases.extend([
            _case("EP-01", "Equivalence Partitioning", "equivalence", 100, 150, "Test Consumer",
                  "50 units; bill ₹150", "Representative valid non-negative reading pair.",
                  "valid_reading", 150, risk="Medium", priority=2),
            _case("EP-02", "Equivalence Partitioning", "equivalence", 200, 150, "Test Consumer",
                  "Reject with current-reading validation error.",
                  "Representative of the invalid class current < previous.",
                  "reversed_readings", expected_error="Current reading cannot be less than previous reading.",
                  risk="High", priority=1),
            _case("EP-03", "Equivalence Partitioning", "equivalence", -1, 100, "Test Consumer",
                  "Reject negative meter readings.", "Representative of negative input class.",
                  "negative_reading", expected_error="Meter readings cannot be negative.",
                  risk="High", priority=1),
            _case("EP-04", "Equivalence Partitioning", "equivalence", 100, 150, "",
                  "Reject blank consumer name.", "Required consumer name is blank.",
                  "blank_name", expected_error="Consumer name is required.", risk="High", priority=1),
            _case("EP-05", "Equivalence Partitioning", "equivalence", None, 100, "Test Consumer",
                  "Reject missing previous reading.", "Previous reading is required.",
                  "missing_previous", expected_error="Both meter readings are required.", risk="High", priority=1),
            _case("EP-06", "Equivalence Partitioning", "equivalence", 50, None, "Test Consumer",
                  "Reject missing current reading.", "Current reading is required.",
                  "missing_current", expected_error="Both meter readings are required.", risk="High", priority=1),
            _case("EP-07", "Equivalence Partitioning", "equivalence", "abc", 100, "Test Consumer",
                  "Reject non-numeric previous reading.", "Previous reading must be an integer.",
                  "non_numeric_reading",
expected_error="Meter readings must be whole numbers without letters or decimal points.",
risk="High", priority=1),
            _case("EP-08", "Equivalence Partitioning", "equivalence", 50, "abc", "Test Consumer",
                  "Reject non-numeric current reading.", "Current reading must be an integer.",
                  "non_numeric_reading",
expected_error="Meter readings must be whole numbers without letters or decimal points.",
risk="High", priority=1),
            _case("EP-09", "Equivalence Partitioning", "equivalence", 0, -1, "Test Consumer",
                  "Reject negative current reading.", "Current reading cannot be negative.",
                  "negative_current", expected_error="Meter readings cannot be negative.", risk="High", priority=1),
        ])
    if category in ("all", "decision"):
        cases.extend([
            _case("DT-01", "Decision Table", "decision", 50, 50, "Test Consumer",
                  "Accept valid inputs and calculate ₹0.", "Name present, readings valid, consumption zero.",
                  "zero_consumption", 0, risk="Medium", priority=2),
            _case("DT-02", "Decision Table", "decision", 0, 50, "",
                  "Reject with required-name validation error.", "Name condition is false while readings are valid.",
                  "blank_name", expected_error="Consumer name is required.", risk="High", priority=1),
            _case("DT-03", "Decision Table", "decision", 200, 150, "Test Consumer",
                  "Reject with reading-order validation error.", "Reading-order condition is false.",
                  "reversed_readings", expected_error="Current reading cannot be less than previous reading.",
                  risk="High", priority=1),
        ])
    return cases


def find_missing_scenarios(cases):
    covered = {case["scenario"] for case in cases}
    return [{"scenario": key, "description": description}
            for key, description in SCENARIO_CATALOG.items() if key not in covered]


def compare_with_manual_baseline(cases):
    generated = {case["scenario"] for case in cases}
    manual = {case["scenario"] for case in MANUAL_BASELINE}
    overlap = generated & manual
    return {
        "manual_count": len(MANUAL_BASELINE),
        "generated_count": len(cases),
        "overlap_count": len(overlap),
        "overlap_percent": round(100 * len(overlap) / len(manual), 1) if manual else 0,
        "matched_scenarios": sorted(overlap),
        "manual_only": sorted(manual - generated),
        "generated_only": sorted(generated - manual),
    }


HTML = """<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>GridGuard AI Test Generator</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
<style>body{background:#f3f7fb;font-family:Arial,sans-serif}.container{max-width:1200px;margin:30px auto}.panel{background:white;border-radius:14px;padding:24px;margin-bottom:18px;box-shadow:0 4px 16px #0f172a0b}h1{color:#12304a}.reason{min-width:240px}.risk-high{color:#b91c1c;font-weight:bold}.risk-medium{color:#92400e;font-weight:bold}.metric{font-size:24px;font-weight:bold;color:#0f766e}</style></head><body><div class="container">
<div class="panel"><h1>GridGuard AI</h1><p class="text-muted">Explainable AI-Assisted Test Case Generator · rule-based black-box test design</p>
<p>This component applies documented BVA, equivalence-partitioning and decision-table rules. It is rule-based, not a trained ML model. Generated cases are candidates until executed.</p>
<form method="post"><label class="form-label" for="category">Testing technique</label><select class="form-select mb-3" id="category" name="category"><option value="all">All techniques</option><option value="boundary">Boundary Value Analysis</option><option value="equivalence">Equivalence Partitioning</option><option value="decision">Decision Table</option></select><button class="btn btn-primary">Generate cases</button></form>
</div>
{% if cases is not none %}<div class="panel"><h2>Generated candidates: {{ cases|length }}</h2><div class="table-responsive"><table class="table table-bordered align-middle"><thead><tr><th>ID</th><th>Technique</th><th>Input</th><th>Expected result</th><th>Risk / priority</th><th>Why this test?</th></tr></thead><tbody>{% for c in cases %}<tr><td>{{ c.id }}</td><td>{{ c.technique }}</td><td>{{ c.input }}</td><td>{{ c.expected }}</td><td class="{{ 'risk-high' if c.risk == 'High' else 'risk-medium' }}">{{ c.risk }} / P{{ c.priority }}</td><td class="reason">{{ c.reason }}</td></tr>{% endfor %}</tbody></table></div>
<h3>Scenario gap analysis</h3>{% if missing %}<p>The following catalog scenarios are not represented in this selected set:</p><ul>{% for m in missing %}<li><strong>{{ m.scenario }}</strong>: {{ m.description }}</li>{% endfor %}</ul>{% else %}<p class="text-success">All catalog scenarios are represented.</p>{% endif %}
<h3>Manual baseline comparison</h3><div class="metric">{{ comparison.overlap_percent }}% overlap</div><p>Matched {{ comparison.overlap_count }} of {{ comparison.manual_count }} manually authored baseline scenarios.</p><p class="small text-muted">Overlap is scenario-level coverage, not proof of test quality.</p>
<a class="btn btn-outline-primary" href="http://127.0.0.1:5001/test-lab" target="_blank">Execute these cases in QA Test Lab</a>
</div>{% endif %}
<div class="panel"><h3>How prioritization works</h3><p>High priority is assigned to tariff transitions and invalid inputs because these directly affect charge correctness or validation. Medium priority covers representative valid cases. Review the generated rationale before relying on a test.</p></div></div></body></html>"""


@app.route("/", methods=["GET", "POST"])
def home():
    cases = None
    category = "all"
    if request.method == "POST":
        category = request.form.get("category", "all")
        if category not in ("all", "boundary", "equivalence", "decision"):
            category = "all"
        cases = generate_test_cases(category)
    return render_template_string(HTML, cases=cases,
        missing=find_missing_scenarios(cases or []),
        comparison=compare_with_manual_baseline(cases or []), category=category)


if __name__ == "__main__":
    app.run(port=5002, debug=False)
