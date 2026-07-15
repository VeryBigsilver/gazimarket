from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import datetime
from functools import wraps
from pathlib import Path

import yaml
from flask import (
    Flask,
    abort,
    flash,
    g,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
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

ALLOWED_IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}


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
        """
    )
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


@app.before_request
def load_user() -> None:
    init_db()
    user_id = session.get("user_id")
    g.user = None
    if user_id:
        g.user = get_db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if g.user is None or g.user["status"] != "active":
            session.clear()
            flash("이용할 수 없는 계정입니다.", "error")


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


@app.route("/")
def index():
    query = request.args.get("q", "").strip()
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
        username = request.form["username"].strip()
        password = request.form["password"]
        name = request.form["name"].strip()
        email = request.form["email"].strip()
        error = None
        if len(username) < 3:
            error = "아이디는 3자 이상이어야 합니다."
        elif len(password) < 8:
            error = "비밀번호는 8자 이상이어야 합니다."
        elif not name or "@" not in email:
            error = "이름과 이메일을 올바르게 입력하세요."

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
        username = request.form["username"].strip()
        password = request.form["password"]
        user = get_db().execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if user is None or not check_password_hash(user["password_hash"], password):
            flash("아이디 또는 비밀번호가 올바르지 않습니다.", "error")
        elif user["status"] != "active":
            flash("이용 제한 또는 휴면 상태인 계정입니다.", "error")
        else:
            session.clear()
            session["user_id"] = user["id"]
            return redirect(request.args.get("next") or url_for("index"))
    return render_template("login.html")


@app.route("/logout")
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
            db.execute(
                "UPDATE users SET name = ?, email = ?, bio = ? WHERE id = ?",
                (
                    request.form["name"].strip(),
                    request.form["email"].strip(),
                    request.form.get("bio", "").strip(),
                    g.user["id"],
                ),
            )
            db.commit()
            flash("프로필을 수정했습니다.", "success")
        elif action == "password":
            current = request.form["current_password"]
            new_password = request.form["new_password"]
            if not check_password_hash(g.user["password_hash"], current):
                flash("현재 비밀번호가 올바르지 않습니다.", "error")
            elif len(new_password) < 8:
                flash("새 비밀번호는 8자 이상이어야 합니다.", "error")
            else:
                db.execute(
                    "UPDATE users SET password_hash = ? WHERE id = ?",
                    (generate_password_hash(new_password), g.user["id"]),
                )
                db.commit()
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
        except ValueError as exc:
            flash(str(exc), "error")
            return render_template("product_form.html", product=None)
        title = request.form["title"].strip()
        description = request.form["description"].strip()
        if not title or not description:
            flash("상품명과 설명을 입력하세요.", "error")
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
                request.form["title"].strip(),
                price,
                request.form["description"].strip(),
                image_path,
                request.form.get("status", product["status"]),
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
        message = request.form["message"].strip()
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
        ORDER BY c.created_at ASC
        LIMIT 100
        """
    ).fetchall()
    return render_template("chat.html", messages=messages, title="전체 채팅", room="public")


@app.route("/chat/product/<int:product_id>/<int:seller_id>", methods=("GET", "POST"))
@login_required
def private_chat(product_id: int, seller_id: int):
    db = get_db()
    product = db.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if product is None:
        abort(404)
    participant_ids = sorted([g.user["id"], seller_id])
    room_key = f"product:{product_id}:{participant_ids[0]}:{participant_ids[1]}"
    if request.method == "POST":
        message = request.form["message"].strip()
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
        ORDER BY c.created_at ASC
        LIMIT 100
        """,
        (room_key,),
    ).fetchall()
    return render_template("chat.html", messages=messages, title=f"{product['title']} 1:1 채팅", room="private")


@app.route("/reports/new", methods=("GET", "POST"))
@login_required
def report_new():
    target_type = request.args.get("target_type", request.form.get("target_type", "product"))
    target_id = request.args.get("target_id", request.form.get("target_id", ""))
    if request.method == "POST":
        reason = request.form["reason"].strip()
        if target_type not in {"product", "user"} or not target_id.isdigit() or not reason:
            flash("신고 대상과 사유를 올바르게 입력하세요.", "error")
        else:
            get_db().execute(
                """
                INSERT INTO reports (reporter_id, target_type, target_id, reason, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (g.user["id"], target_type, int(target_id), reason, now()),
            )
            get_db().commit()
            flash("신고가 접수되었습니다.", "success")
            return redirect(url_for("index"))
    return render_template("report_form.html", target_type=target_type, target_id=target_id)


@app.route("/admin", methods=("GET", "POST"))
@admin_required
def admin():
    db = get_db()
    if request.method == "POST":
        action = request.form["action"]
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
        db.commit()
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


if __name__ == "__main__":
    app.run(host=APP_CONFIG["host"], port=APP_CONFIG["port"], debug=True)
