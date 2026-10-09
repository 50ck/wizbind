"""Dedicated radio sessions. Own processes only; no default routes or services."""

import fcntl
import ipaddress
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import tempfile
import time
from pathlib import Path

TOOLS = ("ip", "iw", "hostapd", "dnsmasq")


class AlreadyRunning(RuntimeError):
    """The selected interface is owned by another wizbind instance."""


def set_hidden(control, interface, hidden):
    """Update beacons through the owned hostapd control socket, without reload."""
    with (
        tempfile.TemporaryDirectory(prefix="wizbind-control-") as directory,
        socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sock,
    ):
        sock.settimeout(3)
        sock.bind(str(Path(directory) / "client"))
        sock.connect(str(Path(control) / interface))
        for message in (f"SET ignore_broadcast_ssid {int(hidden)}", "UPDATE_BEACON"):
            sock.send(message.encode())
            if sock.recv(4096).strip() != b"OK":
                raise RuntimeError("hostapd could not update SSID visibility")


def command(*args, timeout=15):
    try:
        result = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise RuntimeError(f"{args[0]} failed to execute") from error
    if result.returncode:
        # Supplicant/AP diagnostics can contain credential material.
        raise RuntimeError(f"{args[0]} operation failed (exit {result.returncode})")
    return result.stdout


def preflight(interface, address):
    if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,15}", interface):
        raise ValueError("Invalid interface name")
    missing = [tool for tool in TOOLS if not shutil.which(tool)]
    if missing:
        raise RuntimeError("Missing system tools: " + ", ".join(missing))
    links = json.loads(command("ip", "-j", "address", "show"))
    selected = next((link for link in links if link["ifname"] == interface), None)
    if selected is None or selected.get("master"):
        raise RuntimeError("Dedicated unbridged Wi-Fi interface required")
    info = command("iw", "dev", interface, "info")
    if not re.search(r"^\s*type managed$", info, re.MULTILINE):
        raise RuntimeError(
            "Interface must start in idle managed mode; stop its existing AP yourself"
        )
    if "Not connected." not in command("iw", "dev", interface, "link"):
        raise RuntimeError(
            "Interface is connected; use a spare radio, not your router connection"
        )
    radio = re.search(r"^\s*wiphy (\d+)$", info, re.MULTILINE)
    if not radio:
        raise RuntimeError("Cannot identify the Wi-Fi radio")
    phy = "phy" + radio[1]
    capabilities = command("iw", "phy", phy, "info")
    if not re.search(r"^\s*\* AP$", capabilities, re.MULTILINE):
        raise RuntimeError("Radio does not support access point mode")
    # No sibling virtual interfaces: changing channel could break their connections.
    wireless = command("iw", "dev")
    section = re.search(rf"(?ms)^phy#{radio[1]}\n(.*?)(?=^phy#|\Z)", wireless)
    if not section or re.findall(r"^\s*Interface (\S+)", section[1], re.MULTILINE) != [
        interface
    ]:
        raise RuntimeError("Selected radio must have exactly one Wi-Fi interface")
    routes = json.loads(command("ip", "-j", "route", "show", "table", "all"))
    routes6 = json.loads(command("ip", "-j", "-6", "route", "show", "table", "all"))
    if any(route.get("dev") == interface for route in routes + routes6):
        # Link-local IPv6 routes are also refused: bring an unused radio down first.
        raise RuntimeError(
            "Interface has routes; provide an idle interface with no configured network"
        )
    if selected.get("addr_info"):
        raise RuntimeError(
            "Interface has addresses; provide an idle interface with no configured network"
        )
    target = ipaddress.IPv4Interface(address)
    if (
        target.ip in (target.network.network_address, target.network.broadcast_address)
        or target.network.prefixlen != 24
        or int(target.ip) & 255 in range(10, 31)
        or not any(
            target.network.subnet_of(ipaddress.IPv4Network(n))
            for n in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
        )
    ):
        raise ValueError(
            "AP must be private IPv4 /24, with its address outside DHCP hosts 10–30"
        )
    occupied = [
        ipaddress.ip_interface(f"{a['local']}/{a['prefixlen']}").network
        for link in links
        for a in link.get("addr_info", [])
        if a["family"] == "inet"
    ]
    if any(target.network.overlaps(network) for network in occupied):
        raise RuntimeError("AP subnet overlaps another host interface")
    # Managers can recreate interfaces or silently change the radio. Never stop them.
    for proc in Path("/proc").glob("[0-9]*/comm"):
        try:
            name = proc.read_text().strip()
            args = (
                proc.with_name("cmdline")
                .read_bytes()
                .replace(b"\0", b" ")
                .decode(errors="replace")
            )
        except (OSError, PermissionError):
            continue
        if name in ("iwd", "NetworkManager", "wpa_supplicant") or (
            name in ("hostapd", "wpa_supplicant", "dnsmasq", "dhcpcd")
            and (interface in args or name == "dhcpcd")
        ):
            raise RuntimeError(
                "A network manager may own this interface; explicitly exclude the spare radio first"
            )
    return selected


