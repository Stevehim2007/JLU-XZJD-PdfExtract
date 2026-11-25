# JLU-XZJD-PdfExtract

> 把"每页课件停留几秒"的录屏视频，还原成**像素级无损**的位图 PDF。
> Turn a slide-show screen recording into a pixel-lossless, page-per-slide PDF.

**学在吉大（XZJD）课件录屏 → PDF 提取工具**。单文件、只依赖 `ffmpeg` + `numpy`，
面向"整段视频就是幻灯片在翻页"这类课程录屏（PPT 放映录制、直播回放、屏幕录制都算）。

---

## 它解决什么问题

课件录屏的本质是：**每页幻灯片静止停留几秒 → 切换 → 下一页静止几秒**。
所以只要能准确找出"哪几段是静止的、每段取哪一帧"，就能把视频完整还原成 PDF——而且
**不需要重新编码**：抽出来的 PNG 被原样塞进 PDF（PNG 的 IDAT 直通 + `/Predictor 15`），
像素与视频帧逐字节一致（实测 `maxdelta = 0`）。

这点和绝大多数"视频转幻灯片"工具不同：它们要么重新压缩一遍 JPEG，要么走 OCR 重新排版，
都会损失像素或版式。本项目**只搬运像素，不重画内容**。

## 实测效果

| 输入 | 输出 |
|---|---|
| 103 分 29 秒、1920×1080、30 fps、1.16 Mbps、4:3 幻灯片信箱在 16:9 画布里 | **52 页 / 20.7 MB**（人工核对版）<br>**49 页 / 16.7 MB**（全自动版） |
| 处理耗时 | 全片两遍解码，约 8–10 分钟（普通笔记本） |
| 单页 | 1440×1080 px，150 dpi → 691.2 × 518.4 pt，内容与源帧**逐字节相同** |
| 附带产物 | CSV 页码 ↔ 秒 ↔ 时间码 ↔ 停留时长 ↔ 墨量 ↔ 置信度；7 列缩略拼图（人工验收用） |

## 特性

- **零重编码 PDF**：PNG 的 IDAT 直接作为 PDF 图像 XObject（`/FlateDecode` + `/Predictor 15`），
  不经过任何二次压缩；用 PyMuPDF 以 150 dpi 渲染回来与源 PNG 逐字节比对 `maxdelta = 0`。
- **自动裁掉黑边/信箱**：`cropdetect` 在多个时间点取样取众数（本例自动认出 `1440x1080+240+0`）。
- **静止换页检测**（默认模式）：一次解码出 80×60@N fps 指纹 → 逐格变化频率 → 屏蔽常动区域
  （鼠标、摄像头小窗、真动图）→ 变化段之间的"静止段"才是候选页。
- **build 合并**：同一页的分步动画（PPT 一条条出 bullet）会被识别为"同页补内容"，
  保留**最完整**的那一态，而不是把半成品当新页。
- **转场中间态剔除**：擦除/推入/百叶窗/淡出到白 这类动画停在半途时，画面会留下一条纯白带；
  竖向白带 > 45% 直接丢弃，横向白带 > 55% 只提示（"标题栏 + 大片空白"的正常页也长这样，
  宁可让人复核也不误删）。
- **空白/黑屏段自动丢弃**：`ink < 0.6%` 或 `> 99%` 的段不是课件页（录像开头结尾、无信号画面）。
- **两种模式**：静止换页检测（默认，自动分页）/ 固定间隔截帧（`--every` / `--every-sec`）。
- **单行实时进度条**：中文按双宽计算、宽度自适应、放不下按固定优先级降级，绝不折行。
- **抽帧双通道**：`select` 表达式（快）/ 逐帧 `seek`（稳）；表达式过长或滤镜图初始化失败时**自动兜底**。
- **PDF 合并**：`--merge`，优先用 PyMuPDF，没有就用手写的零依赖实现（自己解析 xref、重编号对象）。
- **工程细节**：`--outdir`（默认视频所在目录，自动建目录，路径写错 0.1 秒内报错并提示正确拼写）、
  `--dry-run`、`--preview`、`--csv`、`--keep-temp`、Windows 便携 ffmpeg 自动发现（PE/ELF 双体检 + 冒烟测试）。

