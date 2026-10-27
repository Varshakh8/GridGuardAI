
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ai_assistant import app, generate_test_cases


def test_boundary_cases_are_generated():
    cases = generate_test_cases("boundary")
    assert len(cases) == 7
    assert any(case["id"] == "BVA-02" for case in cases)


def test_equivalence_cases_are_generated():
    cases = generate_test_cases("equivalence")
    assert len(cases) == 9
    assert any(case["id"] == "EP-03" for case in cases)


def test_decision_table_cases_are_generated():
    cases = generate_test_cases("decision")
    assert len(cases) == 3
    assert any(case["id"] == "DT-03" for case in cases)


def test_all_techniques_are_generated():
    cases = generate_test_cases("all")
    assert len(cases) == 19
    assert len({case["id"] for case in cases}) == 19


def test_assistant_web_page():
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["role"] = "admin"
    response = client.get("/")
    assert response.status_code == 200
    assert b"Explainable AI-Assisted Test Case Generator" in response.data


def test_generate_cases_through_web_form():
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["role"] = "admin"
    response = client.post("/", data={"category": "boundary"})
    assert response.status_code == 200
    assert b"BVA-02" in response.data
    assert b"Boundary Value Analysis" in response.data


def test_default_category_generates_all_cases():
    from ai_assistant import generate_test_cases

    cases = generate_test_cases("all")

    assert len(cases) == 19

def test_invalid_category_defaults_to_all():
    from ai_assistant import app as assistant_app

    assistant_app.config["TESTING"] = True
    with assistant_app.test_client() as client:
        with client.session_transaction() as sess:
            sess["role"] = "admin"
        response = client.post(
            "/",
            data={"category": "invalid_category"}
        )

    assert response.status_code == 200

def test_ai_assistant_startup():
    import runpy
    from unittest.mock import patch

    with patch("flask.Flask.run") as mock_run:
        runpy.run_module("ai_assistant", run_name="__main__")

    mock_run.assert_called_once_with(port=5002, debug=False)
