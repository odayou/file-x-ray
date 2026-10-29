# 文件索引与管理工具（File X-Ray）

> 项目仓库：https://gitee.com/odayou/file-x-ray

一个跑在你**本机**的工具：递归扫描某个根目录建索引，给每个文件加**备注**、**状态**和**主题标签**；对**语音 / 视频可一键转写成文字稿**；对**图片可 OCR 识别文字、对 PDF 可抽取内部文字**；按文件名 / 备注 / 内容 / 标签 / 转写文字 / 图中文字 / 类型全文搜索；快速预览图片·视频·音频·PDF·文本；勾选后**按原目录结构打包**（带走备注、标签、转写稿、OCR 文字与 PDF 正文）；每次搜索可存为**任务清单**并随时重试。

> 为什么 FastAPI 能操作你的文件？因为后端进程就运行在你的电脑上，拥有完整的系统文件权限；浏览器只负责界面和发请求，真正的扫描 / 读取 / 转写 / OCR / 打开文件夹 / 打包都在本地后端完成。

## 运行方式（源码）

需要 Python 3.9+。

```bash
pip install -r requirements.txt
# requirements.txt 已含：fastapi uvicorn faster-whisper pymupdf pytesseract pillow
```

**额外系统依赖（OCR 用）**：
- **推荐（中文识别更强）**：安装 [Umi-OCR](https://github.com/hiroi-sora/Umi-OCR)，启动其「HTTP 接口」后本工具会自动优先调用它。
- **轻量兜底**：安装系统 `tesseract`（含中文包 `chi_sim`）。
  - Windows：`pip install tesseract` 不行，需到 UB-Mannheim 下载安装，并把 `tesseract.exe` 所在目录加入 PATH；中文包随安装勾选或单独下载 `chi_sim.traineddata` 放到 tessdata 目录。
  - Debian/Ubuntu：`sudo apt install tesseract-ocr tesseract-ocr-chi-sim`
  - macOS：`brew install tesseract tesseract-lang`

启动：

```bash
cd file-index-tool
python backend.py
```

浏览器打开 **http://127.0.0.1:8000** 即可。默认监听本机回环地址，只有你自己能访问。

### 可选环境变量

| 变量 | 默认值 | 说明 |
|---|---|---|
| `FIT_DB` | `file_index.db`（打包后为 `~/file_index_data/file_index.db`） | 数据库文件路径 |
| `FIT_HOST` / `FIT_PORT` | `127.0.0.1` / `8000` | 监听地址与端口 |
| `FIT_WHISPER_MODEL` | `base` | 转写模型：`tiny`/`base`/`small`/`medium`/`large-v3`（越大越准越慢） |
| `FIT_WHISPER_LANG` | 空（自动检测） | 固定语种可填 `zh` / `en` 等，提升准确率 |
| `FIT_TRANSCRIBE_MAX_MB` | `300` | 超过此大小的音视频文件跳过转写，避免卡死 |
| `FIT_OCR_ENGINE` | `auto` | OCR 引擎：`auto`（优先 Umi-OCR，失败回退 tesseract）/ `umi` / `tesseract` |
| `FIT_UMI_OCR_URL` | `http://127.0.0.1:1224` | Umi-OCR 的 HTTP 接口地址 |
| `FIT_OCR_LANGS` | `chi_sim+eng` | tesseract 语言（多语言用 `+` 连接） |
| `FIT_OCR_MAX_MB` | `30` | 超过此大小的图片跳过 OCR |
| `PDF_TEXT_LIMIT` | `200000` | 单个 PDF 抽取文字上限（字符） |

## 界面与功能

```
顶栏   根目录路径 + 扫描层级 + [扫描建索引] + 统计（总数/已打标签/已转写/已识别图片/各状态数量）
工具栏 搜索框 + 状态过滤 + 类型过滤 + 当前位置面包屑 + 标签筛选 + [管理状态] + [任务清单] + 已选/[转写选中]/[识别选中图片]/[导出选中]
左栏   文件夹树（可按显示层级折叠）｜ 主题标签云（点击即按标签筛选）
中栏   文件清单（全选本页、逐条勾选、状态徽标、备注/标签摘要、分页）
右栏   详情：预览 + 状态 + 主题标签(增删) + 备注编辑
         · 音频/视频：🎙 转写此文件（生成可搜索文字稿）
         · 图片：🖼 识别图中文字（OCR，支持中文）
         · PDF：自动抽取内部文字并显示（可复制、可搜索）
         + 打开所在文件夹 + 加入/移出选择
```

### 功能对照

| 诉求 | 实现 |
|---|---|
| 递归扫描建索引 | 后端 `os.walk` 递归入库，重扫不会覆盖已有备注/状态/标签/转写稿/OCR 结果 |
| 层级可设置 | 扫描时填「扫描最大层级」控下钻深度；左栏「显示层级」控树折叠 |
| 每个文件备注 + 状态 + 主题标签 | 右栏编辑 `note` / `status` / `tags`；标签可多值，对应"按主题查" |
| **可自定义状态** | 工具栏「⚙ 管理状态」可增删状态并配色，持久化到数据库 |
| **语音 / 视频转写** | 右栏「🎙 转写此文件」或工具栏「🎙 转写选中」；基于 faster-whisper |
| **图片 OCR 文字识别** | 右栏「🖼 识别图中文字」或工具栏「🖼 识别选中图片」；Umi-OCR 优先、tesseract 兜底 |
| **PDF 文字提取** | 扫描时自动抽取 PDF 内部文字写入索引，右栏可直接查看/复制，可被搜索 |
| 按文字/标签/转写/图中文字搜索 | 同时匹配 `name / note / content / tags / transcript / ext`，可叠加状态、类型、目录、标签过滤 |
| 快速预览 | 图片/视频/音频用原生播放器，PDF 内嵌阅读器，文本直接显示正文 |
| 打开所在文件夹 | 后端调系统命令：Windows `explorer /select,"路径"`、macOS `open -R`、Linux `xdg-open` |
| 全选/勾选 → 按原结构打包 + 带走全部元数据 | 导出 zip 保留 `rel_path` 目录树；根目录附 `manifest.json/csv`（含备注/标签/转写稿/OCR 文字/PDF 正文）；可选为每个文件生成 `.文件名.note.json` 副文件 |
| 搜索任务清单 + 重试 | 每次搜索自动入库为任务，右侧面板可看结果数、一键重试或应用 |

## 图片 OCR 与 PDF 文字提取

### 图片 OCR（图中文字 → 可搜索）
- **引擎优先级**：默认 `auto`——先尝试本机已启动的 **Umi-OCR HTTP 接口**（中文识别更强、开箱即用），不可用时自动回退到本地 **tesseract**。可用 `FIT_OCR_ENGINE=umi` / `tesseract` 强制指定。
- **Umi-OCR 接入**：下载运行 [Umi-OCR](https://github.com/hiroi-sora/Umi-OCR)，在「全局设置 → 开启 HTTP 服务」后，本工具即可通过 `http://127.0.0.1:1224/api/ocr` 调用；语言由 `FIT_OCR_LANGS` 映射（如 `chi_sim+eng` → `ch_and_en`）。
- **tesseract 兜底**：需系统安装 tesseract 及 `chi_sim`（中文）语言包（见上方安装说明）。
- **触发**：右栏选中图片点「🖼 识别图中文字」，或勾选多个后点工具栏「🖼 识别选中图片」批量识别；非图片自动跳过。
- 识别结果写入索引中的 `transcript` 字段，之后按图中文字即可搜到该图片；导出时一并带走。

### PDF 文字提取（内部文字 → 可搜索）
- 扫描时**自动**用 PyMuPDF（回退 pypdf / pdfminer）抽取 PDF 文字层，写入索引的 `content` 字段，无需手动操作。
- 右栏对 PDF 直接展示「PDF 文字内容」可读/可复制；搜索时按正文命中。
- 纯图片型扫描件（无文字层）不会被抽出文字——此类 PDF 可先转成图片再用上方 OCR 处理，或后续接入扫描件 OCR。

## 打包成 Windows 免安装 exe（双击即用）

本仓库已内置 PyInstaller 配置，在 **Windows** 上执行：

```bat
pip install pyinstaller
pyinstaller build.spec
```

生成的 `dist\文件索引工具.exe` 双击即可运行——它会自动启动服务并打开浏览器。数据库默认放在用户目录 `~/file_index_data/file_index.db`，不会因程序目录只读而报错。

> 说明：PyInstaller 不支持跨平台编译，需在与目标系统相同的系统上打包（在 Windows 上打 Windows 包）。本沙箱为 Linux，无法直接产出 exe，故只提供配置与步骤。

## 已知范围与扩展建议

- **转写 / OCR 是按需触发**，不在扫描时自动进行（避免一次性处理大量长音频/大图卡住）。
- 单个文件内容索引上限 20 万字符、大于 5MB 的文本文件不索引内容（可在 `backend.py` 调整 `CONTENT_INDEX_LIMIT` / `FILE_READ_LIMIT`）。
- 搜索结果列表单次最多返回 500 条用于展示，统计数字是全量匹配数。
- 数据库为单个 SQLite 文件，可随时删除后重新扫描。

## 文件结构

```
file-index-tool/
├── backend.py        # FastAPI 后端：扫描/搜索/预览/打开文件夹/打包 + 转写(faster-whisper) + OCR(Umi-OCR/tesseract) + PDF 提取
├── index.html        # 前端单页（内联 CSS/JS，无需构建）
├── requirements.txt  # Python 依赖
├── build.spec        # PyInstaller 打包配置（Windows exe）
├── file_index.db     # 首次运行自动生成（已加入 .gitignore）
└── README.md
```
