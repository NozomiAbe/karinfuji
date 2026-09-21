"""Phase 1 browser and image-response verification tool."""

from __future__ import annotations

import argparse
import re
import signal
import sys
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import BrowserContext, Page, Playwright, TimeoutError as PlaywrightTimeoutError, sync_playwright


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
    parser.add_argument(
        "--probe-video",
        action="store_true",
        help="Open a media-list page and probe one video tab/item click",
    )
    parser.add_argument(
        "--download-videos",
        action="store_true",
        help="Open the video tab and save clicked video/mp4 responses",
    )
    parser.add_argument(
        "--verify-videos",
        action="store_true",
        help="Click every visible video item without saving responses",
    )
    parser.add_argument(
        "--scroll-videos",
        action="store_true",
        help="Scroll upward and process video items as they become visible",
    )
    parser.add_argument(
        "--save-videos",
        action="store_true",
        help="Save video/mp4 responses while using --scroll-videos",
    )
    parser.add_argument(
        "--max-videos",
        type=int,
        default=1,
        help="Maximum number of video items to click (default: 1)",
    )
    parser.add_argument(
        "--click-interval",
        type=float,
        default=0.5,
        help="Minimum seconds between media clicks (default: 0.5)",
    )
    parser.add_argument(
        "--media-list-url",
        help="Media-list URL used by --probe-video",
    )
    parser.add_argument("--video-tab-x", type=float, default=200, help="Video tab X coordinate")
    parser.add_argument("--video-tab-y", type=float, default=190, help="Video tab Y coordinate")
    parser.add_argument("--video-item-x", type=float, default=60, help="Video item X coordinate")
    parser.add_argument("--video-item-y", type=float, default=240, help="Video item Y coordinate")
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
    output_dir = output_dir / "photo"
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

    def attach_response_listener(target_page: Page) -> None:
        target_page.on("response", on_response)

    for open_page in page.context.pages:
        attach_response_listener(open_page)
    page.context.on("page", attach_response_listener)
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
    try:
        browser = playwright.chromium.connect_over_cdp(cdp_url, timeout=15_000)
    except PlaywrightTimeoutError as error:
        raise RuntimeError(
            "Edge には接続できましたが、ページの初期化がタイムアウトしました。"
            "再生用のタブやDevToolsを閉じ、専用Edgeを再起動してから再実行してください。"
        ) from error
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


