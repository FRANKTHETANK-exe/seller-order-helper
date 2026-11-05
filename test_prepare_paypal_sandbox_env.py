"""Filesystem-only tests; never use the real secret directory or real secrets."""
import os
from pathlib import Path
import stat
import tempfile
import unittest

from prepare_paypal_sandbox_env import SetupError, create_env_file


class CredentialSetupTests(unittest.TestCase):
    def test_creates_owner_only_file_without_echo_surface(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "private"
            parent.mkdir(mode=0o700)
            target = parent / "credentials.env"
            create_env_file(target, "fixture-id", "fixture-secret")
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(target.read_text(), 'PAYPAL_SANDBOX_CLIENT_ID="fixture-id"\nPAYPAL_SANDBOX_CLIENT_SECRET="fixture-secret"\n')

    def test_refuses_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "private"
            parent.mkdir(mode=0o700)
            target = parent / "credentials.env"
            target.write_text("preserve")
            with self.assertRaises(FileExistsError):
                create_env_file(target, "fixture-id", "fixture-secret")
            self.assertEqual(target.read_text(), "preserve")

    def test_refuses_insecure_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "private"
            parent.mkdir(mode=0o755)
            target = parent / "credentials.env"
            with self.assertRaises(SetupError):
                create_env_file(target, "fixture-id", "fixture-secret")
            self.assertFalse(target.exists())

    def test_rejects_newline_injection(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = Path(tmp) / "private"
            parent.mkdir(mode=0o700)
            target = parent / "credentials.env"
            with self.assertRaises(SetupError):
                create_env_file(target, "fixture-id\nEVIL=1", "fixture-secret")
            self.assertFalse(target.exists())


if __name__ == "__main__":
    unittest.main()
