# what is

WiZ pairing, CLI and web controls, in an isolated environment with no access to the Internet so it does never share telemetry in case it does by default.

Requires Linux, Python 3.11+, an idle AP-capable Wi-Fi interface (can be an external Wi-Fi dongle) and a second Wi-Fi device (a laptop or another dongle) with curl for pairing.

# install

## Dependencies

**Arch Linux**

```sh
pacman -Syu --needed python python-cryptography iproute2 iw hostapd dnsmasq git curl
```

**Void Linux**

```sh
xbps-install -S python3 python3-cryptography iproute2 iw hostapd dnsmasq git curl
```

**Debian 12+**

```sh
apt update
apt install --no-install-recommends python3 python3-cryptography iproute2 iw hostapd dnsmasq-base git curl
```

**Fedora**

```sh
dnf install python3 python3-cryptography iproute iw hostapd dnsmasq opendoas git curl
```

```sh
git clone https://github.com/50ck/wizbind
cd wizbind
mkdir -p ~/.local/bin
ln -s "$PWD/wizbind" ~/.local/bin/wizbind
```

Keep the checkout in place and ensure `~/.local/bin` is in your PATH.

# examples

```sh
wizbind onboard wlan1
wizbind                            # Start AP, DHCP and web panel

# In another terminal, with the AP running:
wizbind list devices
wizbind list modes
wizbind device bulb0 mode ocean
wizbind device bulb0 color '#ff00ff66'
wizbind device bulb0 brightness 60
wizbind device bulb0 off
```
