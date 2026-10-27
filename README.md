# GridGuard AI — Enhanced STQA Project

**Project title:** GridGuard AI: An AI-Assisted Black-Box Testing and Quality Assurance Framework for an Electricity Consumption and Billing System.

GridGuard AI combines a usable electricity billing application with an interactive QA test lab, explainable rule-based test-case generation, test execution history, defect demonstration, and coverage reporting.

> **Accuracy note:** The test-case assistant is a transparent rule-based engine, not a trained machine-learning model. It generates candidate cases from documented Boundary Value Analysis, Equivalence Partitioning, and Decision Table rules, explains each case, assigns risk priority, detects scenario gaps, and compares scenarios with a manually authored baseline.

## Features

### Authentication and roles
- Separate customer and administrator access using hashed passwords and signed sessions.
- Customer account activation requires a previously registered consumer ID and matching 10-digit Indian phone number.
- Customers can view only their own profile, bills and receipts.
- Administrators manage consumers, meter readings/bill generation, tariff settings, the AI test generator and QA dashboard.
- The AI generator and QA dashboard check the shared admin session, so visiting ports 5001/5002 directly does not bypass the role check.
- Demo admin login on a fresh local database: username `admin`, password `GridGuard@123`. Change these before any shared or deployed use by setting `GRIDGUARD_ADMIN_USERNAME`, `GRIDGUARD_ADMIN_PASSWORD`, and `GRIDGUARD_SECRET_KEY` environment variables before first startup. The demo defaults are for local academic demonstration only.
- Indian phone numbers must be exactly 10 digits; optional phone fields may be left blank on admin consumer records, but customer account activation requires a matching phone number.

### Phase 1 — Billing system
- Consumer registration with unique IDs such as `GG-00001`.
- Persistent SQLite consumer and bill records.
- Bill history with consumer/month filters.
- Progressive tariff calculation with editable slab sizes and per-unit rates.
- A tariff snapshot is saved with every bill, so editing future tariffs does not alter previous bills.
- Receipt page with print support and CSV download.
- Billing-history CSV export.

### Phase 2 — Interactive test lab
- Generate and execute BVA, Equivalence Partitioning, Decision Table, or all candidate cases.
- Each result displays the test input, expected result, observed result, PASS/FAIL status, risk and explanatory details.
- The generated test lab executes requests through the billing application's Flask test client against an isolated SQLite database; it does not create test consumers in the real billing database.
- Full pytest suite execution is also available in the QA dashboard.

### Phase 3 — Explainable test generation
- Test cases include IDs, technique, scenario, input, expected result, rationale, risk and priority.
- Missing-scenario analysis compares selected tests with a documented scenario catalog.
- A scenario-level comparison with six manually authored baseline scenarios is displayed.
- Risk is rule-based: tariff transitions and invalid inputs are prioritized because they can affect charge correctness or validation.

### Phase 4 — QA analytics
- Actual results from generated tests and pytest runs are stored in a separate SQLite QA history database.
- Dashboard metrics include test runs, cases, passes, failures/errors, defect-demo detections and coverage.
- Coverage trend is recorded for full pytest runs.
- Individual run results can be revisited.
- QA execution history and details can be exported to CSV.
- An intentionally defective function demonstrates detection of an overcharge (kept separate from production billing logic).

## Requirements
- Windows 10/11
- Python 3.11 recommended
- Internet connection for initial package installation and Bootstrap styling (the application itself runs locally)
- Playwright Chromium for browser tests

## Setup in VS Code (PowerShell)

Open the extracted `GridGuardAI_Enhanced` folder in VS Code. Open a terminal in that folder and run:

```powershell
py -3.11 -m venv venv
.\venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
python -m playwright install chromium
```

If PowerShell blocks activation, run this for the current terminal only, then activate again:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\venv\Scripts\Activate.ps1
```

## Run the three applications

Open **three separate VS Code terminals**. Activate the virtual environment in each terminal (`.\venv\Scripts\Activate.ps1`).

**Terminal 1 — Billing application**
```powershell
python app.py
```
Open: `http://127.0.0.1:5000`

**Terminal 2 — Test-case generator**
```powershell
python ai_assistant.py
```
Open: `http://127.0.0.1:5002`

**Terminal 3 — QA dashboard**
```powershell
python qa_dashboard.py
```
Open: `http://127.0.0.1:5001`

The billing app navigation links to the generator and QA dashboard. Keep the billing application running when running browser automation tests.

## Recommended demonstration flow
1. Log in as the demo administrator (`admin` / `GridGuard@123`) for local demonstration.
2. Register a consumer from **Manage consumers**, including a 10-digit phone number, and show the generated consumer ID.
3. Log out and activate a customer account using that consumer ID and matching phone number; show that the customer sees only their own account and bills.
4. Calculate a bill with previous reading `100` and current reading `150`; default tariff should produce 50 units and ₹150.
5. Open the receipt, print it or download its CSV, then show it in Billing History.
6. Open **Tariff settings**, change a rate, save it, and calculate a new bill. Explain that previous bills retain their tariff snapshots.
7. Open the test generator at port 5002, choose all techniques, and discuss the risk and rationale columns, missing scenarios, and manual-baseline overlap.
8. Open the QA dashboard at port 5001 → **Interactive Test Lab** → **Generate and execute suite**. Inspect individual results and explain why test records are isolated from real consumer data.
9. Run the intentional defect demo and show how a failing assertion identifies the ₹200 overcharge.
10. Run the full pytest suite from the dashboard. For browser tests, the billing app must be running and Playwright Chromium must be installed.
11. Export QA history and show the recorded run/coverage history.

## Testing commands

Run non-browser tests:
```powershell
python -m pytest tests -v --ignore=tests/test_browser.py --ignore=tests/test_defect_demo.py
```

Run browser tests (requires the billing server at port 5000 and installed Chromium):
```powershell
python -m pytest tests/test_browser.py -v
```

Run normal suite with statement coverage:
```powershell
python -m coverage run --source=app,ai_assistant -m pytest tests --ignore=tests/test_defect_demo.py
python -m coverage report -m
```

The defect demo is intentionally failing by design; do not include `tests/test_defect_demo.py` in the normal passing suite.

## Data and reset
- `gridguard.db`: consumer records, bills and tariff settings.
- `reports/qa_history.db`: QA run history and per-test results.
- `reports/junit.xml`: latest full pytest XML report.
- These databases are created automatically when the apps start.
- Back up these files before deleting them. To reset demo data, stop the apps and remove the relevant database file; the next start recreates an empty database with the default tariff slabs.

## Project limitations to disclose
- Tariff rules are a simplified academic progressive-tariff example, not an official utility tariff.
- This is still a college prototype. Change demo credentials and the session secret before any deployment; do not expose the development server to the public internet.
- The rule-based generator is explainable test design, not a learned model. Do not claim machine-learning accuracy.
- The manual baseline comparison measures scenario overlap, not whether every test is optimal.
- 100% statement coverage, if achieved for selected modules, does not guarantee absence of defects.
