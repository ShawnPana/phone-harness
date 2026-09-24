"""Headless keyboard input sent straight to a connected physical iPhone."""
import hashlib
import json
import os
import platform
import subprocess
import tempfile
from pathlib import Path

from .transport import Unsupported

_FRAMEWORKS = Path("/Library/Developer/PrivateFrameworks")
_TMP = Path(tempfile.gettempdir()) / "phone-harness"
_KEYS = {
    **{chr(ord("a") + i): 0x04 + i for i in range(26)},
    **{str(i): 0x1D + i for i in range(1, 10)},
    "0": 0x27,
    "return": 0x28, "enter": 0x28, "escape": 0x29, "esc": 0x29,
    "backspace": 0x2A, "delete": 0x2A, "tab": 0x2B, " ": 0x2C, "space": 0x2C,
    "-": 0x2D, "=": 0x2E, "[": 0x2F, "]": 0x30, "\\": 0x31,
    ";": 0x33, "'": 0x34, "`": 0x35, ",": 0x36, ".": 0x37,
    "/": 0x38, "right": 0x4F, "left": 0x50, "down": 0x51, "up": 0x52,
}
_MODIFIERS = {"ctrl": 0xE0, "shift": 0xE1, "alt": 0xE2,
              "option": 0xE2, "cmd": 0xE3}
_SHIFTED = {
    **{chr(ord("A") + i): chr(ord("a") + i) for i in range(26)},
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6",
    "&": "7", "*": "8", "(": "9", ")": "0", "_": "-", "+": "=",
    ":": ";", '"': "'", "<": ",", ">": ".", "?": "/", "~": "`",
    "{": "[", "}": "]", "|": "\\",
}

_INTERFACE = """// swift-interface-format-version: 1.0
// swift-compiler-version: Apple Swift version 6.2
// swift-module-flags: -target arm64-apple-macos15.0 -enable-library-evolution -module-name CoreDevice
import Foundation
public final class DeviceManager {
  public static var shared: CoreDevice.DeviceManager { get }
  public func deviceNamed(_ name: Swift.String) throws -> CoreDevice.RemoteDevice
}
open class RemoteDevice: Swift.CustomStringConvertible {
  public var description: Swift.String { get }
  public func getImplementation<A>(for capability: CoreDevice.CapabilityStaticMember<A>) async throws -> A.ProtocolType where A : CoreDevice.DeviceCapability
}
public struct Capability {}
public protocol DeviceCapability {
  associatedtype ProtocolType
  associatedtype ImplType
  static var capability: CoreDevice.Capability { get }
}
public struct CapabilityStaticMember<A> {}
public protocol DeviceCapabilityProtocol {
  static var capability: CoreDevice.Capability { get }
  static var identifier: Swift.String { get }
  static var identifierSuffix: Swift.String { get }
  static var priority: Swift.Int { get }
}
public protocol HIDKeyboard : CoreDevice.DeviceCapabilityProtocol {
  func send(key: CoreDevice.HIDKeyboardUsageCode, state: CoreDevice.HIDButtonState) throws
  func sendBarrier()
}
public struct HIDDeviceCapability : CoreDevice.DeviceCapability {
  public typealias ProtocolType = Any
  public typealias ImplType = Any
  public static var capability: CoreDevice.Capability { get }
}
public struct KeyboardHIDCapability : CoreDevice.DeviceCapability {
  public typealias ProtocolType = any CoreDevice.HIDKeyboard
  public typealias ImplType = any CoreDevice.HIDKeyboard
  public static var capability: CoreDevice.Capability { get }
}
extension CoreDevice.CapabilityStaticMember where A == CoreDevice.HIDDeviceCapability {
  public static var keyboard: CoreDevice.CapabilityStaticMember<CoreDevice.KeyboardHIDCapability> { get }
}
// Both types are resilient. Marking either @frozen changes the calling ABI.
public struct HIDKeyboardUsageCode { public var rawValue: Swift.UInt16 }
// Apple's tag order is up, down; reversing it leaves every key held.
public enum HIDButtonState { case up; case down }
"""

_SOURCE = """import CoreDevice
import Foundation

@main struct PhoneHarnessHID {
  static func main() async throws {
    let device = try DeviceManager.shared.deviceNamed(CommandLine.arguments[1])
    let delay = Double(CommandLine.arguments[2])!
    let keyboard = try await device.getImplementation(
      for: CapabilityStaticMember<HIDDeviceCapability>.keyboard)
    var held: [UInt16] = []
    defer {
      for raw in held.reversed() {
        let key = unsafeBitCast(raw, to: HIDKeyboardUsageCode.self)
        try? keyboard.send(key: key, state: .up)
        keyboard.sendBarrier()
      }
      // sendBarrier returns before delivery; exiting at once drops the last keys.
      Thread.sleep(forTimeInterval: 0.1)
    }
    for event in CommandLine.arguments.dropFirst(3) {
      let parts = event.split(separator: ":", maxSplits: 1)
      let raw = UInt16(parts[1], radix: 16)!
      let key = unsafeBitCast(raw, to: HIDKeyboardUsageCode.self)
      switch parts[0] {
      case "d":
        try keyboard.send(key: key, state: .down)
        held.append(raw)
      case "u":
        try keyboard.send(key: key, state: .up)
        held.removeAll { $0 == raw }
      default:
        try keyboard.send(key: key, state: .down)
        keyboard.sendBarrier()
        try keyboard.send(key: key, state: .up)
      }
      keyboard.sendBarrier()
      if delay > 0 { Thread.sleep(forTimeInterval: delay) }
    }
  }
}
"""


