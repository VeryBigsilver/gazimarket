from __future__ import annotations

import os
import logging
from logging.handlers import RotatingFileHandler
import re
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from urllib.parse import urlsplit

import yaml
from flask import (
    Flask,
    abort,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.exceptions import HTTPException
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename


BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.yaml"
if not CONFIG_PATH.exists():
    CONFIG_PATH = BASE_DIR / "config.example.yaml"
CONFIG = yaml.safe_load(CONFIG_PATH.read_text())
APP_CONFIG = CONFIG["app"]

app = Flask(__name__)
app.secret_key = os.environ.get("GAZIMARKET_SECRET_KEY", APP_CONFIG["secret_key"])
app.config["DATABASE"] = str(BASE_DIR / APP_CONFIG["database"])
app.config["UPLOAD_FOLDER"] = str(BASE_DIR / APP_CONFIG["upload_folder"])
app.config["MAX_CONTENT_LENGTH"] = int(APP_CONFIG["max_upload_mb"]) * 1024 * 1024
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
MAX_AUTH_FAILURES = 10
AUTH_LOCK_MINUTES = 30
BLOCKED_INPUT_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"<\s*script",
        r"</\s*script",
        r"javascript\s*:",
        r"data\s*:\s*text/html",
        r"\bon[a-z]+\s*=",
    )
]
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_]{3,30}$")
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)
file_handler = RotatingFileHandler(
    LOG_DIR / "security.log",
    maxBytes=1_000_000,
    backupCount=5,
    encoding="utf-8",
)
file_handler.setLevel(logging.INFO)
file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
app.logger.addHandler(file_handler)
app.logger.setLevel(logging.INFO)


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
    return g.db


