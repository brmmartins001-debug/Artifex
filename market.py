"""Vitrine: home, explorar, categorias, página do recurso, favoritos, obtenção gratuita, downloads e mídia."""
import json
import os
import re

from flask import (Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request,
                   send_from_directory, url_for)

from . import catalog, db as dbm, utils
from .auth import login_required

bp = Blueprint("market", __name__)

PUBLISHED = "a.status='published' AND u.is_active=1 AND u.deleted_at IS NULL"
CARD_COLS = """a.id, a.title, a.price_cents, a.downloads, a.views, a.published_at,
  c.name AS category_name, c.slug AS category_slug, u.display_name AS creator_name, u.handle AS creator_handle,
  (SELECT m.thumb_name FROM asset_media m WHERE m.asset_id=a.id AND m.kind='cover' LIMIT 1) AS cover"""
CARD_FROM = "FROM assets a JOIN categories c ON c.id=a.category_id JOIN users u ON u.id=a.creator_id"
POPULARITY = "(a.downloads*5 + a.views)"


def media_url(kind, name):
    return url_for("market.media", kind=kind, name=name) if name else ""


def can_view_asset(a):
    if a["status"] == "published":
        owner = dbm.query("SELECT is_active, deleted_at FROM users WHERE id=?", (a["creator_id"],), one=True)
        if owner and owner["is_active"] and not owner["deleted_at"]:
            return True
    return bool(g.user and (g.user["id"] == a["creator_id"] or g.user["role"] == "admin"))


# ---------- Home ----------
def _cards(order, limit=12, extra="", params=()):
    return dbm.query(f"SELECT {CARD_COLS} {CARD_FROM} WHERE {PUBLISHED} {extra} ORDER BY {order} LIMIT {limit}", params)


def recommended_for(user, limit=12):
    """Recomendações a partir de biblioteca e favoritos do usuário (+ descoberta de itens novos)."""
    cats = dbm.query(
        """SELECT category_id, SUM(w) w FROM (
             SELECT a.category_id, 2 AS w FROM library_items l JOIN assets a ON a.id=l.asset_id WHERE l.user_id=?
             UNION ALL
             SELECT a.category_id, 3 AS w FROM favorites f JOIN assets a ON a.id=f.asset_id WHERE f.user_id=?
           ) WHERE category_id IS NOT NULL GROUP BY category_id ORDER BY w DESC LIMIT 3""",
        (user["id"], user["id"]),
    )
    if not cats:
        return None
    ids = [c["category_id"] for c in cats]
    marks = ",".join("?" * len(ids))
    excl = ("AND a.creator_id<>? AND a.id NOT IN (SELECT asset_id FROM library_items WHERE user_id=?) "
            "AND a.id NOT IN (SELECT asset_id FROM favorites WHERE user_id=?)")
    base = (user["id"], user["id"], user["id"])
    personal = _cards(POPULARITY + " DESC", 8, f"AND a.category_id IN ({marks}) {excl}", (*ids, *base))
    discover = _cards("a.published_at DESC", limit - len(personal), f"AND a.category_id NOT IN ({marks}) {excl}", (*ids, *base))
    return list(personal) + list(discover) or None


@bp.get("/")
def home():
    now = dbm.utcnow()
    banners = dbm.query(
        """SELECT * FROM banners WHERE is_active=1 AND (starts_at IS NULL OR starts_at='' OR starts_at<=?)
           AND (ends_at IS NULL OR ends_at='' OR ends_at>=?) ORDER BY position, id LIMIT 8""",
        (now, now),
    )
    total = dbm.query(f"SELECT COUNT(*) c {CARD_FROM} WHERE {PUBLISHED}", one=True)["c"]
    ctx = {"banners": banners, "total": total, "popular": [], "recent": [], "free": [], "recommended": None,
           "popular_categories": [], "creators": []}
    if total:
        ctx["popular"] = _cards(POPULARITY + " DESC, a.published_at DESC")
        ctx["recent"] = _cards("a.published_at DESC")
        ctx["free"] = _cards("a.published_at DESC", 12, "AND a.price_cents=0")
        if g.user:
            ctx["recommended"] = recommended_for(g.user)
        if not ctx["recommended"]:
            ctx["popular_categories"] = dbm.query(
                f"""SELECT c.name, c.slug, c.icon, COUNT(*) n {CARD_FROM} WHERE {PUBLISHED}
                    GROUP BY c.id ORDER BY n DESC, c.position LIMIT 6""")
        ctx["creators"] = dbm.query(
            """SELECT u.display_name, u.handle, u.avatar_path, COUNT(*) n, SUM(a.downloads) d FROM users u
               JOIN assets a ON a.creator_id=u.id AND a.status='published'
               WHERE u.is_active=1 AND u.deleted_at IS NULL GROUP BY u.id ORDER BY n DESC, d DESC LIMIT 8""")
    return render_template("market/home.html", media_url=media_url, **ctx)


