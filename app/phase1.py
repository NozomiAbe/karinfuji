from __future__ import annotations
import argparse, json, re, time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from playwright.sync_api import Page, Playwright, Response, TimeoutError as PlaywrightTimeoutError, sync_playwright

SIGNED_KEYS={"expires","signature","key-pair-id","key_pair_id"}

def normalize_url(url:str)->str:
    p=urlsplit(url)
    q=[(k,v) for k,v in parse_qsl(p.query,keep_blank_values=True) if k.lower() not in SIGNED_KEYS]
    return urlunsplit((p.scheme.lower(),p.netloc.lower(),p.path,urlencode(q,doseq=True),""))

def mime(r:Response)->str:return (r.headers.get("content-type") or "").split(";",1)[0].strip().lower()
def filename(url:str,fallback:str)->str:
    n=Path(urlsplit(url).path).name or fallback
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]','_',n)

def is_jpeg_url(url:str)->bool:
    return re.search(r'\.jpe?g$',urlsplit(url).path,re.I) is not None

def is_full_photo_url(url:str)->bool:
    path=urlsplit(url).path.lower()
    return '/files/' in path and is_jpeg_url(url)

def is_thumbnail_url(url:str)->bool:
    path=urlsplit(url).path.lower()
    return re.search(r'/thumbnails?/',path) is not None and is_jpeg_url(url)

def photo_output_path(root:Path,url:str,fallback:str)->Path:
    name=filename(url,fallback)
    match=re.fullmatch(r'\d+-(\d{8})-\d+\.jpe?g',name,re.I)
    if match:
        try:
            date=datetime.strptime(match[1],'%Y%m%d')
            return root/'photo'/f'{date.year}年'/f'{date.month}月'/name
        except ValueError:
            pass
    return root/'photo'/name

def audio_suffix(content_type:str)->str:
    subtype=content_type.partition('/')[2].split(';',1)[0].lower()
    return {
        'mpeg':'.mp3',
        'mp3':'.mp3',
        'mp4':'.m4a',
        'x-m4a':'.m4a',
        'aac':'.aac',
        'wav':'.wav',
        'x-wav':'.wav',
        'ogg':'.ogg',
        'webm':'.webm',
        'flac':'.flac',
        '3gpp':'.3gp',
        '3gpp2':'.3g2',
    }.get(subtype,f'.{subtype}' if re.fullmatch(r'[a-z0-9.+-]+',subtype) else '.audio')

def audio_output_path(root:Path,url:str,content_type:str,fallback:str)->Path:
    name=filename(url,fallback)
    suffix=audio_suffix(content_type)
    if Path(name).suffix.lower()!=suffix:
        name=f'{Path(name).stem}{suffix}'
    return root/'audio'/name

def video_filename(url:str,fallback:str)->str:
    name=filename(url,fallback)
    return name if Path(name).suffix.lower()=='.mp4' else f'{Path(name).stem}.mp4'

def unique(p:Path)->Path:
    if not p.exists(): return p
    for i in range(2,100000):
        q=p.with_name(f"{p.stem}_{i}{p.suffix}")
        if not q.exists(): return q
    raise RuntimeError("保存先を作れません")

def parse_range(v:str):
    m=re.fullmatch(r"bytes\s+(\d+)-(\d+)/(\d+|\*)",v.strip(),re.I)
    if not m:return None
    return int(m[1]),int(m[2]),None if m[3]=="*" else int(m[3])

@dataclass
class State:
    root:Path

    photos:set[str]=field(default_factory=set)
    thumbs:set[str]=field(default_factory=set)
    audio_keys:set[str]=field(default_factory=set)
    thumbnail_urls:set[str]=field(default_factory=set)
    download_mode:bool=False
    saved_photos:int=0
    saved_videos:int=0
    saved_audio:int=0
    video_full:list[tuple[str,bytes]]=field(default_factory=list)
    video_parts:list[tuple[int,int,int|None,bytes]]=field(default_factory=list)
    audio_full:list[tuple[str,str,bytes]]=field(default_factory=list)
    audio_parts:list[tuple[int,int,int|None,str,str,bytes]]=field(default_factory=list)
    photo_active:bool=False
    audio_active:bool=False
    def load(self):
        self.root.mkdir(parents=True,exist_ok=True)
        p=self.root/'.processed.json'
        if p.exists():
            try:
                d=json.loads(p.read_text(encoding='utf-8'))
                self.photos.update(d.get('photos',[]))
                self.thumbs.update(d.get('thumbnails',[]))
                self.audio_keys.update(d.get('audio',[]))
            except Exception: pass
    def save(self):
        (self.root/'.processed.json').write_text(json.dumps({
            'photos':sorted(self.photos),
            'thumbnails':sorted(self.thumbs),
            'audio':sorted(self.audio_keys),
        },ensure_ascii=False,indent=2),encoding='utf-8')

