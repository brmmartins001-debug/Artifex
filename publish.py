"""Publicação de recursos em três etapas, com rascunho, upload seguro e envio para moderação."""
import json
import os
import re

from flask import Blueprint, abort, current_app, flash, g, redirect, render_template, request, url_for

from . import catalog, db as dbm, uploads, utils
from .auth import login_required
from .market import REPORT_REASONS, _load_asset, asset_context, media_url

bp = Blueprint("publish", __name__, url_prefix="/publish")

MAX_IMAGES = 12
MAX_VIDEOS = 3
MAX_FILES = 20
MAX_DRAFTS = 20
TAG_RE = re.compile(r"^[a-z0-9][a-z0-9 \-]{1,29}$")


# ---------- Helpers ----------
def _dirs():
    d = current_app.config["DATA_DIR"]
    return {k: os.path.join(d, k) for k in ("media", "private", "tmp")}


def _owned(asset_id, editable=True):
    a = dbm.query("SELECT * FROM assets WHERE id=? AND creator_id=?", (asset_id, g.user["id"]), one=True)
    if not a or a["status"] == "removed":
        abort(404)
    if editable and a["status"] not in catalog.EDITABLE_STATUSES:
        flash("Este recurso não pode ser editado agora (está em análise ou já foi decidido).", "error")
        return None
    return a


def _category(a):
    if not a["category_id"]:
        return None
    return dbm.query("SELECT * FROM categories WHERE id=?", (a["category_id"],), one=True)


def _touch(asset_id):
    dbm.execute("UPDATE assets SET updated_at=? WHERE id=?", (dbm.utcnow(), asset_id))


def _media(asset_id):
    rows = dbm.query("SELECT * FROM asset_media WHERE asset_id=? ORDER BY position, id", (asset_id,))
    return {
        "cover": next((m for m in rows if m["kind"] == "cover"), None),
        "images": [m for m in rows if m["kind"] == "image"],
        "videos": [m for m in rows if m["kind"] == "video"],
    }


def _tags(asset_id):
    return [r["tag"] for r in dbm.query("SELECT tag FROM asset_tags WHERE asset_id=? ORDER BY tag", (asset_id,))]


def _specs(a):
    try:
        return json.loads(a["specs"] or "{}")
    except ValueError:
        return {}


def _next_pos(asset_id, kind):
    r = dbm.query("SELECT COALESCE(MAX(position),0)+1 p FROM asset_media WHERE asset_id=? AND kind=?", (asset_id, kind), one=True)
    return r["p"]


def _store_media(a, kind, files):
    """Salva uploads de mídia; devolve lista de mensagens de erro (os arquivos válidos são mantidos)."""
    errors = []
    media_dir = _dirs()["media"]
    cfg = current_app.config
    for f in files:
        if not f or not f.filename:
            continue
        try:
            if kind == "video":
                if len(_media(a["id"])["videos"]) >= MAX_VIDEOS:
                    raise uploads.UploadError(f"Máximo de {MAX_VIDEOS} vídeos.")
                saved = uploads.save_video(f, media_dir, cfg["MAX_VIDEO_MB"])
            else:
                if kind == "image" and len(_media(a["id"])["images"]) >= MAX_IMAGES:
                    raise uploads.UploadError(f"Máximo de {MAX_IMAGES} imagens adicionais.")
                saved = uploads.save_image(f, media_dir, cfg["MAX_IMAGE_MB"])
        except uploads.UploadError as e:
            errors.append(f"{f.filename}: {e}")
            continue
        if kind == "cover":
            old = _media(a["id"])["cover"]
            if old:
                uploads.remove_quietly(media_dir, old["stored_name"], old["thumb_name"])
                dbm.execute("DELETE FROM asset_media WHERE id=?", (old["id"],))
        dbm.execute(
            "INSERT INTO asset_media (asset_id, kind, stored_name, thumb_name, mime, size_bytes, position, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (a["id"], kind, saved["stored_name"], saved["thumb_name"], saved["mime"], saved["size"],
             _next_pos(a["id"], kind), dbm.utcnow()))
    return errors


def _parse_tags(raw):
    out = []
    for t in re.split(r"[,\n;]", raw or ""):
        t = re.sub(r"\s+", " ", t.strip().lower())
        if t and t not in out:
            if not TAG_RE.match(t):
                return None, f"Tag inválida: “{t}”. Use 2 a 30 letras, números, espaços ou hífens."
            out.append(t)
    if len(out) > 10:
        return None, "Use no máximo 10 tags."
    return out, None


