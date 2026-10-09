"""User CLI: private config, local labels, AP lifetime and assisted onboarding."""

import argparse
import getpass
import json
import os
import re
import shutil
import signal
import sys
import threading
import time
import unicodedata
from pathlib import Path

from . import config
from .linux import Session
from .pairing import generate_pairing_payload, validate_credentials
from .protocol import (
    complete,
    discover_all,
    identify,
    query,
    request,
    set_pilot,
    verify_home,
)
from .web import ASSETS, PRESETS, pilot_params, serve


def normalize(value):
    return (
        "".join(
            c
            for c in unicodedata.normalize("NFKD", value.casefold())
            if not unicodedata.combining(c)
        )
        .replace(" ", "")
        .replace("_", "")
        .replace("-", "")
    )


def mode(value):
    for preset in PRESETS:
        if normalize(value) in {
            normalize(str(preset[k])) for k in ("id", "name", "label")
        } | {
            normalize(preset["name"].rsplit("_", 1)[-1]),
            normalize(Path(preset["icon"]).stem),
        }:
            return pilot_params({"scene": preset["id"], "state": True})
    raise ValueError("Unknown mode; use wizbind list modes")


def color(value):
    if re.fullmatch(r"#[0-9a-fA-F]{6}(?:[0-9a-fA-F]{2})?", value):
        rgb = value[:7]
        alpha = int(value[7:], 16) * 100 / 255 if len(value) == 9 else None
    else:
        match = re.fullmatch(
            r"rgba\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+(?:\.\d+)?)\s*\)", value
        )
        if not match or any(int(match[i]) > 255 for i in (1, 2, 3)):
            raise ValueError("Use #RRGGBB, #RRGGBBAA or rgba(R,G,B,percent)")
        rgb = "#" + "".join(f"{int(match[i]):02x}" for i in (1, 2, 3))
        alpha = float(match[4])
        if not 0 <= alpha <= 100:
            raise ValueError("RGBA alpha is brightness percent, 0–100")
    data = {"color": rgb, "state": alpha != 0}
    if alpha:
        data["brightness"] = max(10, round(alpha))
    return pilot_params(data)


def parser():
    p = argparse.ArgumentParser(
        description="Minimal offline WiZ AP, pairing and local control"
    )
    p.add_argument(
        "--config",
        type=Path,
        default=Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        / "wizbind/config",
    )
    p.add_argument("--elevate", choices=("doas", "sudo", "none"), default="doas")
    p.add_argument(
        "--interface", dest="default_interface", help="Override the saved AP interface"
    )
    p.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="action")
    onboard = sub.add_parser(
        "onboard", help="AP on this host; manual pairing sender on a second device"
    )
    onboard.add_argument("interface", nargs="?")
    onboard.add_argument("--ssid")
    onboard.add_argument("--timeout", type=int, default=600)
    onboard.add_argument("--name")
    onboard.add_argument("--device", help="Re-pair a saved device label after reset")
    listings = sub.add_parser("list")
    listings.add_argument("what", choices=("modes", "devices"))
    device = sub.add_parser("device")
    device.add_argument("label")
    device.add_argument(
        "command",
        choices=(
            "mode",
            "color",
            "brightness",
            "on",
            "off",
            "state",
            "temperature",
            "speed",
        ),
    )
    device.add_argument("value", nargs="?")
    run = sub.add_parser("run", help="Start AP, DHCP and web panel")
    run.add_argument("interface", nargs="?")
    return p


def elevated(args, argv):
    if os.geteuid() == 0:
        return
    if args.elevate == "none" or not shutil.which(args.elevate):
        raise RuntimeError("Root needed for interface binding; use doas or sudo")
    launcher = Path(__file__).resolve().parents[2] / "wizbind"
    entry = [str(launcher)] if launcher.is_file() else ["-m", "wizbind"]
    os.execvp(
        args.elevate,
        [
            args.elevate,
            sys.executable,
            *entry,
            "--config",
            str(args.config),
            "--worker",
            *argv,
        ],
    )


def refresh(args, cfg, interface):
    found = discover_all(interface, cfg["ap"]["address"])
    for mac, (ip, _) in found.items():
        config.remember(cfg, mac, ip)
    if found:
        config.save(args.config, cfg)
    return found


