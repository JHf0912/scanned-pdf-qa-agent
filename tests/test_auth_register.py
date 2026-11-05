"""开放自助注册接口的回归测试。"""

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


class TestRegister:
    def test_register_creates_user_and_logs_in(self, app):
        c = TestClient(app)
        r = c.post("/api/auth/register", json={"username": "carol", "pw": "CarolPass1"})
        assert r.status_code == 200
        assert r.json() == {"authenticated": True, "username": "carol", "role": "user"}
        # 已带 cookie，可直接访问受保护接口
        assert c.get("/api/status").status_code == 200
        # 新用户不能访问管理接口
        assert c.get("/api/admin/users").status_code == 403

    def test_registered_user_can_relogin(self, app):
        TestClient(app).post("/api/auth/register", json={"username": "carol", "pw": "CarolPass1"})
        r = TestClient(app).post("/api/auth/login", json={"username": "carol", "pw": "CarolPass1"})
        assert r.status_code == 200
        assert r.json()["role"] == "user"

    def test_duplicate_username_rejected(self, app):
        TestClient(app).post("/api/auth/register", json={"username": "carol", "pw": "CarolPass1"})
        r = TestClient(app).post("/api/auth/register", json={"username": "carol", "pw": "CarolPass1"})
        assert r.status_code == 400

    def test_short_pw_rejected(self, app):
        r = TestClient(app).post("/api/auth/register", json={"username": "dave", "pw": "short"})
        assert r.status_code == 400