## 快速开始

### 1. 装 ffmpeg

```bash
# macOS
brew install ffmpeg
# Ubuntu / Debian
sudo apt install ffmpeg
# Windows（任选一种）
winget install Gyan.FFmpeg
#   或下载 https://www.gyan.dev/ffmpeg/builds/ 的 essentials build，
#   把 ffmpeg.exe / ffprobe.exe 放到脚本同目录，或 bin\ 下的任意子目录（会被自动发现）
```

### 2. 装 Python 依赖

```bash
python -m venv venv
source venv/bin/activate          # Windows: venv\Scripts\activate
pip install -r requirements.txt   # 只需要 numpy
pip install pymupdf               # 可选：让 --merge 走快路径
```

### 3. 跑

```bash
python slide2pdf.py 录屏.mp4
# → 录屏.pdf，默认写在视频同目录
```

先看看视频参数是否正常（帧率/总帧数/黑边）：

```bash
python slide2pdf.py 录屏.mp4 --info
```

## 常用命令

```bash
# 全自动：检测换页 → 出一份 PDF + 时间码表 + 缩略拼图
python slide2pdf.py lec.mp4 --csv pages.csv --preview preview.jpg

# 先干跑，只看它打算选哪几页（不抽帧，几秒出结果）
python slide2pdf.py lec.mp4 --dry-run

# 只处理一段（大文件先试跑）
python slide2pdf.py lec.mp4 --start 00:17:00 --end 00:25:00 -o part.pdf

# 固定间隔截帧：每 30 帧一张（= 1 秒一张，视频 30 fps）
python slide2pdf.py lec.mp4 --every 30

# 每 20 秒一张
python slide2pdf.py lec.mp4 --every-sec 20

# 输出到指定目录（不存在会自动创建）
python slide2pdf.py lec.mp4 --outdir out/ -o 课件.pdf

# 合并两个 PDF
python slide2pdf.py --merge a.pdf b.pdf -o merged.pdf

# 抽帧老失败？直接用最稳的逐帧 seek
python slide2pdf.py lec.mp4 --extract-mode seek

# 想把"看起来空白"的页也留下
python slide2pdf.py lec.mp4 --keep-blank --keep-wipe
```

完整的参数表、判读要点、FAQ 见 **[docs/使用说明书.md](docs/使用说明书.md)**（约 440 行）。

## 工作原理

```
probe()        ffprobe 取 宽高 / 帧率 / 总帧数 / 时长（没有 ffprobe 也能回退解析 ffmpeg -i）
detect_crop()  多时间点 cropdetect 取众数 → 幻灯片区域（含黑边/信箱）
fingerprint()  一次解码：crop → 80×60 灰度 → N fps 指纹（默认 2 fps）
analyse()      逐格变化频率 → mask 常动格 → 变化段 → 静止段 → 候选页（含 build 合并、空白过滤）
extract()      select 表达式一次抽出所有候选帧（失败/过长自动改逐帧 seek）
              同时输出 1440×1080 PNG 和 480×360 灰度图（去重、白带统计用后者）
dedupe()       保守去重：重复页丢弃；"同页补内容"保留最完整帧
wipe filter    竖白带 > 45% 的转场中间态丢弃，横白带 > 55% 只提示
build_pdf()    PNG 的 IDAT 直通写进 PDF（零重编码），/Info + 页序按时间
```

判断"这一帧有没有变"用的是**像素占比**而不是整帧哈希：`|Δ| > 20` 的像素占比超过阈值
才算"在变"。这样对 H.264 的 ±1 噪点免疫（实测同一页相邻帧 PSNR 88–93 dB，甚至 12/199 对逐位相同，
但亮度通道 `YAVG = YMAX = 0`，只有极少数色度样本差 1）。