class Capture:
    def __init__(self,page:Page,state:State):
        self.page,self.s=page,state
        self.current_thumb_keys=set()
    def start(self):self.page.on('response',self.on_response)
    def stop(self):
        try:self.page.remove_listener('response',self.on_response)
        except Exception:pass
    def on_response(self,r:Response):

        try:

            print(
                mime(r),
                r.url
            )

            if mime(r) == 'image/jpeg':
                if is_thumbnail_url(r.url):
                    self.s.thumbnail_urls.add(normalize_url(r.url))
                self.jpeg(r)

            elif (
                self.s.audio_active
                and mime(r) in {'audio/mp4', 'video/mp4'}
            ):
                self.audio(r)

        except Exception as e:
            print('response error:',e)
    def jpeg(self,r:Response):
        if not is_full_photo_url(r.url) or not self.s.download_mode:
            return

        key=normalize_url(r.url)

        photo_path=photo_output_path(
            self.s.root,
            r.url,
            f'photo_{self.s.saved_photos+1:06}.jpg'
        )

        already=photo_path.is_file()

        if already:
            self.s.photos.add(key)
            self.s.save()
            print("[SKIP]", photo_path.name, "(already exists)")
            return

        b=r.body()

        p=unique(photo_path)

        p.parent.mkdir(parents=True,exist_ok=True)

        p.write_bytes(b)

        self.s.photos.add(key)

        self.s.saved_photos+=1

        self.s.save()

        print("[SAVE]", p.name)
    def mp4(self,r:Response):
        b=r.body(); cr=parse_range(r.headers.get('content-range',''))
        if cr:self.s.video_parts.append((*cr,b))
        else:self.s.video_full.append((r.url,b))
    def audio(self,r:Response):
        content_type=mime(r)
        body=r.body()
        content_range=parse_range(r.headers.get('content-range',''))
        if content_range:
            self.s.audio_parts.append((*content_range,r.url,content_type,body))
        else:
            self.s.audio_full.append((r.url,content_type,body))

def connect(p:Playwright,cdp:str):
    try:b=p.chromium.connect_over_cdp(cdp,timeout=15000)
    except PlaywrightTimeoutError as e:raise RuntimeError('Edgeへ接続できません') from e
    pages=[x for c in b.contexts for x in c.pages]
    if not pages:raise RuntimeError('Edgeにタブがありません')
    return b,pages[-1]

def photo_grid_positions(page:Page,max_clicks:int=43):
    """Scan four thumbnail columns bottom-up inside the centered mobile view."""
    size=page.viewport_size
    if not size:
        size=page.evaluate('({width:innerWidth,height:innerHeight})')
    width,height=size['width'],size['height']

    panel_width=min(430,width)
    panel_left=(width-panel_width)/2
    tile=panel_width*0.21
    x_centers=[panel_left+panel_width*(0.145+0.235*column) for column in range(4)]

    top_y=height/8+tile/2
    bottom_y=height-tile/2
    row_step=max(35,tile*0.55)
    rows=[]
    y=bottom_y
    while y>=top_y and len(rows)<(max_clicks+3)//4:
        rows.append(y)
        y-=row_step

    return [(x,y) for y in rows for x in x_centers][:max_clicks]

def audio_row_positions(page:Page,max_rows:int=7):
    size=page.viewport_size
    if not size:
        size=page.evaluate('({width:innerWidth,height:innerHeight})')
    width,height=size['width'],size['height']
    center_x=width/2
    first_row=height*0.185
    row_step=height*0.09
    return [(center_x,first_row+row_step*row) for row in range(max_rows)]

