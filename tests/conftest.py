import pytest
import app as billing_module


@pytest.fixture(autouse=True)
def isolated_billing_database(tmp_path):
    original = billing_module.app.config["DATABASE"]
    billing_module.app.config["DATABASE"] = str(tmp_path / "gridguard_test.db")
    billing_module.init_db()
    yield
    billing_module.app.config["DATABASE"] = original


@pytest.fixture
def client():
    return billing_module.app.test_client()
