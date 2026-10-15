"""Cadastro, login, recuperação de senha e decoradores de permissão."""
import re
import secrets
from datetime import datetime, timedelta, timezone
from functools import wraps

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from . import db as dbm, mail, utils

bp = Blueprint("auth", __name__)

EMAIL_RE = re.compile(r"^[^@\s]{1,64}@[^@\s]{1,255}\.[^@\s.]{2,}$")
MIN_PASSWORD = 10
_DUMMY_HASH = generate_password_hash("dummy-password-for-timing")


def load_user():
    g.user = None
    uid = session.get("uid")
    if not uid:
        return
    u = dbm.query("SELECT * FROM users WHERE id=? AND is_active=1 AND deleted_at IS NULL", (uid,), one=True)
    if u and u["session_epoch"] == session.get("epoch"):
        g.user = u
    else:
        session.clear()


def login_user(user):
    session.clear()
    session.permanent = True
    session["uid"] = user["id"]
    session["epoch"] = user["session_epoch"]
    utils.csrf_token()


def login_required(view):
    @wraps(view)
    def wrapped(*a, **kw):
        if not g.user:
            if utils.wants_json():
                abort(401)
            flash("Entre na sua conta para continuar.", "info")
            return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
        return view(*a, **kw)

    return wrapped


def admin_required(view):
    """Verificação feita no servidor a cada requisição, lendo o papel no banco."""

    @wraps(view)
    def wrapped(*a, **kw):
        if not g.user:
            return redirect(url_for("auth.login", next=request.full_path.rstrip("?")))
        if g.user["role"] != "admin":
            abort(403)
        return view(*a, **kw)

    return wrapped


# ---------- Limitação de tentativas ----------
def _too_many(key, limit, minutes):
    since = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).strftime("%Y-%m-%dT%H:%M:%SZ")
    n = dbm.query("SELECT COUNT(*) c FROM login_attempts WHERE key=? AND created_at>?", (key, since), one=True)["c"]
    return n >= limit


def _record(key):
    dbm.execute("INSERT INTO login_attempts (key, created_at) VALUES (?,?)", (key, dbm.utcnow()))
    old = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    dbm.execute("DELETE FROM login_attempts WHERE created_at<?", (old,))
    dbm.commit()


def _unique_handle(name):
    base = utils.slugify(name, "user")[:24]
    handle = base
    while dbm.query("SELECT 1 FROM users WHERE handle=?", (handle,), one=True):
        handle = f"{base}-{secrets.randbelow(9000) + 1000}"
    return handle


def validate_password(pw, email=""):
    if len(pw) < MIN_PASSWORD:
        return f"A senha precisa ter pelo menos {MIN_PASSWORD} caracteres."
    if email and pw.lower() == email.lower():
        return "A senha não pode ser igual ao e-mail."
    if len(set(pw)) < 4:
        return "Escolha uma senha menos repetitiva."
    return None


# ---------- Rotas ----------
@bp.route("/register", methods=["GET", "POST"])
def register():
    if g.user:
        return redirect(url_for("market.home"))
    form = {}
    if request.method == "POST":
        form = request.form
        name = form.get("display_name", "").strip()
        email = form.get("email", "").strip().lower()
        pw = form.get("password", "")
        errors = []
        ip_key = f"register:{request.remote_addr}"
        if _too_many(ip_key, 10, 60):
            abort(429)
        if not 2 <= len(name) <= 50:
            errors.append("Informe seu nome (2 a 50 caracteres).")
        if not EMAIL_RE.match(email):
            errors.append("Informe um e-mail válido.")
        err = validate_password(pw, email)
        if err:
            errors.append(err)
        if pw != form.get("password2", ""):
            errors.append("As senhas não conferem.")
        if not form.get("terms"):
            errors.append("É preciso aceitar os Termos de uso e a Política de privacidade.")
        if not errors and dbm.query("SELECT 1 FROM users WHERE email=?", (email,), one=True):
            errors.append("Este e-mail já está cadastrado. Tente entrar ou recuperar a senha.")
        if not errors:
            cur = dbm.execute(
                "INSERT INTO users (email, password_hash, display_name, handle, created_at) VALUES (?,?,?,?,?)",
                (email, generate_password_hash(pw), name, _unique_handle(name), dbm.utcnow()),
            )
            dbm.commit()
            _record(ip_key)
            user = dbm.query("SELECT * FROM users WHERE id=?", (cur.lastrowid,), one=True)
            login_user(user)
            flash("Conta criada. Bem-vindo(a) ao Artifex!", "success")
            return redirect(utils.safe_next(request.args.get("next")) or url_for("market.home"))
        for e in errors:
            flash(e, "error")
    return render_template("auth/register.html", form=form)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("market.home"))
    form = {}
    if request.method == "POST":
        form = request.form
        email = form.get("email", "").strip().lower()
        key = f"login:{email}:{request.remote_addr}"
        if _too_many(key, 5, 15):
            flash("Muitas tentativas de login. Aguarde 15 minutos e tente de novo.", "error")
            return render_template("auth/login.html", form=form), 429
        user = dbm.query("SELECT * FROM users WHERE email=? AND is_active=1 AND deleted_at IS NULL", (email,), one=True)
        ok = check_password_hash(user["password_hash"] if user else _DUMMY_HASH, form.get("password", ""))
        if user and ok:
            dbm.execute("DELETE FROM login_attempts WHERE key=?", (key,))
            dbm.commit()
            login_user(user)
            return redirect(utils.safe_next(request.args.get("next")) or url_for("market.home"))
        _record(key)
        flash("E-mail ou senha incorretos.", "error")
    return render_template("auth/login.html", form=form)