def wait_after_media_response(response_received_at:float)->None:
    """Wait until 0.5 seconds have passed since a successful media response."""
    remaining=0.5-(time.monotonic()-response_received_at)
    if remaining>0:
        time.sleep(remaining)

def wait_for_photo_grid(page:Page,c:Capture,initial_thumbnails:int,minimum_wait:float)->int:
    """Wait for new thumbnail responses to settle after scrolling."""
    deadline=time.monotonic()+max(minimum_wait,1.0)
    hard_deadline=deadline+max(minimum_wait,2.0)
    stable_since=time.monotonic()
    last_thumbnails=initial_thumbnails

    while time.monotonic()<deadline or (
        time.monotonic()<hard_deadline and time.monotonic()-stable_since<0.8
    ):
        page.wait_for_timeout(250)
        thumbnails=len(c.s.thumbnail_urls)
        if thumbnails!=last_thumbnails:
            last_thumbnails=thumbnails
            stable_since=time.monotonic()

    return last_thumbnails

def scroll_media_list(page:Page,pause:float)->None:
    view=page.locator('flutter-view')
    try:
        box=view.bounding_box(timeout=1000)
    except Exception:
        box=None
    if not box:
        size=page.viewport_size or page.evaluate('({width:innerWidth,height:innerHeight})')
        box={'x':0,'y':0,'width':size['width'],'height':size['height']}

    page.mouse.move(box['x']+box['width']/2,box['y']+box['height']/2)
    page.mouse.wheel(0,-max(300,int(box['height']*0.7*1.15*1.1)))
    page.wait_for_timeout(int(max(1.0,pause)*1000))

def click(page,s,i):
    try:page.locator(s).nth(i).scroll_into_view_if_needed(timeout=3000);page.locator(s).nth(i).click(timeout=5000);return True
    except Exception as e:print('click failed:',e);return False

def find_scroll_container(page):
    """メディア一覧として使われている可能性が高いスクロールコンテナを探す。
    scrollTop / scrollHeight / clientHeight と画面上の位置を返す。
    """
    try:
        return page.evaluate("""() => {
            const els = [...document.querySelectorAll('*')];

            const candidates = els
                .map((el, i) => {
                    const r = el.getBoundingClientRect();
                    const s = getComputedStyle(el);

                    return {
                        i,
                        tag: el.tagName,
                        cls: typeof el.className === 'string' ? el.className : '',
                        id: el.id || '',
                        scrollTop: el.scrollTop,
                        scrollHeight: el.scrollHeight,
                        clientHeight: el.clientHeight,
                        overflowY: s.overflowY,
                        top: r.top,
                        bottom: r.bottom,
                        height: r.height,
                        width: r.width,
                        x: r.left + r.width / 2,
                        y: r.top + r.height / 2
                    };
                })
                .filter(x =>
                    x.width > 200 &&
                    x.height > 100 &&
                    x.scrollHeight > x.clientHeight + 20 &&
                    x.overflowY !== 'visible' &&
                    x.bottom > 0 &&
                    x.top < window.innerHeight
                )
                .sort((a, b) => {
                    // 画面内に大きく表示されているものを優先。
                    const aVisible =
                        Math.max(0, Math.min(a.bottom, window.innerHeight) - Math.max(a.top, 0));
                    const bVisible =
                        Math.max(0, Math.min(b.bottom, window.innerHeight) - Math.max(b.top, 0));

                    if (bVisible !== aVisible) {
                        return bVisible - aVisible;
                    }

                    // 次に実際にスクロールできる量が大きいものを優先。
                    return (b.scrollHeight - b.clientHeight) -
                           (a.scrollHeight - a.clientHeight);
                });

            return candidates;
        }""")
    except Exception:
        return []


