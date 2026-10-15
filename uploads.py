"""Validação e armazenamento seguro de uploads (imagens, vídeos e arquivos entregáveis).

Princípios: nunca confiar no nome/extensão/Content-Type enviados; reprocessar imagens (remove metadados e
conteúdo embutido); guardar arquivos com nomes aleatórios; arquivos entregáveis ficam fora da pasta pública.
"""
import hashlib
import os
import shutil
import subprocess
import uuid
import zipfile

from PIL import Image, ImageOps
from werkzeug.utils import secure_filename

MB = 1024 * 1024
Image.MAX_IMAGE_PIXELS = 50_000_000


class UploadError(Exception):
    """Erro de validação mostrado ao usuário."""


IMAGE_EXTS = {"png", "jpg", "jpeg", "webp", "gif"}
IMAGE_FORMATS = {"PNG", "JPEG", "WEBP", "GIF"}
VIDEO_EXTS = {"mp4", "webm"}

# Arquivos entregáveis aceitos. Arquivos compactados: somente .zip (conteúdo é inspecionado).
FILE_EXTS = {
    "zip", "unitypackage",
    "fbx", "obj", "glb", "gltf", "blend", "dae", "3ds", "stl", "usd", "usdz", "abc", "bvh",
    "png", "jpg", "jpeg", "webp", "tga", "psd", "exr", "hdr", "svg", "ase", "aseprite", "tmx", "tsx", "sbsar",
    "wav", "mp3", "ogg", "flac", "mid", "midi",
    "ttf", "otf", "woff", "woff2",
    "shader", "gdshader", "hlsl", "glsl", "cginc", "shadergraph", "tres", "tscn",
    "json", "txt", "md", "pdf", "csv",
}

# Executáveis e instaladores: bloqueados mesmo dentro de .zip.
BLOCKED_EXTS = {
    "exe", "dll", "so", "dylib", "bat", "cmd", "com", "scr", "msi", "msp", "ps1", "psm1", "vbs", "vbe", "wsf",
    "jar", "apk", "aab", "ipa", "app", "deb", "rpm", "dmg", "pkg", "sh", "bash", "run", "bin", "lnk", "reg",
    "hta", "cpl", "pif", "gadget", "appimage",
}
NOT_INSPECTABLE = {"rar", "7z", "tar", "gz", "tgz", "bz2", "xz"}

_MAGIC = {
    "png": [b"\x89PNG\r\n\x1a\n"],
    "jpg": [b"\xff\xd8\xff"],
    "jpeg": [b"\xff\xd8\xff"],
    "gif": [b"GIF87a", b"GIF89a"],
    "glb": [b"glTF"],
    "zip": [b"PK\x03\x04", b"PK\x05\x06"],
    "unitypackage": [b"\x1f\x8b"],
    "wav": [b"RIFF"],
    "ogg": [b"OggS"],
    "flac": [b"fLaC"],
    "ttf": [b"\x00\x01\x00\x00", b"true"],
    "otf": [b"OTTO"],
    "woff": [b"wOFF"],
    "woff2": [b"wOF2"],
    "pdf": [b"%PDF"],
    "psd": [b"8BPS"],
    "mid": [b"MThd"],
    "midi": [b"MThd"],
    "mp3": [b"ID3", b"\xff\xfb", b"\xff\xf3", b"\xff\xf2", b"\xff\xe3"],
}
_EXEC_MAGIC = (b"MZ", b"\x7fELF", b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf", b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe")


def ext_of(filename):
    return os.path.splitext(filename or "")[1].lower().lstrip(".")


def _size(fs):
    pos = fs.stream.tell()
    fs.stream.seek(0, 2)
    n = fs.stream.tell()
    fs.stream.seek(pos)
    return n


def _limit(n, max_mb, what):
    if n <= 0:
        raise UploadError(f"{what}: o arquivo está vazio.")
    if n > max_mb * MB:
        raise UploadError(f"{what}: o arquivo excede o limite de {max_mb} MB.")


