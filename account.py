"""Área do usuário: biblioteca, favoritos, notificações, perfil público, configurações e "meus recursos"."""
import os

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from . import db as dbm, uploads
from .auth import login_required, login_user, validate_password
from .market import CARD_COLS, CARD_FROM, PUBLISHED, media_url

bp = Blueprint("account", __name__)


@bp.get("/library")
@login_required
def library():
    items = dbm.query(
        f"""SELECT l.source, l.license AS item_license, l.version AS item_version, l.acquired_at,
                   {CARD_COLS}, a.status AS asset_status
            FROM library_items l JOIN assets a ON a.id=l.asset_id
            JOIN categories c ON c.id=a.category_id JOIN users u ON u.id=a.creator_id
            WHERE l.user_id=? ORDER BY l.acquired_at DESC""", (g.user["id"],))
    files = {}
    for r in dbm.query(
            "SELECT f.* FROM asset_files f JOIN library_items l ON l.asset_id=f.asset_id WHERE l.user_id=? ORDER BY f.id",
            (g.user["id"],)):
        files.setdefault(r["asset_id"], []).append(r)
    versions = {}
    for r in dbm.query(
            "SELECT v.* FROM asset_versions v JOIN library_items l ON l.asset_id=v.asset_id WHERE l.user_id=? ORDER BY v.id DESC",
            (g.user["id"],)):
        versions.setdefault(r["asset_id"], []).append(r)
    return render_template("account/library.html", items=items, files=files, versions=versions, media_url=media_url)


@bp.get("/favorites")
@login_required
def favorites():
    rows = dbm.query(
        f"""SELECT {CARD_COLS} {CARD_FROM} JOIN favorites f ON f.asset_id=a.id
            WHERE f.user_id=? AND {PUBLISHED} ORDER BY f.created_at DESC""", (g.user["id"],))
    return render_template("account/favorites.html", rows=rows, media_url=media_url)


# ---------- Notificações ----------
@bp.get("/notifications")
@login_required
def notifications():
    rows = dbm.query("SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 100", (g.user["id"],))
    return render_template("account/notifications.html", rows=rows)


@bp.post("/notifications/read")
@login_required
def notifications_read():
    nid = request.form.get("id", type=int)
    if nid:
        dbm.execute("UPDATE notifications SET read_at=? WHERE id=? AND user_id=? AND read_at IS NULL",
                    (dbm.utcnow(), nid, g.user["id"]))
    else:
        dbm.execute("UPDATE notifications SET read_at=? WHERE user_id=? AND read_at IS NULL", (dbm.utcnow(), g.user["id"]))
    dbm.commit()
    return redirect(url_for("account.notifications"))


# ---------- Perfil público ----------
@bp.get("/u/<handle>")
def profile(handle):
    u = dbm.query("SELECT * FROM users WHERE handle=? AND is_active=1 AND deleted_at IS NULL", (handle,), one=True)
    if not u:
        abort(404)
    rows = dbm.query(f"SELECT {CARD_COLS} {CARD_FROM} WHERE {PUBLISHED} AND a.creator_id=? ORDER BY a.published_at DESC", (u["id"],))
    stats = dbm.query(
        "SELECT COUNT(*) n, COALESCE(SUM(downloads),0) d FROM assets WHERE creator_id=? AND status='published'", (u["id"],), one=True)
    return render_template("account/profile.html", u=u, rows=rows, stats=stats, media_url=media_url)


# ---------- Meus recursos ----------
@bp.get("/creator")
@login_required
def creator():
    rows = dbm.query(
        """SELECT a.*, c.name AS category_name FROM assets a LEFT JOIN categories c ON c.id=a.category_id
           WHERE a.creator_id=? AND a.status<>'removed' ORDER BY a.updated_at DESC""", (g.user["id"],))
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    totals = {"views": sum(r["views"] for r in rows), "downloads": sum(r["downloads"] for r in rows),
              "favorites": dbm.query(
                  "SELECT COUNT(*) c FROM favorites f JOIN assets a ON a.id=f.asset_id WHERE a.creator_id=?",
                  (g.user["id"],), one=True)["c"]}
    return render_template("account/creator.html", rows=rows, counts=counts, totals=totals)