# ---------- Validação por etapa ----------
def validate_step1(a):
    errs = []
    m = _media(a["id"])
    if len((a["title"] or "").strip()) < 3:
        errs.append("Informe o nome do recurso (mínimo 3 caracteres).")
    if len((a["description"] or "").strip()) < 30:
        errs.append("A descrição precisa ter pelo menos 30 caracteres.")
    if not a["category_id"]:
        errs.append("Escolha a categoria.")
    if not _tags(a["id"]):
        errs.append("Adicione pelo menos uma tag.")
    if not m["cover"]:
        errs.append("Envie a imagem de capa.")
    if len(m["images"]) < 2:
        errs.append("Envie pelo menos duas imagens adicionais.")
    if len(m["videos"]) < 1:
        errs.append("Envie pelo menos um vídeo de demonstração.")
    return errs


def validate_step2(a):
    errs = []
    if not dbm.query("SELECT 1 FROM asset_formats WHERE asset_id=?", (a["id"],), one=True):
        errs.append("Selecione pelo menos um formato de arquivo.")
    if not dbm.query("SELECT 1 FROM asset_compat WHERE asset_id=?", (a["id"],), one=True):
        errs.append("Selecione pelo menos uma engine compatível.")
    if not (a["included_content"] or "").strip():
        errs.append("Descreva o conteúdo incluído.")
    if not a["license"]:
        errs.append("Escolha a licença de uso.")
    if a["license"] == "custom" and not (a["license_notes"] or "").strip():
        errs.append("Descreva os termos da licença personalizada.")
    if _specs(a).get("rights_basis") not in dict(catalog.RIGHTS_BASIS):
        errs.append("Informe a base de titularidade dos direitos.")
    return errs


def validate_step3(a):
    errs = []
    if not dbm.query("SELECT 1 FROM asset_files WHERE asset_id=?", (a["id"],), one=True):
        errs.append("Envie pelo menos um arquivo do recurso.")
    return errs


# ---------- Rotas ----------
@bp.get("")
@login_required
def index():
    drafts = dbm.query(
        """SELECT a.*, c.name AS category_name FROM assets a LEFT JOIN categories c ON c.id=a.category_id
           WHERE a.creator_id=? AND a.status IN ('draft','changes_requested') ORDER BY a.updated_at DESC""", (g.user["id"],))
    return render_template("publish/index.html", drafts=drafts)


@bp.post("/new")
@login_required
def new():
    n = dbm.query("SELECT COUNT(*) c FROM assets WHERE creator_id=? AND status='draft'", (g.user["id"],), one=True)["c"]
    if n >= MAX_DRAFTS:
        flash(f"Você já tem {MAX_DRAFTS} rascunhos. Conclua ou exclua algum antes de criar outro.", "error")
        return redirect(url_for("publish.index"))
    now = dbm.utcnow()
    cur = dbm.execute("INSERT INTO assets (creator_id, status, created_at, updated_at) VALUES (?,?,?,?)",
                      (g.user["id"], "draft", now, now))
    dbm.commit()
    return redirect(url_for("publish.step1", asset_id=cur.lastrowid))


@bp.post("/<int:asset_id>/delete")
@login_required
def delete(asset_id):
    a = _owned(asset_id, editable=False)
    if a["status"] != "draft":
        flash("Só rascunhos podem ser excluídos.", "error")
        return redirect(url_for("publish.index"))
    d = _dirs()
    for m in dbm.query("SELECT * FROM asset_media WHERE asset_id=?", (a["id"],)):
        uploads.remove_quietly(d["media"], m["stored_name"], m["thumb_name"])
    for f in dbm.query("SELECT * FROM asset_files WHERE asset_id=?", (a["id"],)):
        uploads.remove_quietly(d["private"], f["stored_name"])
    dbm.execute("DELETE FROM assets WHERE id=?", (a["id"],))
    dbm.commit()
    flash("Rascunho excluído.", "success")
    return redirect(url_for("publish.index"))


def _wizard_ctx(a, step):
    return {"a": a, "step": step, "media_url": media_url}