class Session:
    def __init__(self, interface, address, ssid, password, country, channel):
        if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,15}", interface):
            raise ValueError("Invalid interface name")
        if not re.fullmatch(r"[A-Z]{2}", country) or channel not in range(1, 14):
            raise ValueError("Invalid AP country/channel")
        self.interface, self.address = interface, ipaddress.IPv4Interface(address)
        self.ssid, self.password, self.country, self.channel = (
            ssid,
            password,
            country,
            channel,
        )
        self.processes = []
        self.addresses = []
        self.settings = {}
        self.changed = False
        self.temp = None
        self.lock = None
        self.log_events = False

    def __enter__(self):
        try:
            # Advisory per-radio lock, root-owned, symlink-safe.
            fd = os.open(
                f"/run/wizbind-{self.interface}.lock",
                os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW,
                0o600,
            )
            self.lock = os.fdopen(fd, "w")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise AlreadyRunning("Another instance is already running!") from None
            print("Testing interface... AP supported.", flush=True)
            self.original = preflight(self.interface, str(self.address))
            self.defaults = command("ip", "-j", "route", "show", "default")
            self.temp = tempfile.TemporaryDirectory(prefix="wizbind-")
            self.directory = Path(self.temp.name)
            for family, setting in (
                ("ipv4", "forwarding"),
                ("ipv6", "forwarding"),
                ("ipv6", "disable_ipv6"),
            ):
                path = Path(f"/proc/sys/net/{family}/conf/{self.interface}/{setting}")
                old = path.read_text()
                self.settings[path] = old
                path.write_text("1" if setting == "disable_ipv6" else "0")
            self.changed = True
            return self
        except BaseException:
            self.close()
            raise

    def __exit__(self, *exc):
        self.close()

    def write(self, name, text):
        path = self.directory / name
        path.write_text(text)
        path.chmod(0o600)
        return str(path)

    def start(self, *args):
        old_mask = signal.pthread_sigmask(
            signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM}
        )
        try:
            process = subprocess.Popen(
                args,
                stdin=subprocess.DEVNULL,
                stdout=None if self.log_events else subprocess.DEVNULL,
                stderr=None if self.log_events else subprocess.DEVNULL,
                start_new_session=True,
            )
            self.processes.append(process)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
        return process

    def stop(self, process):
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=3)
        self.processes.remove(process)

    def alive(self):
        if any(process.poll() is not None for process in self.processes):
            raise RuntimeError("An owned networking process exited unexpectedly")

    def mode(self, mode):
        command("ip", "link", "set", "dev", self.interface, "down")
        command("iw", "dev", self.interface, "set", "type", mode)
        command("ip", "link", "set", "dev", self.interface, "up")

    def add_address(self, address):
        self.addresses.append(
            str(address)
        )  # Track before command, including cancellation.
        command("ip", "address", "add", str(address), "dev", self.interface)

    def prepare_ap(self, reservations=None):
        # Keep Wi-Fi credentials out of process arguments and hostapd logs.
        import hashlib

        psk = hashlib.pbkdf2_hmac(
            "sha1", self.password.encode(), self.ssid.encode(), 4096, 32
        ).hex()
        self.ap_config = self.write(
            "ap.conf",
            f"interface={self.interface}\ndriver=nl80211\n"
            f"ctrl_interface={self.directory}/ctrl\n"
            f"ssid2={self.ssid.encode().hex()}\ncountry_code={self.country}\n"
            f"hw_mode=g\nchannel={self.channel}\nwpa=2\nwpa_key_mgmt=WPA-PSK\n"
            f"rsn_pairwise=CCMP\nwpa_psk={psk}\n",
        )
        network = self.address.network
        # No DNS, gateway advertisement, forwarding or NAT; explicit on-link control.
        fixed = ""
        used_ips = set()
        for mac, ip in (reservations or {}).items():
            target = ipaddress.IPv4Address(ip)
            if (
                not re.fullmatch(r"[0-9a-f]{12}", mac)
                or target not in network
                or target
                in (self.address.ip, network.network_address, network.broadcast_address)
                or ip in used_ips
            ):
                raise ValueError("Invalid or duplicate DHCP reservation")
            used_ips.add(ip)
            fixed += (
                "dhcp-host="
                + ":".join(mac[i : i + 2] for i in range(0, 12, 2))
                + ","
                + ip
                + "\n"
            )
        self.dhcp_config = self.write(
            "dhcp.conf",
            f"interface={self.interface}\nbind-interfaces\nport=0\n"
            f"dhcp-range={network.network_address + 10},{network.network_address + 30},255.255.255.0,12h\n"
            f"dhcp-option=3\ndhcp-option=6\ndhcp-leasefile={self.directory}/ap.leases\n"
            f"pid-file={self.directory}/dnsmasq.pid\nuser=root\n" + fixed,
        )

    def activate_ap(self):
        self.mode("__ap")
        self.add_address(self.address)
        self.start("hostapd", *(["-t"] if self.log_events else []), self.ap_config)
        self.start(
            "dnsmasq",
            "--keep-in-foreground",
            "--conf-file=" + self.dhcp_config,
            *(["--log-dhcp", "--log-facility=-"] if self.log_events else []),
        )
        self.alive()

    def wait_for_ap(self, timeout=15):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self.alive()
            info = command("iw", "dev", self.interface, "info")
            if re.search(r"^\s*type AP$", info, re.MULTILINE) and re.search(
                r"^\s*ssid " + re.escape(self.ssid) + r"$", info, re.MULTILINE
            ):
                return
            time.sleep(0.2)
        raise RuntimeError("AP did not start before the deadline")

    def hide_ap(self, hidden=True):
        set_hidden(self.directory / "ctrl", self.interface, hidden)

    def close(self):
        failures = []
        # Repeated Ctrl+C/SIGTERM cannot interrupt rollback.
        old_mask = signal.pthread_sigmask(
            signal.SIG_BLOCK, {signal.SIGINT, signal.SIGTERM}
        )
        try:
            for process in list(reversed(self.processes)):
                if isinstance(process.args, (list, tuple)) and process.args:
                    service = {"dnsmasq": "DHCP", "hostapd": "AP"}.get(process.args[0])
                    if service:
                        print(f"Shutting down {service} service...", flush=True)
                try:
                    self.stop(process)
                except (OSError, subprocess.TimeoutExpired):
                    failures.append("process")
            if self.changed or self.addresses or self.settings:
                print("Restoring interface to its original state...", flush=True)
            for address in list(self.addresses):
                try:
                    command("ip", "address", "del", address, "dev", self.interface)
                    self.addresses.remove(address)
                except RuntimeError:
                    failures.append("address")
            if self.changed:
                for args in [
                    ("ip", "link", "set", "dev", self.interface, "down"),
                    ("iw", "dev", self.interface, "set", "type", "managed"),
                ]:
                    try:
                        command(*args)
                    except RuntimeError:
                        failures.append("mode")
            for path, value in self.settings.items():
                try:
                    path.write_text(value)
                except OSError:
                    failures.append("interface setting")
            if self.changed:
                try:
                    command(
                        "ip",
                        "link",
                        "set",
                        "dev",
                        self.interface,
                        "up" if "UP" in self.original.get("flags", []) else "down",
                    )
                    if command("ip", "-j", "route", "show", "default") != self.defaults:
                        failures.append("default route changed externally")
                except RuntimeError:
                    failures.append("link")
            if self.temp and not self.processes:
                self.temp.cleanup()
            if self.lock:
                self.lock.close()
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, old_mask)
        if failures:
            raise RuntimeError("Rollback needs attention: " + ", ".join(failures))
