#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
slide2pdf —— 把"课件录屏视频"还原成 PDF。

原理
----
录屏类课件视频里，一页课件会静止停留若干秒（教师讲解），然后才切页。
静止期间画面（几乎）逐帧相同，切页/动画期间画面剧烈变化。于是：

  1. 对全片做低分辨率指纹（80x60 灰度 @ 2fps），逐帧算变化比例；
  2. 变化集中的区间 = 转场/动画；变化为零的区间 = 一页正在"停留"；
  3. 在每个停留区间里挑一帧质量最好（对前后 ±1s 也是静止、墨量足够）的；
  4. 全分辨率抽出这些帧，做一次重复页剔除；
  5. 把 PNG 的 IDAT 直接塞进 PDF 图像流（FlateDecode + PNG 15 号预测器），
     不做二次压缩 → 位图逐字节等价，无重编码损失。

依赖：ffmpeg / ffprobe（可用 PATH 或 --ffmpeg/--ffprobe 指定）+ numpy。
不需要 PIL / OpenCV。

用法
----
  python slide2pdf.py 课件录屏.mp4                    # 输出 课件录屏.pdf
  python slide2pdf.py 课件录屏.mp4 --dry-run          # 只分析不抽帧，先看会出几页
  python slide2pdf.py 课件录屏.mp4 -o out.pdf --preview prev.jpg
  python slide2pdf.py 录屏.mp4 --keep-motion --keep-blank   # 动画页/空白页也保留
"""
import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
import unicodedata

import numpy as np

EXAMPLES = """\
常用示例
  # 1) 全自动：静止换页检测（默认）
  python slide2pdf.py 课件.mp4

  # 2) 只处理 19:00–25:30 这一段
  python slide2pdf.py 课件.mp4 --start 00:19:00 --end 00:25:30

  # 3) 固定间隔：每 150 帧截一张（30fps 视频 = 每 5 秒）
  python slide2pdf.py 课件.mp4 --every 150

  # 4) 固定间隔：每 30 秒截一张
  python slide2pdf.py 课件.mp4 --every-sec 30

  # 5) 提高扫描密度（每秒扫 5 帧），抓 1 秒的快速切页
  python slide2pdf.py 课件.mp4 --scan-fps 5

  # 6) 只扫描视频参数（帧率/总帧数/时长/黑边），不出 PDF
  python slide2pdf.py 课件.mp4 --info
  python slide2pdf.py 课件.mp4 --info --json

