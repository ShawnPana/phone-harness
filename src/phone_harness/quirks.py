"""Host-OS quirks that change what the harness may safely do.

Kept apart from the backends because this is not "how to send an event", it is
"whether sending it can be trusted at all on this Mac".
"""
import os
import platform


def macos_version():
    """(major, minor) of the host macOS, or (0, 0) when it cannot be read."""
    try:
        parts = platform.mac_ver()[0].split(".")
        return int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        return (0, 0)


def _env_override(name):
    return os.environ.get(name, "").lower() in ("1", "true", "yes")


#: macOS 26 is where iPhone Mirroring began discarding synthetic scroll.
SCROLL_BROKEN_FROM = 26

SCROLL_DEAD_HINT = (
    "iPhone Mirroring on macOS {ver} discards synthetic scroll and vertical "
    "drag, so this gesture cannot move the screen — and would land as a TAP "
    "at the point the finger went down, opening whatever is under it.\n"
    "Navigate by taps instead: a search field, or tap_index_letter() on the "
    "A-Z index bar of a sectioned list.\n"
    "See https://github.com/ShawnPana/phone-harness/issues/51 — set "
    "PHONE_HARNESS_FORCE_SCROLL=1 to send it anyway."
)


def touch_scroll_is_delivered():
    """False when a synthetic scroll gesture is known to be dropped by the host.

    The block is by macOS version rather than by probing, because the probe
    would have to be the very gesture that misfires: a flick whose motion is
    discarded arrives as a tap, so a "does scrolling work?" experiment opens a
    random row on the phone every time the answer is no.

    PHONE_HARNESS_FORCE_SCROLL=1 sends it regardless, for a build where it
    works again.
    """
    if _env_override("PHONE_HARNESS_FORCE_SCROLL"):
        return True
    return macos_version()[0] < SCROLL_BROKEN_FROM


def scroll_dead_hint():
    major, minor = macos_version()
    return SCROLL_DEAD_HINT.format(ver=f"{major}.{minor}")
