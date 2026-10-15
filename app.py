import os
import secrets

from flask import Flask, g, jsonify, render_template, request, session
from werkzeug.exceptions import HTTPException

from . import catalog, db as dbm, utils
from .icons import ICONS, avatar, icon


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def create_app(test_config=None):
    app = Flask(__name__)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = os.environ.get("ARTIFEX_ENV", "development")

    cfg = {
        "ENV_NAME": env,
        "DATA_DIR": os.environ.get("ARTIFEX_DATA_DIR", os.path.join(root, "instance")),
        "MAX_IMAGE_MB": _env_int("ARTIFEX_MAX_IMAGE_MB", 10),
        "MAX_VIDEO_MB": _env_int("ARTIFEX_MAX_VIDEO_MB", 100),
        "MAX_FILE_MB": _env_int("ARTIFEX_MAX_FILE_MB", 500),
        "MAX_UNZIPPED_MB": _env_int("ARTIFEX_MAX_UNZIPPED_MB", 2048),
        "BASE_URL": os.environ.get("ARTIFEX_BASE_URL", "").rstrip("/"),
        "SMTP_HOST": os.environ.get("ARTIFEX_SMTP_HOST", ""),
        "SMTP_PORT": _env_int("ARTIFEX_SMTP_PORT", 587),
        "SMTP_USER": os.environ.get("ARTIFEX_SMTP_USER", ""),
        "SMTP_PASSWORD": os.environ.get("ARTIFEX_SMTP_PASSWORD", ""),
        "SMTP_FROM": os.environ.get("ARTIFEX_SMTP_FROM", ""),
        "ADMIN_EMAIL": os.environ.get("ARTIFEX_ADMIN_EMAIL", ""),
        "ADMIN_PASSWORD": os.environ.get("ARTIFEX_ADMIN_PASSWORD", ""),
        "PER_PAGE": 24,
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SAMESITE": "Lax",
        "SESSION_COOKIE_SECURE": env == "production",
        "PERMANENT_SESSION_LIFETIME": 60 * 60 * 24 * 30,
    }
    if test_config:
        cfg.update(test_config)
    app.config.update(cfg)

    data_dir = app.config["DATA_DIR"]
    for sub in ("media", "banners", "avatars", "private", "tmp"):
        os.makedirs(os.path.join(data_dir, sub), exist_ok=True)
    app.config.setdefault("DATABASE", os.path.join(data_dir, "artifex.db"))
    app.config["MAX_CONTENT_LENGTH"] = (app.config["MAX_FILE_MB"] + 20) * 1024 * 1024

    secret = app.config.get("SECRET_KEY") or os.environ.get("ARTIFEX_SECRET_KEY")
    if not secret:
        if env == "production":
            raise RuntimeError("Defina ARTIFEX_SECRET_KEY em produção.")
        key_file = os.path.join(data_dir, "secret_key")
        if os.path.exists(key_file):
            with open(key_file) as f:
                secret = f.read().strip()
        else:
            secret = secrets.token_hex(32)
            with open(key_file, "w") as f:
                f.write(secret)
            os.chmod(key_file, 0o600)
    app.config["SECRET_KEY"] = secret

    if os.environ.get("ARTIFEX_BEHIND_PROXY") == "1":
        from werkzeug.middleware.proxy_fix import ProxyFix

        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    with app.app_context():
        dbm.init_db()

    app.teardown_appcontext(dbm.close_db)

    # ---------- Jinja ----------
    app.jinja_env.globals.update(
        icon=icon,
        avatar=avatar,
        ICONS=ICONS,
        csrf_token=utils.csrf_token,
        STATUS_LABELS=catalog.STATUS_LABELS,
        LICENSE_LABELS=catalog.LICENSE_LABELS,
    )
    app.jinja_env.filters["price"] = utils.price_label
    app.jinja_env.filters["datebr"] = utils.date_br
    app.jinja_env.filters["filesize"] = utils.human_size
    app.jinja_env.filters["paragraphs"] = utils.paragraphs

    from . import account, admin, auth, market, publish

    for module in (auth, market, account, publish, admin):
        app.register_blueprint(module.bp)

    @app.before_request
    def _before():
        auth.load_user()
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and not utils.csrf_valid():
            from werkzeug.exceptions import BadRequest

            raise BadRequest("Sessão expirada ou formulário inválido. Recarregue a página e tente de novo.")

    @app.context_processor
    def _ctx():
        unread = 0
        if g.get("user"):
            unread = dbm.query(
                "SELECT COUNT(*) c FROM notifications WHERE user_id=? AND read_at IS NULL", (g.user["id"],), one=True
            )["c"]
        return {"current_user": g.get("user"), "unread_count": unread}

    @app.after_request
    def _headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
        resp.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; media-src 'self'; style-src 'self'; "
            "script-src 'self'; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'",
        )
        if env == "production":
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        if request.endpoint and not request.endpoint.endswith("static") and "Cache-Control" not in resp.headers:
            resp.headers["Cache-Control"] = "no-cache"
        return resp

    @app.errorhandler(HTTPException)
    def _http_error(e):
        messages = {
            400: "Requisição inválida.",
            401: "Entre na sua conta para continuar.",
            403: "Você não tem permissão para acessar esta página.",
            404: "Página não encontrada.",
            405: "Ação não permitida.",
            413: "O arquivo enviado é maior que o limite permitido.",
            429: "Muitas tentativas. Aguarde um pouco e tente de novo.",
        }
        msg = e.description if e.code == 400 and e.description else messages.get(e.code, e.name)
        if utils.wants_json():
            return jsonify(error=msg), e.code
        return render_template("error.html", code=e.code, message=msg), e.code

    @app.errorhandler(Exception)
    def _unhandled(e):
        app.logger.exception("Erro não tratado")
        if utils.wants_json():
            return jsonify(error="Erro interno. Tente novamente."), 500
        return render_template("error.html", code=500, message="Algo deu errado. Tente novamente em instantes."), 500

    @app.get("/healthz")
    def healthz():
        dbm.query("SELECT 1")
        return jsonify(status="ok")

    return app
