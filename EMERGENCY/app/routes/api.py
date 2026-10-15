from flask import Blueprint, jsonify

from app.supabase_client import get_supabase

bp = Blueprint("api", __name__)


@bp.route("/users")
def get_users():
    data = get_supabase().table("users").select("*").execute()
    return jsonify(data.data)
