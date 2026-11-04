import time

import pytest
from fastapi.testclient import TestClient

from application import auth, server


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("SESSION_SIGNING_KEY", "test-key")
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
    monkeypatch.setenv("ADMIN_EMAILS", "e2npark@gmail.com")
    monkeypatch.setattr(auth, "_key", None)
    monkeypatch.setattr(server.catalog, "start_sync", lambda force=False: False)


def login(client, email, monkeypatch):
    monkeypatch.setattr(auth, "verify_google_credential",
                        lambda c: {"email": email, "name": "N", "picture": ""})
    return client.post("/api/auth/google", json={"credential": "x"})


def test_admin_and_user_roles(monkeypatch):
    c = TestClient(server.app)
    assert c.get("/api/me").status_code == 401
    assert login(c, "E2NPark@gmail.com", monkeypatch).json()["role"] == "admin"
    assert c.get("/api/admin/ping").status_code == 200
    c2 = TestClient(server.app)
    assert login(c2, "someone@example.com", monkeypatch).json()["role"] == "user"
    assert c2.get("/api/admin/ping").status_code == 403


def test_invalid_google_credential_rejected():
    c = TestClient(server.app)
    assert c.post("/api/auth/google", json={"credential": "garbage"}).status_code == 401


def test_tampered_and_expired_session():
    good = auth.make_session("a@b.com", "A", "")
    head, payload, sig = good.split(".")
    with pytest.raises(auth.AuthError):
        auth.read_session(f"{head}.{payload}.{sig[:-2]}xx")
    with pytest.raises(auth.AuthError):
        auth.read_session("v1." + auth._b64e(b'{"e":"e2npark@gmail.com","x":9999999999}') + "." + sig)
    import os
    os.environ["SESSION_MAX_AGE_SECONDS"] = "1"
    old = auth.make_session("a@b.com", "A", "")
    time.sleep(2)
    with pytest.raises(auth.AuthError):
        auth.read_session(old)
    del os.environ["SESSION_MAX_AGE_SECONDS"]


def test_logout_clears(monkeypatch):
    c = TestClient(server.app)
    login(c, "a@b.com", monkeypatch)
    c.post("/api/auth/logout")
    assert c.get("/api/me").status_code == 401
