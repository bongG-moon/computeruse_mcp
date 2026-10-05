"""Build developer-only native Windows fixtures; compilation never launches UI.

The generated WinForms executables and synthetic state stay under .data and are
not runtime dependencies or distribution files. No packages are downloaded.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess


HERE = Path(__file__).resolve().parent
DATA = HERE / ".data"
SOURCE = r'''using System;
using System.Collections.Generic;
using System.Drawing;
using System.IO;
using System.Reflection;
using System.Web.Script.Serialization;
using System.Windows.Forms;

internal sealed class NativeFixture : Form {
    private readonly string outputPath;
    private readonly string instance;
    private readonly TextBox work = new TextBox();
    private readonly ComboBox status = new ComboBox();
    private readonly CheckBox confirm = new CheckBox();
    private readonly TextBox applied = new TextBox();
    private readonly TextBox choiceResult = new TextBox();
    private readonly TextBox checkResult = new TextBox();
    private readonly TextBox keyResult = new TextBox();
    private readonly TextBox applyCount = new TextBox();
    private readonly TextBox choiceCount = new TextBox();
    private readonly TextBox dialogResult = new TextBox();
    private int applications, changes, keys;
    private int detailOpenCount;
    private string lastKey = "없음";
    private string detailInput = "", scopeLeft = "", scopeRight = "";

    private static void Identify(Control control, string id, string name) {
        control.Name = id; control.AccessibleName = name;
    }
    private void Row(string caption, Control control, int y, string id) {
        Label label = new Label(); label.Text = caption; label.Location = new Point(24, y + 5);
        label.Size = new Size(145, 25); label.TabStop = false; Controls.Add(label);
        Identify(control, id, caption); control.Location = new Point(178, y);
        control.Size = new Size(375, 29); Controls.Add(control);
    }
    private void ReadOnlyRow(string caption, TextBox control, int y, string id, string value) {
        control.ReadOnly = true; control.TabStop = false; control.Text = value;
        Row(caption, control, y, id);
    }
    private void SaveState() {
        if (String.IsNullOrEmpty(outputPath)) return;
        var data = new Dictionary<string, object>();
        data["synthetic_fixture"] = true; data["instance"] = instance;
        data["input"] = work.Text; data["status"] = status.Text;
        data["checked"] = confirm.Checked; data["apply_count"] = applications;
        data["selection_count"] = changes; data["key_count"] = keys;
        data["last_key"] = lastKey; data["applied"] = applied.Text;
        data["dialog_result"] = dialogResult.Text;
        data["detail_open_count"] = detailOpenCount; data["detail_input"] = detailInput;
        data["scope_left"] = scopeLeft; data["scope_right"] = scopeRight;
        File.WriteAllText(outputPath, new JavaScriptSerializer().Serialize(data), new System.Text.UTF8Encoding(false));
    }
    private void OpenDetail(object sender, EventArgs args) {
        {
            var detail = new Form();
            detail.Text = "Native Fixture Detail - " + instance;
            detail.Name = "DetailWindow"; detail.AccessibleName = detail.Text;
            detail.StartPosition = FormStartPosition.CenterParent; detail.ClientSize = new Size(440, 280);
            detail.Font = Font; detail.FormBorderStyle = FormBorderStyle.FixedDialog;
            detail.MinimizeBox = false; detail.MaximizeBox = false;
            var input = new TextBox(); Identify(input, "DetailInput", "보조 입력");
            input.Location = new Point(24, 25); input.Size = new Size(385, 29); detail.Controls.Add(input);
            input.TextChanged += delegate { detailInput = input.Text; SaveState(); };
            for (int side = 0; side < 2; side++) {
                bool left = side == 0;
                var group = new GroupBox(); group.Text = left ? "왼쪽 그룹" : "오른쪽 그룹";
                Identify(group, left ? "LeftGroup" : "RightGroup", group.Text);
                group.Location = new Point(left ? 24 : 230, 75); group.Size = new Size(179, 95);
                var field = new TextBox(); Identify(field, left ? "ScopeLeft" : "ScopeRight", "범위 입력");
                field.Location = new Point(12, 35); field.Size = new Size(155, 29);
                field.TextChanged += delegate { if (left) scopeLeft = field.Text; else scopeRight = field.Text; SaveState(); };
                group.Controls.Add(field); detail.Controls.Add(group);
            }
            var button = new Button(); Identify(button, "DetailConfirm", "보조 확인");
            button.Text = "보조 확인"; button.Location = new Point(279, 215); button.Size = new Size(130, 35);
            button.Click += delegate { dialogResult.Text = "보조: " + input.Text; SaveState(); detail.Close(); };
            detail.Controls.Add(button); detail.AcceptButton = button;
            detailOpenCount++; dialogResult.Text = "열림"; SaveState();
            detail.Show(this);
        }
    }
    public NativeFixture(string title, string statePath, string label) {
        instance = label; outputPath = statePath;
        Text = title; Identify(this, "NativeFixtureWindow", title);
        Font = new Font("Malgun Gothic", 10F); ClientSize = new Size(595, 695);
        FormBorderStyle = FormBorderStyle.FixedSingle; MaximizeBox = false;
        StartPosition = FormStartPosition.Manual;
        Location = label == "A" ? new Point(70, 70) : new Point(700, 70);
        KeyPreview = true;
        var heading = new Label(); heading.Text = "네이티브 화면 조작 시험 " + label;
        heading.Font = new Font(Font.FontFamily, 15F, FontStyle.Bold);
        heading.Location = new Point(24, 18); heading.Size = new Size(535, 35); Controls.Add(heading);
        var note = new Label(); note.Text = "합성 자료만 사용하며 네트워크 연결이 없습니다.";
        note.Location = new Point(24, 57); note.Size = new Size(535, 27); Controls.Add(note);
        Row("작업 문구", work, 96, "WorkText");
        status.DropDownStyle = ComboBoxStyle.DropDownList;
        status.Items.AddRange(new object[] { "대기", "진행", "완료" }); status.SelectedIndex = 0;
        Row("처리 상태", status, 140, "StatusChoice");
        Identify(confirm, "ConfirmCheck", "확인 체크"); confirm.Text = "확인 체크";
        confirm.Location = new Point(178, 184); confirm.Size = new Size(200, 29); Controls.Add(confirm);
        var apply = new Button(); Identify(apply, "ApplyButton", "적용"); apply.Text = "적용";
        apply.Location = new Point(178, 225); apply.Size = new Size(170, 36); Controls.Add(apply);
        var detail = new Button(); Identify(detail, "DetailButton", "보조 창 열기"); detail.Text = "보조 창 열기";
        detail.Location = new Point(363, 225); detail.Size = new Size(190, 36); detail.Click += OpenDetail; Controls.Add(detail);
        ReadOnlyRow("적용 결과", applied, 279, "AppliedResult", "확인 전");
        ReadOnlyRow("선택 결과", choiceResult, 321, "ChoiceResult", "대기");
        ReadOnlyRow("체크 결과", checkResult, 363, "CheckResult", "해제");
        ReadOnlyRow("키 입력 결과", keyResult, 405, "KeyResult", "없음");
        ReadOnlyRow("적용 횟수", applyCount, 447, "ApplyCount", "0");
        ReadOnlyRow("선택 변경 횟수", choiceCount, 489, "ChoiceCount", "0");
        ReadOnlyRow("보조 창 결과", dialogResult, 531, "DialogResult", "확인 전");
        var items = new ListBox(); Identify(items, "WorkItems", "작업 목록");
        items.Items.AddRange(new object[] { "합성 작업 1", "합성 작업 2", "합성 작업 3" });
        items.Location = new Point(178, 578); items.Size = new Size(375, 79); Controls.Add(items);
        status.SelectedIndexChanged += delegate { changes++; choiceResult.Text = status.Text; choiceCount.Text = changes.ToString(); SaveState(); };
        confirm.CheckedChanged += delegate { checkResult.Text = confirm.Checked ? "선택" : "해제"; SaveState(); };
        work.TextChanged += delegate { SaveState(); };
        apply.Click += delegate { applications++; applied.Text = "확인: " + work.Text; applyCount.Text = applications.ToString(); SaveState(); };
        KeyDown += delegate(object sender, KeyEventArgs e) { keys++; lastKey = e.KeyCode.ToString(); keyResult.Text = lastKey; SaveState(); };
        Shown += delegate { work.Focus(); SaveState(); };
    }
    [STAThread] public static void Main(string[] args) {
        string executable = Assembly.GetExecutingAssembly().GetName().Name;
        string label = executable.EndsWith("B") ? "B" : "A";
        string title = args.Length > 0 ? args[0] : "Computer Use Native Fixture " + label;
        string state = args.Length > 1 ? args[1] : "";
        Application.EnableVisualStyles(); Application.SetCompatibleTextRenderingDefault(false);
        Application.Run(new NativeFixture(title, state, label));
    }
}
'''


def fixture_folder(path: Path) -> Path:
    path = path.resolve()
    try:
        relative = path.relative_to(DATA.resolve())
    except ValueError:
        raise ValueError("Native fixture builds must stay under computer-use-mcp/.data") from None
    if not relative.parts:
        raise ValueError("Use a dedicated fixture subfolder")
    for candidate in [path, *path.parents]:
        if candidate == DATA.parent:
            break
        if candidate.exists() and (candidate.is_symlink() or getattr(candidate.lstat(), "st_file_attributes", 0) & 0x400):
            raise ValueError("Fixture build folder cannot be a linked path")
    return path


def compiler_path() -> Path:
    windows = Path(os.environ.get("WINDIR", r"C:\Windows"))
    for framework in ("Framework64", "Framework"):
        path = windows / "Microsoft.NET" / framework / "v4.0.30319" / "csc.exe"
        if path.is_file():
            return path
    raise FileNotFoundError("Existing Windows .NET Framework compiler is unavailable; no downloads attempted")


def build_fixtures(destination: Path) -> dict:
    destination = fixture_folder(destination)
    destination.mkdir(parents=True, exist_ok=True)
    source = destination / "NativeFixture.cs"
    source.write_text(SOURCE, encoding="utf-8-sig")
    compiler = compiler_path()
    apps = []
    for label in ("A", "B"):
        executable = destination / ("NativeFixture" + label + ".exe")
        completed = subprocess.run([str(compiler), "/nologo", "/target:winexe", "/platform:anycpu", "/optimize+",
            "/codepage:65001", "/reference:System.Windows.Forms.dll", "/reference:System.Drawing.dll",
            "/reference:System.Web.Extensions.dll", "/out:" + str(executable), str(source)],
            cwd=destination, capture_output=True, timeout=60, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if completed.returncode != 0 or not executable.is_file():
            raise RuntimeError("Fixture compilation failed: " + completed.stdout.decode("utf-8", "replace") + completed.stderr.decode("utf-8", "replace"))
        apps.append({"id": "native_" + label.lower(), "label": label, "exe": str(executable),
                     "sha256": hashlib.sha256(executable.read_bytes()).hexdigest()})
    manifest = {"developer_only": True, "gui_launched": False, "compiler": str(compiler), "apps": apps,
                "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
                "controls": {"input": "작업 문구", "combo": "처리 상태", "checkbox": "확인 체크",
                             "button": "적용", "list": "작업 목록", "dialog": "보조 창 열기"}}
    (destination / "fixture-build.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DATA / "native-fixtures")
    args = parser.parse_args()
    print(json.dumps(build_fixtures(args.output), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
