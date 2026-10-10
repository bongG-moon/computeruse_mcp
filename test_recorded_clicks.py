"""Recorded clicks remain one-shot actions with explicit human completion."""
import unittest

from operations import OperationError, Operations, validate_step, verification_step
from test_operations import Runtime, TARGET, snapshot, element


class RecordedClickTests(unittest.TestCase):
    def step(self, **extra):
        return {"operation": "click", "selector": {"name": "Query", "role": "Button"},
                "completion_mode": "human", **extra}

    def before(self):
        return snapshot({**element(name="Query", role="Button"), "actions": ["click"]})

    def test_only_explicit_clicks_can_defer_completion(self):
        for operation in ("click", "double_click", "right_click"):
            self.assertEqual(validate_step(self.step(operation=operation))["completion_mode"], "human")
        for extra in ({"operation": "set_value", "value": "x"}, {"operation": "press_key", "key": "Enter"},
                      {"completion_mode": "auto"}, {"window_transition": {"mode": "auto"}}, {"expect": []}):
            with self.subTest(extra=extra), self.assertRaises(OperationError):
                validate_step(self.step(**extra))
        with self.assertRaises(OperationError):
            verification_step(self.step())

    def test_dispatch_is_not_completion_and_observation_is_not_reused(self):
        runtime = Runtime([self.before(), self.before()])
        engine = Operations(runtime)
        result = engine.execute(self.step(), TARGET)
        self.assertTrue(result["input_dispatched"])
        self.assertTrue(result["verification_deferred"])
        self.assertFalse(result["task_verified"])
        self.assertEqual(result["status"], "needs_review")
        self.assertEqual([c[0] for c in runtime.calls], ["get_window_state", "click"])
        engine.execute(self.step(), TARGET, reuse_verified=True)
        self.assertEqual([c[0] for c in runtime.calls], ["get_window_state", "click", "get_window_state", "click"])

    def test_explicit_refusal_and_uncertain_delivery_are_not_deferred(self):
        for action, sent in (({"isError": True, "structuredContent": {"input_sent": False, "error_code": "background_unavailable"}}, False),
                             (RuntimeError("transport closed"), True)):
            runtime = Runtime([self.before()], [action])
            result = Operations(runtime).execute(self.step(), TARGET)
            self.assertFalse(result["task_verified"])
            self.assertEqual(result["input_dispatched"], sent)
            self.assertFalse(result.get("verification_deferred", False))
            self.assertEqual([c[0] for c in runtime.calls].count("click"), 1)
            self.assertEqual([c[0] for c in runtime.calls].count("get_window_state"), 1)

    def test_ambiguous_target_never_clicks(self):
        runtime = Runtime([snapshot(element(name="Query", role="Button"), element(1, name="Query", role="Button"))])
        result = Operations(runtime).execute(self.step(), TARGET)
        self.assertFalse(result["input_dispatched"])
        self.assertEqual([c[0] for c in runtime.calls], ["get_window_state"])


if __name__ == "__main__":
    unittest.main()
