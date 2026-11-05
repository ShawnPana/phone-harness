"""Turn a raw screen read into numbered tap targets.

Two sources feed this module:

- accessibility tree nodes from Android, which carry roles, clickability and
  the parent/child hierarchy;
- text boxes from OCR on iPhone (and the Android pixel fallback), which carry
  only a string and a box.

The output is one flat list of targets in reading order, each with an id that
matches the badge mark_screen() draws. Nothing here calls a model, a platform
API, or Pillow. It is pure data shaping, so the same rules are testable without
a phone.
"""
import hashlib
import re
from statistics import median

_SYSTEM_PACKAGE = "com.android.systemui"
_INPUT_CLASSES = ("edittext", "autocompletetextview", "searchview", "textfield")
_TOGGLE_CLASSES = ("checkbox", "radiobutton", "switch", "togglebutton",
                   "materialswitch")
_SLIDER_CLASSES = ("seekbar", "slider", "ratingbar")
_ICON_CLASSES = ("imagebutton", "imageview", "floatingactionbutton")
_SPECIFIC_KINDS = frozenset({"icon", "button", "input", "toggle"})
_LABEL_FIELDS = ("label", "text", "desc", "id_hint", "kind", "role")


# --- public entry points ----------------------------------------------------

def from_tree(nodes, include_system=False, screen=None):
    """Build targets from Android accessibility nodes.

    `nodes` is the flat list from Android._tree(); parent is the index of the
    node's parent in that list. `screen` is {x, y, w, h} for the display, used
    to drop off-screen nodes and the status/navigation bars.
    """
    children = {}
    for i, node in enumerate(nodes):
        children.setdefault(node.get("parent"), []).append(i)

    candidates = {i for i, node in enumerate(nodes) if _is_candidate(node)}
    bars = {i for i in candidates if _is_bar_container(nodes[i])}
    records = []
    for i in sorted(candidates):
        node = nodes[i]
        if not _on_screen(node, screen):
            continue
        if not include_system and node.get("package") == _SYSTEM_PACKAGE:
            continue
        label, source = _label(i, nodes, children, candidates)
        kind = _kind(node)
        if i in bars:
            # An app-bar container is a layout, not a tap target. Its icon and
            # button children are separate candidates and stay.
            continue
        if (kind == "control" and _inside_bar(i, nodes, bars)
                and not (node.get("text") or "").strip()
                and not (node.get("desc") or "").strip()
                and not _searchish(node)):
            # A clickable title wrapper inside the app bar. Keep search
            # headers: "header_view" is the real search field.
            continue
        if kind == "scroll":
            # A scroll container is not a tap target. scroll(at=...) accepts
            # any row box, so numbering the container only adds a badge that
            # sits on top of the first row.
            continue
        area = max(0, node.get("w", 0)) * max(0, node.get("h", 0))
        if (label is None and kind == "control" and screen
                and area > 0.8 * screen["w"] * screen["h"]):
            continue
        if (label is None and kind == "control" and screen
                and node["h"] <= max(24, screen["h"] * 0.05)
                and node["w"] >= 0.8 * screen["w"]):
            # A thin full-width unlabeled clickable row is a divider, not a
            # control. Real rows carry a label from their child text.
            continue
        target = _target(node, label, source, kind)
        records.append({
            "index": i,
            "target": target,
            "label": label,
            "label_source": source,
            "kind": kind,
            "depth": node.get("depth", 0),
            "area": area,
            "left": node["x"] - node["w"] / 2,
            "top": node["y"] - node["h"] / 2,
            "right": node["x"] + node["w"] / 2,
            "bottom": node["y"] + node["h"] / 2,
        })

    kept = _dedupe(records)
    kept = _drop_wrappers(kept, children)
    return _assign_ids([record["target"] for record in kept])


def from_text(boxes, include_system=False, screen=None):
    """Build targets from OCR text boxes.

    Each box is one target. iPhone has no tree for phone content, so text is
    the only thing this source can enumerate; icons stay for the agent's own
    vision and the pixel ruler from mark_screen(grid=True).
    """
    out = []
    for box in boxes:
        text = (box.get("text") or "").strip()
        if not text:
            continue
        if not include_system and _in_system_band(box, screen):
            continue
        source = box.get("source", "pixels")
        out.append({
            "id": 0,
            "label": text,
            "label_source": "text" if source == "tree" else "ocr",
            "kind": "text",
            "role": "text",
            "x": box["x"], "y": box["y"],
            "w": box["w"], "h": box["h"],
            "source": source,
            "enabled": True,
            "long_clickable": False,
            "scrollable": False,
            "checked": None,
            "selected": False,
        })
    return _assign_ids(out)


