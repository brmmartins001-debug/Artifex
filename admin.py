"""Painel administrativo: moderação, usuários, denúncias, categorias, banners, políticas e auditoria.

Todas as rotas exigem papel 'admin' lido do banco a cada requisição (nunca de cookie ou campo do navegador).
"""
import os
import re

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, url_for

from . import catalog, db as dbm, uploads, utils
from .auth import admin_required
from .market import REPORT_REASONS, asset_context, _load_asset, media_url

bp = Blueprint("admin", __name__, url_prefix="/admin")

REASON_REQUIRED = {a for a in catalog.MODERATION_ACTIONS if a != "start_review"}


@bp.before_request
@admin_required
def _guard():
    return None


# ---------- Painel ----------
@bp.get("")
def dashboard():
    c = lambda sql, *a: dbm.query(sql, a, one=True)["c"]  # noqa: E731
    stats = {
        "queue": c("SELECT COUNT(*) c FROM assets WHERE status IN ('submitted','in_review')"),
        "changes": c("SELECT COUNT(*) c FROM assets WHERE status='changes_requested'"),
        "published": c("SELECT COUNT(*) c FROM assets WHERE status='published'"),
        "reports": c("SELECT COUNT(*) c FROM reports WHERE status='open'"),
        "users": c("SELECT COUNT(*) c FROM users WHERE deleted_at IS NULL"),
    }
    recent = dbm.query("SELECT a.*, u.display_name creator_name FROM assets a JOIN users u ON u.id=a.creator_id "
                       "WHERE a.status IN ('submitted','in_review') ORDER BY a.submitted_at LIMIT 8")
    return render_template("admin/dashboard.html", stats=stats, recent=recent)


# ---------- Recursos / fila ----------
@bp.get("/assets")
def assets():
    status = request.args.get("status", "queue")
    q = request.args.get("q", "").strip()[:100]
    where, params = ["a.status<>'draft'"], []
    if status == "queue":
        where.append("a.status IN ('submitted','in_review')")
    elif status in catalog.STATUS_LABELS:
        where.append("a.status=?")
        params.append(status)
    if q:
        where.append("(fold(a.title) LIKE ? ESCAPE '\\' OR fold(u.display_name) LIKE ? ESCAPE '\\')")
        like = utils.like_escape(dbm.fold(q))
        params += [like, like]
    rows = dbm.query(
        f"""SELECT a.*, u.display_name creator_name, c.name category_name FROM assets a
            JOIN users u ON u.id=a.creator_id LEFT JOIN categories c ON c.id=a.category_id
            WHERE {' AND '.join(where)} ORDER BY COALESCE(a.submitted_at, a.updated_at) LIMIT 200""", params)
    return render_template("admin/assets.html", rows=rows, status=status, q=q)


@bp.get("/review/<int:asset_id>")
def review(asset_id):
    a = _load_asset(asset_id)
    ctx = asset_context(a)
    events = dbm.query(
        """SELECT e.*, u.display_name actor FROM moderation_events e LEFT JOIN users u ON u.id=e.actor_id
           WHERE e.asset_id=? ORDER BY e.id DESC""", (asset_id,))
    reports = dbm.query(
        """SELECT r.*, u.display_name reporter FROM reports r JOIN users u ON u.id=r.reporter_id
           WHERE r.asset_id=? ORDER BY r.id DESC""", (asset_id,))
    actions = [(k, v[2]) for k, v in catalog.MODERATION_ACTIONS.items() if a["status"] in v[0]]
    return render_template("admin/review.html", a=a, events=events, reports=reports, actions=actions,
                           reason_required=REASON_REQUIRED, media_url=media_url, report_reasons=REPORT_REASONS,
                           is_owner=False, is_admin=True, in_library=False, is_fav=False, can_download=True,
                           similar=[], last_decision=None, preview=True, **ctx)