@app.teardown_appcontext
def close_db(_error: Exception | None = None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def now() -> str:
    return datetime.utcnow().isoformat(timespec="seconds")


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def init_db() -> None:
    Path(app.config["DATABASE"]).parent.mkdir(parents=True, exist_ok=True)
    Path(app.config["UPLOAD_FOLDER"]).mkdir(parents=True, exist_ok=True)
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS users (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          username TEXT NOT NULL UNIQUE,
          password_hash TEXT NOT NULL,
          name TEXT NOT NULL,
          email TEXT NOT NULL,
          bio TEXT NOT NULL DEFAULT '',
          points INTEGER NOT NULL DEFAULT 100000,
          role TEXT NOT NULL DEFAULT 'user',
          status TEXT NOT NULL DEFAULT 'active',
          created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS products (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          seller_id INTEGER NOT NULL,
          title TEXT NOT NULL,
          price INTEGER NOT NULL,
          description TEXT NOT NULL,
          image_path TEXT,
          status TEXT NOT NULL DEFAULT 'selling',
          blocked INTEGER NOT NULL DEFAULT 0,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          FOREIGN KEY (seller_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS transactions (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          product_id INTEGER NOT NULL,
          buyer_id INTEGER NOT NULL,
          seller_id INTEGER NOT NULL,
          amount INTEGER NOT NULL,
          status TEXT NOT NULL DEFAULT 'paid',
          created_at TEXT NOT NULL,
          completed_at TEXT,
          FOREIGN KEY (product_id) REFERENCES products(id),
          FOREIGN KEY (buyer_id) REFERENCES users(id),
          FOREIGN KEY (seller_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS chats (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          room_key TEXT NOT NULL,
          sender_id INTEGER NOT NULL,
          receiver_id INTEGER,
          product_id INTEGER,
          message TEXT NOT NULL,
          created_at TEXT NOT NULL,
          FOREIGN KEY (sender_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS chat_reads (
          room_key TEXT NOT NULL,
          user_id INTEGER NOT NULL,
          last_read_message_id INTEGER NOT NULL DEFAULT 0,
          last_read_at TEXT NOT NULL,
          PRIMARY KEY (room_key, user_id),
          FOREIGN KEY (user_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS reports (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          reporter_id INTEGER NOT NULL,
          target_type TEXT NOT NULL,
          target_id INTEGER NOT NULL,
          reason TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'open',
          created_at TEXT NOT NULL,
          FOREIGN KEY (reporter_id) REFERENCES users(id)
        );

        CREATE TABLE IF NOT EXISTS auth_attempts (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          purpose TEXT NOT NULL,
          identifier TEXT NOT NULL,
          failures INTEGER NOT NULL DEFAULT 0,
          locked_until TEXT,
          updated_at TEXT NOT NULL,
          UNIQUE (purpose, identifier)
        );

        CREATE TABLE IF NOT EXISTS security_logs (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          event TEXT NOT NULL,
          actor_id INTEGER,
          ip_address TEXT,
          detail TEXT NOT NULL DEFAULT '',
          created_at TEXT NOT NULL
        );
        """
    )
    chat_read_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(chat_reads)").fetchall()
    }
    if "last_read_message_id" not in chat_read_columns:
        db.execute("ALTER TABLE chat_reads ADD COLUMN last_read_message_id INTEGER NOT NULL DEFAULT 0")
    db.commit()
    ensure_admin()


def ensure_admin() -> None:
    db = get_db()
    admin = db.execute("SELECT id FROM users WHERE username = ?", ("admin",)).fetchone()
    if admin:
        return
    db.execute(
        """
        INSERT INTO users (username, password_hash, name, email, bio, role, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "admin",
            generate_password_hash("admin1234"),
            "관리자",
            "admin@gazimarket.local",
            "가지마켓 운영자",
            "admin",
            now(),
        ),
    )
    db.commit()


def security_log(event: str, detail: str = "", actor_id: int | None = None) -> None:
    safe_detail = re.sub(
        r"(?i)(password|token|session|csrf)[^,\s]*",
        "[redacted]",
        detail,
    )[:500]
    try:
        get_db().execute(
            """
            INSERT INTO security_logs (event, actor_id, ip_address, detail, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (event, actor_id, request.remote_addr or "", safe_detail, now()),
        )
        get_db().commit()
    except Exception:
        app.logger.exception("security log database write failed")
    app.logger.info(
        "security_event=%s actor_id=%s ip=%s detail=%s",
        event,
        actor_id,
        request.remote_addr or "",
        safe_detail,
    )


def csrf_token() -> str:
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


@app.context_processor
def inject_csrf_token():
    return {"csrf_token": csrf_token, "status_label": status_label}


def status_label(value: str) -> str:
    labels = {
        "active": "정상",
        "restricted": "이용 제한",
        "dormant": "휴면",
        "selling": "판매 중",
        "reserved": "예약 중",
        "sold": "판매 완료",
        "paid": "결제 완료",
        "completed": "거래 완료",
        "open": "접수",
        "closed": "처리 완료",
        "product": "상품",
        "user": "사용자",
    }
    return labels.get(value, value)


def validate_csrf() -> None:
    expected = session.get("csrf_token")
    submitted = request.form.get("csrf_token")
    if not expected or not submitted or not secrets.compare_digest(expected, submitted):
        security_log("csrf_failed", f"path={request.path}", session.get("user_id"))
        abort(400)


def contains_blocked_input(value: str) -> bool:
    return any(pattern.search(value) for pattern in BLOCKED_INPUT_PATTERNS)


def clean_text(name: str, value: str, *, min_length: int = 0, max_length: int = 500) -> str:
    cleaned = value.strip()
    if len(cleaned) < min_length:
        raise ValueError(f"{name} 값을 입력하세요.")
    if len(cleaned) > max_length:
        raise ValueError(f"{name} 값은 {max_length}자 이하여야 합니다.")
    if contains_blocked_input(cleaned):
        raise ValueError(f"{name} 값에 허용되지 않는 문자가 포함되어 있습니다.")
    return cleaned


def clean_email(value: str) -> str:
    email = clean_text("이메일", value, min_length=3, max_length=254)
    if not EMAIL_PATTERN.match(email):
        raise ValueError("이메일을 올바르게 입력하세요.")
    return email


def clean_username(value: str) -> str:
    username = clean_text("아이디", value, min_length=3, max_length=30)
    if not USERNAME_PATTERN.match(username):
        raise ValueError("아이디는 영문, 숫자, 밑줄만 사용할 수 있습니다.")
    return username


def auth_identifier(purpose: str, username: str) -> str:
    ip_address = request.remote_addr or "unknown"
    return f"{username.strip().lower()}:{ip_address}:{purpose}"


def safe_redirect_target(default: str) -> str:
    target = request.args.get("next", "")
    parts = urlsplit(target)
    if parts.scheme or parts.netloc or not target.startswith("/"):
        return default
    return target


def auth_is_locked(purpose: str, username: str) -> bool:
    row = get_db().execute(
        "SELECT locked_until FROM auth_attempts WHERE purpose = ? AND identifier = ?",
        (purpose, auth_identifier(purpose, username)),
    ).fetchone()
    locked_until = parse_time(row["locked_until"] if row else None)
    if locked_until and locked_until > datetime.utcnow():
        return True
    return False


def record_auth_failure(purpose: str, username: str) -> None:
    db = get_db()
    identifier = auth_identifier(purpose, username)
    row = db.execute(
        "SELECT failures FROM auth_attempts WHERE purpose = ? AND identifier = ?",
        (purpose, identifier),
    ).fetchone()
    failures = (row["failures"] if row else 0) + 1
    locked_until = None
    if failures >= MAX_AUTH_FAILURES:
        locked_until = (datetime.utcnow() + timedelta(minutes=AUTH_LOCK_MINUTES)).isoformat(
            timespec="seconds"
        )
    db.execute(
        """
        INSERT INTO auth_attempts (purpose, identifier, failures, locked_until, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(purpose, identifier)
        DO UPDATE SET failures = excluded.failures,
                      locked_until = excluded.locked_until,
                      updated_at = excluded.updated_at
        """,
        (purpose, identifier, failures, locked_until, now()),
    )
    db.commit()
    if locked_until:
        security_log("auth_locked", f"purpose={purpose} identifier={identifier}")


def clear_auth_failures(purpose: str, username: str) -> None:
    get_db().execute(
        "DELETE FROM auth_attempts WHERE purpose = ? AND identifier = ?",
        (purpose, auth_identifier(purpose, username)),
    )
    get_db().commit()


def private_room_key(product_id: int, user_a: int, user_b: int) -> str:
    participant_ids = sorted([user_a, user_b])
    return f"product:{product_id}:{participant_ids[0]}:{participant_ids[1]}"


def private_unread_count(user_id: int) -> int:
    row = get_db().execute(
        """
        SELECT COUNT(*) AS count
        FROM chats c
        LEFT JOIN chat_reads cr
          ON cr.room_key = c.room_key AND cr.user_id = ?
        WHERE c.receiver_id = ?
          AND c.sender_id != ?
          AND c.room_key != 'public'
          AND c.id > COALESCE(cr.last_read_message_id, 0)
        """,
        (user_id, user_id, user_id),
    ).fetchone()
    return row["count"] if row else 0


def mark_room_read(room_key: str, user_id: int) -> None:
    row = get_db().execute(
        "SELECT COALESCE(MAX(id), 0) AS last_id FROM chats WHERE room_key = ?",
        (room_key,),
    ).fetchone()
    last_read_message_id = row["last_id"] if row else 0
    get_db().execute(
        """
        INSERT INTO chat_reads (room_key, user_id, last_read_message_id, last_read_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(room_key, user_id)
        DO UPDATE SET last_read_message_id = excluded.last_read_message_id,
                      last_read_at = excluded.last_read_at
        """,
        (room_key, user_id, last_read_message_id, now()),
    )
    get_db().commit()


def chat_message_dict(message: sqlite3.Row) -> dict[str, object]:
    return {
        "id": message["id"],
        "sender_id": message["sender_id"],
        "username": message["username"],
        "message": message["message"],
        "created_at": message["created_at"],
    }


def private_chat_context(product_id: int, other_user_id: int):
    db = get_db()
    product = db.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if product is None:
        abort(404)
    if other_user_id == g.user["id"]:
        abort(400)
    if g.user["id"] == product["seller_id"]:
        room_key = private_room_key(product_id, g.user["id"], other_user_id)
        existing_room = db.execute("SELECT id FROM chats WHERE room_key = ? LIMIT 1", (room_key,)).fetchone()
        if existing_room is None:
            abort(404)
    elif other_user_id == product["seller_id"]:
        room_key = private_room_key(product_id, g.user["id"], other_user_id)
    else:
        abort(403)
    partner = db.execute("SELECT id, username, name FROM users WHERE id = ?", (other_user_id,)).fetchone()
    if partner is None:
        abort(404)
    return product, partner, room_key


def report_target_error(target_type: str, target_id: str, user_id: int) -> str | None:
    if target_type not in {"product", "user"} or not target_id.isdigit():
        return "신고 대상이 올바르지 않습니다."
    target_int = int(target_id)
    if target_type == "user":
        if target_int == user_id:
            return "자기 자신은 신고할 수 없습니다."
        target = get_db().execute("SELECT id FROM users WHERE id = ?", (target_int,)).fetchone()
        if target is None:
            return "신고할 사용자를 찾을 수 없습니다."
    else:
        product = get_db().execute(
            "SELECT seller_id FROM products WHERE id = ?",
            (target_int,),
        ).fetchone()
        if product is None:
            return "신고할 상품을 찾을 수 없습니다."
        if product["seller_id"] == user_id:
            return "자신이 등록한 상품은 신고할 수 없습니다."
    return None


@app.before_request
def load_user() -> None:
    init_db()
    session.permanent = True
    if request.method == "POST":
        validate_csrf()
    user_id = session.get("user_id")
    g.user = None
    if user_id:
        g.user = get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if g.user is None or g.user["status"] != "active":
            session.clear()
            flash("이용할 수 없는 계정입니다.", "error")
        elif request.endpoint != "static":
            g.private_unread_count = private_unread_count(g.user["id"])
    else:
        g.private_unread_count = 0


def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if g.user is None:
            flash("로그인이 필요합니다.", "error")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped_view


def admin_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if g.user is None or g.user["role"] != "admin":
            abort(403)
        return view(*args, **kwargs)

    return wrapped_view


def save_image(file_storage) -> str | None:
    if not file_storage or not file_storage.filename:
        return None
    filename = secure_filename(file_storage.filename)
    extension = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if extension not in ALLOWED_IMAGE_EXTENSIONS:
        raise ValueError("이미지는 png, jpg, jpeg, gif, webp 형식만 가능합니다.")
    unique_name = f"{uuid.uuid4().hex}.{extension}"
    file_storage.save(Path(app.config["UPLOAD_FOLDER"]) / unique_name)
    return f"uploads/{unique_name}"


def int_form(name: str, minimum: int = 0) -> int:
    try:
        value = int(request.form.get(name, "0"))
    except ValueError:
        raise ValueError(f"{name} 값이 올바르지 않습니다.")
    if value < minimum:
        raise ValueError(f"{name} 값은 {minimum} 이상이어야 합니다.")
    return value


def int_query(name: str, minimum: int = 0) -> int:
    try:
        value = int(request.args.get(name, "0"))
    except ValueError:
        raise ValueError(f"{name} 값이 올바르지 않습니다.")
    if value < minimum:
        raise ValueError(f"{name} 값은 {minimum} 이상이어야 합니다.")
    return value


def find_user_by_name_email(name: str, email: str) -> sqlite3.Row | None:
    return get_db().execute(
        """
        SELECT * FROM users
        WHERE name = ? AND lower(email) = lower(?)
        ORDER BY id ASC
        LIMIT 1
        """,
        (name.strip(), email.strip()),
    ).fetchone()


@app.route("/")
def index():
    try:
        query = clean_text("검색어", request.args.get("q", ""), max_length=80)
    except ValueError as exc:
        flash(str(exc), "error")
        query = ""
    params: list[object] = []
    sql = """
        SELECT p.*, u.username, u.name,
               (SELECT COUNT(*) FROM reports r WHERE r.target_type = 'product' AND r.target_id = p.id) AS report_count
        FROM products p
        JOIN users u ON u.id = p.seller_id
        WHERE p.blocked = 0
    """
    if query:
        sql += " AND (p.title LIKE ? OR p.description LIKE ?)"
        params.extend([f"%{query}%", f"%{query}%"])
    sql += " ORDER BY p.created_at DESC"
    products = get_db().execute(sql, params).fetchall()
    return render_template("index.html", products=products, query=query)


@app.route("/register", methods=("GET", "POST"))
def register():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        name = request.form.get("name", "")
        email = request.form.get("email", "")
        error = None
        try:
            username = clean_username(username)
            name = clean_text("이름", name, min_length=1, max_length=50)
            email = clean_email(email)
        except ValueError as exc:
            error = str(exc)
        if error is None and len(password) < 8:
            error = "비밀번호는 8자 이상이어야 합니다."
        elif error is None and contains_blocked_input(password):
            error = "비밀번호에 허용되지 않는 문자열이 포함되어 있습니다."

        if error is None:
            try:
                get_db().execute(
                    """
                    INSERT INTO users (username, password_hash, name, email, created_at)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (username, generate_password_hash(password), name, email, now()),
                )
                get_db().commit()
            except sqlite3.IntegrityError:
                error = "이미 사용 중인 아이디입니다."
            else:
                flash("회원가입이 완료되었습니다. 로그인하세요.", "success")
                return redirect(url_for("login"))
        flash(error, "error")
    return render_template("register.html")


@app.route("/login", methods=("GET", "POST"))
def login():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        if auth_is_locked("login", username):
            security_log("login_blocked", f"username={username}")
            flash("로그인 실패가 반복되어 잠시 후 다시 시도하세요.", "error")
            return render_template("login.html")
        user = get_db().execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if user is None or not check_password_hash(user["password_hash"], password):
            record_auth_failure("login", username)
            flash("아이디 또는 비밀번호가 올바르지 않습니다.", "error")
        elif user["status"] != "active":
            record_auth_failure("login", username)
            flash("이용 제한 또는 휴면 상태인 계정입니다.", "error")
        else:
            clear_auth_failures("login", username)
            session.clear()
            session.permanent = True
            session["csrf_token"] = secrets.token_urlsafe(32)
            session["user_id"] = user["id"]
            security_log("login_success", f"username={username}", user["id"])
            return redirect(safe_redirect_target(url_for("index")))
    return render_template("login.html")


@app.route("/find-id", methods=("GET", "POST"))
def find_id():
    found_username = None
    if request.method == "POST":
        try:
            name = clean_text("이름", request.form.get("name", ""), min_length=1, max_length=50)
            email = clean_email(request.form.get("email", ""))
            user = find_user_by_name_email(name, email)
            if user is None:
                flash("입력한 정보와 일치하는 계정을 찾을 수 없습니다.", "error")
            else:
                found_username = user["username"]
        except ValueError as exc:
            flash(str(exc), "error")
    return render_template("find_id.html", found_username=found_username)


@app.route("/reset-password", methods=("GET", "POST"))
def reset_password():
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        name = request.form.get("name", "")
        email = request.form.get("email", "")
        new_password = request.form.get("new_password", "")
        confirm_password = request.form.get("confirm_password", "")
        if auth_is_locked("reset_password", username):
            security_log("reset_password_blocked", f"username={username}")
            flash("비밀번호 재설정 실패가 반복되어 잠시 후 다시 시도하세요.", "error")
            return render_template("reset_password.html")
        try:
            username = clean_username(username)
            name = clean_text("이름", name, min_length=1, max_length=50)
            email = clean_email(email)
        except ValueError as exc:
            record_auth_failure("reset_password", username)
            flash(str(exc), "error")
            return render_template("reset_password.html")
        user = get_db().execute(
            """
            SELECT * FROM users
            WHERE username = ? AND name = ? AND lower(email) = lower(?)
            """,
            (username, name, email),
        ).fetchone()

        if len(new_password) < 8:
            record_auth_failure("reset_password", username)
            flash("새 비밀번호는 8자 이상이어야 합니다.", "error")
        elif new_password != confirm_password:
            record_auth_failure("reset_password", username)
            flash("새 비밀번호 확인이 일치하지 않습니다.", "error")
        elif contains_blocked_input(new_password):
            record_auth_failure("reset_password", username)
            flash("비밀번호에 허용되지 않는 문자열이 포함되어 있습니다.", "error")
        elif user is None:
            record_auth_failure("reset_password", username)
            flash("입력한 정보와 일치하는 계정을 찾을 수 없습니다.", "error")
        elif user["status"] != "active":
            record_auth_failure("reset_password", username)
            flash("이용 제한 또는 휴면 상태인 계정은 비밀번호를 재설정할 수 없습니다.", "error")
        else:
            get_db().execute(
                "UPDATE users SET password_hash = ? WHERE id = ?",
                (generate_password_hash(new_password), user["id"]),
            )
            get_db().commit()
            clear_auth_failures("reset_password", username)
            security_log("password_reset", f"username={username}", user["id"])
            flash("비밀번호를 재설정했습니다. 새 비밀번호로 로그인하세요.", "success")
            return redirect(url_for("login"))
    return render_template("reset_password.html")


@app.route("/logout", methods=("POST",))
def logout():
    session.clear()
    flash("로그아웃되었습니다.", "success")
    return redirect(url_for("index"))


@app.route("/mypage", methods=("GET", "POST"))
@login_required
def mypage():
    db = get_db()
    if request.method == "POST":
        action = request.form.get("action")
        if action == "profile":
            try:
                name = clean_text("이름", request.form.get("name", ""), min_length=1, max_length=50)
                email = clean_email(request.form.get("email", ""))
                bio = clean_text("소개글", request.form.get("bio", ""), max_length=500)
            except ValueError as exc:
                flash(str(exc), "error")
                return redirect(url_for("mypage"))
            db.execute(
                "UPDATE users SET name = ?, email = ?, bio = ? WHERE id = ?",
                (
                    name,
                    email,
                    bio,
                    g.user["id"],
                ),
            )
            db.commit()
            flash("프로필을 수정했습니다.", "success")
        elif action == "password":
            current = request.form.get("current_password", "")
            new_password = request.form.get("new_password", "")
            if not check_password_hash(g.user["password_hash"], current):
                flash("현재 비밀번호가 올바르지 않습니다.", "error")
            elif len(new_password) < 8:
                flash("새 비밀번호는 8자 이상이어야 합니다.", "error")
            elif contains_blocked_input(new_password):
                flash("비밀번호에 허용되지 않는 문자열이 포함되어 있습니다.", "error")
            else:
                db.execute(
                    "UPDATE users SET password_hash = ? WHERE id = ?",
                    (generate_password_hash(new_password), g.user["id"]),
                )
                db.commit()
                security_log("password_changed", actor_id=g.user["id"])
                flash("비밀번호를 변경했습니다.", "success")
        return redirect(url_for("mypage"))

    products = db.execute(
        "SELECT * FROM products WHERE seller_id = ? ORDER BY created_at DESC", (g.user["id"],)
    ).fetchall()
    trades = db.execute(
        """
        SELECT t.*, p.title, buyer.username AS buyer_name, seller.username AS seller_name
        FROM transactions t
        JOIN products p ON p.id = t.product_id
        JOIN users buyer ON buyer.id = t.buyer_id
        JOIN users seller ON seller.id = t.seller_id
        WHERE t.buyer_id = ? OR t.seller_id = ?
        ORDER BY t.created_at DESC
        """,
        (g.user["id"], g.user["id"]),
    ).fetchall()
    return render_template("mypage.html", products=products, trades=trades)


@app.route("/products/new", methods=("GET", "POST"))
@login_required
def product_new():
    if request.method == "POST":
        try:
            image_path = save_image(request.files.get("image"))
            price = int_form("price", 0)
            title = clean_text("상품명", request.form.get("title", ""), min_length=1, max_length=100)
            description = clean_text(
                "상품 설명", request.form.get("description", ""), min_length=1, max_length=2000
            )
        except ValueError as exc:
            flash(str(exc), "error")
            return render_template("product_form.html", product=None)
        get_db().execute(
            """
            INSERT INTO products (seller_id, title, price, description, image_path, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (g.user["id"], title, price, description, image_path, now(), now()),
        )
        get_db().commit()
        flash("상품을 등록했습니다.", "success")
        return redirect(url_for("index"))
    return render_template("product_form.html", product=None)


@app.route("/products/<int:product_id>")
def product_detail(product_id: int):
    product = get_db().execute(
        """
        SELECT p.*, u.username, u.name, u.bio, u.id AS seller_user_id
        FROM products p JOIN users u ON u.id = p.seller_id
        WHERE p.id = ?
        """,
        (product_id,),
    ).fetchone()
    if product is None or product["blocked"]:
        abort(404)
    report_count = get_db().execute(
        "SELECT COUNT(*) AS count FROM reports WHERE target_type = 'product' AND target_id = ?",
        (product_id,),
    ).fetchone()["count"]
    return render_template("product_detail.html", product=product, report_count=report_count)


@app.route("/products/<int:product_id>/edit", methods=("GET", "POST"))
@login_required
def product_edit(product_id: int):
    product = get_db().execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if product is None:
        abort(404)
    if product["seller_id"] != g.user["id"] and g.user["role"] != "admin":
        abort(403)
    if request.method == "POST":
        try:
            image_path = save_image(request.files.get("image")) or product["image_path"]
            price = int_form("price", 0)
            title = clean_text("상품명", request.form.get("title", ""), min_length=1, max_length=100)
            description = clean_text(
                "상품 설명", request.form.get("description", ""), min_length=1, max_length=2000
            )
            status = request.form.get("status", product["status"])
            if status not in {"selling", "reserved", "sold"}:
                raise ValueError("상품 상태가 올바르지 않습니다.")
        except ValueError as exc:
            flash(str(exc), "error")
            return render_template("product_form.html", product=product)
        get_db().execute(
            """
            UPDATE products
            SET title = ?, price = ?, description = ?, image_path = ?, status = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                title,
                price,
                description,
                image_path,
                status,
                now(),
                product_id,
            ),
        )
        get_db().commit()
        flash("상품을 수정했습니다.", "success")
        return redirect(url_for("product_detail", product_id=product_id))
    return render_template("product_form.html", product=product)


@app.route("/products/<int:product_id>/delete", methods=("POST",))
@login_required
def product_delete(product_id: int):
    product = get_db().execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if product is None:
        abort(404)
    if product["seller_id"] != g.user["id"] and g.user["role"] != "admin":
        abort(403)
    get_db().execute("DELETE FROM products WHERE id = ?", (product_id,))
    get_db().commit()
    flash("상품을 삭제했습니다.", "success")
    return redirect(url_for("mypage"))


@app.route("/products/<int:product_id>/buy", methods=("POST",))
@login_required
def buy_product(product_id: int):
    db = get_db()
    product = db.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if product is None or product["blocked"]:
        abort(404)
    if product["seller_id"] == g.user["id"]:
        flash("내 상품은 구매할 수 없습니다.", "error")
    elif product["status"] != "selling":
        flash("판매 중인 상품만 구매할 수 있습니다.", "error")
    elif g.user["points"] < product["price"]:
        flash("포인트가 부족합니다.", "error")
    else:
        db.execute("UPDATE users SET points = points - ? WHERE id = ?", (product["price"], g.user["id"]))
        db.execute("UPDATE users SET points = points + ? WHERE id = ?", (product["price"], product["seller_id"]))
        db.execute("UPDATE products SET status = 'reserved', updated_at = ? WHERE id = ?", (now(), product_id))
        db.execute(
            """
            INSERT INTO transactions (product_id, buyer_id, seller_id, amount, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (product_id, g.user["id"], product["seller_id"], product["price"], now()),
        )
        db.commit()
        flash("송금이 완료되었습니다. 판매자가 거래 완료 처리할 수 있습니다.", "success")
    return redirect(url_for("product_detail", product_id=product_id))


@app.route("/transactions/<int:transaction_id>/complete", methods=("POST",))
@login_required
def complete_transaction(transaction_id: int):
    db = get_db()
    trade = db.execute("SELECT * FROM transactions WHERE id = ?", (transaction_id,)).fetchone()
    if trade is None:
        abort(404)
    if trade["seller_id"] != g.user["id"] and g.user["role"] != "admin":
        abort(403)
    db.execute(
        "UPDATE transactions SET status = 'completed', completed_at = ? WHERE id = ?",
        (now(), transaction_id),
    )
    db.execute("UPDATE products SET status = 'sold', updated_at = ? WHERE id = ?", (now(), trade["product_id"]))
    db.commit()
    flash("거래 완료 처리했습니다.", "success")
    return redirect(url_for("mypage"))


@app.route("/chat/public", methods=("GET", "POST"))
@login_required
def public_chat():
    db = get_db()
    if request.method == "POST":
        try:
            message = clean_text("메시지", request.form.get("message", ""), min_length=1, max_length=1000)
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("public_chat"))
        if message:
            db.execute(
                """
                INSERT INTO chats (room_key, sender_id, message, created_at)
                VALUES ('public', ?, ?, ?)
                """,
                (g.user["id"], message, now()),
            )
            db.commit()
        return redirect(url_for("public_chat"))
    messages = db.execute(
        """
        SELECT c.*, u.username FROM chats c
        JOIN users u ON u.id = c.sender_id
        WHERE c.room_key = 'public'
        ORDER BY c.id ASC
        LIMIT 100
        """
    ).fetchall()
    return render_template(
        "chat.html",
        messages=messages,
        title="전체 채팅",
        room="public",
        messages_url=url_for("public_chat_messages"),
    )


@app.route("/chat/public/messages")
@login_required
def public_chat_messages():
    since_id = int_query("since_id", 0)
    messages = get_db().execute(
        """
        SELECT c.*, u.username FROM chats c
        JOIN users u ON u.id = c.sender_id
        WHERE c.room_key = 'public' AND c.id > ?
        ORDER BY c.id ASC
        LIMIT 100
        """,
        (since_id,),
    ).fetchall()
    return jsonify(
        {
            "messages": [chat_message_dict(message) for message in messages],
            "unread_count": private_unread_count(g.user["id"]),
        }
    )


@app.route("/chat/private")
@login_required
def private_chat_list():
    db = get_db()
    rooms = db.execute(
        """
        SELECT c.room_key, MAX(c.id) AS last_message_id
        FROM chats c
        WHERE c.room_key != 'public'
          AND (c.sender_id = ? OR c.receiver_id = ?)
        GROUP BY c.room_key
        ORDER BY last_message_id DESC
        """,
        (g.user["id"], g.user["id"]),
    ).fetchall()
    chat_rooms = []
    for room in rooms:
        parts = room["room_key"].split(":")
        if len(parts) != 4 or parts[0] != "product":
            continue
        try:
            product_id = int(parts[1])
            participant_ids = [int(parts[2]), int(parts[3])]
        except ValueError:
            continue
        if g.user["id"] not in participant_ids:
            continue
        partner_id = participant_ids[0] if participant_ids[1] == g.user["id"] else participant_ids[1]
        product = db.execute("SELECT id, title FROM products WHERE id = ?", (product_id,)).fetchone()
        partner = db.execute("SELECT id, username, name FROM users WHERE id = ?", (partner_id,)).fetchone()
        last_message = db.execute(
            """
            SELECT c.*, u.username
            FROM chats c JOIN users u ON u.id = c.sender_id
            WHERE c.room_key = ?
            ORDER BY c.id DESC
            LIMIT 1
            """,
            (room["room_key"],),
        ).fetchone()
        unread = db.execute(
            """
            SELECT COUNT(*) AS count
            FROM chats c
            LEFT JOIN chat_reads cr
              ON cr.room_key = c.room_key AND cr.user_id = ?
            WHERE c.room_key = ?
              AND c.receiver_id = ?
              AND c.sender_id != ?
              AND c.id > COALESCE(cr.last_read_message_id, 0)
            """,
            (g.user["id"], room["room_key"], g.user["id"], g.user["id"]),
        ).fetchone()["count"]
        if product and partner and last_message:
            chat_rooms.append(
                {
                    "room_key": room["room_key"],
                    "product": product,
                    "partner": partner,
                    "last_message": last_message,
                    "unread": unread,
                }
            )
    return render_template("private_chats.html", chat_rooms=chat_rooms)


@app.route("/chat/product/<int:product_id>/<int:seller_id>", methods=("GET", "POST"))
@login_required
def private_chat(product_id: int, seller_id: int):
    db = get_db()
    product, partner, room_key = private_chat_context(product_id, seller_id)
    if request.method == "POST":
        try:
            message = clean_text("메시지", request.form.get("message", ""), min_length=1, max_length=1000)
        except ValueError as exc:
            flash(str(exc), "error")
            return redirect(url_for("private_chat", product_id=product_id, seller_id=seller_id))
        if message:
            db.execute(
                """
                INSERT INTO chats (room_key, sender_id, receiver_id, product_id, message, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (room_key, g.user["id"], seller_id, product_id, message, now()),
            )
            db.commit()
        return redirect(url_for("private_chat", product_id=product_id, seller_id=seller_id))
    messages = db.execute(
        """
        SELECT c.*, u.username FROM chats c
        JOIN users u ON u.id = c.sender_id
        WHERE c.room_key = ?
        ORDER BY c.id ASC
        LIMIT 100
        """,
        (room_key,),
    ).fetchall()
    mark_room_read(room_key, g.user["id"])
    g.private_unread_count = private_unread_count(g.user["id"])
    return render_template(
        "chat.html",
        messages=messages,
        title=f"{product['title']} 1:1 채팅",
        room="private",
        partner=partner,
        messages_url=url_for("private_chat_messages", product_id=product_id, seller_id=seller_id),
    )


@app.route("/chat/product/<int:product_id>/<int:seller_id>/messages")
@login_required
def private_chat_messages(product_id: int, seller_id: int):
    _product, _partner, room_key = private_chat_context(product_id, seller_id)
    since_id = int_query("since_id", 0)
    messages = get_db().execute(
        """
        SELECT c.*, u.username FROM chats c
        JOIN users u ON u.id = c.sender_id
        WHERE c.room_key = ? AND c.id > ?
        ORDER BY c.id ASC
        LIMIT 100
        """,
        (room_key, since_id),
    ).fetchall()
    mark_room_read(room_key, g.user["id"])
    return jsonify(
        {
            "messages": [chat_message_dict(message) for message in messages],
            "unread_count": private_unread_count(g.user["id"]),
        }
    )


@app.route("/reports/new", methods=("GET", "POST"))
@login_required
def report_new():
    target_type = request.args.get("target_type", request.form.get("target_type", "product"))
    target_id = request.args.get("target_id", request.form.get("target_id", ""))
    target_error = report_target_error(target_type, target_id, g.user["id"]) if target_id else None
    if request.method == "GET" and target_error:
        flash(target_error, "error")
        return redirect(url_for("index"))
    if request.method == "POST":
        try:
            reason = clean_text("신고 사유", request.form.get("reason", ""), min_length=1, max_length=1000)
        except ValueError as exc:
            flash(str(exc), "error")
            return render_template("report_form.html", target_type=target_type, target_id=target_id)
        target_error = report_target_error(target_type, target_id, g.user["id"])
        if target_error or not reason:
            flash(target_error or "신고 대상과 사유를 올바르게 입력하세요.", "error")
        else:
            get_db().execute(
                """
                INSERT INTO reports (reporter_id, target_type, target_id, reason, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (g.user["id"], target_type, int(target_id), reason, now()),
            )
            get_db().commit()
            security_log("report_created", f"target={target_type}:{target_id}", g.user["id"])
            flash("신고가 접수되었습니다.", "success")
            return redirect(url_for("index"))
    return render_template("report_form.html", target_type=target_type, target_id=target_id)


@app.route("/admin", methods=("GET", "POST"))
@admin_required
def admin():
    db = get_db()
    if request.method == "POST":
        action = request.form.get("action", "")
        target_id = int_form("target_id", 1)
        if action == "block_product":
            db.execute("UPDATE products SET blocked = 1, updated_at = ? WHERE id = ?", (now(), target_id))
            flash("상품을 차단했습니다.", "success")
        elif action == "delete_product":
            db.execute("DELETE FROM products WHERE id = ?", (target_id,))
            flash("상품을 삭제했습니다.", "success")
        elif action == "restrict_user":
            db.execute("UPDATE users SET status = 'restricted' WHERE id = ? AND role != 'admin'", (target_id,))
            flash("사용자를 이용 제한했습니다.", "success")
        elif action == "sleep_user":
            db.execute("UPDATE users SET status = 'dormant' WHERE id = ? AND role != 'admin'", (target_id,))
            flash("사용자를 휴면 처리했습니다.", "success")
        elif action == "close_report":
            db.execute("UPDATE reports SET status = 'closed' WHERE id = ?", (target_id,))
            flash("신고를 처리 완료로 변경했습니다.", "success")
        else:
            abort(400)
        db.commit()
        security_log("admin_action", f"action={action} target_id={target_id}", g.user["id"])
        return redirect(url_for("admin"))

    users = db.execute(
        """
        SELECT u.*,
               (SELECT COUNT(*) FROM reports r WHERE r.target_type = 'user' AND r.target_id = u.id) AS report_count
        FROM users u ORDER BY u.created_at DESC
        """
    ).fetchall()
    products = db.execute(
        """
        SELECT p.*, u.username,
               (SELECT COUNT(*) FROM reports r WHERE r.target_type = 'product' AND r.target_id = p.id) AS report_count
        FROM products p JOIN users u ON u.id = p.seller_id
        ORDER BY p.created_at DESC
        """
    ).fetchall()
    reports = db.execute(
        """
        SELECT r.*, u.username AS reporter
        FROM reports r JOIN users u ON u.id = r.reporter_id
        ORDER BY r.created_at DESC
        """
    ).fetchall()
    return render_template("admin.html", users=users, products=products, reports=reports)


@app.errorhandler(400)
def bad_request(error):
    security_log("bad_request", f"path={request.path} error={error}", session.get("user_id"))
    return render_template("error.html", title="잘못된 요청", message="요청을 처리할 수 없습니다."), 400


@app.errorhandler(403)
def forbidden(error):
    security_log("forbidden", f"path={request.path} error={error}", session.get("user_id"))
    return render_template("error.html", title="접근 제한", message="접근 권한이 없습니다."), 403


@app.errorhandler(404)
def not_found(error):
    return render_template("error.html", title="페이지 없음", message="요청한 페이지를 찾을 수 없습니다."), 404


@app.errorhandler(Exception)
def internal_error(error):
    if isinstance(error, HTTPException):
        return error
    app.logger.exception("unhandled application error")
    security_log("server_error", f"path={request.path}", session.get("user_id"))
    return render_template("error.html", title="오류 발생", message="일시적인 오류가 발생했습니다."), 500


if __name__ == "__main__":
    app.run(
        host=APP_CONFIG["host"],
        port=APP_CONFIG["port"],
        debug=os.environ.get("GAZIMARKET_DEBUG") == "1",
    )
