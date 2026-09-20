from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPT = Path(__file__).parent / "rootfs/opt/haos-cik-updater/updater.py"
SPEC = importlib.util.spec_from_file_location("updater", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
updater = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(updater)


class Response(io.BytesIO):
    def __enter__(self) -> Response:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


class UpdaterTests(unittest.TestCase):
    def test_reads_installed_version_from_supervisor_api(self) -> None:
        response = Response(
            json.dumps({"result": "ok", "data": {"version": "18.3.20260920013543"}}).encode()
        )
        with (
            mock.patch.dict(updater.os.environ, {"SUPERVISOR_TOKEN": "test-token"}),
            mock.patch.object(updater.urllib.request, "urlopen", return_value=response) as urlopen,
        ):
            self.assertEqual(updater.supervisor_os_version(), "18.3.20260920013543")
        request = urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "http://supervisor/os/info")
        self.assertEqual(request.headers["Authorization"], "Bearer test-token")

    def test_downloads_bundle_larger_than_rauc_http_limit_and_checks_hash(self) -> None:
        payload = b"x" * (8 * 1024 * 1024 + 1)
        expected = hashlib.sha256(payload).hexdigest()
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "update.raucb"
            with mock.patch.object(
                updater.urllib.request,
                "urlopen",
                return_value=Response(payload),
            ):
                result = updater.download_bundle(
                    "https://example.invalid/update.raucb", expected, destination
                )
            self.assertEqual(result, destination)
            self.assertEqual(destination.read_bytes(), payload)

    def test_rejects_bundle_with_wrong_hash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "update.raucb"
            with mock.patch.object(
                updater.urllib.request,
                "urlopen",
                return_value=Response(b"truncated"),
            ):
                with self.assertRaisesRegex(ValueError, "checksum"):
                    updater.download_bundle(
                        "https://example.invalid/update.raucb", "0" * 64, destination
                    )
            self.assertFalse(destination.exists())

    def test_maps_shared_bundle_to_host_path(self) -> None:
        self.assertEqual(
            updater.host_bundle_path(Path("/share/haos-amdgpu-cik-update.raucb")),
            Path("/mnt/data/supervisor/share/haos-amdgpu-cik-update.raucb"),
        )


if __name__ == "__main__":
    unittest.main()
