"""Desktop launcher for the authenticated media collector."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from playwright.sync_api import sync_playwright


PROJECT_ROOT = Path(__file__).resolve().parent.parent
MEMBERS_FILE = PROJECT_ROOT / "data" / "members.json"
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

        self.title("画像保存ツール")
        self.geometry("680x720")

        self.selected_browser = tk.StringVar(value="Edge")

        self.selected_member_id = tk.IntVar(value=0)

        self.media_photo = tk.BooleanVar(value=True)
        self.media_video = tk.BooleanVar(value=False)
        self.media_audio = tk.BooleanVar(value=False)

        self.output_dir = tk.StringVar(
            value=str(PROJECT_ROOT / "output")
        )

        self.status = tk.StringVar(
            value="ブラウザを起動してください。"
        )

        self.process: subprocess.Popen[str] | None = None
        self.running = False
        self.start_button: ttk.Button | None = None

        self.members = self.load_members()

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

    @staticmethod
    def load_members() -> list[dict[str, object]]:
        with MEMBERS_FILE.open(encoding="utf-8") as file:
            return json.load(file)["members"]

    def build_ui(self) -> None:
        root = ttk.Frame(self, padding=16)
        root.pack(fill="both", expand=True)
        ttk.Label(root, text="画像保存ツール", font=("Segoe UI", 18, "bold")).pack(anchor="w")
        ttk.Label(root, text="選択したブラウザでログイン後、メンバーと種類を選択して取得を開始します。").pack(anchor="w", pady=(4, 12))

        browser_frame = ttk.LabelFrame(root, text="ブラウザ")
        browser_frame.pack(fill="x", pady=(0, 12))

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
        member_frame = ttk.LabelFrame(root, text="メンバー")
        member_frame.pack(fill="both", expand=True)
        canvas = tk.Canvas(member_frame, highlightthickness=0)
        scrollbar = ttk.Scrollbar(member_frame, orient="vertical", command=canvas.yview)
        content = ttk.Frame(canvas)
        content.bind("<Configure>", lambda event: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=content, anchor="nw")
        canvas.configure(yscrollcommand=scrollbar.set)
        canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        generations: dict[int, list[dict[str, object]]] = {}
        for member in self.members:
            generation = int(member["generation"])
            generations.setdefault(generation, []).append(member)
        for generation, members in generations.items():
            ttk.Label(content, text=f"{generation}期生", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(8, 2))
            for member in members:
                talk_id = int(member["talk_id"])

                ttk.Radiobutton(
                    content,
                    text=str(member["name"]),
                    variable=self.selected_member_id,
                    value=talk_id,
                ).pack(anchor="w")

        media_frame = ttk.LabelFrame(root, text="取得する種類")
        media_frame.pack(fill="x", pady=12)
        ttk.Checkbutton(media_frame, text="写真", variable=self.media_photo).pack(side="left", padx=8)
        ttk.Checkbutton(media_frame, text="動画(準備中)", variable=self.media_video, state="disabled").pack(side="left", padx=8)
        ttk.Checkbutton(media_frame, text="音声（準備中）", variable=self.media_audio, state="disabled").pack(side="left", padx=8)

        output_frame = ttk.Frame(root)
        output_frame.pack(fill="x")
        ttk.Label(output_frame, text="保存先").pack(side="left")
        ttk.Entry(output_frame, textvariable=self.output_dir).pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(output_frame, text="参照", command=self.choose_output).pack(side="right")
        self.start_button = ttk.Button(root, text="選択した内容で取得開始", command=self.start)
        self.start_button.pack(fill="x", pady=(12, 4))
        ttk.Label(root, textvariable=self.status, foreground="#555").pack(anchor="w")


    def navigate_browser(self, url: str) -> None:
        """起動中のブラウザを指定したURLへ移動する。"""

        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(
                CDP_URL,
                timeout=15000,
            )

            pages = [
                page
                for context in browser.contexts
                for page in context.pages
            ]

            if not pages:
                raise RuntimeError("ブラウザにタブがありません。")

            page = pages[-1]

            print(f"ブラウザをメディア一覧へ移動します: {url}")

            page.goto(
                url,
                wait_until="domcontentloaded",
            )

            page.wait_for_timeout(1000)

            print(f"現在URL: {page.url}")
            print("タブ数:", len(pages))

            for i, p in enumerate(pages):
                print(i, p.url)

    def choose_output(self) -> None:
        selected = filedialog.askdirectory(initialdir=self.output_dir.get())
        if selected:
            self.output_dir.set(selected)

    def start(self) -> None:
        if self.running:
            messagebox.showinfo("実行中", "すでに取得処理を実行しています。")
            return
        selected_id = self.selected_member_id.get()

        if selected_id == 0:
            messagebox.showwarning(
                "メンバー未選択",
                "メンバーを1人選択してください。",
            )
            return

        selected_ids = [selected_id]
        if not selected_ids:
            messagebox.showwarning("メンバー未選択", "メンバーを1人以上選択してください。")
            return
        if not self.media_video.get() and not self.media_photo.get():
            messagebox.showwarning("取得種類", "写真または動画を選択してください。")
            return
        if self.media_audio.get():
            messagebox.showinfo("取得種類", "音声処理は準備中です。写真・動画のみ実行します。")
        photo_selected = self.media_photo.get()
        video_selected = self.media_video.get()
        output_dir = self.output_dir.get()
                # 最初に選択されたメンバーのメディア一覧へEdgeを移動
        first_talk_id = selected_ids[0]
        first_url = (
            f"https://message.sakurazaka46.com/"
            f"organization/1/talk/timeline/{first_talk_id}/media-list"
        )

        try:
            self.status.set("ブラウザをメディア一覧へ移動しています。")
            self.navigate_browser(first_url)
        except Exception as error:
            messagebox.showerror(
                "ブラウザ遷移エラー",
                str(error),
            )
            return

        self.running = True

        if self.start_button:
            self.start_button.configure(state="disabled")

        self.status.set(
            "メディア一覧へ移動しました。取得処理を開始します。"
        )

        threading.Thread(
            target=self.run_jobs,
            args=(
                selected_ids,
                photo_selected,
                video_selected,
                output_dir,
            ),
            daemon=True,
        ).start()

    def run_jobs(
        self, talk_ids: list[int], photo_selected: bool, video_selected: bool, output_dir_value: str
    ) -> None:
        output_dir = Path(output_dir_value)
        if output_dir.name.lower() in {"photo", "video", "audio"}:
            output_dir = output_dir.parent
        try:
            for talk_id in talk_ids:
                url = f"https://message.sakurazaka46.com/organization/1/talk/timeline/{talk_id}/media-list"
                python_path = Path(__file__).resolve().parent.parent / ".venv/Scripts/python.exe"
                python = str(python_path) if python_path.exists() else "python"
                common = [
                    python,
                    "-m",
                    "app.phase1",
                    "--cdp-url",
                    CDP_URL,
                    "--media-list-url",
                    url,
                    "--output-dir",
                    str(output_dir),
                    "--skip-navigation",
                ]
                # Read the Tk variables once on the UI thread before entering
                # this worker in normal use; keep the existing UI choices.
                if photo_selected:
                    self.process = subprocess.Popen(
                        common + [
                            "--mode", "photo",
                            "--max-items", "999999",
                            "--max-no-change", "3",
                            "--scroll-pause", "1",
                        ],
                        cwd=PROJECT_ROOT,
                        text=True,
                    )
                    return_code = self.process.wait()
                    if return_code != 0:
                        raise RuntimeError(f"写真処理が終了コード {return_code} で終了しました。")
                if video_selected:
                    self.process = subprocess.Popen(
                        common + [
                            "--mode", "video",
                            "--max-items", "999999",
                            "--max-no-change", "3",
                            "--scroll-pause", "1",
                            "--click-interval", "1.0",
                            "--video-wait", "8",
                        ],
                        cwd=PROJECT_ROOT,
                        text=True,
                    )
                    return_code = self.process.wait()
                    if return_code != 0:
                        raise RuntimeError(f"動画処理が終了コード {return_code} で終了しました。")
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