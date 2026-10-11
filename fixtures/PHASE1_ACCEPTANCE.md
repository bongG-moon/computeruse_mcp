# Phase 1: actual recording, draft recovery and replay

This is a developer-only acceptance fixture, not a product dependency. It uses
synthetic data, has no network behavior, and independently logs input effects.
`prepare` only compiles; it does not open or control any desktop window.

## Prepare and connect

Run from `computer-use-mcp` with a **new** folder under `.data`:

```powershell
C:\Python313\python.exe acceptance_phase1_recording.py prepare .data\phase1-recording-20261011-a
C:\Python313\python.exe acceptance_phase1_recording.py serve .data\phase1-recording-20261011-a
```

The second command uses the existing public-MCP stdio command-server pattern.
Start a background shell helper with `-WindowStyle Hidden` if a
separate process is needed. It polls `commands/*.json` and writes matching
`responses/*.json`. Files are consumed once; use unique increasing names.
Actual image response bytes are preserved alongside the JSON as
`<command>-image-N.png` or `.jpg`; `images` records their paths, content order and
SHA-256. This lets the operator view the exact frame bound to a replay review.
The server only accepts the isolated fixture configuration and does not convert
an image observation into an automatic success verdict.

Example commands (the operator supplies desktop interaction using Computer Use):

```json
{"tool":"computer_open","arguments":{"program_id":"phase1","task_description":"Synthetic phase 1 recording acceptance","image_delivery":"vision"}}
```

```json
{"tool":"computer_process_editor","arguments":{"program_ids":["phase1"],"name":"Phase 1 recorded actions","timeout_seconds":1800}}
```

```json
{"tool":"computer_status","arguments":{"editor_id":"<returned editor_id>"}}
```

The title is `Computer Use phase 1 recording fixture`. Its program ID is `phase1`.
The source is `Phase1RecordingFixture.cs`; receipt path is the build folder's
`receipt.json`. The fixture can also be opened through the permitted Computer
Use app launch surface, using the generated EXE path in `provenance.json`.

## Test A: one small selection, distinctive repeated icons

1. Open the process editor and choose a manual image target.
2. Select only the small Play icon on row **ALPHA 001**, leaving the row text
   outside the initially selected area. Confirm once.
3. Check that the editor accepts it or gives a specific ambiguity explanation.
   If automatic context succeeds, the stored click anchor must still be on the
   ALPHA icon, and the preview should distinguish the row.
4. Repeat for the tiny native **Small UIA play** button. Image-quality rejection
   must not prevent the usable UIA button from being returned.
5. For image execution, verify that only `play_rows=["ALPHA 001"]` is appended.
   The other three icons are visually identical; selecting the wrong row is a
   failure even if the click was dispatched.

Test A is separate from recording: manually constructing a process is not
evidence that the recorder captured these actions.

## Test B: real recorder, native dropdown and save/reopen

1. Use **Reset fixture** before starting. Wait for recorder readiness, then
   **동작 녹화 → 기록 시작** in the actual editor.
2. Click row ALPHA's Play icon. Open the native dropdown and choose **Beta**.
3. Open and choose, in order: **Gamma → Alpha → Beta → Gamma → Alpha → Beta**.
   This is seven dropdown openings and seven changed values in total.
4. Click **Query** once. Click the Play icon on **GAMMA 003** once.
5. Stop with **기록 마치고 검토**. Inspect the real recorded sequence, warnings
   and popup bindings before saving. In particular, an ordinary related popup
   must not be labeled an unrelated cover.
6. Save and close. Save an independent copy of `receipt.json` as
   `demonstration-receipt.json` before any reset. Reopen the saved process and
   confirm targets, selected values and ordering remain present.

The demonstration receipt must have:

```json
{
  "selection": "Beta",
  "dropdown_opened": 7,
  "play_rows": ["ALPHA 001", "GAMMA 003"],
  "selections": ["Beta", "Gamma", "Alpha", "Beta", "Gamma", "Alpha", "Beta"],
  "queries": ["Beta"]
}
```

The event log also records each open, committed selection and query. It can
distinguish a value that was only typed/read from one actually applied by the
native control.

## Test C: incomplete draft recovery

1. In a **separate** draft, exercise one genuine unresolved case (for example a
   related popup whose pre-action capture was unavailable). Do not fabricate a
   successful native recording event.
2. Save with the unresolved stage. It must persist as an incomplete draft, with
   unresolved count and clear execution readiness.
3. Close and reopen. Verify the completed steps, values and original order were
   preserved. Execution must not silently skip the unresolved step.
4. Repair only that step using the picker, then save again. Warnings proven
   resolved must clear, and warnings for other unresolved steps must remain.

For deliberate external occlusion, the fixture's **Open unrelated cover** opens
an ownerless window in the same process. It deliberately covers part of the
painted rows. It is *not* an authorized related popup merely because its PID
matches. Do not confuse clicking the cover itself with sending an action through
it. This negative case must not be counted as a successful related-dropdown
recording. Close the cover with X before positive recording/replay.

## Test D: fresh saved-process replay

1. Close the editor; reset the fixture through Computer Use outside recording.
2. Run the **saved task**, not a reconstructed in-memory step list:

```json
{"tool":"computer_run_task","arguments":{"task_id":"<saved task_id>"}}
```

3. When image review is requested, inspect the supplied current frame and use
   the exact returned review IDs. Never acknowledge an actual-human checkpoint
   without that human's confirmation; never replay a click with an unknown
   result just to make progress.
4. Compare receipt against the demonstrated action effects. The required
   semantic effects are exact `play_rows`, `selections` and `queries` above.
   Replay may use a verified semantic selection without reopening the dropdown;
   do **not** require `dropdown_opened=7` for replay if that route is intended.
5. Repeat from a clean reset three times, saving each independent receipt and
   MCP final result. Check no duplicated query or wrong-row Play click occurred.

An optional exact receipt check (expected JSON contains the subset to assert):

```powershell
C:\Python313\python.exe acceptance_phase1_recording.py verify .data\phase1-recording-20261011-a --expected .data\phase1-recording-20261011-a\replay-expected.json
```

This writes `replay-expected-verification.json`; it verifies independent effects
only. A passing receipt check is not by itself proof that recording, reopening,
all review steps or the complete saved workflow succeeded. Keep those source
receipts and final MCP responses as separate evidence. Existing UI tests must
run serially so one fixture does not cover another test's window.