def _paired_iphones():
    result = subprocess.run(
        ["xcrun", "devicectl", "list", "devices", "--quiet",
         "--json-output", "-"], capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise Unsupported(f"cannot list connected iPhones: {result.stderr.strip()}")
    try:
        listed = json.loads(result.stdout)["result"]["devices"]
    except (json.JSONDecodeError, KeyError) as error:
        raise RuntimeError("devicectl returned an invalid device list") from error
    return [
        item for item in listed
        if item.get("properties", {}).get("hardware", {}).get("reality") == "physical"
        and item.get("properties", {}).get("hardware", {}).get("platform") == "iOS"
        and item.get("properties", {}).get("connection", {}).get("pairingState") == "paired"
    ]


def _is_connected(item):
    return item.get("properties", {}).get("connection", {}).get("state") == "connected"


def _connected_iphone():
    phones = _paired_iphones()
    if len(phones) == 1 and not _is_connected(phones[0]):
        # CoreDevice drops an idle tunnel (the phone then lists as "available
        # (paired)"); any devicectl request to the phone brings it back up.
        subprocess.run(
            ["xcrun", "devicectl", "device", "info", "details", "--device",
             phones[0]["identifier"], "--quiet", "--json-output", os.devnull],
            capture_output=True, timeout=30)
        phones = _paired_iphones()
    devices = [
        item for item in phones
        if _is_connected(item)
        and any(cap.get("featureIdentifier") ==
                "com.apple.coredevice.feature.remote.hid.keyboard"
                for cap in item.get("capabilities", []))
    ]
    if len(devices) != 1:
        raise Unsupported(
            f"headless iPhone keys need exactly one connected physical iPhone; found {len(devices)}")
    return devices[0]["properties"]["state"]["name"]


def _binary():
    framework = _FRAMEWORKS / "CoreDevice.framework/CoreDevice"
    if not framework.exists():
        raise Unsupported("headless iPhone keys require Xcode's CoreDevice framework")
    # An Xcode update replaces CoreDevice; rebuild against the new one.
    stamp = str(framework.stat().st_mtime_ns)
    digest = hashlib.sha256((_INTERFACE + _SOURCE + stamp).encode()).hexdigest()[:12]
    root = _TMP / f"coredevice-hid-{digest}"
    binary = root / "phone-harness-hid"
    if binary.exists():
        return binary
    module = root / "CoreDevice.swiftmodule"
    module.mkdir(parents=True, exist_ok=True)
    arch = platform.machine()
    interface = module / f"{arch}-apple-macos.swiftinterface"
    staged = interface.with_suffix(f".{os.getpid()}")
    staged.write_text(_INTERFACE.replace("arm64-apple-macos", f"{arch}-apple-macos"))
    os.replace(staged, interface)
    candidate = root / f"phone-harness-hid.{os.getpid()}"
    result = subprocess.run(
        ["xcrun", "swiftc", "-parse-as-library", "-I", str(root),
         "-F", str(_FRAMEWORKS), "-framework", "CoreDevice",
         "-o", str(candidate), "-"], input=_SOURCE.encode(),
        capture_output=True, timeout=30)
    if result.returncode:
        raise Unsupported(
            "cannot build the headless iPhone key helper: "
            + result.stderr.decode(errors="replace").strip())
    os.replace(candidate, binary)
    return binary


def _events_for_combo(combo):
    parts = combo.lower().split("+")
    key, modifiers = parts[-1], parts[:-1]
    if key not in _KEYS:
        raise ValueError(f"unknown key {key!r}")
    if unknown := [name for name in modifiers if name not in _MODIFIERS]:
        raise ValueError(f"unknown modifier {unknown[0]!r}")
    held = [_MODIFIERS[name] for name in modifiers]
    return ([f"d:{code:02x}" for code in held]
            + [f"t:{_KEYS[key]:02x}"]
            + [f"u:{code:02x}" for code in reversed(held)])


def _events_for_text(text):
    events = []
    for char in text:
        base = _SHIFTED.get(char, char)
        key = {"\n": "return", "\t": "tab"}.get(char, base)
        if key not in _KEYS:
            raise ValueError(f"cannot type {char!r} via US-layout keycodes")
        if char in _SHIFTED:
            events.extend(("d:e1", f"t:{_KEYS[key]:02x}", "u:e1"))
        else:
            events.append(f"t:{_KEYS[key]:02x}")
    return events


def _send(events, delay=0, device=None):
    if not events:
        return
    name = device or _connected_iphone()
    result = subprocess.run(
        [str(_binary()), name, str(max(delay, 0)), *events],
        capture_output=True, timeout=30 + len(events) * (max(delay, 0) + 0.05))
    if result.returncode:
        detail = result.stderr.decode(errors="replace").strip()
        raise RuntimeError(f"headless iPhone key delivery failed: {detail}")


def press(combo):
    _send(_events_for_combo(combo))


def type_text(text, delay=0.03, keystrokes=False):
    return _send(_events_for_text(text), delay)


if __name__ == "__main__":
    assert _events_for_combo("cmd+v") == ["d:e3", "t:19", "u:e3"]
    assert _events_for_text("A\n") == ["d:e1", "t:04", "u:e1", "t:28"]
