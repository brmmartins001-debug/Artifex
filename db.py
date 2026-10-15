"""Acesso ao SQLite, criação do esquema e dados iniciais (categorias e políticas)."""
import os
import sqlite3
import unicodedata
from datetime import datetime, timezone

from flask import current_app, g
from werkzeug.security import generate_password_hash


def utcnow():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fold(text):
    """Minúsculas e sem acentos: 'Veículos' -> 'veiculos' (busca insensível a acentos)."""
    if text is None:
        return ""
    return "".join(c for c in unicodedata.normalize("NFKD", str(text)) if not unicodedata.combining(c)).lower()


def get_db():
    if "db" not in g:
        con = sqlite3.connect(current_app.config["DATABASE"], timeout=15)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys = ON")
        con.execute("PRAGMA journal_mode = WAL")
        con.create_function("fold", 1, fold, deterministic=True)
        g.db = con
    return g.db


def close_db(exc=None):
    con = g.pop("db", None)
    if con is not None:
        if exc is not None:
            con.rollback()
        con.close()


def query(sql, args=(), one=False):
    cur = get_db().execute(sql, args)
    rows = cur.fetchall()
    cur.close()
    if one:
        return rows[0] if rows else None
    return rows


def execute(sql, args=()):
    return get_db().execute(sql, args)


def commit():
    get_db().commit()


def audit(action, entity, entity_id="", detail="", actor_id=None):
    from flask import request

    execute(
        "INSERT INTO audit_log (actor_id, action, entity, entity_id, detail, ip, created_at) VALUES (?,?,?,?,?,?,?)",
        (actor_id, action, entity, str(entity_id), detail[:1000], request.remote_addr or "", utcnow()),
    )


def notify(user_id, kind, title, body="", link=""):
    execute(
        "INSERT INTO notifications (user_id, kind, title, body, link, created_at) VALUES (?,?,?,?,?,?)",
        (user_id, kind, title, body, link, utcnow()),
    )


CATEGORIES = [
    ("modelos-3d", "Modelos 3D", "3d", "box"),
    ("personagens", "Personagens", "3d", "user"),
    ("veiculos", "Veículos", "3d", "car"),
    ("ambientes-cenarios", "Ambientes e cenários", "3d", "mountain"),
    ("texturas-materiais", "Texturas e materiais", "texture", "texture"),
    ("sprites-pixel-art", "Sprites e pixel art", "2d", "image"),
    ("tilesets", "Tilesets", "2d", "grid"),
    ("animacoes", "Animações", "anim", "play"),
    ("shaders-efeitos", "Shaders e efeitos", "shader", "sparkles"),
    ("interfaces-huds-icones", "Interfaces, HUDs e ícones", "ui", "monitor"),
    ("audio", "Áudio, músicas e efeitos sonoros", "audio", "volume"),
    ("fontes", "Fontes", "font", "type"),
    ("particulas-vfx", "Partículas e efeitos visuais", "vfx", "flame"),
]

_DRAFT = "RASCUNHO — este texto é um modelo inicial e precisa de revisão jurídica antes do lançamento público."

POLICIES = [
    ("termos", "Termos de uso", "Ao usar o Artifex você concorda com estes termos. O Artifex é um marketplace de recursos digitais para desenvolvimento de jogos: criadores publicam, o Artifex analisa antes de exibir e usuários obtêm recursos conforme a licença de cada um.\n\nVocê é responsável pelas informações da sua conta e pelo conteúdo que publica. É proibido publicar conteúdo sem ter os direitos necessários, malware, executáveis ou material ilegal."),
    ("privacidade", "Política de privacidade", "Coletamos os dados necessários para operar a conta (nome, e-mail, conteúdo publicado, histórico de downloads e de pesquisas). Você pode limpar o histórico de pesquisas e excluir sua conta nas configurações.\n\nNão armazenamos dados de cartão. Pagamentos, quando habilitados, serão tratados por um provedor de pagamentos externo."),
    ("licencas", "Licenças de uso", "Cada recurso informa a licença escolhida pelo criador (ex.: uso pessoal e comercial, CC0, CC BY). A licença aplicável é a exibida na página do recurso no momento da obtenção e registrada na sua biblioteca.\n\nRevender ou redistribuir os arquivos originais não é permitido, salvo se a licença disser o contrário."),
    ("regras-criadores", "Regras para criadores", "Publique apenas conteúdo próprio ou com autorização de distribuição. Informe formatos, compatibilidade e requisitos com precisão. Todo recurso passa por análise antes de ser exibido; a equipe pode pedir alterações, rejeitar, suspender ou remover recursos, sempre com um motivo registrado."),
    ("reembolsos", "Política de reembolsos", "Os pagamentos ainda não estão habilitados no Artifex; esta política será definida antes da ativação das compras. Recursos gratuitos não geram cobrança."),
    ("diretrizes", "Diretrizes da comunidade", "Seja respeitoso. Não publique conteúdo ofensivo, ilegal ou que viole direitos de terceiros. Use o botão de denúncia na página do recurso para avisar a equipe."),
    ("verificacao", "Política de verificação", "O selo Artifex Verified ainda não está disponível. Quando for lançado, será concedido apenas após análise da equipe, e pagar uma taxa não garantirá a verificação."),
    ("planos", "Condições dos planos", "Planos de assinatura ainda não estão disponíveis. As condições serão publicadas antes do lançamento."),
]


def init_db():
    path = current_app.config["DATABASE"]
    con = sqlite3.connect(path, timeout=15)
    con.row_factory = sqlite3.Row
    with open(os.path.join(os.path.dirname(__file__), "schema.sql"), encoding="utf-8") as f:
        con.executescript(f.read())
    now = utcnow()
    for i, (slug, name, kind, icon) in enumerate(CATEGORIES):
        con.execute(
            "INSERT OR IGNORE INTO categories (slug, name, kind, icon, position) VALUES (?,?,?,?,?)",
            (slug, name, kind, icon, i),
        )
    for slug, title, body in POLICIES:
        con.execute(
            "INSERT OR IGNORE INTO policies (slug, title, body, updated_at) VALUES (?,?,?,?)",
            (slug, title, _DRAFT + "\n\n" + body, now),
        )
    # Administrador inicial por variável de ambiente (apenas se ainda não houver nenhum admin).
    email = current_app.config.get("ADMIN_EMAIL")
    password = current_app.config.get("ADMIN_PASSWORD")
    if email and password and len(password) >= 10:
        has_admin = con.execute("SELECT 1 FROM users WHERE role='admin' LIMIT 1").fetchone()
        if not has_admin and not con.execute("SELECT 1 FROM users WHERE email=?", (email.lower(),)).fetchone():
            con.execute(
                "INSERT INTO users (email, password_hash, display_name, handle, role, created_at) VALUES (?,?,?,?,?,?)",
                (email.lower(), generate_password_hash(password), "Administrador", "admin", "admin", now),
            )
    con.commit()
    con.close()
