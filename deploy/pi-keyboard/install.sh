#!/bin/sh
# Install or update the keyboard on a Raspberry Pi (Raspberry Pi OS / Debian,
# BlueZ 5). Run from this directory: sudo ./install.sh
# Safe to run again: it only replaces its own files and keeps the token.
set -eu
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
here=$(cd "$(dirname "$0")" && pwd)

apt-get install -y -q python3-dbus python3-gi bluez >/dev/null

install -d -m 755 /usr/local/lib/pi-keyboard
install -m 755 "$here/pi-keyboard.py" /usr/local/lib/pi-keyboard/pi-keyboard.py
install -m 644 "$here/pi-keyboard.service" /etc/systemd/system/pi-keyboard.service
install -d -m 755 /etc/systemd/system/bluetooth.service.d
install -m 644 "$here/bluetooth-noinput.conf" /etc/systemd/system/bluetooth.service.d/pi-keyboard.conf

# Present as a keyboard (Class of Device: peripheral, keyboard).
conf=/etc/bluetooth/main.conf
[ -f "$conf.bak-pikbd" ] || cp "$conf" "$conf.bak-pikbd"
if grep -q '^Class *=' "$conf"; then
    sed -i 's/^Class *=.*/Class = 0x002540/' "$conf"
else
    sed -i 's/^\[General\]/[General]\nClass = 0x002540/' "$conf"
fi

# The token the host sends with every request. Made once, kept after.
install -d -m 700 /etc/pi-keyboard
[ -s /etc/pi-keyboard/token ] || { head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n' > /etc/pi-keyboard/token; }
chmod 600 /etc/pi-keyboard/token

systemctl daemon-reload
systemctl restart bluetooth
systemctl enable --now pi-keyboard
systemctl restart pi-keyboard
echo "pi-keyboard installed; token in /etc/pi-keyboard/token"
