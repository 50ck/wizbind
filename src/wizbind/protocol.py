"""Bound local HTTP and WiZ UDP; credentials never enter diagnostics."""

import base64
import hashlib
import hmac
import http.client
import json
import re
import socket
import time
from ipaddress import IPv4Address, IPv4Interface


def bind(sock, interface, source):
    sock.setsockopt(
        socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface.encode() + b"\0"
    )
    sock.bind((str(source), 0))


def request(interface, address, peer, path, payload=None, timeout=3):
    network = IPv4Interface(address)
    peer = IPv4Address(peer)
    if (
        path not in ("/device", "/pairing", "/complete")
        or peer not in network.network
        or peer
        in (
            network.ip,
            network.network.network_address,
            network.network.broadcast_address,
        )
    ):
        raise ValueError("Invalid local setup endpoint")
    connection = http.client.HTTPConnection(str(peer), timeout=timeout)
    try:
        connection.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        connection.sock.settimeout(timeout)
        bind(connection.sock, interface, network.ip)
        connection.sock.connect((str(peer), 80))
        body = (
            None
            if payload is None
            else json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        )
        connection.request(
            "GET" if path == "/device" else "POST",
            path,
            body,
            {
                "Content-Type": "application/json; charset=utf-8",
                "Connection": "close",
            },
        )
        response = connection.getresponse()
        raw = response.read(8193)
        if response.status != 200 or len(raw) > 8192:
            raise RuntimeError("Setup request rejected or oversized")
        result = json.loads(raw) if raw.strip() else {}
        if not isinstance(result, dict):
            raise TypeError("Setup response must be an object")
        return result
    except (OSError, http.client.HTTPException, ValueError, TypeError) as error:
        raise RuntimeError("Local HTTP exchange failed") from error
    finally:
        connection.close()


def identify(device):
    mac = device.get("mac", "")
    if (
        not isinstance(mac, str)
        or not re.fullmatch(r"[0-9a-fA-F]{12}", mac)
        or int(mac[:2], 16) & 1
        or int(mac, 16) == 0
        or device.get("name") != "ESP25_SHRGB_01"
        or type(device.get("status")) is not int
    ):
        raise RuntimeError("Unsupported WiZ setup device or invalid identity/status")
    return mac.lower()


def complete(interface, address, peer, mac, timeout=30):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            device = request(
                interface,
                address,
                peer,
                "/device",
                timeout=min(3, deadline - time.monotonic()),
            )
        except RuntimeError:
            device = None
        if device is not None:
            if identify(device) != mac:
                raise RuntimeError("Bulb identity changed before setup completion")
            if device["status"] == 5:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                # {} is the body verified on ESP25/1.38.0 hardware, twice rebooted.
                request(
                    interface, address, peer, "/complete", {}, timeout=min(3, remaining)
                )
                return
        time.sleep(min(0.2, max(0, deadline - time.monotonic())))
    raise RuntimeError("Bulb did not reach connected setup status before the deadline")


