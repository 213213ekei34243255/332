"""Run ONLY the staff recruitment system locally (no torch / llama / Redis needed).

    python staff_dev.py                # http://127.0.0.1:5055/staff
    STAFF_AI_MOCK=1 python staff_dev.py   # fake AI for trying the flow without an AI key

Local defaults: SQLite in ./staff_data, emails logged instead of sent (set
SMTP_* to send for real).
"""
import logging
import os

os.environ.setdefault("STAFF_ADMIN_PASSWORD", "dev-admin-password")
os.environ.setdefault("STAFF_EMAIL_DRY_RUN", "1")

from flask import Flask  # noqa: E402

from staff import init_staff  # noqa: E402

logging.basicConfig(level=logging.INFO)
app = Flask(__name__)
init_staff(app)


@app.get("/")
def home():
    from staff.pages import landing_page
    return landing_page()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5055))
    print(f"Staff dashboard: http://127.0.0.1:{port}/staff  (password: {os.environ['STAFF_ADMIN_PASSWORD'] if os.environ['STAFF_ADMIN_PASSWORD'] == 'dev-admin-password' else '<your STAFF_ADMIN_PASSWORD>'})")
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
