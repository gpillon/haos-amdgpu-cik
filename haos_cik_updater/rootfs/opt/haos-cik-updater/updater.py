#!/usr/bin/env python3
"""Post-install updater for gpillon's signed HAOS AMDGPU CIK images."""

from __future__ import annotations

import html
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


OPTIONS_PATH = Path("/data/options.json")
RAUC_SERVICE = "de.pengutronix.rauc"
RAUC_OBJECT = "/"
RAUC_INTERFACE = "de.pengutronix.rauc.Installer"
VERSION_RE = re.compile(r"^(\d+)\.(\d+)(?:\.(\d{14}))?$")


def load_options() -> dict[str, Any]:
    defaults: dict[str, Any] = {
        "manifest_url": "https://gpillon.github.io/haos-amdgpu-cik/stable.json",
        "auto_install": False,
        "reboot_after_install": False,
        "check_interval_hours": 6,
    }
    if OPTIONS_PATH.exists():
        defaults.update(json.loads(OPTIONS_PATH.read_text(encoding="utf-8")))
    return defaults


def version_key(value: str) -> tuple[int, int, int, int]:
    match = VERSION_RE.fullmatch(value)
    if not match:
        raise ValueError(f"Unsupported HAOS version format: {value}")
    major, minor, timestamp = match.groups()
    return int(major), int(minor), 1 if timestamp else 0, int(timestamp or 0)


def rauc_property(name: str) -> str:
    completed = subprocess.run(
        [
            "gdbus",
            "call",
            "--system",
            "--dest",
            RAUC_SERVICE,
            "--object-path",
            RAUC_OBJECT,
            "--method",
            "org.freedesktop.DBus.Properties.Get",
            RAUC_INTERFACE,
            name,
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    match = re.search(r"<[\"']([^\"']*)[\"']>", completed.stdout)
    if not match:
        raise RuntimeError(f"Unexpected D-Bus response for {name}: {completed.stdout.strip()}")
    return match.group(1)


def fetch_manifest(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "haos-cik-updater/0.1"})
    with urllib.request.urlopen(request, timeout=20) as response:
        data = json.load(response)

    custom = data["custom_haos"]
    version = custom["version"]
    if custom["board"] != "generic-x86-64":
        raise ValueError("Manifest is not for generic-x86-64")
    if data["hassos"]["generic-x86-64"] != version:
        raise ValueError("Manifest version fields disagree")
    if not data["ota"].startswith("https://"):
        raise ValueError("OTA URL must use HTTPS")
    if not re.fullmatch(r"[0-9a-f]{64}", custom["raucb_sha256"]):
        raise ValueError("Manifest RAUC checksum is invalid")
    version_key(version)
    return data


class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.current_version = "unknown"
        self.manifest: dict[str, Any] | None = None
        self.update_available = False
        self.installing = False
        self.message = "Waiting for the first update check"
        self.last_check = 0.0

    def check(self) -> None:
        options = load_options()
        try:
            current = rauc_property("SystemVersion")
            manifest = fetch_manifest(str(options["manifest_url"]))
            target = manifest["custom_haos"]["version"]
            available = version_key(target) > version_key(current)
            message = f"Update {target} is available" if available else "System is up to date"
            with self.lock:
                self.current_version = current
                self.manifest = manifest
                self.update_available = available
                self.message = message
                self.last_check = time.time()
            if available and bool(options["auto_install"]):
                self.install()
        except Exception as err:  # Keep the service/UI alive and expose the failure.
            with self.lock:
                self.message = f"Update check failed: {err}"
                self.last_check = time.time()

    def install(self) -> None:
        with self.lock:
            if self.installing:
                raise RuntimeError("An installation is already running")
            if not self.update_available or self.manifest is None:
                raise RuntimeError("No newer compatible update is available")
            url = self.manifest["ota"]
            self.installing = True
            self.message = "RAUC installation requested"
        threading.Thread(target=self._install_worker, args=(url,), daemon=True).start()

    def _install_worker(self, url: str) -> None:
        try:
            subprocess.run(
                [
                    "gdbus",
                    "call",
                    "--system",
                    "--dest",
                    RAUC_SERVICE,
                    "--object-path",
                    RAUC_OBJECT,
                    "--method",
                    f"{RAUC_INTERFACE}.InstallBundle",
                    url,
                    "{}",
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            saw_installing = False
            deadline = time.monotonic() + 3600
            while time.monotonic() < deadline:
                operation = rauc_property("Operation")
                if operation == "installing":
                    saw_installing = True
                elif saw_installing and operation == "idle":
                    error = rauc_property("LastError")
                    if error:
                        raise RuntimeError(error)
                    with self.lock:
                        self.message = "Update installed in the inactive slot; reboot to activate it"
                        self.update_available = False
                    if bool(load_options()["reboot_after_install"]):
                        self._reboot_host()
                    return
                time.sleep(5)
            raise TimeoutError("Timed out waiting for RAUC to complete")
        except Exception as err:
            with self.lock:
                self.message = f"Installation failed: {err}"
        finally:
            with self.lock:
                self.installing = False

    @staticmethod
    def _reboot_host() -> None:
        token = os.environ.get("SUPERVISOR_TOKEN")
        if not token:
            raise RuntimeError("SUPERVISOR_TOKEN is unavailable; reboot manually")
        request = urllib.request.Request(
            "http://supervisor/host/reboot",
            data=b"{}",
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            if response.status >= 300:
                raise RuntimeError(f"Supervisor reboot request returned HTTP {response.status}")


STATE = State()


class Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path.rstrip("/") == "/api/status":
            self._json_status()
            return
        if self.path.rstrip("/") not in ("", "/"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        with STATE.lock:
            current = STATE.current_version
            target = (STATE.manifest or {}).get("custom_haos", {}).get("version", "unavailable")
            message = STATE.message
            available = STATE.update_available
            installing = STATE.installing
        button = ""
        if available and not installing:
            button = '<form method="post" action="install"><button type="submit">Install signed update</button></form>'
        page = f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>HAOS CIK Updater</title></head><body>
<h1>HAOS AMDGPU CIK Updater</h1>
<p><strong>Installed:</strong> {html.escape(current)}</p>
<p><strong>Available:</strong> {html.escape(str(target))}</p>
<p>{html.escape(message)}</p>
{button}
<form method="post" action="check"><button type="submit">Check now</button></form>
</body></html>"""
        encoded = page.encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_POST(self) -> None:
        try:
            action = self.path.rstrip("/").rsplit("/", 1)[-1]
            if action == "check":
                STATE.check()
            elif action == "install":
                STATE.install()
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.send_response(HTTPStatus.SEE_OTHER)
            self.send_header("Location", "./")
            self.end_headers()
        except Exception as err:
            self.send_error(HTTPStatus.CONFLICT, str(err))

    def _json_status(self) -> None:
        with STATE.lock:
            body = json.dumps(
                {
                    "current_version": STATE.current_version,
                    "target_version": (STATE.manifest or {}).get("custom_haos", {}).get("version"),
                    "update_available": STATE.update_available,
                    "installing": STATE.installing,
                    "message": STATE.message,
                    "last_check": STATE.last_check,
                }
            ).encode()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[http] {fmt % args}", flush=True)


def checker_loop() -> None:
    while True:
        STATE.check()
        interval = max(1, int(load_options()["check_interval_hours"]))
        time.sleep(interval * 3600)


if __name__ == "__main__":
    threading.Thread(target=checker_loop, daemon=True).start()
    print("HAOS AMDGPU CIK updater listening on port 8099", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 8099), Handler).serve_forever()
