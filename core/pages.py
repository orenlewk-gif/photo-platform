"""Page templates in templates/: HTML files with __NAME__ markers filled in from Python."""
import os
import re

TEMPLATE_DIR = "templates"
_MARKER = re.compile(r"__([A-Z][A-Z0-9_]*)__")


def render_page(name: str, **values) -> str:
    """templates/<name> with each __NAME__ replaced by str(values["name"]), all in one pass
    (so a value can never be mistaken for another marker). A missing value is an error."""
    with open(os.path.join(TEMPLATE_DIR, name), encoding="utf-8") as f:
        text = f.read()

    def fill(m):
        key = m.group(1).lower()
        if key not in values:
            raise KeyError(f"{name}: no value for __{m.group(1)}__")
        return str(values[key])
    return _MARKER.sub(fill, text)


def site_header() -> str:
    """The site header (logo and menu, the gallery's look): templates/site_header.html, for pages
    outside the gallery (the partner page, the policy pages)."""
    with open(os.path.join(TEMPLATE_DIR, "site_header.html"), encoding="utf-8") as f:
        return f.read()


def site_footer() -> str:
    """The site footer (business details and policy links, the gallery's): templates/site_footer.html."""
    with open(os.path.join(TEMPLATE_DIR, "site_footer.html"), encoding="utf-8") as f:
        return f.read()
