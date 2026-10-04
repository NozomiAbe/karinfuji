"""Desktop launcher for the authenticated media collector."""

from __future__ import annotations

import os
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SITE_URL = "https://message.sakurazaka46.com"
CDP_URL = "http://127.0.0.1:9222"


def find_browser(browser_name: str) -> Path:
    browsers = {
        "Edge": [
            Path(os.environ.get("ProgramFiles(x86)", ""))
            / "Microsoft/Edge/Application/msedge.exe",
            Path(os.environ.get("ProgramFiles", ""))
            / "Microsoft/Edge/Application/msedge.exe",
        ],
        "Chrome": [
            Path(os.environ.get("ProgramFiles", ""))
            / "Google/Chrome/Application/chrome.exe",

            Path(os.environ.get("ProgramFiles(x86)", ""))
            / "Google/Chrome/Application/chrome.exe",

            Path(os.environ.get("LOCALAPPDATA", ""))
            / "Google/Chrome/Application/chrome.exe",
        ],
    }

    for candidate in browsers.get(browser_name, []):
        if candidate.exists():
            return candidate

    raise FileNotFoundError(f"{browser_name} が見つかりません。")


class Launcher(tk.Tk):
    def __init__(self) -> None:
        super().__init__()

        self.title("メディア保存ツール")
        self.geometry("620x430")
        self.resizable(False, False)

        self.selected_browser = tk.StringVar(value="Edge")

        self.media_type = tk.StringVar(value="photo")

        self.output_dir = tk.StringVar(
            value=str(PROJECT_ROOT / "output")
        )

        self.status = tk.StringVar(
            value="ブラウザを起動してください。"
        )

        self.process: subprocess.Popen[str] | None = None
        self.running = False
        self.start_button: ttk.Button | None = None

        self.build_ui()

    def launch_browser(self) -> None:
        browser_name = self.selected_browser.get()

        try:
            browser_path = find_browser(browser_name)

            profile = (
                PROJECT_ROOT
                / ".data"
                / f"{browser_name.lower()}-profile"
            )

            profile.mkdir(
                parents=True,
                exist_ok=True,
            )

            subprocess.Popen(
                [
                    str(browser_path),
                    "--remote-debugging-port=9222",
                    f"--user-data-dir={profile}",
                    SITE_URL,
                ]
            )

            self.status.set(
                f"{browser_name}を起動しました。ログインしてください。"
            )

        except Exception as error:
            messagebox.showerror(
                f"{browser_name}を起動できません",
                str(error),
            )

    def build_ui(self) -> None:
        root = ttk.Frame(self, padding=10)
        root.pack(fill="both", expand=True)
        ttk.Label(
            root,
            text="保存ツール",
            font=("Segoe UI", 16, "bold"),
        ).pack(anchor="w", pady=(0, 6))

        usage_frame = ttk.LabelFrame(root, text="使い方", padding=(8, 4))
        usage_frame.pack(fill="x", pady=(0, 8))
        for instruction in (
            "1. ブラウザを起動する",
            "2. サイトにログインする",
            "3. 保存したいメンバーの「メディア一覧」を開く",
            "4. 一覧で対象タブを開く（一番上までスクロールするとエラー起きにくい）",
            "5.「取得する種類」を選択して開始する"
        ):
            ttk.Label(usage_frame, text=instruction).pack(anchor="w", pady=1)

        browser_frame = ttk.LabelFrame(root, text="ブラウザ")
        browser_frame.pack(fill="x", pady=(0, 8))
        ttk.Combobox(
            browser_frame,
            textvariable=self.selected_browser,
            values=["Edge", "Chrome"],
            state="readonly",
            width=15,
        ).pack(side="left", padx=8, pady=8)

        ttk.Button(
            browser_frame,
            text="ブラウザ起動",
            command=self.launch_browser,
        ).pack(side="left", padx=8)

        media_frame = ttk.LabelFrame(root, text="取得する種類")
        media_frame.pack(fill="x", pady=(0, 8))
        ttk.Radiobutton(
            media_frame,
            text="写真",
            variable=self.media_type,
            value="photo"
        ).pack(side="left", padx=8)

        ttk.Radiobutton(
            media_frame,
            text="動画",
            variable=self.media_type,
            value="video"
        ).pack(side="left", padx=8)

        ttk.Radiobutton(
            media_frame,
            text="音声",
            variable=self.media_type,
            value="audio"
        ).pack(side="left", padx=8)
        
        output_frame = ttk.Frame(root)
        output_frame.pack(fill="x")
        ttk.Label(output_frame, text="保存先").pack(side="left")
        ttk.Entry(output_frame, textvariable=self.output_dir).pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(output_frame, text="参照", command=self.choose_output).pack(side="right")
        self.start_button = ttk.Button(root, text="選択した種類の取得を開始", command=self.start)
        self.start_button.pack(fill="x", pady=(8, 4))
        ttk.Label(root, textvariable=self.status, foreground="#555").pack(anchor="w")


    def choose_output(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.output_dir.get())
        if selected:
            self.output_dir.set(selected)

    def start(self) -> None:
        if self.running:
            messagebox.showinfo(
                "実行中",
                "すでに取得処理を実行しています。"
            )
            return

        selected_mode = self.media_type.get()
        output_dir = self.output_dir.get()

        self.running = True

        if self.start_button:
            self.start_button.configure(state="disabled")

        self.status.set(
            "現在のブラウザ画面で取得を開始します。"
        )

        threading.Thread(
            target=self.run_jobs,
            args=(selected_mode, output_dir),
            daemon=True,
        ).start()

    def run_jobs(
        self, mode: str, output_dir_value: str
    ) -> None:
        output_dir = Path(output_dir_value)
        try:
            python_path = Path(__file__).resolve().parent.parent / ".venv/Scripts/python.exe"
            python = str(python_path) if python_path.exists() else "python"
            command = [
                python,
                "-m",
                "app.phase1",
                "--cdp-url",
                CDP_URL,
                "--mode",
                mode,
                "--output-dir",
                str(output_dir),
                "--max-items",
                "999999",
                "--max-no-change",
                "5",
                "--scroll-pause",
                "1",
                "--photo-wait",
                "1",
                "--video-wait",
                "2",
                "--audio-wait",
                "2",
            ]
            self.process = subprocess.Popen(
                command,
                cwd=PROJECT_ROOT,
                text=True,
            )
            return_code = self.process.wait()
            if return_code != 0:
                raise RuntimeError(f"取得処理が終了コード {return_code} で終了しました。")
            self.after(0, lambda: self.status.set("取得処理が終了しました。"))
        except Exception as error:
            self.after(0, lambda: messagebox.showerror("取得処理エラー", str(error)))
            self.after(0, lambda: self.status.set("取得処理がエラーで終了しました。"))
        finally:
            self.running = False
            self.after(0, self._enable_start)

    def _enable_start(self) -> None:
        if self.start_button:
            self.start_button.configure(state="normal")



def main() -> None:
    Launcher().mainloop()


if __name__ == "__main__":
    main()