def scroll_up(page: Page):
    """メディア一覧のスクロールコンテナを上方向へスクロールする。"""

    containers = find_scroll_container(page)

    if containers:
        info = containers[0]

        print(
            f"スクロール対象: "
            f"{info['tag']} "
            f"class={info['cls'][:80]} "
            f"scrollTop={info['scrollTop']} "
            f"scrollHeight={info['scrollHeight']} "
            f"clientHeight={info['clientHeight']}"
        )

        before = info["scrollTop"]

        try:
            page.evaluate(
                """info => {
                    const els = [...document.querySelectorAll('*')];
                    const el = els[info.i];

                    if (el) {
                        el.scrollTop = Math.max(
                            0,
                            el.scrollTop - 1200
                        );
                    }
                }""",
                info,
            )
        except Exception as e:
            print("スクロール失敗:", e)

        page.wait_for_timeout(1000)

        containers_after = find_scroll_container(page)

        if containers_after:
            after = containers_after[0]["scrollTop"]
        else:
            after = before

        print(
            f"写真一覧を上へスクロール: "
            f"{before} -> {after}"
        )

        return before, after

    # スクロールコンテナが見つからなかった場合
    print("スクロール可能なコンテナが見つかりません。")

    before = page.evaluate("window.scrollY")

    try:
        page.mouse.move(600, 400)
        page.mouse.wheel(0, -1200)
        page.wait_for_timeout(1000)
    except Exception as e:
        print("マウスホイール失敗:", e)

    after = page.evaluate("window.scrollY")

    print(
        f"ページ全体をスクロール: "
        f"{before} -> {after}"
    )

    return before, after

def media_signature(page):
    """現在表示されている画像/動画要素のURL群を取得して、
    スクロールによる新規読み込みを判定する。"""
    try:
        return page.evaluate("""() => [...document.querySelectorAll('img, video, source')]
            .map(x => x.currentSrc || x.src || '')
            .filter(Boolean).map(x => x.split('?')[0]).sort().join('|')""")
    except Exception:
        return ''

def save_video(s:State):
    d=s.root/'video';d.mkdir(parents=True,exist_ok=True)
    if s.video_full:
        u,b=s.video_full[-1];p=unique(d/video_filename(u,f'video_{s.saved_videos+1:06}.mp4'));p.write_bytes(b);s.saved_videos+=1;return True
    parts=sorted(s.video_parts)
    pos=0;total=None;chunks=[]
    for st,en,to,b in parts:
        if st!=pos:return False
        chunks.append(b);pos=en+1;total=to or total
    if total is not None and pos!=total:return False
    if not chunks:return False
    p=unique(d/f'video_{s.saved_videos+1:06}.mp4');p.write_bytes(b''.join(chunks));s.saved_videos+=1;return True

def save_audio(s:State)->bool:
    if s.audio_full:
        url,content_type,body=s.audio_full[-1]
    else:
        parts=sorted(s.audio_parts)
        position=0
        total=None
        chunks=[]
        for start,end,part_total,_,_,body in parts:
            if start!=position:
                return False
            chunks.append(body)
            position=end+1
            total=part_total or total
        if not chunks or (total is not None and position!=total):
            return False
        _,_,_,url,content_type,_=parts[0]
        body=b''.join(chunks)

    key=normalize_url(url)
    if key in s.audio_keys:
        print('[AUDIO] 保存済み:',filename(url,'audio'))
        return True

    path=audio_output_path(
        s.root,
        url,
        content_type,
        f'audio_{s.saved_audio+1:06}{audio_suffix(content_type)}',
    )
    path.parent.mkdir(parents=True,exist_ok=True)
    destination=unique(path)
    destination.write_bytes(body)
    s.audio_keys.add(key)
    s.saved_audio+=1
    s.save()
    print('[AUDIO] 保存しました:',destination.name)
    return True


def wait_for_current_media(page, c, seconds=2.0):
    """現在表示されている一覧の遅延画像が要求され終わるまで待つ。"""
    deadline = page.evaluate('Date.now()') + int(seconds * 1000)
    last = c.s.saved_photos

    while page.evaluate('Date.now()') < deadline:
        page.wait_for_timeout(250)

        if c.s.saved_photos != last:
            last = c.s.saved_photos
            deadline = page.evaluate('Date.now()') + int(seconds * 1000)


def wait_for_media_list(page, expected_url, timeout=30.0):
    """Wait until the selected member's media-list URL is open."""

    print('メディア一覧への遷移を待っています...')

    deadline = page.evaluate('Date.now()') + int(timeout * 1000)
    expected=urlsplit(expected_url)
    expected_path=expected.path.rstrip('/')

    while page.evaluate('Date.now()') < deadline:
        try:
            current=urlsplit(page.url)
            if (current.scheme.lower()==expected.scheme.lower()
                    and current.netloc.lower()==expected.netloc.lower()
                    and current.path.rstrip('/')==expected_path):
                print(f'選択メンバーのメディア一覧へ遷移しました: {page.url}')
                return True
        except Exception:
            pass

        page.wait_for_timeout(300)

    print(f'{timeout}秒以内に選択メンバーのメディア一覧へ遷移しませんでした。')
    print(f'期待URL: {expected_url}')
    print(f'現在のURL: {page.url}')

    return False


