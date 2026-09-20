#!/usr/bin/env python3
"""Post-install updater for gpillon's signed HAOS AMDGPU CIK images."""

from __future__ import annotations

import html
import hashlib
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
CONTAINER_SHARE_DIR = Path("/share")
HOST_SHARE_DIR = Path("/mnt/data/supervisor/share")
BUNDLE_PATH = CONTAINER_SHARE_DIR / "haos-amdgpu-cik-update.raucb"
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


def supervisor_token() -> str:
    token = os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HASSIO_TOKEN")
    if not token:
        raise RuntimeError(
            "Token Supervisor non disponibile. Reinstalla o riavvia l'add-on dopo l'aggiornamento"
        )
    return token


def supervisor_os_version() -> str:
    """Read the installed HAOS version from the supported Supervisor API."""
    token = supervisor_token()
    request = urllib.request.Request(
        "http://supervisor/os/info",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.load(response)
    if payload.get("result") != "ok":
        raise RuntimeError(f"Risposta Supervisor non valida: {payload.get('message', 'errore sconosciuto')}")
    version = payload.get("data", {}).get("version")
    if not isinstance(version, str):
        raise RuntimeError("La risposta Supervisor non contiene la versione HAOS")
    version_key(version)
    return version


def _gvariant_string(properties: str, key: str) -> str | None:
    match = re.search(
        rf"['\"]{re.escape(key)}['\"]\s*:\s*<\s*(['\"])(.*?)\1\s*>",
        properties,
        flags=re.DOTALL,
    )
    return match.group(2) if match else None


def rauc_booted_version() -> str:
    """Read the installed version from the booted RAUC slot."""
    output = gdbus_call(f"{RAUC_INTERFACE}.GetSlotStatus", timeout=30)
    for match in re.finditer(
        r"\(\s*['\"][^'\"]+['\"]\s*,\s*\{(?P<properties>.*?)\}\s*\)",
        output,
        flags=re.DOTALL,
    ):
        properties = match.group("properties")
        if _gvariant_string(properties, "state") != "booted":
            continue
        version = _gvariant_string(properties, "bundle.version")
        if version is not None:
            version_key(version)
            return version
    raise RuntimeError("RAUC non riporta la versione dello slot avviato")


def installed_os_version() -> str:
    """Use Supervisor when available and RAUC as the token-free source."""
    if os.environ.get("SUPERVISOR_TOKEN") or os.environ.get("HASSIO_TOKEN"):
        try:
            return supervisor_os_version()
        except Exception:
            pass
    return rauc_booted_version()


def rauc_property(name: str) -> str:
    output = gdbus_call(
        "org.freedesktop.DBus.Properties.Get",
        RAUC_INTERFACE,
        name,
        timeout=15,
    )
    match = re.search(r"<[\"']([^\"']*)[\"']>", output)
    if not match:
        raise RuntimeError(f"Risposta RAUC inattesa per {name}: {output.strip()}")
    return match.group(1)


def gdbus_call(method: str, *arguments: str, timeout: int) -> str:
    """Call host RAUC over D-Bus and surface its useful error text."""
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
            method,
            *arguments,
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "errore sconosciuto"
        raise RuntimeError(f"RAUC D-Bus: {detail}")
    return completed.stdout


def fetch_manifest(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": "haos-cik-updater/0.2"})
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


def download_bundle(url: str, expected_sha256: str, destination: Path = BUNDLE_PATH) -> Path:
    """Download and verify a bundle outside RAUC's limited HTTP downloader."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(f"{destination.suffix}.part")
    temporary.unlink(missing_ok=True)
    digest = hashlib.sha256()
    request = urllib.request.Request(url, headers={"User-Agent": "haos-cik-updater/0.2"})
    try:
        with urllib.request.urlopen(request, timeout=300) as response, temporary.open("wb") as output:
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
                digest.update(chunk)
        actual_sha256 = digest.hexdigest()
        if actual_sha256 != expected_sha256:
            raise ValueError(
                f"Bundle checksum mismatch: expected {expected_sha256}, got {actual_sha256}"
            )
        os.replace(temporary, destination)
        return destination
    except Exception:
        temporary.unlink(missing_ok=True)
        raise


def host_bundle_path(container_path: Path) -> Path:
    """Translate the add-on /share mount to the same file in the HAOS host namespace."""
    if container_path.parent != CONTAINER_SHARE_DIR:
        raise ValueError(f"Bundle must be stored directly in {CONTAINER_SHARE_DIR}")
    return HOST_SHARE_DIR / container_path.name


class State:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.current_version = "unknown"
        self.manifest: dict[str, Any] | None = None
        self.update_available = False
        self.installing = False
        self.message = "In attesa del primo controllo"
        self.last_check = 0.0

    def check(self) -> None:
        options = load_options()
        try:
            current = installed_os_version()
            manifest = fetch_manifest(str(options["manifest_url"]))
            target = manifest["custom_haos"]["version"]
            available = version_key(target) > version_key(current)
            message = f"Aggiornamento {target} disponibile" if available else "Sistema aggiornato"
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
                self.message = f"Controllo non riuscito: {err}"
                self.last_check = time.time()

    def install(self) -> None:
        with self.lock:
            if self.installing:
                raise RuntimeError("An installation is already running")
            if not self.update_available or self.manifest is None:
                raise RuntimeError("No newer compatible update is available")
            url = self.manifest["ota"]
            checksum = self.manifest["custom_haos"]["raucb_sha256"]
            self.installing = True
            self.message = "Download e verifica del bundle firmato"
        threading.Thread(target=self._install_worker, args=(url, checksum), daemon=True).start()

    def _install_worker(self, url: str, expected_sha256: str) -> None:
        bundle_path: Path | None = None
        try:
            bundle_path = download_bundle(url, expected_sha256)
            host_path = host_bundle_path(bundle_path)
            with self.lock:
                self.message = "Installazione nello slot inattivo"
            gdbus_call(
                f"{RAUC_INTERFACE}.InstallBundle",
                str(host_path),
                "{}",
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
                        self.message = "Aggiornamento installato. Riavvia per attivarlo"
                        self.update_available = False
                    if bool(load_options()["reboot_after_install"]):
                        self._reboot_host()
                    return
                time.sleep(5)
            raise TimeoutError("Timed out waiting for RAUC to complete")
        except Exception as err:
            with self.lock:
                self.message = f"Installazione non riuscita: {err}"
        finally:
            if bundle_path is not None:
                bundle_path.unlink(missing_ok=True)
            with self.lock:
                self.installing = False

    @staticmethod
    def _reboot_host() -> None:
        token = supervisor_token()
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
        if installing:
            tone = "working"
            status_title = "Aggiornamento in corso"
        elif message.startswith(("Controllo non riuscito", "Installazione non riuscita")):
            tone = "error"
            status_title = "Intervento necessario"
        elif available:
            tone = "ready"
            status_title = "Aggiornamento pronto"
        elif current == "unknown":
            tone = "working"
            status_title = "Controllo del sistema"
        else:
            tone = "ok"
            status_title = "Sistema aggiornato"
        install_button = ""
        if available and not installing:
            install_button = (
                '<form method="post" action="install">'
                '<button class="primary" type="submit">Installa aggiornamento</button></form>'
            )
        refresh = '<meta http-equiv="refresh" content="5">' if installing else ""
        page = f"""<!doctype html>
<html lang="it"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
{refresh}<title>HAOS CIK Updater</title>
<style>
:root {{ color-scheme: light dark; --navy:#0b1f2a; --blue:#18a4e0; --paper:#f4f8fa;
  --ink:#17313d; --muted:#607985; --line:#d5e2e8; --ok:#16865c; --ready:#b66a12;
  --error:#b42318; --white:#fff; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; min-height:100vh; background:var(--paper); color:var(--ink);
  font-family:Inter,ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif; }}
main {{ width:min(760px,calc(100% - 32px)); margin:0 auto; padding:40px 0 56px; }}
header {{ display:flex; align-items:center; gap:16px; margin-bottom:30px; }}
.mark {{ width:52px; height:52px; display:grid; place-items:center; border-radius:12px;
  background:var(--navy); color:var(--white); font-weight:800; letter-spacing:-.04em; }}
h1 {{ margin:0; font-size:clamp(1.65rem,4vw,2.35rem); letter-spacing:-.035em; line-height:1.05; }}
.subtitle {{ margin:6px 0 0; color:var(--muted); font-size:.98rem; }}
.status {{ border-left:5px solid var(--blue); background:var(--white); padding:22px 24px;
  box-shadow:0 12px 34px rgba(18,54,70,.08); }}
.status.ok {{ border-color:var(--ok); }} .status.ready {{ border-color:var(--ready); }}
.status.error {{ border-color:var(--error); }}
.status-line {{ display:flex; align-items:center; gap:10px; margin-bottom:8px; }}
.dot {{ width:10px; height:10px; border-radius:50%; background:var(--blue); flex:none; }}
.ok .dot {{ background:var(--ok); }} .ready .dot {{ background:var(--ready); }}
.error .dot {{ background:var(--error); }}
h2 {{ margin:0; font-size:1.12rem; letter-spacing:-.015em; }}
.message {{ margin:0; color:var(--muted); line-height:1.55; overflow-wrap:anywhere; }}
.versions {{ display:grid; grid-template-columns:1fr 1fr; gap:1px; margin:26px 0;
  background:var(--line); border:1px solid var(--line); }}
.version {{ background:var(--white); padding:20px 22px; }}
.version span {{ display:block; color:var(--muted); font-size:.84rem; margin-bottom:8px; }}
.version strong {{ font-family:"SFMono-Regular",Consolas,"Liberation Mono",monospace;
  font-size:clamp(.9rem,3vw,1.08rem); overflow-wrap:anywhere; }}
.actions {{ display:flex; flex-wrap:wrap; gap:10px; align-items:center; }}
form {{ margin:0; }} button {{ appearance:none; border:1px solid var(--navy); border-radius:8px;
  padding:11px 16px; font:inherit; font-weight:700; cursor:pointer; background:transparent; color:var(--navy); }}
button.primary {{ background:var(--blue); border-color:var(--blue); color:#05202c; }}
button:focus-visible {{ outline:3px solid rgba(24,164,224,.35); outline-offset:3px; }}
.trust {{ margin:24px 0 0; color:var(--muted); font-size:.85rem; line-height:1.5; }}
@media (max-width:560px) {{ main {{ padding-top:24px; }} .versions {{ grid-template-columns:1fr; }}
  .actions, .actions form, button {{ width:100%; }} }}
@media (prefers-color-scheme:dark) {{ :root {{ --paper:#07151c; --ink:#e6f2f6; --muted:#99b0ba;
  --line:#29434e; --white:#102630; --navy:#dcecf2; }} .mark {{ background:var(--blue); color:#05202c; }}
  button {{ color:var(--ink); border-color:var(--muted); }} }}
</style></head><body><main>
<header><div class="mark">CIK</div><div><h1>Aggiornamenti HAOS CIK</h1>
<p class="subtitle">Bundle firmati per generic-x86-64</p></div></header>
<section class="status {tone}" aria-live="polite"><div class="status-line"><span class="dot"></span>
<h2>{html.escape(status_title)}</h2></div><p class="message">{html.escape(message)}</p></section>
<section class="versions"><div class="version"><span>Versione installata</span>
<strong>{html.escape(current)}</strong></div><div class="version"><span>Versione disponibile</span>
<strong>{html.escape(str(target))}</strong></div></section>
<div class="actions">{install_button}<form method="post" action="check">
<button type="submit">Controlla ora</button></form></div>
<p class="trust">Il bundle viene scaricato nella cartella condivisa, verificato con SHA-256 e installato nello slot inattivo.</p>
</main></body></html>"""
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
