from functools import wraps

import folium
from flask import Blueprint, current_app, flash, redirect, render_template, send_from_directory, session, url_for

from app import dispatch as dsp
from app.routes.analytics import client_config as analytics_config
from app.routes.announcements import can_manage, manage_context
from app.routes.dispatch import can_dispatch
from app.routes.hospital import client_options as hospital_config
from app.routes.hospital import is_hospital_staff

bp = Blueprint("main", __name__)

# Default map center (Davao City).
MAP_CENTER = (7.0731, 125.6128)

ESRI_TILES = "https://server.arcgisonline.com/ArcGIS/rest/services/{service}/MapServer/tile/{{z}}/{{y}}/{{x}}"
ESRI_ATTR = "Tiles &copy; Esri &mdash; Source: Esri, Maxar, Earthstar Geographics"


def build_live_map():
    m = folium.Map(
        location=MAP_CENTER,
        zoom_start=12,
        tiles=None,
        width="100%",
        height="100%",
        control_scale=True,
    )
    folium.TileLayer("OpenStreetMap", name="Map").add_to(m)
    # Hybrid: satellite imagery with roads and place names drawn on top.
    hybrid = folium.FeatureGroup(name="Hybrid", overlay=False)
    for service in ("World_Imagery", "Reference/World_Transportation", "Reference/World_Boundaries_and_Places"):
        folium.TileLayer(tiles=ESRI_TILES.format(service=service), attr=ESRI_ATTR, max_zoom=19).add_to(hybrid)
    hybrid.add_to(m)
    folium.LayerControl(position="topright", collapsed=True).add_to(m)
    root = m.get_root()
    root.render()
    # Drop Bootstrap CSS that folium injects; it would restyle the Tailwind dashboard.
    header = "\n".join(
        line for line in root.header.render().splitlines()
        if not ("bootstrap" in line and "stylesheet" in line)
    )
    return {
        "header": header,
        "body": root.html.render(),
        "script": root.script.render(),
        # JS variable folium assigns the Leaflet map to; the dispatch console reuses it.
        "name": m.get_name(),
        "center": MAP_CENTER,
        "zoom": 12,
    }


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if "user" not in session:
            flash("Please sign in to continue.", "error")
            return redirect(url_for("auth.login"))
        return view(*args, **kwargs)

    return wrapped


@bp.route("/")
def index():
    return render_template("portal.html")


@bp.route("/sw.js")
def service_worker():
    """Push service worker. Served from the root so its scope covers the whole site."""
    response = send_from_directory(current_app.static_folder, "js/sw.js", mimetype="text/javascript")
    response.headers["Cache-Control"] = "no-cache"
    return response


@bp.route("/dashboard")
@login_required
def dashboard():
    if session["user"].get("role") == "admin":
        return redirect(url_for("admin.index"))
    if session["user"].get("role") == "organization":
        return render_dispatch_center()
    # Hospital Portal accounts, and accounts a dispatcher linked to a hospital, land on the hospital dashboard.
    if session["user"].get("role") == "hospital" or is_hospital_staff(session["user"]["id"]):
        return redirect(url_for("hospital.dashboard_page"))
    return render_template("dashboard/default.html")


@bp.route("/dispatch")
@login_required
def dispatch_center():
    """Dispatch Center for any dispatch role, including administrators."""
    if not can_dispatch(session["user"]):
        flash("You do not have access to the Dispatch Center.", "error")
        return redirect(url_for("main.dashboard"))
    return render_dispatch_center()


def render_dispatch_center():
    user = session["user"]
    extra = manage_context() if can_manage(user) else {}
    return render_template(
        "dashboard/organization.html",
        live_map=build_live_map(),
        can_dispatch=can_dispatch(user),
        analytics_options=analytics_config(),
        hospital_options=hospital_config(),
        dispatch_options={
            "statuses": dsp.STATUSES,
            "closed": sorted(dsp.CLOSED),
            "priorities": dsp.PRIORITIES,
            "hotlineCategories": dsp.HOTLINE_CATEGORIES,
        },
        **extra,
    )