def udp_packet(method, params, signing_key=None, timestamp=None):
    """Build the observed v1 envelope; never mutate the caller's parameters."""
    params = dict(params)
    packet = {"version": 1, "method": method, "params": params}
    if signing_key is not None:
        try:
            key = bytes.fromhex(signing_key)
        except (ValueError, TypeError):
            raise ValueError("UDP signing key must be nonempty hexadecimal") from None
        if not key:
            raise ValueError("UDP signing key must be nonempty hexadecimal")
        if method in ("setPilot", "setEffect"):
            params.setdefault("orig", "andr2")
        params["sigTs"] = int(time.time()) if timestamp is None else timestamp
        message = json.dumps(
            params, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        packet["hmac"] = base64.b64encode(
            hmac.new(key, message, hashlib.sha256).digest()
        ).decode("ascii")
    return packet


def query(interface, address, peer, method, timeout=3):
    """Read-only unicast query; never route outside the explicitly selected subnet."""
    network = IPv4Interface(address)
    target = IPv4Address(peer)
    if (
        method not in ("getPilot", "getSystemConfig")
        or target not in network.network
        or target
        in (
            network.ip,
            network.network.network_address,
            network.network.broadcast_address,
        )
    ):
        raise ValueError("Invalid local query")
    packet = udp_packet(method, {})
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        bind(sock, interface, network.ip)
        deadline = time.monotonic() + timeout
        sock.sendto(
            json.dumps(packet, separators=(",", ":")).encode(), (str(target), 38899)
        )
        while time.monotonic() < deadline:
            sock.settimeout(max(0.01, deadline - time.monotonic()))
            try:
                raw, sender = sock.recvfrom(8192)
            except TimeoutError:
                break
            if sender != (str(target), 38899):
                continue
            try:
                response = json.loads(raw)
            except (ValueError, UnicodeError):
                continue
            if (
                isinstance(response, dict)
                and response.get("method") == method
                and isinstance(response.get("result"), dict)
            ):
                return response
    raise RuntimeError("Local WiZ query timed out")


def verify_home(interface, address, peer, mac, home_id, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            result = query(
                interface,
                address,
                peer,
                "getSystemConfig",
                min(3, deadline - time.monotonic()),
            )["result"]
        except RuntimeError:
            continue
        if not isinstance(result.get("mac"), str) or result["mac"].lower() != mac:
            raise RuntimeError("Bulb identity changed after completion")
        if type(result.get("homeId")) is int and result["homeId"] == home_id:
            return
        time.sleep(min(0.2, max(0, deadline - time.monotonic())))
    raise RuntimeError("Bulb did not confirm the requested nonzero homeId")


def set_pilot(interface, address, peer, params, signing_key=None):
    network = IPv4Interface(address)
    if IPv4Address(peer) not in network.network or IPv4Address(peer) in (
        network.ip,
        network.network.network_address,
        network.network.broadcast_address,
    ):
        raise ValueError("Bulb must be on the selected subnet")
    packet = udp_packet("setPilot", params, signing_key)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        bind(sock, interface, network.ip)
        sock.settimeout(3)
        sock.sendto(
            json.dumps(packet, ensure_ascii=False, separators=(",", ":")).encode(),
            (peer, 38899),
        )
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            sock.settimeout(max(0.01, deadline - time.monotonic()))
            data, sender = sock.recvfrom(8192)
            if sender == (peer, 38899):
                response = json.loads(data)
                if (
                    response.get("method") != "setPilot"
                    or response.get("result", {}).get("success") is not True
                ):
                    code = response.get("error", {}).get("code")
                    if code in (-32602, -32605):
                        raise RuntimeError(
                            f"Bulb rejected setPilot ({code}); check UDP signing key/security"
                        )
                    raise RuntimeError("Bulb rejected setPilot")
                return response
        raise RuntimeError("setPilot response timed out")


def discover_all(interface, address, timeout=2):
    """Read-only bounded broadcast discovery on the chosen interface."""
    network = IPv4Interface(address)
    found = {}
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        bind(sock, interface, network.ip)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        packet = json.dumps(udp_packet("getPilot", {})).encode()
        deadline = time.monotonic() + timeout
        next_send = 0
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_send:
                sock.sendto(packet, (str(network.network.broadcast_address), 38899))
                next_send = now + 0.5
            sock.settimeout(min(0.5, max(0.01, deadline - now)))
            try:
                raw, sender = sock.recvfrom(8192)
                result = json.loads(raw)
                mac = result.get("result", {}).get("mac", "").lower()
                if (
                    sender[1] == 38899
                    and IPv4Address(sender[0]) in network.network
                    and sender[0] != str(network.ip)
                    and result.get("method") == "getPilot"
                    and re.fullmatch(r"[0-9a-f]{12}", mac)
                ):
                    found[mac] = (sender[0], result["result"])
            except (TimeoutError, ValueError, TypeError, AttributeError):
                pass
    return found
