"""Where phone-harness keeps what the user chose and what it learned.

Two kinds of thing, kept apart because they live differently:

  config   intent — the default platform, the adb binary, how often to poke
           a phone awake. Hand-editable, small, worth putting in dotfiles.
             ~/.config/phone-harness/config.json
  state    what the harness learned — remembered phones and which is
           primary, the pid of a running awake session. Machine-managed,
           rebuildable, never worth backing up.
             ~/.local/state/phone-harness/devices.json
             ~/.local/state/phone-harness/run/awake.pid

XDG_CONFIG_HOME / XDG_STATE_HOME are honoured; PHONE_HARNESS_HOME moves both
roots under one directory (tests, sandboxes).

One rule for every setting: explicit argument, else environment variable,
else config file, else built-in default. `get("android.poke_every")` looks
at PHONE_HARNESS_ANDROID_POKE_EVERY, then config.json, then DEFAULTS. Env
vars are per-call overrides; nothing requires them.

Files are versioned, read forgivingly (a corrupt file is a warning and
defaults, never a crash) and written atomically. Unknown keys survive a
round trip. This module owns the disk; backends ask it.
"""
import json
import os
import sys
import tempfile
from pathlib import Path

VERSION = 1

DEFAULTS = {
    "platform": "ios",
    "android": {
        "adb": "adb",          # the binary; a path if it is not on PATH
        "poke_every": 25,      # seconds between keep-awake pokes
        "mirror": True,        # open scrcpy during `android awake` if installed
    },
}

# Settings that historically had their own environment variable.
_ENV_ALIASES = {
    "platform": ["PHONE_HARNESS_PLATFORM"],
    "android.adb": ["PHONE_HARNESS_ADB"],
}


# --- where -------------------------------------------------------------------

def config_dir():
    home = os.environ.get("PHONE_HARNESS_HOME")
    if home:
        return Path(home) / "config"
    base = os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config"
    return Path(base) / "phone-harness"


def state_dir():
    home = os.environ.get("PHONE_HARNESS_HOME")
    if home:
        return Path(home) / "state"
    base = os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state"
    return Path(base) / "phone-harness"


def run_dir():
    return state_dir() / "run"


def paths():
    return {"config": config_dir() / "config.json",
            "devices": state_dir() / "devices.json",
            "run": run_dir()}


# --- files -------------------------------------------------------------------

def _read(path, empty):
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError("not an object")
        return data
    except FileNotFoundError:
        return dict(empty)
    except (OSError, ValueError) as e:
        print(f"phone-harness: ignoring unreadable {path} ({e}); using defaults",
              file=sys.stderr)
        return dict(empty)


def _write(path, data):
    """Atomic: a crash mid-write leaves the old file, not half a new one."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"version": VERSION, **{k: v for k, v in data.items() if k != "version"}}
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def load():
    return _read(paths()["config"], {})


def save(data):
    _write(paths()["config"], data)


# --- settings ----------------------------------------------------------------

def _dig(d, key):
    for part in key.split("."):
        if not isinstance(d, dict) or part not in d:
            return None
        d = d[part]
    return d


def _coerce(text, like):
    """An env-var string, shaped like the default it overrides."""
    if isinstance(like, bool):
        return text.strip().lower() in ("1", "true", "yes", "on")
    if isinstance(like, int):
        try:
            return int(text)
        except ValueError:
            return like
    return text


def _env_names(key):
    return _ENV_ALIASES.get(key, []) + [
        "PHONE_HARNESS_" + key.upper().replace(".", "_")]


def lookup(key, default=None):
    """(value, source) for a dotted key; source is 'env:NAME', 'file',
    'default' or 'unset'."""
    built_in = _dig(DEFAULTS, key)
    for name in _env_names(key):
        if name in os.environ:
            return _coerce(os.environ[name], built_in), f"env:{name}"
    v = _dig(load(), key)
    if v is not None:
        return v, "file"
    if built_in is not None:
        return built_in, "default"
    return default, "unset"


def get(key, default=None):
    return lookup(key, default)[0]


def set(key, value):
    """Set a dotted key in config.json. Strings that look like JSON scalars
    ('true', '25') become that value, so the CLI can pass text through."""
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, (bool, int, float)) or parsed is None:
                value = parsed
        except ValueError:
            pass
    data = load()
    d = data
    parts = key.split(".")
    for part in parts[:-1]:
        if not isinstance(d.get(part), dict):
            d[part] = {}
        d = d[part]
    d[parts[-1]] = value
    save(data)
    return value


def unset(key):
    data = load()
    d = data
    parts = key.split(".")
    for part in parts[:-1]:
        d = d.get(part) if isinstance(d, dict) else None
        if d is None:
            return
    d.pop(parts[-1], None)
    save(data)


def known_keys():
    out = []
    def walk(d, prefix=""):
        for k, v in d.items():
            if isinstance(v, dict):
                walk(v, prefix + k + ".")
            else:
                out.append(prefix + k)
    walk(DEFAULTS)
    return out


# --- devices (state) ---------------------------------------------------------

def devices():
    """The remembered-devices file, whole: {"version", "<kind>": {...}}.
    Migrates the pre-config android.json once, if it is found."""
    p = paths()["devices"]
    data = _read(p, {})
    if not data.get("android"):
        legacy = config_dir() / "android.json"
        if legacy.exists():
            old = _read(legacy, {})
            if old.get("phones") or old.get("primary"):
                data["android"] = {"primary": old.get("primary"),
                                   "phones": old.get("phones") or {}}
                _write(p, data)
                legacy.rename(legacy.with_suffix(".json.migrated"))
    return data


def devices_of(kind):
    """The section for one backend, always shaped {"primary", "phones"}."""
    sect = devices().get(kind) or {}
    return {"primary": sect.get("primary"), "phones": sect.get("phones") or {}}


def save_devices_of(kind, section):
    data = devices()
    data[kind] = {"primary": section.get("primary"),
                  "phones": section.get("phones") or {}}
    _write(paths()["devices"], data)


# --- CLI (phone-harness config ...) -----------------------------------------

CLI_USAGE = """Usage:
  phone-harness config                 every setting, its value, and where it came from
  phone-harness config get KEY
  phone-harness config set KEY VALUE   e.g. config set platform android
  phone-harness config unset KEY
  phone-harness config path            where the files live
"""


def cli(args):
    cmd = args[0] if args else None
    if cmd is None:
        for key in known_keys():
            value, source = lookup(key)
            print(f"{key:<22} {json.dumps(value):<10} ({source})")
        for k, v in load().items():
            if k != "version" and not isinstance(v, dict) and k not in known_keys():
                print(f"{k:<22} {json.dumps(v):<10} (file, unknown key)")
        return 0
    if cmd == "get" and len(args) == 2:
        value, source = lookup(args[1])
        print(json.dumps(value) if source != "unset" else f"{args[1]}: unset")
        return 0 if source != "unset" else 1
    if cmd == "set" and len(args) == 3:
        if args[1] == "platform" and args[2] not in ("ios", "android"):
            print("platform must be ios or android"); return 2
        v = set(args[1], args[2])
        print(f"{args[1]} = {json.dumps(v)}")
        return 0
    if cmd == "unset" and len(args) == 2:
        unset(args[1]); print(f"{args[1]} unset"); return 0
    if cmd == "path":
        for k, v in paths().items():
            print(f"{k:<8} {v}")
        return 0
    print(CLI_USAGE)
    return 2
