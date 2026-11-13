import hashlib
import hmac
import json
import os
import time
import uuid
from urllib.parse import parse_qsl
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

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
STAR_PACKS = {
    "pack_50": {"game_stars": 50, "price_xtr": 50},
    "pack_150": {"game_stars": 150, "price_xtr": 100},
    "pack_250": {"game_stars": 250, "price_xtr": 200},
}


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
    conn.execute("ALTER TABLE players ADD COLUMN IF NOT EXISTS stars_balance BIGINT NOT NULL DEFAULT 0")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS star_invoices (
            payload TEXT PRIMARY KEY, telegram_id BIGINT NOT NULL,
            game_stars INTEGER NOT NULL, price_xtr INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS star_payments (
            telegram_payment_charge_id TEXT PRIMARY KEY,
            payload TEXT NOT NULL UNIQUE, telegram_id BIGINT NOT NULL,
            game_stars INTEGER NOT NULL, total_amount INTEGER NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS star_recoveries (
            payload TEXT PRIMARY KEY,
            telegram_id BIGINT NOT NULL,
            game_stars INTEGER NOT NULL,
            recovered_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)


def verify_telegram_init_data(init_data):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    if not init_data or len(init_data) > 10_000:
        return None
    try:
        data = dict(parse_qsl(init_data, keep_blank_values=True, strict_parsing=True))
        received_hash = data.pop("hash", None)
        if not received_hash:
            return None
        data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(data.items()))
        secret_key = hmac.new(b"WebAppData", token.encode("utf-8"), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode("utf-8"), hashlib.sha256).hexdigest()
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
        display_name = (first_name + " " + last_name).strip() or ("Игрок " + str(telegram_id))
        return {"telegram_id": telegram_id, "display_name": display_name[:48],
                "username": username[:64] if username else None}
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def telegram_api(method, payload):
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
    req = Request(f"https://api.telegram.org/bot{token}/{method}",
                  data=json.dumps(payload).encode("utf-8"),
                  headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urlopen(req, timeout=12) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (URLError, HTTPError, TimeoutError) as exc:
        raise RuntimeError(f"Telegram API request failed: {exc}") from exc
    if not result.get("ok"):
        raise RuntimeError(f"Telegram API error: {result}")
    return result.get("result")


def authenticated_player():
    payload = request.get_json(silent=True) or {}
    try:
        player = verify_telegram_init_data(payload.get("initData", ""))
    except RuntimeError:
        return None, (jsonify({"error": "server_auth_not_configured"}), 503)
    if not player:
        return None, (jsonify({"error": "invalid_telegram_auth"}), 401)
    return player, None


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
            rows = conn.execute("""
                SELECT display_name, username, best_score FROM players
                WHERE best_score > 0 ORDER BY best_score DESC, updated_at ASC LIMIT %s
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
    try:
        player = verify_telegram_init_data(payload.get("initData", ""))
    except RuntimeError:
        app.logger.exception("Telegram authentication is not configured")
        return jsonify({"error": "server_auth_not_configured"}), 503
    if not player:
        return jsonify({"error": "invalid_telegram_auth"}), 401
    try:
        with get_connection() as conn:
            ensure_schema(conn)
            row = conn.execute("""
                INSERT INTO players (telegram_id, display_name, username, best_score)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (telegram_id) DO UPDATE SET
                    display_name = EXCLUDED.display_name, username = EXCLUDED.username,
                    best_score = GREATEST(players.best_score, EXCLUDED.best_score),
                    updated_at = CASE WHEN EXCLUDED.best_score > players.best_score
                                      THEN NOW() ELSE players.updated_at END
                RETURNING display_name, best_score
            """, (player["telegram_id"], player["display_name"], player["username"], score)).fetchone()
        return jsonify({"ok": True, "player": row})
    except Exception:
        app.logger.exception("Could not save score")
        return jsonify({"error": "score_save_failed"}), 503


@app.post("/api/stars/invoice")
def create_stars_invoice():
    player, error = authenticated_player()
    if error:
        return error
    payload = request.get_json(silent=True) or {}
    pack = STAR_PACKS.get(str(payload.get("packId", "")))
    if not pack:
        return jsonify({"error": "unknown_star_pack"}), 400
    invoice_payload = "samoletik:" + uuid.uuid4().hex
    try:
        with get_connection() as conn:
            ensure_schema(conn)
            conn.execute("""
                INSERT INTO players (telegram_id, display_name, username, best_score)
                VALUES (%s, %s, %s, 0)
                ON CONFLICT (telegram_id) DO UPDATE SET
                    display_name = EXCLUDED.display_name, username = EXCLUDED.username
            """, (player["telegram_id"], player["display_name"], player["username"]))
            conn.execute("""
                INSERT INTO star_invoices (payload, telegram_id, game_stars, price_xtr)
                VALUES (%s, %s, %s, %s)
            """, (invoice_payload, player["telegram_id"], pack["game_stars"], pack["price_xtr"]))
        link = telegram_api("createInvoiceLink", {
            "title": f'{pack["game_stars"]} игровых звёзд',
            "description": f'Пополнение баланса игры «Самолётик» на {pack["game_stars"]} звёзд',
            "payload": invoice_payload, "provider_token": "", "currency": "XTR",
            "prices": [{"label": f'{pack["game_stars"]} игровых звёзд', "amount": pack["price_xtr"]}],
        })
        return jsonify({"ok": True, "invoiceLink": link})
    except Exception:
        app.logger.exception("Could not create Telegram Stars invoice")
        try:
            with get_connection() as conn:
                conn.execute("DELETE FROM star_invoices WHERE payload = %s AND status = 'pending'", (invoice_payload,))
        except Exception:
            app.logger.exception("Could not remove failed invoice")
        return jsonify({"error": "invoice_creation_failed"}), 503


@app.post("/api/stars/claim")
def claim_stars():
    player, error = authenticated_player()
    if error:
        return error
    try:
        with get_connection() as conn:
            ensure_schema(conn)
            recovered = conn.execute("""
                INSERT INTO star_recoveries (payload, telegram_id, game_stars)
                SELECT p.payload, p.telegram_id, p.game_stars
                FROM star_payments p
                WHERE p.telegram_id = %s
                ON CONFLICT (payload) DO NOTHING
                RETURNING game_stars
            """, (player["telegram_id"],)).fetchall()
            recovered_total = sum(int(item["game_stars"]) for item in recovered)
            if recovered_total:
                conn.execute("""
                    UPDATE players SET stars_balance = stars_balance + %s
                    WHERE telegram_id = %s
                """, (recovered_total, player["telegram_id"]))
            row = conn.execute("""
                WITH amount AS (
                    SELECT stars_balance FROM players WHERE telegram_id = %s FOR UPDATE
                )
                UPDATE players SET stars_balance = 0
                WHERE telegram_id = %s
                RETURNING (SELECT stars_balance FROM amount) AS claimed_stars
            """, (player["telegram_id"], player["telegram_id"])).fetchone()
        return jsonify({
            "ok": True,
            "stars": int(row["claimed_stars"]) if row and row["claimed_stars"] else 0,
            "recovered": recovered_total,
        })
    except Exception:
        app.logger.exception("Could not claim purchased stars")
        return jsonify({"error": "stars_claim_failed"}), 503


@app.post("/api/stars/status")
def stars_status():
    player, error = authenticated_player()
    if error:
        return error
    try:
        with get_connection() as conn:
            ensure_schema(conn)
            row = conn.execute("""
                SELECT stars_balance FROM players WHERE telegram_id = %s
            """, (player["telegram_id"],)).fetchone()
            invoices = conn.execute("""
                SELECT i.created_at, i.game_stars, i.price_xtr, i.status,
                       EXISTS (
                           SELECT 1 FROM star_payments p WHERE p.payload = i.payload
                       ) AS payment_recorded
                FROM star_invoices i
                WHERE i.telegram_id = %s
                ORDER BY i.created_at DESC
                LIMIT 5
            """, (player["telegram_id"],)).fetchall()
        return jsonify({
            "ok": True,
            "server_balance": int(row["stars_balance"]) if row else 0,
            "recent_invoices": [
                {
                    "created_at": item["created_at"].isoformat(),
                    "game_stars": item["game_stars"],
                    "price_xtr": item["price_xtr"],
                    "status": item["status"],
                    "payment_recorded": item["payment_recorded"],
                } for item in invoices
            ],
        })
    except Exception:
        app.logger.exception("Could not load star payment status")
        return jsonify({"error": "stars_status_unavailable"}), 503


@app.post("/api/telegram/webhook")
def telegram_webhook():
    expected_secret = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "").strip()
    received_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not expected_secret or not hmac.compare_digest(expected_secret, received_secret):
        return jsonify({"ok": False}), 403
    update = request.get_json(silent=True) or {}
    query = update.get("pre_checkout_query")
    if query:
        query_id = query.get("id", "")
        invoice_payload = query.get("invoice_payload", "")
        user_id = (query.get("from") or {}).get("id")
        try:
            with get_connection() as conn:
                ensure_schema(conn)
                invoice = conn.execute("""
                    SELECT telegram_id FROM star_invoices
                    WHERE payload = %s AND status = 'pending'
                """, (invoice_payload,)).fetchone()
                ok = bool(invoice and int(invoice["telegram_id"]) == int(user_id))
            telegram_api("answerPreCheckoutQuery", {
                "pre_checkout_query_id": query_id, "ok": ok,
                **({} if ok else {"error_message": "Счёт недействителен. Создайте новый."})
            })
        except Exception:
            app.logger.exception("Could not validate pre-checkout query")
            try:
                telegram_api("answerPreCheckoutQuery", {
                    "pre_checkout_query_id": query_id, "ok": False,
                    "error_message": "Не удалось проверить платёж. Попробуйте позже."
                })
            except Exception:
                app.logger.exception("Could not answer pre-checkout query")
        return jsonify({"ok": True})

    message = update.get("message") or update.get("edited_message") or {}
    payment = message.get("successful_payment")
    if payment:
        user_id = (message.get("from") or {}).get("id")
        invoice_payload = payment.get("invoice_payload", "")
        charge_id = payment.get("telegram_payment_charge_id", "")
        total_amount = payment.get("total_amount")
        currency = payment.get("currency")
        try:
            with get_connection() as conn:
                ensure_schema(conn)
                invoice = conn.execute("""
                    SELECT telegram_id, game_stars, price_xtr, status
                    FROM star_invoices WHERE payload = %s FOR UPDATE
                """, (invoice_payload,)).fetchone()
                if (invoice and invoice["status"] == "pending"
                        and int(invoice["telegram_id"]) == int(user_id)
                        and currency == "XTR" and int(total_amount) == int(invoice["price_xtr"])):
                    inserted = conn.execute("""
                        INSERT INTO star_payments
                        (telegram_payment_charge_id, payload, telegram_id, game_stars, total_amount)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT DO NOTHING RETURNING telegram_payment_charge_id
                    """, (charge_id, invoice_payload, user_id, invoice["game_stars"], total_amount)).fetchone()
                    if inserted:
                        conn.execute("UPDATE players SET stars_balance = stars_balance + %s WHERE telegram_id = %s",
                                     (invoice["game_stars"], user_id))
                        conn.execute("UPDATE star_invoices SET status = 'paid' WHERE payload = %s", (invoice_payload,))
                else:
                    app.logger.error("Payment did not match a pending invoice: %s", invoice_payload)
        except Exception:
            app.logger.exception("Could not credit successful Telegram payment")
            return jsonify({"ok": False}), 500
    return jsonify({"ok": True})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "10000")))