def probe_video_click(
    page: Page,
    media_list_url: str,
    video_tab_x: float,
    video_tab_y: float,
    video_item_x: float,
    video_item_y: float,
) -> None:
    responses = []

    def on_response(response) -> None:
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type == "video/mp4" or "mp4" in response.url.lower():
            responses.append((response.status, content_type, response.url))

    def attach_response_listener(target_page: Page) -> None:
        target_page.on("response", on_response)

    context = page.context
    for open_page in context.pages:
        attach_response_listener(open_page)
    context.on("page", attach_response_listener)
    page.goto(media_list_url, wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    print(f"メディア一覧を開きました: {page.url}")
    semantics_placeholder = page.locator("flt-semantics-placeholder")
    if semantics_placeholder.count():
        semantics_placeholder.evaluate("element => element.click()")
        page.wait_for_timeout(500)
    tabs = page.locator('[role="tablist"] [role="button"]')
    if tabs.count() >= 3:
        print("動画タブをクリックします: tablist内の2番目のボタン")
        tabs.nth(1).click()
    else:
        print(f"動画タブをクリックします: ({video_tab_x}, {video_tab_y})")
        page.mouse.click(video_tab_x, video_tab_y)
    page.wait_for_timeout(1000)
    print(f"動画項目をクリックします: ({video_item_x}, {video_item_y})")
    page.mouse.click(video_item_x, video_item_y)
    page.wait_for_timeout(2000)

    if responses:
        for status, content_type, url in responses:
            print(f"動画レスポンスを検出しました: {status} {content_type} {url}")
    else:
        print("video/mp4 のレスポンスは検出できませんでした。座標または対象項目を確認してください。")


def buttons_in_region(page: Page, region: Page | object) -> list[int]:
    try:
        region_box = region.bounding_box(timeout=3000)
    except PlaywrightTimeoutError:
        return []
    if not region_box:
        return []
    buttons = page.locator('[role="button"]')
    indices = []
    for index in range(buttons.count()):
        button_box = buttons.nth(index).bounding_box()
        if not button_box:
            continue
        overlaps = (
            button_box["x"] < region_box["x"] + region_box["width"]
            and button_box["x"] + button_box["width"] > region_box["x"]
            and button_box["y"] < region_box["y"] + region_box["height"]
            and button_box["y"] + button_box["height"] > region_box["y"]
        )
        if overlaps:
            indices.append(index)
    return indices


def activate_video_tab(page: Page) -> list[int]:
    for attempt in range(2):
        tablist = page.locator('[role="tablist"]').first
        tab_indices = buttons_in_region(page, tablist)
        if len(tab_indices) >= 3:
            page.locator('[role="button"]').nth(tab_indices[1]).click(timeout=5000)
            page.wait_for_timeout(1000)
            return buttons_in_region(page, page.locator('[role="tabpanel"]').first)
        if attempt == 0:
            placeholder = page.locator("flt-semantics-placeholder")
            if placeholder.count():
                placeholder.evaluate("element => element.click()")
            page.wait_for_timeout(1000)
    raise RuntimeError("写真・動画・音声のタブをSemanticsから取得できませんでした。ページを再読み込みして再実行してください。")


def video_item_targets(page: Page) -> list[tuple[str, int]]:
    positioned_targets: list[tuple[float, float, str, int, float, float]] = []
    tablist_box = page.locator('[role="tablist"]').first.bounding_box()
    tab_bottom = tablist_box["y"] + tablist_box["height"] if tablist_box else 100

    buttons = page.locator('[role="button"]')
    for index in range(buttons.count()):
        button_box = buttons.nth(index).bounding_box()
        if (
            button_box
            and button_box["y"] > tab_bottom + 5
            and button_box["width"] >= 40
            and button_box["height"] >= 40
        ):
            positioned_targets.append(
                (button_box["y"], button_box["x"], "button", index, button_box["width"], button_box["height"])
            )

    images = page.locator('[role="img"]')
    tab_bottom = tablist_box["y"] + tablist_box["height"] if tablist_box else 100
    for index in range(images.count()):
        button_box = images.nth(index).bounding_box()
        if (
            button_box
            and button_box["y"] > tab_bottom + 5
            and button_box["width"] >= 40
            and button_box["height"] >= 40
        ):
            positioned_targets.append(
                (button_box["y"], button_box["x"], "img", index, button_box["width"], button_box["height"])
            )

    positioned_targets.sort(key=lambda item: (round(item[0] / 10), item[1]))
    targets: list[tuple[str, int]] = []
    seen_boxes: list[tuple[float, float, float, float]] = []
    for y, x, role, index, width, height in positioned_targets:
        box = (x, y, width, height)
        if any(
            abs(x - old_x) < 2
            and abs(y - old_y) < 2
            and abs(width - old_width) < 2
            and abs(height - old_height) < 2
            for old_x, old_y, old_width, old_height in seen_boxes
        ):
            continue
        seen_boxes.append(box)
        targets.append((role, index))
    return targets


def click_video_target(page: Page, target: tuple[str, int]) -> None:
    role, index = target
    page.locator(f'[role="{role}"]').nth(index).click(timeout=5000)


def combine_video_responses(responses: list[tuple[str, bytes, str]]) -> tuple[str, bytes, bool]:
    response_url, first_body, _ = responses[0]
    ranges = []
    for url, body, content_range in responses:
        match = re.match(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", content_range or "")
        if match:
            total = None if match.group(3) == "*" else int(match.group(3))
            ranges.append((int(match.group(1)), int(match.group(2)), total, body))
    if not ranges:
        return response_url, first_body, first_body.startswith(b"ftyp")
    total_size = max((total for _, _, total, _ in ranges if total is not None), default=None)
    if total_size is None:
        ranges.sort(key=lambda item: item[0])
        combined = b"".join(body for _, _, _, body in ranges)
        return response_url, combined, combined.startswith(b"ftyp")
    combined = bytearray(total_size)
    received = bytearray(total_size)
    for start, end, _, body in ranges:
        chunk = body[: min(end, total_size - 1) - start + 1]
        combined[start : start + len(chunk)] = chunk
        received[start : start + len(chunk)] = b"\1" * len(chunk)
    complete = all(received) and bytes(combined).startswith(b"ftyp")
    if not complete:
        print("動画のRangeレスポンスまたはMP4先頭が不足しています。保存をスキップします。", file=sys.stderr)
    return response_url, bytes(combined), complete


def wait_for_video_end(page: Page, timeout_ms: int = 120_000) -> None:
    video = page.locator("video").last
    if not video.count():
        page.wait_for_timeout(1500)
        return
    try:
        video.evaluate("element => element.play()")
        page.wait_for_function(
            "element => element.ended",
            arg=video.element_handle(timeout=5000),
            timeout=timeout_ms,
        )
    except Exception:
        page.wait_for_timeout(1500)


def fetch_video_url(page: Page, video_url: str, output_dir: Path, sequence: int) -> Path:
    response = page.context.request.get(video_url, timeout=120_000, fail_on_status_code=True)
    body = response.body()
    print(
        f"動画URLを直接取得しました: status={response.status}, "
        f"content-type={response.headers.get('content-type', '')}, bytes={len(body)}"
    )
    ftyp_position = body.find(b"ftyp", 0, 32)
    if ftyp_position < 0:
        raise RuntimeError("直接取得したレスポンスが完全なMP4ではありません。")
    output_dir.mkdir(parents=True, exist_ok=True)
    filename = Path(urlparse(video_url).path).stem or f"video-{sequence}"
    output_path = output_dir / f"{sequence:03d}-{filename}.mp4"
    if output_path.exists():
        output_path = output_dir / f"{output_path.stem}-{sequence}.mp4"
    output_path.write_bytes(body)
    print(f"動画データを直接取得しました: {len(body)} bytes")
    return output_path


def download_videos(
    page: Page,
    media_list_url: str,
    output_dir: Path,
    max_videos: int,
    click_interval: float,
) -> None:
    if max_videos < 1:
        raise ValueError("--max-videos は1以上で指定してください。")
    if click_interval < 0.5:
        raise ValueError("--click-interval は0.5秒以上で指定してください。")

    responses: list[tuple[str, bytes, str]] = []

    def on_response(response) -> None:
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type == "video/mp4":
            try:
                body = response.body()
            except Exception as error:
                print(f"動画レスポンス本文を取得できませんでした: {response.url} ({error})", file=sys.stderr)
                return
            responses.append((response.url, body, response.headers.get("content-range", "")))

    def attach_response_listener(target_page: Page) -> None:
        target_page.on("response", on_response)

    for open_page in page.context.pages:
        attach_response_listener(open_page)
    page.context.on("page", attach_response_listener)
    page.goto(media_list_url, wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    print(f"メディア一覧を開きました: {page.url}")
    semantics_placeholder = page.locator("flt-semantics-placeholder")
    if semantics_placeholder.count():
        semantics_placeholder.evaluate("element => element.click()")
        page.wait_for_timeout(500)
    activate_video_tab(page)
    item_targets = video_item_targets(page)
    print("動画タブを開きました。")

    item_count = min(len(item_targets), max_videos)
    if item_count == 0:
        raise RuntimeError("動画タブ内にクリック可能な動画項目がありません。")
    print(f"動画項目を検出しました: {len(item_targets)}件。今回の処理: {item_count}件")

    output_dir = output_dir / "video"
    output_dir.mkdir(parents=True, exist_ok=True)
    saved_count = 0
    for index in range(item_count):
        if index > 0:
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
            activate_video_tab(page)
            item_targets = video_item_targets(page)
        if index >= len(item_targets):
            print(f"動画 {index + 1} は現在の画面から取得できないため終了します。")
            break

        responses_before_click = len(responses)
        print(f"動画 {index + 1}/{item_count} をクリックします。")
        try:
            click_video_target(page, item_targets[index])
        except PlaywrightTimeoutError:
            print(f"動画 {index + 1} のクリック対象が消えました。プレイヤーを閉じて再取得します。")
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
            activate_video_tab(page)
            item_targets = video_item_targets(page)
            if index >= len(item_targets):
                break
            click_video_target(page, item_targets[index])
        wait_for_video_end(page)

        response_url, response_body, complete = combine_video_responses(responses[responses_before_click:]) if responses[responses_before_click:] else ("", b"", False)
        if response_url and complete:
            try:
                filename = Path(urlparse(response_url).path).stem or f"video-{index + 1}"
                output_path = output_dir / f"{filename}.mp4"
                if output_path.exists():
                    output_path = output_dir / f"{output_path.stem}-{saved_count + 1}.mp4"
                output_path.write_bytes(response_body)
                saved_count += 1
                print(f"動画を保存しました ({saved_count}): {output_path}（Rangeレスポンスを{len(responses) - responses_before_click}件取得）")
            except Exception as error:
                print(f"動画の保存に失敗しました: {response_url} ({error})", file=sys.stderr)

            page.keyboard.press("Escape")
        if index + 1 < item_count:
            page.wait_for_timeout(round(click_interval * 1000))

    if saved_count == 0:
        print("video/mp4 のレスポンスは検出できませんでした。動画項目のクリック結果を確認してください。")
    else:
        print(f"動画処理を終了しました。保存件数: {saved_count}")


def verify_videos(page: Page, media_list_url: str, click_interval: float) -> None:
    if click_interval < 0.5:
        raise ValueError("--click-interval は0.5秒以上で指定してください。")

    page.goto(media_list_url, wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    print(f"メディア一覧を開きました: {page.url}")
    semantics_placeholder = page.locator("flt-semantics-placeholder")
    if semantics_placeholder.count():
        semantics_placeholder.evaluate("element => element.click()")
        page.wait_for_timeout(500)

    activate_video_tab(page)
    item_targets = video_item_targets(page)
    total = len(item_targets)
    if total == 0:
        button_count = page.locator('[role="button"]').count()
        raise RuntimeError(
            "動画タブ内にクリック可能な動画項目がありません。"
            f" Semanticsのbutton数: {button_count}。"
            "動画カードが画面内に表示される位置までスクロールされているか確認してください。"
        )
    print(f"動画項目を検出しました: {total}件。保存せず全件を確認します。")

    verified_count = 0
    for index in range(total):
        if index > 0:
            page.keyboard.press("Escape")
            page.wait_for_timeout(500)
            activate_video_tab(page)
            item_targets = video_item_targets(page)
        if index >= len(item_targets):
            print(f"動画 {index + 1} は再取得できないため終了します。")
            break
        print(f"動画 {index + 1}/{total} をクリックします。")
        try:
            click_video_target(page, item_targets[index])
            page.wait_for_timeout(1500)
            verified_count += 1
            print(f"動画 {index + 1}/{total} のクリックを確認しました。")
        except PlaywrightTimeoutError:
            print(f"動画 {index + 1}/{total} はクリックできませんでした。", file=sys.stderr)
        if index + 1 < total:
            page.keyboard.press("Escape")
            page.wait_for_timeout(round(click_interval * 1000))

    print(f"動画クリック確認を終了しました。成功: {verified_count}/{total}")


def process_visible_videos(
    page: Page,
    click_interval: float,
    save_responses: bool,
    processed_ids: set[str],
    output_dir: Path | None = None,
    max_count: int | None = None,
) -> int:
    video_urls: list[str] = []
    seen_urls: set[str] = set()

    def on_response(response) -> None:
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type == "video/mp4" and response.url not in seen_urls:
            seen_urls.add(response.url)
            video_urls.append(response.url)
            print(f"動画URLを検出しました: {response.url}")

    def attach_response_listener(target_page: Page) -> None:
        target_page.on("response", on_response)

    for open_page in page.context.pages:
        attach_response_listener(open_page)
    page.context.on("page", attach_response_listener)
    processed_count = 0
    initial_target_count = len(video_item_targets(page))
    target_count = min(initial_target_count, max_count) if max_count else initial_target_count
    while processed_count < target_count:
        targets = video_item_targets(page)
        target = next(
            (
                target
                for target in targets
                if (
                    target_key(page, target) not in processed_ids
                    and target_key(page, target) is not None
                )
            ),
            None,
        )
        if target is None:
            break
        role, index = target
        locator = page.locator(f'[role="{role}"]').nth(index)
        target_id = target_key(page, target)
        if target_id is None:
            continue
        if target_id in processed_ids:
            continue
        processed_ids.add(target_id)
        url_count = len(video_urls)
        print(f"動画をクリックします: {target_id}")
        try:
            locator.click(timeout=5000)
            response_waited = 0
            while save_responses and len(video_urls) == url_count and response_waited < 10_000:
                page.wait_for_timeout(250)
                response_waited += 250
            if save_responses and output_dir:
                if len(video_urls) > url_count:
                    try:
                        print("検出した動画URLの直接取得を開始します。")
                        output_path = fetch_video_url(page, video_urls[url_count], output_dir, processed_count + 1)
                        print(f"動画を保存しました: {output_path}（URL直接取得）")
                    except Exception as error:
                        print(f"動画の直接取得に失敗しました: {error}", file=sys.stderr)
                else:
                    print("video/mp4レスポンスを10秒待ちましたが検出できませんでした。")
            processed_count += 1
            print(f"動画の再生を確認しました: {processed_count}件")
        except PlaywrightTimeoutError:
            print(f"動画をクリックできませんでした: {target_id}", file=sys.stderr)
        finally:
            page.keyboard.press("Escape")
            page.wait_for_timeout(round(click_interval * 1000))
    return processed_count


def target_key(page: Page, target: tuple[str, int]) -> str | None:
    role, index = target
    locator = page.locator(f'[role="{role}"]').nth(index)
    box = locator.bounding_box()
    if not box:
        return None
    element_id = locator.get_attribute("id")
    if element_id:
        return element_id
    return f"{role}:{round(box['x'])}:{round(box['y'])}:{round(box['width'])}:{round(box['height'])}"


def scroll_video_list(
    page: Page,
    media_list_url: str,
    click_interval: float,
    save: bool,
    output_dir: Path,
    max_videos: int,
) -> None:
    if max_videos < 1:
        raise ValueError("--max-videos は1以上で指定してください。")
    page.goto(media_list_url, wait_until="domcontentloaded")
    page.wait_for_timeout(1500)
    placeholder = page.locator("flt-semantics-placeholder")
    if placeholder.count():
        placeholder.evaluate("element => element.click()")
        page.wait_for_timeout(500)
    activate_video_tab(page)
    processed_ids: set[str] = set()
    total = 0
    unchanged_rounds = 0
    for _ in range(100):
        if total >= max_videos:
            break
        before_count = len(processed_ids)
        total += process_visible_videos(
            page,
            click_interval,
            save,
            processed_ids,
            output_dir,
            max_videos - total,
        )
        if len(processed_ids) == before_count:
            unchanged_rounds += 1
        else:
            unchanged_rounds = 0
        if unchanged_rounds >= 3:
            break
        page.mouse.wheel(0, -500)
        page.wait_for_timeout(1000)
    print(f"動画処理を終了しました。確認件数: {total}")


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
    output_dir = output_dir / "photo"
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

    for media_type in ("photo", "video", "audio"):
        (args.output_dir / media_type).mkdir(parents=True, exist_ok=True)

    if args.probe_video:
        if not args.cdp_url or not args.media_list_url:
            raise ValueError("--probe-video には --cdp-url と --media-list-url が必要です。")
        with sync_playwright() as playwright:
            _, existing_page = connect_to_existing_page(
                playwright, args.cdp_url, args.page_url_contains
            )
            page = existing_page.context.new_page()
            probe_video_click(
                page,
                args.media_list_url,
                args.video_tab_x,
                args.video_tab_y,
                args.video_item_x,
                args.video_item_y,
            )
        return 0

    if args.download_videos:
        if not args.cdp_url or not args.media_list_url:
            raise ValueError("--download-videos には --cdp-url と --media-list-url が必要です。")
        with sync_playwright() as playwright:
            _, existing_page = connect_to_existing_page(
                playwright, args.cdp_url, args.page_url_contains
            )
            page = existing_page.context.new_page()
            download_videos(
                page,
                args.media_list_url,
                args.output_dir,
                args.max_videos,
                args.click_interval,
            )
        return 0

    if args.verify_videos:
        if not args.cdp_url or not args.media_list_url:
            raise ValueError("--verify-videos には --cdp-url と --media-list-url が必要です。")
        with sync_playwright() as playwright:
            _, existing_page = connect_to_existing_page(
                playwright, args.cdp_url, args.page_url_contains
            )
            page = existing_page.context.new_page()
            verify_videos(page, args.media_list_url, args.click_interval)
        return 0

    if args.scroll_videos:
        if not args.cdp_url or not args.media_list_url:
            raise ValueError("--scroll-videos には --cdp-url と --media-list-url が必要です。")
        with sync_playwright() as playwright:
            _, existing_page = connect_to_existing_page(
                playwright, args.cdp_url, args.page_url_contains
            )
            page = existing_page.context.new_page()
            scroll_video_list(
                page,
                args.media_list_url,
                args.click_interval,
                save=args.save_videos,
                output_dir=args.output_dir / "video",
                max_videos=args.max_videos,
            )
        return 0

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
