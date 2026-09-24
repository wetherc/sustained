import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import sync_support  # noqa: E402

COMPOSE = """services:
  local:
    ports:
      - "127.0.0.1:55432:5432"
    healthcheck:
      test:
        - "CMD-SHELL"
  open:
    ports:
      - "53306:3306"
      - 0.0.0.0:58080:8080
"""


class ExposedPortTests(unittest.TestCase):
    def test_a_port_without_the_loopback_address_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "compose.yaml"
            path.write_text(COMPOSE)
            self.assertEqual(
                sync_support.exposed_ports(path),
                ["53306:3306", "0.0.0.0:58080:8080"],
            )

    def test_the_committed_compose_file_binds_loopback_only(self):
        compose = ROOT / "docker" / "compose.yaml"
        self.assertEqual(sync_support.exposed_ports(compose), [])
        self.assertTrue(sync_support.load(ROOT / "support.json")["databases"])


if __name__ == "__main__":
    unittest.main()