def coverage(targets):
    """'tree' when unlabeled controls are included, else 'text'."""
    return "tree" if any(t.get("source") == "tree" for t in targets) else "text"


def fingerprint(target):
    """A stable string for one target, used by mark_id()."""
    values = (
        target.get("role", ""),
        target.get("label") or "",
        target.get("kind", ""),
        int(round(float(target.get("x", 0)))),
        int(round(float(target.get("y", 0)))),
        int(round(float(target.get("w", 0)))),
        int(round(float(target.get("h", 0)))),
    )
    return "|".join(str(v) for v in values)


def mark_id(app, orientation, img_px, coverage_name, targets):
    """A short id that is stable while the screen is unchanged."""
    parts = [
        str(app or ""),
        orientation,
        f"{img_px[0]}x{img_px[1]}",
        coverage_name,
    ]
    parts.extend(sorted(fingerprint(t) for t in targets))
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()
    return "m" + digest[:5]


# --- candidate and label rules ---------------------------------------------

def _is_candidate(node):
    if (node.get("clickable") or node.get("long_clickable")
            or node.get("checkable") or node.get("scrollable")):
        return True
    cls = (node.get("class") or "").lower()
    if any(k in cls for k in _INPUT_CLASSES):
        return True
    if "button" in cls:
        return True
    return False


def _is_bar_container(node):
    rid = (node.get("id") or "").lower()
    return any(rid.endswith(suffix) for suffix in (
        "action_bar_container", "actionbar", "appbar", "toolbar"))


def _inside_bar(index, nodes, bars):
    parent = nodes[index].get("parent")
    seen = set()
    while parent is not None and parent not in seen and parent < len(nodes):
        if parent in bars:
            return True
        seen.add(parent)
        parent = nodes[parent].get("parent")
    return False


def _searchish(node):
    text = " ".join((node.get("id") or "", node.get("text") or "",
                     node.get("desc") or "")).lower()
    return "search" in text or "header" in text


def _kind(node):
    cls = (node.get("class") or "").lower()
    if any(k in cls for k in _INPUT_CLASSES):
        return "input"
    if any(k in cls for k in _TOGGLE_CLASSES):
        return "toggle"
    if any(k in cls for k in _SLIDER_CLASSES):
        return "slider"
    if (any(k in cls for k in ("tablayout", "tabwidget", "tabhost", "tabview",
                               "tabitem"))
            or cls.endswith("$tab") or cls.endswith(".tab")):
        return "tab"
    if any(k in cls for k in _ICON_CLASSES):
        return "icon"
    if "button" in cls:
        return "button"
    if node.get("scrollable"):
        return "scroll"
    if "textview" in cls:
        return "text"
    return "control"


def _label(index, nodes, children, candidates):
    node = nodes[index]
    text = (node.get("text") or "").strip()
    if text:
        return text, "text"
    desc = (node.get("desc") or "").strip()
    if desc:
        return desc, "desc"

    parts = []
    sources = set()
    blocked = set()
    for child in _descendants(index, children):
        if child in blocked:
            continue
        if child in candidates:
            # A clickable child is its own target; do not mine its text for
            # the parent. Block its whole subtree, not just the child, or a
            # scroll container would inherit every row label below it.
            blocked.update(_descendants(child, children))
            continue
        child_node = nodes[child]
        child_text = (child_node.get("text") or "").strip()
        child_desc = (child_node.get("desc") or "").strip()
        value = child_text or child_desc
        if not value or value in parts:
            continue
        parts.append(value)
        sources.add("text" if child_text else "desc")
        if len(parts) >= 3:
            break
    if parts:
        return " ".join(parts), ("text" if "text" in sources else "desc")

    hint = _id_hint(node.get("id"))
    if hint:
        return hint, "id_hint"
    return None, None


def _id_hint(resource_id):
    if not resource_id:
        return ""
    tail = str(resource_id).rsplit("/", 1)[-1]
    tail = re.sub(r"^(btn|button|iv|img|image|icon|action)[_-]", "",
                  tail, flags=re.I)
    tail = re.sub(r"[_-](btn|button|iv|img|image|icon)$", "",
                  tail, flags=re.I)
    return tail.replace("_", " ").replace("-", " ").strip()


