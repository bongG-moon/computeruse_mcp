"""Our private direct Driver must not create a cursor recording occluder."""
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from session_runtime import SAFE_TOOLS, manifest_for
from vendor.guard import DriverTransport


class DriverOverlayTests(unittest.TestCase):
    def test_private_driver_disables_overlay_without_new_tool_permissions(self):
        with tempfile.TemporaryDirectory() as folder:
            child = mock.Mock(stdin=io.StringIO(), stdout=io.StringIO(), stderr=None)
            child.poll.return_value = 0
            factory = mock.Mock(return_value=child)
            policy = {"driver": "cua-driver.exe", "run_dir": folder,
                      "driver_env": {"CUA_DRIVER_PERMISSION_MODE": "bounded"}}
            with mock.patch("vendor.guard.threading.Thread"):
                transport = DriverTransport(policy, process_factory=factory)
            try:
                args, options = factory.call_args
                self.assertEqual(args[0], ["cua-driver.exe", "mcp", "--direct", "--no-overlay"])
                self.assertEqual(options["env"]["CUA_DRIVER_PERMISSION_MODE"], "bounded")
                # Only the transport's private read-only lifecycle recovery is
                # additional; it is unavailable through the public Guard API.
                self.assertEqual(set(manifest_for(["approved.exe"], 1)["allow"]["tools"]), SAFE_TOOLS | {'start_session'})
                self.assertNotIn('start_session', SAFE_TOOLS)
                self.assertNotIn("set_agent_cursor_enabled", SAFE_TOOLS)
                self.assertNotIn("get_agent_cursor_state", SAFE_TOOLS)
                self.assertTrue(Path(folder).is_dir())
            finally:
                transport.close()

    def test_injected_transport_child_is_not_replaced_or_reconfigured(self):
        with tempfile.TemporaryDirectory() as folder:
            child = mock.Mock(stdin=io.StringIO(), stdout=io.StringIO(), stderr=None)
            child.poll.return_value = 0
            factory = mock.Mock()
            with mock.patch("vendor.guard.threading.Thread"):
                transport = DriverTransport({"run_dir": folder}, child=child, process_factory=factory)
            try:
                factory.assert_not_called()
                self.assertIs(transport.child, child)
                self.assertEqual(child.stdin.getvalue(), "")
            finally:
                transport.close()


if __name__ == "__main__":
    unittest.main()
