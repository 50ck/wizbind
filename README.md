# wizbind

`wizbind` is an unofficial WiZ tool that supports pairing, CLI and web controls, in an isolated environment with no access to the Internet so it does never share telemetry in case it does by default. I don't trust IoT devices and neither should you.

Requires Linux, Python 3.11+, an idle AP-capable Wi-Fi interface (can be an external Wi-Fi dongle) and a second Wi-Fi device (a laptop or another dongle) with curl for pairing.

## install
Download dependencies:

- **Arch Linux**: `pacman -Syu --needed python python-cryptography iproute2 iw hostapd dnsmasq git curl`
- **Void Linux**: `xbps-install -S python3 python3-cryptography iproute2 iw hostapd dnsmasq git curl`
- **Debian 12+**: `apt update && apt install --no-install-recommends python3 python3-cryptography iproute2 iw hostapd dnsmasq-base git curl`
- **Fedora**: `dnf install python3 python3-cryptography iproute iw hostapd dnsmasq git curl`


Then: 

```sh
git clone https://github.com/50ck/wizbind
cd wizbind
mkdir -p ~/.local/bin
ln -s "$PWD/wizbind" ~/.local/bin/wizbind
```
Ensure `~/.local/bin` is in your PATH.

## examples

```sh
wizbind onboard wlan0 # or the interface of the Wi-Fi dongle
wizbind  # to start AP, DHCP and web panel

# in another terminal, with the AP running:
wizbind list devices
wizbind list modes
wizbind device bulb0 mode ocean
wizbind device bulb0 color '#ff00ff66'
wizbind device bulb0 brightness 60
wizbind device bulb0 off
```