@bp.post("/review/<int:asset_id>/decide")
def decide(asset_id):
    a = dbm.query("SELECT * FROM assets WHERE id=?", (asset_id,), one=True)
    if not a:
        abort(404)
    action = request.form.get("action", "")
    reason = request.form.get("reason", "").strip()[:2000]
    rule = catalog.MODERATION_ACTIONS.get(action)
    back = redirect(url_for("admin.review", asset_id=asset_id))
    if not rule or a["status"] not in rule[0]:
        flash("Esta ação não é permitida para o estado atual do recurso.", "error")
        return back
    if action in REASON_REQUIRED and len(reason) < 5:
        flash("Registre o motivo da decisão (mínimo 5 caracteres).", "error")
        return back
    if rule[1] == "published" and not dbm.query("SELECT 1 FROM asset_files WHERE asset_id=?", (a["id"],), one=True):
        flash("O recurso não tem arquivos e não pode ser publicado.", "error")
        return back
    now = dbm.utcnow()
    cur = dbm.execute(
        "UPDATE assets SET status=?, updated_at=?, published_at=CASE WHEN ?='published' AND published_at IS NULL THEN ? ELSE published_at END "
        "WHERE id=? AND status=?", (rule[1], now, rule[1], now, a["id"], a["status"]))
    if not cur.rowcount:
        flash("O estado do recurso mudou enquanto você decidia. Recarregue a página.", "error")
        return back
    dbm.execute("INSERT INTO moderation_events (asset_id, actor_id, action, from_status, to_status, reason, created_at) "
                "VALUES (?,?,?,?,?,?,?)", (a["id"], g.user["id"], action, a["status"], rule[1], reason, now))
    dbm.audit("moderation:" + action, "asset", a["id"], f"{a['status']} -> {rule[1]}: {reason}", actor_id=g.user["id"])
    messages = {
        "request_changes": ("Alterações solicitadas", "Edite seu recurso e envie novamente."),
        "reject": ("Recurso rejeitado", ""),
        "approve": ("Recurso aprovado", "Ele será publicado em breve."),
        "publish": ("Recurso publicado", "Já está visível no marketplace."),
        "approve_publish": ("Recurso aprovado e publicado", "Já está visível no marketplace."),
        "suspend": ("Recurso suspenso", ""),
        "reinstate": ("Recurso reativado", ""),
        "remove": ("Recurso removido", ""),
    }
    if action in messages:
        title, extra = messages[action]
        body = f"“{a['title']}”. " + (f"Motivo: {reason}. " if reason else "") + extra
        dbm.notify(a["creator_id"], "moderation", title, body.strip(), url_for("market.asset", asset_id=a["id"]))
    dbm.commit()
    flash(f"{rule[2]}: concluído.", "success")
    return redirect(url_for("admin.assets"))


# ---------- Usuários ----------
@bp.get("/users")
def users():
    q = request.args.get("q", "").strip()[:100]
    params, where = [], "deleted_at IS NULL"
    if q:
        like = utils.like_escape(dbm.fold(q))
        where += " AND (fold(display_name) LIKE ? ESCAPE '\\' OR fold(email) LIKE ? ESCAPE '\\' OR fold(handle) LIKE ? ESCAPE '\\')"
        params = [like] * 3
    rows = dbm.query(
        f"""SELECT u.*, (SELECT COUNT(*) FROM assets a WHERE a.creator_id=u.id AND a.status='published') n_assets
            FROM users u WHERE {where} ORDER BY u.created_at DESC LIMIT 200""", params)
    return render_template("admin/users.html", rows=rows, q=q)


def _target_user(uid):
    u = dbm.query("SELECT * FROM users WHERE id=? AND deleted_at IS NULL", (uid,), one=True)
    if not u:
        abort(404)
    return u


@bp.post("/users/<int:uid>/toggle")
def user_toggle(uid):
    u = _target_user(uid)
    if u["id"] == g.user["id"]:
        flash("Você não pode suspender a própria conta.", "error")
    else:
        new = 0 if u["is_active"] else 1
        dbm.execute("UPDATE users SET is_active=?, session_epoch=session_epoch+1 WHERE id=?", (new, u["id"]))
        dbm.audit("user:" + ("reactivate" if new else "suspend"), "user", u["id"], u["email"], actor_id=g.user["id"])
        dbm.commit()
        flash("Conta reativada." if new else "Conta suspensa e sessões encerradas.", "success")
    return redirect(url_for("admin.users"))