# ----- Etapa 1 -----
@bp.route("/<int:asset_id>/1", methods=["GET", "POST"])
@login_required
def step1(asset_id):
    a = _owned(asset_id)
    if a is None:
        return redirect(url_for("market.asset", asset_id=asset_id))
    categories = dbm.query("SELECT * FROM categories WHERE is_active=1 ORDER BY position")
    if request.method == "POST":
        f = request.form
        title = f.get("title", "").strip()[:120]
        desc = f.get("description", "").strip()[:5000]
        sub = f.get("subcategory", "").strip()[:60]
        cat = f.get("category_id", type=int)
        if cat and not dbm.query("SELECT 1 FROM categories WHERE id=? AND is_active=1", (cat,), one=True):
            cat = None
        tags, tag_err = _parse_tags(f.get("tags", ""))
        if tag_err:
            flash(tag_err, "error")
        else:
            dbm.execute("DELETE FROM asset_tags WHERE asset_id=?", (a["id"],))
            for t in tags:
                dbm.execute("INSERT INTO asset_tags (asset_id, tag) VALUES (?,?)", (a["id"], t))
        dbm.execute("UPDATE assets SET title=?, description=?, subcategory=?, category_id=?, updated_at=? WHERE id=?",
                    (title, desc, sub, cat, dbm.utcnow(), a["id"]))
        errors = []
        errors += _store_media(a, "cover", [request.files.get("cover")])
        errors += _store_media(a, "image", request.files.getlist("images"))
        errors += _store_media(a, "video", request.files.getlist("videos"))
        dbm.commit()
        for e in errors:
            flash(e, "error")
        a = _owned(asset_id)
        if request.form.get("action") == "next":
            problems = validate_step1(a)
            if problems:
                for p in problems:
                    flash(p, "error")
            else:
                return redirect(url_for("publish.step2", asset_id=a["id"]))
        elif not errors:
            flash("Rascunho salvo.", "success")
        return redirect(url_for("publish.step1", asset_id=a["id"]))
    return render_template("publish/step1.html", categories=categories, tags=", ".join(_tags(a["id"])),
                           media=_media(a["id"]), limits=current_app.config, max_images=MAX_IMAGES,
                           max_videos=MAX_VIDEOS, **_wizard_ctx(a, 1))


@bp.post("/<int:asset_id>/media/<int:media_id>/delete")
@login_required
def media_delete(asset_id, media_id):
    a = _owned(asset_id)
    if a is None:
        return redirect(url_for("market.asset", asset_id=asset_id))
    m = dbm.query("SELECT * FROM asset_media WHERE id=? AND asset_id=?", (media_id, a["id"]), one=True)
    if m:
        uploads.remove_quietly(_dirs()["media"], m["stored_name"], m["thumb_name"])
        dbm.execute("DELETE FROM asset_media WHERE id=?", (m["id"],))
        _touch(a["id"])
        dbm.commit()
        flash("Mídia removida.", "success")
    return redirect(url_for("publish.step1", asset_id=a["id"]))


@bp.post("/<int:asset_id>/media/<int:media_id>/move")
@login_required
def media_move(asset_id, media_id):
    a = _owned(asset_id)
    if a is None:
        return redirect(url_for("market.asset", asset_id=asset_id))
    direction = request.form.get("dir")
    m = dbm.query("SELECT * FROM asset_media WHERE id=? AND asset_id=?", (media_id, a["id"]), one=True)
    if m and m["kind"] in ("image", "video") and direction in ("up", "down"):
        rows = dbm.query("SELECT id FROM asset_media WHERE asset_id=? AND kind=? ORDER BY position, id", (a["id"], m["kind"]))
        ids = [r["id"] for r in rows]
        i = ids.index(m["id"])
        j = i - 1 if direction == "up" else i + 1
        if 0 <= j < len(ids):
            ids[i], ids[j] = ids[j], ids[i]
            for pos, mid in enumerate(ids, start=1):
                dbm.execute("UPDATE asset_media SET position=? WHERE id=?", (pos, mid))
            _touch(a["id"])
            dbm.commit()
    return redirect(url_for("publish.step1", asset_id=a["id"]) + "#midias")


