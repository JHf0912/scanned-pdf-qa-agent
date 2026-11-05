"""登录、两级权限与按用户隔离的回归测试。"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from docqa.api.app import create_app
from docqa.config import load_config

ADMIN_PW = "AdminPass123"


@pytest.fixture(autouse=True)
def _auth_enabled(monkeypatch):
    """本模块要测**真实鉴权**：关掉 conftest 为历史用例开的 AUTH_DISABLED 旁路。"""
    import docqa.api.app as _api_app

    monkeypatch.setattr(_api_app, "AUTH_DISABLED", False)


def _cfg(tmp_path: Path):
    return dataclasses.replace(load_config(), cache_dir=tmp_path / "derived" / "cache")


def _artifact(tmp_path: Path, doc_id: str, owner: str | None) -> None:
    out = tmp_path / "derived" / doc_id
    out.mkdir(parents=True, exist_ok=True)
    (out / "manifest.json").write_text(
        json.dumps(
            {"doc_id": doc_id, "source": {"path": "x.pdf", "sha256": "s", "owner": owner},
             "render": {"images": []}, "ocr": {"pages_failed": []},
             "chunk": {"chunks": 1, "citation_mode": "clause"}},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (out / "profile.json").write_text(
        json.dumps({"path": "x.pdf", "sha256": "s", "page_count": 1, "pages": [], "kind": "TEXT"}),
        encoding="utf-8",
    )
    (out / "ocr.json").write_text("[]", encoding="utf-8")
    (out / "chunks.jsonl").write_text(
        json.dumps({"chunk_id": f"{doc_id}:p1:c1", "doc_id": doc_id, "page_no": 1,
                    "ordinal": 1, "kind": "clause", "text": "正文",
                    "clause_path": [], "clause_label": "1", "block_ids": []},
                   ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv("DOCQA_ADMIN_PASSWORD", ADMIN_PW)
    return create_app(_cfg(tmp_path), domain="standard")


@pytest.fixture
def admin(app):
    c = TestClient(app)
    assert c.post("/api/auth/login", json={"username": "admin", "pw": ADMIN_PW}).status_code == 200
    return c


class TestLogin:
    def test_anonymous_is_401(self, app):
        assert TestClient(app).get("/api/status").status_code == 401

    def test_login_success_and_me(self, admin):
        r = admin.get("/api/auth/me").json()
        assert r == {"authenticated": True, "username": "admin", "role": "admin"}

    def test_bad_pw_is_401(self, app):
        assert TestClient(app).post(
            "/api/auth/login", json={"username": "admin", "pw": "wrong-pass"}
        ).status_code == 401

    def test_logout_clears_session(self, admin):
        assert admin.post("/api/auth/logout").status_code == 200
        assert admin.get("/api/status").status_code == 401


class TestUserManagement:
    def test_admin_creates_user_and_user_cannot_admin(self, app, admin):
        r = admin.post("/api/admin/users", json={"username": "bob", "pw": "BobPass123", "role": "user"})
        assert r.status_code == 200
        assert {u["username"] for u in admin.get("/api/admin/users").json()["users"]} == {"admin", "bob"}

        bob = TestClient(app)
        assert bob.post("/api/auth/login", json={"username": "bob", "pw": "BobPass123"}).status_code == 200
        assert bob.get("/api/admin/users").status_code == 403

    def test_duplicate_username_rejected(self, admin):
        admin.post("/api/admin/users", json={"username": "bob", "pw": "BobPass123", "role": "user"})
        r = admin.post("/api/admin/users", json={"username": "bob", "pw": "BobPass123", "role": "user"})
        assert r.status_code == 400

    def test_cannot_demote_self(self, admin):
        r = admin.patch("/api/admin/users/admin", json={"role": "user"})
        assert r.status_code == 400

    def test_cannot_delete_self(self, admin):
        assert admin.delete("/api/admin/users/admin").status_code == 400

    def test_disable_user_blocks_login(self, app, admin):
        admin.post("/api/admin/users", json={"username": "bob", "pw": "BobPass123", "role": "user"})
        admin.patch("/api/admin/users/bob", json={"disabled": True})
        assert TestClient(app).post(
            "/api/auth/login", json={"username": "bob", "pw": "BobPass123"}
        ).status_code == 401

    def test_reset_pw(self, app, admin):
        admin.post("/api/admin/users", json={"username": "bob", "pw": "BobPass123", "role": "user"})
        admin.patch("/api/admin/users/bob", json={"pw": "NewPass456"})
        assert TestClient(app).post(
            "/api/auth/login", json={"username": "bob", "pw": "NewPass456"}
        ).status_code == 200


class TestDocumentIsolation:
    def test_user_sees_only_own_docs_admin_sees_all(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DOCQA_ADMIN_PASSWORD", ADMIN_PW)
        _artifact(tmp_path, "shared-legacy", owner=None)
        _artifact(tmp_path, "own-bob", owner="bob")
        app = create_app(_cfg(tmp_path), domain="standard")
        admin = TestClient(app)
        admin.post("/api/auth/login", json={"username": "admin", "pw": ADMIN_PW})
        admin.post("/api/admin/users", json={"username": "bob", "pw": "BobPass123", "role": "user"})

        bob = TestClient(app)
        bob.post("/api/auth/login", json={"username": "bob", "pw": "BobPass123"})

        bob_docs = {d["doc_id"] for d in bob.get("/api/docs").json()["docs"]}
        admin_docs = {d["doc_id"] for d in admin.get("/api/docs").json()["docs"]}
        assert bob_docs == {"own-bob"}
        assert admin_docs == {"shared-legacy", "own-bob"}

    def test_user_cannot_select_others_doc(self, tmp_path, monkeypatch):
        monkeypatch.setenv("DOCQA_ADMIN_PASSWORD", ADMIN_PW)
        _artifact(tmp_path, "own-bob", owner="bob")
        _artifact(tmp_path, "own-carol", owner="carol")
        app = create_app(_cfg(tmp_path), domain="standard")
        admin = TestClient(app)
        admin.post("/api/auth/login", json={"username": "admin", "pw": ADMIN_PW})
        admin.post("/api/admin/users", json={"username": "bob", "pw": "BobPass123", "role": "user"})
        bob = TestClient(app)
        bob.post("/api/auth/login", json={"username": "bob", "pw": "BobPass123"})
        r = bob.post("/api/select", json={"doc_ids": ["own-carol"]})
        assert r.status_code == 403
