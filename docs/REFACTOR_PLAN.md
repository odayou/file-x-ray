# File X-Ray 改造交接说明（REFACTOR_PLAN）

> 面向后续执行者：本文自包含，未读过上一轮对话也能直接执行。
> 结论来源：上一轮对 `D:\pythonCode\file-x_ray` 与 `D:\AiCode\agentSessions` 的逐文件架构对比分析（两个项目均已实际读代码核对，非按目录名推测）。

---

## 1. 背景与目标

用户最初的诉求是：**`file-x_ray` 是否可以（以及是否应该）整体套用 `D:\AiCode\agentSessions` 的架构**。起因是 agentSessions 在运行模式、打包、发布等工程化环节看起来更成熟。

最终结论：

- **不整体套用**。整体套用等于换语言重写（详见第 3 节前提纠正），收益集中在"分发与工程纪律"，代价是全部业务回归。
- **只借鉴工程化模式**。真正值得抄的是五条主线（第 4 节），落地为 P1–P11 的优先级清单（第 5 节）。
- 最值得先做的前三条（P1/P2/P3）合计不到 2 天，且都不改动现有表结构。

本次分析为只读：**只读取了代码与配置，未修改任一项目的任何一行代码**，也未修改 agentSessions 的任何文件。后续执行时同样**不得修改 `D:\AiCode\agentSessions`**（它只作为参考样本）。

目标产物：对 `file-x_ray` 做增量式工程化改造，**不改变现有用户可见行为、不破坏已有标注数据**。

---

## 2. 当前状态

