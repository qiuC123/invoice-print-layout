"""Bounded Windows photo retrieval through local nuphus; no model or sends."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
import hashlib
import importlib
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, cast

from PIL import Image

TZ = timezone(timedelta(hours=8))


def normalize(text: str) -> str:
    return re.sub(r'\s+', '', text.replace('（', '(').replace('）', ')'))


def header_matches(observed: str, expected: str, group: bool) -> bool:
    value, title = normalize(observed), normalize(expected)
    return value == title or bool(group and re.fullmatch(re.escape(title) + r'\(\d+\)', value))


def local_result_heading(text: str) -> bool:
    return normalize(text) in {'联系人', '群聊', '最常使用'}


def verify_filename(filename: str, target: dict[str, Any]) -> str:
    stamp = datetime.fromisoformat(target['sent_at']).astimezone(TZ).strftime('%Y%m%d%H%M%S')
    match = re.fullmatch(r'微信图片_(\d{14})_(\d+)_(\d+)\.(jpg|jpeg|png)', filename, re.I)
    # The final sequence is opaque; it is NOT necessarily the local message ID.
    if not match or match[1] != stamp or int(match[2]) != target['local_id']:
        raise ValueError('保存窗口的时间或消息编号不匹配，未保存此图')
    return match[4].lower()


def image_info(path: Path) -> dict[str, Any]:
    with Image.open(path) as image:
        image.load()
        if min(image.size) < 300:
            raise ValueError('保存图像过小，不能作为已补齐大图')
        size = list(image.size)
    return {'path': str(path.resolve()), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
            'bytes': path.stat().st_size, 'size': size}


def locate_thumbnail(screen: Image.Image, thumbnail: Image.Image) -> tuple[int, int] | None:
    """Compare complete images, never facial features. Reject separate matches."""
    cv: Any = importlib.import_module('cv2')
    np: Any = importlib.import_module('numpy')
    source = cv.cvtColor(np.asarray(screen.convert('RGB')), cv.COLOR_RGB2GRAY)
    anchor = cv.cvtColor(np.asarray(thumbnail.convert('RGB')), cv.COLOR_RGB2GRAY)
    if float(anchor.std()) < 5:
        raise ValueError('缩略图缺少可区分细节')
    matches: list[tuple[float, int, int, int, int]] = []
    # WeChat scales thumbnails to the current display size. Bounds avoid full
    # history searches and prevent tiny generic patches from matching.
    for scale in np.linspace(0.5, 3.0, 51):
        w, h = round(anchor.shape[1]*scale), round(anchor.shape[0]*scale)
        if min(w, h) < 40 or w >= source.shape[1] or h >= source.shape[0]:
            continue
        resized = cv.resize(anchor, (w, h))
        scores = cv.matchTemplate(source, resized, cv.TM_CCOEFF_NORMED)
        for _ in range(2):
            _, score, _, point = cv.minMaxLoc(scores)
            if score < 0.90:
                break
            x, y = point
            matches.append((float(score), x+w//2, y+h//2, w, h))
            scores[max(0, y-h+1):y+h, max(0, x-w+1):x+w] = -1
    if not matches:
        return None
    matches.sort(reverse=True)
    best = matches[0]
    if any(abs(m[1]-best[1]) > best[3]*0.6 or abs(m[2]-best[2]) > best[4]*0.6 for m in matches[1:]):
        raise ValueError('可见区域有多张相同缩略图，需人工定位')
    return best[1], best[2]


class LocalMcp:
    def __init__(self, executable: Path) -> None:
        if os.name != 'nt' or not executable.is_absolute() or not executable.is_file():
            raise ValueError('请配置本机nuphus可执行文件的绝对路径（Windows）')
        self.process = subprocess.Popen([str(executable), '--confirm-write'], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, encoding='utf-8',
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        self.messages: queue.Queue[dict[str, Any]] = queue.Queue()
        self.counter = 0
        threading.Thread(target=self._read, daemon=True).start()
        try:
            self.rpc('initialize', {'protocolVersion': '2024-11-05', 'capabilities': {},
                'clientInfo': {'name': 'wechat-photo-recovery', 'version': '1'}})
            self._send({'jsonrpc': '2.0', 'method': 'notifications/initialized'})
        except Exception:
            self.close()
            raise

    def _read(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            try:
                self.messages.put(json.loads(line))
            except ValueError:
                continue
        self.messages.put({'error': 'nuphus_closed'})

    def _send(self, body: dict[str, Any]) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(body)+'\n')
        self.process.stdin.flush()

    def rpc(self, method: str, params: dict[str, Any]) -> Any:
        self.counter += 1
        self._send({'jsonrpc': '2.0', 'id': self.counter, 'method': method, 'params': params})
        deadline = time.monotonic()+20
        while time.monotonic() < deadline:
            try:
                result = self.messages.get(timeout=max(0.01, deadline-time.monotonic()))
            except queue.Empty:
                raise ValueError('nuphus响应超时') from None
            if result.get('error'):
                raise ValueError('nuphus调用失败')
            if result.get('id') == self.counter:
                return result['result']
        raise ValueError('nuphus响应超时')

    def call(self, name: str, **args: Any) -> Any:
        result = self.rpc('tools/call', {'name': name, 'arguments': args})
        if result.get('isError'):
            raise ValueError('桌面操作失败：'+name)
        return json.loads(''.join(c.get('text', '') for c in result.get('content', [])))

    def close(self) -> None:
        self.process.terminate()
        try:
            self.process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=3)


class Native:
    def __init__(self) -> None:
        self.u: Any = ctypes.WinDLL('user32', use_last_error=True)
        self.u.GetForegroundWindow.restype = wintypes.HWND
        self.u.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        self.u.GetDlgCtrlID.argtypes = [wintypes.HWND]
        self.u.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM,
            wintypes.LPARAM, wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
        self.enum_type: Any = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        self.u.EnumChildWindows.argtypes = [wintypes.HWND, self.enum_type, wintypes.LPARAM]

    def foreground(self) -> int:
        return int(self.u.GetForegroundWindow())

    def filename(self, hwnd: int) -> str:
        values: list[str] = []

        def visit(child: int, unused: int) -> bool:
            kind = ctypes.create_unicode_buffer(256)
            self.u.GetClassNameW(child, kind, len(kind))
            if kind.value == 'Edit' and self.u.GetDlgCtrlID(child) == 1001:
                buffer = ctypes.create_unicode_buffer(4096)
                result = ctypes.c_size_t()
                if self.u.SendMessageTimeoutW(child, 13, len(buffer), ctypes.addressof(buffer), 2, 1000, ctypes.byref(result)):
                    values.append(buffer.value)
            return True

        self.u.EnumChildWindows(hwnd, self.enum_type(visit), 0)
        if len(values) != 1:
            raise ValueError('保存文件名控件无法唯一确定')
        return values[0]


class DesktopRecovery:
    def __init__(self, config: dict[str, Any], profile: dict[str, Any], folder: Path) -> None:
        self.config, self.profile, self.folder = config, profile, folder
        self.group = profile['chat_id'].endswith('@chatroom')
        if profile.get('unique_title') is not True or not profile.get('title') or any(c in profile['title'] for c in '\r\n\t'):
            raise ValueError('会话显示名称缺失或不唯一，不能自动定位')
        self.client = LocalMcp(Path(config['nuphus_executable']))
        try:
            self.native = Native()
            self.ocr: Any = importlib.import_module('rapidocr_onnxruntime').RapidOCR()
            self.np: Any = importlib.import_module('numpy')
            self.sequence = 0
            found = [w for w in self.client.call('desktop_windows_list') if w['title'] == '微信']
            if len(found) != 1:
                raise ValueError('微信主窗口不唯一')
            self.main: dict[str, Any] = found[0]
            if profile.get('process_id') != self.main['process_id']:
                raise ValueError('微信窗口与读取器进程不一致')
            if self.windows('保存') or self.windows('图片和视频'):
                raise ValueError('请先关闭已有微信图片查看器或保存窗口')
        except Exception:
            self.client.close()
            raise

    def close(self) -> None:
        self.client.close()

    def windows(self, title: str) -> list[dict[str, Any]]:
        return [w for w in self.client.call('desktop_windows_list')
                if w['title'] == title and w['process_id'] == self.main['process_id']]

    def activate(self, hwnd: int) -> None:
        if self.native.foreground() == hwnd:
            return
        result = self.client.call('desktop_window_activate', hwnd=hwnd, confirm=True)
        if result.get('activated') is not True or self.native.foreground() != hwnd:
            raise ValueError('未取得微信窗口焦点，停止操作')

    def keys(self, hwnd: int, keys: list[str]) -> None:
        self.activate(hwnd)
        self.client.call('desktop_input', hwnd=hwnd, mode='hotkey', keys=keys, confirm=True)

    def screenshot(self) -> Image.Image:
        self.sequence += 1
        self.activate(self.main['hwnd'])
        info = self.windows('微信')
        if len(info) != 1:
            raise ValueError('微信主窗口已变化')
        self.main = info[0]
        path = self.folder/f'view-{self.sequence:03}.png'
        self.client.call('desktop_window_screenshot', hwnd=self.main['hwnd'], path=str(path), confirm=True)
        with Image.open(path) as image:
            return cast(Image.Image, image.convert('RGB'))

    def words(self, image: Image.Image, box: tuple[int, int, int, int], *,
              confidence: float = 0.8) -> list[tuple[str, int, int]]:
        cropped = image.crop(box)
        scale = 3 if cropped.height < 100 else 1
        rows, _ = self.ocr(self.np.asarray(cropped.resize((cropped.width*scale, cropped.height*scale))))
        return [(str(r[1]), round(sum(p[0] for p in r[0])/4/scale)+box[0],
                 round(sum(p[1] for p in r[0])/4/scale)+box[1]) for r in rows or [] if r[2] >= confidence]

    def header(self, image: Image.Image) -> bool:
        text = ''.join(r[0] for r in self.words(image, (round(image.width*.247), round(image.height*.027),
                                                    round(image.width*.78), round(image.height*.066))))
        return header_matches(text, self.profile['title'], self.group)

    def click(self, image: Image.Image, x: int, y: int, *, double: bool = False) -> None:
        # Refuse coordinates after a move or resize rather than guessing.
        current = self.windows('微信')
        if len(current) != 1 or any(current[0][k] != self.main[k] for k in ('x', 'y', 'width', 'height')):
            raise ValueError('微信窗口位置已改变，请重试')
        self.activate(self.main['hwnd'])
        self.client.call('desktop_mouse', action='double_click' if double else 'click',
            x=self.main['x']+round(x*self.main['width']/image.width),
            y=self.main['y']+round(y*self.main['height']/image.height), confirm=True)

    def enter_chat(self) -> None:
        image = self.screenshot()
        if self.header(image):
            return
        # Locate the observed Search placeholder, then verify the result row.
        words = self.words(image, (0, 0, round(image.width*.245), round(image.height*.072)))
        targets = [r for r in words if re.fullmatch(r'[QqOo0]?搜索', normalize(r[0]))]
        if len(targets) != 1:
            raise ValueError('未找到微信搜索框，请清空现有搜索后重试')
        # OCR includes the magnifier icon; click inside the editable area to its
        # right, not the icon's hit region (which does not focus the text field).
        self.click(image, min(round(image.width*.20), targets[0][1]+round(image.width*.04)), targets[0][2])
        time.sleep(0.5)
        self.activate(self.main['hwnd'])
        # Short token avoids WeChat's multi-token/punctuation search behavior.
        # Selection and the final header still require the full exact title.
        token = re.match(r'\w+', self.profile['title'], re.UNICODE)
        query = token[0][:12] if token else self.profile['title'][:12]
        self.client.call('desktop_input', hwnd=self.main['hwnd'], mode='type',
                         text=query, send='none', confirm=True)
        # WeChat draws search results in a separate Weixin popup. A main-window
        # screenshot still shows the OLD chat list and must never select it.
        deadline = time.monotonic()+5
        popups: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            popups = [w for w in self.windows('Weixin') if w['width'] > 150 and w['height'] > 80
                      and self.main['x'] <= w['x'] < self.main['x']+self.main['width']*.4]
            if len(popups) == 1:
                break
            time.sleep(0.25)
        if len(popups) != 1:
            raise ValueError('微信搜索结果弹窗未出现或不唯一')
        popup = popups[0]
        time.sleep(0.5)
        self.sequence += 1
        path = self.folder/f'search-{self.sequence:03}.png'
        self.client.call('desktop_window_screenshot', hwnd=popup['hwnd'], path=str(path), confirm=True)
        with Image.open(path) as loaded:
            image = loaded.convert('RGB')
        # Only the top local-contact result; web-search suggestions below it are
        # not contacts and can contain exactly the same text.
        headings = self.words(image, (0, 0, image.width, min(40, image.height)), confidence=0.7)
        if not any(local_result_heading(r[0]) for r in headings):
            self.keys(self.main['hwnd'], ['esc'])
            raise ValueError('搜索没有返回本地联系人或群聊结果')
        words = self.words(image, (0, 35, image.width, min(110, image.height)))
        matches = [r for r in words if header_matches(r[0], self.profile['title'], self.group)]
        if len(matches) != 1:
            self.keys(self.main['hwnd'], ['esc'])
            raise ValueError('搜索结果未唯一匹配指定会话，未进入其他会话')
        if self.native.foreground() not in (self.main['hwnd'], popup['hwnd']):
            raise ValueError('搜索时焦点离开微信，停止选择')
        self.client.call('desktop_mouse', action='click', x=popup['x']+matches[0][1],
                         y=popup['y']+matches[0][2], confirm=True)
        time.sleep(0.6)
        if not self.header(self.screenshot()):
            raise ValueError('进入后会话标题核验失败')

    def wait_window(self, title: str) -> int:
        deadline = time.monotonic()+12
        while time.monotonic() < deadline:
            found = self.windows(title)
            if len(found) == 1:
                return int(found[0]['hwnd'])
            if len(found) > 1:
                raise ValueError('微信弹窗不唯一')
            time.sleep(0.25)
        raise ValueError('等待微信窗口超时：'+title)

    def recover_photo(self, target: dict[str, Any], thumbnail: dict[str, Any]) -> dict[str, Any]:
        thumb_path = Path(thumbnail['path'])
        if hashlib.sha256(thumb_path.read_bytes()).hexdigest() != thumbnail['sha256']:
            raise ValueError('缩略图校验失败')
        viewer: int | None = None
        dialog: int | None = None
        with Image.open(thumb_path) as opened:
            anchor = opened.convert('RGB')
        for step in range(int(self.config.get('max_scrolls', 6))+1):
            screen = self.screenshot()
            if not self.header(screen):
                raise ValueError('逐张补图前会话标题不匹配')
            box = (round(screen.width*.247), round(screen.height*.075), screen.width, round(screen.height*.86))
            point = locate_thumbnail(screen.crop(box), anchor)
            if point:
                self.click(screen, point[0]+box[0], point[1]+box[1], double=True)
                break
            if step == int(self.config.get('max_scrolls', 6)):
                raise ValueError('有限翻页内未找到目标缩略图，需人工定位或补发')
            self.activate(self.main['hwnd'])
            self.client.call('desktop_mouse', action='scroll', direction='up', amount=5,
                x=self.main['x']+round(self.main['width']*.75),
                y=self.main['y']+round(self.main['height']*.6), confirm=True)
            time.sleep(0.4)
        try:
            viewer = self.wait_window('图片和视频')
            self.keys(viewer, ['ctrl', 's'])
            dialog = self.wait_window('保存')
            self.activate(dialog)
            default = self.native.filename(dialog)
            suffix = verify_filename(default, target)
            stem = hashlib.sha256(json.dumps(target, sort_keys=True).encode()).hexdigest()[:20]
            path = self.folder/(stem+'.'+suffix)
            if path.exists():
                raise ValueError('本批次另存目标已存在，未覆盖')
            self.client.call('desktop_window_screenshot', hwnd=dialog, path=str(self.folder/(stem+'-source.png')), confirm=True)
            self.keys(dialog, ['alt', 'n'])
            self.keys(dialog, ['ctrl', 'a'])
            self.activate(dialog)
            self.client.call('desktop_input', hwnd=dialog, mode='type', text=str(path), send='none', confirm=True)
            if self.native.filename(dialog) != str(path):
                raise ValueError('另存路径核验失败')
            self.keys(dialog, ['alt', 's'])
            deadline = time.monotonic()+15
            while time.monotonic() < deadline:
                if path.exists() and not self.windows('保存'):
                    info = image_info(path)
                    dialog = None
                    return {**info, 'source': target, 'default_filename': default,
                        'quality': '微信界面另存，未核验与拍摄原件字节一致',
                        'acquisition': 'wechat_gui_save', 'verified_chat_id': self.profile['chat_id']}
                time.sleep(0.25)
            raise ValueError('另存超时，结果未确认')
        finally:
            for hwnd in (dialog, viewer):
                if hwnd:
                    # A cleanup failure propagates; do not keep interacting with
                    # another picture while a previous modal might remain.
                    self.keys(hwnd, ['esc'])
                    time.sleep(0.25)
