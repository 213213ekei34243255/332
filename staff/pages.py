"""HTML pages: /staff (admin), /apply (enrollment) and /join (interview room)."""
import mimetypes

from flask import Blueprint, abort, make_response, send_from_directory

from .config import get_settings
from .security import PAGE_CSP

mimetypes.add_type("application/wasm", ".wasm")
mimetypes.add_type("text/javascript", ".mjs")
mimetypes.add_type("application/octet-stream", ".task")

bp = Blueprint("staff_pages", __name__)


def _page(name):
    resp = make_response(send_from_directory(get_settings().web_dir, name))
    resp.headers["Content-Security-Policy"] = PAGE_CSP
    resp.headers["Cache-Control"] = "no-cache"
    resp.headers["X-Robots-Tag"] = "noindex, nofollow"
    return resp


def landing_page():
    """The public front door ('/'): apply for an appointment or enter an interview code."""
    return _page("landing.html")


@bp.get("/staff", strict_slashes=False)
def staff_dashboard():
    return _page("admin.html")


@bp.get("/apply", strict_slashes=False)
def apply_page():
    return _page("apply.html")


@bp.get("/join", strict_slashes=False)
def join_page():
    return _page("join.html")


@bp.get("/staff-static/<path:filename>")
def assets(filename):
    root = get_settings().web_dir
    if filename.startswith(".") or "/." in filename or filename.endswith((".html", ".py")):
        abort(404)
    resp = make_response(send_from_directory(root, filename))
    if filename.startswith("vendor/"):
        resp.headers["Cache-Control"] = "public, max-age=604800, immutable"
    else:
        resp.headers["Cache-Control"] = "no-cache"
    return resp