# ---------- Imagens ----------
def save_image(fs, directory, max_mb, *, min_side=300, max_side=2400, thumb_side=480):
    """Valida, reprocessa para WEBP (sem metadados) e gera miniatura. Retorna dict."""
    if ext_of(fs.filename) not in IMAGE_EXTS:
        raise UploadError("Formato de imagem não permitido. Use PNG, JPG, WEBP ou GIF.")
    _limit(_size(fs), max_mb, "Imagem")
    try:
        probe = Image.open(fs.stream)
        probe.verify()
        fs.stream.seek(0)
        img = Image.open(fs.stream)
        fmt = img.format
        img = ImageOps.exif_transpose(img)
        img.load()
    except Exception as exc:  # noqa: BLE001
        raise UploadError("Arquivo de imagem inválido ou corrompido.") from exc
    if fmt not in IMAGE_FORMATS:
        raise UploadError("O conteúdo do arquivo não é uma imagem PNG, JPG, WEBP ou GIF.")
    w, h = img.size
    if min(w, h) < min_side:
        raise UploadError(f"A imagem é pequena demais (mínimo {min_side}px no menor lado).")
    has_alpha = "A" in img.mode or "transparency" in img.info
    img = img.convert("RGBA" if has_alpha else "RGB")

    name = uuid.uuid4().hex + ".webp"
    main = img.copy()
    main.thumbnail((max_side, max_side))
    main.save(os.path.join(directory, name), "WEBP", quality=88, method=4)
    thumb_name = None
    if thumb_side:
        thumb_name = uuid.uuid4().hex + ".webp"
        t = img.copy()
        t.thumbnail((thumb_side, thumb_side))
        t.save(os.path.join(directory, thumb_name), "WEBP", quality=80, method=4)
    return {
        "stored_name": name,
        "thumb_name": thumb_name,
        "mime": "image/webp",
        "size": os.path.getsize(os.path.join(directory, name)),
    }


# ---------- Vídeos ----------
def save_video(fs, directory, max_mb):
    ext = ext_of(fs.filename)
    if ext not in VIDEO_EXTS:
        raise UploadError("Formato de vídeo não permitido. Use MP4 ou WEBM.")
    _limit(_size(fs), max_mb, "Vídeo")
    fs.stream.seek(0)
    head = fs.stream.read(16)
    fs.stream.seek(0)
    if ext == "mp4" and head[4:8] != b"ftyp":
        raise UploadError("O arquivo não parece ser um vídeo MP4 válido.")
    if ext == "webm" and not head.startswith(b"\x1a\x45\xdf\xa3"):
        raise UploadError("O arquivo não parece ser um vídeo WEBM válido.")
    name = uuid.uuid4().hex + "." + ext
    path = os.path.join(directory, name)
    with open(path, "wb") as out:
        shutil.copyfileobj(fs.stream, out)
    return {"stored_name": name, "thumb_name": None, "mime": "video/mp4" if ext == "mp4" else "video/webm", "size": os.path.getsize(path)}


# ---------- Arquivos entregáveis ----------
def _check_head(ext, head):
    if any(head.startswith(m) for m in _EXEC_MAGIC):
        raise UploadError("Arquivos executáveis não são permitidos.")
    if ext == "webp":
        if not (head[:4] == b"RIFF" and head[8:12] == b"WEBP"):
            raise UploadError("O conteúdo do arquivo não corresponde à extensão informada.")
        return
    magics = _MAGIC.get(ext)
    if magics and not any(head.startswith(m) for m in magics):
        raise UploadError(f"O conteúdo do arquivo .{ext} não corresponde ao formato esperado.")