## 跟其他项目比

| 项目 | 语言/依赖 | 取帧思路 | 去重 | 输出 | 和本项目的差别 |
|---|---|---|---|---|---|
| **本项目** | Python，`numpy` + ffmpeg | 变化检测 + 静止段 + build 合并 + 转场白带剔除 | 像素占比（chg/loss/add 三指标） | **零重编码位图 PDF** | — |
| [szanni/slideextract](https://github.com/szanni/slideextract) | C | 逐帧差阈值，靠人工框"比较区域"（如页码）避开动画 | 取每段**最后一帧**，之后人工删 | ImageMagick 拼 PDF | 要人工指定区域；无 PDF 内建 |
| [patrickmineault/vid2slides](https://github.com/patrickmineault/vid2slides) | Python/conda，仅 Unix | ffmpeg 抽缩略图 + Tesseract OCR | 按 OCR 文本 | 文字可选 PDF + 章节 + GIF | 重 OCR 排版，**丢像素保真** |
| [bit-admin/AutoSlides-Extractor](https://github.com/bit-admin/AutoSlides-Extractor) | C++/Qt6/OpenCV/ONNX | pHash + MobileNetV4 判"是不是幻灯片" | pHash + GUI 复核 | PDF | 依赖重、要 GUI |
| [SMUsamaShah/video2pdf](https://github.com/SMUsamaShah/video2pdf) | Python 单脚本 | 定时抽帧 | 无 | PDF | 不区分静止/转场 |

一句话：**要"还原原样"选本项目，要"文字可搜索"选 vid2slides**。

## 已知局限

- **位图 PDF，文字不可选/不可搜索**：要文字层得再接一层 OCR（Tesseract/PaddleOCR）。
- **视频的压缩噪声已经烧进像素**：H.264 的振铃、色度抽样误差会留在页面上（视觉上几乎无感）。
- **一页多个动画态只保留一个**：PPT 的分步建造只留最完整那一态；被"擦除/白色遮罩"盖住的答案不会还原。
- **段落级判断，不是语义判断**：没有 OCR 就没有真正的"这一页是不是同一页"的判断，
  所以去重是保守的（宁可多留一页，也不误删），页数会在不同版本间浮动 ±2~3。
- **面向"幻灯片为主"的视频**：真人讲课画面靠 `--mask-anim` 屏蔽变化格，效果取决于画面布局。
- **必须能解码**：DRM/加密视频、损坏文件不在范围内。

## 目录

```
slide2pdf.py            单文件主程序（1500 行，无第三方依赖除 numpy）
requirements.txt        numpy（+ 可选 PyMuPDF）
docs/使用说明书.md       详细说明书：参数、判读、FAQ、内部流水线
```

## 常见问题（摘要）

**Q: 抽出的页里有"内容不全 / 被白块盖住"的？**
那是转场中间态（擦除/推入/百叶窗停在一半）。工具已按"竖白带 > 45% 丢弃"处理；
想全留下加 `--keep-wipe`。

**Q: 有空白页？**
视频里真有（录像开头结尾、切到桌面、无信号画面）。`ink < 0.6%` 的段默认丢弃；
想保留加 `--keep-blank`。

**Q: 抽帧报 `Error initializing filters` / `Cannot allocate memory`？**
候选页太多时 `select` 表达式过长，某些 ffmpeg 构建会 ENOMEM。工具会自动改用逐帧 seek；
也可以直接 `--extract-mode seek`。

**Q: 页数和我预期不一样？**
用 `--preview preview.jpg` 看缩略拼图，或 `--dry-run` 看它打算选哪几页、丢了哪些（含原因）。

## 许可

未指定许可证。如需开源发布请自行补充 `LICENSE`（作者可按需选择 MIT / Apache-2.0 等）。
