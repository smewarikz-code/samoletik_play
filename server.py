import os
import uuid

import psycopg
from flask import Flask, jsonify, request
from flask_cors import CORS
from psycopg.rows import dict_row

app = Flask(__name__)
allowed_origin = os.environ.get("GAME_ORIGIN", "*")
CORS(app, resources={r"/api/*": {"origins": allowed_origin}},
     methods=["GET", "POST", "OPTIONS"], allow_headers=["Content-Type"])

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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS players (
            telegram_id BIGINT PRIMARY KEY,
            display_name TEXT NOT NULL,
            username TEXT,
            best_score INTEGER NOT NULL DEFAULT 0,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS online_sessions (
            session_id UUID PRIMARY KEY,
            last_seen TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)


@app.post("/api/presence")
def update_presence():
    payload = request.get_json(silent=True) or {}
    try:
        session_id = str(uuid.UUID(str(payload.get("sessionId", ""))))
    except (ValueError, TypeError, AttributeError):
        return jsonify({"error": "invalid_session_id"}), 400
    try:
        with get_connection() as conn:
            ensure_schema(conn)
            conn.execute("""
                INSERT INTO online_sessions (session_id, last_seen)
                VALUES (%s, NOW())
                ON CONFLICT (session_id) DO UPDATE SET last_seen = NOW()
            """, (session_id,))
            # Remove abandoned sessions periodically; online count uses a shorter 90-second window.
            conn.execute("DELETE FROM online_sessions WHERE last_seen < NOW() - INTERVAL '10 minutes'")
            row = conn.execute("""
                SELECT COUNT(*) AS online
                FROM online_sessions
                WHERE last_seen > NOW() - INTERVAL '90 seconds'
            """).fetchone()
        return jsonify({"ok": True, "online": int(row["online"]) if row else 0})
    except Exception:
        app.logger.exception("Could not update online presence")
        return jsonify({"error": "presence_unavailable"}), 503


@app.get("/api/online")
def online_count():
    try:
        with get_connection() as conn:
            ensure_schema(conn)
            row = conn.execute("""
                SELECT COUNT(*) AS online
                FROM online_sessions
                WHERE last_seen > NOW() - INTERVAL '90 seconds'
            """).fetchone()
        return jsonify({"online": int(row["online"]) if row else 0})
    except Exception:
        app.logger.exception("Could not load online count")
        return jsonify({"error": "online_count_unavailable"}), 503


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
            rows = conn.execute("""
                SELECT telegram_id, NULL::uuid AS player_id, display_name, username, best_score
                FROM players WHERE best_score > 0
                UNION ALL
                SELECT NULL::bigint AS telegram_id, player_id, display_name, NULL::text AS username, best_score
                FROM ru_players WHERE best_score > 0
                ORDER BY best_score DESC
                LIMIT %s
            """, (limit,)).fetchall()
        return jsonify({"players": rows})
    except Exception:
        app.logger.exception("Could not load leaderboard")
        return jsonify({"error": "leaderboard_unavailable"}), 503


@app.post("/api/score")
def submit_score():
    payload = request.get_json(silent=True) or {}
    score = payload.get("score")
    if isinstance(score, bool) or not isinstance(score, int):
        return jsonify({"error": "score_must_be_integer"}), 400
    if score < 0 or score > MAX_SCORE:
        return jsonify({"error": "score_out_of_range"}), 400

    # RuStore/native clients use a persistent installation UUID; Telegram auth is not required.
    try:
        player_id = str(uuid.UUID(str(payload.get("playerId", ""))))
    except (ValueError, TypeError, AttributeError):
        return jsonify({"error": "invalid_player_id"}), 400
    display_name = str(payload.get("displayName") or "Игрок").strip()
    display_name = "".join(ch for ch in display_name if ch.isprintable())[:24] or "Игрок"

    try:
        with get_connection() as conn:
            ensure_schema(conn)
            row = conn.execute("""
                INSERT INTO ru_players (player_id, display_name, best_score)
                VALUES (%s, %s, %s)
                ON CONFLICT (player_id) DO UPDATE SET
                    display_name = EXCLUDED.display_name,
                    best_score = GREATEST(ru_players.best_score, EXCLUDED.best_score),
                    updated_at = CASE WHEN EXCLUDED.best_score > ru_players.best_score
                                      THEN NOW() ELSE ru_players.updated_at END
                RETURNING player_id, display_name, best_score
            """, (player_id, display_name, score)).fetchone()
        return jsonify({"ok": True, "player": row})
    except Exception:
        app.logger.exception("Could not save score")
        return jsonify({"error": "score_save_failed"}), 503




if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