def grid_mode(page,args,c,media_type:str):
    photo_mode=media_type=='photo'
    c.s.download_mode=photo_mode
    c.s.photo_active=photo_mode
    c.s.root.joinpath(media_type).mkdir(parents=True,exist_ok=True)
    print(f'現在の画面で{media_type}の取得を開始します。')

    if photo_mode:
        wait_for_current_media(page,c,2.0)
    no_new_scrolls=0

    def processed_count()->int:
        return c.s.saved_photos if photo_mode else c.s.saved_videos

    while processed_count()<args.max_items:
        positions=photo_grid_positions(page)
        opened_nearby=[]
        click_count=0
        stop_video_scan=False
        print(f'この画面のクリック範囲: 上端{100/8:.1f}%を除外、最大{len(positions)}地点')
        for x,y in positions:
            if processed_count()>=args.max_items:
                break
            if any(abs(opened_x-x)<45 and abs(opened_y-y)<85
                   for opened_x,opened_y in opened_nearby):
                continue
            click_count+=1
            before_saved=processed_count()
            response_received_at=None
            video_saved=False
            if not photo_mode:
                c.s.video_full.clear()
                c.s.video_parts.clear()
            try:
                with page.expect_response(
                    lambda response:(
                        is_full_photo_url(response.url) and mime(response)=='image/jpeg'
                        if photo_mode else mime(response)=='video/mp4'
                    ),
                    timeout=int((args.photo_wait if photo_mode else args.video_wait)*1000),
                ) as response_info:
                    page.mouse.click(x,y)
                response=response_info.value
                response_received_at=time.monotonic()
                if photo_mode:
                    opened_nearby.append((x,y))
                    result_count=c.s.saved_photos
                    result_type='PHOTO'
                else:
                    c.mp4(response)
                    saved=save_video(c.s)
                    video_saved=saved
                    result_count=c.s.saved_videos
                    result_type='VIDEO'
                    if not saved:
                        print('[VIDEO] MP4全体の受信が完了していません。')
                        stop_video_scan=True
                    else:
                        opened_nearby.append((x,y))
                if result_count>before_saved:
                    print(f'[{result_type}] 保存しました: {filename(response.url,"media")}')
                else:
                    print(f'[{result_type}] 保存済みまたは未保存: {filename(response.url,"media")}')
            except PlaywrightTimeoutError:
                expected='JPEG' if photo_mode else 'video/mp4'
                print(f'[{media_type.upper()}] 座標クリック ({x:.0f}, {y:.0f}) 後に {expected} 応答がありません')
            except Exception as e:
                print(f'[{media_type.upper()}] 座標クリック失敗 ({x:.0f}, {y:.0f}):',e)
                if not photo_mode and response_received_at is not None:
                    stop_video_scan=True
            finally:
                if photo_mode or video_saved:
                    try:
                        page.keyboard.press('Escape')
                        print(f'[{media_type.upper()}] Escapeを送信しました')
                    except Exception as e:
                        print(f'[{media_type.upper()}] Escape送信に失敗しました:',e)
                    if response_received_at is not None:
                        wait_after_media_response(response_received_at)
            if stop_video_scan:
                break

        print(f'この画面のクリック完了: {click_count}/{len(positions)}地点')
        if stop_video_scan:
            print('[VIDEO] 完全なMP4を保存できなかったため、次のクリックやEscapeは行わず処理を止めます。')
            break
        if processed_count()>=args.max_items:
            break
        thumbnail_count_before=len(c.s.thumbnail_urls)
        scroll_media_list(page,args.scroll_pause)
        scroll_wait=min(8.0,max(args.scroll_pause,1.0+no_new_scrolls*1.5))
        print(f'サムネイル読込待ち: {scroll_wait:.1f}秒')
        thumbnail_count_after=wait_for_photo_grid(
            page,c,thumbnail_count_before,scroll_wait
        )

        if thumbnail_count_after==thumbnail_count_before:
            no_new_scrolls+=1
            print(f'スクロール後に新規サムネイル通信なし: {no_new_scrolls}/{args.max_no_change}回')
        else:
            no_new_scrolls=0
            print(f'新規サムネイル通信を検出。受信済みURL: {thumbnail_count_after}件')

        if no_new_scrolls>=args.max_no_change:
            print(f'上方向へ{args.max_no_change}回スクロールしても新規サムネイル通信がないため終了します。')
            break

    print(f'{media_type}処理終了:',processed_count())

