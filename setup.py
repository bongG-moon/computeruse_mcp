"""Small, independent MCP setup utility. Does not modify the Workspace app."""
from __future__ import annotations

import argparse
from collections import Counter
import copy
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext
import uuid

from settings import default_config_path, default_config, load_config, save_config, validate_config, VERSION
from consent import stop_active_runs
from vendor.windows import running_apps, discover, native_claude, hidden_flags

HERE = Path(__file__).resolve().parent


def _config_source_bytes(path):
    """Snapshot only this setup file; an absent file is distinct from an empty one."""
    try:
        if Path(path).stat().st_size > 1_000_000:
            raise ValueError("설정 파일이 너무 큽니다. 원본은 변경하지 않았습니다.")
        return Path(path).read_bytes()
    except FileNotFoundError:
        return None


def _check_config_snapshot(path, expected):
    if _config_source_bytes(path) != expected:
        raise ValueError("설정 파일이 다른 창이나 채팅에서 변경되었습니다. 다른 변경을 덮어쓰지 않았습니다.\n"
                         "이 창에서 편집한 내용은 그대로 남아 있습니다. 필요한 내용을 복사한 뒤 설정 창을 닫고 다시 열어주세요.")


class TaskEditor(tk.Toplevel):
    """Human task descriptions only; this editor never changes allowed programs."""
    def __init__(self, parent, programs, task=None):
        super().__init__(parent)
        self.result = None
        self.original = copy.deepcopy(task or {})
        self.programs = copy.deepcopy(programs)
        self.title("저장할 작업 작성")
        self.geometry("820x720")
        self.minsize(720, 640)
        self.transient(parent)
        self.grab_set()
        body = ttk.Frame(self, padding=20)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        ttk.Label(body, text="자주 할 일을 글로 저장하세요", font=("맑은 고딕", 16, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(body, text="저장한 설명은 다음 요청에 다시 사용할 수 있습니다. 저장 자체로 실행되거나 프로그램 권한이 늘어나지 않습니다.", wraplength=750).grid(row=1, column=0, sticky="w", pady=(8, 14))
        ttk.Label(body, text="작업 이름").grid(row=2, column=0, sticky="w")
        self.name = tk.StringVar(value=self.original.get("name", ""))
        ttk.Entry(body, textvariable=self.name).grid(row=3, column=0, sticky="ew", pady=(4, 12))
        ttk.Label(body, text="할 일 — 사용할 메뉴, 순서, 입력할 내용, 멈출 지점을 적어주세요").grid(row=4, column=0, sticky="w")
        self.instructions = scrolledtext.ScrolledText(body, height=7, wrap="word")
        self.instructions.grid(row=5, column=0, sticky="nsew", pady=(4, 12))
        self.instructions.insert("1.0", self.original.get("instructions", ""))
        body.rowconfigure(5, weight=1)
        ttk.Label(body, text="완료 기준 — 실제 화면이나 파일에서 무엇을 확인하면 되나요?").grid(row=6, column=0, sticky="w")
        self.expected = scrolledtext.ScrolledText(body, height=3, wrap="word")
        self.expected.grid(row=7, column=0, sticky="ew", pady=(4, 12))
        self.expected.insert("1.0", self.original.get("expected", ""))
        ttk.Label(body, text="사용할 프로그램 (여러 개 선택 가능, 꺼진 프로그램은 실행 전 켜야 합니다)").grid(row=8, column=0, sticky="w")
        self.program_list = tk.Listbox(body, height=4, selectmode="multiple", exportselection=False)
        self.program_list.grid(row=9, column=0, sticky="ew", pady=(4, 12))
        for i, item in enumerate(self.programs):
            self.program_list.insert("end", item["name"] + ("" if item.get("enabled") else " [사용 꺼짐]"))
            if item["id"] in self.original.get("program_ids", []):
                self.program_list.selection_set(i)
        ttk.Label(body, text="작업 이름·지시·완료 기준은 이 PC에 평문으로 저장됩니다. 비밀번호나 실제 기밀을 입력하지 마세요.", wraplength=750).grid(row=10, column=0, sticky="w")
        buttons = ttk.Frame(body)
        buttons.grid(row=11, column=0, sticky="e", pady=(14, 0))
        ttk.Button(buttons, text="저장", command=self.accept).pack(side="left", padx=6)
        ttk.Button(buttons, text="취소", command=self.destroy).pack(side="left")

    def accept(self):
        value = {"name": self.name.get().strip(), "instructions": self.instructions.get("1.0", "end").strip(),
                 "expected": self.expected.get("1.0", "end").strip(),
                 "program_ids": [self.programs[i]["id"] for i in self.program_list.curselection()]}
        if any(not value[key] for key in value):
            messagebox.showinfo("작업 내용 확인", "작업 이름, 할 일, 완료 기준을 적고 프로그램을 한 개 이상 선택해주세요.", parent=self)
            return
        if self.original.get("id"):
            value["id"] = self.original["id"]
        self.result = value
        self.destroy()


class TextResult(tk.Toplevel):
    def __init__(self, parent, title, text):
        super().__init__(parent)
        self.title(title)
        self.geometry("900x710")
        self.minsize(700, 500)
        self.transient(parent)
        body = ttk.Frame(self, padding=20)
        body.pack(fill="both", expand=True)
        box = scrolledtext.ScrolledText(body, wrap="word", font=("맑은 고딕", 11))
        box.pack(fill="both", expand=True)
        box.insert("1.0", text)
        box.configure(state="disabled")
        ttk.Button(body, text="닫기", command=self.destroy).pack(anchor="e", pady=(12, 0))


_PROGRAM_COLORS = {"background": "#F3F6FB", "card": "#FFFFFF", "ink": "#172B4D",
                   "muted": "#5C6D84", "border": "#DCE4EE", "accent": "#2563EB"}


def _program_dialog_metrics(window, width, height):
    """Use current Tk font metrics; constrain the window to the available screen."""
    from tkinter import font as tkfont
    family = "맑은 고딕" if os.name == "nt" else "TkDefaultFont"
    sample = tkfont.Font(root=window, family=family, size=10)
    scale = max(0.85, min(3.0, sample.metrics("linespace") / 20.0))
    px = lambda number: max(1, round(number * scale))
    screen_width, screen_height = window.winfo_screenwidth(), window.winfo_screenheight()
    width = min(px(width), max(320, screen_width - px(44)))
    height = min(px(height), max(320, screen_height - px(80)))
    window.geometry(f"{width}x{height}+{max(0, (screen_width-width)//2)}+{max(0, (screen_height-height)//2-px(15))}")
    window.minsize(min(px(490), width), min(px(420), height))
    window.configure(background=_PROGRAM_COLORS["background"])
    return family, px


def _program_button(parent, text, command, *, family, px, primary=False):
    """Native flat buttons keep their colors under Windows themes without changing the app theme."""
    colors = _PROGRAM_COLORS
    button = tk.Button(parent, text=text, command=command, font=(family, 10, "bold" if primary else "normal"),
                       background=colors["accent"] if primary else colors["card"],
                       foreground=colors["card"] if primary else colors["ink"],
                       activebackground="#1D4ED8" if primary else "#EAF0F9",
                       activeforeground=colors["card"] if primary else colors["ink"],
                       disabledforeground="#9AA8BC", relief="flat", borderwidth=0,
                       highlightthickness=1, highlightbackground=colors["accent"] if primary else colors["border"],
                       highlightcolor="#88AEFA", padx=px(17), pady=px(9), cursor="hand2", takefocus=True)
    return button


class ProgramEditor(tk.Toplevel):
    def __init__(self, parent, program=None):
        super().__init__(parent)
        self.title("사용할 프로그램 등록")
        self.transient(parent)
        self.grab_set()
        self._family, self._px = _program_dialog_metrics(self, 680, 740)
        self.result = None
        self.original = copy.deepcopy(program or {})
        self.name = tk.StringVar(self, value=self.original.get("name", ""))
        self.exe = tk.StringVar(self, value=self.original.get("exe", ""))
        self.enabled = tk.BooleanVar(self, value=self.original.get("enabled", True))
        launch = self.original.get("launch", {})
        self.launch_uri = tk.StringVar(self, value=launch.get("target", ""))
        self.working_directory = tk.StringVar(self, value=launch.get("cwd", ""))
        self.advanced_open = tk.BooleanVar(self, value=bool(self.original.get("control_exes")))
        colors, px = _PROGRAM_COLORS, self._px
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        header = tk.Frame(self, background=colors["background"], padx=px(24), pady=px(20))
        header.grid(row=0, column=0, sticky="ew")
        self._label(header, "COMPUTER USE", color=colors["accent"], size=9, bold=True).pack(anchor="w")
        title = self._label(header, "사용할 프로그램을 등록하세요", size=18, bold=True)
        title.pack(fill="x", pady=(px(9), px(6)))
        self._wrap_with(header, title, inset=48)
        subtitle = self._label(header, "실행파일을 고르거나, 지금 열려 있는 프로그램을 선택하면 됩니다.", color=colors["muted"])
        subtitle.pack(fill="x")
        self._wrap_with(header, subtitle, inset=48)

        viewport = tk.Frame(self, background=colors["background"])
        viewport.grid(row=1, column=0, sticky="nsew")
        viewport.columnconfigure(0, weight=1)
        viewport.rowconfigure(0, weight=1)
        self.body_canvas = tk.Canvas(viewport, background=colors["background"], highlightthickness=0, borderwidth=0)
        self.body_canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(viewport, orient="vertical", command=self.body_canvas.yview)
        scrollbar.grid(row=0, column=1, sticky="ns", padx=(0, px(7)))
        self.body_canvas.configure(yscrollcommand=scrollbar.set)
        self.form = tk.Frame(self.body_canvas, background=colors["background"], padx=px(24), pady=px(2))
        self.form.columnconfigure(0, weight=1)
        self._body_id = self.body_canvas.create_window((0, 0), window=self.form, anchor="nw")
        self.form.bind("<Configure>", lambda _: self.body_canvas.configure(scrollregion=self.body_canvas.bbox("all")))
        self.body_canvas.bind("<Configure>", lambda event: self.body_canvas.itemconfigure(self._body_id, width=event.width))
        self.bind("<MouseWheel>", self._wheel)
        self.bind("<FocusIn>", self._show_focused_field)

        basic = self._card(0)
        self._label(basic, "기본 정보", bold=True, background=colors["card"]).grid(row=0, column=0, sticky="w", pady=(0, px(14)))
        self._label(basic, "프로그램 이름", background=colors["card"]).grid(row=1, column=0, sticky="w")
        self.name_entry = self._entry(basic, self.name)
        self.name_entry.grid(row=2, column=0, sticky="ew", pady=(px(6), px(14)))
        self._label(basic, "실행파일 (.exe)", background=colors["card"]).grid(row=3, column=0, sticky="w")
        self.exe_entry = self._entry(basic, self.exe)
        self.exe_entry.grid(row=4, column=0, sticky="ew", pady=(px(6), px(10)))
        file_buttons = tk.Frame(basic, background=colors["card"])
        file_buttons.grid(row=5, column=0, sticky="w")
        self._button(file_buttons, "파일 선택", self.pick_exe).pack(side="left")
        self._button(file_buttons, "열린 창에서 선택", self.pick_window).pack(side="left", padx=(px(8), 0))
        helper = self._label(basic, "파일을 고르면 이름이 자동으로 채워집니다. 원하는 이름으로 바꿔도 됩니다.",
                             background=colors["card"], color=colors["muted"], size=9)
        helper.grid(row=6, column=0, sticky="ew", pady=(px(10), px(11)))
        self._wrap_with(basic, helper, inset=36)
        tk.Checkbutton(basic, text="이 프로그램 사용 허용", variable=self.enabled, font=(self._family, 10),
                       background=colors["card"], foreground=colors["ink"], activebackground=colors["card"],
                       selectcolor=colors["card"], highlightthickness=0, anchor="w", takefocus=True).grid(row=7, column=0, sticky="w")

        guidance = self._card(1)
        self._label(guidance, "사용법  ·  선택", bold=True, background=colors["card"]).grid(row=0, column=0, sticky="w")
        hint_label = self._label(guidance, "예: 조회 메뉴를 열고 날짜를 입력합니다. 시험 문서에서만 작업합니다.",
                                 background=colors["card"], color=colors["muted"], size=9)
        hint_label.grid(row=1, column=0, sticky="ew", pady=(px(6), px(10)))
        self._wrap_with(guidance, hint_label, inset=36)
        self.hints = self._text(guidance, height=3)
        self.hints.grid(row=2, column=0, sticky="ew")
        self.hints.insert("1.0", self.original.get("hints", ""))

        launching = self._card(2)
        self._label(launching, "여는 방법  ·  선택", bold=True, background=colors["card"]).grid(row=0, column=0, sticky="w")
        launch_help = self._label(launching,
            "보통은 비워 두면 됩니다. 주소로 여는 프로그램은 실행 주소를, 바로가기에 인자가 있으면 한 줄에 하나씩 입력하세요. 위 실행파일은 실제 업무 창의 프로그램입니다.",
            background=colors["card"], color=colors["muted"], size=9)
        launch_help.grid(row=1, column=0, sticky="ew", pady=(px(6), px(10)))
        self._wrap_with(launching, launch_help, inset=36)
        self._label(launching, "실행 주소 (https:// 또는 전용 protocol://)", background=colors["card"]).grid(row=2, column=0, sticky="w")
        self._entry(launching, self.launch_uri).grid(row=3, column=0, sticky="ew", pady=(px(6), px(10)))
        self._label(launching, "EXE 실행 인자 — 한 줄에 한 개", background=colors["card"]).grid(row=4, column=0, sticky="w")
        self.arguments = self._text(launching, height=2)
        self.arguments.grid(row=5, column=0, sticky="ew", pady=(px(6), px(10)))
        self.arguments.insert("1.0", "\n".join(launch.get("arguments", [])))
        self._label(launching, "EXE 시작 폴더 — 비우면 실행파일 폴더", background=colors["card"]).grid(row=6, column=0, sticky="w")
        self._entry(launching, self.working_directory).grid(row=7, column=0, sticky="ew", pady=(px(6), 0))

        advanced = self._card(3)
        self.advanced_button = self._button(advanced, "", self.toggle_advanced)
        self.advanced_button.configure(anchor="w", highlightthickness=0, padx=0, pady=px(2))
        self.advanced_button.grid(row=0, column=0, sticky="ew")
        self.advanced_frame = tk.Frame(advanced, background=colors["card"])
        self.advanced_frame.columnconfigure(0, weight=1)
        advanced_label = self._label(self.advanced_frame,
            "처음 실행한 파일과 실제 업무 창의 실행파일이 다를 때만 입력하세요. 한 줄에 하나씩 적습니다.",
            background=colors["card"], color=colors["muted"], size=9)
        advanced_label.grid(row=0, column=0, sticky="ew", pady=(px(10), px(9)))
        self._wrap_with(advanced, advanced_label, inset=36)
        self.extra = self._text(self.advanced_frame, height=3)
        self.extra.grid(row=1, column=0, sticky="ew")
        self.extra.insert("1.0", "\n".join(self.original.get("control_exes", [])))
        scope_note = self._label(self.advanced_frame,
            "등록한 실행파일이 소유한 창들이 대상입니다. 공용 실행파일은 다른 프로그램의 창도 포함할 수 있습니다.",
            background=colors["card"], color=colors["muted"], size=9)
        scope_note.grid(row=2, column=0, sticky="ew", pady=(px(9), 0))
        self._wrap_with(advanced, scope_note, inset=36)
        self._set_advanced_visibility()

        # The footer belongs to the Toplevel, not the scrolling form.
        self.footer = tk.Frame(self, background=colors["card"], padx=px(24), pady=px(15),
                               highlightbackground=colors["border"], highlightthickness=1)
        self.footer.grid(row=2, column=0, sticky="ew")
        self.footer.columnconfigure(0, weight=1)
        footer_text = self._label(self.footer, "저장 후 설정 창에서 ‘설정 저장’을 누르면 다음 연결부터 사용할 수 있습니다.",
                                  background=colors["card"], color=colors["muted"], size=9)
        footer_text.grid(row=0, column=0, sticky="ew", pady=(0, px(11)))
        self._wrap_with(self.footer, footer_text, inset=48)
        actions = tk.Frame(self.footer, background=colors["card"])
        actions.grid(row=1, column=0, sticky="e")
        self._button(actions, "취소", self.cancel).pack(side="left", padx=(0, px(9)))
        self.save_button = self._button(actions, "저장", self.accept, primary=True)
        self.save_button.pack(side="left")
        self.save_button.bind("<Return>", lambda _: "break")
        self.save_button.bind("<KP_Enter>", lambda _: "break")
        self.bind("<Escape>", self.cancel)
        self.protocol("WM_DELETE_WINDOW", self.cancel)

    def _label(self, parent, text, *, background=None, color=None, size=10, bold=False):
        return tk.Label(parent, text=text, font=(self._family, size, "bold" if bold else "normal"),
                        background=background or _PROGRAM_COLORS["background"], foreground=color or _PROGRAM_COLORS["ink"],
                        anchor="w", justify="left")

    def _button(self, parent, text, command, primary=False):
        return _program_button(parent, text, command, family=self._family, px=self._px, primary=primary)

    def _card(self, row):
        panel = tk.Frame(self.form, background=_PROGRAM_COLORS["card"], padx=self._px(18), pady=self._px(16),
                         highlightbackground=_PROGRAM_COLORS["border"], highlightthickness=1)
        panel.grid(row=row, column=0, sticky="ew", pady=(0, self._px(14)))
        panel.columnconfigure(0, weight=1)
        return panel

    def _entry(self, parent, variable):
        return tk.Entry(parent, textvariable=variable, font=(self._family, 10), background="#FFFFFF",
                        foreground=_PROGRAM_COLORS["ink"], relief="flat", borderwidth=self._px(7),
                        highlightthickness=1, highlightbackground=_PROGRAM_COLORS["border"],
                        highlightcolor=_PROGRAM_COLORS["accent"], insertbackground=_PROGRAM_COLORS["ink"])

    def _text(self, parent, height):
        box = tk.Text(parent, height=height, width=1, wrap="word", font=(self._family, 10), background="#FFFFFF",
                      foreground=_PROGRAM_COLORS["ink"], relief="flat", borderwidth=0,
                      highlightthickness=1, highlightbackground=_PROGRAM_COLORS["border"],
                      highlightcolor=_PROGRAM_COLORS["accent"], padx=self._px(9), pady=self._px(8),
                      insertbackground=_PROGRAM_COLORS["ink"], undo=True, takefocus=True)
        box.bind("<Tab>", lambda event: (event.widget.tk_focusNext().focus_set(), "break")[1])
        box.bind("<Shift-Tab>", lambda event: (event.widget.tk_focusPrev().focus_set(), "break")[1])
        return box

    def _wrap_with(self, frame, label, inset):
        def wrap(event):
            if event.widget is frame:
                label.configure(wraplength=max(100, event.width-self._px(inset)))
        frame.bind("<Configure>", wrap, add="+")

    def _set_advanced_visibility(self):
        self.advanced_button.configure(text=("▾  " if self.advanced_open.get() else "▸  ") + "추가 조작 파일  ·  고급 설정")
        if self.advanced_open.get():
            self.advanced_frame.grid(row=1, column=0, sticky="ew")
        else:
            self.advanced_frame.grid_remove()

    def toggle_advanced(self):
        self.advanced_open.set(not self.advanced_open.get())
        self._set_advanced_visibility()

    def _wheel(self, event):
        if isinstance(event.widget, tk.Text) and event.widget.yview() != (0.0, 1.0):
            return None
        if self.body_canvas.bbox("all") and self.form.winfo_height() > self.body_canvas.winfo_height():
            amount = -1 if event.delta > 0 else 1
            self.body_canvas.yview_scroll(amount * 3, "units")
            return "break"
        return None

    def _show_focused_field(self, event):
        widget = event.widget
        parent = widget
        while parent is not None and parent is not self.form:
            parent = getattr(parent, "master", None)
        if parent is not self.form or not widget.winfo_ismapped():
            return
        top = widget.winfo_rooty() - self.form.winfo_rooty()
        bottom = top + widget.winfo_height()
        visible_top = self.body_canvas.canvasy(0)
        visible_bottom = visible_top + self.body_canvas.winfo_height()
        total = max(1, self.form.winfo_height())
        if top < visible_top:
            self.body_canvas.yview_moveto(max(0, top-self._px(8)) / total)
        elif bottom > visible_bottom:
            self.body_canvas.yview_moveto((bottom-self.body_canvas.winfo_height()+self._px(8)) / total)

    def cancel(self, event=None):
        self.result = None
        self.destroy()
        return "break"

    def pick_exe(self):
        path = filedialog.askopenfilename(parent=self, title="프로그램 실행파일 선택", filetypes=[("Windows 실행파일", "*.exe")])
        if path:
            self.exe.set(path)
            if not self.name.get().strip():
                self.name.set(Path(path).stem)

    def pick_window(self):
        popup = tk.Toplevel(self)
        popup.title("현재 열린 창 선택")
        popup.transient(self)
        popup.grab_set()
        family, px = _program_dialog_metrics(popup, 850, 610)
        colors = _PROGRAM_COLORS
        popup.columnconfigure(0, weight=1)
        popup.rowconfigure(1, weight=1)
        heading = tk.Frame(popup, background=colors["background"], padx=px(24), pady=px(20))
        heading.grid(row=0, column=0, sticky="ew")
        heading.columnconfigure(0, weight=1)
        heading_label = self._label(heading, "열려 있는 프로그램에서 선택", size=18, bold=True)
        heading_label.grid(row=0, column=0, sticky="ew")
        self._wrap_with(heading, heading_label, inset=48)
        heading_help = self._label(heading, "창 제목과 실행파일을 확인한 뒤 사용할 항목을 선택하세요.", color=colors["muted"])
        heading_help.grid(row=1, column=0, sticky="ew", pady=(px(8), 0))
        self._wrap_with(heading, heading_help, inset=48)
        panel = tk.Frame(popup, background=colors["card"], highlightbackground=colors["border"], highlightthickness=1)
        panel.grid(row=1, column=0, sticky="nsew", padx=px(24))
        panel.columnconfigure(0, weight=1)
        panel.rowconfigure(0, weight=1)
        style = ttk.Style(popup)
        style.configure("ProgramEditor.Treeview", font=(family, 10), rowheight=px(34),
                        background=colors["card"], fieldbackground=colors["card"], foreground=colors["ink"], borderwidth=0)
        style.configure("ProgramEditor.Treeview.Heading", font=(family, 9, "bold"))
        style.map("ProgramEditor.Treeview", background=[("selected", "#DDEAFE")], foreground=[("selected", colors["ink"])])
        listing = ttk.Treeview(panel, columns=("title", "program", "path"), show="headings", selectmode="browse",
                               style="ProgramEditor.Treeview", height=7)
        for column, title, size in (("title", "창 제목", 285), ("program", "프로그램", 145), ("path", "실행파일 경로", 380)):
            listing.heading(column, text=title, anchor="w")
            listing.column(column, width=px(size), minwidth=px(90), stretch=column == "title", anchor="w")
        listing.grid(row=0, column=0, sticky="nsew")
        vertical = ttk.Scrollbar(panel, orient="vertical", command=listing.yview)
        vertical.grid(row=0, column=1, sticky="ns")
        horizontal = ttk.Scrollbar(panel, orient="horizontal", command=listing.xview)
        horizontal.grid(row=1, column=0, sticky="ew")
        listing.configure(yscrollcommand=vertical.set, xscrollcommand=horizontal.set)
        popup.empty_text = tk.StringVar(popup)
        status = self._label(popup, "", color=colors["muted"], size=9)
        status.configure(textvariable=popup.empty_text)
        status.grid(row=2, column=0, sticky="ew", padx=px(24), pady=(px(10), px(7)))
        self._wrap_with(popup, status, inset=48)
        selected_path = tk.StringVar(popup, value="선택한 실행파일의 전체 경로가 여기에 표시됩니다.")
        selected_box = tk.Entry(popup, textvariable=selected_path, state="readonly", font=(family, 9),
                                relief="flat", readonlybackground=colors["background"], foreground=colors["muted"])
        selected_box.grid(row=3, column=0, sticky="ew", padx=px(24), pady=(0, px(12)))
        popup.rows_by_id = {}

        def cancel(event=None):
            popup.destroy()
            if self.winfo_exists():
                self.grab_set()
            return "break"

        def select(extra=False):
            selection = listing.selection()
            if not selection or selection[0] not in popup.rows_by_id:
                return
            row = popup.rows_by_id[selection[0]]
            if extra:
                paths = self.extra.get("1.0", "end").strip().splitlines()
                if row["exe"] not in paths:
                    self.extra.insert("end", ("\n" if paths else "") + row["exe"])
                self.advanced_open.set(True)
                self._set_advanced_visibility()
            else:
                self.exe.set(row["exe"])
                if not self.name.get().strip():
                    self.name.set(row["name"])
            cancel()

        bar = tk.Frame(popup, background=colors["card"], padx=px(24), pady=px(16),
                       highlightbackground=colors["border"], highlightthickness=1)
        bar.grid(row=4, column=0, sticky="ew")
        bar.columnconfigure(0, weight=1)
        primary = _program_button(bar, "실행파일로 선택", select, family=family, px=px, primary=True)
        extra_button = _program_button(bar, "추가 조작 파일로 선택", lambda: select(True), family=family, px=px)
        primary.grid(row=1, column=2, sticky="e")
        extra_button.grid(row=1, column=1, sticky="e", padx=(px(8), px(8)))
        _program_button(bar, "취소", cancel, family=family, px=px).grid(row=1, column=0, sticky="w")

        def selection_changed(_event=None):
            selection = listing.selection()
            row = popup.rows_by_id.get(selection[0]) if selection else None
            state = "normal" if row else "disabled"
            primary.configure(state=state)
            extra_button.configure(state=state)
            selected_path.set(row["exe"] if row else "선택한 실행파일의 전체 경로가 여기에 표시됩니다.")
            selected_box.xview_moveto(0)

        def refresh():
            # A refresh clears the selection: an index never silently changes its target.
            for item in listing.get_children():
                listing.delete(item)
            popup.rows_by_id.clear()
            try:
                rows = running_apps()
                for index, row in enumerate(rows):
                    identity = f"window-{index}"
                    popup.rows_by_id[identity] = copy.deepcopy(row)
                    listing.insert("", "end", iid=identity, values=(row["window_title"], row["name"], row["exe"]))
                popup.empty_text.set(f"{len(rows)}개 창을 찾았습니다. 항목을 선택하면 아래 버튼이 활성화됩니다." if rows else
                                     "선택할 창이 없습니다. 사용할 프로그램을 연 뒤 ‘새로고침’을 누르세요.")
            except (OSError, RuntimeError) as error:
                popup.empty_text.set("열린 창을 확인하지 못했습니다. ‘새로고침’을 눌러 다시 확인하세요. " + str(error))
            selection_changed()

        _program_button(heading, "새로고침", refresh, family=family, px=px).grid(row=2, column=0, sticky="w", pady=(px(13), 0))
        listing.bind("<<TreeviewSelect>>", selection_changed)
        popup.bind("<Escape>", cancel)
        popup.protocol("WM_DELETE_WINDOW", cancel)
        primary.bind("<Return>", lambda _: "break")
        primary.bind("<KP_Enter>", lambda _: "break")
        popup.listing, popup.refresh_rows, popup.select_program = listing, refresh, select
        popup.cancel, popup.footer = cancel, bar
        popup.selection_changed = selection_changed
        refresh()
        return popup

    def accept(self):
        item = {"id": self.original.get("id") or "app-" + uuid.uuid4().hex[:12], "name": self.name.get().strip(),
                "exe": self.exe.get().strip().strip('"'), "enabled": self.enabled.get(),
                "control_exes": [p.strip().strip('"') for p in self.extra.get("1.0", "end").splitlines() if p.strip()],
                "hints": self.hints.get("1.0", "end").strip()}
        from settings import validate_config
        trial = copy.deepcopy(self.master.config_value)
        trial["programs"] = [item]
        try:
            uri = self.launch_uri.get().strip()
            argument_text = self.arguments.get("1.0", "end-1c")
            original_arguments = self.original.get("launch", {}).get("arguments", [])
            arguments = (list(original_arguments) if argument_text == "\n".join(original_arguments) else
                         argument_text.splitlines() if argument_text else [])
            cwd = self.working_directory.get().strip().strip('"')
            if uri and (arguments or cwd):
                raise ValueError("실행 주소를 사용할 때는 EXE 인자와 시작 폴더를 비워주세요.")
            if uri:
                item["launch"] = {"kind": "uri", "target": uri}
            elif arguments or cwd or "launch" in self.original:
                item["launch"] = {"kind": "exe", "arguments": arguments}
                if cwd:
                    item["launch"]["cwd"] = cwd
            validate_config(trial)
        except ValueError as error:
            messagebox.showerror("등록 내용 확인", str(error), parent=self)
            return
        self.result = item
        self.destroy()


class Setup(tk.Tk):
    def __init__(self, config_path):
        super().__init__()
        self.withdraw()
        self.config_path = Path(config_path).resolve()
        try:
            self._loaded_config_bytes = _config_source_bytes(self.config_path)
            self.config_value = load_config(self.config_path) if self._loaded_config_bytes is not None else default_config(self.config_path)
            _check_config_snapshot(self.config_path, self._loaded_config_bytes)
        except (ValueError, OSError) as error:
            messagebox.showerror("설정 파일 확인", str(error), parent=self)
            self.destroy()
            raise
        self.title(f"Computer Use MCP {VERSION} 설정")
        self.geometry("1060x820")
        self.minsize(920, 720)
        self.option_add("*Font", ("맑은 고딕", 10))
        self.events = queue.Queue()
        self.busy = False
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.driver = tk.StringVar(value=self.config_value["driver"])
        self.mode = tk.StringVar(value=self.config_value["mode"])
        self.approval = tk.StringVar(value=self.config_value["approval"])
        self.log_detail = tk.StringVar(value=self.config_value.get("log_detail", "metadata"))
        self.last_diagnostics = None
        self.saved_tasks = []
        self.minutes = tk.IntVar(value=self.config_value["max_minutes"])
        self.actions = tk.IntVar(value=self.config_value["max_actions"])
        self.status = tk.StringVar(value="먼저 Driver를 선택하고 ‘연결 확인’을 누르세요. 기본 실행 범위는 그대로 시작해도 됩니다.")
        body = ttk.Frame(self, padding=20)
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=1)
        ttk.Label(body, text="Computer Use MCP", font=("맑은 고딕", 18, "bold")).grid(row=0, column=0, sticky="w")
        ttk.Label(body, text="Claude Code가 이 PC의 프로그램 화면을 조작하도록 연결합니다. 모델은 기존 Claude Code 설정을 사용합니다.", wraplength=900).grid(row=1, column=0, sticky="w", pady=(4, 12))
        tabs = ttk.Notebook(body)
        tabs.grid(row=2, column=0, sticky="nsew")
        body.rowconfigure(2, weight=1)
        connection, programs, options, tasks, records = (ttk.Frame(tabs, padding=18) for _ in range(5))
        tabs.add(connection, text="1 연결 준비")
        ttk.Label(connection, text="0.7.0 배포본은 MCP와 Cua Driver를 관리자 권한으로 연결합니다.\n연결 시작 때 Windows UAC 창이 나오면 같은 로그인 사용자의 권한으로 허용하세요.\n취소되면 연결을 시작하지 않습니다. Claude Code 전체를 관리자 권한으로 실행할 필요는 없습니다.", wraplength=850).grid(row=10, column=0, columnspan=2, sticky="w", pady=(10, 4))
        tabs.add(programs, text="2 사용할 프로그램")
        tabs.add(options, text="3 실행 범위")
        tabs.add(tasks, text="4 저장한 작업")
        tabs.add(records, text="5 기록 관리")
        connection.columnconfigure(0, weight=1)
        ttk.Label(connection, text="직접 받은 Cua Driver 실행파일을 선택하세요. Driver를 자동 다운로드하지 않습니다.", wraplength=850).grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 12))
        ttk.Entry(connection, textvariable=self.driver).grid(row=1, column=0, sticky="ew")
        ttk.Button(connection, text="Driver 파일 선택", command=self.pick_driver).grid(row=1, column=1, padx=(8, 0))
        check_bar = ttk.Frame(connection)
        check_bar.grid(row=2, column=0, columnspan=2, sticky="w", pady=12)
        ttk.Button(check_bar, text="연결 확인 (화면 조작 없음)", command=self.check).pack(side="left")
        ttk.Button(check_bar, text="최근 확인 결과", command=self.show_diagnostics).pack(side="left", padx=8)
        ttk.Label(connection, text="Claude Code에 연결하면 모든 작업 폴더에서 이 MCP를 사용할 수 있습니다.\n회사 앱이나 하네스의 파일은 변경하지 않습니다. 기존 모델·로그인·다른 MCP 설정을 유지합니다.", wraplength=850).grid(row=3, column=0, columnspan=2, sticky="w", pady=(8, 16))
        ttk.Button(connection, text="저장하고 Claude Code에 연결", command=self.register).grid(row=4, column=0, sticky="w", pady=5)
        more_connections = ttk.Frame(connection)
        more_connections.grid(row=5, column=0, columnspan=2, sticky="w", pady=5)
        ttk.Button(more_connections, text="다른 MCP용 연결 파일", command=self.export).pack(side="left")
        ttk.Button(more_connections, text="이전 버전 연결 확인·갱신", command=self.check_upgrade).pack(side="left", padx=8)
        ttk.Button(connection, text="이 MCP의 Claude Code 연결 해제", command=self.unregister).grid(row=6, column=0, sticky="w", pady=5)
        ttk.Label(connection, text="연결한 뒤에는 Claude Code를 다시 열고 /mcp에서 local-computer-use 상태를 확인하세요.\n설정 창은 닫아도 됩니다. 새 설정은 별도의 화면 작업 승인 창 없이 실행하며, 연결한 프로그램의 승인 설정은 유지합니다.", wraplength=850).grid(row=7, column=0, columnspan=2, sticky="w", pady=16)
        ttk.Label(connection, text="첫 요청 예시: ‘메모장의 새 시험 문서에 가짜 내용을 입력하고, 결과를 확인한 뒤 멈춰줘.’\n실제 문서 대신 새 시험 문서에서 시작하세요. 연결 확인 성공은 업무 성공을 뜻하지 않습니다.", wraplength=900).grid(row=8, column=0, columnspan=2, sticky="w", pady=(0, 10))
        ttk.Label(connection, text="설정 저장 위치: " + str(self.config_path), wraplength=900).grid(row=9, column=0, columnspan=2, sticky="w")
        programs.columnconfigure(0, weight=1)
        programs.rowconfigure(1, weight=1)
        ttk.Label(programs, text="기본 브라우저는 Google Chrome입니다. Chrome이 없으면 경로를 지정하세요. Edge로 자동 대체하지 않습니다. 그 외 프로그램은 직접 등록하세요.", wraplength=850).grid(row=0, column=0, sticky="w")
        self.program_list = tk.Listbox(programs, exportselection=False, height=8)
        self.program_list.grid(row=1, column=0, sticky="nsew", pady=12)
        bar = ttk.Frame(programs)
        bar.grid(row=2, column=0, sticky="w")
        for text, command in (("프로그램 추가", self.add_program), ("선택 항목 수정", self.edit_program), ("사용 켜기/끄기", self.toggle_program)):
            ttk.Button(bar, text=text, command=command).pack(side="left", padx=(0, 7))
        ttk.Label(programs, text="목록에 없는 프로그램도 ‘프로그램 추가’에서 실행파일을 고르거나 열린 창을 선택해 등록할 수 있습니다.\nClaude Code에 원하는 프로그램 추가를 직접 요청할 수도 있습니다. 저장 후 MCP를 다시 연결하면 반영됩니다.\nExcel 등 사용할 프로그램은 이 PC에 설치되어 있어야 합니다.", wraplength=850).grid(row=3, column=0, sticky="w", pady=15)
        self.refresh_programs()
        ttk.Label(options, text="화면을 읽는 방식").pack(anchor="w")
        ttk.Radiobutton(options, text="버튼·글자 정보를 읽기 (먼저 권장)", variable=self.mode, value="uia").pack(anchor="w", pady=4)
        ttk.Radiobutton(options, text="화면 이미지로 위치 찾기 (이미지 입력 가능한 LLM 필요)", variable=self.mode, value="visual").pack(anchor="w", pady=4)
        ttk.Label(options, text="확인 방식").pack(anchor="w", pady=(18, 4))
        for label, value in (("별도 승인 창 없이 실행 · 연결한 프로그램의 승인 설정 유지 (새 설정 기본)", "client"),
                             ("작업 시작 때 프로그램·내용을 한 번 확인", "session"),
                             ("시작과 각 조작을 매번 확인", "each")):
            ttk.Radiobutton(options, text=label, variable=self.approval, value=value).pack(anchor="w", pady=3)
        ttk.Label(options, text="첫 번째 선택은 이 MCP가 띄우는 시작·조작 승인 창만 생략합니다. Claude 등 연결한 프로그램의 승인 창은 해당 프로그램의 설정에 따릅니다. 허용 프로그램과 작업 제한은 계속 적용됩니다.", wraplength=900).pack(anchor="w", pady=(8, 0))
        ttk.Label(options, text="기본값은 한 작업에 최대 10분·변경 동작 120회입니다. 필요할 때만 아래 고급 설정을 펼치세요.", wraplength=900).pack(anchor="w", pady=(14, 6))
        self.advanced_open = tk.BooleanVar(value=self.minutes.get() != 10 or self.actions.get() != 120)
        ttk.Checkbutton(options, text="고급 설정 보기", variable=self.advanced_open, command=self.toggle_advanced).pack(anchor="w")
        self.advanced_frame = ttk.Frame(options)
        limits = ttk.Frame(self.advanced_frame)
        limits.pack(anchor="w", pady=12)
        ttk.Label(limits, text="작업 제한 시간(분)").pack(side="left")
        ttk.Spinbox(limits, from_=1, to=30, width=5, textvariable=self.minutes).pack(side="left", padx=8)
        ttk.Label(limits, text="최대 조작 횟수").pack(side="left", padx=(12, 0))
        ttk.Spinbox(limits, from_=1, to=1000, width=6, textvariable=self.actions).pack(side="left", padx=8)
        self.stop_explanation = ttk.Label(options, text="한 PC에서 한 작업씩 실행합니다. Ctrl + Alt + F12로 중지를 요청할 수 있습니다.\n단축키가 다른 프로그램에서 사용 중이면 아래 ‘자동화 중지’ 또는 채팅의 computer_stop을 사용하세요.\n이미 끝난 클릭이나 입력을 되돌리는 기능은 아닙니다.", wraplength=900)
        self.stop_explanation.pack(anchor="w", pady=(16, 0))
        self.toggle_advanced()
        self.build_tasks(tasks)
        self.build_records(records)
        footer = ttk.Frame(body)
        footer.grid(row=3, column=0, sticky="ew", pady=(16, 8))
        ttk.Button(footer, text="설정 저장", command=self.save).pack(side="left", padx=(0, 8))
        ttk.Button(footer, text="사용 안내", command=lambda: os.startfile(str(HERE / "README.html"))).pack(side="left")
        ttk.Button(footer, text="자동화 중지", command=self.stop).pack(side="right")
        ttk.Label(body, textvariable=self.status, wraplength=930, justify="left").grid(row=4, column=0, sticky="ew")
        self.after(150, self.drain)
        self.deiconify()

    def toggle_advanced(self):
        if self.advanced_open.get():
            self.advanced_frame.pack(anchor="w", fill="x", before=self.stop_explanation)
        else:
            self.advanced_frame.pack_forget()

    def build_tasks(self, parent):
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(1, weight=1)
        ttk.Label(parent, text="직접 만든 작업 설명을 저장·수정하고 다음 요청에 다시 사용할 수 있습니다. 저장만으로 실행되지는 않습니다.", wraplength=900).grid(row=0, column=0, sticky="w")
        self.task_list = ttk.Treeview(parent, columns=("name", "id"), show="headings", selectmode="browse")
        self.task_list.heading("name", text="작업 이름")
        self.task_list.heading("id", text="작업 ID")
        self.task_list.column("name", width=480)
        self.task_list.column("id", width=300)
        self.task_list.grid(row=1, column=0, sticky="nsew", pady=12)
        self.task_list.bind("<Double-1>", lambda _: self.edit_task())
        buttons = ttk.Frame(parent)
        buttons.grid(row=2, column=0, sticky="w")
        for label, action in (("목록 새로고침", self.refresh_tasks), ("새 작업", self.new_task), ("읽기·수정", self.edit_task), ("선택 작업 삭제", self.delete_task)):
            ttk.Button(buttons, text=label, command=action).pack(side="left", padx=(0, 8))
        ttk.Label(parent, text="사용 예: ‘저장한 작업 목록을 보여줘. 그중 원하는 작업을 선택해 시작할게.’\n저장 작업에는 입력한 지시와 완료 기준이 평문으로 남습니다. 프로그램 권한은 ‘사용할 프로그램’에서 사람이 별도로 정합니다.", wraplength=900).grid(row=3, column=0, sticky="w", pady=14)
        self.refresh_tasks()

    def build_records(self, parent):
        ttk.Label(parent, text="이 PC에 남길 실행 기록", font=("맑은 고딕", 13, "bold")).pack(anchor="w")
        ttk.Radiobutton(parent, text="기본 정보만 (추천): 시각·도구·성공 여부 등", variable=self.log_detail, value="metadata").pack(anchor="w", pady=(12, 4))
        ttk.Radiobutton(parent, text="진단용 내용 포함: 입력문·화면 정보가 기록될 수 있음", variable=self.log_detail, value="content").pack(anchor="w", pady=4)
        ttk.Label(parent, text="기본 정보 모드에서도 직접 저장한 작업 설명은 남습니다. 연결한 Claude/MCP 클라이언트에는 화면 내용이 전달될 수 있으며, 그 보관 정책은 별도입니다. 이 설정이 모든 프로그램의 기록을 삭제하거나 외부 통신을 차단하지는 않습니다.", wraplength=900).pack(anchor="w", pady=12)
        ttk.Label(parent, text="설정 파일: " + str(self.config_path), wraplength=900).pack(anchor="w", pady=(12, 4))
        ttk.Label(parent, text="기록 폴더: " + str(self.config_value["state_dir"]), wraplength=900).pack(anchor="w", pady=4)
        buttons = ttk.Frame(parent)
        buttons.pack(anchor="w", pady=12)
        ttk.Button(buttons, text="설정 폴더 열기", command=lambda: self.open_folder(self.config_path.parent)).pack(side="left")
        ttk.Button(buttons, text="기록 폴더 열기", command=lambda: self.open_folder(Path(self.config_value["state_dir"]))).pack(side="left", padx=8)
        ttk.Button(buttons, text="정리할 기록 확인", command=self.preview_cleanup).pack(side="left")
        ttk.Label(parent, text="정리 전에 실제 대상과 개수를 보여줍니다. 실행 중인 작업, 정체를 확인할 수 없는 파일, 설정과 저장 작업은 정리 대상에서 제외합니다.", wraplength=900).pack(anchor="w")

    def open_folder(self, path):
        try:
            path = Path(path)
            path.mkdir(parents=True, exist_ok=True)
            os.startfile(str(path))
        except OSError as error:
            messagebox.showerror("폴더 열기", str(error), parent=self)

    def task_store(self):
        from server import TaskStore
        return TaskStore(self.config_value["state_dir"], copy.deepcopy(self.config_value))

    def refresh_tasks(self):
        try:
            self.saved_tasks = self.task_store().all()
            self.task_list.delete(*self.task_list.get_children())
            for item in self.saved_tasks:
                self.task_list.insert("", "end", iid=item["id"], values=(item.get("name", "이름 없음"), item["id"]))
        except Exception as error:
            messagebox.showerror("저장한 작업", str(error), parent=self)

    def new_task(self):
        self.edit_task(new=True)

    def edit_task(self, new=False):
        if self.busy:
            return
        selected = self.task_list.selection()
        if not new and not selected:
            messagebox.showinfo("작업 선택", "읽거나 수정할 작업을 목록에서 선택해주세요.", parent=self)
            return
        try:
            original = None if new else self.task_store().get(selected[0])
            dialog = TaskEditor(self, self.config_value["programs"], original)
            self.wait_window(dialog)
            if dialog.result:
                # Persist human-edited program IDs before a task can reference them.
                if not self.save():
                    self.status.set("프로그램 설정을 저장하지 않아 작업 설명도 저장하지 않았습니다.")
                    return
                entry = self.task_store().save(dialog.result)
                self.refresh_tasks()
                self.status.set(f"‘{entry['name']}’ 작업 설명을 저장했습니다. 실행할 때는 별도로 작업 시작을 요청하세요.")
        except Exception as error:
            messagebox.showerror("작업 저장", str(error), parent=self)

    def delete_task(self):
        selected = self.task_list.selection()
        if self.busy or not selected:
            return
        try:
            task = self.task_store().get(selected[0])
            if messagebox.askyesno("저장 작업 삭제", f"‘{task['name']}’의 저장된 작업 설명을 삭제할까요? 실제 프로그램과 문서는 삭제하지 않습니다.", parent=self):
                self.task_store().delete(selected[0])
                self.refresh_tasks()
                self.status.set("선택한 작업 설명을 삭제했습니다.")
        except Exception as error:
            messagebox.showerror("작업 삭제", str(error), parent=self)

    def refresh_programs(self):
        self.program_list.delete(0, "end")
        for item in self.config_value["programs"]:
            self.program_list.insert("end", ("[사용] " if item["enabled"] else "[꺼짐] ") + item["name"] + "  —  " + (item["exe"] or "실행파일 선택 필요"))

    def pick_driver(self):
        value = filedialog.askopenfilename(parent=self, filetypes=[("Windows 실행파일", "*.exe")], title="직접 설치한 Cua Driver 선택")
        if value:
            self.driver.set(value)

    def add_program(self):
        dialog = ProgramEditor(self)
        self.wait_window(dialog)
        if dialog.result:
            self.config_value["programs"].append(dialog.result)
            self.refresh_programs()
            self.status.set("목록에 추가했습니다. 아래 ‘설정 저장’을 누른 뒤 MCP를 다시 연결하세요.")

    def edit_program(self):
        if self.program_list.curselection():
            index = self.program_list.curselection()[0]
            dialog = ProgramEditor(self, self.config_value["programs"][index])
            self.wait_window(dialog)
            if dialog.result:
                self.config_value["programs"][index] = dialog.result
                self.refresh_programs()

    def toggle_program(self):
        if self.program_list.curselection():
            item = self.config_value["programs"][self.program_list.curselection()[0]]
            item["enabled"] = not item["enabled"]
            self.refresh_programs()

    def current(self):
        value = copy.deepcopy(self.config_value)
        value.update(driver=self.driver.get().strip().strip('"'), mode=self.mode.get(), approval=self.approval.get(),
                     max_minutes=self.minutes.get(), max_actions=self.actions.get(), log_detail=self.log_detail.get())
        return value

    def save(self):
        try:
            _check_config_snapshot(self.config_path, self._loaded_config_bytes)
            value = self.current()
            if value["log_detail"] == "content" and self.config_value.get("log_detail", "metadata") != "content":
                if not messagebox.askyesno("진단용 내용 기록", "앞으로 실행할 작업의 입력문과 화면 정보가 기록에 포함될 수 있습니다.\n가짜 자료를 사용하는 진단용으로 내용 기록을 켤까요?", parent=self):
                    return False
            # A confirmation window may have been open while a chat command saved.
            # This is a stale-editor check, not an atomic lock on other writers.
            _check_config_snapshot(self.config_path, self._loaded_config_bytes)
            save_config(self.config_path, value)
            self._loaded_config_bytes = _config_source_bytes(self.config_path)
            self.config_value = value
            self.status.set("설정을 저장했습니다. 이미 연결된 MCP에는 Claude Code를 다시 연 뒤 적용됩니다.")
            return True
        except (ValueError, tk.TclError, OSError) as error:
            messagebox.showerror("설정 확인", str(error), parent=self)
            return False

    def background(self, action):
        if self.busy:
            return
        self.busy = True
        self.status.set("확인 중입니다. 잠시 기다려주세요.")
        def work():
            try:
                self.events.put((True, action()))
            except Exception as error:
                self.events.put((False, str(error)))
        threading.Thread(target=work, daemon=True).start()

    def check_files(self, config=None):
        from diagnostics import run_diagnostics
        value = copy.deepcopy(config) if config is not None else load_config(self.config_path)
        return {"kind": "diagnostics", "report": run_diagnostics(value)}

    def check(self):
        if self.busy:
            return
        try:
            snapshot = validate_config(self.current())
        except (ValueError, tk.TclError, OSError) as error:
            messagebox.showerror("연결 확인", str(error), parent=self)
            return
        self.background(lambda: self.check_files(snapshot))

    def show_diagnostics(self):
        if self.last_diagnostics is None:
            messagebox.showinfo("연결 확인", "먼저 ‘연결 확인’을 눌러주세요. 화면을 조작하지 않고 파일과 MCP 연결을 확인합니다.", parent=self)
            return
        from diagnostics import format_diagnostics
        TextResult(self, "연결 확인 결과", format_diagnostics(self.last_diagnostics))

    def preview_cleanup(self):
        if self.busy:
            return
        from maintenance import preview_cleanup
        value = copy.deepcopy(self.config_value)
        self.background(lambda: {"kind": "cleanup_preview", "config": value, "preview": preview_cleanup(value)})

    def check_upgrade(self):
        if self.busy:
            return
        from register import registration_status
        self.background(lambda: {"kind": "upgrade_status", "data": registration_status(self.config_path)})

    def register(self):
        if not self.busy and self.save():
            from register import register_claude
            self.background(lambda: register_claude(self.config_path)["message"])

    def unregister(self):
        from register import unregister_claude
        self.background(lambda: unregister_claude(self.config_path)["message"])

    def export(self):
        if not self.busy and self.save():
            from register import export_config
            try:
                path = export_config(self.config_path)
                self.status.set("연결 파일을 만들었습니다: " + str(path))
            except Exception as error:
                messagebox.showerror("연결 파일", str(error), parent=self)

    def stop(self):
        try:
            count = stop_active_runs()
            self.status.set(f"현재 Windows 로그인 세션의 자동화 {count}개에 중지를 요청했습니다. 이미 수행된 입력은 화면에서 확인해주세요.")
        except (OSError, ValueError, RuntimeError) as error:
            self.status.set("중지 요청을 전달하지 못했습니다: " + str(error))

    def drain(self):
        try:
            while True:
                okay, text = self.events.get_nowait()
                self.busy = False
                if okay and isinstance(text, dict):
                    self.handle_result(text)
                else:
                    self.status.set(text)
                if not okay:
                    messagebox.showerror("확인 필요", text, parent=self)
        except queue.Empty:
            pass
        self.after(150, self.drain)

    def handle_result(self, value):
        kind = value.get("kind")
        if kind == "diagnostics":
            self.last_diagnostics = value["report"]
            self.status.set("MCP 연결 확인 완료. 결과를 읽고 ‘저장하고 Claude Code에 연결’을 진행하세요. 실제 업무는 별도 시험이 필요합니다." if self.last_diagnostics.get("ok") else "연결 확인에서 해결할 항목이 나왔습니다. 결과 창의 안내를 따라주세요.")
            self.show_diagnostics()
        elif kind == "cleanup_preview":
            preview = value["preview"]
            count = preview.get("candidate_count", 0)
            kept = preview.get("preserved_count", 0)
            if not count:
                self.status.set(f"정리 가능한 기록이 없습니다. 보존 대상 {kept}개는 유지했습니다.")
                if preview.get("errors"):
                    messagebox.showwarning("기록 확인", "일부 기록을 확인하지 못했습니다. 해당 폴더는 삭제하지 않습니다.\n" + self.cleanup_errors(preview), parent=self)
                return
            locations = self.cleanup_target_summary(preview)
            text = f"다음 기록 항목 {count}개({preview.get('candidate_bytes', 0):,}바이트)를 삭제할까요?\n\n{locations}\n\n보존 대상 {kept}개, 설정 파일과 저장 작업은 유지합니다."
            if messagebox.askyesno("기록 정리 확인", text, parent=self):
                from maintenance import cleanup_completed_runs
                config = value["config"]
                self.background(lambda: {"kind": "cleanup_done", "data": cleanup_completed_runs(config, preview)})
            else:
                self.status.set("기록 정리를 취소했습니다.")
        elif kind == "cleanup_done":
            result = value["data"]
            self.status.set(f"기록 {result.get('deleted_count', 0)}개를 정리했습니다. 보존 대상 {result.get('preserved_count', 0)}개는 유지했습니다.")
            if result.get("errors"):
                messagebox.showwarning("일부 기록 유지", self.cleanup_errors(result), parent=self)
        elif kind == "upgrade_status":
            result = value["data"]
            if result.get("can_upgrade"):
                message = "이 도구의 이전 버전 연결을 찾았습니다. 현재 배포본으로 바꿀까요?\n\n기존 서버: " + str(result.get("previous_server", "")) + "\n현재 서버: " + str(HERE / "server.py") + "\n\n기존 모델·로그인·다른 MCP 설정은 유지합니다."
                if messagebox.askyesno("이전 버전 연결 갱신", message, parent=self) and self.save():
                    from register import upgrade_claude
                    self.background(lambda: upgrade_claude(self.config_path)["message"])
            else:
                self.status.set(result.get("message") or "현재 경로에서 갱신할 이전 버전 연결을 찾지 못했습니다. 처음 연결하는 경우 ‘저장하고 Claude Code에 연결’을 사용하세요.")

    @staticmethod
    def cleanup_errors(value):
        return "\n".join(str(item.get("path", "")) + ": " + str(item.get("reason", "확인 필요")) for item in value.get("errors", [])[:5])

    @staticmethod
    def cleanup_target_summary(preview):
        paths = [str(item["path"]) for item in preview.get("candidates", [])]
        if len(paths) <= 12:
            return "\n".join(paths) or str(preview.get("root", ""))
        counts = Counter(str(Path(path).parent) for path in paths)
        return "위치별 대상 수:\n" + "\n".join(f"{parent} — {count}개" for parent, count in sorted(counts.items()))

    def on_close(self):
        if self.busy:
            messagebox.showinfo("연결 처리 중", "현재 확인 또는 연결 작업이 끝난 뒤 닫아주세요.", parent=self)
            return
        self.destroy()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=default_config_path())
    options = parser.parse_args()
    Setup(options.config).mainloop()


if __name__ == "__main__":
    main()
