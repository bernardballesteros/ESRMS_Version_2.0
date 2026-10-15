"""Insert a TEST emergency report the way the Citizen Emergency App will, for dashboard testing.

These rows are test data only (description starts with "[TEST]"); the dashboard
never ships with built-in incidents.

    .venv\\Scripts\\python scripts\\simulate_incident.py                     # report near Davao City Hall
    .venv\\Scripts\\python scripts\\simulate_incident.py --lat 7.0644 --lng 125.6078 --type Fire --priority 1
    .venv\\Scripts\\python scripts\\simulate_incident.py --no-location        # report without GPS (Test E)
    .venv\\Scripts\\python scripts\\simulate_incident.py --cleanup            # delete all [TEST] reports
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app  # noqa: E402
from app.supabase_client import get_supabase_admin  # noqa: E402


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--type", default="Medical Emergency")
    p.add_argument("--description", default="Elderly man collapsed, not responsive.")
    p.add_argument("--lat", type=float, default=7.07320)
    p.add_argument("--lng", type=float, default=125.61290)
    p.add_argument("--accuracy", type=float, default=12)
    p.add_argument("--priority", type=int, choices=[1, 2, 3, 4])
    p.add_argument("--verified", action="store_true")
    p.add_argument("--no-location", action="store_true")
    p.add_argument("--name", default="Test Citizen")
    p.add_argument("--phone", default="+63 900 000 0000")
    p.add_argument("--cleanup", action="store_true")
    args = p.parse_args()

    app = create_app()
    with app.app_context():
        db = get_supabase_admin()
        if args.cleanup:
            gone = db.table("emergency_incidents").delete().like("description", "[TEST]%").execute().data
            print(f"Deleted {len(gone)} test report(s).")
            return
        row = {
            "emergency_type": args.type,
            "description": f"[TEST] {args.description}",
            "latitude": None if args.no_location else args.lat,
            "longitude": None if args.no_location else args.lng,
            "location_accuracy_m": None if args.no_location else args.accuracy,
            "location_verified": args.verified,
            "priority": args.priority,
            "citizen_name": args.name,
            "contact_number": args.phone,
        }
        created = db.table("emergency_incidents").insert(row).execute().data[0]
        print(f"Inserted INC-{created['incident_number']:06d} ({created['emergency_type']}) "
              f"at {created['latitude']}, {created['longitude']}")


if __name__ == "__main__":
    main()
