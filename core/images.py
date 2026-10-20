"""Image helpers: orientation and where each thumbnail version is stored."""
from PIL import ImageOps


def fix_orientation(img):
    try:
        img = ImageOps.exif_transpose(img)
    except Exception:
        pass
    return img


def _under(prefix: str, r2_key: str, suffix: str = "") -> str:
    if r2_key.startswith("images/"):
        return prefix + r2_key[len("images/"):] + suffix
    return prefix + r2_key + suffix


def thumb_r2_key(r2_key: str) -> str:
    """maps images/foo/bar.jpg → thumbs/v2/foo/bar.jpg"""
    return _under("thumbs/v2/", r2_key)


def nowm_thumb_r2_key(r2_key: str) -> str:
    """maps images/foo/bar.jpg → thumbs/nowm/foo/bar.jpg (no-watermark POS thumbnails)"""
    return _under("thumbs/nowm/", r2_key)


def grid_thumb_r2_key(r2_key: str) -> str:
    """maps images/foo/bar.jpg → thumbs/gridwm/foo/bar.jpg.webp (gallery thumbnails, WebP, with the
    full watermark pattern baked in; the older thumbs/grid/ files only had the centered logo)"""
    return _under("thumbs/gridwm/", r2_key, ".webp")


def admin_thumb_r2_key(r2_key: str) -> str:
    """maps images/foo/bar.jpg → admin_thumbs/foo/bar.jpg (studio / package thumbnails)"""
    return _under("admin_thumbs/", r2_key)
