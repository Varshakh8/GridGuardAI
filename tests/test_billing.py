
import sys
from pathlib import Path
import pytest
from app import app as flask_app

# Allow Python to import app.py from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import app
@pytest.fixture
def client():
    flask_app.config["TESTING"] = True

    with flask_app.test_client() as test_client:
        with test_client.session_transaction() as sess:
            with flask_app.app_context():
                with __import__('app').get_db() as con:
                    admin = con.execute("SELECT id, username FROM users WHERE role='admin' LIMIT 1").fetchone()
            sess["user_id"] = admin["id"]
            sess["username"] = admin["username"]
            sess["role"] = "admin"
            sess["consumer_pk"] = None
        yield test_client

def setup_module():
    app.config["TESTING"] = True


def submit_readings(previous, current, name="Test Consumer"):
    client = app.test_client()
    with client.session_transaction() as sess:
        with app.app_context():
            with __import__('app').get_db() as con:
                admin = con.execute("SELECT id, username FROM users WHERE role='admin' LIMIT 1").fetchone()
        sess["user_id"] = admin["id"]
        sess["username"] = admin["username"]
        sess["role"] = "admin"
        sess["consumer_pk"] = None
    return client.post(
        "/calculate",
        data={
            "consumer_name": name,
            "previous_reading": str(previous),
            "current_reading": str(current),
        },
    )


def test_normal_billing():
    response = submit_readings(100, 150)
    assert response.status_code == 200
    assert b"Rs. 150.00" in response.data


def test_zero_consumption():
    response = submit_readings(100, 100)
    assert b"Rs. 0.00" in response.data


def test_first_tariff_boundary():
    response = submit_readings(0, 100)
    assert b"Rs. 300.00" in response.data


def test_second_tariff_boundary():
    response = submit_readings(0, 101)
    assert b"Rs. 305.00" in response.data


def test_200_units():
    response = submit_readings(0, 200)
    assert b"Rs. 800.00" in response.data


def test_201_units():
    response = submit_readings(0, 201)
    assert b"Rs. 807.00" in response.data


def test_300_units():
    response = submit_readings(0, 300)
    assert b"Rs. 1500.00" in response.data


def test_301_units():
    response = submit_readings(0, 301)
    assert b"Rs. 1508.00" in response.data


def test_current_reading_less_than_previous():
    response = submit_readings(200, 150)
    assert b"Current reading cannot be less than previous reading." in response.data
    assert b"Total: Rs." not in response.data


def test_negative_reading():
    response = submit_readings(-10, 100)
    assert b"Meter readings cannot be negative." in response.data
    assert b"Total: Rs." not in response.data


def test_missing_consumer_name():
    response = submit_readings(100, 150, name="   ")
    assert b"Consumer name is required." in response.data
    assert b"Total: Rs." not in response.data


def test_missing_previous_reading(client):
    response = client.post(
        "/calculate",
        data={
            "consumer_name": "Test Consumer",
            "previous_reading": "",
            "current_reading": "100"
        }
    )
    assert response.status_code == 200
    assert b"Both meter readings are required." in response.data


def test_missing_current_reading(client):
    response = client.post(
        "/calculate",
        data={
            "consumer_name": "Test Consumer",
            "previous_reading": "50",
            "current_reading": ""
        }
    )
    assert response.status_code == 200
    assert b"Both meter readings are required." in response.data


def test_non_numeric_previous_reading(client):
    response = client.post(
        "/calculate",
        data={
            "consumer_name": "Test Consumer",
            "previous_reading": "abc",
            "current_reading": "100"
        }
    )
    assert response.status_code == 200
    assert b"whole numbers" in response.data.lower()


def test_non_numeric_current_reading(client):
    response = client.post(
        "/calculate",
        data={
            "consumer_name": "Test Consumer",
            "previous_reading": "50",
            "current_reading": "xyz"
        }
    )
    assert response.status_code == 200
    assert b"whole numbers" in response.data.lower()


def test_negative_previous_reading(client):
    response = client.post(
        "/calculate",
        data={
            "consumer_name": "Test Consumer",
            "previous_reading": "-1",
            "current_reading": "100"
        }
    )
    assert response.status_code == 200
    assert b"Meter readings cannot be negative." in response.data


def test_negative_current_reading(client):
    response = client.post(
        "/calculate",
        data={
            "consumer_name": "Test Consumer",
            "previous_reading": "0",
            "current_reading": "-1"
        }
    )
    assert response.status_code == 200
    assert b"Meter readings cannot be negative." in response.data


def test_admin_home_page(client):
    response = client.get("/admin")
    assert response.status_code == 200


def test_app_startup():
    import runpy
    from unittest.mock import patch

    with patch("flask.Flask.run") as mock_run:
        runpy.run_module("app", run_name="__main__")

    mock_run.assert_called_once_with(debug=False)
