import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from session_runtime import loaded_code_identity


class LoadedCodeTests(unittest.TestCase):
    def test_loaded_code_change_changes_identity_without_source_file(self):
        module = types.ModuleType("identity_test_fixture")
        module.__file__ = str(Path(__file__).with_name("identity_test_fixture.py"))
        with patch.dict(sys.modules, {module.__name__: module}):
            exec("def value(): return 1", module.__dict__)
            first = loaded_code_identity()
            exec("def value(): return 2", module.__dict__)
            second = loaded_code_identity()
        self.assertNotEqual(first["modules"][module.__name__], second["modules"][module.__name__])
        self.assertNotEqual(first["sha256"], second["sha256"])


if __name__ == "__main__":
    unittest.main()
