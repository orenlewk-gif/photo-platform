"""Small text helpers: folder names, file ordering, storage keys."""
import os
import re


def clean_location(raw):
    cleaned = re.sub(r'^[\d\-_\s]+', '', raw)
    return cleaned.replace('-', ' ').replace('_', ' ').strip().title()


def natural_sort_key(path):
    name = os.path.basename(path).lower()
    return [int(t) if t.isdigit() else t for t in re.split(r'(\d+)', name)]


def pick_four(photos):
    """Four evenly spaced preview photos (all of them if there are four or fewer)."""
    n = len(photos)
    if n <= 4:
        return photos
    step = n / 4
    return [photos[int(i * step)] for i in range(4)]


def item_group_from_path(item):
    """Fallback: parse the sub-folder (group) from the path when the group field is empty."""
    parts = item.get("path", "").replace("\\", "/").split("/")
    try:
        img_idx = next(i for i, p in enumerate(parts) if p == "images")
        if len(parts) > img_idx + 4:
            return parts[img_idx + 3]
    except StopIteration:
        pass
    return ""


def folder_key(date, location, last_name):
    return f"{date}|{location}|{last_name}"


def to_r2_key(path: str) -> str:
    # Convert absolute local path to R2 key (relative, starting from 'images/')
    idx = path.find("images/")
    return path[idx:] if idx >= 0 else path