# ---------- Busca ----------
def build_search(args):
    where, params = [PUBLISHED], []
    score, score_params = [], []
    q = (args.get("q") or "").strip()[:100]
    for term in q.split()[:6]:
        like = utils.like_escape(dbm.fold(term))
        esc = "ESCAPE '\\'"
        title = f"fold(a.title) LIKE ? {esc}"
        desc = f"fold(a.description) LIKE ? {esc}"
        cat = f"fold(c.name) LIKE ? {esc}"
        tag = f"EXISTS(SELECT 1 FROM asset_tags x WHERE x.asset_id=a.id AND fold(x.tag) LIKE ? {esc})"
        fmt = f"EXISTS(SELECT 1 FROM asset_formats x WHERE x.asset_id=a.id AND fold(x.format) LIKE ? {esc})"
        eng = f"EXISTS(SELECT 1 FROM asset_compat x WHERE x.asset_id=a.id AND (fold(x.engine) LIKE ? {esc} OR fold(x.versions) LIKE ? {esc}))"
        where.append(f"({title} OR {desc} OR {cat} OR {tag} OR {fmt} OR {eng})")
        params += [like] * 7
        score.append(f"(CASE WHEN {title} THEN 5 ELSE 0 END)+(CASE WHEN {tag} THEN 3 ELSE 0 END)"
                     f"+(CASE WHEN {cat} THEN 2 ELSE 0 END)+(CASE WHEN {desc} THEN 1 ELSE 0 END)")
        score_params += [like] * 4
    if args.get("category"):
        where.append("c.slug=?")
        params.append(args["category"])
    if args.get("format"):
        where.append("EXISTS(SELECT 1 FROM asset_formats x WHERE x.asset_id=a.id AND x.format=?)")
        params.append(args["format"])
    if args.get("engine"):
        where.append("EXISTS(SELECT 1 FROM asset_compat x WHERE x.asset_id=a.id AND x.engine=?)")
        params.append(args["engine"])
    if args.get("tag"):
        where.append("EXISTS(SELECT 1 FROM asset_tags x WHERE x.asset_id=a.id AND x.tag=?)")
        params.append(args["tag"].strip().lower())
    if args.get("creator"):
        where.append("u.handle=?")
        params.append(args["creator"])
    if args.get("price") == "free":
        where.append("a.price_cents=0")
    elif args.get("price") == "paid":
        where.append("a.price_cents>0")
    sort = args.get("sort") or ("relevance" if q else "recent")
    if sort not in dict(catalog.SORTS):
        sort = "recent"
    score_sql = " + ".join(score) if score else "0"
    order = {
        "relevance": f"({score_sql}) DESC, {POPULARITY} DESC, a.published_at DESC",
        "popular": f"{POPULARITY} DESC, a.published_at DESC",
        "recent": "a.published_at DESC",
        "price_asc": "a.price_cents ASC, a.published_at DESC",
        "price_desc": "a.price_cents DESC, a.published_at DESC",
    }[sort]
    order_params = score_params if sort == "relevance" else []
    return q, sort, " AND ".join(where), params, order, order_params


def run_search(args, page, per_page):
    q, sort, where, params, order, order_params = build_search(args)
    total = dbm.query(f"SELECT COUNT(*) c {CARD_FROM} WHERE {where}", params, one=True)["c"]
    pg = utils.paginate(total, page, per_page)
    rows = dbm.query(
        f"SELECT {CARD_COLS} {CARD_FROM} WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
        (*params, *order_params, pg["per_page"], pg["offset"]),
    )
    return q, sort, rows, pg


def _remember_query(q):
    if g.user and len(q) >= 2:
        dbm.execute("DELETE FROM search_history WHERE user_id=? AND query=?", (g.user["id"], q))
        dbm.execute("INSERT INTO search_history (user_id, query, created_at) VALUES (?,?,?)", (g.user["id"], q, dbm.utcnow()))
        dbm.execute(
            "DELETE FROM search_history WHERE user_id=? AND id NOT IN (SELECT id FROM search_history WHERE user_id=? ORDER BY id DESC LIMIT 20)",
            (g.user["id"], g.user["id"]),
        )
        dbm.commit()


