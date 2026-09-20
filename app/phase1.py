"""Phase 1 browser and image-response verification tool."""

from __future__ import annotations

import argparse
import signal
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import BrowserContext, Page, Playwright, sync_playwright


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Capture image responses from an authenticated browser page."
    )
    parser.add_argument("--url", help="Target image-list page URL")
    parser.add_argument(
        "--cdp-url",
        help="Connect to an already open Chromium started with remote debugging",
    )
    parser.add_argument(
        "--page-url-contains",
        help="Only monitor the browser tab whose URL contains this text",
    )
    parser.add_argument(
        "--image-selector",
        help="CSS selector for the image or clickable image element",
    )
    parser.add_argument(
        "--output-dir", type=Path, default=Path("output"), help="Directory for the saved image"
    )
    parser.add_argument(
        "--auth-state",
        type=Path,
        default=Path(".data/auth.json"),
        help="Playwright storage-state JSON file",
    )
    parser.add_argument(
        "--url-contains",
        default="",
        help="Only save image responses whose URL contains this text",
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="Open a browser for manual login and save the resulting session",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="Save every matching image response until Ctrl+C",
    )
    return parser


def launch_context(playwright: Playwright, auth_state: Path | None) -> BrowserContext:
    browser = playwright.chromium.launch(headless=False)
    if auth_state and auth_state.exists():
        return browser.new_context(storage_state=str(auth_state))
    return browser.new_context()


def save_login_state(page: Page, auth_state: Path) -> None:
    input("ブラウザでログインが完了したら、この画面で Enter を押してください: ")
    auth_state.parent.mkdir(parents=True, exist_ok=True)
    page.context.storage_state(path=str(auth_state))
    print(f"ログイン状態を保存しました: {auth_state}")


def response_matches(response_url: str, content_type: str, url_filter: str) -> bool:
    media_type = content_type.split(";", 1)[0].strip().lower()
    return media_type == "image/jpeg" and url_filter in response_url


def capture_responses(page: Page, output_dir: Path, url_filter: str) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    saved_urls: set[str] = set()
    saved_count = 0
    pending_responses = []
    stop_requested = False

    def request_stop(_signal_number, _frame) -> None:
        nonlocal stop_requested
        stop_requested = True

    def on_response(response) -> None:
        content_type = response.headers.get("content-type", "")
        if response.url in saved_urls or not response_matches(response.url, content_type, url_filter):
            return
        pending_responses.append(response)

    def save_response(response) -> None:
        nonlocal saved_count
        try:
            content_type = response.headers.get("content-type", "")
            body = response.body()
            extension = ".jpg"
            filename = Path(urlparse(response.url).path).stem or f"captured-{saved_count + 1}"
            filename += extension
            output_path = output_dir / filename
            if output_path.exists():
                output_path = output_dir / f"{output_path.stem}-{saved_count + 1}{output_path.suffix}"
            output_path.write_bytes(body)
            saved_urls.add(response.url)
            saved_count += 1
            print(f"保存しました ({saved_count}): {output_path}")
        except Exception as error:
            print(f"画像の保存に失敗しました: {response.url} ({error})", file=sys.stderr)

    page.on("response", on_response)
    print(f"画像通信を監視中です。対象タブ: {page.url}")
    print("終了するには Ctrl+C を押してください。")
    previous_sigint_handler = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, request_stop)
    try:
        while not stop_requested:
            page.wait_for_timeout(250)
            while pending_responses:
                response = pending_responses.pop(0)
                if response.url not in saved_urls:
                    save_response(response)
        print(f"監視を終了しました。保存件数: {saved_count}")
    finally:
        signal.signal(signal.SIGINT, previous_sigint_handler)


def connect_to_existing_page(
    playwright: Playwright, cdp_url: str, page_url_contains: str | None
) -> tuple[object, Page]:
    browser = playwright.chromium.connect_over_cdp(cdp_url)
    pages = [page for context in browser.contexts for page in context.pages]
    if not pages:
        raise RuntimeError("接続先ブラウザに開いているページがありません。")
    if page_url_contains:
        matching_pages = [page for page in pages if page_url_contains in page.url]
        if not matching_pages:
            available_urls = "\n".join(f"- {page.url}" for page in pages)
            raise RuntimeError(
                f"URL に一致する監視対象タブがありません: {page_url_contains}\n"
                f"接続中のタブ:\n{available_urls}"
            )
        return browser, matching_pages[-1]
    return browser, pages[-1]


def capture_image(page: Page, selector: str, output_dir: Path, url_filter: str) -> Path:
    image_response = None

    def on_response(response) -> None:
        nonlocal image_response
        content_type = response.headers.get("content-type", "")
        if response_matches(response.url, content_type, url_filter):
            image_response = response
            print(f"画像レスポンスを検出: {response.status} {response.url}")

    page.on("response", on_response)
    locator = page.locator(selector).first
    locator.wait_for(state="visible")
    print(f"クリック対象: {selector}")
    locator.click()
    page.wait_for_timeout(1500)

    if image_response is None:
        raise RuntimeError(
            "クリック後に画像レスポンスを検出できませんでした。"
            " --image-selector または --url-contains を確認してください。"
        )

    body = image_response.body()
    content_type = image_response.headers.get("content-type", "image/jpeg")
    extension = {
        "image/jpeg": ".jpg",
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
    }.get(content_type.split(";", 1)[0], ".bin")
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = Path(urlparse(image_response.url).path).name or f"captured{extension}"
    if "." not in filename:
        filename += extension
    output_path = output_dir / filename
    output_path.write_bytes(body)
    return output_path


def run(args: argparse.Namespace) -> int:
    if not args.url and not args.cdp_url:
        raise ValueError("--url または --cdp-url のどちらかが必要です。")
    if args.cdp_url and args.login:
        raise ValueError("--cdp-url と --login は同時に指定できません。")

    if args.cdp_url:
        with sync_playwright() as playwright:
            _, page = connect_to_existing_page(playwright, args.cdp_url, args.page_url_contains)
            capture_responses(page, args.output_dir, args.url_contains)
        return 0

    if not args.login and not args.image_selector:
        raise ValueError("通常取得には --image-selector が必要です。")

    if args.login and args.auth_state.exists():
        print(f"既存のログイン状態を上書きします: {args.auth_state}")

    with sync_playwright() as playwright:
        context = launch_context(playwright, None if args.login else args.auth_state)
        page = context.new_page()
        page.goto(args.url, wait_until="domcontentloaded")

        if args.login:
            save_login_state(page, args.auth_state)
            context.close()
            return 0

        try:
            output_path = capture_image(
                page, args.image_selector, args.output_dir, args.url_contains
            )
        finally:
            context.close()

    print(f"画像を保存しました: {output_path}")
    return 0


def main() -> None:
    try:
        raise SystemExit(run(build_parser().parse_args()))
    except KeyboardInterrupt:
        print("処理を中断しました。", file=sys.stderr)
        raise SystemExit(130)


if __name__ == "__main__":
    main()