# ----- Etapa 2 -----
@bp.route("/<int:asset_id>/2", methods=["GET", "POST"])
@login_required
def step2(asset_id):
    a = _owned(asset_id)
    if a is None:
        return redirect(url_for("market.asset", asset_id=asset_id))
    cat = _category(a)
    if not cat or validate_step1(a):
        flash("Conclua a etapa 1 antes de continuar.", "info")
        return redirect(url_for("publish.step1", asset_id=a["id"]))
    kind = cat["kind"]
    fields = catalog.SPEC_FIELDS.get(kind, [])
    formats_allowed = catalog.FORMATS_BY_KIND.get(kind, [])
    if request.method == "POST":
        f = request.form
        specs = _specs(a)
        for key, _label, ftype, options in fields:
            raw = f.get(f"spec_{key}", "").strip()
            if ftype == "bool":
                specs[key] = f.get(f"spec_{key}") == "1"
            elif ftype == "number":
                specs[key] = raw if raw.isdigit() and len(raw) <= 9 else ""
            elif ftype == "select":
                specs[key] = raw if raw in (options or []) else ""
            else:
                specs[key] = raw[:100]
        basis = f.get("rights_basis", "")
        specs["rights_basis"] = basis if basis in dict(catalog.RIGHTS_BASIS) else ""
        specs["rights_detail"] = f.get("rights_detail", "").strip()[:500]
        license_ = f.get("license", "")
        if license_ not in catalog.LICENSE_LABELS:
            license_ = ""
        dbm.execute(
            """UPDATE assets SET requirements=?, dependencies=?, included_content=?, license=?, license_notes=?,
               specs=?, updated_at=? WHERE id=?""",
            (f.get("requirements", "").strip()[:2000], f.get("dependencies", "").strip()[:2000],
             f.get("included_content", "").strip()[:3000], license_, f.get("license_notes", "").strip()[:2000],
             json.dumps(specs, ensure_ascii=False), dbm.utcnow(), a["id"]))
        dbm.execute("DELETE FROM asset_formats WHERE asset_id=?", (a["id"],))
        for fmt in dict.fromkeys(f.getlist("format")):
            if fmt in formats_allowed:
                dbm.execute("INSERT INTO asset_formats (asset_id, format) VALUES (?,?)", (a["id"], fmt))
        dbm.execute("DELETE FROM asset_compat WHERE asset_id=?", (a["id"],))
        for i, eng in enumerate(catalog.ENGINES):
            if f.get(f"engine_{i}"):
                dbm.execute("INSERT INTO asset_compat (asset_id, engine, versions) VALUES (?,?,?)",
                            (a["id"], eng, f.get(f"versions_{i}", "").strip()[:100]))
        dbm.commit()
        a = _owned(asset_id)
        if f.get("action") == "next":
            problems = validate_step2(a)
            if problems:
                for p in problems:
                    flash(p, "error")
            else:
                return redirect(url_for("publish.step3", asset_id=a["id"]))
        elif f.get("action") == "back":
            return redirect(url_for("publish.step1", asset_id=a["id"]))
        else:
            flash("Rascunho salvo.", "success")
        return redirect(url_for("publish.step2", asset_id=a["id"]))
    specs = _specs(a)
    chosen_formats = {r["format"] for r in dbm.query("SELECT format FROM asset_formats WHERE asset_id=?", (a["id"],))}
    compat = {r["engine"]: r["versions"] for r in dbm.query("SELECT * FROM asset_compat WHERE asset_id=?", (a["id"],))}
    return render_template("publish/step2.html", cat=cat, fields=fields, specs=specs, formats_allowed=formats_allowed,
                           chosen_formats=chosen_formats, engines=list(enumerate(catalog.ENGINES)), compat=compat,
                           licenses=catalog.LICENSES, rights=catalog.RIGHTS_BASIS, **_wizard_ctx(a, 2))


