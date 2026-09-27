# Enrolling an iPhone into the farm

One command per phone. No clicking, no keychain, no GUI session.

    phone-harness host enroll <UDID> --wifi "<ssid>" --wifi-password "<pw>"

What it does, in order: erase the phone, supervise it under this host's
organization with every Setup Assistant pane skipped, install the farm
profile with a Wi-Fi payload, turn Developer Mode on. The phone comes back
on its home screen, on Wi-Fi, and the host's daemon picks it up.
`--keep-data` skips the erase and only reinstalls the profile.

## One-time setup on the host Mac

1. Install Apple Configurator from the App Store (it provides `cfgutil`).
2. In Apple Configurator: Settings > Organizations > `+`, skip Apple Business,
   name the organization. Then the gear menu > Export Supervision Identity >
   format **Unencrypted DER (.crt and .der, for Automator and cfgutil)**.
   Save the two files as `identity.crt` and `identity.der` in
   `~/.config/phone-harness/supervision/`. Back them up somewhere else: this
   identity is the only thing that can manage the phones it supervises.
3. Put `farm-iphone.mobileconfig` (this repo's `deploy/farm-iphone.mobileconfig`)
   in the same directory.

`cfgutil` accepts the identity only in that exported form. A key produced by
OpenSSL, or a `.p12` converted by hand, fails with "couldn't create
supervision identity".

## Per phone, by hand, once

- Plug it in and tap Trust on the phone (only needed for a phone that was
  never paired with this Mac).
- If the phone ever had a personal Apple ID with Find My on, the erase leaves
  it Activation Locked. Select the phone in Finder on the host and enter that
  Apple ID there; farm phones never get an Apple ID, so this is a one-time
  rescue, not a step.

## What the profile enforces

Blocked: Activation Lock, Find My Device, erase, passcode changes,
Screen Time restrictions, manual profile installs, VPN creation, OS updates
for 90 days. Allowed: Apple ID sign-in and sign-out, iCloud, App Store,
Safari, camera. Auto-Lock is a Settings value, not a restriction; set it to
Never once after enrolling (Settings > Display & Brightness > Auto-Lock).

## The phone's keyboard

Apple's own Apple Account sheets refuse keys that arrive over the developer
connection, so each farm phone gets a real keyboard: a Raspberry Pi paired with
it over Bluetooth. With `keyboards[udid]` in host.json, every key for that phone
(`input.text`, `input.keys`, and the live preview's typing) goes through it.

1. On the Pi: copy `deploy/pi-keyboard/` over and run `sudo ./install.sh`.
2. On this Mac: make an SSH key, authorize it on the Pi for the forward only
   (`restrict,port-forwarding,permitopen="127.0.0.1:7777",command="/bin/false"`),
   and install `deploy/com.phone-harness.keyboard-tunnel.plist`.
3. Copy the Pi's `/etc/pi-keyboard/token` to a 600 file here, then
   `phone-harness host keyboard UDID set 127.0.0.1:7777 TOKEN_FILE`.
4. Pair: `host keyboard UDID pairing on`, pick "Phone Harness Keyboard" in the
   phone's Settings > Bluetooth, and send the code it shows with
   `host keyboard UDID passkey CODE`.

An erase makes the phone forget the keyboard: remove the old pairing on the Pi
(`bluetoothctl remove <address>`) and pair again from step 4.