@bp.get("/explore")
def explore():
    page = request.args.get("page", 1, type=int)
    q, sort, rows, pg = run_search(request.args, page, current_app.config["PER_PAGE"])
    if q and page == 1:
        _remember_query(q)
    categories = dbm.query("SELECT slug, name FROM categories WHERE is_active=1 ORDER BY position")
    formats = [r["format"] for r in dbm.query(
        f"""SELECT DISTINCT f.format FROM asset_formats f JOIN assets a ON a.id=f.asset_id JOIN users u ON u.id=a.creator_id
            WHERE {PUBLISHED} ORDER BY f.format""")]
    filters = {k: request.args.get(k, "") for k in ("category", "format", "engine", "price", "tag", "creator")}
    active = {k: v for k, v in filters.items() if v}
    base_args = {k: v for k, v in request.args.items() if k != "page" and v}
    return render_template("market/explore.html", rows=rows, pg=pg, q=q, sort=sort, categories=categories,
                           formats=formats, engines=catalog.ENGINES, sorts=catalog.SORTS, filters=filters,
                           active=active, base_args=base_args, media_url=media_url)


@bp.get("/api/suggest")
def api_suggest():
    q = (request.args.get("q") or "").strip()
    if len(q) < 2:
        return jsonify(items=[])
    _, _, rows, _ = run_search({"q": q}, 1, 6)
    return jsonify(items=[{
        "title": r["title"], "category": r["category_name"], "price": utils.price_label(r["price_cents"]),
        "free": r["price_cents"] == 0, "thumb": media_url("asset", r["cover"]),
        "url": url_for("market.asset", asset_id=r["id"]),
    } for r in rows])


@bp.get("/api/search-history")
def api_history():
    if not g.user:
        return jsonify(items=[])
    rows = dbm.query("SELECT query FROM search_history WHERE user_id=? ORDER BY id DESC LIMIT 8", (g.user["id"],))
    return jsonify(items=[r["query"] for r in rows])


@bp.post("/search/history/clear")
@login_required
def clear_history():
    dbm.execute("DELETE FROM search_history WHERE user_id=?", (g.user["id"],))
    dbm.commit()
    if utils.wants_json():
        return jsonify(ok=True)
    flash("Histórico de pesquisas apagado.", "success")
    return redirect(url_for("market.explore"))


@bp.get("/categories")
def categories():
    rows = dbm.query(
        f"""SELECT c.*, (SELECT COUNT(*) FROM assets a JOIN users u ON u.id=a.creator_id
              WHERE a.category_id=c.id AND {PUBLISHED}) AS n
            FROM categories c WHERE c.is_active=1 ORDER BY c.position""")
    featured = []
    for c in rows:
        if c["n"]:
            featured.append((c, _cards(POPULARITY + " DESC", 4, "AND a.category_id=?", (c["id"],))))
    return render_template("market/categories.html", cats=rows, featured=featured[:6], media_url=media_url)


# ---------- Página do recurso ----------
def _load_asset(asset_id):
    a = dbm.query(
        """SELECT a.*, c.name AS category_name, c.slug AS category_slug, c.kind AS category_kind,
                  u.display_name AS creator_name, u.handle AS creator_handle, u.avatar_path AS creator_avatar
           FROM assets a LEFT JOIN categories c ON c.id=a.category_id JOIN users u ON u.id=a.creator_id WHERE a.id=?""",
        (asset_id,), one=True)
    if not a or not can_view_asset(a):
        abort(404)
    return a


