"""Small private INI: AP settings and labels tied to MAC addresses."""

import configparser
import ipaddress
import os
import re
import tempfile
from pathlib import Path

DEFAULTS = {
    "ssid": "RGB",
    "address": "192.168.33.1/24",
    "country": "ES",
    "channel": "6",
    "home_id": "420",
    "hidden": "yes",
    "port": "24800",
}


def load(path):
    cfg = configparser.ConfigParser(interpolation=None)
    cfg["ap"] = DEFAULTS
    if path.exists():
        if path.is_symlink() or path.stat().st_mode & 0o077:
            raise ValueError("Config must be a private regular file (chmod 600)")
        if not path.is_file():
            raise ValueError("Config must be a regular file")
        try:
            with path.open() as file:
                cfg.read_file(file)
        except configparser.Error:
            raise ValueError("Invalid INI configuration; contents redacted") from None
    try:
        network = ipaddress.IPv4Interface(cfg["ap"]["address"])
        if network.network.prefixlen != 24:
            raise ValueError
        if (
            not 1024 <= cfg.getint("ap", "port") <= 65534
            or cfg.getint("ap", "channel") not in range(1, 14)
            or not 1 <= cfg.getint("ap", "home_id") <= 0x7FFFFFFF
            or not re.fullmatch(r"[A-Z]{2}", cfg["ap"]["country"])
        ):
            raise ValueError
        cfg.getboolean("ap", "hidden")
        for label, device in devices(cfg).items():
            if not label or not re.fullmatch(r"[0-9a-f]{12}", device["mac"]):
                raise ValueError
            peer = ipaddress.IPv4Address(device["ip"])
            if peer not in network.network or peer in (
                network.ip,
                network.network.network_address,
                network.network.broadcast_address,
            ):
                raise ValueError
    except (ValueError, KeyError, configparser.Error):
        raise ValueError(
            "Invalid AP settings or device identity; contents redacted"
        ) from None
    return cfg


def save(path, cfg):
    if path.is_symlink():
        raise ValueError("Config cannot be a symlink")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    owner = path.stat() if path.exists() else path.parent.stat()
    fd, temporary = tempfile.mkstemp(prefix=".config-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as file:
            cfg.write(file)
            file.flush()
            os.fsync(file.fileno())
        if os.geteuid() == 0:
            os.chown(temporary, owner.st_uid, owner.st_gid)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def devices(cfg):
    return {s[7:]: dict(cfg[s]) for s in cfg.sections() if s.startswith("device:")}


def remember(cfg, mac, ip):
    if not re.fullmatch(r"[0-9a-f]{12}", mac):
        raise ValueError("Invalid device MAC")
    for label, device in devices(cfg).items():
        if device.get("mac") == mac:
            cfg[f"device:{label}"]["ip"] = ip
            return label
    i = 0
    while f"device:bulb{i}" in cfg:
        i += 1
    label = f"bulb{i}"
    cfg[f"device:{label}"] = {"mac": mac, "ip": ip}
    return label
