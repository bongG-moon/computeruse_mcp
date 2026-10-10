"""Default app discovery uses current manifests and fixed activation only."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock

from builtin_programs import package_executables, builtin_catalog, query_packages, PACKAGE_QUERY
from program_launch import create_program, validate_launch_profile
from program_registration import RegistrationError
import test_program_registration as registration_fixtures


class BuiltinDiscoveryTests(unittest.TestCase):
    def row(self, **patch):
        return {"Name": "Microsoft.WindowsCalculator", "PublisherId": "8wekyb3d8bbwe",
            "InstallLocation": r"C:\Program Files\WindowsApps\Microsoft.WindowsCalculator_NEW_VERSION_x64__8wekyb3d8bbwe",
            "Executable": "CalculatorApp.exe", "ApplicationId": "App", **patch}

    def test_exact_installed_path_not_hardcoded_version_or_user(self):
        row = self.row()
        catalog = builtin_catalog(package_rows=[row], first=lambda *args: "", app_path=lambda name: "")
        item = catalog["calculator"]
        self.assertEqual(item["exe"], row["InstallLocation"]+r"\CalculatorApp.exe")
        self.assertEqual(item["launch"], {"kind": "builtin", "id": "calculator"})
        self.assertTrue(item["available"])

    def test_untrusted_publisher_and_manifest_path_escape_rejected(self):
        for patch in ({"PublisherId": "other"}, {"Executable": r"..\Elsewhere.exe"},
                {"Executable": r"C:\Other.exe"}, {"Executable": r"\Other.exe"},
                {"Executable": r"%WINDIR%\cmd.exe"}, {"Executable": "file.ps1"},
                {"InstallLocation": r"\\server\share"}, {"Name": "Unrelated.Package"}):
            with self.subTest(patch=patch):
                self.assertFalse(package_executables([self.row(**patch)])["calculator"])

    def test_store_manifest_helpers_are_exact_deduplicated_executables(self):
        rows = [self.row(Name="Microsoft.WindowsStore", Executable=name) for name in ("WinStore.App.exe", "Helper.exe", "Helper.exe")]
        item = builtin_catalog(package_rows=rows, first=lambda *args: "", app_path=lambda name: "")["store"]
        self.assertTrue(item["exe"].endswith("WinStore.App.exe"))
        self.assertEqual(len(item["control_exes"]), 1)
        self.assertTrue(item["control_exes"][0].endswith("Helper.exe"))

    def test_missing_package_does_not_scan_other_users_or_install_apps(self):
        item = builtin_catalog(package_rows=[], first=lambda *args: "", app_path=lambda name: "")["store"]
        self.assertFalse(item["available"])
        self.assertNotIn("-AllUsers", PACKAGE_QUERY)
        self.assertNotIn("Add-AppxPackage", PACKAGE_QUERY)
        self.assertNotIn("Start-Process", PACKAGE_QUERY)

    def test_fixed_activation_does_not_accept_generic_system_protocols(self):
        for name, expected in (("calculator", "calculator:"), ("store", "ms-windows-store:")):
            launched = mock.Mock()
            result = create_program({"exe": r"C:\Application.exe", "launch": {"kind": "builtin", "id": name}}, uri_launcher=launched)
            launched.assert_called_once_with(expected, "open")
            self.assertIsNone(result.pid)
        for profile in ({"kind": "builtin", "id": "powershell"}, {"kind": "builtin", "id": "store", "target": "shell:AppsFolder"},
                {"kind": "uri", "target": "ms-windows-store://anything"}, {"kind": "uri", "target": "calculator://anything"}):
            if profile.get("target") == "calculator://anything": continue  # Existing exact registered custom URI policy.
            with self.subTest(profile=profile), self.assertRaises(ValueError): validate_launch_profile(profile)

    def test_query_failures_remain_read_only_and_bounded(self):
        fake = mock.Mock(return_value=SimpleNamespace(returncode=0, stdout=b"[]"))
        with mock.patch("builtin_programs.os.name", "nt"), mock.patch.object(Path, "is_file", return_value=True):
            self.assertEqual(query_packages(run=fake), [])
        kwargs = fake.call_args.kwargs
        self.assertEqual(kwargs["timeout"], 12)
        self.assertTrue(kwargs["capture_output"])


class BuiltinRegistrationTests(unittest.TestCase):
    setUp = registration_fixtures.RegistrationTests.setUp
    write = registration_fixtures.RegistrationTests.write
    call = registration_fixtures.RegistrationTests.call
    def found(self):
        return {"id": "calculator", "name": "Calculator", "exe": str(self.app), "control_exes": [],
            "launch": {"kind": "builtin", "id": "calculator"}, "hints": "Test app", "available": True}

    def test_builtin_registers_without_paths_and_is_idempotent(self):
        with mock.patch("builtin_programs.resolve_builtin", return_value=self.found()) as discover:
            first = self.call(builtin="calculator", name="My calculator")
            original = self.config_path.read_bytes()
            again = self.call(builtin="calculator")
        self.assertEqual(first["status"], "registered")
        self.assertEqual(first["program"]["launch"], {"kind": "builtin", "id": "calculator"})
        self.assertEqual(again["status"], "already_present")
        self.assertEqual(again["program"]["name"], "My calculator")
        self.assertEqual(self.config_path.read_bytes(), original)

    def test_public_calculator_refresh_accepts_cli_string_config_path(self):
        # server.py receives --config as text; only the older unit fixtures
        # passed a Path object. Exercise the public simple-profile refresh.
        windows = self.root / "Windows"
        legacy = windows / "System32/calc.exe"
        legacy.parent.mkdir(parents=True)
        legacy.touch()
        actual = self.root / "WindowsApps/Microsoft.WindowsCalculator_2.0.0.0_x64__8wekyb3d8bbwe/CalculatorApp.exe"
        actual.parent.mkdir(parents=True)
        actual.touch()
        value = copy.deepcopy(self.original)
        value["tool_profile"] = "simple"
        value["programs"].append({"id": "calculator", "name": "My calculator", "exe": str(legacy),
            "control_exes": [str(actual)], "enabled": True, "hints": "Keep my help"})
        self.write(value)
        self.manager = registration_fixtures.ComputerManager(
            registration_fixtures.load_config(self.config_path), config_path=str(self.config_path))
        found = {**self.found(), "exe": str(actual)}
        with mock.patch.dict("os.environ", {"WINDIR": str(windows)}), mock.patch(
                "builtin_programs.resolve_builtin", return_value=found), mock.patch.object(
                self.manager, "runtime_factory") as runtime:
            answer = self.call(builtin="calculator")
            after = self.config_path.read_bytes()
            again = self.call(builtin="calculator")
        self.assertEqual(answer["status"], "registered")
        self.assertEqual(answer["program"]["exe"], str(actual))
        self.assertEqual(answer["program"]["name"], "My calculator")
        self.assertEqual(answer["program"]["hints"], "Keep my help")
        self.assertEqual(answer["program"]["control_exes"], [])
        self.assertEqual(answer["next_tool"], "computer_open")
        self.assertEqual(again["status"], "already_present")
        self.assertEqual(self.config_path.read_bytes(), after)
        written = json.loads(after)
        self.assertEqual(written["programs"][0], value["programs"][0])
        self.assertEqual({k: v for k, v in written.items() if k != "programs"},
                         {k: v for k, v in value.items() if k != "programs"})
        runtime.assert_not_called()

    def test_builtin_cannot_override_path_arguments_or_system_protocol(self):
        with mock.patch("builtin_programs.resolve_builtin", return_value=self.found()):
            for extra in ({"exe": str(self.app)}, {"arguments": ["--anything"]}, {"launch_uri": "ms-settings://x"}, {"control_exes": []}):
                with self.subTest(extra=extra):
                    answer = self.call(builtin="calculator", **extra)
                    self.assertFalse(answer.get("ok", False))
        self.assertEqual(len(self.manager.config["programs"]), 1)

    def test_legacy_notepad_registration_refreshes_empty_controls_without_scope_expansion(self):
        actual = self.root / "InstalledNotepad.exe"
        actual.touch()
        found = {"id": "notepad", "name": "메모장", "exe": str(self.old), "control_exes": [str(actual)],
            "hints": "New default", "available": True}
        active = SimpleNamespace(state="active", programs=copy.deepcopy(self.manager.config["programs"]),
            config=copy.deepcopy(self.manager.config), guard=object())
        self.manager.session = active
        before = copy.deepcopy(active.programs), copy.deepcopy(active.config), active.guard
        with mock.patch("builtin_programs.resolve_builtin", return_value=found):
            answer = self.call(builtin="notepad")
            disk = self.config_path.read_bytes()
            repeated = self.call(builtin="notepad")
        self.assertTrue(answer["changed"])
        self.assertEqual(answer["program"]["id"], "old")
        self.assertEqual(answer["program"]["name"], "기존")
        self.assertEqual(answer["program"]["control_exes"], [str(actual)])
        self.assertFalse(answer["active_session_scope_changed"])
        self.assertEqual((active.programs, active.config, active.guard), before)
        self.assertEqual(repeated["status"], "already_present")
        self.assertEqual(self.config_path.read_bytes(), disk)
        raw = json.loads(disk)
        self.assertEqual({k: v for k, v in raw.items() if k != "programs"},
            {k: v for k, v in self.original.items() if k != "programs"})

    def test_new_package_version_replaces_only_old_family_paths_and_preserves_custom_controls(self):
        old_folder = self.root / "WindowsApps/Microsoft.WindowsCalculator_1.0.0.0_x64__8wekyb3d8bbwe"
        new_folder = self.root / "WindowsApps/Microsoft.WindowsCalculator_2.0.0.0_x64__8wekyb3d8bbwe"
        new_folder.mkdir(parents=True)
        actual, helper = new_folder / "CalculatorApp.exe", new_folder / "Helper.exe"
        actual.touch(); helper.touch()
        old = {"id": "my-calculator", "name": "My name", "exe": str(old_folder / "CalculatorApp.exe"),
            "control_exes": [str(old_folder / "OldHelper.exe"), str(self.app)], "enabled": False,
            "hints": "My hints", "launch": {"kind": "builtin", "id": "calculator"}}
        value = copy.deepcopy(self.original)
        value["programs"].append(old)
        self.write(value)
        self.manager = registration_fixtures.ComputerManager(registration_fixtures.load_config(self.config_path), config_path=self.config_path)
        found = {**self.found(), "exe": str(actual), "control_exes": [str(helper)]}
        with mock.patch("builtin_programs.resolve_builtin", return_value=found):
            answer = self.call(builtin="calculator")
        changed = answer["program"]
        self.assertTrue(answer["changed"])
        self.assertEqual(changed["id"], old["id"])
        self.assertEqual(changed["control_exes"], [str(self.app), str(helper)])
        self.assertEqual(changed["exe"], str(actual))
        for field in ("name", "hints", "enabled", "launch"):
            self.assertEqual(changed[field], old[field])
        self.assertEqual(json.loads(self.config_path.read_text(encoding="utf-8"))["programs"][0], value["programs"][0])

    def test_builtin_refresh_rejects_config_race_and_preserves_other_writer(self):
        found = {"id": "notepad", "name": "메모장", "exe": str(self.old), "control_exes": [str(self.app)],
            "hints": "default", "available": True}
        foreign = copy.deepcopy(self.original)
        foreign["notes"] = "another writer"
        def discover(name):
            self.write(foreign)
            return found
        with mock.patch("builtin_programs.resolve_builtin", side_effect=discover):
            answer = self.call(builtin="notepad")
        self.assertFalse(answer.get("ok", False))
        self.assertEqual(json.loads(self.config_path.read_text(encoding="utf-8")), foreign)
        self.assertEqual(self.manager.config["notes"], "preserve")

    def test_builtin_does_not_replace_unrelated_entry_with_same_id(self):
        with mock.patch("builtin_programs.resolve_builtin", return_value=self.found()):
            before = self.config_path.read_bytes()
            answer = self.call(builtin="calculator", program_id="old")
        self.assertFalse(answer.get("ok", False))
        self.assertEqual(self.config_path.read_bytes(), before)


if __name__ == "__main__": unittest.main()