# ----- Etapa 3 -----
@bp.route("/<int:asset_id>/3", methods=["GET", "POST"])
@login_required
def step3(asset_id):
    a = _owned(asset_id)
    if a is None:
        return redirect(url_for("market.asset", asset_id=asset_id))
    if validate_step1(a):
        flash("Conclua a etapa 1 antes de continuar.", "info")
        return redirect(url_for("publish.step1", asset_id=a["id"]))
    if validate_step2(a):
        flash("Conclua a etapa 2 antes de continuar.", "info")
        return redirect(url_for("publish.step2", asset_id=a["id"]))
    d = _dirs()
    if request.method == "POST":
        f = request.form
        errors = []
        existing = dbm.query("SELECT COUNT(*) c FROM asset_files WHERE asset_id=?", (a["id"],), one=True)["c"]
        for fs in request.files.getlist("files"):
            if not fs or not fs.filename:
                continue
            if existing >= MAX_FILES:
                errors.append(f"Máximo de {MAX_FILES} arquivos por recurso.")
                break
            try:
                saved = uploads.save_asset_file(fs, d["private"], d["tmp"], current_app.config["MAX_FILE_MB"],
                                                current_app.config["MAX_UNZIPPED_MB"])
            except uploads.UploadError as e:
                errors.append(f"{fs.filename}: {e}")
                continue
            dbm.execute(
                "INSERT INTO asset_files (asset_id, version, original_name, stored_name, ext, size_bytes, sha256, created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (a["id"], a["current_version"], saved["original_name"], saved["stored_name"], saved["ext"],
                 saved["size"], saved["sha256"], dbm.utcnow()))
            existing += 1
        # preço
        if "price_mode" in f:
            if f.get("price_mode") == "free":
                price = 0
            else:
                price = utils.parse_price(f.get("price", ""))
                if price is None or price < 100:
                    errors.append("Informe um preço válido em reais (mínimo R$ 1,00) ou marque o recurso como gratuito.")
                    price = a["price_cents"]
            dbm.execute("UPDATE assets SET price_cents=?, updated_at=? WHERE id=?", (price, dbm.utcnow(), a["id"]))
        dbm.execute("UPDATE assets SET rights_confirmed=? WHERE id=?", (1 if f.get("rights") else 0, a["id"]))
        dbm.commit()
        for e in errors:
            flash(e, "error")
        a = _owned(asset_id)
        action = f.get("action")
        if action == "back":
            return redirect(url_for("publish.step2", asset_id=a["id"]))
        if action == "submit":
            problems = validate_step1(a) + validate_step2(a) + validate_step3(a)
            if errors:
                problems.append("Corrija os erros acima antes de enviar.")
            if not f.get("rights"):
                problems.append("Confirme que você possui os direitos necessários e que a licença informada está correta.")
            if problems:
                for p in problems:
                    flash(p, "error")
            else:
                return _submit(a)
        elif action == "preview":
            return redirect(url_for("publish.preview", asset_id=a["id"]))
        elif not errors:
            flash("Rascunho salvo.", "success")
        return redirect(url_for("publish.step3", asset_id=a["id"]))
    files = dbm.query("SELECT * FROM asset_files WHERE asset_id=? ORDER BY id", (a["id"],))
    return render_template("publish/step3.html", files=files, price_value=("%.2f" % (a["price_cents"] / 100)).replace(".", ",")
                           if a["price_cents"] else "", limits=current_app.config, max_files=MAX_FILES,
                           allowed_exts=sorted(uploads.FILE_EXTS), **_wizard_ctx(a, 3))


@bp.post("/<int:asset_id>/files/<int:file_id>/delete")
@login_required
def file_delete(asset_id, file_id):
    a = _owned(asset_id)
    if a is None:
        return redirect(url_for("market.asset", asset_id=asset_id))
    f = dbm.query("SELECT * FROM asset_files WHERE id=? AND asset_id=?", (file_id, a["id"]), one=True)
    if f:
        uploads.remove_quietly(_dirs()["private"], f["stored_name"])
        dbm.execute("DELETE FROM asset_files WHERE id=?", (f["id"],))
        _touch(a["id"])
        dbm.commit()
        flash("Arquivo removido.", "success")
    return redirect(url_for("publish.step3", asset_id=a["id"]))


@bp.get("/<int:asset_id>/preview")
@login_required
def preview(asset_id):
    a = _owned(asset_id, editable=False)
    full = _load_asset(a["id"])
    ctx = asset_context(full)
    return render_template(
        "market/asset.html", a=full, is_owner=True, is_admin=False, in_library=False, is_fav=False,
        can_download=False, similar=[], last_decision=None, media_url=media_url, report_reasons=REPORT_REASONS,
        preview=True, **ctx)


def _submit(a):
    """Envia para moderação. O recurso NUNCA vira público aqui."""
    now = dbm.utcnow()
    prev = a["status"]
    dbm.execute("UPDATE assets SET status='submitted', submitted_at=?, updated_at=?, rights_confirmed=1, "
                "rights_confirmed_at=? WHERE id=? AND status IN ('draft','changes_requested')", (now, now, now, a["id"]))
    if not dbm.query("SELECT 1 FROM asset_versions WHERE asset_id=? AND version=?", (a["id"], a["current_version"]), one=True):
        dbm.execute("INSERT INTO asset_versions (asset_id, version, changelog, created_at) VALUES (?,?,?,?)",
                    (a["id"], a["current_version"], "Versão inicial", now))
    dbm.execute("INSERT INTO moderation_events (asset_id, actor_id, action, from_status, to_status, reason, created_at) "
                "VALUES (?,?,?,?,?,?,?)", (a["id"], g.user["id"], "submit", prev, "submitted", "", now))
    for adm in dbm.query("SELECT id FROM users WHERE role='admin' AND is_active=1"):
        dbm.notify(adm["id"], "moderation", "Novo recurso aguardando análise", a["title"], url_for("admin.review", asset_id=a["id"]))
    dbm.notify(g.user["id"], "moderation", "Recurso enviado para análise",
               f"“{a['title']}” entrou na fila de moderação e só ficará público após a aprovação.",
               url_for("market.asset", asset_id=a["id"]))
    dbm.commit()
    flash("Recurso enviado para análise. Ele só ficará público depois de aprovado.", "success")
    return redirect(url_for("account.creator"))
