"""`phone-harness host enroll`: turn a plugged-in iPhone into a farm phone.

One command, no hands, repeatable. It erases the phone, makes it supervised
by this host's organization, installs the farm profile (which carries the
farm Wi-Fi), and turns Developer Mode on. The phone comes back on its home
screen, on Wi-Fi, ready for the host's daemon.

Everything goes through Apple's own tools:
  cfgutil   (inside Apple Configurator.app)  erase, prepare, install-profile
  pymobiledevice3                              Developer Mode
cfgutil takes the supervision identity as the two files Apple Configurator
exports with "Unencrypted DER (.crt and .der, for Automator and cfgutil)":
the certificate and the private key. It accepts no other key encoding, and
it needs no keychain, so this runs from any shell.

One-time setup on the host, in Apple Configurator:
  Settings > Organizations > + > create the organization (skip Apple Business),
  then the gear menu > Export Supervision Identity > Unencrypted DER, saved
  into the supervision directory below. Keep a copy off the machine: the
  identity is what lets this host manage every phone it supervises.

Layout (config dir / supervision):
  identity.crt         the organization's certificate
  identity.der         its private key (mode 600)
  farm-iphone.mobileconfig   the restrictions and Wi-Fi profile
"""
import argparse
import json
import os
import plistlib
import subprocess
import sys
import time
import uuid
from pathlib import Path

from . import config

CFGUTIL_CANDIDATES = (
    "/Applications/Apple Configurator.app/Contents/MacOS/cfgutil",
    str(Path.home() / "Applications/Apple Configurator.app/Contents/MacOS/cfgutil"),
)
DEVICE_WAIT = 300        # seconds for the phone to come back after an erase
DEVMODE_WAIT = 240       # seconds for Developer Mode to report on after the reboot


class EnrollError(RuntimeError):
    pass


def supervision_dir():
    return config.config_dir() / "supervision"


def cfgutil():
    for c in CFGUTIL_CANDIDATES:
        if os.access(c, os.X_OK):
            return c
    raise EnrollError("Apple Configurator is not installed (it provides cfgutil). "
                      "Install it from the App Store on this Mac.")


def identity():
    d = supervision_dir()
    cert, key = d / "identity.crt", d / "identity.der"
    if not cert.is_file() or not key.is_file():
        raise EnrollError(f"no supervision identity at {d}: export it from Apple Configurator "
                          "(Organizations > Export Supervision Identity > Unencrypted DER) as "
                          "identity.crt and identity.der")
    return cert, key


def org_name(cert):
    """The organization's name, read from the certificate so it is never typed twice."""
    out = subprocess.run(["openssl", "x509", "-in", str(cert), "-inform", "DER", "-noout", "-subject"],
                         capture_output=True, text=True).stdout
    for part in out.replace("subject=", "").split(","):
        k, _, v = part.strip().partition("=")
        if k == "O":
            return v.strip()
    raise EnrollError("the supervision certificate names no organization")


def run(args, timeout=600):
    p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if p.returncode != 0 or "error:" in p.stdout or "error:" in p.stderr:
        raise EnrollError((p.stdout + p.stderr).strip().splitlines()[-1] if (p.stdout + p.stderr).strip()
                          else f"{args[1]} failed")
    return p.stdout


def devices(cfg):
    out = subprocess.run([cfg, "--timeout", "10", "--format", "JSON", "list"],
                         capture_output=True, text=True).stdout
    try:
        return json.loads(out).get("Output") or {}
    except ValueError:
        return {}


def wait_for_device(cfg, udid, seconds, what):
    t0 = time.time()
    while time.time() - t0 < seconds:
        for ecid, d in devices(cfg).items():
            if d.get("UDID") == udid:
                return ecid
        time.sleep(5)
    raise EnrollError(f"the phone did not come back {what} within {seconds}s")


def ecid_of(cfg, udid):
    for ecid, d in devices(cfg).items():
        if d.get("UDID") == udid:
            return ecid
    raise EnrollError(f"no iPhone {udid} on USB (cfgutil sees: "
                      f"{', '.join(d.get('UDID', '?') for d in devices(cfg).values()) or 'nothing'})")


