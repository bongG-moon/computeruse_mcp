# FlaUI 5.0.0 comparison — 2026-10-17

Decision: retain the existing native UIA reader in the production path. The
comparison did not demonstrate a target-recognition recovery that justifies a
second installed runtime. This is a measured decision, not an assertion that
FlaUI is unsuitable for other applications.

## What was actually tested

Official, version-pinned FlaUI.Core/UIA2/UIA3 5.0.0 NuGet assemblies were compiled
into a separate read-only .NET Framework 4.8 helper. Existing MuMu, Chrome,
Notepad++ and Calculator windows were read without activating, clicking,
launching or editing them. Each backend used the same HWND, PID, selector,
depth 12 and 600-element ceiling. The helper process was retained for three
requests and terminated by its Python owner on a six-second timeout.

This is an engine-reading comparison, **not** recording/replay validation.
The end-to-end product acceptance run is separate. Names/values from user
documents were not persisted in the benchmark; the report stores counts,
control roles, duration and error codes.

| Existing window | Current native | FlaUI UIA2 | FlaUI UIA3 |
|---|---:|---:|---:|
| MuMu | 48 controls / 9 named | 48 / 9 | 41 / 2 |
| Notepad++ | 106 controls / 62 named | 106 / 62 | 104 / 57 |
| Chrome | Cross-PID provider rejected | Same rejection | Same rejection |
| Calculator host frame | Duplicate window selector rejected | Same rejection | Same rejection |

Local evidence: `.data/flaui018/comparison-final.json`. The last three-sample
median read durations were MuMu 110.61 / 133.11 / 117.40 ms and Notepad++
154.67 / 151.71 / 332.39 ms (native/UIA2/UIA3). These are small, sequential
local samples including a cold first request, **not** a general speed claim or
an application-action latency benchmark. Earlier samples fluctuated with load.

Chrome and Calculator exclusions do not prove missing accessibility. A browser
renderer or hosted app can use a different PID; this test deliberately keeps the
product's single-PID read boundary. Production hosted-window handling is a
separate guarded path and must not be weakened to make a comparison pass.
Calculator's title+Window selector matched both its frame and nested window;
all three readers correctly refused to silently choose one.

## Direct selection comparison

The unchanged `ElementPicker.Observe` was compiled with a non-interactive test
entry point and compared with FlaUI `FromPoint` at the two MuMu view toggles
and the first-row play position. Physical coordinates were derived from the
visible screenshot and corrected for the two-times screenshot display scale.

- Both view toggles have the same anonymous CheckBox/Group identity and bounds
  across all three readers. FlaUI did not supply a missing name or AutomationId.
- At the first-row play position, native UIA returned an anonymous List ancestor.
  FlaUI's stricter raw-tree ancestry proof found repeated runtime IDs and refused
  the result. No new actionable play-button identity was established.
- Three-sample warm point timings were shorter with FlaUI, but its point reader
  and the picker do different work (one row versus candidate ancestors), so the
  numbers cannot establish equivalent-work speed improvement.

Local evidence: `.data/flaui018/point-comparison-physical.json`.
Earlier `point-comparison.json` used unscaled display offsets and is **not**
the intended-button comparison; use the physical-coordinate evidence above.

Therefore, MuMu's remaining identifier problem needs the product's image target
and surrounding-context logic. Replacing System.Windows.Automation with FlaUI
alone does not establish that the recorded click can find the same button.

## Reproduce without installing user dependencies

Developer preparation only:

```powershell
python tools/prepare_flaui.py --download
python -c "from pathlib import Path; from tools.prepare_flaui import build_point_baseline; build_point_baseline(Path('.data/flaui018/FlaUINativePointBaseline.exe'))"
python tools/benchmark_flaui.py --window MuMu:CURRENT_HWND --output .data/flaui018/comparison.json
python -m unittest tools.test_flaui_comparison -v
```

After package preparation, omit `--download`: compilation is offline. Nothing is
installed globally. The helper and DLLs remain under `.data/flaui018/runtime`;
normal users do not need FlaUI, NuGet, extra settings or these commands.
The benchmark starts a hidden, read-only helper and never opens the target app.
`--point --window Name:HWND:X:Y` expects **physical pixel offsets from the window
rectangle**, not resized preview coordinates; it does not bring the window forward.

Seven preparation/protocol checks passed: fixed hashes, no implicit network,
tampered package rejection, invalid backend rejection, nonexistent HWND/PID
rejection, depth bound and timeout bound. Source does not contain input methods.
An external process deadline is still necessary because an app's UIA provider
can hang inside a COM call despite cooperative traversal checks.

## Dependencies and notices

- FlaUI.Core/UIA2/UIA3 5.0.0: MIT, source commit
  `c5e7eb7af081e68534cb6b6890a38aa791ce4168`.
- Interop.UIAutomationClient 10.19041.0: MIT.
- System.Management 8.0.0: MIT. Its .NET Framework target uses the Windows
  framework assembly; no additional System.Management DLL is bundled.
- `prepare_flaui.py` pins each official NuGet SHA-256, verifies it before
  extraction, and copies the exact LICENSE and third-party notice files beside
  any generated helper. It also emits `flaui-dependencies.json` with hashes.
- Neither the helper nor benchmark has a model, telemetry or network dependency.
  Only the explicit developer `--download` preparation reaches NuGet.

Primary sources:
[FlaUI](https://github.com/FlaUI/FlaUI/tree/v5.0.0),
[UIA2 package](https://www.nuget.org/packages/FlaUI.UIA2/5.0.0),
[UIA3 package](https://www.nuget.org/packages/FlaUI.UIA3/5.0.0),
[Interop package](https://www.nuget.org/packages/Interop.UIAutomationClient/10.19041.0).