@bp.post("/users/<int:uid>/role")
def user_role(uid):
    u = _target_user(uid)
    role = request.form.get("role")
    if role not in ("user", "admin"):
        abort(400)
    if u["id"] == g.user["id"]:
        flash("Você não pode alterar o seu próprio papel.", "error")
    else:
        dbm.execute("UPDATE users SET role=?, session_epoch=session_epoch+1 WHERE id=?", (role, u["id"]))
        dbm.audit("user:role", "user", u["id"], f"{u['role']} -> {role}", actor_id=g.user["id"])
        dbm.commit()
        flash("Papel atualizado.", "success")
    return redirect(url_for("admin.users"))


# ---------- Denúncias ----------
@bp.get("/reports")
def reports():
    status = request.args.get("status", "open")
    if status not in ("open", "resolved", "dismissed"):
        status = "open"
    rows = dbm.query(
        """SELECT r.*, a.title asset_title, u.display_name reporter FROM reports r
           JOIN assets a ON a.id=r.asset_id JOIN users u ON u.id=r.reporter_id
           WHERE r.status=? ORDER BY r.id DESC LIMIT 200""", (status,))
    return render_template("admin/reports.html", rows=rows, status=status)


@bp.post("/reports/<int:rid>/resolve")
def report_resolve(rid):
    r = dbm.query("SELECT * FROM reports WHERE id=?", (rid,), one=True)
    if not r:
        abort(404)
    outcome = request.form.get("outcome")
    note = request.form.get("resolution", "").strip()[:1000]
    if outcome not in ("resolved", "dismissed") or len(note) < 3:
        flash("Escolha o resultado e registre uma justificativa.", "error")
    else:
        dbm.execute("UPDATE reports SET status=?, resolution=?, resolved_by=?, resolved_at=? WHERE id=? AND status='open'",
                    (outcome, note, g.user["id"], dbm.utcnow(), rid))
        dbm.audit("report:" + outcome, "report", rid, note, actor_id=g.user["id"])
        dbm.commit()
        flash("Denúncia encerrada.", "success")
    return redirect(url_for("admin.reports"))


# ---------- Categorias ----------
@bp.route("/categories", methods=["GET", "POST"])
def categories():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "create":
            name = request.form.get("name", "").strip()[:60]
            kind = request.form.get("kind", "")
            if len(name) < 2 or kind not in catalog.SPEC_FIELDS:
                flash("Informe o nome e o tipo técnico da categoria.", "error")
            else:
                slug = base = utils.slugify(name)
                n = 1
                while dbm.query("SELECT 1 FROM categories WHERE slug=?", (slug,), one=True):
                    n += 1
                    slug = f"{base}-{n}"
                pos = dbm.query("SELECT COALESCE(MAX(position),0)+1 p FROM categories", one=True)["p"]
                dbm.execute("INSERT INTO categories (slug, name, kind, icon, position) VALUES (?,?,?,?,?)",
                            (slug, name, kind, request.form.get("icon", "box") if request.form.get("icon") in
                             ("box", "user", "car", "mountain", "texture", "image", "grid", "play", "sparkles", "monitor",
                              "volume", "type", "flame") else "box", pos))
                dbm.audit("category:create", "category", slug, name, actor_id=g.user["id"])
                dbm.commit()
                flash("Categoria criada.", "success")
        elif action in ("rename", "toggle"):
            cid = request.form.get("id", type=int)
            c = dbm.query("SELECT * FROM categories WHERE id=?", (cid,), one=True)
            if not c:
                abort(404)
            if action == "rename":
                name = request.form.get("name", "").strip()[:60]
                if len(name) >= 2:
                    dbm.execute("UPDATE categories SET name=? WHERE id=?", (name, cid))
                    dbm.audit("category:rename", "category", cid, f"{c['name']} -> {name}", actor_id=g.user["id"])
            else:
                dbm.execute("UPDATE categories SET is_active=? WHERE id=?", (0 if c["is_active"] else 1, cid))
                dbm.audit("category:toggle", "category", cid, c["name"], actor_id=g.user["id"])
            dbm.commit()
            flash("Categoria atualizada.", "success")
        return redirect(url_for("admin.categories"))
    rows = dbm.query("SELECT c.*, (SELECT COUNT(*) FROM assets a WHERE a.category_id=c.id AND a.status='published') n "
                     "FROM categories c ORDER BY position")
    return render_template("admin/categories.html", rows=rows, kinds=list(catalog.SPEC_FIELDS))


