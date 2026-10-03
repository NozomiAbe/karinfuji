from __future__ import annotations
import argparse, json, re, signal, time
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
    thumbnail_urls:set[str]=field(default_factory=set)
    download_mode:bool=False
    saved_photos:int=0
    saved_videos:int=0
    video_full:list[tuple[str,bytes]]=field(default_factory=list)
    video_parts:list[tuple[int,int,int|None,bytes]]=field(default_factory=list)
    photo_active:bool=False
    video_active:bool=False
    def load(self):
        self.root.mkdir(parents=True,exist_ok=True)
        p=self.root/'.processed.json'
        if p.exists():
            try:
                d=json.loads(p.read_text(encoding='utf-8'))
                self.photos.update(d.get('photos',[]))
                self.thumbs.update(d.get('thumbnails',[]))
            except Exception: pass
    def save(self):
        (self.root/'.processed.json').write_text(json.dumps({
            'photos':sorted(self.photos),
            'thumbnails':sorted(self.thumbs),
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

            elif mime(r) == 'video/mp4' and self.s.video_active:
                self.mp4(r)

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

def connect(p:Playwright,cdp:str):
    try:b=p.chromium.connect_over_cdp(cdp,timeout=15000)
    except PlaywrightTimeoutError as e:raise RuntimeError('Edgeへ接続できません') from e
    pages=[x for c in b.contexts for x in c.pages]
    if not pages:raise RuntimeError('Edgeにタブがありません')
    return b,pages[-1]

def video_tab(page:Page,index:int):
    try:
        x=page.locator('[role="tablist"] [role="button"]')
        if x.count()>index:x.nth(index).click(timeout=5000);page.wait_for_timeout(800);return
    except Exception:pass
    x=page.get_by_text(re.compile('動画'))
    if x.count():x.last.click(timeout=5000);page.wait_for_timeout(800);return
    raise RuntimeError('動画タブを見つけられません')

def targets(page:Page):
    out=[]
    for sel in ('[role="button"]','[role="img"]','img','video'):
        loc=page.locator(sel)
        for i in range(loc.count()):
            try:b=loc.nth(i).bounding_box()
            except Exception:b=None
            if not b or b['width']<40 or b['height']<40 or b['y']<80:continue
            src=loc.nth(i).get_attribute('src') or loc.nth(i).get_attribute('href') or ''
            key=normalize_url(src) if src else f'{sel}:{round(b["x"])}:{round(b["y"])}:{round(b["width"])}:{round(b["height"])}'
            out.append((b['y'],b['x'],sel,i,key))
    out.sort(key=lambda x:(round(x[0]/10),x[1])); seen=set(); ans=[]
    for y,x,s,i,k in out:
        g=(round(x),round(y))
        if g not in seen:seen.add(g);ans.append((s,i,k))
    return ans

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

def wait_after_photo_response(response_received_at:float)->None:
    """Wait until 0.5 seconds have passed since a successful photo response."""
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
    d=s.root/'videos';d.mkdir(parents=True,exist_ok=True)
    if s.video_full:
        u,b=s.video_full[-1];p=unique(d/filename(u,f'video_{s.saved_videos+1:06}.mp4'));p.write_bytes(b);s.saved_videos+=1;return True
    parts=sorted(s.video_parts)
    pos=0;total=None;chunks=[]
    for st,en,to,b in parts:
        if st!=pos:return False
        chunks.append(b);pos=en+1;total=to or total
    if total is not None and pos!=total:return False
    if not chunks:return False
    p=unique(d/f'video_{s.saved_videos+1:06}.mp4');p.write_bytes(b''.join(chunks));s.saved_videos+=1;return True


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


def photo_mode(page, args, c):

    c.s.download_mode = True

    print('写真一覧が開くのを待っています...')

    # 一覧が実際に表示されるまで待つ
    if not wait_for_media_list(page, args.media_list_url, 15.0):
        print('写真一覧を確認できないため終了します。')
        return

    print('写真一覧が開きました。画面上の4列グリッドを下段からクリックします。')

    wait_for_current_media(page, c, 2.0)
    no_new_scrolls=0

    while c.s.saved_photos < args.max_items:
        positions=photo_grid_positions(page)
        opened_nearby=[]
        click_count=0
        print(f'この画面のクリック範囲: 上端{100/8:.1f}%を除外、最大{len(positions)}地点')
        for x,y in positions:
            if c.s.saved_photos>=args.max_items:
                break
            if any(abs(opened_x-x)<45 and abs(opened_y-y)<85
                   for opened_x,opened_y in opened_nearby):
                continue
            click_count+=1
            before_saved=c.s.saved_photos
            response_received_at=None
            try:
                with page.expect_response(
                    lambda response:is_full_photo_url(response.url) and mime(response)=='image/jpeg',
                    timeout=int(args.photo_wait*1000),
                ) as response_info:
                    page.mouse.click(x,y)
                response=response_info.value
                response_received_at=time.monotonic()
                opened_nearby.append((x,y))
                if c.s.saved_photos>before_saved:
                    print(f'[PHOTO] 保存しました: {filename(response.url,"photo.jpg")}')
                else:
                    print(f'[PHOTO] 保存済み: {filename(response.url,"photo.jpg")}')
            except PlaywrightTimeoutError:
                print(f'[PHOTO] 座標クリック ({x:.0f}, {y:.0f}) 後に /files/ のJPEG応答がありません')
            except Exception as e:
                print(f'[PHOTO] 座標クリック失敗 ({x:.0f}, {y:.0f}):',e)
            finally:
                if response_received_at is not None:
                    page.wait_for_timeout(200)
                try:
                    page.keyboard.press('Escape')
                    print('[PHOTO] Escapeを送信しました')
                except Exception as e:
                    print('[PHOTO] Escape送信に失敗しました:',e)
                if response_received_at is not None:
                    wait_after_photo_response(response_received_at)

        print(f'この画面のクリック完了: {click_count}/{len(positions)}地点')
        thumbnail_count_before=len(c.s.thumbnail_urls)
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

    print('写真処理終了:',c.s.saved_photos)

def video_mode(page,args,c):
    video_tab(page,args.video_tab_index)
    c.s.video_active=True

    print('動画一覧が開くのを待っています...')

    # 動画タブを開いた後、実際に一覧が表示されるまで待つ
    if not wait_for_media_list(page, args.media_list_url, 15.0):
        print('動画一覧を確認できないため終了します。')
        return

    print('動画一覧が開きました。ここから処理を開始します。')

    opened=set()
    no_new_scrolls=0
    last_sig=media_signature(page)

    # 現在見えている動画を先に処理する。
    while len(opened) < args.max_items:
        ts=targets(page)
        new=[x for x in ts if x[2] not in opened and x[2] not in c.s.thumbs]
        if not new:
            break
        for sel,i,key in new:
            opened.add(key)
            c.s.thumbs.add(key)
            c.s.save()
            c.s.video_full.clear(); c.s.video_parts.clear()
            if click(page,sel,i):
                page.wait_for_timeout(int(args.video_wait*1000))
                if save_video(c.s):
                    print('[VIDEO] saved',c.s.saved_videos)
                else:
                    print('[VIDEO] complete MP4 not captured')
                try: page.keyboard.press('Escape')
                except Exception: pass
                page.wait_for_timeout(int(args.click_interval*1000))

    # 現在分を処理し終えたら上へスクロールし、新しいJPEGサムネイルが要求されたら続ける。
    while len(opened) < args.max_items:
        before_count=len(opened)
        before, after=scroll_up(page)
        print(f'動画一覧を上へスクロール: {before} -> {after}')
        page.wait_for_timeout(int(max(1.0,args.scroll_pause)*1000))

        ts=targets(page)
        new=[x for x in ts if x[2] not in opened and x[2] not in c.s.thumbs]
        new_sig=media_signature(page)

        if new:
            no_new_scrolls=0
            print(f'新規動画サムネイル: {len(new)}件')
            for sel,i,key in new:
                opened.add(key)
                c.s.thumbs.add(key)
                c.s.save()
                c.s.video_full.clear(); c.s.video_parts.clear()
                if click(page,sel,i):
                    page.wait_for_timeout(int(args.video_wait*1000))
                    if save_video(c.s): print('[VIDEO] saved',c.s.saved_videos)
                    else: print('[VIDEO] complete MP4 not captured')
                    try: page.keyboard.press('Escape')
                    except Exception: pass
                    page.wait_for_timeout(int(args.click_interval*1000))
        else:
            no_new_scrolls += 1
            print(f'新規動画サムネイルなし: {no_new_scrolls}/{args.max_no_change}回')

        if before==after and new_sig==last_sig:
            no_new_scrolls=max(no_new_scrolls,1)
        last_sig=new_sig
        if no_new_scrolls>=args.max_no_change:
            print(f'上方向へ{args.max_no_change}回スクロールしても新規動画がないため終了します。')
            break

    print('動画終了:',c.s.saved_videos)

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--cdp-url',required=True)
    ap.add_argument('--media-list-url',required=True)
    ap.add_argument('--mode',choices=['photo','video'],required=True)
    ap.add_argument('--output-dir',default='output')
    ap.add_argument('--max-items',type=int,default=100000)
    ap.add_argument('--scroll-pause',type=float,default=1)
    ap.add_argument('--click-interval',type=float,default=1)
    ap.add_argument('--max-no-change',type=int,default=4)
    ap.add_argument('--video-tab-index',type=int,default=1)
    ap.add_argument('--video-wait',type=float,default=8)
    ap.add_argument('--photo-wait',type=float,default=1.0)
    ap.add_argument('--skip-navigation',action='store_true')
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
            current=urlsplit(page.url)
            target=urlsplit(args.media_list_url)
            same_target=(
                current.scheme.lower()==target.scheme.lower()
                and current.netloc.lower()==target.netloc.lower()
                and current.path.rstrip('/')==target.path.rstrip('/')
            )
            if not same_target:
                print(f'現在のURLが対象メンバーと異なるため移動します: {args.media_list_url}')
                page.goto(args.media_list_url, wait_until='domcontentloaded')
                page.wait_for_timeout(1500)
            else:
                print(f'対象メンバーの一覧URLを確認しました: {page.url}')

            if args.mode == 'photo':
                photo_mode(page,args,c)
            else:
                video_mode(page,args,c)

        finally:
            c.stop()

    print(f'完了 写真={s.saved_photos} 動画={s.saved_videos}')
if __name__=='__main__':main()
