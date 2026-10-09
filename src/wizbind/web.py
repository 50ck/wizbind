"""Interface-bound browser bridge to selected local bulbs; Python stdlib only."""

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import IPv4Address, IPv4Interface
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .protocol import query, set_pilot, udp_packet

ASSETS = Path(__file__).with_name("assets")
PRESETS = json.loads((ASSETS / "presets.json").read_text())
SCENES = {p["id"]: p for p in PRESETS if not p["accessory"]}


def pilot_params(data):
    if not isinstance(data, dict):
        raise TypeError("Invalid controls")
    data = dict(data)
    for wire, alias in (
        ("sceneId", "scene"),
        ("dimming", "brightness"),
        ("temp", "temperature"),
    ):
        if wire in data:
            if alias in data:
                raise ValueError("Duplicate control fields")
            data[alias] = data.pop(wire)
    if any(k in data for k in ("r", "g", "b", "c", "w")):
        if (
            "color" in data
            or not all(
                type(data.get(k)) is int and 0 <= data[k] <= 255
                for k in ("r", "g", "b")
            )
            or any(data.get(k, 0) != 0 for k in ("c", "w"))
        ):
            raise ValueError("Use complete RGB channels with c/w=0")
        data["color"] = "#" + "".join(f"{data.pop(k):02x}" for k in ("r", "g", "b"))
        data.pop("c", None)
        data.pop("w", None)
    if set(data) - {"color", "brightness", "state", "scene", "temperature", "speed"}:
        raise ValueError("Invalid controls")
    if sum(k in data for k in ("color", "scene", "temperature")) > 1:
        raise ValueError("Choose one mode: RGB, scene or white temperature")
    params = {}
    if "scene" in data:
        if type(data["scene"]) is not int or data["scene"] not in SCENES:
            raise ValueError("Scene requires compatible hardware or an accessory")
        params["sceneId"] = data["scene"]
    if "temperature" in data:
        if (
            type(data["temperature"]) is not int
            or not 2200 <= data["temperature"] <= 6500
        ):
            raise ValueError("Temperature must be 2200–6500 K")
        params["temp"] = data["temperature"]
    if "speed" in data:
        if (
            type(data["speed"]) is not int
            or not 10 <= data["speed"] <= 200
            or not SCENES.get(data.get("scene"), {}).get("dynamic")
        ):
            raise ValueError("Speed requires a dynamic scene and a value of 10–200")
        params["speed"] = data["speed"]
    if "color" in data:
        color = data["color"]
        if not isinstance(color, str) or len(color) != 7 or not color.startswith("#"):
            raise ValueError("Use a hexadecimal RGB color")
        try:
            values = bytes.fromhex(color[1:])
        except ValueError:
            raise ValueError("Use a hexadecimal RGB color") from None
        if len(values) != 3:
            raise ValueError("Use a hexadecimal RGB color")
        params.update(zip(("r", "g", "b"), values))
        params.update(c=0, w=0)
    if "brightness" in data:
        value = data["brightness"]
        if type(value) is not int or not 10 <= value <= 100:
            raise ValueError("Brightness must be 10–100")
        params["dimming"] = value
    if "state" in data:
        if type(data["state"]) is not bool:
            raise ValueError("State must be true or false")
        params["state"] = data["state"]
    if not params:
        raise ValueError("No controls supplied")
    return params


