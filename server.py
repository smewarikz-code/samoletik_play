import hashlib
import hmac
import json
import os
import time
from urllib.parse import parse_qsl

import psycopg
from flask import Flask, jsonify, request
from flask_cors import CORS
from psycopg.rows import dict_row

app = Flask(__name__)

# The game is hosted separately (for example, on GitHub Pages).
# Set GAME_ORIGIN in Render to your exact game origin for tighter CORS.
allowed_origin = os.environ.get("GAME_ORIGIN", "*")
CORS(
    app,
    resources={r"/api/*": {"origins": allowed_origin}},
    methods=["GET", "POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)

MAX_SCORE = 10_000_000
TELEGRAM_AUTH_MAX_AGE = 24 * 60 * 60


def database_url():
    url = os.environ.get("DATABASE_URL", "").strip()
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


def get_connection():
    url = database_url()
    if not url:
        raise RuntimeError("DATABASE_URL is not configured")
    return psycopg.connect(url, row_factory=dict_row, connect_timeout=8)


def ensure_schema(conn):
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS players (
            telegram_id BIGINT PRIMARY KEY,
            display_name TEXT NOT NULL,
            username TEXT,
            best_score INTEGER NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
        """
    )


def verify_telegram_init_data(init_data):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    if not init_data or len(init_data) > 10_000:
        return None

    try:
        pairs = parse_qsl(init_data, keep_blank_values=True, strict_parsing=True)
        data = dict(pairs)
        received_hash = data.pop("hash", None)
        if not received_hash:
            return None

        data_check_string = "\n".join(
            f"{key}={value}" for key, value in sorted(data.items())
        )
        secret_key = hmac.new(
            b"WebAppData", token.encode("utf-8"), hashlib.sha256
        ).digest()
        calculated_hash = hmac.new(
            secret_key, data_check_string.encode("utf-8"), hashlib.sha256
        ).hexdigest()

        if not hmac.compare_digest(calculated_hash, received_hash):
            return None

        auth_date = int(data.get("auth_date", "0"))
        now = int(time.time())
        if auth_date > now + 30 or now - auth_date > TELEGRAM_AUTH_MAX_AGE:
            return None

        user = json.loads(data.get("user", "{}"))
        telegram_id = int(user["id"])
        if telegram_id <= 0:
            return None

        first_name = str(user.get("first_name") or "").strip()
        last_name = str(user.get("last_name") or "").strip()
        username = str(user.get("username") or "").strip() or None
        display_name = (first_name + " " + last_name).strip() or (
            "Игрок " + str(telegram_id)
        )
        return {
            "telegram_id": telegram_id,
            "display_name": display_name[:48],
            "username": username[:64] if username else None,
        }
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


@app.get("/")
def index():
    return jsonify({"service": "samoletik-leaderboard", "status": "ok"})


@app.get("/health")
def health():
    try:
        with get_connection() as conn:
            ensure_schema(conn)
        return jsonify({"status": "ok", "database": "connected"})
    except Exception:
        app.logger.exception("Health check failed")
        return jsonify({"status": "error", "database": "unavailable"}), 503


@app.get("/api/leaderboard")
def leaderboard():
    try:
        limit = request.args.get("limit", default=100, type=int)
        limit = max(1, min(limit or 100, 100))
        with get_connection() as conn:
            ensure_schema(conn)
            rows = conn.execute(
                """
                SELECT display_name, username, best_score
                FROM players
                WHERE best_score > 0
                ORDER BY best_score DESC, updated_at ASC
                LIMIT %s
                """,
                (limit,),
            ).fetchall()
        return jsonify({"players": rows})
    except Exception:
        app.logger.exception("Could not load leaderboard")
        return jsonify({"error": "leaderboard_unavailable"}), 503


@app.post("/api/score")
def submit_score():
    payload = request.get_json(silent=True) or {}
    init_data = payload.get("initData", "")
    score = payload.get("score")

    if isinstance(score, bool) or not isinstance(score, int):
        return jsonify({"error": "score_must_be_integer"}), 400
    if score < 0 or score > MAX_SCORE:
        return jsonify({"error": "score_out_of_range"}), 400

    try:
        player = verify_telegram_init_data(init_data)
    except RuntimeError:
        app.logger.exception("Telegram authentication is not configured")
        return jsonify({"error": "server_auth_not_configured"}), 503

    if not player:
        return jsonify({"error": "invalid_telegram_auth"}), 401

    try:
        with get_connection() as conn:
            ensure_schema(conn)
            row = conn.execute(
                """
                INSERT INTO players (telegram_id, display_name, username, best_score)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (telegram_id) DO UPDATE SET
                    display_name = EXCLUDED.display_name,
                    username = EXCLUDED.username,
                    best_score = GREATEST(players.best_score, EXCLUDED.best_score),
                    updated_at = CASE
                        WHEN EXCLUDED.best_score > players.best_score THEN NOW()
                        ELSE players.updated_at
                    END
                RETURNING display_name, best_score
                """,
                (
                    player["telegram_id"],
                    player["display_name"],
                    player["username"],
                    score,
                ),
            ).fetchone()
        return jsonify({"ok": True, "player": row})
    except Exception:
        app.logger.exception("Could not save score")
        return jsonify({"error": "score_save_failed"}), 503


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