# ---------- Configurações ----------
@bp.get("/settings")
@login_required
def settings():
    return render_template("account/settings.html")


@bp.post("/settings/profile")
@login_required
def settings_profile():
    name = request.form.get("display_name", "").strip()
    bio = request.form.get("bio", "").strip()
    if not 2 <= len(name) <= 50:
        flash("Informe seu nome (2 a 50 caracteres).", "error")
        return redirect(url_for("account.settings"))
    if len(bio) > 500:
        flash("A biografia pode ter até 500 caracteres.", "error")
        return redirect(url_for("account.settings"))
    avatar = g.user["avatar_path"]
    f = request.files.get("avatar")
    if f and f.filename:
        try:
            saved = uploads.save_image(f, os.path.join(current_app.config["DATA_DIR"], "avatars"),
                                       current_app.config["MAX_IMAGE_MB"], min_side=64, max_side=512, thumb_side=0)
        except uploads.UploadError as e:
            flash(str(e), "error")
            return redirect(url_for("account.settings"))
        uploads.remove_quietly(os.path.join(current_app.config["DATA_DIR"], "avatars"), avatar)
        avatar = saved["stored_name"]
    dbm.execute("UPDATE users SET display_name=?, bio=?, avatar_path=? WHERE id=?", (name, bio, avatar, g.user["id"]))
    dbm.commit()
    flash("Perfil atualizado.", "success")
    return redirect(url_for("account.settings"))


@bp.post("/settings/password")
@login_required
def settings_password():
    if not check_password_hash(g.user["password_hash"], request.form.get("current", "")):
        flash("Senha atual incorreta.", "error")
        return redirect(url_for("account.settings"))
    pw = request.form.get("password", "")
    err = validate_password(pw, g.user["email"])
    if err:
        flash(err, "error")
    elif pw != request.form.get("password2", ""):
        flash("As senhas não conferem.", "error")
    else:
        dbm.execute("UPDATE users SET password_hash=?, session_epoch=session_epoch+1 WHERE id=?",
                    (generate_password_hash(pw), g.user["id"]))
        dbm.notify(g.user["id"], "security", "Sua senha foi alterada", "Os outros dispositivos foram desconectados.")
        dbm.commit()
        login_user(dbm.query("SELECT * FROM users WHERE id=?", (g.user["id"],), one=True))
        flash("Senha alterada. Outros dispositivos foram desconectados.", "success")
    return redirect(url_for("account.settings"))


@bp.post("/settings/delete")
@login_required
def settings_delete():
    """Exclusão de conta: anonimiza os dados pessoais e retira os recursos publicados."""
    if not check_password_hash(g.user["password_hash"], request.form.get("current", "")):
        flash("Senha incorreta. A conta não foi excluída.", "error")
        return redirect(url_for("account.settings"))
    uid = g.user["id"]
    now = dbm.utcnow()
    uploads.remove_quietly(os.path.join(current_app.config["DATA_DIR"], "avatars"), g.user["avatar_path"])
    for a in dbm.query("SELECT id, status FROM assets WHERE creator_id=? AND status<>'removed'", (uid,)):
        dbm.execute("INSERT INTO moderation_events (asset_id, actor_id, action, from_status, to_status, reason, created_at) "
                    "VALUES (?,?,?,?,?,?,?)", (a["id"], uid, "account_deleted", a["status"], "removed", "Conta excluída pelo usuário", now))
    dbm.execute("UPDATE assets SET status='removed', updated_at=? WHERE creator_id=?", (now, uid))
    for t in ("favorites", "library_items", "search_history", "notifications"):
        dbm.execute(f"DELETE FROM {t} WHERE user_id=?", (uid,))
    dbm.execute(
        """UPDATE users SET email=?, display_name='Conta excluída', handle=?, bio='', avatar_path=NULL, is_active=0,
           deleted_at=?, session_epoch=session_epoch+1, password_hash='!' WHERE id=?""",
        (f"deleted-{uid}@deleted.invalid", f"excluida-{uid}", now, uid))
    dbm.audit("account_deleted", "user", uid, "Exclusão solicitada pelo próprio usuário", actor_id=uid)
    dbm.commit()
    session.clear()
    flash("Sua conta foi excluída.", "info")
    return redirect(url_for("market.home"))
