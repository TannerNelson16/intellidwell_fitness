"""Send a manual push notification to the first saved subscription."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENV_FILE = ROOT / ".env.vapid"

if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        if "=" in line and not line.strip().startswith("#"):
            key, value = line.strip().split("=", 1)
            os.environ.setdefault(key, value)

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import SessionLocal, NotificationSubscription, send_push_message  # noqa: E402


def main():
    with SessionLocal() as db:
        subs = db.query(NotificationSubscription).all()
        if not subs:
            print("No push subscriptions found. Enable notifications in the UI first.")
            return
        for sub in subs:
            send_push_message(sub, "Test Push", "Notifications are live!")
        print(f"Queued {len(subs)} notification(s).")


if __name__ == "__main__":
    main()