def _target(node, label, source, kind):
    target = {
        "id": 0,
        "label": label,
        "label_source": source,
        "kind": kind,
        "role": node.get("class", ""),
        "x": node["x"], "y": node["y"],
        "w": node["w"], "h": node["h"],
        "source": "tree",
        "enabled": bool(node.get("enabled", True)),
        "long_clickable": bool(node.get("long_clickable", False)),
        "scrollable": bool(node.get("scrollable", False)),
        "checked": node.get("checked"),
        "selected": bool(node.get("selected", False)),
    }
    for key in ("package", "text", "desc"):
        value = (node.get(key) or "").strip()
        if value:
            target[key] = value
    hint = _id_hint(node.get("id"))
    if hint:
        target["id_hint"] = hint
    return target


# --- geometry, ordering, dedupe --------------------------------------------

def _on_screen(node, screen):
    if node.get("w", 0) <= 0 or node.get("h", 0) <= 0:
        return False
    if not screen:
        return True
    x, y = node["x"], node["y"]
    return (screen["x"] <= x < screen["x"] + screen["w"]
            and screen["y"] <= y < screen["y"] + screen["h"])


def _in_system_band(box, screen):
    if not screen:
        return False
    top = screen["y"] + screen["h"] * 0.04
    bottom = screen["y"] + screen["h"] * 0.97
    return not (top <= box["y"] <= bottom)


def _descendants(index, children):
    stack = list(reversed(children.get(index, [])))
    while stack:
        child = stack.pop()
        yield child
        stack.extend(reversed(children.get(child, [])))


def _dedupe(records):
    """Keep one target per near-identical box, preferring the useful one."""
    ordered = sorted(records, key=_score, reverse=True)
    kept = []
    for record in ordered:
        if any(_iou(record, other) > 0.9 for other in kept):
            continue
        kept.append(record)
    return kept


def _score(record):
    return (
        record["label"] is not None,
        record["label_source"] in ("text", "desc"),
        record["kind"] in _SPECIFIC_KINDS,
        record["depth"],
        -record["area"],
    )


def _drop_wrappers(records, children):
    """Drop an unlabeled wrapper whose only clickable child fills it."""
    dropped = set()
    for record in records:
        if record["label"] is not None or record["kind"] != "control":
            continue
        inside = set(_descendants(record["index"], children))
        kids = [other for other in records
                if other["index"] in inside and other["index"] not in dropped]
        if len(kids) == 1 and kids[0]["area"] > 0.9 * record["area"]:
            dropped.add(record["index"])
    return [record for record in records if record["index"] not in dropped]


def _iou(a, b):
    left = max(a["left"], b["left"])
    top = max(a["top"], b["top"])
    right = min(a["right"], b["right"])
    bottom = min(a["bottom"], b["bottom"])
    if right <= left or bottom <= top:
        return 0.0
    intersection = (right - left) * (bottom - top)
    union = a["area"] + b["area"] - intersection
    return intersection / union if union else 0.0


def _assign_ids(targets):
    if not targets:
        return []
    row_tol = max(24.0, median(t["h"] for t in targets) * 0.5)
    ordered = sorted(targets, key=lambda t: (
        t["y"], t["x"], t["role"], str(t["label"]), t["w"], t["h"]))
    rows = []
    for target in ordered:
        if rows and abs(target["y"] - rows[-1][0]["y"]) <= row_tol:
            rows[-1].append(target)
        else:
            rows.append([target])
    out = []
    for row in rows:
        out.extend(sorted(row, key=lambda t: (
            t["x"], t["y"], t["role"], str(t["label"]), t["w"], t["h"])))
    for i, target in enumerate(out, 1):
        target["id"] = i
    return out


# --- filters used by helpers.targets() and tap_target() ---------------------

def matches(target, query, exact=False):
    """Does `target` match a query across its human-facing fields?"""
    if query is None:
        return True
    q = str(query).lower()
    for field in _LABEL_FIELDS:
        value = target.get(field)
        if not value:
            continue
        value = str(value).lower()
        if (value == q) if exact else (q in value):
            return True
    return False


def filter_targets(targets, query=None, kind=None, exact=False):
    """Filter a target list without changing its ids."""
    out = list(targets)
    if kind:
        kinds = {kind} if isinstance(kind, str) else set(kind)
        out = [t for t in out if t["kind"] in kinds]
    if query is not None:
        out = [t for t in out if matches(t, query, exact=exact)]
    return out
