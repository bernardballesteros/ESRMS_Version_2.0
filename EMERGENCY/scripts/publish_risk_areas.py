"""Publish the public-safe High-Risk Areas to the Citizen App's Safety Radar, once.

    .venv\\Scripts\\python scripts\\publish_risk_areas.py           # publish
    .venv\\Scripts\\python scripts\\publish_risk_areas.py --dry-run  # show what would be published

The Flask server already republishes hourly while a dispatcher has the Analytics tab open. Use this
script to publish right after applying migration 012, or from a scheduler when nobody is signed in.
Uses the same analysis as the Analytics tab (app/risk.py), reduced by app/public_risk.py.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import analytics as an  # noqa: E402
from app import create_app, public_risk, risk  # noqa: E402
from app.routes import analytics as routes  # noqa: E402
from app.supabase_client import get_supabase_admin  # noqa: E402


def main():
    app = create_app()
    with app.app_context():
        db = get_supabase_admin()
        if "--dry-run" in sys.argv:
            period = an.resolve_period(public_risk.PUBLIC_RANGE)
            bases = routes._organization_bases(db)
            settings = risk.settings_from(app.config)
            result = risk.analyze(routes._window_rows(db, period, routes.RISK_COLUMNS), period, bases, settings)
            print(json.dumps(public_risk.payload(result, period, bases, settings,
                                                 app.config["RISK_PUBLIC_MIN_INCIDENTS"]), indent=2))
            return
        if not app.config["RISK_PUBLIC_ENABLED"]:
            sys.exit("Publishing is switched off (RISK_PUBLIC_ENABLED=0).")
        print(routes.publish_public_risk(db, app.config))


if __name__ == "__main__":
    main()
