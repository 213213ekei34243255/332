"""The dashboard must say so loudly when recruitment data lives on a server's temporary disk."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_staff_flow import admin, client, env  # noqa: F401,E402

from staff.config import is_persistent  # noqa: E402


def mounted_at(*mounts):
    return lambda p: p.replace("\\", "/").rstrip("/").endswith(mounts)


def test_only_mounted_volumes_count_as_persistent():
    disk = mounted_at("/var/data")
    assert is_persistent("/var/data/staff", windows=False, ismount=disk) is True       # Render disk at /var/data
    assert is_persistent("/var/data/staff/recordings", windows=False, ismount=disk) is True
    assert is_persistent("/var/data/staff", windows=False, ismount=mounted_at()) is False   # plain container FS
    assert is_persistent("/app/staff_data", windows=False, ismount=disk) is False           # not under the disk
    assert is_persistent("/anything", windows=True) is True                                  # local Windows dev


def test_warnings_explain_both_failure_modes(monkeypatch):
    import staff.config as cfg
    monkeypatch.setattr(cfg, "is_persistent", lambda path, **kw: False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    s = cfg.reload_settings()
    text = " ".join(s.persistence_warnings())
    assert "DATABASE_URL" in text and "persistent disk" in text and len(s.persistence_warnings()) == 2
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@host/db")          # a Postgres DB keeps the records safe
    s = cfg.reload_settings()
    assert len(s.persistence_warnings()) == 1 and "videos" in s.persistence_warnings()[0]
    monkeypatch.setattr(cfg, "is_persistent", lambda path, **kw: True)
    assert cfg.reload_settings().persistence_warnings() == []


def test_system_endpoint_reports_persistence(env, client, admin, monkeypatch):
    out = admin.get("/staff/api/system").get_json()["system"]
    assert out["persistence"]["warnings"] == [] or isinstance(out["persistence"]["warnings"], list)
    assert set(out["persistence"]) == {"database_persistent", "files_persistent", "warnings"}
    import staff.config as cfg
    monkeypatch.setattr(cfg, "is_persistent", lambda path, **kw: False)
    cfg.reload_settings()
    out = admin.get("/staff/api/system").get_json()["system"]
    assert out["persistence"]["files_persistent"] is False and out["persistence"]["warnings"]