def profile_with_wifi(profile, ssid, password):
    """The farm profile plus a Wi-Fi payload for this network, written next to it."""
    data = plistlib.loads(profile.read_bytes())
    payloads = [p for p in data["PayloadContent"] if p.get("PayloadType") != "com.apple.wifi.managed"]
    payloads.append({
        "PayloadType": "com.apple.wifi.managed",
        "PayloadIdentifier": data["PayloadIdentifier"] + ".wifi",
        "PayloadUUID": str(uuid.uuid4()).upper(),
        "PayloadVersion": 1,
        "PayloadDisplayName": f"Wi-Fi: {ssid}",
        "SSID_STR": ssid,
        "AutoJoin": True,
        "EncryptionType": "WPA2" if password else "None",
        "Password": password or "",
        "HIDDEN_NETWORK": False,
    })
    data["PayloadContent"] = payloads
    out = profile.with_name(profile.stem + ".with-wifi.mobileconfig")
    out.write_bytes(plistlib.dumps(data))
    os.chmod(out, 0o600)
    return out


def developer_mode(udid, on=True):
    exe = [sys.executable, "-m", "pymobiledevice3", "amfi"]
    status = lambda: subprocess.run(exe + ["developer-mode-status", "--udid", udid],
                                    capture_output=True, text=True).stdout.strip().lower()
    if status() == "true":
        return
    subprocess.run(exe + ["enable-developer-mode", "--udid", udid], capture_output=True, text=True)
    t0 = time.time()
    while time.time() - t0 < DEVMODE_WAIT:
        if status() == "true":
            return
        time.sleep(5)
    raise EnrollError("Developer Mode did not turn on; the phone may still be rebooting")


def enroll(udid, wifi_ssid=None, wifi_password=None, keep_data=False, log=print):
    cfg = cfgutil()
    cert, key = identity()
    org = org_name(cert)
    profile = supervision_dir() / "farm-iphone.mobileconfig"
    if not profile.is_file():
        raise EnrollError(f"no farm profile at {profile}")
    if wifi_ssid:
        profile = profile_with_wifi(profile, wifi_ssid, wifi_password)
    ident = ["-C", str(cert), "-K", str(key)]

    ecid = ecid_of(cfg, udid)
    log(f"phone {udid} (ECID {ecid}), organization {org!r}")
    if not keep_data:
        log("erasing…")
        run([cfg, "--timeout", "30", "-e", ecid, "erase"])
        wait_for_device(cfg, udid, DEVICE_WAIT, "after the erase")
        log("supervising and skipping setup…")
        run([cfg, "--timeout", "120", "-e", ecid] + ident +
            ["prepare", "--supervised", "--name", org, "--host-cert", str(cert),
             "--skip-all", "--language", "en", "--locale", "en_US"], timeout=900)
    log("installing the farm profile…")
    run([cfg, "--timeout", "60", "-e", ecid] + ident + ["install-profile", str(profile)])
    log("turning Developer Mode on…")
    developer_mode(udid)
    log("done: supervised, profile installed, Developer Mode on")


CLI_USAGE = """Usage:
  phone-harness host enroll UDID [--wifi SSID --wifi-password PW] [--keep-data]
      Erase the phone, supervise it under this host's organization, install the
      farm profile (with the farm Wi-Fi when given), turn Developer Mode on.
      --keep-data skips the erase and only (re)installs the profile.
"""


def cli(args):
    ap = argparse.ArgumentParser(prog="phone-harness host enroll", usage=CLI_USAGE, add_help=False)
    ap.add_argument("udid", nargs="?")
    ap.add_argument("--wifi")
    ap.add_argument("--wifi-password", default="")
    ap.add_argument("--keep-data", action="store_true")
    ap.add_argument("-h", "--help", action="store_true")
    ns = ap.parse_args(args)
    if ns.help or not ns.udid:
        print(CLI_USAGE)
        return 0
    try:
        enroll(ns.udid, ns.wifi, ns.wifi_password, ns.keep_data)
    except EnrollError as e:
        sys.exit(f"enroll: {e}")
    return 0
