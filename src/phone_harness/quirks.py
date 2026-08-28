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


#: macOS 26 is where iPhone Mirroring began reading a vertical touch-drag as a
#: TAP at the point the finger went down, rather than as a scroll — the same
#: regression that once looked like scrolling itself was dead (issue #51).
#: It turned out to be specific to *drags*: a scroll-wheel event shaped like a
#: real trackpad gesture (continuous + phase began/changed/ended, nonzero
#: delta on every phase, see mirror.scroll_wheel) is still honoured on macOS
#: 26, so scroll()/scroll_screen()/scroll_collect() need no gate here at all
#: any more — they always go through that gesture. This flag is what is left
#: of the regression: it tells swipe('up'|'down') to route through the same
#: scroll-wheel gesture instead of firing a raw drag that would land as a tap.
VERTICAL_DRAG_BROKEN_FROM = 26

VERTICAL_DRAG_HINT = (
    "iPhone Mirroring on macOS {ver} reads a vertical touch-drag as a TAP at "
    "the point the finger went down, not a scroll — so swipe('up'|'down') "
    "sends the phased scroll-wheel gesture mirror.scroll_wheel() uses "
    "instead of a raw drag. Horizontal drags are unaffected.\n"
    "See https://github.com/ShawnPana/phone-harness/issues/51 — set "
    "PHONE_HARNESS_FORCE_SCROLL=1 to send the raw drag anyway."
)


def vertical_drag_is_delivered():
    """False when a raw vertical touch-drag is known to land as a tap rather
    than a scroll on this host.

    The block is by macOS version rather than by probing, because the probe
    would have to be the very drag that misfires: one whose motion is read as
    a tap opens a random row on the phone every time the answer is no.

    PHONE_HARNESS_FORCE_SCROLL=1 sends the raw drag regardless, for a build
    where this is fixed.
    """
    if _env_override("PHONE_HARNESS_FORCE_SCROLL"):
        return True
    return macos_version()[0] < VERTICAL_DRAG_BROKEN_FROM


def vertical_drag_hint():
    major, minor = macos_version()
    return VERTICAL_DRAG_HINT.format(ver=f"{major}.{minor}")