def asset_context(a):
    """Dados completos de um recurso para a página (também usada na prévia do criador)."""
    q = dbm.query
    media = q("SELECT * FROM asset_media WHERE asset_id=? ORDER BY kind='video', position, id", (a["id"],))
    specs = {}
    try:
        specs = json.loads(a["specs"] or "{}")
    except ValueError:
        pass
    labels = {k: lbl for k, lbl, _t, _o in catalog.SPEC_FIELDS.get(a["category_kind"] or "", [])}
    spec_rows = []
    for k, lbl in labels.items():
        v = specs.get(k)
        if v in (None, "", False):
            continue
        spec_rows.append((lbl, "Sim" if v is True else v))
    return {
        "cover": next((m for m in media if m["kind"] == "cover"), None),
        "images": [m for m in media if m["kind"] == "image"],
        "videos": [m for m in media if m["kind"] == "video"],
        "tags": [r["tag"] for r in q("SELECT tag FROM asset_tags WHERE asset_id=? ORDER BY tag", (a["id"],))],
        "formats": [r["format"] for r in q("SELECT format FROM asset_formats WHERE asset_id=? ORDER BY format", (a["id"],))],
        "compat": q("SELECT engine, versions FROM asset_compat WHERE asset_id=? ORDER BY engine", (a["id"],)),
        "spec_rows": spec_rows,
        "rights_basis": specs.get("rights_basis"),
        "versions": q("SELECT * FROM asset_versions WHERE asset_id=? ORDER BY id DESC", (a["id"],)),
        "files": q("SELECT * FROM asset_files WHERE asset_id=? ORDER BY id", (a["id"],)),
    }


REPORT_REASONS = [
    "Violação de direitos autorais",
    "Conteúdo impróprio ou ilegal",
    "Arquivo malicioso ou suspeito",
    "Descrição enganosa",
    "Spam ou golpe",
    "Outro motivo",
]


@bp.get("/asset/<int:asset_id>")
def asset(asset_id):
    a = _load_asset(asset_id)
    is_owner = bool(g.user and g.user["id"] == a["creator_id"])
    is_admin = bool(g.user and g.user["role"] == "admin")
    if a["status"] == "published" and not is_owner:
        dbm.execute("UPDATE assets SET views=views+1 WHERE id=?", (a["id"],))
        dbm.commit()
    ctx = asset_context(a)
    in_library = bool(g.user and dbm.query(
        "SELECT 1 FROM library_items WHERE user_id=? AND asset_id=?", (g.user["id"], a["id"]), one=True))
    is_fav = bool(g.user and dbm.query(
        "SELECT 1 FROM favorites WHERE user_id=? AND asset_id=?", (g.user["id"], a["id"]), one=True))
    can_download = (a["status"] == "published" and in_library) or is_owner or is_admin
    similar = []
    if a["status"] == "published" and a["category_id"]:
        similar = _cards(POPULARITY + " DESC", 6, "AND a.category_id=? AND a.id<>?", (a["category_id"], a["id"]))
    last_decision = None
    if is_owner and a["status"] in ("changes_requested", "rejected", "suspended", "removed"):
        last_decision = dbm.query(
            "SELECT * FROM moderation_events WHERE asset_id=? AND reason<>'' ORDER BY id DESC LIMIT 1", (a["id"],), one=True)
    return render_template(
        "market/asset.html", a=a, is_owner=is_owner, is_admin=is_admin, in_library=in_library, is_fav=is_fav,
        can_download=can_download, similar=similar, last_decision=last_decision, media_url=media_url,
        report_reasons=REPORT_REASONS, preview=False, **ctx)


@bp.post("/asset/<int:asset_id>/favorite")
@login_required
def favorite(asset_id):
    a = _load_asset(asset_id)
    row = dbm.query("SELECT 1 FROM favorites WHERE user_id=? AND asset_id=?", (g.user["id"], a["id"]), one=True)
    if row:
        dbm.execute("DELETE FROM favorites WHERE user_id=? AND asset_id=?", (g.user["id"], a["id"]))
    elif a["status"] == "published":
        dbm.execute("INSERT INTO favorites (user_id, asset_id, created_at) VALUES (?,?,?)", (g.user["id"], a["id"], dbm.utcnow()))
    dbm.commit()
    fav = not row and a["status"] == "published"
    if utils.wants_json():
        return jsonify(favorited=fav)
    return redirect(url_for("market.asset", asset_id=a["id"]))


@bp.post("/asset/<int:asset_id>/get")
@login_required
def get_free(asset_id):
    a = _load_asset(asset_id)
    back = redirect(url_for("market.asset", asset_id=a["id"]))
    if a["status"] != "published":
        flash("Este recurso não está disponível.", "error")
        return back
    if a["price_cents"] > 0:
        flash("As compras ainda não estão habilitadas: o pagamento não está configurado.", "error")
        return back
    if a["creator_id"] == g.user["id"]:
        flash("Este recurso é seu; você já tem acesso aos arquivos.", "info")
        return back
    if not dbm.query("SELECT 1 FROM asset_files WHERE asset_id=?", (a["id"],), one=True):
        flash("Este recurso não tem arquivos disponíveis.", "error")
        return back
    cur = dbm.execute(
        "INSERT OR IGNORE INTO library_items (user_id, asset_id, source, license, version, acquired_at) VALUES (?,?,?,?,?,?)",
        (g.user["id"], a["id"], "free", a["license"], a["current_version"], dbm.utcnow()))
    dbm.commit()
    flash("Recurso adicionado à sua biblioteca." if cur.rowcount else "Este recurso já está na sua biblioteca.", "success")
    return back


