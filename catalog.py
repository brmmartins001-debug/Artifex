"""Constantes do catálogo: licenças, engines, formatos e campos técnicos por categoria."""

STATUS_LABELS = {
    "draft": "Rascunho",
    "submitted": "Enviado para análise",
    "in_review": "Em análise",
    "changes_requested": "Alterações solicitadas",
    "rejected": "Rejeitado",
    "approved": "Aprovado",
    "published": "Publicado",
    "suspended": "Suspenso",
    "removed": "Removido",
}

EDITABLE_STATUSES = ("draft", "changes_requested")

LICENSES = [
    ("artifex-standard", "Artifex padrão — uso pessoal e comercial em projetos próprios"),
    ("personal", "Somente uso pessoal e não comercial"),
    ("cc0", "CC0 — domínio público"),
    ("cc-by-4", "CC BY 4.0 — atribuição"),
    ("cc-by-sa-4", "CC BY-SA 4.0 — atribuição e compartilhamento igual"),
    ("custom", "Personalizada (descreva em observações)"),
]
LICENSE_LABELS = dict(LICENSES)

ENGINES = ["Unity", "Unreal Engine", "Godot", "GameMaker", "Construct", "Defold", "Agnóstico (qualquer engine)"]

RIGHTS_BASIS = [
    ("owner", "Sou o(a) autor(a) e titular de todos os direitos"),
    ("licensed", "Tenho autorização/licença para distribuir (descreva)"),
]

FORMATS_BY_KIND = {
    "3d": ["FBX", "OBJ", "GLB", "GLTF", "BLEND", "DAE", "STL", "USD", "Unity Package", "Outro"],
    "audio": ["WAV", "MP3", "OGG", "FLAC", "MIDI"],
    "2d": ["PNG", "JPG", "PSD", "Aseprite", "Tiled (TMX/TSX)", "SVG"],
    "texture": ["PNG", "JPG", "TGA", "EXR", "SBSAR", "PSD"],
    "anim": ["FBX", "BLEND", "BVH", "GLB", "Unity Package", "Spine JSON"],
    "shader": ["GLSL", "HLSL", "Godot Shader", "Shader Graph", "Unity Package"],
    "ui": ["PNG", "SVG", "PSD", "Unity Package"],
    "font": ["TTF", "OTF", "WOFF", "WOFF2"],
    "vfx": ["Unity Package", "Unreal Asset", "Godot Scene", "PNG (sprite sheet)"],
}

# Campos técnicos que dependem do tipo da categoria: (chave, rótulo, tipo, opções)
SPEC_FIELDS = {
    "3d": [
        ("poly_count", "Contagem de polígonos", "number", None),
        ("rigged", "Possui rig/esqueleto", "bool", None),
        ("textures", "Texturas", "select", ["Sem texturas", "Texturas simples", "PBR (metal/roughness)", "PBR (specular/glossiness)"]),
        ("units", "Escala / unidade", "text", None),
    ],
    "audio": [
        ("duration", "Duração total", "text", None),
        ("sample_rate", "Taxa de amostragem", "select", ["44,1 kHz", "48 kHz", "96 kHz"]),
        ("bit_depth", "Profundidade", "select", ["16 bits", "24 bits", "32 bits (float)"]),
        ("loopable", "Possui loops", "bool", None),
        ("file_count", "Quantidade de faixas", "number", None),
    ],
    "2d": [
        ("sprite_size", "Tamanho dos sprites/tiles (ex.: 32x32)", "text", None),
        ("frame_count", "Quantidade de quadros/tiles", "number", None),
        ("palette", "Paleta de cores", "text", None),
    ],
    "texture": [
        ("resolution", "Resolução", "select", ["512 px", "1K", "2K", "4K", "8K"]),
        ("tileable", "Repetível (tileable)", "bool", None),
        ("maps", "Mapas incluídos (albedo, normal, roughness…)", "text", None),
    ],
    "anim": [
        ("fps", "Quadros por segundo", "number", None),
        ("clip_count", "Quantidade de animações", "number", None),
        ("rig", "Rig compatível", "text", None),
    ],
    "shader": [
        ("pipeline", "Pipeline de renderização", "select", ["Built-in", "URP", "HDRP", "Godot", "Outro"]),
        ("language", "Linguagem", "select", ["HLSL", "GLSL", "Shader Graph", "Godot Shading Language", "Outra"]),
        ("mobile_ok", "Otimizado para mobile", "bool", None),
    ],
    "ui": [
        ("resolution", "Resolução de referência", "text", None),
        ("vector", "Inclui arquivos vetoriais", "bool", None),
        ("icon_count", "Quantidade de elementos/ícones", "number", None),
    ],
    "font": [
        ("weights", "Pesos e estilos", "text", None),
        ("languages", "Idiomas/alfabetos suportados", "text", None),
        ("glyph_count", "Quantidade de glifos", "number", None),
    ],
    "vfx": [
        ("system", "Sistema de partículas", "text", None),
        ("looping", "Efeitos em loop", "bool", None),
        ("gpu", "Usa partículas por GPU", "bool", None),
    ],
}

SORTS = [
    ("relevance", "Relevância"),
    ("popular", "Popularidade"),
    ("recent", "Mais recentes"),
    ("price_asc", "Menor preço"),
    ("price_desc", "Maior preço"),
]

# Transições de moderação: ação -> (estados de origem, estado de destino, rótulo)
MODERATION_ACTIONS = {
    "start_review": (("submitted",), "in_review", "Assumir análise"),
    "request_changes": (("submitted", "in_review"), "changes_requested", "Solicitar alterações"),
    "reject": (("submitted", "in_review"), "rejected", "Rejeitar"),
    "approve": (("submitted", "in_review"), "approved", "Aprovar (sem publicar)"),
    "publish": (("approved",), "published", "Publicar"),
    "approve_publish": (("submitted", "in_review"), "published", "Aprovar e publicar"),
    "suspend": (("published",), "suspended", "Suspender"),
    "reinstate": (("suspended",), "published", "Reativar"),
    "remove": (("published", "suspended", "rejected", "approved"), "removed", "Remover"),
}