def _inspect_zip(path, max_unzipped_mb):
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise UploadError("O arquivo .zip está corrompido ou inválido.") from exc
    with zf:
        infos = zf.infolist()
        if not infos:
            raise UploadError("O arquivo .zip está vazio.")
        if len(infos) > 20000:
            raise UploadError("O .zip tem arquivos demais (máximo 20.000).")
        total = 0
        for i, info in enumerate(infos):
            name = info.filename
            parts = [p for p in name.replace("\\", "/").split("/") if p]
            if name.startswith(("/", "\\")) or ".." in parts or (len(name) > 1 and name[1] == ":"):
                raise UploadError("O .zip contém caminhos inseguros.")
            if info.flag_bits & 0x1:
                raise UploadError("ZIP protegido por senha não é permitido, pois não pode ser verificado.")
            if info.is_dir():
                continue
            total += info.file_size
            if total > max_unzipped_mb * MB:
                raise UploadError(f"O conteúdo descompactado excede {max_unzipped_mb} MB.")
            if info.file_size > 50 * MB and info.compress_size and info.file_size / info.compress_size > 200:
                raise UploadError("O .zip tem taxa de compressão suspeita.")
            member_ext = ext_of(parts[-1]) if parts else ""
            if member_ext in BLOCKED_EXTS:
                raise UploadError(f"O .zip contém um arquivo não permitido (.{member_ext}). Executáveis e scripts de sistema são bloqueados.")
            if member_ext in NOT_INSPECTABLE:
                raise UploadError("O .zip contém outro arquivo compactado, que não pode ser verificado. Envie os arquivos diretamente.")
            if i < 5000 and info.file_size >= 4:
                with zf.open(info) as m:
                    if any(m.read(4).startswith(x) for x in _EXEC_MAGIC):
                        raise UploadError("O .zip contém um arquivo executável, que não é permitido.")


def _scan(path):
    """Se o ClamAV estiver instalado no servidor, escaneia o arquivo. Sem ClamAV, nenhum escaneamento é feito."""
    exe = shutil.which("clamscan")
    if not exe:
        return
    try:
        res = subprocess.run([exe, "--no-summary", path], capture_output=True, timeout=180)
    except subprocess.TimeoutExpired as exc:
        raise UploadError("Não foi possível concluir a verificação de segurança do arquivo. Tente novamente.") from exc
    if res.returncode == 1:
        raise UploadError("O arquivo foi bloqueado pela verificação antivírus.")


def save_asset_file(fs, directory, tmp_dir, max_mb, max_unzipped_mb=2048):
    ext = ext_of(fs.filename)
    if ext in BLOCKED_EXTS:
        raise UploadError("Executáveis e instaladores não são permitidos.")
    if ext in NOT_INSPECTABLE:
        raise UploadError("Envie seus arquivos em .zip (formatos .rar, .7z e similares não podem ser verificados).")
    if ext not in FILE_EXTS:
        raise UploadError("Tipo de arquivo não permitido para entrega.")
    _limit(_size(fs), max_mb, "Arquivo")

    tmp = os.path.join(tmp_dir, uuid.uuid4().hex)
    sha = hashlib.sha256()
    fs.stream.seek(0)
    with open(tmp, "wb") as out:
        while True:
            chunk = fs.stream.read(1024 * 1024)
            if not chunk:
                break
            sha.update(chunk)
            out.write(chunk)
    try:
        with open(tmp, "rb") as f:
            head = f.read(16)
        _check_head(ext, head)
        if ext == "zip":
            _inspect_zip(tmp, max_unzipped_mb)
        _scan(tmp)
        stored = uuid.uuid4().hex + ".dat"
        os.replace(tmp, os.path.join(directory, stored))
    except Exception:
        if os.path.exists(tmp):
            os.remove(tmp)
        raise
    return {
        "stored_name": stored,
        "original_name": secure_filename(fs.filename) or f"arquivo.{ext}",
        "ext": ext,
        "size": os.path.getsize(os.path.join(directory, stored)),
        "sha256": sha.hexdigest(),
    }


def remove_quietly(directory, *names):
    for n in names:
        if n:
            try:
                os.remove(os.path.join(directory, n))
            except OSError:
                pass