@bp.post("/asset/<int:asset_id>/report")
@login_required
def report(asset_id):
    a = _load_asset(asset_id)
    reason = request.form.get("reason", "")
    details = request.form.get("details", "").strip()[:1000]
    if reason not in REPORT_REASONS:
        flash("Escolha um motivo para a denúncia.", "error")
    elif dbm.query("SELECT 1 FROM reports WHERE reporter_id=? AND asset_id=? AND status='open'", (g.user["id"], a["id"]), one=True):
        flash("Você já tem uma denúncia em aberto para este recurso.", "info")
    else:
        dbm.execute("INSERT INTO reports (reporter_id, asset_id, reason, details, created_at) VALUES (?,?,?,?,?)",
                    (g.user["id"], a["id"], reason, details, dbm.utcnow()))
        dbm.commit()
        flash("Denúncia enviada. Nossa equipe vai analisar.", "success")
    return redirect(url_for("market.asset", asset_id=a["id"]))


# ---------- Download protegido ----------
@bp.get("/download/<int:file_id>")
def download(file_id):
    if not g.user:
        flash("Entre na sua conta para baixar.", "info")
        return redirect(url_for("auth.login", next=request.path))
    f = dbm.query("SELECT * FROM asset_files WHERE id=?", (file_id,), one=True)
    a = dbm.query("SELECT * FROM assets WHERE id=?", (f["asset_id"],), one=True) if f else None
    if not f or not a:
        abort(404)
    is_owner = g.user["id"] == a["creator_id"]
    is_admin = g.user["role"] == "admin"
    allowed = is_owner or is_admin
    if not allowed and a["status"] == "published":
        allowed = bool(dbm.query("SELECT 1 FROM library_items WHERE user_id=? AND asset_id=?", (g.user["id"], a["id"]), one=True))
    if not allowed:
        abort(403)
    if not is_owner:
        dbm.execute("INSERT INTO download_log (user_id, asset_id, file_id, created_at) VALUES (?,?,?,?)",
                    (g.user["id"], a["id"], f["id"], dbm.utcnow()))
        dbm.execute("UPDATE assets SET downloads=downloads+1 WHERE id=?", (a["id"],))
        dbm.commit()
    resp = send_from_directory(os.path.join(current_app.config["DATA_DIR"], "private"), f["stored_name"],
                               as_attachment=True, download_name=f["original_name"], mimetype="application/octet-stream")
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


# ---------- Mídia (imagens/vídeos/banners/avatares) ----------
_NAME_RE = re.compile(r"^[0-9a-f]{32}\.(webp|mp4|webm)$")


@bp.get("/media/<kind>/<name>")
def media(kind, name):
    if not _NAME_RE.match(name):
        abort(404)
    data_dir = current_app.config["DATA_DIR"]
    public = True
    if kind == "asset":
        m = dbm.query(
            """SELECT a.status, a.creator_id FROM asset_media m JOIN assets a ON a.id=m.asset_id
               WHERE m.stored_name=? OR m.thumb_name=?""", (name, name), one=True)
        if not m:
            abort(404)
        public = m["status"] == "published"
        if not public and not (g.user and (g.user["id"] == m["creator_id"] or g.user["role"] == "admin")):
            abort(404)
        directory = "media"
    elif kind == "banner":
        if not dbm.query("SELECT 1 FROM banners WHERE stored_name=?", (name,), one=True):
            abort(404)
        directory = "banners"
    elif kind == "avatar":
        if not dbm.query("SELECT 1 FROM users WHERE avatar_path=?", (name,), one=True):
            abort(404)
        directory = "avatars"
    else:
        abort(404)
    resp = send_from_directory(os.path.join(data_dir, directory), name, conditional=True)
    resp.headers["Cache-Control"] = "public, max-age=3600" if public else "private, no-cache"
    return resp


@bp.get("/p/<slug>")
def policy(slug):
    p = dbm.query("SELECT * FROM policies WHERE slug=?", (slug,), one=True)
    if not p:
        abort(404)
    return render_template("market/policy.html", p=p)
