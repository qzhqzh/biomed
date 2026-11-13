import tempfile
import unittest
from pathlib import Path

from scripts.install_runtime_definitions import DEFINITIONS, install, sha256


class RuntimeDefinitionTests(unittest.TestCase):
    def test_installs_exact_versions_and_reuses_them(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(all(state == "installed" for _, state in install(directory)))
            self.assertTrue(all(state == "reused" for _, state in install(directory)))
            for _, relative, expected in DEFINITIONS:
                self.assertEqual(sha256(Path(directory) / relative), expected)

    def test_conflicting_existing_file_is_preserved_before_any_install(self):
        with tempfile.TemporaryDirectory() as directory:
            conflicting = Path(directory) / DEFINITIONS[1][1]
            conflicting.parent.mkdir(parents=True)
            conflicting.write_text("different local configuration")
            with self.assertRaisesRegex(ValueError, "preserve it"):
                install(directory)
            self.assertEqual(conflicting.read_text(), "different local configuration")
            self.assertFalse((Path(directory) / DEFINITIONS[0][1]).exists())


if __name__ == "__main__":
    unittest.main()