def control(args, cfg, interface):
    found = refresh(args, cfg, interface)
    known = config.devices(cfg)
    if args.label not in known:
        raise ValueError("Unknown label; use wizbind list devices")
    device = known[args.label]
    if device["mac"] not in found:
        raise RuntimeError("Selected bulb is offline; no command sent")
    ip, state = found[device["mac"]]
    if args.command == "state":
        print(json.dumps(state, ensure_ascii=False, indent=2))
        return
    if args.command == "mode":
        params = mode(args.value or "")
    elif args.command == "color":
        params = color(args.value or "")
    elif args.command in ("on", "off"):
        params = {"state": args.command == "on"}
    else:
        if args.value is None:
            raise ValueError("Supply a value")
        field = args.command
        controls = {field: int(args.value)}
        if field == "speed":
            controls["scene"] = state.get("sceneId")
        params = pilot_params(controls)
    # Recheck identity immediately before the write: never trust a cached lease alone.
    if (
        query(interface, cfg["ap"]["address"], ip, "getPilot")["result"]
        .get("mac", "")
        .lower()
        != device["mac"]
    ):
        raise RuntimeError("Device identity changed; no command sent")
    response = set_pilot(interface, cfg["ap"]["address"], ip, params)
    result = {"request": {"method": "setPilot", "params": params}, "response": response}
    if "sceneId" in params:
        deadline = time.monotonic() + 2
        while True:
            result["readback"] = query(interface, cfg["ap"]["address"], ip, "getPilot")
            if result["readback"]["result"].get("sceneId") == params["sceneId"]:
                break
            if time.monotonic() >= deadline:
                raise RuntimeError("Bulb acknowledged but did not confirm this preset")
            time.sleep(0.1)
    print(json.dumps(result, ensure_ascii=False))


def assisted(args, cfg, ap):
    settings = cfg["ap"]
    payload = generate_pairing_payload(
        settings["ssid"], settings["password"], hid=int(settings["home_id"])
    )
    print("1. Put the bulb in manual pairing mode (purple blinking).")
    print("2. On the second device, connect to WiZConfig_xxxx and obtain DHCP.")
    print("3. Paste this command (credential material; keep it private):")
    print("   Use the WiZConfig DHCP gateway if it differs from 192.168.56.1.")
    # Quoted heredoc prevents shell expansion and keeps the payload out of curl argv.
    print(
        "curl --fail --silent --show-error --noproxy '*' "
        "--connect-timeout 5 --max-time 10 --output /dev/null "
        "--write-out 'POST /pairing: HTTP %{http_code}\\n' "
        "--header 'Content-Type: application/json; charset=utf-8' "
        "--data-binary @- http://192.168.56.1/pairing <<'WIZBIND_PAIRING'"
    )
    print(json.dumps(payload, ensure_ascii=True, separators=(",", ":")))
    print("WIZBIND_PAIRING")
    print(
        "Listening for updates (DHCP lease observations; credentials redacted)…",
        flush=True,
    )
    deadline = time.monotonic() + args.timeout
    known = {d["mac"] for d in config.devices(cfg).values()}
    target = config.devices(cfg)[args.device]["mac"] if args.device else None
    seen = set()
    while time.monotonic() < deadline:
        ap.alive()
        try:
            lines = (ap.directory / "ap.leases").read_text().splitlines()
        except FileNotFoundError:
            lines = []
        for line in lines:
            fields = line.split()
            if len(fields) < 3:
                continue
            mac, ip = fields[1].replace(":", "").lower(), fields[2]
            if (target is not None and mac != target) or (
                target is None and mac in known
            ):
                continue
            if (mac, ip) not in seen:
                print(f"DHCP lease observed: {mac} → {ip}", flush=True)
                seen.add((mac, ip))
            try:
                device = request(ap.interface, str(ap.address), ip, "/device")
                if identify(device) != mac or device["status"] != 5:
                    continue
            except RuntimeError:
                continue
            print(f"GET http://{ip}/device → HTTP 200; MAC verified, status=5")
            print(f"POST http://{ip}/complete {{}}", flush=True)
            complete(
                ap.interface,
                str(ap.address),
                ip,
                mac,
                max(0, deadline - time.monotonic()),
            )
            print("/complete → HTTP 200", flush=True)
            verify_home(
                ap.interface, str(ap.address), ip, mac, int(settings["home_id"])
            )
            pilot = query(ap.interface, str(ap.address), ip, "getPilot")["result"]
            if pilot.get("mac", "").lower() != mac:
                raise RuntimeError("Device identity changed")
            label = config.remember(cfg, mac, ip)
            if args.name:
                if not re.fullmatch(
                    r"[\w -]{1,48}", args.name
                ) or args.name in config.devices(cfg):
                    raise ValueError("Invalid or duplicate label")
                cfg[f"device:{args.name}"] = dict(cfg[f"device:{label}"])
                cfg.remove_section(f"device:{label}")
                label = args.name
            config.save(args.config, cfg)
            print(
                f"getPilot + homeId={settings['home_id']} confirmed; saved {label} ({mac}). All set!",
                flush=True,
            )
            return
        time.sleep(0.5)
    raise RuntimeError("Onboarding timed out; owned AP will be restored")


