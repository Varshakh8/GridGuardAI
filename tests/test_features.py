import csv
import io

import app as billing
from ai_assistant import generate_test_cases, find_missing_scenarios, compare_with_manual_baseline


def test_consumer_registration_assigns_unique_id(client):
    response = client.post("/consumers", data={
        "name": "Asha Rao", "phone": "9876543210", "address": "Mysuru"
    })
    assert response.status_code == 200
    assert b"GG-00001" in response.data
    assert b"Asha Rao" in response.data


def test_duplicate_consumer_registration_is_rejected(client):
    client.post("/consumers", data={"name": "Asha Rao"})
    response = client.post("/consumers", data={"name": "Asha Rao"})
    assert b"already registered" in response.data


def test_billing_persists_history_and_downloadable_receipt(client):
    response = client.post("/calculate", data={
        "consumer_name": "Receipt Consumer", "previous_reading": "100", "current_reading": "150"
    })
    assert response.status_code == 200
    assert b"Total: Rs. 150.00" in response.data
    with billing.get_db() as con:
        bill = con.execute("SELECT id, bill_number FROM bills ORDER BY id DESC LIMIT 1").fetchone()
    assert bill is not None
    receipt = client.get(f"/receipt/{bill['id']}")
    assert receipt.status_code == 200
    assert bill["bill_number"].encode() in receipt.data
    download = client.get(f"/receipt/{bill['id']}/download")
    assert download.status_code == 200
    assert download.mimetype == "text/csv"
    assert b"Receipt Consumer" in download.data
    history = client.get("/history")
    assert bill["bill_number"].encode() in history.data


def test_tariff_rates_can_be_edited_and_apply_to_new_bills(client):
    response = client.post("/tariffs", data={
        "slab_units_0": "100", "rate_0": "2",
        "slab_units_1": "100", "rate_1": "4",
        "slab_units_2": "100", "rate_2": "6",
        "slab_units_3": "", "rate_3": "8",
    })
    assert b"Tariff configuration saved" in response.data
    billed = client.post("/calculate", data={
        "consumer_name": "Tariff Consumer", "previous_reading": "0", "current_reading": "10"
    })
    assert b"Total: Rs. 20.00" in billed.data


def test_invalid_tariff_configuration_is_not_saved(client):
    response = client.post("/tariffs", data={
        "slab_units_0": "", "rate_0": "3",
        "slab_units_1": "100", "rate_1": "5",
        "slab_units_2": "100", "rate_2": "7",
        "slab_units_3": "", "rate_3": "8",
    })
    assert b"Only the final slab can be unlimited" in response.data
    assert billing.get_tariffs()[0]["slab_units"] == 100


def test_history_csv_exports_saved_bill(client):
    client.post("/calculate", data={
        "consumer_name": "CSV Consumer", "previous_reading": "0", "current_reading": "10"
    })
    response = client.get("/history/export")
    assert response.status_code == 200
    assert b"Consumer Name" in response.data
    assert b"CSV Consumer" in response.data


def test_generated_cases_include_explanations_and_risk():
    cases = generate_test_cases("all")
    assert len(cases) == 19
    assert all(c["reason"] and c["risk"] in ("High", "Medium", "Low") for c in cases)
    assert all("scenario" in c and "priority" in c for c in cases)


def test_missing_scenario_analysis_identifies_gaps():
    missing = find_missing_scenarios(generate_test_cases("all"))
    names = {item["scenario"] for item in missing}
    assert "custom_tariff" in names
    assert "missing_previous" not in names


def test_generated_cases_compare_to_manual_baseline():
    comparison = compare_with_manual_baseline(generate_test_cases("all"))
    assert comparison["manual_count"] == 6
    assert comparison["overlap_count"] == 6
    assert comparison["overlap_percent"] == 100.0


def test_qa_test_lab_executes_generated_cases_and_saves_history(tmp_path, monkeypatch):
    import qa_dashboard as qa
    monkeypatch.setattr(qa, "QA_DB", tmp_path / "qa_history.db")
    qa.init_qa_db()
    qa_client = qa.app.test_client()
    with qa_client.session_transaction() as sess:
        sess["role"] = "admin"
        sess["user_id"] = 1
        sess["username"] = "admin"
    response = qa_client.post("/run-generated-tests", data={"category": "all"})
    assert response.status_code == 200
    assert b"Execution results" in response.data
    with qa.qa_connect() as con:
        run = con.execute("SELECT total, passed, failed FROM qa_runs ORDER BY id DESC LIMIT 1").fetchone()
        assert run["total"] == 19
        assert run["passed"] == 19
        assert run["failed"] == 0
