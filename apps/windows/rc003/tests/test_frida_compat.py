import hashlib
import tempfile
import unittest
from pathlib import Path

from ovb_rc003 import device_profile, frida_compat


class AssetDescriptorTests(unittest.TestCase):
    def test_uses_official_release_url(self):
        self.assertTrue(
            frida_compat.FRIDA_GADGET.url.startswith(
                "https://github.com/frida/frida/releases/download/"
            )
        )

    def test_sha256_is_pinned_and_well_formed(self):
        self.assertEqual(len(frida_compat.FRIDA_GADGET.sha256), 64)
        int(frida_compat.FRIDA_GADGET.sha256, 16)


class VerifyAssetTests(unittest.TestCase):
    def test_false_when_missing(self):
        missing = Path("/nonexistent/frida-gadget.dll.xz")
        self.assertFalse(frida_compat.verify_asset(missing, frida_compat.FRIDA_GADGET))

    def test_false_when_hash_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "asset.bin"
            path.write_bytes(b"not the real gadget")
            self.assertFalse(frida_compat.verify_asset(path, frida_compat.FRIDA_GADGET))

    def test_true_when_hash_matches(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "asset.bin"
            content = b"pretend gadget bytes"
            path.write_bytes(content)
            digest = hashlib.sha256(content).hexdigest()
            asset = frida_compat.ThirdPartyAsset(
                name="test",
                version="0",
                url="https://example.invalid/a",
                sha256=digest,
                license_name="x",
                license_url="https://example.invalid/license",
            )
            self.assertTrue(frida_compat.verify_asset(path, asset))


class ReportDecodeTests(unittest.TestCase):
    def test_decodes_verified_hidogatt_buffer(self):
        self.assertEqual(
            frida_compat.decode_rc003_ioctl_output(
                bytes.fromhex("010000f10080008100")
            ),
            bytes.fromhex("f10080008100"),
        )

    def test_rejects_wrong_prefix_or_length(self):
        self.assertIsNone(frida_compat.decode_rc003_ioctl_output(b"\x01\x00\x00"))
        self.assertIsNone(
            frida_compat.decode_rc003_ioctl_output(
                bytes.fromhex("020000f10080008100")
            )
        )

    def test_extracts_nonzero_little_endian_usages(self):
        self.assertEqual(
            frida_compat.payload_usages(bytes.fromhex("f10000008100")),
            {0xF1, 0x81},
        )
        self.assertEqual(frida_compat.payload_usages(b"short"), set())


class ReportTapTests(unittest.TestCase):
    def test_emits_only_edges_for_missing_usages(self):
        reports = []
        tap = frida_compat.RC003HidReportTap(
            lambda report_id, payload: reports.append((report_id, payload)),
            enabled=False,
        )
        tap._handle_ioctl_output(bytes.fromhex("010000f10080008100"))
        tap._handle_ioctl_output(bytes.fromhex("010000f10000000000"))
        self.assertEqual(
            reports,
            [
                (1, bytes.fromhex("80008100f100")),
                (1, bytes.fromhex("f10000000000")),
            ],
        )

    def test_releases_active_usages_when_stopped(self):
        reports = []
        tap = frida_compat.RC003HidReportTap(
            lambda report_id, payload: reports.append((report_id, payload)),
            enabled=False,
        )
        tap._handle_ioctl_output(bytes.fromhex("010000f10000000000"))
        tap._release_active()
        self.assertEqual(reports[-1], (1, b"\x00" * 6))

    def test_missing_gadget_degrades_without_starting(self):
        tap = frida_compat.RC003HidReportTap(
            lambda _report_id, _payload: None,
            archive_path=Path("/nonexistent/frida-gadget.dll.xz"),
            enabled=True,
        )
        self.assertFalse(tap.available)
        self.assertIn("unavailable", tap.status)
        self.assertFalse(tap.start())

    def test_compatibility_name_accepts_custom_verified_asset(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "asset.bin"
            content = b"pretend gadget bytes"
            path.write_bytes(content)
            asset = frida_compat.ThirdPartyAsset(
                name="test",
                version="0",
                url="https://example.invalid/a",
                sha256=hashlib.sha256(content).hexdigest(),
                license_name="x",
                license_url="https://example.invalid/license",
            )
            layer = frida_compat.BackKeyCompatLayer(gadget_path=path, asset=asset)
            self.assertTrue(layer.available)
            self.assertEqual(layer.status, "ready_gadget_verified")


class TvNativeKeycodeTests(unittest.TestCase):
    """Regression coverage for HD838A Bugs/2026-08-22-tv-native-keycode-leak.md.

    The RC003 actually reports the TV button as HID usage 0x0031
    ("Keyboard 1 and !"), not 0x0035 ("Grave Accent and Tilde") that the
    upstream table shipped with. If we re-arm the wrong usage the
    DuplicateSuppressor never matches the real keystroke, the binding still
    runs, and § leaks onto the foreground app every time TV is pressed.

    These tests pin both halves of the mapping so a future refactor cannot
    silently regress the fix.
    """

    def test_tv_button_resolves_to_keyboard_1_usage(self):
        self.assertEqual(device_profile.BUTTON_USAGE_IDS[0x0031], "tv")

    def test_tv_button_does_not_resolve_to_grave_accent_usage(self):
        self.assertNotIn(0x0035, device_profile.BUTTON_USAGE_IDS)

    def test_tap_usage_to_key_maps_tv_to_digit_one(self):
        vk, scan, extended = frida_compat.TAP_USAGE_TO_KEY[0x0031]
        self.assertEqual(vk, 0x31)  # VK_1
        self.assertFalse(extended)
        # The PS/2 Set 1 make-code for "1" on a US keyboard is 0x02.
        self.assertEqual(scan, 0x02)

    def test_tap_usage_to_button_mirror_tv_to_button_name(self):
        # TAP_USAGE_TO_BUTTON is built from BUTTON_USAGE_IDS at import time,
        # so the 0x0031 -> "tv" mapping must propagate.
        from ovb_rc003.frida_compat import TAP_USAGE_TO_BUTTON
        self.assertEqual(TAP_USAGE_TO_BUTTON.get(0x0031), "tv")


if __name__ == "__main__":
    unittest.main()