def runtime(args, cfg, interface):
    settings = cfg["ap"]
    validate_credentials(settings["ssid"], settings["password"])
    if not 1 <= int(settings["home_id"]) <= 0x7FFFFFFF:
        raise ValueError("Use a nonzero home_id for persistence")
    print("Testing interface... AP supported.", flush=True)
    with Session(
        interface,
        settings["address"],
        settings["ssid"],
        settings["password"],
        settings["country"],
        int(settings["channel"]),
    ) as ap:
        print("Setting up interface...", flush=True)
        ap.prepare_ap(
            reservations={d["mac"]: d["ip"] for d in config.devices(cfg).values()}
        )
        print("Starting AP service...\nStarting DHCP service...", flush=True)
        ap.activate_ap()
        ap.wait_for_ap()
        if args.action == "onboard":
            assisted(args, cfg, ap)
            return
        if settings.getboolean("hidden"):
            ap.hide_ap()
        refresh(args, cfg, interface)
        stopped = threading.Event()
        failures = []

        def watch():
            while not stopped.wait(1):
                try:
                    ap.alive()
                except RuntimeError as error:
                    failures.append(error)
                    stopped.set()

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()
        try:
            print("Starting web server...", flush=True)
            peers = [d["ip"] for d in config.devices(cfg).values()]

            def remembered(ip, state):
                latest = config.load(args.config)
                config.remember(latest, state["mac"].lower(), ip)
                config.save(args.config, latest)
                return next(
                    label
                    for label, d in config.devices(latest).items()
                    if d["mac"] == state["mac"].lower()
                )

            serve(
                interface,
                settings["address"],
                peers,
                ASSETS / "rgb.html",
                int(settings["port"]),
                host="0.0.0.0",
                stop_event=stopped,
                on_device=remembered,
                allow_empty=True,
                labels={d["ip"]: label for label, d in config.devices(cfg).items()},
            )
            if failures:
                raise failures[0]
        finally:
            stopped.set()
            watcher.join(timeout=3)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # `wizbind wlan1` is shorthand for `wizbind run wlan1`.
    if argv and re.fullmatch(r"(?:wlan|wl|wifi)[\w.-]*", argv[0]):
        argv.insert(0, "run")
    args = parser().parse_args(argv)
    if args.worker and os.geteuid() != 0:
        raise ValueError("Internal worker requires elevation")
    args.config = args.config.expanduser().absolute()
    cfg = config.load(args.config)
    if args.action == "list" and args.what == "modes":
        for preset in PRESETS:
            print(
                f"{preset['id']:>3}  {preset['label']}"
                + (" [accessory required]" if preset["accessory"] else "")
            )
        return 0
    args.action = args.action or "run"
    interface = (
        getattr(args, "interface", None)
        or args.default_interface
        or cfg["ap"].get("interface")
    )
    if not interface or not re.fullmatch(r"[a-zA-Z0-9_.-]{1,15}", interface):
        raise ValueError(
            "Provide a dedicated AP dongle: wizbind onboard wlan1 or wizbind wlan1"
        )
    if args.action == "onboard":
        if args.device and args.device not in config.devices(cfg):
            raise ValueError("Unknown device label")
        if args.name and (
            not re.fullmatch(r"[\w -]{1,48}", args.name)
            or args.name in config.devices(cfg)
        ):
            raise ValueError("Invalid or duplicate label")
        if not 1 <= args.timeout <= 3600:
            raise ValueError("Timeout must be 1–3600 seconds")
        if (
            not args.worker
            and cfg["ap"].get("interface") != interface
            and input("Save this interface as the default for the AP? (y/N) ")
            .strip()
            .casefold()
            in ("y", "yes")
        ):
            cfg["ap"]["interface"] = interface
        if args.ssid:
            cfg["ap"]["ssid"] = args.ssid
    if args.action in ("run", "onboard") and not cfg["ap"].get("password"):
        cfg["ap"]["password"] = getpass.getpass(
            "AP Wi-Fi password (stored in private config): "
        )
        validate_credentials(cfg["ap"]["ssid"], cfg["ap"]["password"])
    config.save(args.config, cfg)
    # Canonical arguments carry the invoking user's config and radio through doas.
    elevated_argv = ["--interface", interface, args.action]
    if args.action in ("run", "onboard"):
        elevated_argv.append(interface)
    if args.action == "onboard":
        elevated_argv += ["--timeout", str(args.timeout)]
        for field in ("name", "device"):
            if getattr(args, field):
                elevated_argv += ["--" + field, getattr(args, field)]
    elif args.action == "list":
        elevated_argv.append(args.what)
    elif args.action == "device":
        elevated_argv += [args.label, args.command]
        if args.value is not None:
            elevated_argv.append(args.value)
    elevated(args, elevated_argv)

    def terminate(signum, frame):
        raise KeyboardInterrupt

    old = signal.signal(signal.SIGTERM, terminate)
    try:
        if args.action == "list":
            found = refresh(args, cfg, interface)
            for label, device in config.devices(cfg).items():
                print(
                    f"{label}\t{device['mac']}\t{device['ip']}\t{'online' if device['mac'] in found else 'offline'}"
                )
        elif args.action == "device":
            control(args, cfg, interface)
        else:
            runtime(args, cfg, interface)
    finally:
        signal.signal(signal.SIGTERM, old)
    return 0
