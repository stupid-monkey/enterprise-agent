"""Encrypted backup format rejects wrong keys and changed ciphertext."""

import tempfile
import unittest
from pathlib import Path

from cryptography.exceptions import InvalidTag

from eval.backup_memory_bundle import decrypt, encrypt


class MemoryBackupEncryptionTests(unittest.TestCase):
    def test_roundtrip_and_authentication(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "plain"
            encrypted = root / "bundle.eab"
            source.write_bytes((b"memory-snapshot" * 10000) + b"tail")
            encrypt(source, encrypted, "synthetic-test-passphrase")
            restored = root / "restored"
            decrypt(encrypted, restored, "synthetic-test-passphrase")
            self.assertEqual(restored.read_bytes(), source.read_bytes())
            with self.assertRaises(InvalidTag):
                decrypt(encrypted, root / "wrong-key", "different-synthetic-passphrase")
            data = bytearray(encrypted.read_bytes())
            data[-17] ^= 1
            encrypted.write_bytes(data)
            with self.assertRaises(InvalidTag):
                decrypt(encrypted, root / "tampered", "synthetic-test-passphrase")


if __name__ == "__main__":
    unittest.main()
