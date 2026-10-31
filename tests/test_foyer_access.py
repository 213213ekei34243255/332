"""Bearer-token admin sessions and signed media links: what the Foyer dashboard (via its server proxy) uses."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_staff_flow import (admin, apply_as_candidate, client, env, make_position,  # noqa: F401,E402
                             run_face_check)

PW = "correct horse battery"


def bearer(client):
    r = client.post("/staff/api/login", json={"password": PW, "mode": "token"})
    assert r.status_code == 200
    body = r.get_json()
    assert "Set-Cookie" not in r.headers and body["token"] and body["expires_in"] > 0
    return {"Authorization": f"Bearer {body['token']}"}


def interview_with_video(client, admin):
    pos = make_position(admin)
    app_id = apply_as_candidate(client, pos["id"]).get_json()["id"]
    code = admin.post(f"/staff/api/applications/{app_id}/invite", json={}).get_json()["enrollment"]["code"]
    tok = client.post("/api/recruit/enroll/lookup", json={"code": code}).get_json()["token"]
    h = {"Authorization": f"Bearer {tok}"}
    assert run_face_check(client, tok).status_code == 200
    iv = client.post("/api/recruit/interview/start", headers=h).get_json()["interview_id"]
    r = client.post("/api/recruit/interview/video/chunk?segment=0&seq=0", headers=dict(h, **{"Content-Type": "video/webm"}),
                    data=b"\x1a\x45\xdf\xa3" + b"A" * 3000)
    assert r.status_code == 200
    rec = admin.get(f"/staff/api/interviews/{iv}").get_json()["interview"]["recordings"][0]["id"]
    return app_id, iv, rec


def test_bearer_login_rejects_bad_password(client):
    assert client.post("/staff/api/login", json={"password": "nope", "mode": "token"}).status_code == 401


def test_bearer_session_reads_and_writes_without_csrf_header(client):
    h = bearer(client)
    anon = client.application.test_client()
    assert anon.get("/staff/api/positions").status_code == 401
    assert client.get("/staff/api/positions", headers=h).status_code == 200
    from test_staff_flow import POSITION
    assert client.post("/staff/api/positions", json=POSITION, headers=h).status_code == 201
    me = client.get("/staff/api/me", headers=h).get_json()
    assert me["signed_in"] is True


def test_cookie_sessions_still_require_csrf(client):
    client.post("/staff/api/login", json={"password": PW})          # cookie mode
    from test_staff_flow import POSITION
    assert client.post("/staff/api/positions", json=POSITION).status_code == 403


def test_garbage_or_wrong_scope_bearer_is_rejected(client):
    from staff.security import sign_token
    for token in ("abc.def", sign_token({"scope": "enroll", "eid": "x"}, 60), sign_token({"scope": "admin"}, -5)):
        assert client.get("/staff/api/positions", headers={"Authorization": f"Bearer {token}"}).status_code == 401
    # a valid cookie is not a fallback when a bad bearer header is present
    client.post("/staff/api/login", json={"password": PW})
    assert client.get("/staff/api/positions", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_signed_media_urls_are_scoped_to_one_resource_and_kind(env, client, admin):
    app_id, iv, rec = interview_with_video(client, admin)
    h = bearer(client)
    anon = env[0].test_client()

    url = client.post("/staff/api/media-url", json={"type": "recording", "id": rec}, headers=h).get_json()["url"]
    assert url.startswith(f"/staff/api/recordings/{rec}/stream?st=")
    full = anon.get(url)                                              # no cookie, no header: the token is enough
    assert full.status_code == 200 and full.headers["Cache-Control"] == "private, no-store"
    full.close()
    part = anon.get(url, headers={"Range": "bytes=0-3"})
    assert part.status_code == 206 and part.data == b"\x1a\x45\xdf\xa3"
    part.close()

    token = url.split("st=")[1]
    assert anon.get(f"/staff/api/recordings/{rec}/download?st={token}").status_code == 401      # wrong kind
    assert anon.get(f"/staff/api/recordings/rec_other/stream?st={token}").status_code == 401    # wrong resource
    assert anon.get(f"/staff/api/recordings/{rec}/stream?st={token}x").status_code == 401        # tampered
    assert anon.get(f"/staff/api/recordings/{rec}/stream").status_code == 401                    # nothing at all

    from staff.security import sign_token
    expired = sign_token({"scope": "media", "kind": "recording", "sub": rec}, -5)
    assert anon.get(f"/staff/api/recordings/{rec}/stream?st={expired}").status_code == 401
    admin_token = client.post("/staff/api/login", json={"password": PW, "mode": "token"}).get_json()["token"]
    assert anon.get(f"/staff/api/recordings/{rec}/stream?st={admin_token}").status_code == 401   # admin token != media token

    for kind, rid, path in (("photo", iv, f"/staff/api/interviews/{iv}/face.jpg"),
                            ("download", rec, f"/staff/api/recordings/{rec}/download"),
                            ("resume", app_id, f"/staff/api/applications/{app_id}/resume")):
        u = client.post("/staff/api/media-url", json={"type": kind, "id": rid}, headers=h).get_json()["url"]
        assert u.startswith(path + "?st=")
        r = anon.get(u)
        assert r.status_code == 200, (kind, r.status_code)
        r.close()

    assert client.post("/staff/api/media-url", json={"type": "recording", "id": "rec_nope"}, headers=h).status_code == 404
    assert client.post("/staff/api/media-url", json={"type": "bogus", "id": rec}, headers=h).status_code == 404
    assert anon.post("/staff/api/media-url", json={"type": "recording", "id": rec}).status_code == 401
    actions = [a["action"] for a in client.get("/staff/api/audit", headers=h).get_json()["items"]]
    assert "view_recording" in actions


def test_forwarded_client_ip_only_trusted_with_the_proxy_key(client, monkeypatch):
    import staff.config as cfg
    monkeypatch.setenv("STAFF_PROXY_KEY", "s3cret-proxy-key")
    cfg.reload_settings()
    from flask import Flask
    from staff.security import client_ip
    app = Flask("ip")
    with app.test_request_context("/", headers={"X-Staff-Proxy-Key": "s3cret-proxy-key", "X-Staff-Client-IP": "203.0.113.7"},
                                  environ_base={"REMOTE_ADDR": "10.0.0.1"}):
        assert client_ip() == "203.0.113.7"
    with app.test_request_context("/", headers={"X-Staff-Proxy-Key": "wrong", "X-Staff-Client-IP": "203.0.113.7"},
                                  environ_base={"REMOTE_ADDR": "10.0.0.1"}):
        assert client_ip() == "10.0.0.1"
    with app.test_request_context("/", headers={"X-Staff-Proxy-Key": "s3cret-proxy-key", "X-Staff-Client-IP": "not an ip"},
                                  environ_base={"REMOTE_ADDR": "10.0.0.1"}):
        assert client_ip() == "10.0.0.1"
