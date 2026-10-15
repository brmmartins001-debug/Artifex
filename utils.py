"""Funções utilitárias: formatação, CSRF, paginação, URLs seguras."""
import hashlib
import math
import re
import secrets
import unicodedata
from decimal import Decimal, InvalidOperation

from flask import request, session


def slugify(text, fallback="item"):
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return text or fallback


def price_label(cents):
    cents = int(cents or 0)
    if cents == 0:
        return "Gratuito"
    reais, rest = divmod(cents, 100)
    return "R$ {},{:02d}".format("{:,}".format(reais).replace(",", "."), rest)


def parse_price(text):
    """'49,90' ou '1.234,50' -> centavos. Retorna None se inválido."""
    t = (text or "").strip().replace("R$", "").replace(" ", "")
    if not t:
        return None
    if "," in t:
        t = t.replace(".", "").replace(",", ".")
    try:
        value = Decimal(t)
    except InvalidOperation:
        return None
    if not value.is_finite() or value < 0 or value > Decimal("100000"):
        return None
    return int((value * 100).to_integral_value())


def human_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def date_br(iso):
    if not iso:
        return ""
    try:
        d, t = iso.rstrip("Z").split("T")
        y, m, dd = d.split("-")
        return f"{dd}/{m}/{y} {t[:5]}"
    except ValueError:
        return iso


def paragraphs(text):
    return [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]


def token_hash(token):
    return hashlib.sha256(token.encode()).hexdigest()


def safe_next(target):
    """Só aceita caminhos relativos do próprio site (evita open redirect)."""
    if target and target.startswith("/") and not target.startswith("//") and "\\" not in target:
        return target
    return None


# ---------- CSRF ----------
def csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)
    return session["_csrf"]


def csrf_valid():
    sent = request.form.get("csrf_token") or request.headers.get("X-CSRF-Token") or ""
    expected = session.get("_csrf", "")
    return bool(expected) and secrets.compare_digest(sent, expected)


# ---------- Paginação ----------
def paginate(total, page, per_page):
    pages = max(1, math.ceil(total / per_page))
    page = min(max(1, page), pages)
    return {
        "page": page,
        "pages": pages,
        "per_page": per_page,
        "offset": (page - 1) * per_page,
        "total": total,
        "has_prev": page > 1,
        "has_next": page < pages,
    }


def like_escape(term):
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def wants_json():
    return (
        request.path.startswith("/api/")
        or request.headers.get("X-Requested-With") == "fetch"
        or request.accept_mimetypes.best == "application/json"
    )
