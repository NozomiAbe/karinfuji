from __future__ import annotations
import argparse, json, re, signal
from dataclasses import dataclass, field
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
                d=json.loads(p.read_text(encoding='utf-8')); self.photos.update(d.get('photos',[])); self.thumbs.update(d.get('thumbnails',[]))
            except Exception: pass
    def save(self):
        (self.root/'.processed.json').write_text(json.dumps({'photos':sorted(self.photos),'thumbnails':sorted(self.thumbs)},ensure_ascii=False,indent=2),encoding='utf-8')

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
            if mime(r)=='image/jpeg': self.jpeg(r)
            elif mime(r)=='video/mp4' and self.s.video_active:self.mp4(r)
        except Exception as e: print('response error:',e)
    def jpeg(self,r:Response):
        key=normalize_url(r.url)
        if self.s.photo_active and key not in self.s.photos:
            b=r.body(); p=unique(self.s.root/'photos'/filename(r.url,f'photo_{self.s.saved_photos+1:06}.jpg')); p.parent.mkdir(parents=True,exist_ok=True); p.write_bytes(b)
            self.s.photos.add(key); self.s.saved_photos+=1; self.s.save(); print('[PHOTO]',p)
        if self.s.video_active:self.current_thumb_keys.add(key)
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


def wait_for_media_list(page, timeout=30.0):
    """URLが /media-list/ になるまで待つ。"""

    print('メディア一覧への遷移を待っています...')

    deadline = page.evaluate('Date.now()') + int(timeout * 1000)

    while page.evaluate('Date.now()') < deadline:
        try:
            url = page.url

            # URLの末尾が /media-list/ になったら一覧ページと判断
            if url.endswith('/media-list'):
                print(f'メディア一覧へ遷移しました: {url}')
                return True

        except Exception:
            pass

        page.wait_for_timeout(300)

    print(f'{timeout}秒待っても /media-list へ遷移しませんでした。')
    print(f'現在のURL: {page.url}')

    return False


def photo_mode(page,args,c):
    c.s.photo_active=True

    print('写真一覧が開くのを待っています...')

    # 一覧が実際に表示されるまで待つ
    if not wait_for_media_list(page, 15.0):
        print('写真一覧を確認できないため終了します。')
        return

    print('写真一覧が開きました。ここから処理を開始します。')

    print(f"現在URL: {page.url}")
    print(f"ページタイトル: {page.title()}")

    try:
        print("imgタグ数:", page.locator("img").count())
    except Exception as e:
        print("img取得エラー:", e)
    # 現在表示されている分の遅延読み込みを待つ
    wait_for_current_media(page, c, 2.0)

    print(f'現在表示分のJPEG保存完了: {c.s.saved_photos}件')

    no_new_scrolls = 0
    last_sig = media_signature(page)

    while c.s.saved_photos < args.max_items:
        before_saved = c.s.saved_photos
        before, after = scroll_up(page)
        print(f'写真一覧を上へスクロール: {before} -> {after}')

        # スクロールによる遅延読み込みが終わり、新しいJPEGレスポンスが返るまで待つ。
        wait_for_current_media(page, c, max(1.0, args.scroll_pause))
        after_saved = c.s.saved_photos
        new_sig = media_signature(page)

        if after_saved > before_saved:
            no_new_scrolls = 0
            print(f'新規JPEGを保存: +{after_saved-before_saved}件 / 累計 {after_saved}件')
        else:
            no_new_scrolls += 1
            print(f'新規JPEGなし: {no_new_scrolls}/{args.max_no_change}回')

        # スクロール先が変わらず、かつ新しいJPEGも来ない場合も終了候補。
        if before == after and new_sig == last_sig:
            no_new_scrolls = max(no_new_scrolls, 1)
        last_sig = new_sig

        if no_new_scrolls >= args.max_no_change:
            print(f'上方向へ{args.max_no_change}回スクロールしても新規JPEGがないため終了します。')
            break

    print('写真終了:',c.s.saved_photos)

def video_mode(page,args,c):
    video_tab(page,args.video_tab_index)
    c.s.video_active=True

    print('動画一覧が開くのを待っています...')

    # 動画タブを開いた後、実際に一覧が表示されるまで待つ
    if not wait_for_media_list(page, 15.0):
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
    ap=argparse.ArgumentParser();ap.add_argument('--cdp-url',required=True);ap.add_argument('--media-list-url',required=True);ap.add_argument('--mode',choices=['photo','video'],required=True);ap.add_argument('--output-dir',default='output');ap.add_argument('--max-items',type=int,default=100000);ap.add_argument('--scroll-pause',type=float,default=1);ap.add_argument('--click-interval',type=float,default=1);ap.add_argument('--max-no-change',type=int,default=4);ap.add_argument('--video-tab-index',type=int,default=1);ap.add_argument('--video-wait',type=float,default=8);ap.add_argument('--skip-navigation',action='store_true');args=ap.parse_args()
    s=State(Path(args.output_dir));s.load()
    with sync_playwright() as pw:
        b,page=connect(pw,args.cdp_url);c=Capture(page,s);c.start()
        try:
            if not args.skip_navigation:
                page.goto(args.media_list_url, wait_until='domcontentloaded')
                page.wait_for_timeout(1500)
            else:
                print(f'現在のEdge URL: {page.url}')

            if args.mode == 'photo':
                photo_mode(page, args, c)
            else:
                video_mode(page, args, c)
        finally:c.stop()
    print(f'完了 写真={s.saved_photos} 動画={s.saved_videos}')
if __name__=='__main__':main()
