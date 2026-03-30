import zerovm_sphinx_theme

project = "Synarius Apps"
extensions = ["sphinx_needs"]
html_theme = "zerovm"
html_theme_path = [zerovm_sphinx_theme.theme_path]

needs_types = [
    {"directive": "need", "title": "Requirement", "prefix": "NEED_", "color": "#E8DAEF", "style": "node"},
    {"directive": "sys", "title": "System Requirement", "prefix": "SYS_", "color": "#BFD8D2", "style": "node"},
    {"directive": "arch", "title": "Architecture Requirement", "prefix": "ARCH_", "color": "#F2D7D5", "style": "node"},
    {"directive": "comp", "title": "Component Requirement", "prefix": "COMP_", "color": "#D6EAF8", "style": "node"},
]

needs_id_regex = "^[A-Z0-9-]{5,}$"

needs_default_layout = "synarius_key_title"
needs_layouts = {
    "synarius_key_title": {
        "grid": "simple",
        "layout": {
            "head": [
                '<<meta_id()>> **<<meta("title")>>** '
                '<<collapse_button("meta", '
                'collapsed="icon:arrow-down-circle", visible="icon:arrow-right-circle", '
                'initial=False)>>'
            ],
            "meta": ['**status**: <<meta("status")>>'],
            "footer": [],
        },
    }
}
