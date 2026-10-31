"""Private file storage for interview recordings, face photos and resumes.

Files live under STAFF_DATA_DIR (a Render persistent disk in production) --
never under the Flask `static/` folder and never inside the Foyer app -- and
are only reachable through authenticated admin endpoints.
"""
import re
import shutil
import threading
from pathlib import Path

from .config import get_settings

SAFE_ID = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")
VIDEO_MIMES = {"video/webm": "webm", "video/mp4": "mp4"}
RESUME_MIMES = {
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "text/plain": "txt",
}

_locks = {}
_locks_guard = threading.Lock()


def _lock_for(key):
    with _locks_guard:
        return _locks.setdefault(key, threading.Lock())


def safe_join(base, *parts):
    """Join and make sure the result cannot escape `base` (path traversal)."""
    base = Path(base).resolve()
    for part in parts:
        if not SAFE_ID.match(part.split(".")[0]) and not re.match(r"^[A-Za-z0-9_\-]+\.[A-Za-z0-9]{1,5}$", part):
            raise ValueError("unsafe path component")
    target = base.joinpath(*parts).resolve()
    if base != target and base not in target.parents:
        raise ValueError("path escapes storage root")
    return target


class Storage:
    def __init__(self):
        self.s = get_settings()
        for d in (self.s.recordings_dir, self.s.photos_dir, self.s.resumes_dir):
            d.mkdir(parents=True, exist_ok=True)

    # ---- capacity ------------------------------------------------------
    def free_mb(self):
        try:
            return shutil.disk_usage(self.s.data_dir).free // (1024 * 1024)
        except OSError:
            return -1

    def used_mb(self):
        total = 0
        for root in (self.s.recordings_dir, self.s.photos_dir, self.s.resumes_dir):
            for p in root.rglob("*"):
                try:
                    if p.is_file():
                        total += p.stat().st_size
                except OSError:
                    pass
        return total // (1024 * 1024)

    def has_room(self):
        free = self.free_mb()
        return free < 0 or free >= self.s.min_free_disk_mb

    # ---- recordings ----------------------------------------------------
    def recording_path(self, interview_id, filename):
        return safe_join(self.s.recordings_dir, interview_id, filename)

    def append_chunk(self, interview_id, filename, data, reset=False):
        path = self.recording_path(interview_id, filename)
        path.parent.mkdir(parents=True, exist_ok=True)
        with _lock_for(str(path)):
            with open(path, "wb" if reset else "ab") as fh:
                fh.write(data)
            return path.stat().st_size

    def delete_interview_files(self, interview_id):
        if not SAFE_ID.match(interview_id):
            return
        shutil.rmtree(self.s.recordings_dir / interview_id, ignore_errors=True)

    # ---- face photos ---------------------------------------------------
    def photo_path(self, enrollment_id):
        return safe_join(self.s.photos_dir, f"{enrollment_id}.jpg")

    def save_photo(self, enrollment_id, data):
        path = self.photo_path(enrollment_id)
        path.write_bytes(data)
        return path

    def delete_photo(self, enrollment_id):
        try:
            self.photo_path(enrollment_id).unlink(missing_ok=True)
        except (ValueError, OSError):
            pass

    # ---- resumes -------------------------------------------------------
    def resume_path(self, application_id, ext):
        return safe_join(self.s.resumes_dir, f"{application_id}.{ext}")

    def save_resume(self, application_id, ext, data):
        path = self.resume_path(application_id, ext)
        path.write_bytes(data)
        return path

    def delete_resume(self, application_id, filename):
        try:
            if filename:
                safe_join(self.s.resumes_dir, filename).unlink(missing_ok=True)
        except (ValueError, OSError):
            pass
