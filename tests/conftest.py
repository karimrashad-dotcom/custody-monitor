import os
import tempfile

import pytest

os.environ.setdefault("CUSTODY_DATA_DIR", tempfile.mkdtemp())

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

@pytest.fixture(scope="session")
def admin():
    c = TestClient(app)
    r = c.post("/setup", data={"full_name": "كريم", "username": "admin", "password": "secret1", "company_name": "KRT"})
    assert r.status_code == 200
    c.post("/admin/users", data={"full_name": "أحمد", "username": "ahmed", "password": "secret1",
                                 "role": "employee", "weekly_amount": "2000", "per_invoice_limit": "500"})
    return c


@pytest.fixture(scope="session")
def emp(admin):
    c = TestClient(app)
    r = c.post("/login", data={"username": "ahmed", "password": "secret1"})
    assert "عهدة أحمد" in r.text
    return c