def serve(
    interface,
    address,
    peer,
    html,
    port=8765,
    *,
    host="127.0.0.1",
    stop_event=None,
    on_device=None,
    allow_empty=False,
    labels=None,
):
    network = IPv4Interface(address)

    def validate_peer(value):
        if not isinstance(value, str):
            raise TypeError("Supply an IPv4 string")
        target = IPv4Address(value)
        if target not in network.network or target in (
            network.ip,
            network.network.network_address,
            network.network.broadcast_address,
        ):
            raise ValueError("Invalid local bulb address")
        return str(target)

    if not 1024 <= port <= 65535:
        raise ValueError("Invalid web port")
    peers = list(
        dict.fromkeys(
            validate_peer(p) for p in ([peer] if isinstance(peer, str) else peer)
        )
    )
    if len(peers) > 32 or (not peers and not allow_empty):
        raise ValueError("Supply 1–32 bulbs")
    labels = dict(labels or {})
    lock = threading.Lock()

    def selected(values):
        with lock:
            if not isinstance(values, list) or not values or len(values) > 32:
                raise ValueError("Select 1–32 bulbs")
            if any(not isinstance(p, str) or p not in peers for p in values):
                raise ValueError("Select registered bulbs only")
            return list(dict.fromkeys(values))

    def exchange(target, params=None):
        data = {
            "ip": target,
            "request": udp_packet(
                "getPilot" if params is None else "setPilot", params or {}
            ),
        }
        try:
            data["response"] = (
                query(interface, address, target, "getPilot")
                if params is None
                else set_pilot(interface, address, target, params)
            )
            if params is not None and "sceneId" in params:
                deadline = time.monotonic() + 2
                while True:
                    data["readback"] = query(interface, address, target, "getPilot")
                    if data["readback"]["result"].get("sceneId") == params["sceneId"]:
                        break
                    if time.monotonic() >= deadline:
                        data["error"] = (
                            "The bulb did not confirm this preset; check its actual state"
                        )
                        return 422, data
                    time.sleep(0.1)
            return 200, data
        except (OSError, RuntimeError, ValueError, TypeError):
            data["error"] = (
                "The bulb did not respond or confirm the change; no automatic retry"
            )
            return 502, data

    def group(targets, params=None):
        with ThreadPoolExecutor(max_workers=min(32, len(targets))) as pool:
            results = list(pool.map(lambda p: exchange(p, params), targets))
        return {"results": [dict(data, ok=code == 200) for code, data in results]}

    page = html.read_bytes()
    allowed_hosts = {f"127.0.0.1:{port}"}
    if host == "0.0.0.0":
        allowed_hosts.add(f"{network.ip}:{port}")
    elif host != "127.0.0.1":
        raise ValueError("Use loopback or isolated-AP web listener")

    class Handler(BaseHTTPRequestHandler):
        def handle(self):
            try:
                super().handle()
            except (BrokenPipeError, ConnectionResetError):
                pass  # The panel cancels polling when a control changes.

        def log_message(self, *args):
            pass  # No HTTP bodies or user data in persistent logs.

        def reply(self, code, value, content_type="application/json; charset=utf-8"):
            body = value if isinstance(value, bytes) else json.dumps(value).encode()
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(body)

        def allowed(self):
            client = IPv4Address(self.client_address[0])
            return self.headers.get("Host") in allowed_hosts and (
                client.is_loopback or (host == "0.0.0.0" and client in network.network)
            )

        def do_GET(self):
            if not self.allowed():
                return self.reply(
                    403, {"error": "Use the loopback URL printed by wizbind"}
                )
            if self.path in ("/", "/rgb.html"):
                return self.reply(200, page, "text/html; charset=utf-8")
            if self.path.startswith("/assets/"):
                name = self.path.removeprefix("/assets/")
                if (
                    name == Path(name).name
                    and (ASSETS / name).is_file()
                    and name.endswith((".svg", ".json"))
                ):
                    return self.reply(
                        200,
                        (ASSETS / name).read_bytes(),
                        "image/svg+xml"
                        if name.endswith(".svg")
                        else "application/json; charset=utf-8",
                    )
                return self.reply(404, {"error": "Unknown asset"})
            path = urlsplit(self.path)
            if path.path == "/bulbs":
                with lock:
                    return self.reply(
                        200, {"bulbs": list(peers), "labels": dict(labels)}
                    )
            if path.path != "/state":
                return self.reply(404, {"error": "Unknown endpoint"})
            try:
                arguments = parse_qs(path.query)
                if "ip" in arguments:
                    return self.reply(200, group(selected(arguments["ip"])))
                if not peers:
                    return self.reply(200, {"results": []})
                code, data = exchange(peers[0])
                self.reply(code, data)
            except (ValueError, TypeError) as error:
                self.reply(400, {"error": str(error)})

        def do_POST(self):
            if not self.allowed() or self.headers.get(
                "Origin"
            ) != "http://" + self.headers.get("Host", ""):
                return self.reply(403, {"error": "Local same-origin requests only"})
            if self.path not in ("/pilot", "/bulbs"):
                return self.reply(404, {"error": "Unknown endpoint"})
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if (
                    not 1 <= size <= 1024
                    or self.headers.get_content_type() != "application/json"
                ):
                    raise ValueError("Send a small JSON object")
                controls = json.loads(self.rfile.read(size))
                if not isinstance(controls, dict):
                    raise TypeError("Send a JSON object")
                if self.path == "/bulbs":
                    if set(controls) != {"ip"}:
                        raise ValueError("Supply one local IP")
                    target = validate_peer(controls["ip"])
                    result = query(interface, address, target, "getPilot")["result"]
                    if not isinstance(result.get("mac"), str):
                        raise ValueError("No WiZ identity in response")
                    with lock:
                        if target not in peers and len(peers) >= 32:
                            raise ValueError("Maximum 32 bulbs")
                        if on_device is not None:
                            labels[target] = on_device(target, result)
                        if target not in peers:
                            peers.append(target)
                    return self.reply(200, {"ip": target, "state": result})
                targets = (
                    selected(controls.pop("targets")) if "targets" in controls else None
                )
                params = pilot_params(controls)
            except (OSError, RuntimeError):
                return self.reply(
                    502, {"error": "The bulb did not respond over local UDP"}
                )
            except (ValueError, TypeError, UnicodeError) as error:
                return self.reply(
                    400,
                    {
                        "error": str(error)
                        if not isinstance(error, json.JSONDecodeError)
                        else "Invalid JSON"
                    },
                )
            if targets is not None:
                return self.reply(200, group(targets, params))
            if not peers:
                return self.reply(400, {"error": "Select a registered bulb"})
            code, data = exchange(peers[0], params)
            self.reply(code, data)

    with ThreadingHTTPServer((host, port), Handler) as server:
        if stop_event is not None:

            def stop():
                stop_event.wait()
                server.shutdown()

            threading.Thread(target=stop, daemon=True).start()
        print(f"Listening on {host}:{port}", flush=True)
        try:
            server.serve_forever()
        finally:
            print("Shutting down web server...", flush=True)
            if stop_event is not None:
                stop_event.set()