# ---------- Banners ----------
def _valid_destination(dest):
    dest = (dest or "").strip()
    if utils.safe_next(dest) or re.match(r"^https://[^\s/]+\.[^\s]+$", dest):
        return dest
    return None


@bp.route("/banners", methods=["GET", "POST"])
def banners():
    if request.method == "POST":
        action = request.form.get("action")
        if action == "create":
            f = request.form
            title = f.get("title", "").strip()[:80]
            dest = _valid_destination(f.get("destination", "/"))
            img = request.files.get("image")
            if len(title) < 2 or not dest or not img or not img.filename:
                flash("Informe título, imagem e um destino válido (caminho do site ou https://…).", "error")
            else:
                try:
                    saved = uploads.save_image(img, os.path.join(current_app.config["DATA_DIR"], "banners"),
                                               current_app.config["MAX_IMAGE_MB"], min_side=400, max_side=2000, thumb_side=0)
                except uploads.UploadError as e:
                    flash(str(e), "error")
                    return redirect(url_for("admin.banners"))
                dbm.execute(
                    """INSERT INTO banners (title, description, stored_name, destination, position, starts_at, ends_at, is_active, created_at)
                       VALUES (?,?,?,?,?,?,?,1,?)""",
                    (title, f.get("description", "").strip()[:200], saved["stored_name"], dest,
                     f.get("position", type=int) or 0, _date(f.get("starts_at"), "00:00:00"),
                     _date(f.get("ends_at"), "23:59:59"), dbm.utcnow()))
                dbm.audit("banner:create", "banner", "", title, actor_id=g.user["id"])
                dbm.commit()
                flash("Banner criado.", "success")
        elif action in ("toggle", "delete"):
            b = dbm.query("SELECT * FROM banners WHERE id=?", (request.form.get("id", type=int),), one=True)
            if not b:
                abort(404)
            if action == "toggle":
                dbm.execute("UPDATE banners SET is_active=? WHERE id=?", (0 if b["is_active"] else 1, b["id"]))
            else:
                uploads.remove_quietly(os.path.join(current_app.config["DATA_DIR"], "banners"), b["stored_name"])
                dbm.execute("DELETE FROM banners WHERE id=?", (b["id"],))
            dbm.audit("banner:" + action, "banner", b["id"], b["title"], actor_id=g.user["id"])
            dbm.commit()
            flash("Banner atualizado.", "success")
        return redirect(url_for("admin.banners"))
    rows = dbm.query("SELECT * FROM banners ORDER BY position, id")
    return render_template("admin/banners.html", rows=rows, media_url=media_url)


def _date(value, time):
    value = (value or "").strip()
    return f"{value}T{time}Z" if re.match(r"^\d{4}-\d{2}-\d{2}$", value) else None


# ---------- Políticas ----------
@bp.get("/policies")
def policies():
    return render_template("admin/policies.html", rows=dbm.query("SELECT * FROM policies ORDER BY title"))


@bp.route("/policies/<slug>", methods=["GET", "POST"])
def policy_edit(slug):
    p = dbm.query("SELECT * FROM policies WHERE slug=?", (slug,), one=True)
    if not p:
        abort(404)
    if request.method == "POST":
        body = request.form.get("body", "").strip()
        if len(body) < 20:
            flash("O texto da política está curto demais.", "error")
        else:
            dbm.execute("UPDATE policies SET body=?, updated_at=? WHERE slug=?", (body[:50000], dbm.utcnow(), slug))
            dbm.audit("policy:update", "policy", slug, "", actor_id=g.user["id"])
            dbm.commit()
            flash("Política atualizada.", "success")
            return redirect(url_for("admin.policies"))
    return render_template("admin/policy_edit.html", p=p)


# ---------- Auditoria ----------
@bp.get("/audit")
def audit():
    rows = dbm.query(
        """SELECT l.*, u.display_name actor FROM audit_log l LEFT JOIN users u ON u.id=l.actor_id
           ORDER BY l.id DESC LIMIT 300""")
    return render_template("admin/audit.html", rows=rows)