def audio_mode(page,args,c):
    c.s.audio_active=True
    c.s.root.joinpath('audio').mkdir(parents=True,exist_ok=True)
    print('現在の画面で音声の取得を開始します。各行の中央を1回ずつクリックします。')
    no_new_scrolls=0
    while c.s.saved_audio<args.max_items:
        before_saved=c.s.saved_audio
        for x,y in audio_row_positions(page):
            if c.s.saved_audio>=args.max_items:
                break
            c.s.audio_full.clear()
            c.s.audio_parts.clear()
            response_received_at=None
            try:
                with page.expect_response(
                    lambda response:mime(response) in {'audio/mp4', 'video/mp4'},
                    timeout=int(args.audio_wait*1000),
                ) as response_info:
                    page.mouse.click(x,y)
                response=response_info.value
                response_received_at=time.monotonic()
                page.wait_for_timeout(200)
                if save_audio(c.s):
                    print('[AUDIO] 応答:',filename(response.url,'audio'))
            except PlaywrightTimeoutError:
                print(f'[AUDIO] 行クリック ({x:.0f}, {y:.0f}) 後に音声応答がありません')
            except Exception as e:
                print(f'[AUDIO] 行クリック失敗 ({x:.0f}, {y:.0f}):',e)
            finally:
                if response_received_at is not None:
                    wait_after_media_response(response_received_at)

        if c.s.saved_audio==before_saved:
            no_new_scrolls+=1
            print(f'新しい音声応答なし: {no_new_scrolls}/{args.max_no_change}回')
        else:
            no_new_scrolls=0

        scroll_media_list(page,args.scroll_pause)
        if no_new_scrolls>=args.max_no_change:
            print(f'{args.max_no_change}回スクロールしても新しい音声応答がないため終了します。')
            break

    print('音声処理終了:',c.s.saved_audio)

def photo_mode(page,args,c):
    grid_mode(page,args,c,'photo')

def video_mode(page,args,c):
    grid_mode(page,args,c,'video')

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--cdp-url',required=True)
    ap.add_argument('--mode',choices=['photo','video','audio'],required=True)
    ap.add_argument('--output-dir',default='output')
    ap.add_argument('--max-items',type=int,default=100000)
    ap.add_argument('--scroll-pause',type=float,default=1)
    ap.add_argument('--max-no-change',type=int,default=4)
    ap.add_argument('--video-wait',type=float,default=2)
    ap.add_argument('--photo-wait',type=float,default=1.0)
    ap.add_argument('--audio-wait',type=float,default=2)
    args=ap.parse_args()

    s=State(Path(args.output_dir))
    s.load()

    print("=" * 80)
    print("processed file =", s.root / '.processed.json')
    print("photos count =", len(s.photos))
    print("thumbs count =", len(s.thumbs))

    print("=" * 80)

    with sync_playwright() as pw:
        _,page=connect(pw,args.cdp_url)
        c=Capture(page,s)
        c.start()

        try:
            print(f'現在のブラウザ画面を使用します: {page.url}')
            if not urlsplit(page.url).path.rstrip('/').endswith('/media-list'):
                raise RuntimeError(
                    'メディア一覧画面が開かれていません。ブラウザで一覧を開いてから再実行してください。'
                )
            if args.mode=='photo':
                photo_mode(page,args,c)
            elif args.mode=='video':
                video_mode(page,args,c)
            else:
                audio_mode(page,args,c)

        finally:
            c.stop()

    print(f'完了 写真={s.saved_photos} 動画={s.saved_videos} 音声={s.saved_audio}')
if __name__=='__main__':main()