"""

GW, GH = 80, 60          # 指纹网格
SW, SH = 480, 360        # 去重网格
DIFF = 20                # 单像素"变化"的灰度阈值


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def log(*a):
    print(*a, flush=True)


PROGRESS = None          # None=自动(终端才显示) / True=强制显示 / False=关闭

_ETA_WORST = " \u5269 59m59s"


def dwidth(s):
    """终端显示宽度：中文/全角占 2 列（len() 只数字符，中文标签会算少 → 折行）。"""
    n = 0
    for ch in s:
        n += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return n


class Bar:
    """单行实时进度条：先量终端宽度，再决定画什么 —— 永远只占一行。

    放不下时按优先级让路：额外信息 → 剩余时间 → 百分比小数 → 标签截断 → 丢掉计数。
    条宽按"最坏情况"（100% + 最长剩余时间）一次定死，不会忽长忽短；
    计数上万自动缩写（24.8k），不会把数字截成半截。
    """

    def __init__(self, total=0, label="", width=26, enabled=None,
                 stream=None, min_bar=6, min_interval=0.1, unit=""):
        self.total = int(total or 0)
        self.unit = unit
        self.label = label
        self.width = int(width)
        self.min_bar = int(min_bar)
        self.min_interval = float(min_interval)
        self.stream = stream or sys.stderr
        if enabled is None:
            shown = sys.stderr.isatty() if PROGRESS is None else bool(PROGRESS)
        else:
            shown = bool(enabled)
            if PROGRESS is False:
                shown = False
        self.enabled = bool(shown)
        self.t0 = time.time()
        self.t_start = None
        self._bw = None
        self._cols_at = None
        self._last_t = 0.0
        self._last_n = None
        self._last_line = None
        self._pad = 0
        self.unit = unit
        self.shown = False
        self.n = 0

    # ---------- 基础 ----------
    def _cols(self):
        try:
            cols = shutil.get_terminal_size((100, 24)).columns
        except Exception:
            cols = 100
        return max(20, int(cols)) - 1

    @staticmethod
    def _cnt(v, k=False):
        v = max(0, int(v))
        if not k:
            return str(v)
        if v >= 1000000:
            return "%.1fM" % (v / 1000000.0)
        if v >= 1000:
            return "%.1fk" % (v / 1000.0)
        return str(v)

    @staticmethod
    def _dur(sec):
        sec = max(0, int(round(sec)))
        if sec < 60:
            return "%ds" % sec
        if sec < 3600:
            return "%dm%02ds" % (sec // 60, sec % 60)
        return "%dh%02dm" % (sec // 3600, (sec % 3600) // 60)

    def _line(self, n, bw, pct_w=6, label=True, cnt=True, eta=True, extra="",
              eta_txt=None):
        el = max(1e-6, time.time() - (self.t_start or self.t0))
        segs = []
        if self.total > 0:
            frac = min(1.0, max(0.0, n / float(self.total)))
            fill = int(round(frac * bw))
            bar = "[" + "#" * fill + "." * (bw - fill) + "]"
            pct = ("%5.1f%%" if pct_w == 6 else "%4.1f%%") % (frac * 100.0)
            k = self.total >= 10000
            c = "%s/%s" % (self._cnt(n, k), self._cnt(self.total, k))
            if getattr(self, "unit", ""):
                c += self.unit
            e = ""
            if eta_txt is not None:
                e = eta_txt
            elif eta and n > 0 and frac < 1.0:
                rate = n / el
                if rate > 0:
                    e = " \u5269 %s" % self._dur((self.total - n) / rate)
        else:
            spin = "|/-\\"[int(el * 4) % 4]
            bar = spin * (bw + 2)
            pct, c, e = "  --- ", str(n), ""
        if label and self.label:
            segs.append(self.label)
        segs += [bar, pct]
        if cnt and c:
            segs.append(c)
        if e:
            segs.append(e.strip())
        if extra:
            segs.append(extra)
        return " ".join(segs)

    def _pick_bw(self, cols):
        for bw in range(self.width, self.min_bar - 1, -1):
            n = self.total or 1
            if dwidth(self._line(n, bw, 6, True, True, True, "", _ETA_WORST)) <= cols:
                return bw
        return self.min_bar

    def _fit(self, n, extra):
        cols = self._cols()
        if self._bw is None or self._cols_at is None or cols > self._cols_at:
            self._bw, self._cols_at = self._pick_bw(cols), cols
        bw = self._bw
        variants = [
            dict(pct_w=6, label=True,  cnt=True,  eta=True,  extra=extra),
            dict(pct_w=6, label=True,  cnt=True,  eta=True,  extra=""),
            dict(pct_w=6, label=True,  cnt=True,  eta=False, extra=""),
            dict(pct_w=4, label=True,  cnt=True,  eta=False, extra=""),
            dict(pct_w=4, label=False, cnt=True,  eta=False, extra=""),
            dict(pct_w=4, label=False, cnt=False, eta=False, extra=""),
            dict(pct_w=4, label=False, cnt=False, eta=False, extra="", bw=min(bw, self.min_bar)),
        ]
        for v in variants:
            b = v.pop("bw", bw)
            line = self._line(n, b, **v)
            if dwidth(line) <= cols:
                return line
        fixed = dwidth(self._line(n, self.min_bar, 4, False, False, False, ""))
        room = max(0, cols - fixed - 1)
        if room >= 2 and self.label:
            keep, cut, w = self.label, "", 0
            for ch in keep:
                cw = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
                if w + cw > room:
                    break
                cut += ch
                w += cw
            self.label = cut
            line = self._line(n, self.min_bar, 4, True, False, False, "")
            self.label = keep
            if dwidth(line) <= cols:
                return line
        out, w = "", 0
        for ch in self._line(n, self.min_bar, 4, False, False, False, ""):
            cw = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
            if w + cw > cols:
                break
            out += ch
            w += cw
        return out

    # ---------- 对外 ----------
    def update(self, n=None, extra="", force=False):
        if not self.enabled:
            return
        if n is not None:
            self.n = int(n)
        now = time.time()
        if self.t_start is None and self.n > 0:
            self.t_start = now
        same = (self.n == self._last_n)
        if not force and self.n != self.total:
            if now - self._last_t < (1.0 if same else self.min_interval):
                return
        self._last_n, self._last_t = self.n, now
        line = self._fit(self.n, extra)
        if line == self._last_line:
            return
        self._last_line = line
        w = dwidth(line)
        pad = max(w, self._pad)
        self.stream.write("\r" + line + " " * (pad - w))
        self.stream.flush()
        self._pad = w
        self.shown = True

    def done(self, keep=False):
        if not self.enabled or not self.shown:
            return
        self.stream.write("\n" if keep else "\r" + " " * self._pad + "\r")
        self.stream.flush()
        self.shown = False
        self._last_line = None
        self._pad = 0


def tc(sec):
    m, s = divmod(max(0.0, sec), 60)
    h, m = divmod(int(m), 60)
    return "%02d:%02d:%04.1f" % (h, m, s)


def parse_time(v):
    """接受 90 / 1:30 / 00:01:30.5 / 1h2m3s 这类写法，返回秒。"""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    t = str(v).strip()
    if ":" in t:
        parts = [float(x) for x in t.split(":")]
        sec = 0.0
        for x in parts:
            sec = sec * 60 + x
        return sec
    m = re.fullmatch(r"(?:(\d+(?:\.\d+)?)h)?(?:(\d+(?:\.\d+)?)m)?(?:(\d+(?:\.\d+)?)s)?", t)
    if m and any(m.groups()):
        return (float(m.group(1) or 0) * 3600 + float(m.group(2) or 0) * 60
                + float(m.group(3) or 0))
    return float(t)


def _exe_for_platform(path):
    """按文件头判断这个二进制能不能在当前系统跑（防止把 Linux 版 ffmpeg 拿到 Windows 上）"""
    try:
        with open(path, "rb") as f:
            head = f.read(4)
    except OSError:
        return False
    if not head:
        return False
    if os.name == "nt":
        return head[:2] == b"MZ"                       # PE/EXE
    if sys.platform == "darwin":
        return True                                    # 交给冒烟测试
    return head[:4] == b"\x7fELF" or head[:2] in (b"#!", b"MZ")


def _smoke(path):
    """真跑一次 -version，能跑通才算可用（WinError 193 这类会在这里被吃掉）"""
    try:
        r = subprocess.run([path, "-version"], capture_output=True, timeout=30)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _candidates(name, explicit=None):
    ext = ".exe" if os.name == "nt" else ""
    out = []
    if explicit:
        out.append(explicit)
    env = os.environ.get(name.upper())
    if env:
        out.append(env)
    here = os.path.dirname(os.path.abspath(__file__))
    for root in (here, os.path.dirname(here)):
        b = os.path.join(root, "bin")
        if not os.path.isdir(b):
            continue
        for d in sorted(os.listdir(b)):
            sub = os.path.join(b, d)
            if os.path.isdir(sub):
                out += [os.path.join(sub, name), os.path.join(sub, name + ext)]
            out += [os.path.join(b, name), os.path.join(b, name + ext),
                    os.path.join(b, d)]
    w = shutil.which(name)
    if w:
        out.append(w)
    if name == "ffmpeg":                       # pip install imageio-ffmpeg 兜底
        try:
            import imageio_ffmpeg
            out.append(imageio_ffmpeg.get_ffmpeg_exe())
        except Exception:
            pass
    return out


# ── 自检 + 自动下载 ffmpeg ────────────────────────────────────────────────
DOWNLOAD_POLICY = "auto"      # auto=终端里询问 / force=直接下 / never=不下载
ASSUME_YES = False
FFMPEG_HOME = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bin", "auto")
USER_AGENT = "slide2pdf/1.0 (+https://github.com/Stevehim2007/JLU-XZJD-PdfExtract)"


def _human(n):
    if n >= 1073741824:
        return "%.1f GB" % (n / 1073741824.0)
    if n >= 1048576:
        return "%.1f MB" % (n / 1048576.0)
    if n >= 1024:
        return "%.1f KB" % (n / 1024.0)
    return "%d B" % n


def _ffmpeg_sources():
    """按平台给出可自动下载的静态版（只放官方/半官方 HTTPS 源）。"""
    if os.name == "nt":
        u = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
        return [dict(url=u, kind="zip", members=("ffmpeg.exe", "ffprobe.exe"),
                     digest=("sha256", u + ".sha256"), size=119467219)]
    if sys.platform.startswith("linux"):
        u = "https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz"
        return [dict(url=u, kind="tar.xz", members=("ffmpeg", "ffprobe"),
                     digest=("md5", u + ".md5"), size=41888096)]
    return []          # macOS：没有稳定的官方静态包，引导用 brew


def http_download(url, dest, label="下载"):
    import urllib.request
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        mb = max(1, total // 1048576) if total else 0
        bar = Bar(mb, label, unit="MB")
        got = 0
        while True:
            chunk = r.read(262144)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            bar.update(got // 1048576)
        bar.done()
        return got


def _read_digest(url):
    import urllib.request
    try:
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as r:
            txt = r.read(4096).decode("utf-8", "replace").strip()
    except Exception:
        return None
    tok = txt.split()[0] if txt else ""
    return tok.lower() if all(ch in "0123456789abcdef" for ch in tok.lower()) else None


def _file_digest(path, algo):
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1048576), b""):
            h.update(chunk)
    return h.hexdigest()


def _extract_binaries(archive, kind, members, dest):
    """只把 members 里的几个可执行文件取出来（不整包解压，省空间）。"""
    got = {}
    if kind == "zip":
        with zipfile.ZipFile(archive) as z:
            for n in z.namelist():
                b = os.path.basename(n)
                if b in members and b not in got and not n.endswith("/"):
                    got[b] = z.read(n)
    else:
        mode = {"tar.xz": "r:xz", "tar.gz": "r:gz", "tar.bz2": "r:bz2"}[kind]
        with tarfile.open(archive, mode) as t:
            for m in t.getmembers():
                b = os.path.basename(m.name)
                if b in members and b not in got and m.isfile():
                    got[b] = t.extractfile(m).read()
    if not got:
        return []
    os.makedirs(dest, exist_ok=True)
    out = []
    for b, data in got.items():
        p = os.path.join(dest, b)
        with open(p, "wb") as f:
            f.write(data)
        try:
            os.chmod(p, 0o755)
        except OSError:
            pass
        out.append(p)
    return out


def install_ffmpeg(dest=None, assume_yes=None):
    """自检失败时自动下载一份静态版 ffmpeg 到 ./bin/auto/。返回目录或 None。"""
    if DOWNLOAD_POLICY == "never":
        return None
    assume_yes = ASSUME_YES if assume_yes is None else assume_yes
    srcs = _ffmpeg_sources()
    if not srcs:
        return None
    src = srcs[0]
    dest = dest or FFMPEG_HOME
    log("没找到能用的 ffmpeg，可以自动下载一份静态版：")
    log("  来源：%s" % src["url"])
    log("  大小：约 %s   放到：%s" % (_human(src["size"]), dest))
    if DOWNLOAD_POLICY == "auto":
        if not assume_yes and not sys.stdin.isatty():
            log("  当前不是交互终端，跳过自动下载。想直接下就加 --download-ffmpeg")
            return None
        if not assume_yes:
            try:
                ans = input("  现在下载？（Y/n） ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                return None
            if ans not in ("", "y", "yes"):
                log("  已取消。也可以自己装：Windows winget install Gyan.FFmpeg / "
                    "Linux apt install ffmpeg")
                return None
    tmpdir = tempfile.mkdtemp(prefix="ffmpeg-dl-")
    ext = {"zip": ".zip", "tar.xz": ".tar.xz"}.get(src["kind"], ".download")
    arch = os.path.join(tmpdir, "pkg" + ext)
    t0 = time.time()
    try:
        size = http_download(src["url"], arch, "  下载 ffmpeg")
        log("  下载完成：%s，用时 %.1fs" % (_human(size), time.time() - t0))
        algo, url = src["digest"]
        want = _read_digest(url)
        if want:
            got = _file_digest(arch, algo)
            if got != want:
                log("  ⚠ 校验失败：%s 期望 %s 实际 %s，已放弃" % (algo, want[:12], got[:12]))
                return None
            log("  校验通过（%s %s…）" % (algo, got[:12]))
        else:
            log("  ⚠ 没取到官方校验值，跳过校验（只校验了能解压 + 能运行）")
        files = _extract_binaries(arch, src["kind"], src["members"], dest)
    except Exception as e:
        log("  ⚠ 自动下载失败：%s: %s" % (type(e).__name__, e))
        return None
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
    if not files:
        log("  ⚠ 压缩包里没找到 ffmpeg/ffprobe")
        return None
    exe = os.path.join(dest, "ffmpeg" + (".exe" if os.name == "nt" else ""))
    if not _smoke(exe):
        log("  ⚠ 下载到的 ffmpeg 跑不起来（%s）" % exe)
        return None
    log("  ✅ 就绪：%s" % dest)
    log("     以后重跑会自动用这份；想换系统装的可删掉该目录")
    return dest


def doctor(ffmpeg=None, ffprobe=None):
    """把每个候选路径的体检结果摊开：能不能执行、冒烟测试过不过。"""
    log("ffmpeg/ffprobe 自检")
    for name, explicit in (("ffmpeg", ffmpeg), ("ffprobe", ffprobe)):
        log(" %s：" % name)
        seen = []
        for c in _candidates(name, explicit):
            if not c or c in seen:
                continue
            seen.append(c)
            if not os.path.isfile(c):
                continue
            ok_platform = _exe_for_platform(c)
            ok_run = _smoke(c) if ok_platform else False
            mark = "✅" if ok_run else ("⛔" if not ok_platform else "❌")
            why = "" if ok_run else ("  不是本系统的可执行文件" if not ok_platform
                                     else "  无法执行/-version 失败")
            log("   %s %s%s" % (mark, c, why))
        if not seen:
            log("   （没有候选）")
    srcs = _ffmpeg_sources()
    log(" 自动下载：%s" % ("可用（%s）" % srcs[0]["url"] if srcs
                          else "本平台不支持（macOS 用 brew install ffmpeg）"))

def find_tool(name, explicit=None, required=True):
    """找一个当前系统**真的能执行**的 ffmpeg/ffprobe，返回路径。"""
    tried = []
    ext = ".exe" if os.name == "nt" else ""
    for c in _candidates(name, explicit):
        if not c:
            continue
        # imageio-ffmpeg 给的 ffmpeg 路径旁边通常没有 ffprobe，顺手加一个同名兄弟
        for cand in (c, os.path.join(os.path.dirname(c), name + ext)):
            if not os.path.isfile(cand):
                continue
            if cand in tried:
                continue
            tried.append(cand)
            if not _exe_for_platform(cand):
                continue
            if _smoke(cand):
                return cand
            break
    if not required:
        return None
    if name == "ffmpeg" and DOWNLOAD_POLICY != "never":
        d = install_ffmpeg()
        if d:
            got = os.path.join(d, name + ext)
            if _smoke(got):
                return got
    if name == "ffmpeg":
        hint = ("找不到能用的 ffmpeg。\n"
                "  Windows: winget install Gyan.FFmpeg\n"
                "           或到 https://www.gyan.dev/ffmpeg/builds/ 下 "
                "ffmpeg-release-essentials.zip，解压后把 bin\\ffmpeg.exe、"
                "bin\\ffprobe.exe 放到本脚本同目录（或 ./bin/ 下）；\n"
                "           也可以 pip install imageio-ffmpeg 让本工具自动使用它。\n"
                "  macOS:   brew install ffmpeg      Linux: apt install ffmpeg\n"
                "  或用 --ffmpeg <路径> --ffprobe <路径> 显式指定。")
    else:
        hint = ("找不到能用的 ffprobe（ffmpeg 通常和它在同一个目录）。\n"
                "  把 ffprobe" + ext + " 放到与 ffmpeg 相同的目录，或用 --ffprobe 指定。")
    raise SystemExit(hint + "\n已尝试：\n  " + "\n  ".join(tried or ["（无候选）"]))


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, **kw)


def probe_ffmpeg(ffmpeg, path):
    """没有 ffprobe 时，用 `ffmpeg -i` 的 stderr 解析元数据。"""
    p = subprocess.run([ffmpeg, "-hide_banner", "-nostdin", "-i", path],
                       capture_output=True, text=True)
    err = p.stderr or ""
    d = dict(w=0, h=0, fps=0.0, dur=0.0, nb_frames=0, codec=None,
             pix_fmt=None, bit_rate=0, time_base=None)
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", err)
    if m:
        d["dur"] = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    m = re.search(r"bitrate: (\d+) kb/s", err)
    if m:
        d["bit_rate"] = int(m.group(1)) * 1000
    m = re.search(r"Video: (\w+).*?(\d{2,5})x(\d{2,5})", err)
    if m:
        d["codec"], d["w"], d["h"] = m.group(1), int(m.group(2)), int(m.group(3))
    m = re.search(r"Video: .*?([\d.]+) fps", err)
    if m:
        d["fps"] = float(m.group(1))
    m = re.search(r"Video: .*?, ([a-z0-9]+)(?:\(|,)", err)
    if m:
        d["pix_fmt"] = m.group(1)
    return d


def probe(ffprobe, path):
    out = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height,r_frame_rate,nb_frames",
         "-show_entries", "format=duration", "-of", "json", path],
        capture_output=True, text=True, check=True).stdout
    j = json.loads(out)
    st = j["streams"][0]
    num, den = (st.get("r_frame_rate") or "30/1").split("/")
    return dict(w=int(st["width"]), h=int(st["height"]),
                fps=float(num) / float(den or 1),
                dur=float(j["format"]["duration"]))


def measure_fps(ffprobe, path, dur, samples=3, win=2.0):
    """在片子中间均匀取几个窗口，逐帧读 pts，实测每个窗口的帧率。"""
    out = []
    if not ffprobe:
        return out
    for k in range(samples):
        t = max(0.0, dur * (k + 1) / (samples + 1) - win / 2)
        p = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0",
             "-read_intervals", "%.3f%%+%.3f" % (t, win),
             "-show_entries", "frame=pts_time", "-of", "csv=p=0", path],
            capture_output=True, text=True)
        pts = []
        for x in (p.stdout or "").split():
            try:
                pts.append(float(x))
            except ValueError:
                pass
        if len(pts) > 2:
            out.append(dict(t=t, n=len(pts), win=pts[-1] - pts[0],
                            fps=(len(pts) - 1) / (pts[-1] - pts[0])))
    return out


def video_info(ffprobe, path, ffmpeg=None):
    if not ffprobe:
        d = probe_ffmpeg(ffmpeg or "ffmpeg", path)
        return dict(codec=d["codec"], w=d["w"], h=d["h"], pix_fmt=d["pix_fmt"],
                    r_fps=d["fps"], avg_fps=d["fps"], nb_frames=0,
                    time_base=None, dur=d["dur"], size=os.path.getsize(path),
                    bit_rate=d["bit_rate"])
    st = subprocess.run(
        [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries",
         "stream=codec_name,width,height,r_frame_rate,avg_frame_rate,nb_frames,"
         "time_base,pix_fmt", "-show_entries", "format=duration,size,bit_rate",
         "-of", "json", path],
        capture_output=True, text=True, check=True).stdout
    j = json.loads(st)
    s0 = j["streams"][0]
    f0 = j.get("format", {})

    def frac(v):
        try:
            a, b = (v or "0/1").split("/")
            return float(a) / float(b or 1)
        except Exception:
            return 0.0

    return dict(codec=s0.get("codec_name"), w=int(s0.get("width", 0)),
                h=int(s0.get("height", 0)),
                pix_fmt=s0.get("pix_fmt"),
                r_fps=frac(s0.get("r_frame_rate")),
                avg_fps=frac(s0.get("avg_frame_rate")),
                nb_frames=int(s0.get("nb_frames") or 0),
                time_base=s0.get("time_base"),
                dur=float(f0.get("duration") or 0),
                size=int(f0.get("size") or 0),
                bit_rate=int(f0.get("bit_rate") or 0))


def detect_crop(ffmpeg, path, dur, w, h, start=0.0):
    """用 3 个时间点的 cropdetect 求众数黑边；全片无黑边时返回全画幅。"""
    votes = {}
    for frac in (0.15, 0.5, 0.85):
        t = start + max(0.0, min(dur - 20, dur * frac))
        try:
            p = subprocess.run(
                [ffmpeg, "-hide_banner", "-nostdin", "-v", "info",
                 "-ss", "%.1f" % t, "-i", path, "-t", "15", "-an",
                 "-vf", "cropdetect=limit=20:round=2:reset=15",
                 "-f", "null", "-"],
                capture_output=True, text=True)
        except subprocess.CalledProcessError:
            continue
        for m in re.findall(r"crop=(\d+):(\d+):(\d+):(\d+)", p.stderr or ""):
            cw, ch, cx, cy = map(int, m)
            votes[(cw, ch, cx, cy)] = votes.get((cw, ch, cx, cy), 0) + 1
    if not votes:
        return w, h, 0, 0
    best = max(votes.items(), key=lambda kv: (kv[1], kv[0][0] * kv[0][1]))[0]
    cw, ch, cx, cy = best
    if cw * ch < 0.25 * w * h:          # 几乎全是黑的，不可信
        return w, h, 0, 0
    return cw, ch, cx, cy


# --------------------------------------------------------------------------- #
# 第 1 遍：低分辨率指纹
# --------------------------------------------------------------------------- #
def seek_args(start, dur):
    a = []
    if start:
        a += ["-ss", "%.3f" % start]
    if dur:
        a += ["-t", "%.3f" % dur]
    return a


def fingerprint(ffmpeg, path, crop, fps, tmp, start=0.0, dur=None):
    cw, ch, cx, cy = crop
    raw = os.path.join(tmp, "fp.raw")
    vf = ("crop=%d:%d:%d:%d,fps=%s,scale=%d:%d:flags=area,format=gray"
          % (cw, ch, cx, cy, fps, GW, GH))
    fb = GW * GH                               # 每帧字节数
    total = int(dur * float(fps)) if dur else 0
    bar = Bar(total, "  解码扫描")
    cmd = ([ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-v", "error"]
           + seek_args(start, dur)
           + ["-i", path, "-an", "-sn", "-vf", vf,
              "-f", "rawvideo", "-pix_fmt", "gray", "-"])
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         bufsize=fb * 64)
    nbytes = 0
    with open(raw, "wb") as fh:
        while True:
            chunk = p.stdout.read(fb * 64)
            if not chunk:
                break
            fh.write(chunk)
            nbytes += len(chunk)
            bar.update(nbytes // fb)
    err = p.stderr.read().decode("utf-8", "replace")
    p.stdout.close()
    p.stderr.close()
    rc = p.wait()
    bar.update(nbytes // fb, force=True)
    bar.done()
    if rc != 0:
        raise SystemExit("解码失败：\n" + err[-1500:])
    n = os.path.getsize(raw) // fb
    log("  解码完成：%d 帧（%d x %d @ %g fps）用时 %.0fs"
        % (n, GW, GH, fps, time.time() - bar.t0))
    return np.memmap(raw, dtype=np.uint8, mode="r", shape=(n, GH, GW)), raw


def analyse(A, fps, thr, min_hold, mask_anim, keep_motion,
            drop_blank=True, max_gap=2, lag=2):
    F = A.shape[0]
    D = np.empty((F - 1, GH, GW), dtype=bool)
    for i in range(0, F - 1, 512):
        blk = A[i:i + 513].astype(np.int16)
        D[i:i + 512] = np.abs(np.diff(blk, axis=0)) > DIFF
    D2 = D.reshape(F - 1, -1)
    freq = D2.mean(0)
    mask = freq < mask_anim
    if mask.sum() < 0.3 * mask.size:        # 动图占太多，全画幅算
        mask = np.ones_like(mask)
    chg = D2[:, mask].mean(1)
    ink = (A.reshape(F, -1) < 200).mean(1)

    hot = np.where(chg > thr)[0]
    blocks = []
    for i in hot:
        if blocks and i - blocks[-1][1] <= max_gap:
            blocks[-1][1] = i
        else:
            blocks.append([i, i])

    runs, cur = [], 0
    for s, e in blocks:
        if s - cur > 0:
            runs.append((cur, s))
        cur = e + 1
    if F - cur > 0:
        runs.append((cur, F))

    lagc = np.zeros(F)
    for i in range(F):
        lo, hi = max(0, i - lag), min(F - 1, i + lag)
        lagc[i] = chg[lo:hi].max() if hi > lo else 0.0
    clean_thr = max(thr * 0.5, 0.004)

    cands, dropped = [], []
    for s, e in runs:
        ln = e - s
        if ln < max(1, int(round(min_hold * fps))):
            continue
        pool = list(range(s, e))
        cl = [i for i in pool if lagc[i] <= clean_thr]
        if cl:
            med = float(np.median(ink[cl]))
            good = [i for i in cl if ink[i] >= med]
            conf = "high"
        elif keep_motion:
            good, conf = pool, "low"
        else:
            dropped.append(dict(kind="motion", s=s, e=e, n=ln,
                                why="整段都在动（动画/擦屏），无干净帧"))
            continue
        mid = (s + e - 1) / 2.0
        pick = min(good, key=lambda i: (abs(i - mid), -ink[i]))
        iv = float(ink[pick]) * 100
        if drop_blank and (iv < 0.6 or iv > 99.0):
            dropped.append(dict(kind="blank", s=s, e=e, n=ln, ink=iv,
                                why="整页空白/纯黑（ink=%.1f%%），不是有效课件页" % iv))
            continue
        cands.append(dict(frame=pick, s=s / fps, e=e / fps, t=pick / fps,
                          dwell=ln / fps, ink=iv, conf=conf,
                          blank_risk=(iv < 2.0)))
    return cands, dropped, dict(frames=F, blocks=len(blocks), runs=len(runs),
                                clean_ratio=float((lagc <= clean_thr).mean()))


# --------------------------------------------------------------------------- #
# 第 2 遍：全分辨率抽帧（同时输出去重用小图）
# --------------------------------------------------------------------------- #
class ExtractError(Exception):
    """抽帧阶段的错误（可被 seek 兜底重试）。"""


def select_expr(frames):
    idx = sorted(set(int(f) for f in frames))
    parts, start, prev = [], None, None
    for i in idx:
        if start is None:
            start = prev = i
        elif i == prev + 1:
            prev = i
        else:
            parts.append("between(n\\,%d\\,%d)" % (start, prev))
            start = prev = i
    if start is not None:
        parts.append("between(n\\,%d\\,%d)" % (start, prev))
    return "+".join(parts)


def extract(ffmpeg, path, crop, fps, frames, outdir, pred=True,
            start=0.0, dur=None, step=None, max_frames=None, total=0,
            mode="auto"):
    """frames 给定时按这些帧号抽；step 给定时按"每 step 帧一张"抽（此时 fps=None）。"""
    cw, ch, cx, cy = crop
    if step:
        sel = "not(mod(n\\,%d))" % int(step)
        chain = "crop=%d:%d:%d:%d" % (cw, ch, cx, cy)
    else:
        sel = select_expr(frames)
        chain = "crop=%d:%d:%d:%d,fps=%s" % (cw, ch, cx, cy, fps)
    vf = (chain + ",select='%s',split=2[a][b];"
          "[a]format=rgb24[o1];[b]scale=%d:%d:flags=area,format=gray[o2]"
          % (sel, SW, SH))
    pngdir = os.path.join(outdir, "png")
    os.makedirs(pngdir, exist_ok=True)
    small = os.path.join(outdir, "small.raw")

    def build(flags, with_pred):
        """flags = 不补帧参数（不同 ffmpeg 版本名字不一样）。"""
        cmd = ([ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-v", "error",
                "-progress", "pipe:1"]
               + seek_args(start, dur)
               + ["-i", path, "-an", "-sn", "-filter_complex", vf,
                  "-map", "[o1]"] + list(flags))
        if max_frames:
            cmd += ["-frames:v", str(int(max_frames))]
        if with_pred:
            cmd += ["-pred", "mixed"]
        cmd += ["-start_number", "1", os.path.join(pngdir, "%05d.png"),
                "-map", "[o2]"] + list(flags) + ["-f", "rawvideo",
                                                 "-pix_fmt", "gray", small]
        return cmd

    def _seek_run():
        """逐帧 seek 抽取：不拼 select 表达式，任何 ffmpeg 版本都不会爆
        「Error initializing filters / Cannot allocate memory」，代价是每帧一个进程。"""
        bar = Bar(len(frames), "  抽帧(seek)")
        with_pred = bool(pred)
        raw_parts = []
        for i, n in enumerate(frames, 1):
            t = start + n / float(fps)          # fps 滤镜的第 n 帧 = 时刻 n/fps
            raw_i = os.path.join(outdir, "s_%05d.raw" % i)
            base = ([ffmpeg, "-hide_banner", "-nostdin", "-nostats", "-v", "error",
                     "-ss", "%.3f" % t, "-i", path, "-an", "-sn",
                     "-filter_complex",
                     "crop=%d:%d:%d:%d,split=2[a][b];[a]format=rgb24[o1];"
                     "[b]scale=%d:%d:flags=area,format=gray[o2]"
                     % (cw, ch, cx, cy, SW, SH),
                     "-map", "[o1]"])
            tail = (["-map", "[o2]", "-f", "rawvideo", "-pix_fmt", "gray",
                     "-frames:v", "1", raw_i])   # 每个输出都要限量，否则写满磁盘
            for attempt in ("pred", "plain"):
                if attempt == "pred" and not with_pred:
                    continue
                cmd = base + (["-pred", "mixed"] if attempt == "pred" else []) + [
                    "-frames:v", "1", os.path.join(pngdir, "%05d.png" % i)] + tail
                rc, err = _go_quiet(cmd)
                if rc == 0:
                    break
                if "Unrecognized option" in err or "Option not found" in err:
                    with_pred = False
                    continue
                raise ExtractError("抽帧失败（第 %d 帧 / n=%d）：\n%s"
                                   % (i, n, (err or "").strip()[-800:]))
            else:
                raise ExtractError("抽帧失败（第 %d 帧 / n=%d）" % (i, n))
            raw_parts.append(raw_i)
            bar.update(i)
        bar.done()
        with open(small, "wb") as fh:
            for raw_i in raw_parts:
                with open(raw_i, "rb") as src:
                    shutil.copyfileobj(src, fh)
                os.unlink(raw_i)
        pngs = sorted(os.path.join(pngdir, f) for f in os.listdir(pngdir))
        log("  抽帧完成（逐帧 seek）：%d 张（%dx%d PNG + %dx%d 灰度副本）"
            % (len(pngs), cw, ch, SW, SH))
        n = os.path.getsize(small) // (SW * SH)
        return pngs, np.memmap(small, dtype=np.uint8, mode="r", shape=(n, SH, SW))

    def _go_quiet(cmd):
        pr = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                            stderr=subprocess.PIPE, text=True)
        return pr.returncode, pr.stderr

    def _go(cmd):
        pr = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True, bufsize=1)
        bar = Bar(total, "  抽帧")
        for line in pr.stdout:
            line = line.strip()
            if line.startswith("frame="):
                try:
                    bar.update(int(line[6:]))
                except ValueError:
                    pass
        err = pr.stderr.read()
        pr.stdout.close()
        pr.stderr.close()
        rc = pr.wait()
        bar.done()
        return rc, err

    def _select_pass():
        """原有的"一次解码 + select 表达式"抽帧；失败抛 ExtractError。"""
        # ffmpeg 5.x/6.x 用 -vsync 0，7.x 起改名 -fps_mode passthrough，9.x 又删了 -vsync；
        # 所以按"新名 → 旧名 → 不要"顺序试，只对"选项不认识"这类错误降级，别的错误直接报。
        tries = []
        for flags in (("-fps_mode", "passthrough"), ("-vsync", "0"),
                      ("-vsync", "vfr")):
            tries.append((flags, bool(pred)))
            if pred:
                tries.append((flags, False))
        rc, err, used = 1, "", None
        for flags, with_pred in tries:
            rc, err = _go(build(flags, with_pred))
            if rc == 0:
                used = (flags, with_pred)
                break
            if "Unrecognized option" not in err and "Option not found" not in err:
                raise ExtractError("抽帧失败：\n" + (err or "")[-2000:])
        if rc != 0:
            raise ExtractError(
                "抽帧失败：这个 ffmpeg 连「不补帧」的参数都不认（试过 -fps_mode "
                "passthrough / -vsync 0 / -vsync vfr）。\n"
                "少了它会按 30 fps 把同一帧复制成几百张，抽帧结果就是错的，"
                "所以这里直接停下，不猜。\n"
                "办法：换一个 ffmpeg 5.1 以上的版本，或用 --ffmpeg 指定完整路径。\n"
                "原始报错：\n" + (err or "")[-1200:])
        return rc, err, used

    if mode == "seek":
        return _seek_run()
    if mode == "auto" and not step and len(sel) > 8000:
        log("  候选帧太多（select 表达式 %d 字符），直接改用逐帧 seek 抽取" % len(sel))
        return _seek_run()
    try:
        rc, err, used = _select_pass()
    except ExtractError as e:
        if mode == "select":
            raise SystemExit(str(e))
        flat = " ".join((str(e) or "").split())
        log("  ⚠ select 方式抽帧失败：%s" % flat[:240])
        log("  自动改用逐帧 seek 抽取（结果相同，只是每帧一个进程、慢一些）")
        for d in (pngdir,):
            if os.path.isdir(d):
                for f in os.listdir(d):
                    try:
                        os.unlink(os.path.join(d, f))
                    except OSError:
                        pass
        if os.path.exists(small):
            os.unlink(small)
        return _seek_run()
    flags, with_pred = used
    if flags[0] != "-fps_mode":
        log("  注意：这个 ffmpeg 不认 -fps_mode，已退回 %s %s" % flags)
    if pred and not with_pred:
        log("  注意：这个 ffmpeg 不认 -pred mixed，已退回默认预测器")

    pngs = sorted(os.path.join(pngdir, f) for f in os.listdir(pngdir))
    if frames and not max_frames and len(pngs) != len(frames):
        log("  ⚠ 抽帧数量异常：预期 %d 张，实得 %d 张"
            "（多半是 ffmpeg 往输出里插了重复帧）" % (len(frames), len(pngs)))
    log("  抽帧完成：%d 张（%dx%d PNG + %dx%d 灰度副本）"
        % (len(pngs), cw, ch, SW, SH))
    n = os.path.getsize(small) // (SW * SH)
    S = np.memmap(small, dtype=np.uint8, mode="r", shape=(n, SH, SW))
    return pngs, S


# --------------------------------------------------------------------------- #
# 去重
# --------------------------------------------------------------------------- #
def wipe_metrics(a):
    """返回 (竖白带占比, 横白带占比)：最长的一段"几乎没墨"的行/列。

    用于识别擦除/百叶窗/推入这类**转场中间态**：它们的特征是整条整条的空行列
    （比如从右往左推进时，右半边是纯白），而正常课件页很少出现 >45% 的整条空白带。
    """
    ink = (a < 200)
    def longest(profile, total, thr=0.002):
        best = cur = 0
        for v in profile:
            cur = cur + 1 if v < thr else 0
            if cur > best:
                best = cur
        return best / float(total)
    return longest(ink.mean(0), a.shape[1]), longest(ink.mean(1), a.shape[0])


def pair_metrics(S, i, j):
    a = np.asarray(S[i % len(S)]).astype(np.int16)
    b = np.asarray(S[j % len(S)]).astype(np.int16)
    d = np.abs(a - b)
    chg = float((d > DIFF).mean()) * 100
    loss = float(((a < 200) & (b >= 200)).mean()) * 100   # 上一页有、这一页没了
    add = float(((a >= 200) & (b < 200)).mean()) * 100    # 这一页新增的
    return chg, loss, add


def dedupe(S, cands, verbose=False):
    """剔除重复页，并把同一页的"逐步补内容(build)"合并成最完整的那一帧。

    相邻两帧若"只增不减"（loss≈0 且 add>0），说明是同一页在往下加内容/播动画，
    丢掉旧的那帧、保留新的（更完整）；这样中间态（看着像"被白块盖住/内容不全"）
    不会被当成一页。
    """
    keep, why = [], []
    for i, c in enumerate(cands):
        if not keep:
            keep.append(i)
            continue
        chg, loss, add = pair_metrics(S, cands[keep[-1]]["frame"], c["frame"])
        if loss <= 2.0 and add >= 0.5 and chg < 60.0:
            why.append((i, "同页补内容(build)，保留更完整的一帧", chg, loss, add))
            keep[-1] = i
            continue
        if chg < 3.0 or (chg < 10.0 and loss < 8.0 and add < 8.0):
            why.append((i, "重复页", chg, loss, add))
            continue
        keep.append(i)
    if verbose:
        for i, r, chg, loss, add in why:
            log("  合并 %s  ←  %s (chg=%.1f loss=%.1f add=%.1f)"
                % (tc(cands[i]["t"]), r, chg, loss, add))
    return keep


# --------------------------------------------------------------------------- #
# PDF（复用 PNG 的 IDAT，零重编码）
# --------------------------------------------------------------------------- #
def png_idat_and_size(p):
    data = open(p, "rb").read()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise SystemExit("不是 PNG：%s" % p)
    pos, ids, w = 8, [], 0
    while pos < len(data):
        ln = int.from_bytes(data[pos:pos + 4], "big")
        typ = data[pos + 4:pos + 8]
        body = data[pos + 8:pos + 8 + ln]
        if typ == b"IHDR":
            w = int.from_bytes(body[0:4], "big")
            h = int.from_bytes(body[4:8], "big")
            if body[8] != 8 or body[9] != 2:
                raise SystemExit("只支持 8bit RGB PNG：%s" % p)
        elif typ == b"IDAT":
            ids.append(body)
        elif typ == b"IEND":
            break
        pos += 12 + ln
    return w, h, b"".join(ids)


def build_pdf(pngs, out, dpi=150.0, title=None):
    """对象号：1=Catalog 2=Pages 3=Info；第 i 页 = 4+2i，其图像 = 5+2i；
    内容流 = 4+2n+i。图像流直接复用 PNG 的 IDAT（FlateDecode + 15 号预测器）。"""
    n = len(pngs)
    kids = " ".join("%d 0 R" % (4 + 2 * i) for i in range(n))
    objs = {1: b"<< /Type /Catalog /Pages 2 0 R >>",
            2: ("<< /Type /Pages /Count %d /Kids [%s] >>" % (n, kids)).encode()}
    t = re.sub(r"[\()]", "", title or "slide2pdf")
    objs[3] = ("<< /Producer (slide2pdf) /Creator (slide2pdf) /Title (%s) >>"
               % t).encode()
    contents = []
    for i, p in enumerate(pngs):
        w, h, idat = png_idat_and_size(p)
        pw, ph = w * 72.0 / dpi, h * 72.0 / dpi
        objs[4 + 2 * i] = ("<< /Type /Page /Parent 2 0 R "
                           "/MediaBox [0 0 %.4f %.4f] "
                           "/Resources << /XObject << /Im0 %d 0 R >> >> "
                           "/Contents %d 0 R >>"
                           % (pw, ph, 5 + 2 * i, 4 + 2 * n + i)).encode()
        objs[5 + 2 * i] = (
            ("<< /Type /XObject /Subtype /Image /Width %d /Height %d "
             "/ColorSpace /DeviceRGB /BitsPerComponent 8 /Filter /FlateDecode "
             "/DecodeParms << /Predictor 15 /Colors 3 /BitsPerComponent 8 "
             "/Columns %d >> /Length %d >>\nstream\n"
             % (w, h, w, len(idat))).encode() + idat + b"\nendstream")
        contents.append(("q %.4f 0 0 %.4f 0 0 cm /Im0 Do Q\n"
                         % (pw, ph)).encode())
    for i, body in enumerate(contents):
        objs[4 + 2 * n + i] = (("<< /Length %d >>\nstream\n" % len(body)).encode()
                               + body + b"endstream")

    maxnum = 4 + 2 * n + n - 1
    offs = {}
    with open(out, "wb") as fh:
        fh.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        pos = fh.tell()
        for num in range(1, maxnum + 1):
            offs[num] = pos
            chunk = ("%d 0 obj\n" % num).encode() + objs[num] + b"\nendobj\n"
            fh.write(chunk)
            pos += len(chunk)
        xref = pos
        fh.write(("xref\n0 %d\n" % (maxnum + 1)).encode())
        fh.write(b"0000000000 65535 f \n")
        for num in range(1, maxnum + 1):
            fh.write(("%010d 00000 n \n" % offs[num]).encode())
        fh.write(("trailer\n<< /Size %d /Root 1 0 R /Info 3 0 R >>\n"
                  "startxref\n%d\n%%%%EOF\n" % (maxnum + 1, xref)).encode())
    return out


# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# 输出路径
# --------------------------------------------------------------------------- #
def _writable(d):
    try:
        os.makedirs(d, exist_ok=True)
        t = os.path.join(d, ".slide2pdf-test")
        with open(t, "wb") as fh:
            fh.write(b"x")
        os.remove(t)
        return True
    except Exception:
        return False


def _near_dir(missing):
    """给不存在的目录找一个最接近的现有兄弟目录，用于提示拼写错误。"""
    import difflib
    missing = os.path.abspath(missing)
    parent = os.path.dirname(missing.rstrip("\\/")) or os.curdir
    grand = os.path.dirname(parent) or os.curdir
    try:
        names = os.listdir(grand)
    except OSError:
        return None
    hit = difflib.get_close_matches(os.path.basename(parent), names, n=1, cutoff=0.6)
    return os.path.join(grand, hit[0]) if hit else None


def check_out_path(path, what="输出 PDF", mkdir=False):
    """在花几分钟解码之前先确认写得进去（目录不存在时给拼写提示）。"""
    path = os.path.abspath(path)
    d = os.path.dirname(path) or os.curdir
    if not os.path.isdir(d):
        if mkdir:
            try:
                os.makedirs(d, exist_ok=True)
                log("输出目录不存在，已创建：%s" % d)
            except OSError as e:
                raise SystemExit("建不了目录：%s（%s）" % (d, e))
        else:
            hint = _near_dir(path)
            msg = ["%s 的目录不存在：%s" % (what, d),
                   "  完整路径：%s" % path]
            if hint:
                msg.append("  是不是想写：%s" % os.path.join(hint, os.path.basename(path)))
            msg.append("  两种办法：① 用 --outdir 指定目录（不存在会自动创建）"
                       " ② 自己先把该目录建好")
            raise SystemExit("\n".join(msg))
    if not os.access(d, os.W_OK):
        raise SystemExit("%s 的目录不可写：%s" % (what, d))
    return path


def resolve_outdir(video, outdir=None):
    """默认输出到视频所在文件夹；不可写就退回当前目录。"""
    if outdir:
        want = os.path.abspath(outdir)
        if not os.path.isdir(want):
            try:
                os.makedirs(want, exist_ok=True)
                log("输出目录不存在，已创建：%s" % want)
            except OSError as e:
                raise SystemExit("建不了输出目录：%s（%s）" % (want, e))
    else:
        want = os.path.dirname(os.path.abspath(video)) if video else os.getcwd()
    if _writable(want):
        return want
    fallback = os.getcwd()
    log("⚠ 目录不可写：%s\n  改用当前目录：%s" % (want, fallback))
    return fallback


def pick_out(out, outdir, default_name):
    """-o 可以是绝对路径、带目录的相对路径，或纯文件名（放到 outdir 里）。"""
    if out:
        if os.path.isabs(out) or os.path.dirname(out):
            return os.path.abspath(out)
        return os.path.join(outdir, out)
    return os.path.join(outdir, default_name)


# --------------------------------------------------------------------------- #
# PDF 合并
# --------------------------------------------------------------------------- #
def _xref_parse(data):
    """解析传统 xref 表；返回 (entries{obj: offset}, trailer 字节)。"""
    i = data.rfind(b"startxref")
    if i < 0:
        raise ValueError("找不到 startxref")
    m = re.match(rb"startxref\s+(\d+)", data[i:i + 40])
    if not m:
        raise ValueError("startxref 格式异常")
    off = int(m.group(1))
    if data[off:off + 4] != b"xref":
        raise ValueError("不是传统 xref 表（xref 流/对象流不支持无依赖合并）")
    entries, p, trailer = {}, off + 4, b""
    for _ in range(200):
        while p < len(data) and data[p:p + 1] in b" \r\n":
            p += 1
        if data[p:p + 7] == b"trailer":
            trailer = data[p + 7:]
            break
        m = re.match(rb"(\d+)\s+(\d+)", data[p:p + 30])
        if not m:
            raise ValueError("xref 子段头异常 @%d" % p)
        start, count = int(m.group(1)), int(m.group(2))
        p += m.end()
        for k in range(count):
            while p < len(data) and data[p:p + 1] in b" \r\n":
                p += 1          # 子段头后面还有一个换行
            m2 = re.match(rb"(\d{10})\s+(\d{5})\s+([nf])", data[p:p + 20])
            if not m2:
                raise ValueError("xref 条目异常 @%d" % p)
            if m2.group(3) == b"n":
                entries[start + k] = int(m2.group(1))
            p += m2.end()
            while p < len(data) and data[p:p + 1] in b" \r\n":
                p += 1          # 每条 20 字节，正则只吃到 'n'/'f'，剩下的空白自己跳
    if not trailer:
        raise ValueError("没找到 trailer")
    return entries, trailer


def _obj_chunk(data, off, offsets=None):
    """按对象结构精确切出一个对象（含流时用 /Length，绝不靠找 endobj，
    因为压缩后的二进制里也可能偶然出现 endobj/endstream 字节）。"""
    m = re.match(rb"\s*\d+\s+\d+\s+obj\b", data[off:off + 64])
    if not m:
        raise ValueError("对象头异常 @%d" % off)
    body = off + m.end()
    eo = data.find(b"endobj", body)
    sp = data.find(b"stream", body)
    if sp < 0 or (eo >= 0 and eo < sp):          # 没有流：到 endobj 为止
        return data[off:(eo + 6) if eo >= 0 else len(data)]
    ml = re.search(rb"/Length\s+(\d+)(?!\s+\d+\s+R)", data[off:sp])
    if not ml:
        raise ValueError("流对象缺少直接 /Length @%d" % off)
    st = sp + 6
    if data[st:st + 2] == b"\r\n":
        st += 2
    elif data[st:st + 1] in b"\r\n":
        st += 1
    e2 = data.find(b"endobj", st + int(ml.group(1)))
    return data[off:(e2 + 6) if e2 >= 0 else len(data)]


_REF = re.compile(rb"(\d+)\s+(\d+)\s+R")


def _rewrite_refs(chunk, mapping):
    """只改字典部分（stream 之后是二进制，绝不能动）。"""
    m = re.search(rb">>\s*stream", chunk)
    head, tail = (chunk[:m.start() + 2], chunk[m.start() + 2:]) if m else (chunk, b"")

    def sub(mm):
        num = int(mm.group(1))
        new = mapping.get(num)
        return b"%d 0 R" % new if new else mm.group(0)

    return _REF.sub(sub, head) + tail


def _kids_of(chunk):
    m = re.search(rb"/Kids\s*\[(.*?)\]", chunk, re.S)
    if not m:
        return []
    return [int(x) for x in re.findall(rb"(\d+)\s+0\s+R", m.group(1))]


def merge_pdfs_raw(paths, out):
    """零依赖合并：只吃传统 xref 表（本工具产出的 PDF 都符合）。

    做法：按 xref 偏移精确切片每个对象 → 重新编号 → 只改字典部分的引用
    （stream 之后的二进制一个字节都不动）→ 新建 Pages 树。
    """
    srcs, kids, nxt = [], [], 4
    for path in paths:
        data = open(path, "rb").read()
        entries, trailer = _xref_parse(data)
        offsets = sorted(entries.values())
        m_root = re.search(rb"/Root\s+(\d+)\s+0\s+R", trailer)
        if not m_root:
            raise ValueError("%s：trailer 里没有 /Root" % os.path.basename(path))
        root = int(m_root.group(1))
        root_chunk = _obj_chunk(data, entries[root], offsets)
        m_pages = re.search(rb"/Pages\s+(\d+)\s+0\s+R", root_chunk)
        if not m_pages:
            raise ValueError("%s：Catalog 里没有 /Pages" % os.path.basename(path))
        pages = int(m_pages.group(1))
        m_info = re.search(rb"/Info\s+(\d+)\s+0\s+R", trailer)
        info = int(m_info.group(1)) if m_info else None

        mapping = {root: 1, pages: 2}
        if info is not None:
            mapping[info] = 3
        chunks = {}
        for num, off in entries.items():
            if num in (root, pages, info):
                continue
            mapping[num] = nxt
            chunks[num] = _obj_chunk(data, off, offsets)
            nxt += 1

        stack = _kids_of(_obj_chunk(data, entries[pages], offsets))   # 页对象顺序
        order = []
        while stack:
            k = stack.pop(0)
            if k not in entries:
                continue
            ch = _obj_chunk(data, entries[k], offsets)
            if re.search(rb"/Type\s*/Pages\b", ch):
                stack = _kids_of(ch) + stack
            else:
                order.append(k)
        if not order:
            raise ValueError("%s：找不到页面对象" % os.path.basename(path))
        srcs.append((mapping, chunks))
        kids += [mapping[k] for k in order]     # kids 必须用合并后的新对象号

    header = b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"     # 少了文件头 MuPDF 会进修复模式，
    parts, offs, pos = [header], {}, len(header)   # 去二进制里搜 "N 0 obj" 而切错流

    def emit(num, blob):
        nonlocal pos
        offs[num] = pos
        parts.append(blob)
        pos += len(blob)

    emit(1, b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n")
    emit(2, b"2 0 obj\n<< /Type /Pages /Count %d /Kids [%s] >>\nendobj\n"
         % (len(kids), b" ".join(b"%d 0 R" % k for k in kids)))
    emit(3, b"3 0 obj\n<< /Producer (slide2pdf merge) >>\nendobj\n")
    for mapping, chunks in srcs:
        for num in sorted(chunks, key=lambda z: mapping[z]):
            chunk = chunks[num]
            m = re.match(rb"\s*\d+\s+\d+\s+obj\b", chunk)
            rest = chunk[m.end():] if m else chunk
            rest = re.sub(rb"endobj\s*$", b"", rest).lstrip()
            emit(mapping[num], b"%d 0 obj\n" % mapping[num]
                 + _rewrite_refs(rest, mapping) + b"\nendobj\n")

    size = max(offs) + 1
    xref = [b"xref\n0 %d\n" % size, b"0000000000 65535 f \n"]
    for i in range(1, size):
        xref.append(b"%010d 00000 n \n" % offs[i] if i in offs
                    else b"0000000000 65535 f \n")
    tail = (b"trailer\n<< /Size %d /Root 1 0 R /Info 3 0 R >>\n"
            b"startxref\n%d\n%%%%EOF\n" % (size, pos))
    with open(out, "wb") as fh:
        fh.write(b"".join(parts) + b"".join(xref) + tail)
    return len(kids)


def merge_pdfs(paths, out, engine="auto"):
    """把若干 PDF 按顺序合成一个。engine: auto/pymupdf/raw。"""
    for p in paths:
        if not os.path.isfile(p):
            raise SystemExit("找不到 PDF：%s" % p)
    if engine in ("auto", "pymupdf"):
        import warnings
        warnings.filterwarnings("ignore", category=FutureWarning)   # fitz 的弃用提示
        try:
            import fitz
        except ImportError:
            if engine == "pymupdf":
                raise SystemExit("没有 pymupdf：pip install pymupdf")
            fitz = None
        if fitz is not None:
            doc = fitz.open()
            for p in paths:
                with fitz.open(p) as src:
                    doc.insert_pdf(src)
            doc.save(out, deflate=True)
            n = doc.page_count
            doc.close()
            return n
    try:
        return merge_pdfs_raw(paths, out)
    except (ValueError, OSError) as e:
        raise SystemExit("内置合并失败：%s\n提示：内置实现只支持传统 xref 表的 PDF；"
                         "可 pip install pymupdf 后去掉 --merge-raw 再试" % e)


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="把课件录屏视频还原成 PDF（静止换页检测 + 无损位图合成）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=EXAMPLES)
    ap.add_argument("video", nargs="?", help="输入视频（--merge 模式可省略）")
    ap.add_argument("-o", "--out",
                    help="输出 PDF：绝对/相对路径都行；只给文件名则放进 --outdir。"
                         "默认 = 视频同名 .pdf，就在视频所在文件夹")
    ap.add_argument("--outdir", metavar="DIR",
                    help="输出目录，默认 = 视频所在文件夹（不可写时退回当前目录）")

    g0 = ap.add_argument_group("视频参数扫描")
    g0.add_argument("--info", action="store_true",
                    help="只扫描并打印视频参数（分辨率/帧率/总帧数/时长/黑边），不出 PDF")
    g0.add_argument("--info-samples", type=int, default=3, metavar="K",
                    help="实测帧率时取几个窗口，默认 3")
    g0.add_argument("--json", action="store_true", help="配合 --info，用 JSON 输出")

    g = ap.add_argument_group("模式")
    g.add_argument("--every", type=int, metavar="N",
                   help="固定间隔模式：每 N 帧截一张（不做静止检测）")
    g.add_argument("--every-sec", type=float, metavar="S",
                   help="固定间隔模式：每 S 秒截一张")

    g2 = ap.add_argument_group("时间范围")
    g2.add_argument("--start", metavar="T",
                    help="从该时刻开始，如 00:19:00 / 19:00 / 1140")
    g2.add_argument("--end", metavar="T",
                    help="到该时刻结束，如 00:20:00 / 20:00 / 1200")

    g3 = ap.add_argument_group("静止检测参数（默认模式）")
    g3.add_argument("--scan-fps", "--fps", dest="scan_fps", type=float, default=2.0,
                    help="每秒扫描多少帧（指纹采样率），默认 2；越大越细、越慢")
    g3.add_argument("--thr", type=float, default=0.06,
                    help="判定'画面在变'的变化像素比例阈值，默认 0.06")
    g3.add_argument("--min-hold", type=float, default=1.5,
                    help="静止多少秒才算一页，默认 1.5")
    g3.add_argument("--mask-anim", type=float, default=0.35,
                    help="某格变化频率超过该比例则视为常动区域(动图/摄像头)，默认 0.35")
    g3.add_argument("--keep-motion", action="store_true",
                    help="动画段也取一帧（低置信度）")
    g3.add_argument("--keep-blank", action="store_true",
                    help="保留空白/黑屏页")
    g3.add_argument("--keep-wipe", action="store_true",
                    help="保留擦除/百叶窗等转场中间态（默认剔除：它们看着像内容不全/被白块盖住）")

    g4 = ap.add_argument_group("输出与杂项")
    g4.add_argument("--dedupe", dest="dedupe", action="store_true", default=None,
                    help="强制开启重复页剔除")
    g4.add_argument("--no-dedupe", dest="dedupe", action="store_false",
                    help="关闭去重（--every 模式默认关闭）")
    g4.add_argument("--max-frames", type=int, default=500,
                    help="固定间隔模式最多抽多少帧，默认 500（防误操作）")
    g4.add_argument("--dpi", type=float, default=150, help="PDF 页面 DPI，默认 150")
    g4.add_argument("--preview", help="输出一张选页拼图 jpg")
    g4.add_argument("--csv", help="输出页面时间对照表 csv")
    g4.add_argument("--doctor", action="store_true",
                    help="只做自检：列出找到的 ffmpeg/ffprobe 候选、体检结果和建议")
    g4.add_argument("--download-ffmpeg", dest="download", action="store_const",
                    const="force", default="auto",
                    help="找不到 ffmpeg 时直接下载静态版到 ./bin/auto/（不问）")
    g4.add_argument("--no-download", dest="download", action="store_const",
                    const="never", help="禁止自动下载（只给安装提示）")
    g4.add_argument("-y", "--yes", action="store_true", help="所有确认都回答 yes")
    g4.add_argument("--merge", nargs="+", metavar="PDF",
                    help="只合并 PDF（不处理视频）：--merge a.pdf b.pdf -o out.pdf")
    g4.add_argument("--merge-raw", action="store_true",
                    help="合并时用内置实现（不依赖 pymupdf；只支持传统 xref 的 PDF）")
    g4.add_argument("--dry-run", action="store_true", help="只分析，不抽帧不写 PDF")
    g4.add_argument("--extract-mode", choices=("auto", "select", "seek"),
                    default="auto",
                    help="抽帧方式：auto=先试 select 表达式，失败或候选过多自动改逐帧 seek；"
                         "select=只用表达式；seek=只用逐帧 seek（最稳，慢些）")
    g4.add_argument("--keep-temp", action="store_true", help="保留临时目录")
    g4.add_argument("--progress", dest="progress", action="store_true", default=None,
                    help="强制显示进度条（默认只在终端里显示）")
    g4.add_argument("--no-progress", dest="progress", action="store_false",
                    help="不显示进度条（用于写日志/后台运行）")
    g4.add_argument("--ffmpeg")
    g4.add_argument("--ffprobe")
    a = ap.parse_args(argv)
    global DOWNLOAD_POLICY, ASSUME_YES
    DOWNLOAD_POLICY = a.download
    ASSUME_YES = bool(getattr(a, "yes", False))
    if a.doctor:                                 # 只做自检，不需要视频
        doctor(a.ffmpeg, a.ffprobe)
        return 0
    if a.merge:                                  # 合并模式：与视频无关
        outdir = resolve_outdir(a.merge[0], a.outdir)
        out = check_out_path(pick_out(a.out, outdir, "merged.pdf"), "合并输出 PDF")
        n = merge_pdfs(a.merge, out, "raw" if a.merge_raw else "auto")
        log("已合并 %d 个 PDF → %s  %.1f MB  %d 页"
            % (len(a.merge), out, os.path.getsize(out) / 1e6, n))
        return 0
    if not a.video:
        raise SystemExit("用法：\n  slide2pdf.py <视频> [选项]\n"
                         "  slide2pdf.py --merge a.pdf b.pdf -o out.pdf")

    global PROGRESS
    PROGRESS = a.progress
    # 输出路径先体检：别等解码 8 分钟才因为目录写错失败
    outdir = resolve_outdir(a.video, a.outdir)
    stem = os.path.splitext(os.path.basename(a.video))[0]
    out = check_out_path(pick_out(a.out, outdir, stem + ".pdf"), "输出 PDF")
    if a.csv and not os.path.isabs(a.csv) and not os.path.dirname(a.csv):
        a.csv = os.path.join(outdir, a.csv)
    if a.preview and not os.path.isabs(a.preview) and not os.path.dirname(a.preview):
        a.preview = os.path.join(outdir, a.preview)
    if a.csv:
        a.csv = check_out_path(a.csv, "对照表 CSV", mkdir=True)
    if a.preview:
        a.preview = check_out_path(a.preview, "预览图 JPG", mkdir=True)
    t0 = time.time()
    ffmpeg = find_tool("ffmpeg", a.ffmpeg)
    ffprobe = find_tool("ffprobe", a.ffprobe, required=False)
    if not os.path.exists(a.video):
        raise SystemExit("找不到视频：%s" % a.video)

    if a.info:
        if not ffprobe:
            log("提示：没找到 ffprobe，改用 ffmpeg -i 解析元数据（实测帧率不可用）")
        vi = video_info(ffprobe, a.video, ffmpeg)
        meas = measure_fps(ffprobe, a.video, vi["dur"], max(1, a.info_samples))
        crop = detect_crop(ffmpeg, a.video, vi["dur"], vi["w"], vi["h"])
        vfr = abs(vi["r_fps"] - vi["avg_fps"]) > 0.01
        mfps = sorted(m["fps"] for m in meas) if meas else []
        vi.update(crop="%dx%d+%d+%d" % crop,
                  measured_fps=(mfps[len(mfps) // 2] if mfps else None),
                  vfr=bool(vfr), samples=meas)
        if a.json:
            print(json.dumps(vi, ensure_ascii=False, indent=2))
            return 0
        log("文件      %s  (%.1f MB)" % (os.path.basename(a.video), vi["size"] / 1e6))
        log("编码      %s  %s" % (vi["codec"], vi["pix_fmt"]))
        log("分辨率    %dx%d   课件区（自动去黑边）%s" % (vi["w"], vi["h"], vi["crop"]))
        log("帧率      标称 %.4g fps   平均 %.4g fps   %s"
            % (vi["r_fps"], vi["avg_fps"],
               "→ 可变帧率(VFR)" if vfr else "→ 恒定帧率(CFR)"))
        if mfps:
            log("实测      %.4g fps（%d 个窗口抽测：%s）"
                % (vi["measured_fps"], len(meas),
                   "，".join("%.1fs 起 %.1fs 内 %d 帧=%.3gfps"
                            % (m["t"], m["win"], m["n"], m["fps"]) for m in meas)))
        if vi["nb_frames"]:
            log("总帧数    %d（%d ÷ %.3f s = %.3f fps）"
                % (vi["nb_frames"], vi["nb_frames"], vi["dur"],
                   vi["nb_frames"] / vi["dur"] if vi["dur"] else 0))
        else:
            log("总帧数    未知（无 ffprobe；按时长×帧率估算 ≈ %d）"
                % int(vi["dur"] * (vi["measured_fps"] or vi["r_fps"])))
        log("时长      %s（%.3f s）" % (tc(vi["dur"]), vi["dur"]))
        log("码率      %.2f Mbps   time_base %s"
            % (vi["bit_rate"] / 1e6, vi["time_base"]))
        log("每帧间隔  %.4f ms" % (1000.0 / (vi["measured_fps"] or vi["r_fps"])))
        log("使用工具  ffmpeg=%s" % ffmpeg)
        log("          ffprobe=%s" % ffprobe)
        return 0

    info = probe(ffprobe, a.video) if ffprobe else probe_ffmpeg(ffmpeg, a.video)
    start = parse_time(a.start) or 0.0
    end = parse_time(a.end)
    if end is None:
        end = info["dur"]
    if start < 0 or start >= info["dur"]:
        raise SystemExit("--start 超出视频长度（%.1f s）" % info["dur"])
    end = min(end, info["dur"])
    if end <= start:
        raise SystemExit("--end 必须大于 --start")
    dur = end - start
    log("视频 %dx%d  %.3g fps  %s (%.1f min)"
        % (info["w"], info["h"], info["fps"], tc(info["dur"]), info["dur"] / 60))
    if start > 0 or end < info["dur"]:
        log("处理区间：%s → %s（%.1f min）" % (tc(start), tc(end), dur / 60))
    crop = detect_crop(ffmpeg, a.video, dur, info["w"], info["h"], start)
    log("自动黑边裁剪：%dx%d+%d+%d（原图 %dx%d）"
        % (crop[0], crop[1], crop[2], crop[3], info["w"], info["h"]))
    step_mode = bool(a.every or a.every_sec)
    dedupe_on = a.dedupe if a.dedupe is not None else (not step_mode)

    tmp = tempfile.mkdtemp(prefix="slide2pdf-")
    try:
        if step_mode:
            step = int(a.every) if a.every else max(1, int(round(info["fps"] * a.every_sec)))
            n_est = int(dur * info["fps"] / step) + 1
            log("[1/2] 固定间隔模式：每 %d 帧（%.2f s）一张，预计 %d 帧"
                % (step, step / info["fps"], n_est))
            if a.dry_run:
                log("  dry-run：预计 %d 帧（--max-frames 上限 %d）"
                    % (n_est, a.max_frames))
                return 0
            if n_est > a.max_frames:
                raise SystemExit(
                    "预计 %d 帧超过 --max-frames %d，请加大间隔或调高 --max-frames"
                    % (n_est, a.max_frames))
            pngs, S = extract(ffmpeg, a.video, crop, None, None, tmp,
                              start=start, dur=dur, step=step,
                              max_frames=a.max_frames, total=min(n_est, a.max_frames),
                              mode=a.extract_mode)
            cands = [dict(t=start + i * step / info["fps"], dwell=step / info["fps"],
                          ink=float((np.asarray(S[i]) < 200).mean()) * 100,
                          conf="step", blank_risk=False)
                     for i in range(len(pngs))]
        else:
            log("[1/3] 指纹扫描：%d x %d @ %g fps（每秒 %g 帧）..."
                % (GW, GH, float(a.scan_fps), float(a.scan_fps)))
            a.scan_fps = float(a.scan_fps)
            A, raw = fingerprint(ffmpeg, a.video, crop, a.scan_fps, tmp,
                                 start=start, dur=dur)
            cands, dropped, st = analyse(A, float(a.scan_fps), a.thr, a.min_hold,
                                         a.mask_anim, a.keep_motion,
                                         not a.keep_blank)
            for c in cands:
                c["t"] += start
            for d in dropped:
                d["s"] += start
                d["e"] += start
            log("[2/3] 采样 %d 帧 → 变化区间 %d 个 → 静止段 %d 个 → 页面候选 %d 页"
                % (st["frames"], st["blocks"], st["runs"], len(cands)))
            if a.dry_run:
                for c in cands:
                    log("  %s  dwell=%5.1fs ink=%5.1f%% %s%s"
                        % (tc(c["t"]), c["dwell"], c["ink"], c["conf"],
                           "  (空白风险)" if c["blank_risk"] else ""))
                for d in dropped:
                    log("  丢弃 %s..%s %s" % (tc(d["s"]), tc(d["e"]), d["why"]))
                return 0
            pngs, S = extract(ffmpeg, a.video, crop, a.scan_fps,
                              [c["frame"] for c in cands], tmp,
                              start=start, dur=dur, total=len(cands),
                              mode=a.extract_mode)
            if len(pngs) != len(cands):
                log("警告：抽到 %d 帧，候选 %d 页（可能有一帧落在片尾之外）"
                    % (len(pngs), len(cands)))

        n0 = len(pngs)
        keep = list(range(n0)) if not dedupe_on else dedupe(S, cands, True)

        # 转场中间态安全网：去重之后再剔（不改变去重的比较顺序，结果才稳定）
        if not a.keep_wipe:
            kept = []
            for k in keep:
                vc, hc = wipe_metrics(np.asarray(S[k]))
                if vc > 0.45:                 # 整条竖向白带 = 擦除/推入的半途
                    log("  丢弃 %s 疑似转场中间态（竖白带 %.0f%%）"
                        % (tc(cands[k]["t"]), vc * 100))
                    continue
                if hc > 0.55:                 # 只有横白带：可能只是"标题栏+空白"的首帧
                    cands[k]["blank_risk"] = True
                    log("  提示 %s 横向空白较多（%.0f%%），已标记空白风险"
                        % (tc(cands[k]["t"]), hc * 100))
                kept.append(k)
            keep = kept
        pages = [pngs[i] for i in keep]
        log("[%s] 页数：%d%s" % ("2/2" if step_mode else "3/3", len(pages),
                                 "" if not dedupe_on else "（去重前 %d）" % n0))
        build_pdf(pages, out, a.dpi, os.path.basename(a.video))
        log("已写出 %s  %.1f MB  %d 页  用时 %.0fs"
            % (out, os.path.getsize(out) / 1e6, len(pages), time.time() - t0))

        if a.csv:
            with open(a.csv, "w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(["page", "t_sec", "timecode", "dwell_s", "ink_pct",
                            "confidence", "blank_risk"])
                for n, i in enumerate(keep, 1):
                    c = cands[i]
                    w.writerow([n, "%.1f" % c["t"], tc(c["t"]),
                                "%.1f" % c["dwell"], "%.1f" % c["ink"],
                                c["conf"], int(c.get("blank_risk", False))])
            log("页面时间对照：%s" % a.csv)

        if a.preview:
            cols = 7
            rows = (len(pages) + cols - 1) // cols
            lst = os.path.join(tmp, "list.txt")
            with open(lst, "w") as fh:
                for q in pages:
                    fh.write("file '%s'\n" % q.replace("\\", "/").replace("'", "'\\''"))
            run([ffmpeg, "-hide_banner", "-v", "error", "-f", "concat",
                 "-safe", "0", "-i", lst, "-vf",
                 "scale=300:-1,tile=%dx%d:padding=2:margin=2" % (cols, rows),
                 "-frames:v", "1", "-q:v", "3", "-y", a.preview])
            log("选页拼图：%s" % a.preview)
        return 0
    finally:
        if not a.keep_temp:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
