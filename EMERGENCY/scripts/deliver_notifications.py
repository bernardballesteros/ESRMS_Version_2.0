"""Run the notification delivery worker on its own, outside the Flask server.

    .venv\\Scripts\\python scripts\\deliver_notifications.py          # keep running
    .venv\\Scripts\\python scripts\\deliver_notifications.py --once   # one pass, e.g. from a scheduler

The Flask server already runs this worker in the background (main.py). Use this script
instead when the web server is started by something that should not run background threads;
set NOTIFICATION_WORKER=0 for the web server in that case. Running both is safe (each event
and delivery is claimed by exactly one worker), just unnecessary.
"""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app, delivery  # noqa: E402
from app.supabase_client import get_supabase_admin  # noqa: E402


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    app = create_app()
    print(f"Email: {'configured' if delivery.email_configured(app.config) else 'NOT configured (RESEND_API_KEY, EMAIL_FROM)'}")
    print(f"Push:  {'configured' if delivery.push_configured(app.config) else 'NOT configured (VAPID keys)'}")
    if "--once" in sys.argv:
        with app.app_context():
            print(delivery.process_once(get_supabase_admin(), app.config))
        return
    try:
        delivery.run_forever(app)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