| 项 | 状态 |
|---|---|
| 架构对比分析 | 已完成（本文即结论沉淀） |
| 代码改动 | **零**。`backend.py` / `index.html` / `web/` / `build.spec` / `.github/workflows/build.yml` 均未改动 |
| 数据库 | 未触碰。仓库根目录现有 `file_index.db`（约 27MB），结构见 [backend.py init_db](file:///d:/pythonCode/file-x_ray/backend.py#L342-L446) |
| 测试基线 | 无自动化测试（两个项目均无）；验证方式为点测（见第 10 节） |

---

## 3. 关键前提纠正（必须先读）

1. **`agentSessions` 不是 Python 项目。** 它是 **Node.js + Tauri**：
   - 核心 `src/` + `adapters/` 共约 5264 行 JS，见 [package.json](file:///D:/AiCode/agentSessions/package.json#L1-L29)；
   - 本地 REST 桥 [server.js](file:///D:/AiCode/agentSessions/server.js#L1-L30)（Node 原生 http，绑 127.0.0.1:18778）；
   - 前端 React + TypeScript + Vite（[ui/package.json](file:///D:/AiCode/agentSessions/ui/package.json)）；
   - 桌面壳 Tauri 2 + Rust（[tauri.conf.json](file:///D:/AiCode/agentSessions/src-tauri/tauri.conf.json#L6-L41)、[lib.rs](file:///D:/AiCode/agentSessions/src-tauri/src/lib.rs#L33-L102)）；
   - 存储 better-sqlite3（原生模块）；
   - 仓库内**没有任何生产级 Python 代码**，只有 3 个 `_probe*.py` 临时探针脚本。
2. **`file-x_ray` 是 Python 单体服务**：FastAPI + uvicorn，全部后端在一个文件 [backend.py](file:///d:/pythonCode/file-x_ray/backend.py)（2629 行），前端 [index.html](file:///d:/pythonCode/file-x_ray/index.html)（430 行内联）+ `web/*.js` 无构建脚本，打包 PyInstaller（[build.spec](file:///d:/pythonCode/file-x_ray/build.spec#L9-L27)）。
3. **"完全套用"= 换语言重写。** 需要重写 2629 行后端 + 430 行 HTML + 约 948 行 web JS。
4. **file-x_ray 的核心能力在 Node 侧没有等价成熟替代**：
   - `faster-whisper / ctranslate2`（音视频转写，[backend.py L2207-L2269](file:///d:/pythonCode/file-x_ray/backend.py#L2207-L2269)）；
   - `PyMuPDF`（PDF 文字层抽取 [L565](file:///d:/pythonCode/file-x_ray/backend.py#L565)、逐页渲染预览 `/api/pdf/{fid}/page/{num}` [L1729](file:///d:/pythonCode/file-x_ray/backend.py#L1729)）；
   - `pytesseract` + Umi-OCR HTTP（图片 OCR，[L600-L732](file:///d:/pythonCode/file-x_ray/backend.py#L600-L732)）；
   - `watchdog`（目录实时感知，[L1999-L2204](file:///d:/pythonCode/file-x_ray/backend.py#L1999-L2204)）；
   - stdlib `sqlite3` + FTS5（[L159-L171](file:///d:/pythonCode/file-x_ray/backend.py#L159-L171)、[L451-L488](file:///d:/pythonCode/file-x_ray/backend.py#L451-L488)）。
   Node 侧 `tesseract.js` 明显更慢、`pdfjs` 抽取能力弱于 PyMuPDF、无成熟 ctranslate2 绑定 —— 强行迁移会导致能力降级或演变成"Node 前端 + Python 抽取服务"的双语言双进程，维护成本最高。

**因此改造方向固定为：保持 Python 单体，借工程化模式。**

---

## 4. 五条值得借鉴的主线

| 主线 | agentSessions 的做法（证据） | file-x_ray 现状缺口 |
|---|---|---|
| **A. 并发安全的存储姿势** | 打开库即设 WAL + 外键（[model.js L145-L154](file:///D:/AiCode/agentSessions/src/model.js#L145-L154)）；写多行用事务（[store.js L65-L77](file:///D:/AiCode/agentSessions/src/store.js#L65-L77)）；幂等 upsert（[store.js L30-L52](file:///D:/AiCode/agentSessions/src/store.js#L30-L52)） | [get_db](file:///d:/pythonCode/file-x_ray/backend.py#L159-L171) 只 `sqlite3.connect`，**无任何 PRAGMA**（已全局 grep 验证）；扫描线程频繁 commit，与读接口并发访问同一库 |
| **B. 配置化** | `~/.agentsessions/config.json`，白名单键 + 归一化 + 每 tick 热重载（[config.js L35-L82](file:///D:/AiCode/agentSessions/src/config.js#L35-L82)、[server.js L272-L276](file:///D:/AiCode/agentSessions/server.js#L272-L276)）；排除规则（[config.js L113-L127](file:///D:/AiCode/agentSessions/src/config.js#L113-L127)） | 只有环境变量 `FIT_*`（[L79-L154](file:///d:/pythonCode/file-x_ray/backend.py#L79-L154)），双击 exe 的用户无法配置；且**没有任何排除/忽略机制** |
| **C. 失败可见性** | 单适配器/单库失败只降级并写入 `stats.agents[].status='error'`（[scan.js L115-L159](file:///D:/AiCode/agentSessions/src/scan.js#L115-L159)）；被大文件阈值跳过的文件收集上报（[scan.js L77-L93](file:///D:/AiCode/agentSessions/src/scan.js#L77-L93)）；后端 stdout/stderr 落盘日志（[lib.rs L86-L100](file:///D:/AiCode/agentSessions/src-tauri/src/lib.rs#L86-L100)） | 72 处 `except Exception`，其中多处静默 `pass`（[L875-876](file:///d:/pythonCode/file-x_ray/backend.py#L875-L876)、[L883-884](file:///d:/pythonCode/file-x_ray/backend.py#L883-L884)、[L913-914](file:///d:/pythonCode/file-x_ray/backend.py#L913-L914)、[L918-919](file:///d:/pythonCode/file-x_ray/backend.py#L918-L919)）；仅 8 处 `print`，打包后 `console=True` 关窗即丢日志 |
| **D. 任务可续** | 增量指纹持久化到 `scan_state` 表（[model.js L82-L89](file:///D:/AiCode/agentSessions/src/model.js#L82-L89)、[store.js L80-L92](file:///D:/AiCode/agentSessions/src/store.js#L80-L92)） | 进度全在内存全局字典：[scan_state L843](file:///d:/pythonCode/file-x_ray/backend.py#L843)、[transcribe_jobs L2270](file:///d:/pythonCode/file-x_ray/backend.py#L2270)、[ocr_jobs L2436](file:///d:/pythonCode/file-x_ray/backend.py#L2436)；重启即丢；[start_scan](file:///d:/pythonCode/file-x_ray/backend.py#L1436-L1473) 无同根并发护栏 |
| **E. 分发纪律** | onedir 风格自包含载荷（[pack-backend.js L112-L147](file:///D:/AiCode/agentSessions/scripts/pack-backend.js#L112-L147)）+ 安装包矩阵 + draft release 全绿才转正（[build.yml L96-L217](file:///D:/AiCode/agentSessions/.github/workflows/build.yml#L186-L217)） | onefile、`console=True`、无图标无版本信息（[build.spec L31-L46](file:///d:/pythonCode/file-x_ray/build.spec#L31-L46)）；产物 162MB；CI 已有三平台矩阵（[build.yml](file:///d:/pythonCode/file-x_ray/.github/workflows/build.yml)），缺"安装包形态 + 锁文件" |

补充（对 file-x_ray 而言的**非**借鉴点）：单实例锁 + 健康探测 + 复用已运行实例，file-x_ray 已经做得比对方细（[L2553-L2629](file:///d:/pythonCode/file-x_ray/backend.py#L2553-L2629)），不要动。

---

## 5. 优先级表（P1–P11）

| 优先级 | 事项 | 成本量级 | 是否动现有数据 | 判断 |
|---|---|---|---|---|
| **P1** | **SQLite WAL + busy_timeout** | 约 30 分钟 | 否（仅多出 `-wal/-shm` 旁文件） | **立刻做** |
| **P2** | **排除规则 + 命中存量清理** | 0.5–1 天 | 新增配置；清理动作需显式确认 | **立刻做** |
| **P3** | **日志落盘 + 错误可见性** | 0.5 天 | 否 | **立刻做** |
| **P4** | **任务状态持久化 + 并发护栏** | 1–2 天 | 新增 `jobs` 表 | 值得做，排在 P1–P3 之后 |
| **P5** | **配置外置（config.json / settings 表）** | 0.5–1 天 | 新增配置 | 值得做，与 P2 一起落地最省 |
| **P6** | **打包工程化：onedir + 图标 + 安装包 + 依赖锁** | 1–2 天 | 否 | 可以做（**不是**上 Tauri） |
| **P7** | 抽取器注册表（含 docx/xlsx 支持） | 1.5–2 天 | 否（除非新增内容字段语义） | 有条件做：真正要加格式时再做 |
| **P8** | CLI / 库入口子命令 | 0.5 天 | 否 | 优先级低（产品是 GUI） |
| **P9** | **React + TS + Vite 前端重写** | 5–10 天 | 否 | **不建议** |
| **P10** | **Tauri 桌面壳** | 3–5 天起 | 否 | **不建议（现阶段）** |
| **P11** | **声明式清单 / 远端适配器仓库** | 3 天+ | 是 | **不建议** |

### 不建议项的原因（明确）

- **P9（React/TS/Vite）**：收益仅是开发体验（类型提示、组件化），代价是重新引入整条 Node 构建链（`node_modules`、锁文件漂移、打包时还要处理前端产物）。现有 `index.html` + `web/*.js` 无构建链本身就是优势。
- **P10（Tauri 壳）**：Python 载荷实测 162MB（`dist/文件索引工具.exe`），Tauri `externalBin` 只是把它塞进安装包；还要背 Rust 工具链、macOS 公证与 Windows 杀软误报。除非"安装包 + 自动更新"成为业务硬需求，否则不值得。
- **P11（声明式清单/远端仓库）**：该模式是 agentSessions 的领域产物——它要解析 17 种第三方 AI 工具的**私有、易变**格式（[adapters 目录](file:///D:/AiCode/agentSessions/adapters)、[manifest.js](file:///D:/AiCode/agentSessions/src/manifest.js#L1-L10)）。file-x_ray 只有 5 类本地标准文件类型，且抽取依赖原生库（无法"清单化"），远端下发解析配置还引入供应链风险，收益接近 0。

---

## 6. P1：SQLite WAL + busy_timeout（立刻做）

### 6.1 对方做法（证据）

[model.js L145-L154](file:///D:/AiCode/agentSessions/src/model.js#L145-L154)：每次打开库即 `db.pragma('journal_mode = WAL')`、`db.pragma('foreign_keys = ON')`，之后才建表/建触发器。

### 6.2 现状缺口（证据）

[get_db() L159-L171](file:///d:/pythonCode/file-x_ray/backend.py#L159-L171)：

```python
conn = sqlite3.connect(DB_PATH)
conn.row_factory = sqlite3.Row
return conn
```

无 `PRAGMA`、无 `timeout`（已全局 grep 验证：`backend.py` 内不存在 `PRAGMA` / `journal_mode`，仅有的 `timeout=` 都是 `urllib` 网络超时）。

写侧：[scan_worker](file:///d:/pythonCode/file-x_ray/backend.py#L845-L967) 每 200 个文件 commit 一次（[L951-L953](file:///d:/pythonCode/file-x_ray/backend.py#L951-L953)）；读侧：`/api/files`、`/api/tree`、`/api/stats` 每次都新建连接。回滚日志模式下写锁会阻塞读 → 大目录扫描时前端卡顿，极端情况报 `database is locked`。

### 6.3 落地改法

只改 `get_db()`（[backend.py L159-L171](file:///d:/pythonCode/file-x_ray/backend.py#L159-L171)）：

```python
def get_db():
    db_dir = os.path.dirname(DB_PATH)
    if db_dir and not os.path.isdir(db_dir):
        try:
            os.makedirs(db_dir, exist_ok=True)
        except Exception as e:
            raise RuntimeError(...)   # 保留原有报错文案
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn
```

要点：
- 每次连接都执行 PRAGMA 是幂等的（`journal_mode=WAL` 对已是 WAL 的库无副作用）。
- `synchronous=NORMAL` 是 WAL 下的常规折中，掉电最多丢最近一批提交，不会损坏库。
- **不要**顺手加 `foreign_keys=ON`：当前 schema 没有声明外键，加上会改变现有删除行为（如删项目），不属于本次范围。
- 在 `init_db()` 末尾（[L342-L446](file:///d:/pythonCode/file-x_ray/backend.py#L342-L446)）可加一次 `PRAGMA wal_checkpoint(TRUNCATE)`，让长期运行后 wal 文件不无限增长。

### 6.4 验证方法（点测）

1. **原有功能不受影响**：冷启动 → 打开首页 → 列表/树/统计/搜索各点一次；在一个已存在的项目上重扫，确认备注/状态/标签/转写/OCR 全部保留。
2. **并发场景**：对一个含 1 万+ 文件的大目录触发扫描，扫描进行中反复刷新 `/api/files` 与 `/api/tree`，页面不出现长时间白屏，后端日志无 `database is locked`。
3. **共存**：扫描进行中同时点"识别选中图片"，两条任务并行完成。
4. **数据完整性**：扫描结束后 `PRAGMA integrity_check` 返回 `ok`；标注数据条数与改前一致。

### 6.5 风险

极低。唯一可见变化是库旁多出 `file_index.db-wal` / `file_index.db-shm`；删库重扫时需一起删（见第 11 节）。

---

## 7. P2：排除规则 + 命中存量清理（立刻做）

### 7.1 对方做法（证据）

- 规则定义与匹配：[config.js L113-L127](file:///D:/AiCode/agentSessions/src/config.js#L113-L127)（含 `*`/`?` 通配按 glob 整路径匹配；无通配按路径包含匹配；大小写不敏感；源文件路径与项目 cwd 共用一套规则）。
- 采集时跳过：[scan.js L111-L114](file:///D:/AiCode/agentSessions/src/scan.js#L111-L114)。
- **存量清理**：[store.js L101-L118](file:///D:/AiCode/agentSessions/src/store.js#L101-L118) `applyExcludes` 删除命中记录并清掉对应增量指纹（否则"移除规则后未变更的文件不会被重新索引"）。

### 7.2 现状缺口（证据）

[scan_worker L853-L856](file:///d:/pythonCode/file-x_ray/backend.py#L853-L856) 直接 `os.walk(root)`，`dirnames` 只按 `max_depth` 裁剪（[L855-L856](file:///d:/pythonCode/file-x_ray/backend.py#L855-L856)），**没有任何忽略列表**。扫代码仓库会把 `.git/objects`、`node_modules`、`__pycache__` 全量入库，是当前 DB 膨胀（27MB）与搜索噪音的主要来源之一。

### 7.3 落地改法

**（a）规则存储**：优先复用 P5 的配置载体（推荐 `~/.file_xray/config.json`，字段 `excludes: string[]`）。若 P5 尚未开始，可先落 DB 新表：

```sql
CREATE TABLE IF NOT EXISTS app_excludes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pattern TEXT UNIQUE NOT NULL,
  added_at REAL
);
```

**（b）匹配函数**（新模块 `xray_config.py`，或先放进 `backend.py` 顶部配置区 [L79-L154](file:///d:/pythonCode/file-x_ray/backend.py#L79-L154)）：

```python
def _glob_to_regex(pattern: str) -> re.Pattern:
    # 逐字符翻译：** -> .*（吞掉后随的 /），* -> [^/]*，? -> [^/]，其余 re.escape
    ...

def is_excluded(path: str) -> bool:
    norm = path.replace("\\", "/").lower()
    for rule in EXCLUDES:
        if "*" in rule or "?" in rule:
            if _glob_to_regex(rule).match(norm):
                return True
        elif rule.replace("\\", "/").lower() in norm:
            return True
    return False
```

注意：`fnmatch` 不支持 `**` 跨目录语义，必须自己转正则（Y 点：`**/node_modules/**` 要能命中 `D:/repo/node_modules/a/b`）。

**（c）扫描时跳过**（[scan_worker L853-L856](file:///d:/pythonCode/file-x_ray/backend.py#L853-L856)）：

```python
for dirpath, dirnames, filenames in os.walk(root):
    dirnames[:] = [d for d in dirnames
                   if not is_excluded(os.path.join(dirpath, d))]
    ...
    for fn in filenames:
        full = os.path.join(dirpath, fn)
        if is_excluded(full):
            continue
```

**（d）存量清理**：新增显式接口 `POST /api/excludes/prune`（**不要**在保存配置时自动删数据）：

```sql
DELETE FROM files WHERE path LIKE ? OR path LIKE ? ...   -- 按规则逐条匹配
```

`files_fts` 由现有触发器自动同步（[L462-L478](file:///d:/pythonCode/file-x_ray/backend.py#L462-L478)），无需手动维护索引。返回删除条数给前端提示。

**（e）UI**：设置区加"排除规则"输入（每行一条）+ "应用到已索引文件"按钮（二次确认弹窗，文案明确"只删索引，不删磁盘文件"）。

### 7.4 验证方法（点测）

1. **原有功能不受影响**：规则为空时，扫描结果条数与改前**逐项一致**（对同一个小目录做前后对比）。
2. **新增功能**：加入 `node_modules` 规则后重扫，该目录不再出现在树与列表中；统计数字相应减少。
3. **共存**：命中的目录若此前已标注过备注，清理后应"从视图消失"而不是"文件消失"（磁盘上仍在）；把规则删掉并重扫后，文件重新出现（备注按路径重建为新行，属预期——如需保留需额外设计，本次不做）。
4. **边界**：`**/node_modules/**`、`D:\private`、大小写混写三种规则各测一次。

### 7.5 风险

中低。默认空规则 = 零行为变化；主要风险是清理误删——用显式确认 + 只 `DELETE FROM files`（不碰磁盘）+ 返回删除条数可接受。

---

## 8. P3：日志落盘 + 错误可见性（立刻做）

### 8.1 对方做法（证据）

- 单元级降级不吞错：[scan.js L122-L131](file:///D:/AiCode/agentSessions/src/scan.js#L148-L159) 单库解析失败写入 `status:'error'` 与 `scanErr`，继续扫其它源。
- 跳过原因上报：[scan.js L77-L93](file:///D:/AiCode/agentSessions/src/scan.js#L77-L93) 超阈值文件进 `oversized[]`，最终进统计。
- 日志落盘：[lib.rs L86-L100](file:///D:/AiCode/agentSessions/src-tauri/src/lib.rs#L86-L100) 把后端 stdout/stderr 重定向到临时目录日志文件（release 无控制台时仍可排查）。

### 8.2 现状缺口（证据）

- **静默吞错**（行为无痕）：
  - [L875-L876](file:///d:/pythonCode/file-x_ray/backend.py#L875-L876)：目录 `os.stat` 失败 → `pass`（目录不入库，用户无感）；
  - [L883-L884](file:///d:/pythonCode/file-x_ray/backend.py#L883-L884)：文件 `lstat` / `isfile` 失败 → `continue`；
  - [L913-L914](file:///d:/pythonCode/file-x_ray/backend.py#L913-L914)：文本读取失败 → `content=""`；
  - [L918-L919](file:///d:/pythonCode/file-x_ray/backend.py#L918-L919)：PDF 抽字失败 → `content=""`。
  直接后果是"这个 PDF 为什么搜不到"用户与开发者都无从判断。
- **无日志文件**：`print` 仅 8 处（L488、622、962、2432、2602、2610、2611、2624）；[build.spec L31-L46](file:///d:/pythonCode/file-x_ray/build.spec#L31-L46) `console=True`，用户关掉黑窗即失去所有日志。

### 8.3 落地改法

**（a）日志基础设施**（`backend.py` 顶部，紧跟 [L79-L154](file:///d:/pythonCode/file-x_ray/backend.py#L79-L154) 配置区）：

```python
import logging
from logging.handlers import RotatingFileHandler

LOG_DIR = os.path.join(os.path.dirname(DB_PATH), "logs")
os.makedirs(LOG_DIR, exist_ok=True)
_log = logging.getLogger("xray")
_log.setLevel(os.environ.get("FIT_LOG_LEVEL", "INFO").upper())
_h = RotatingFileHandler(os.path.join(LOG_DIR, "xray.log"),
                         maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
_h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
_log.addHandler(_h)
```

**（b）替换 `pass`（行为不变）**：

```python
except Exception:
    _log.debug("目录 stat 失败：%s", dirpath, exc_info=True)
```

逐文件场景一律 `debug` 级（默认不写入日志），避免刷屏；整次扫描级失败用 `error`。

**（c）进度里带原因**（[scan_state L1469-L1470](file:///d:/pythonCode/file-x_ray/backend.py#L1469-L1470) 初始化处，及 [L845](file:///d:/pythonCode/file-x_ray/backend.py#L845) 附近）：

```python
scan_state[sid] = {"processed": 0, "done": False, "error": None,
                   "root": root, "project_id": pid,
                   "skipped": [], "errors": []}   # 各上限 200 条
```

在 [L913-L919](file:///d:/pythonCode/file-x_ray/backend.py#L913-L919) 的失败分支追加 `scan_state[sid]["skipped"].append({"path": full, "reason": "text_read_failed"})`（PDF 为 `"pdf_extract_failed"`）。`/api/scan/{sid}`（[L1475-L1481](file:///d:/pythonCode/file-x_ray/backend.py#L1475-L1481)）返回这两个字段；前端任务浮层显示"N 个文件未取到内容（可导出清单）"。

**（d）转写/OCR 任务同样补 `errors`**：已有 `transcribe_jobs[jid].setdefault("errors", []).append(...)`（[L2302-L2303](file:///d:/pythonCode/file-x_ray/backend.py#L2302-L2303)），OCR 侧（[ocr_worker L2438 起](file:///d:/pythonCode/file-x_ray/backend.py#L2438)）做对等处理，并在对应进度接口原样返回。

**（e）启动时打印日志路径**（[L2610-L2611](file:///d:/pythonCode/file-x_ray/backend.py#L2610-L2611) 旁），方便用户反馈问题时附日志。

### 8.4 验证方法（点测）

1. **原有功能不受影响**：正常目录扫描/搜索/详情/导出结果与改前一致；`FIT_LOG_LEVEL` 未设时日志文件只含启动与异常级信息。
2. **新增功能**：放一个损坏 PDF（如 0 字节 `.pdf`）和一个无权限目录，重扫后任务浮层显示跳过条数，日志文件里有对应 `DEBUG` 记录。
3. **共存**：`pass` 改为 `debug` 后，原本"静默跳过"的文件**仍然被跳过**（不因改日志而开始报错或中断扫描）。

---

## 9. P4–P8 落地要点

### P4 任务状态持久化 + 并发护栏（1–2 天，新增表）

- **表**：

```sql
CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY,
  kind TEXT NOT NULL,          -- scan | transcribe | ocr | pdf_backlog
  root TEXT,
  total INTEGER DEFAULT 0,
  done INTEGER DEFAULT 0,
  current TEXT DEFAULT '',
  status TEXT DEFAULT 'running', -- running | done | error | aborted
  error TEXT,
  extra TEXT,                  -- JSON：skipped/errors/ocr_job_id 等
  created_at REAL,
  updated_at REAL
);
```

- **改法**：`scan_worker` / `transcribe_worker` / `ocr_worker` 每 200 件（沿用 [L951-L953](file:///d:/pythonCode/file-x_ray/backend.py#L951-L953) 的节奏）写一次 `jobs`；`/api/scan/{sid}`、`/api/transcribe/{jid}`、`/api/ocr/{jid}` 先查库、再做内存回落（灰度期可双写）。
- **并发护栏**：模块级 `_active_roots: set[str]`（配 `threading.Lock`），[start_scan L1437-L1473](file:///d:/pythonCode/file-x_ray/backend.py#L1437-L1473) 检测同根任务未结束 → 返回 409 + 已有 `scan_id`；[watch 触发的 `_index_single` L2049](file:///d:/pythonCode/file-x_ray/backend.py#L2049) 不受此限（单文件索引）。
- **启动自愈**：`init_db()` 里把 `status='running'` 的行统一改 `aborted`，前端可提示"上次任务被中断"。
- **验证**：扫描中途强杀进程 → 重启后任务列表显示 aborted、`/api/scan/{sid}` 可查到；连点两次相同 root → 第二次 409；原有单次扫描结果不变。

### P5 配置外置（0.5–1 天）

- 文件 `~/.file_xray/config.json`（打包版与源码版同一位置，避免 [《_resolve_db_path》L41-L76](file:///d:/pythonCode/file-x_ray/backend.py#L41-L76) 的多候选逻辑蔓延）。
- 白名单键：`excludes`、`ocr_engine`、`umi_ocr_url`、`ocr_langs`、`whisper_model`、`whisper_lang`、`max_body_mb`、`watch_roots`、`log_level`。
- 优先级固定：**环境变量 > config.json > 代码默认**；`FIT_*` 继续生效（向后兼容）。
- 加载函数 `load_settings()` 每次请求现读（文件小、成本可忽略），实现"改配置即时生效"，对齐对方 [server.js L272-L276](file:///D:/AiCode/agentSessions/server.js#L272-L276) 的热重载思路。
- 验证：改 json 后无需重启即生效；环境变量仍能覆盖；配置损坏时回落到默认而非崩溃。

### P6 打包工程化（1–2 天）

- `build.spec` 由 onefile 改 **onedir**（`EXE` + `COLLECT`），显著改善冷启动；若担心分发便利可保留 onefile 作为备选构建目标（[L9-L46](file:///d:/pythonCode/file-x_ray/build.spec#L9-L46)）。
- 加 `icon`、Windows version resource；`console` 改为可切换（源码运行 `True`，发行版 `False` + P3 的日志文件兜底）。
- 加 `requirements.lock`（`pip-compile requirements.txt`，[requirements.txt L1-L13](file:///d:/pythonCode/file-x_ray/requirements.txt#L1-L13) 注释里已建议但未落地）。
- 安装包用 Inno Setup 或 NSIS 生成（对照对方 [build.yml L167-L176](file:///D:/AiCode/agentSessions/.github/workflows/build.yml#L167-L176) 的产物矩阵思路，但**不引入 Tauri**）。
- 验证：干净机器双击安装 → 启动 → DB 落在用户目录（[_resolve_db_path](file:///d:/pythonCode/file-x_ray/backend.py#L41-L76)）→ 卸载后用户数据保留；同机覆盖升级后标注数据完好。

### P7 抽取器注册表（1.5–2 天，条件触发）

- 触发条件：确实要支持 `docx/xlsx/pptx` 等当前**只分类不抽文**的类型（[DOC_EXTS L125-L126](file:///d:/pythonCode/file-x_ray/backend.py#L125-L126)）。
- 新建 `extractors/` 包，接口：`matches(ext, size, sniff) -> bool` 与 `extract(path) -> tuple[str, str]`（返回 text 与 source 标记），有序注册表 `EXTRACTORS`。
- [scan_worker L906-L921](file:///d:/pythonCode/file-x_ray/backend.py#L906-L921) 的内联分派改为遍历注册表；`content_source` 取值必须保持兼容（`text_layer` / `ocr` / `manual` / 空）。
- **不可破坏**：移动记录时"PDF 抽不到文字层不覆盖旧内容"的语义（[L930-L933](file:///d:/pythonCode/file-x_ray/backend.py#L930-L933)）。
- 验证：新增格式能搜到；原有 txt/pdf 抽取结果逐字节一致；改名迁移后旧内容仍保留。

### P8 CLI（0.5 天，优先级低）

- `argparse` 子命令 `scan --root <path> [--no-browser]`、`search <kw>`、`detect`，内部复用 `scan_worker` 与 `_build_where`（[L1089-L1136](file:///d:/pythonCode/file-x_ray/backend.py#L1089-L1136)）。
- 注意：CLI 模式下必须跳过"自动开浏览器"逻辑（[L2613-L2618](file:///d:/pythonCode/file-x_ray/backend.py#L2613-L2618)）。

---

## 10. 必须原样保留的清单（禁止改动语义）

1. **`files` 表结构与字段语义**（[init_db L342-L446](file:///d:/pythonCode/file-x_ray/backend.py#L342-L446)）：`content` / `transcript` / `pending_text` / `pending_kind` / `content_source` / `is_dir` / `missing` / `rel_path` / `parent`。只允许**新增列/新增表**，不允许改既有列含义。
2. **指纹追认与路径迁移**：`find_orphan`（[L736](file:///d:/pythonCode/file-x_ray/backend.py#L736)）、`update_project_roots_moved`（[L789](file:///d:/pythonCode/file-x_ray/backend.py#L789)）、`relocate_records`（[L817](file:///d:/pythonCode/file-x_ray/backend.py#L817)）、`scan_worker` 的 moved 分支（[L922-L933](file:///d:/pythonCode/file-x_ray/backend.py#L922-L933)）与 [ROADMAP L67-L80](file:///d:/pythonCode/file-x_ray/ROADMAP.md) 的"保守原则"：旧路径仍在磁盘时绝不迁移（那是副本）；多候选全部消失时宁可不迁移。
3. **项目 = 虚拟视图**（[L173-L340](file:///d:/pythonCode/file-x_ray/backend.py#L173-L340)、[L432-L442](file:///d:/pythonCode/file-x_ray/backend.py#L432-L442)）：只存 `roots` JSON，成员按路径前缀实时匹配；**不得新增 files↔projects 关联表**；删除项目只删视图。
4. **OCR / 转写 / PDF 三级降级**：Umi-OCR HTTP → tesseract → 浏览器内置 tesseract.js WASM（[L587-L732](file:///d:/pythonCode/file-x_ray/backend.py#L587-L732)、`web/vendor/tesseract/`）；扫描件 PDF 后台补识别（[L2396-L2437](file:///d:/pythonCode/file-x_ray/backend.py#L2396-L2437)）；失败不写内容、旧内容不被冲掉（[store_recognized L767](file:///d:/pythonCode/file-x_ray/backend.py#L767)）。
5. **本地令牌中间件 + 仅绑 127.0.0.1**（[L83-L103](file:///d:/pythonCode/file-x_ray/backend.py#L83-L103)、[L1059-L1073](file:///d:/pythonCode/file-x_ray/backend.py#L1059-L1073)）：`FIT_TOKEN` / `~/.file_xray_token`、`/` 路由注入 `FIT_TOKEN_PLACEHOLDER`（[L1147](file:///d:/pythonCode/file-x_ray/backend.py#L1147)）；`MAX_BODY` 请求体上限。**不得**为了"简化"去掉令牌或改成 `0.0.0.0`。
6. **浏览器 WASM OCR 静态资源**：`web/vendor/tesseract/`（约 14MB 语言包与核心）必须继续随包分发（[build.spec L13](file:///d:/pythonCode/file-x_ray/build.spec#L13) 的 `datas` 已包含 `web`），这是"零安装兜底"的卖点（[README L18-L27](file:///d:/pythonCode/file-x_ray/README.md#L18-L27)）。
7. **zip 导出与 manifest 格式**：`build_export`（[L1005](file:///d:/pythonCode/file-x_ray/backend.py#L1005)）、`/api/export`（[L1885-L1897](file:///d:/pythonCode/file-x_ray/backend.py#L1885-L1897)）、`/api/report`（[L1899-L1942](file:///d:/pythonCode/file-x_ray/backend.py#L1899-L1942)）保留原 `rel_path` 目录结构与 `manifest.json/csv` 字段；可选 `.文件名.note.json` 副文件格式不变。
8. **单实例锁 + 健康探测 + 复用现有实例**（[L2553-L2629](file:///d:/pythonCode/file-x_ray/backend.py#L2553-L2629)）：`instance.lock`、`/api/health` 返回 `service: file-xray`、已有实例则直接开浏览器退出。这是当前比 agentSessions 更好的部分，任何重构都不得退化。

---

## 11. 既有能力与事实 / 推断分界

### 11.1 已验证事实（可直接引用，均有文件路径与行号）

- agentSessions 无生产 Python 代码；核心是 Node + Tauri（[package.json](file:///D:/AiCode/agentSessions/package.json#L1-L29)、[lib.rs](file:///D:/AiCode/agentSessions/src-tauri/src/lib.rs#L1-L162)）。
- file-x_ray 为单文件 FastAPI 单体（2629 行），`get_db()` 无任何 PRAGMA（grep 验证）。
- file-x_ray 扫描/转写/OCR 进度均为内存字典（L843 / L2270 / L2436 / L2012）。
- file-x_ray 存在 72 处 `except Exception`，其中 4 处为静默跳过（L875、L883、L913、L918）。
- file-x_ray 打包配置为 onefile + `console=True` + 无图标/版本（[build.spec](file:///d:/pythonCode/file-x_ray/build.spec#L31-L46)）；现有 `dist/文件索引工具.exe` 实测 **162,787,475 字节（约 162MB）**。
- `requirements.txt` 使用 `>=` 浮动版本，无 lock 文件。
- 两项目均无自动化测试；agentSessions 的 `tests/` 目录为空（已 ls 验证）。
- file-x_ray 已有跨平台 CI 矩阵 + tag 自动 Release（[build.yml](file:///d:/pythonCode/file-x_ray/.github/workflows/build.yml)），并非"无 CI"。
- [ROADMAP.md L6-L24](file:///d:/pythonCode/file-x_ray/ROADMAP.md) 中"无 LICENSE / 无 CI / 无 watchdog / 无 FTS"等结论**已过期**，不要据此判断现状（该文件后续可单独更新，本次不改）。

### 11.2 推断待实测（执行前/执行中需自己量一下）

- **PyInstaller onefile 的冷启动耗时与 Windows 杀软误报率**：目前只有体积数据，无启动耗时与误报统计；改 onedir 后的实际收益需在目标机器上实测（建议在同一台机器上连续测 5 次冷启动取中位数）。
- **Tauri + Python sidecar 的安装包体积与冷启动**：未实测；基于 162MB 载荷推断会明显劣于对方（对方 payload 仅含 `node.exe` + 裁剪版 better-sqlite3）。
- **Node 侧重写 OCR/ASR/PDF 的性能差距**：未做基准，按生态现状推断显著劣于 Python（`tesseract.js` 单页约 1–3 秒，见 [README L161](file:///d:/pythonCode/file-x_ray/README.md#L161)）。
- **WAL 在现有数据目录（可能位于机械盘/网络盘）上的表现**：本地 SSD 无问题；网络映射盘可能不支持 WAL，若用户把 `FIT_DB` 指到网络盘需回退 `journal_mode=DELETE`。执行时增加一次 try/except 回落即可。

### 11.3 不属于"可借鉴优点"的错觉（避免误判）

- 安装包形态（NSIS/MSI/dmg/deb/AppImage）= Tauri 生态红利，Python 侧需 Inno Setup + macOS 公证（费用与流程），不对等。
- "自包含载荷很优雅" = Node 生态体积优势；Python 因 ctranslate2/PyMuPDF 必然 100MB+。
- better-sqlite3 的版本/ABI 约束（[readme L90-L103](file:///D:/AiCode/agentSessions/readme.md)）看似"严谨"，实为痛点；Python stdlib `sqlite3` 更省心。
- React/TS 的类型化 API 是前端生态红利，对单机小工具是净成本。
- 声明式清单的"生态感"来自领域（解析 17 种私有格式），file-x_ray 只有 5 类标准格式且依赖原生库。
- 多平台 CI 两边都有；差异只在产物形态，不在工程水平。

---

## 12. 执行顺序与验收标准

### 12.1 顺序（每步独立可回滚）

```
P1(WAL) → P2(排除规则) → P3(日志/错误可见性) → P4(jobs 表) → P5(配置外置) → P6(打包) → [P7 视需求] → [P8 可选]
```

- P1 独立提交，改动只有 `get_db()` 一个函数 —— 出问题回滚一行即可。
- P2 依赖 P5 的载体时可先用 DB 表（`app_excludes`），后续 P5 再做迁移或并存；两者都保持"空规则=零变化"。
- P3 只改异常分支与进度返回字段，**不改主流程控制流**。
- P4 只新增表与进度读写，保留内存态作为回落，便于灰度。
- P6 与 P1–P5 无代码耦合，可并行。

### 12.2 项目验证偏好（必须遵守）

- **以点测为主，不追求补单元测试**（两个项目历史上都无测试，新增测试框架不在本次范围）。
- 每个改动点必须覆盖三类场景：
  1. **原有功能**：冷启动 → 首页/列表/树/统计/搜索/详情/预览/导出/转写/OCR 各跑一遍；
  2. **新增功能**：本条改动引入的用例（如排除规则生效、日志有记录、任务可续）；
  3. **两者共存**：新功能开着的情况下原有功能仍正确（例如排除规则开启后，未命中目录的标注/转写/OCR 全部保留；扫描进行中同时做 OCR）。
- **每步都要求**：不破坏现有页面逻辑（含错误路径下的页面表现）、不破坏已有标注数据（备注/状态/标签/转写/OCR/暂存）。
- 数据安全基线：改动前后对同一项目做"文件条数 / 已标注条数 / transcript 非空条数 / content 非空条数"四项计数比对，差异必须可解释。
- 建议在每步开始前**备份一份 `file_index.db`**（WAL 模式下备份时请先关闭进程或执行 checkpoint）。

### 12.3 每步完成定义（DoD）

- 代码改动最小且集中（单条改动原则上不超过 2 个函数/1 个新表）。
- 点测三类场景全过。
- 日志或接口中有可自证的结果（如删除条数、跳过条数、任务状态）。
- 已在本文件对应小节补充"实际改动位置"（可选，但不要新增额外文档）。

---

## 13. 已知坑与勿踩项

1. **WAL 会多出旁文件**：`file_index.db-wal`、`file_index.db-shm`。README 中"数据库为单个 SQLite 文件，可随时删除后重新扫描"（[README L185](file:///d:/pythonCode/file-x_ray/README.md#L185)）与"删库重扫"相关文案需改为"删除 `.db` 及同名 `-wal/-shm`"。备份/迁移时三者要一起处理，或先在程序内 `wal_checkpoint(TRUNCATE)` 后关闭。
2. **排除规则清理存量必须显式二次确认**：绝不在保存配置的同一请求里删数据；绝不动磁盘文件（只 `DELETE FROM files`）；接口要返回删除条数并在 UI 上明示"仅移除索引"。
3. **日志不能用逐文件 INFO 刷屏**：逐文件失败一律 `DEBUG`（`exc_info=True` 只在排查时需要），整次扫描级失败才 `ERROR`；`RotatingFileHandler` 设 10MB × 5。
4. **`except: pass` 改日志时行为不能变**：只允许在 `except` 块内追加日志语句，**不得**改成 `raise`、不得改变 `continue`/跳过/空字符串回落的既有控制流；也不得让日志写入本身抛出异常（`_log` 调用外层包 try 或使用 `logging.raiseExceptions = False` 语义）。
5. **不要顺手改 schema**：P2 只加 `app_excludes`、P4 只加 `jobs`；不要"顺便"给 `files` 加外键、不要改 `files_fts` 触发器、不要重命名列。既有 `ALTER TABLE ... except pass` 的兼容补列方式（[L364-L400](file:///d:/pythonCode/file-x_ray/backend.py#L364-L400)）保持原样。
6. **不要引入 Node 工具链**：不新增 `package.json` / `node_modules`；P9/P10 已明确不建议，除非用户另行明确要求。
7. **不要修改 `D:\AiCode\agentSessions`**：它是只读参考样本，任何"同步修改"都是越界。
8. **不要为了 P4 删掉内存态**：`/api/scan/{sid}` 先查库后回落内存，避免灰度期前端 404。
9. **不要在排除逻辑里使用 `fnmatch`**：它不支持 `**` 跨目录语义，会让 `**/node_modules/**` 规则失效。
10. **不要动单实例锁与令牌机制**：这两块 existing 实现优于参考项目（[L2553-L2629](file:///d:/pythonCode/file-x_ray/backend.py#L2553-L2629)、[L1059-L1073](file:///d:/pythonCode/file-x_ray/backend.py#L1059-L1073)），改造一律绕开。

---

## 14. 附：关键文件与行号速查

### file-x_ray（改造对象）

| 位置 | 内容 |
|---|---|
| [backend.py L41-L76](file:///d:/pythonCode/file-x_ray/backend.py#L41-L76) | DB 路径候选与用户目录策略 |
| [backend.py L79-L154](file:///d:/pythonCode/file-x_ray/backend.py#L79-L154) | 环境变量配置区（P5 落点） |
| [backend.py L159-L171](file:///d:/pythonCode/file-x_ray/backend.py#L159-L171) | `get_db()`（P1 唯一改点） |
| [backend.py L173-L340](file:///d:/pythonCode/file-x_ray/backend.py#L173-L340) | 项目虚拟视图（禁止改动语义） |
| [backend.py L342-L446](file:///d:/pythonCode/file-x_ray/backend.py#L342-L446) | `init_db()` 建表（P2/P4 新表落点） |
| [backend.py L451-L488](file:///d:/pythonCode/file-x_ray/backend.py#L451-L488) | FTS5 初始化与触发器 |
| [backend.py L565-L732](file:///d:/pythonCode/file-x_ray/backend.py#L565-L732) | PDF 抽取 / OCR 三级降级 |
| [backend.py L736-L838](file:///d:/pythonCode/file-x_ray/backend.py#L736-L838) | 指纹追认与路径迁移 |
| [backend.py L845-L967](file:///d:/pythonCode/file-x_ray/backend.py#L845-L967) | `scan_worker`（P2 跳过逻辑、P3 跳过原因、P4 进度落库） |
| [backend.py L1005-L1045](file:///d:/pythonCode/file-x_ray/backend.py#L1005-L1045) | `build_export`（禁止改格式） |
| [backend.py L1059-L1073](file:///d:/pythonCode/file-x_ray/backend.py#L1059-L1073) | 安全中间件（禁止改动） |
| [backend.py L1089-L1136](file:///d:/pythonCode/file-x_ray/backend.py#L1089-L1136) | `_build_where` 搜索条件构造 |
| [backend.py L1436-L1481](file:///d:/pythonCode/file-x_ray/backend.py#L1436-L1481) | 扫描任务启动与进度接口（P4 护栏） |
| [backend.py L2207-L2311](file:///d:/pythonCode/file-x_ray/backend.py#L2207-L2311) | faster-whisper 转写 |
| [backend.py L2396-L2551](file:///d:/pythonCode/file-x_ray/backend.py#L2396-L2551) | OCR 任务与 PDF 后台补识别 |
| [backend.py L2553-L2629](file:///d:/pythonCode/file-x_ray/backend.py#L2553-L2629) | 单实例锁与启动（禁止退化） |
| [build.spec](file:///d:/pythonCode/file-x_ray/build.spec#L31-L46) | PyInstaller 配置（P6 落点） |
| [requirements.txt](file:///d:/pythonCode/file-x_ray/requirements.txt#L1-L13) | 依赖清单（P6 加 lock） |
| [.github/workflows/build.yml](file:///d:/pythonCode/file-x_ray/.github/workflows/build.yml) | CI 三平台构建与 Release |
| [ROADMAP.md L6-L24](file:///d:/pythonCode/file-x_ray/ROADMAP.md) | 过期结论（勿作现状依据） |

### agentSessions（只读参考）

| 位置 | 借鉴内容 |
|---|---|
| [src/model.js L145-L154](file:///D:/AiCode/agentSessions/src/model.js#L145-L154) | WAL + pragma（P1） |
| [src/config.js L35-L82](file:///D:/AiCode/agentSessions/src/config.js#L35-L82) / [L113-L127](file:///D:/AiCode/agentSessions/src/config.js#L113-L127) | 配置白名单/热重载与排除规则（P2/P5） |
| [src/store.js L101-L118](file:///D:/AiCode/agentSessions/src/store.js#L101-L118) | 排除规则存量清理（P2） |
| [src/scan.js L77-L93](file:///D:/AiCode/agentSessions/src/scan.js#L77-L93) | 跳过原因上报（P3） |
| [src/scan.js L115-L159](file:///D:/AiCode/agentSessions/src/scan.js#L115-L159) | 单元级故障隔离（P3） |
| [src-tauri/src/lib.rs L86-L100](file:///D:/AiCode/agentSessions/src-tauri/src/lib.rs#L86-L100) | 日志落盘（P3） |
| [src/model.js L82-L89](file:///D:/AiCode/agentSessions/src/model.js#L82-L89)、[src/store.js L80-L92](file:///D:/AiCode/agentSessions/src/store.js#L80-L92) | 任务/指纹持久化（P4） |
| [scripts/pack-backend.js L112-L147](file:///D:/AiCode/agentSessions/scripts/pack-backend.js#L112-L147)、[build.yml L96-L217](file:///D:/AiCode/agentSessions/.github/workflows/build.yml#L96-L217) | 分发纪律（P6，仅借鉴思路） |
| [src/cli.js L9-L69](file:///D:/AiCode/agentSessions/src/cli.js#L9-L69)、[src/index.js L9-L12](file:///D:/AiCode/agentSessions/src/index.js#L9-L12) | 多入口（P8） |
