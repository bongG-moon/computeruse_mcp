"""Developer acceptance for installed Notepad/Excel on isolated synthetic files.

The default command only prepares files. --run launches apps and must be run
without other foreground GUI validation. All screen reads/input use this MCP;
file access only creates fixtures and independently verifies saved output.
No downloads, global client configuration changes or force-closing processes.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import time
import traceback
import uuid

from acceptance_workflows import Trial, normalized, xlsx_values
from live_validation import Client, DRIVER, decoded
from settings import default_config, save_config


HERE = Path(__file__).resolve().parent
DATA = HERE / ".data" / "generic-validation"
SEED = "CUA desktop validation synthetic seed"
TEXT = "CUA 범용 메모장 시험 가나다 ABC 123"
CELL_VALUE = "314159"


def contained_folder(path):
    path = Path(path).resolve()
    path.relative_to(DATA.resolve())
    if path == DATA.resolve():
        raise ValueError("Use an individual fixture folder below generic-validation")
    return path


def prepare(folder, driver=DRIVER, scenarios=("notepad", "excel")):
    folder = contained_folder(folder)
    driver = Path(driver).resolve(strict=True)
    if not driver.is_file():
        raise ValueError("A manually prepared Driver executable is required")
    folder.mkdir(parents=True, exist_ok=True)
    if (folder / "prepared.json").exists():
        raise ValueError("This fixture folder is already prepared; choose a new folder")
    manifest = {"folder": str(folder), "gui_launched": False, "llm_used": False,
                "driver": str(driver), "scenarios": {}, "developer_only": True}
    for scenario in scenarios:
        if scenario not in ("notepad", "excel"):
            raise ValueError("Unknown desktop scenario")
        location = folder / scenario
        location.mkdir()
        config = default_config(location / "config.json")
        config["programs"] = [p for p in config["programs"] if p["id"] == scenario]
        if len(config["programs"]) != 1 or not Path(config["programs"][0]["exe"]).is_file():
            raise FileNotFoundError("Installed test app not found: " + scenario)
        config["programs"][0]["enabled"] = True
        config.update(driver=str(driver), approval="client", max_minutes=10, max_actions=120,
                      log_detail="metadata", observation_timeout_seconds=20, state_dir=str(location))
        save_config(location / "config.json", config)
        fixture = location / ("CUA-" + folder.name + (".txt" if scenario == "notepad" else ".xlsx"))
        if scenario == "notepad":
            fixture.write_text(SEED, encoding="utf-8")
        else:
            shutil.copy2(HERE / "fixtures" / "blank.xlsx", fixture)
            if xlsx_values(fixture):
                raise AssertionError("Excel fixture is not blank")
        manifest["scenarios"][scenario] = {"folder": str(location), "fixture": str(fixture),
                                           "executable": config["programs"][0]["exe"]}
    (folder / "prepared.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


class DesktopTrial(Trial):
    """Reuse the established stdio test helpers without their historical paths."""
    def __init__(self, scenario, prepared, bundle=None):
        self.folder = contained_folder(prepared["folder"])
        self.fixture = Path(prepared["fixture"]).resolve(strict=True)
        self.fixture.relative_to(self.folder)
        self.id = self.folder.parent.name + "-" + scenario
        self.config = json.loads((self.folder / "config.json").read_text(encoding="utf-8-sig"))
        # Preparation may run under a restricted account that cannot query
        # registered Windows packages. Resolve this test config again in the
        # actual interactive execution context; never change a user's profile.
        discovered = next(p for p in default_config(self.folder / "config.json")["programs"] if p["id"] == scenario)
        existing = self.config["programs"][0]
        if not discovered.get("exe"):
            raise AssertionError("Current user's installed test app could not be resolved")
        existing["control_exes"] = list(dict.fromkeys([discovered["exe"], *discovered.get("control_exes", [])]))
        save_config(self.folder / "config.json", self.config)
        self.apps = {p["id"]: p for p in self.config["programs"]}
        self.started = time.monotonic()
        self.steps = []
        self.summary = {"id": self.id, "scenario": scenario, "attempt": 1, "llm_used": False,
                        "fixture_folder": str(self.folder), "fixture_file": str(self.fixture),
                        "human_interventions": 0, "personal_documents_modified": False,
                        "new_perform_operations": [], "synthetic_cleanup_complete": False}
        if bundle:
            from build_portable import runtime_environment
            bundle = Path(bundle).resolve(strict=True)
            runtime = bundle / "runtime"
            self.client = Client(self.folder / "config.json", bundle / "server.py", runtime / "python.exe", runtime_environment(runtime))
        else:
            self.client = Client(self.folder / "config.json")
        self.schemas = {t["name"]: t["inputSchema"] for t in self.client.request("tools/list")["tools"]}
        (self.folder / "schemas.json").write_text(json.dumps(self.schemas, ensure_ascii=False, indent=2), encoding="utf-8")
        if not {"computer_inspect", "computer_perform"} <= set(self.schemas):
            self.client.close()
            raise AssertionError("Server must expose computer_inspect and computer_perform")

    def ensure_fixture_window(self, target):
        windows = decoded(self.tool("list_windows", {"pid": target["pid"], "on_screen_only": True}))["windows"]
        matches = [w for w in windows if w["window_id"] == target["window_id"]
                   and self.fixture.stem in w.get("title", "")]
        if len(matches) != 1:
            raise AssertionError("Exact synthetic document is no longer the active target")

    def inspect(self, target):
        self.ensure_fixture_window(target)
        return decoded(self.tool("computer_inspect", dict(target, max_controls=200, max_depth=32, max_elements=5000)))["inspection"]

    def inspected_control(self, target, role, name=None, operation=None):
        view = self.inspect(target)
        matches = [c for c in view["controls"] if c.get("role") == role
                   and (name is None or c.get("name") == name)
                   and (operation is None or operation in c.get("suggested_operations", []))
                   and c.get("selector_unique") is True]
        if len(matches) != 1:
            raise AssertionError(f"Expected one inspected {role}/{name}/{operation}; found {len(matches)}")
        return matches[0]

    def perform(self, target, step):
        self.ensure_fixture_window(target)
        result = decoded(self.tool("computer_perform", dict(target, step=step, delivery_mode="foreground")))
        if result.get("task_verified") is not True:
            raise AssertionError("Operation was not verified: " + json.dumps(result, ensure_ascii=False))
        self.summary["new_perform_operations"].append({"operation": step["operation"], "status": result["status"],
                                                        "metrics": result.get("metrics")})
        return result

    def observe(self, target):
        self.ensure_fixture_window(target)
        return decoded(self.tool("get_window_state", dict(target, max_depth=32, max_elements=5000)))

    def wait_closed(self, target, timeout=8):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            windows = decoded(self.tool("list_windows", {"pid": target["pid"], "on_screen_only": True}))["windows"]
            if not any(w["window_id"] == target["window_id"] and self.fixture.stem in w.get("title", "") for w in windows):
                return
            time.sleep(.2)
        raise AssertionError("Synthetic document did not close")

    def close_notepad_tab(self, target):
        state = self.observe(target)
        tabs = [e for e in state["elements"] if e.get("role") == "TabItem" and e.get("selected") is True
                and e.get("label", "").startswith(self.fixture.name)]
        if len(tabs) != 1:
            raise AssertionError("Cannot identify the selected synthetic Notepad tab")
        buttons = [e for e in state["elements"] if e.get("role") == "Button" and e.get("label") == "탭 닫기"
                   and e.get("parent_index") == tabs[0].get("element_index")]
        if len(buttons) != 1:
            raise AssertionError("Synthetic tab close button is not uniquely linked to its tab")
        result = self.tool("click", dict(target, element_token=buttons[0]["element_token"], delivery_mode="foreground"), allow_error=True)
        self.wait_closed(target)
        if result.get("isError"):
            self.summary.setdefault("close_acknowledgement_errors", []).append({"app": "notepad", "outcome": "synthetic tab disappearance independently confirmed; no click replay"})

    def close_excel(self, target):
        state = self.observe(target)
        buttons = [e for e in state["elements"] if e.get("role") == "Button" and e.get("label") == "닫기"
                   and "invoke" in e.get("actions", []) and type(e.get("depth")) is int]
        if not buttons:
            raise AssertionError("Synthetic Excel close control missing")
        depth = min(e["depth"] for e in buttons)
        buttons = [e for e in buttons if e["depth"] == depth]
        if len(buttons) != 1:
            raise AssertionError("Synthetic Excel close control ambiguous")
        result = self.tool("click", dict(target, element_token=buttons[0]["element_token"], delivery_mode="background"), allow_error=True)
        self.wait_closed(target)
        if result.get("isError"):
            self.summary.setdefault("close_acknowledgement_errors", []).append({"app": "excel", "outcome": "synthetic window disappearance independently confirmed; no click replay"})


def condition(selector, property_name, value):
    return {"selector": selector, "property": property_name, "equals": value}


def wait_file(check, timeout=5):
    deadline = time.monotonic() + timeout
    last_error = None
    while time.monotonic() < deadline:
        try:
            if check():
                return
        except (OSError, ValueError) as error:
            last_error = error
        time.sleep(.1)
    raise AssertionError("Independent saved-file verification failed" + (": " + str(last_error) if last_error else ""))


def notepad(trial):
    trial.begin(["notepad"])
    trial.launch_fixture("notepad", trial.fixture)
    target = trial.window(trial.fixture.name, 25)
    document = trial.inspected_control(target, "Document", operation="set_value")
    if normalized(document.get("value", "")) != SEED:
        raise AssertionError("Opened Notepad content is not this synthetic fixture")
    trial.perform(target, {"operation": "set_value", "selector": document["selector"], "value": TEXT})
    file_menu = trial.inspected_control(target, "MenuItem", "파일")
    save_selector = {"name": "저장", "role": "MenuItem"}
    trial.perform(target, {"operation": "click", "selector": file_menu["selector"],
                           "expect": [condition(save_selector, "enabled", True)]})
    state = trial.observe(target)
    save = trial.element(state, role="MenuItem", label="저장")
    trial.tool("click", dict(target, element_token=save["element_token"], delivery_mode="foreground"))
    wait_file(lambda: trial.fixture.read_text(encoding="utf-8-sig") == TEXT)
    trial.summary["independent_saved_text_verified"] = True
    trial.close_notepad_tab(target)
    trial.launch_fixture("notepad", trial.fixture)
    reopened = trial.window(trial.fixture.name, 25)
    document = trial.inspected_control(reopened, "Document", operation="set_value")
    if document.get("value") != TEXT:
        raise AssertionError("Reopened Notepad screen differs from saved synthetic text")
    trial.perform(reopened, {"operation": "assert", "expect": [condition(document["selector"], "value", TEXT)]})
    trial.close_notepad_tab(reopened)
    trial.summary.update(saved_and_reopened=True, synthetic_cleanup_complete=True,
                         sha256=hashlib.sha256(trial.fixture.read_bytes()).hexdigest(),
                         save_route="observed native File/Save menu through guarded screen tools")


def excel(trial):
    trial.begin(["excel"])
    trial.launch_fixture("excel", trial.fixture, "/x")
    target = trial.window(trial.fixture.stem, 35)
    cell = trial.inspected_control(target, "DataItem", "A1")
    if "set_value" in cell.get("suggested_operations", []):
        raise AssertionError("DataItem is not part of this version's verified set_value contract")
    trial.perform(target, {"operation": "click", "selector": cell["selector"],
                           "expect": [condition(cell["selector"], "selected", True)]})
    state = trial.observe(target)
    cell_state = trial.element(state, role="DataItem", label="A1")
    if cell_state.get("selected") is not True:
        raise AssertionError("A1 is not selected before synthetic input")
    trial.tool("type_text", dict(target, element_token=cell_state["element_token"], text=CELL_VALUE,
                                delay_ms=30, delivery_mode="foreground"))
    trial.summary["cell_input_route"] = "guarded low-level type_text on observed A1; computer_perform set_value does not support DataItem"
    editor = trial.inspected_control(target, "Edit", "셀 편집")
    trial.summary["observed_cell_editor"] = editor["selector"]
    trial.summary["input_delay_ms"] = 30
    # A successful input dispatch does not prove all characters reached Excel.
    # Commit only after the current editor exposes the complete requested value.
    # Missing/unavailable values stop this trial; never retype or guess.
    trial.perform(target, {"operation": "assert",
                           "expect": [condition(editor["selector"], "value", CELL_VALUE)]})
    trial.summary["full_editor_value_verified_before_commit"] = True
    trial.summary["cell_commit_route"] = "explicit computer_perform press_key key_target=window after observing in-cell edit; no element-to-window fallback"
    trial.perform(target, {"operation": "press_key", "key_target": "window", "key": "return",
                           "expect": [condition(cell["selector"], "value", CELL_VALUE),
                                      condition({"name": "A2", "role": "DataItem"}, "selected", True)]})
    save = trial.inspected_control(target, "Button", "저장")
    trial.perform(target, {"operation": "click", "selector": save["selector"],
                           "expect": [condition(cell["selector"], "value", CELL_VALUE)]})
    wait_file(lambda: xlsx_values(trial.fixture) == {"A1": CELL_VALUE})
    trial.summary["independent_saved_xlsx_verified"] = True
    trial.summary["save_verification"] = "XML values read independently from saved xlsx; click acknowledgement alone was not treated as saved"
    trial.close_excel(target)
    trial.launch_fixture("excel", trial.fixture, "/x")
    reopened = trial.window(trial.fixture.stem, 35)
    cell = trial.inspected_control(reopened, "DataItem", "A1")
    trial.perform(reopened, {"operation": "assert", "expect": [condition(cell["selector"], "value", CELL_VALUE)]})
    if xlsx_values(trial.fixture) != {"A1": CELL_VALUE}:
        raise AssertionError("Reopened Excel file differs")
    trial.close_excel(reopened)
    trial.summary.update(saved_and_reopened=True, synthetic_cleanup_complete=True,
                         sha256=hashlib.sha256(trial.fixture.read_bytes()).hexdigest(),
                         excel_launch_mode="/x with isolated synthetic workbook")


def run(manifest, bundle=None):
    results = []
    for scenario, prepared in manifest["scenarios"].items():
        trial = DesktopTrial(scenario, prepared, bundle)
        try:
            globals()[scenario](trial)
        except Exception as error:
            traceback.print_exc()
            trial.summary["cleanup_note"] = "Only this isolated fixture may remain open. No forced process close or guessed dismissal was attempted."
            trial.finish(False, str(error))
        else:
            trial.finish(True)
        results.append(trial.summary)
        if not trial.summary["passed"]:
            break
    report = {"passed": len(results) == len(manifest["scenarios"]) and all(r["passed"] for r in results),
              "gui_launched": True, "llm_used": False, "results": results}
    (Path(manifest["folder"]) / "result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main(argv=None):
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", choices=("notepad", "excel", "all"), nargs="?", default="all")
    parser.add_argument("--only", choices=("notepad", "excel"), help="Run or prepare only this installed app")
    parser.add_argument("--run", action="store_true", help="Launch installed apps and exercise MCP; requires exclusive GUI validation time")
    parser.add_argument("--prepared", type=Path, help="Existing prepared.json under .data/generic-validation")
    parser.add_argument("--driver", type=Path, default=DRIVER)
    parser.add_argument("--bundle", type=Path)
    args = parser.parse_args(argv)
    if args.only and args.scenario != "all" and args.only != args.scenario:
        parser.error("scenario and --only must agree")
    scenario = args.only or args.scenario
    if args.prepared:
        prepared = args.prepared.resolve(strict=True)
        contained_folder(prepared.parent)
        manifest = json.loads(prepared.read_text(encoding="utf-8"))
        if Path(manifest["folder"]).resolve() != prepared.parent:
            raise ValueError("Prepared manifest folder mismatch")
        if scenario != "all":
            manifest["scenarios"] = {scenario: manifest["scenarios"][scenario]}
    else:
        folder = DATA / ("desktop-" + uuid.uuid4().hex[:8])
        manifest = prepare(folder, args.driver, ("notepad", "excel") if scenario == "all" else (scenario,))
    report = run(manifest, args.bundle) if args.run else manifest
    print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return 1 if args.run and not report["passed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
