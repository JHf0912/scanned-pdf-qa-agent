"""自助修改密码接口的回归测试。"""

from __future__ import annotations

import dataclasses

import pytest
from fastapi.testclient import TestClient

from docqa.api.app import create_app
from docqa.config import load_config

ADMIN_PW = "AdminPass123"


@pytest.fixture(autouse=True)
def _auth_on(monkeypatch):
    import docqa.api.app as _api_app

    monkeypatch.setattr(_api_app, "AUTH_DISABLED", False)


def _cfg(tmp_path):
    return dataclasses.replace(load_config(), cache_dir=tmp_path / "derived" / "cache")


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCQA_ADMIN_PASSWORD", ADMIN_PW)
    return create_app(_cfg(tmp_path), domain="standard")


@pytest.fixture
def admin(app):
    c = TestClient(app)
    assert c.post("/api/auth/login", json={"username": "admin", "pw": ADMIN_PW}).status_code == 200
    return c


class TestChangePassword:
    def test_user_can_change_own_pw(self, app, admin):
        admin.post("/api/admin/users", json={"username": "bob", "pw": "BobPass123", "role": "user"})
        bob = TestClient(app)
        assert bob.post("/api/auth/login", json={"username": "bob", "pw": "BobPass123"}).status_code == 200
        r = bob.post("/api/auth/change-pw", json={"old_pw": "BobPass123", "new_pw": "NewPass456"})
        assert r.status_code == 200
        assert TestClient(app).post(
            "/api/auth/login", json={"username": "bob", "pw": "NewPass456"}
        ).status_code == 200
        assert TestClient(app).post(
            "/api/auth/login", json={"username": "bob", "pw": "BobPass123"}
        ).status_code == 401

    def test_wrong_old_rejected(self, admin):
        r = admin.post("/api/auth/change-pw", json={"old_pw": "wrongold", "new_pw": "NewPass456"})
        assert r.status_code == 400

    def test_short_new_rejected(self, admin):
        r = admin.post("/api/auth/change-pw", json={"old_pw": ADMIN_PW, "new_pw": "short"})
        assert r.status_code == 400