@bp.post("/logout")
def logout():
    session.clear()
    flash("Você saiu da sua conta.", "info")
    return redirect(url_for("market.home"))


@bp.route("/forgot", methods=["GET", "POST"])
def forgot():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        key = f"forgot:{request.remote_addr}"
        if _too_many(key, 5, 60):
            abort(429)
        _record(key)
        user = dbm.query("SELECT * FROM users WHERE email=? AND is_active=1 AND deleted_at IS NULL", (email,), one=True)
        if user:
            token = secrets.token_urlsafe(32)
            expires = (datetime.now(timezone.utc) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
            dbm.execute(
                "INSERT INTO password_resets (user_id, token_hash, expires_at, created_at) VALUES (?,?,?,?)",
                (user["id"], utils.token_hash(token), expires, dbm.utcnow()),
            )
            base = current_app.config["BASE_URL"] or request.url_root.rstrip("/")
            link = f"{base}{url_for('auth.reset', token=token)}"
            mail.send_mail(
                email,
                "Redefinição de senha — Artifex",
                f"Olá, {user['display_name']}.\n\nPara criar uma nova senha acesse (válido por 1 hora):\n{link}\n\n"
                "Se você não pediu isso, ignore este e-mail.",
            )
            dbm.commit()
        msg = "Se este e-mail estiver cadastrado, o pedido de redefinição foi registrado."
        if not mail.smtp_configured():
            msg += " Atenção: o envio de e-mails ainda não está configurado neste servidor; fale com o administrador."
        flash(msg, "info")
        return redirect(url_for("auth.forgot"))
    return render_template("auth/forgot.html")


def _valid_reset(token):
    return dbm.query(
        "SELECT * FROM password_resets WHERE token_hash=? AND used_at IS NULL AND expires_at>?",
        (utils.token_hash(token), dbm.utcnow()),
        one=True,
    )


@bp.route("/reset/<token>", methods=["GET", "POST"])
def reset(token):
    row = _valid_reset(token)
    if not row:
        flash("Link inválido ou expirado. Peça uma nova redefinição.", "error")
        return redirect(url_for("auth.forgot"))
    if request.method == "POST":
        pw = request.form.get("password", "")
        user = dbm.query("SELECT * FROM users WHERE id=?", (row["user_id"],), one=True)
        err = validate_password(pw, user["email"])
        if err:
            flash(err, "error")
        elif pw != request.form.get("password2", ""):
            flash("As senhas não conferem.", "error")
        else:
            dbm.execute(
                "UPDATE users SET password_hash=?, session_epoch=session_epoch+1 WHERE id=?",
                (generate_password_hash(pw), user["id"]),
            )
            dbm.execute("UPDATE password_resets SET used_at=? WHERE user_id=? AND used_at IS NULL", (dbm.utcnow(), user["id"]))
            dbm.notify(user["id"], "security", "Sua senha foi alterada", "Se não foi você, redefina novamente e contate o suporte.")
            dbm.commit()
            flash("Senha redefinida. Entre com a nova senha.", "success")
            return redirect(url_for("auth.login"))
    return render_template("auth/reset.html", token=token)
