"""Clear stored push subscriptions so notifications can be re-enabled."""
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

from app import SessionLocal, NotificationSubscription  # noqa: E402


def main():
    with SessionLocal() as db:
        subs = db.query(NotificationSubscription).all()
        if not subs:
            print("No active subscriptions found.")
            return
        for sub in subs:
            db.delete(sub)
        db.commit()
        print(f"Cleared {len(subs)} subscription(s).")


if __name__ == "__main__":
    main()
