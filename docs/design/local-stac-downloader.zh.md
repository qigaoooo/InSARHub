# 技术方案：InSARHub `STAC_API` 下载器

| 项 | 内容 |
|---|---|
| 文档版本 | **v0.4（复盘修订稿，待确认）** |
| 状态 | **只迭代文档；你确认前不写业务代码** |
| 分支 | `feat/stac-api`（fork: `qigaoooo/InSARHub`） |
| 对接对象 | 本地 Docker **STAC API**（stac-ingest → 官方 `stac-fastapi-pgstac` + `/assets/`） |
| 上游设计参照 | [`refs/stac-ingest-design_v2.zh.md`](refs/stac-ingest-design_v2.zh.md)（**不进上游 PR**） |
| InSARHub 依据 | `CONTRIBUTING.md`、`docs/contributing/backend.md`、`test/README.md` |
| 规范 | [STAC 1.1.0](https://github.com/radiantearth/stac-spec)、[STAC API](https://github.com/radiantearth/stac-api-spec)、[File Extension 2.1.0](https://github.com/stac-extensions/file) |
| 复盘 | v0.3 → v0.4：补 `gui_hidden`、属性分档、联调 URL、产品定位 |

---

## 0. 本文目的与确认流程

1. 把 `STAC_API` 下载器在 InSARHub 内的边界、契约、落点、测试写清楚。  
2. **你确认本文件（可改条目）后**，再说「确认方案，开写代码」——才开始实现。  
3. 对端 stac-ingest Docker **仍在开发**：编码阶段以 **STAC API 规范 + HTTP mock** 验收；真机联调另开检查项。

---

## 1. 一页结论

| 问题 | 决定 |
|---|---|
| 数据从哪来？ | HTTP 调本机（或内网）**STAC API `/search`**，不是读 `catalog.json`，不是 `file://` 硬链 |
| 文件怎么下？ | 跟 Item 里 **绝对 HTTP(S) Asset `href`**（通常 `/assets/<asset_key>`）做 GET |
| 写 Catalog 吗？ | **不写**（Transactions 仅属 stac-ingest） |
| 类怎么挂？ | `BaseDownloader` → `STAC_Base`（无 `name`）→ **`STAC_API`**（`name="STAC_API"`） |
| 新依赖？ | **否**，只用已有 `requests` |
| 默认 Collection？ | `["sentinel-1-slc"]` |
| 默认 API URL？ | **不写死生产默认**；配置必填或占位空串 + 文档示例；对端 stac-ingest 定稿后再填推荐值 |
| MVP 产品入口？ | **CLI / Python API 优先**；GUI **隐藏**（见 §1.1） |
| 处理器兼容？ | MVP **无** Processor 声明 `compatible_downloader="STAC_API"` |
| 配对 / merge？ | MVP **不支持** `select_pairs`、`merge=True` |
| 同机二次下载？ | MVP 接受 HTTP 再拉一份（可移植）；不做 DATA_ROOT 硬链优化 |

### 1.1 GUI 可见性（复盘强制修订）

InSARHub tier2 约定（`test_gui_offers_only_downloaders_a_processor_can_consume`）：

- 若无 Processor 消费某下载器 → 该类必须 `gui_hidden = True`，否则测试失败，且 GUI 会把用户带进「下完无法处理」死胡同。

因此 **`STAC_API` MVP 必须**：

```python
class STAC_API(STAC_Base):
    name = "STAC_API"
    gui_hidden = True   # 无兼容 Processor 前不得上架 GUI
```

| 入口 | MVP |
|---|---|
| `Downloader.create("STAC_API", …)` / CLI | ✅ 支持 |
| Web GUI 下载器下拉 | ❌ 不出现（与 `NISAR_RSLC` / `NISAR_GUNW` 同模式） |
| GUI「Add Job」→ `select_pairs` | 不适用（已隐藏） |

**二期**（另开设计）：某 Processor（或现有本地处理器）声明 `compatible_downloader="STAC_API"` 且落盘/配对契约对齐后，再去掉 `gui_hidden` 并评估 GUI 配对路径。

### 1.2 已知局限（写入文档，避免误解）

- **不是**「注册后即可像 `S1_SLC` 一样在 Web 里搜→配对→处理」。  
- Catalog 文件已在 stac-ingest 盘上时，InSARHub 仍经 HTTP 下载到 workdir（带宽/磁盘成本）。  
- 无垂直基线时无法复现 ASF `select_pairs` 质量。  
- Item 若缺少 orbit/frame 扩展字段，stack 键退化（见 §4），CLI filter 能力变弱，但 search/download 仍可用。

---

## 2. 系统边界

```text
┌─────────────────────┐         HTTP          ┌──────────────────────────┐
│      InSARHub       │  /search, GET assets  │  Docker STAC (stac-ingest)│
│  STAC_Base/STAC_API │ ◄───────────────────► │  stac-fastapi-pgstac      │
│  CLI / FastAPI GUI  │                       │  /assets 只读映射         │
└─────────────────────┘                       └──────────────────────────┘
        │ 写入 workdir/…
        ▼
   本地 SLC zip 等（供日后 Processor）
```

| 允许 | 禁止 |
|---|---|
| Search / 读 Item / GET Asset | STAC Transactions、直写 PgSTAC |
| 格式化成 InSARHub 场景对象 | 假设能访问 `DATA_ROOT` / `/mnt/diskrsdata` |
| mock 单测 | 把 `file://`、本机绝对路径写进默认配置或测试契约 |

---

## 3. 架构与模块

### 3.1 类层次（贡献指南）

```text
BaseDownloader                    # core/base.py；抽象六方法
  └── STAC_Base                   # downloader/stac_base.py；无 name
        └── STAC_API              # stac_api.py；name="STAC_API"；gui_hidden=True
```

- 中间基类**不设** `name`。  
- `downloader/__init__.py` **必须** `import` 叶子模块，否则不注册。  
- **不**继承 `ASF_Base_Downloader`。

### 3.2 文件落点（获准编码后）

| 路径 | 内容 |
|---|---|
| `src/insarhub/downloader/stac_base.py` | `STACProduct`、`_load_items`、`_format_item`、`STAC_Base` |
| `src/insarhub/downloader/stac_api.py` | `name="STAC_API"`，**`gui_hidden = True`** |
| `src/insarhub/downloader/__init__.py` | 增加 import |
| `src/insarhub/config/defaultconfig.py` | `STAC_Base_Config`、`STAC_API_Config` |
| `src/insarhub/config/__init__.py` | 导出 |
| `src/insarhub/config/paths.py` | `STACPaths`（见 §5） |
| `test/tier2_basic/test_stac_base.py` | 格式化 + search/download mock |
| `test/tier2_basic/fixtures/stac/` | 最小 Item / FeatureCollection JSON |
| `docs/advanced/downloader.md` + `.zh.md` | 用法小节 |
| `CHANGELOG.md` | `[Unreleased]` Added 条目 |

CLI：`commands/downloader.py` 泛型封装，注册后 CLI 可用。  
GUI：MVP **不出现**在下拉（`gui_hidden`）；**无需改 React**。

### 3.3 `stac_base.py` 内部流水线

```text
config ──► 组装 Search body
              │
              ▼
         POST {stac_api_url}/search   （分页 follow next）
              │
              ▼
         原始 STAC Item (GeoJSON Feature)
              │
              ▼
         _format_item → STACProduct
              │
              ▼
         _get_group_key → self.results: dict[tuple, list[STACProduct]]
              │
download ──► 选主 asset → GET href → STACPaths / StackPaths 布局
```

---

## 4. InSARHub 输出契约（格式化）

### 4.1 `STACProduct`（鸭子类型）

与 ASF 产品在 GUI 中的用法对齐（见 `app/routes/search.py` → `_to_geojson`）：

```python
product.properties   # dict
product.geometry     # GeoJSON geometry dict | None
# 可选：保留 raw item 供调试
product.raw_item     # 原始 dict（不进 GeoJSON properties 亦可）
```

`search()` / `active_results` 形状：

```python
dict[tuple, list[STACProduct]]
# 键优先 (pathNumber, frameNumber)；缺失见 §4.3
```

### 4.2 `properties` 映射表（分档）

#### 必填（缺失则跳过该 Item，并 warning）

| InSARHub 键 | STAC 来源 | 说明 |
|---|---|---|
| `sceneName` | `ingest:provider_product_id` → 去 `provider:` 前缀的 `id` → 完整 `id` | 下载子集主键 |
| `startTime` | `datetime` → `start_datetime` | ISO；无法解析则跳过 |
| `url` + `fileName` | 主 asset 绝对 `http(s)` `href` | 无合格主 asset 则不可 download（search 可保留并标记） |

#### 强烈建议（有则映射，无则 `None`，不跳过 Item）

| InSARHub 键 | STAC 来源 |
|---|---|
| `fileID` | `Item.id` |
| `stopTime` | `end_datetime` → `datetime` |
| `collection` | `collection` |
| `bytes` | 主 asset `file:size` |
| `_checksum` | 主 asset `file:checksum` |
| `geometry` | Feature.geometry（无则 footprint 受限） |

#### 可选增强（对端 stac-ingest 写齐后再有完整 stack UX）

| InSARHub 键 | STAC 来源 |
|---|---|
| `pathNumber` | `sat:relative_orbit` → `pathNumber` → `relativeOrbit` |
| `frameNumber` | `frameNumber` → `asfFrame` |
| `flightDirection` | `sat:orbit_state` → `ASCENDING`/`DESCENDING` |
| `platform` | `platform` → `constellation` |
| `polarization` | `sar:polarizations` |
| `processingLevel` | `sar:product_type` → `processing:level` |

**不承诺**：对端未写入 `sat:` / `sar:` 时仍有与 ASF 相同的 path/frame 过滤体验。

### 4.3 Stack 分组键

```text
若 pathNumber 与 frameNumber 均可得 → (pathNumber, frameNumber)
否则若仅 pathNumber → (pathNumber, "unknown")
否则 → ("local", collection_id 或 "default")
```

退化键下：落盘目录仍合法（见 §5）；`filter(path_frame=…)` 仅对能解析的键有效。

### 4.4 主 Asset 选择

1. `assets` 中 `roles` 与配置 `asset_roles`（默认 `["data"]`）有交集者优先；  
2. 否则排除 role 含 `thumbnail` / `overview` / `visual` 后的第一个；  
3. 仍无则该 Item **不可下载**（search 可保留，download 跳过并计数）。

`href` 必须是 `http://` 或 `https://`；其它 scheme → 报错（符合 stac-ingest「禁止 file:// 进 Catalog」）。

### 4.5 示例 Item（契约样例，非真实数据）

```json
{
  "type": "Feature",
  "stac_version": "1.1.0",
  "id": "asf:S1A_IW_SLC__1SDV_20200115T102030_…",
  "collection": "sentinel-1-slc",
  "geometry": { "type": "Polygon", "coordinates": [[[…]]] },
  "properties": {
    "datetime": "2020-01-15T10:20:30Z",
    "platform": "sentinel-1a",
    "sat:relative_orbit": 100,
    "sat:orbit_state": "ascending",
    "sar:polarizations": ["VV", "VH"],
    "sar:product_type": "SLC",
    "ingest:provider": "asf",
    "ingest:provider_product_id": "S1A_IW_SLC__1SDV_20200115T102030_…"
  },
  "assets": {
    "data": {
      "href": "http://127.0.0.1:36610/assets/rs_raw/sar_raw/S1A_IW_SLC__1SDV_20200115T102030_….zip",
      "type": "application/zip",
      "roles": ["data"],
      "file:size": 824123412,
      "file:checksum": "1220…"
    }
  }
}
```

格式化后期望：`sceneName` = 提供方 id，`pathNumber` = 100，`flightDirection` = `ASCENDING`，`url` = 上述 href。

---

## 5. 路径布局（`STACPaths`）

为与现有本地处理器习惯对齐，**下载落盘复用 `StackPaths` 语义**：

```text
workdir/
  p{path}_f{frame}/          # 或退化键时 p_local_{collection}/
    slc/
      <fileName>             # zip / SAFE 等，保持 basename
```

建议在 `paths.py` 增加薄封装（避免硬编码字符串）：

```python
@dataclass
class STACPaths:
    workdir: Path
    def slc_dir_for(self, path_key, frame_key) -> Path:
        # 委托 StackPaths.dir_for(...)/"slc" 或等价命名
        ...
```

`merge=True`：MVP **可先不支持**（或仅文档标明 NotImplemented）；与 ASF 行为对齐留二期。

---

## 6. 配置与入口

```python
@dataclass
class STAC_Base_Config:
    name: str = "STAC_Base_Config"
    # 对端未定稿：不要把现场端口写死进库。空串表示「调用前必须设置」。
    stac_api_url: str = ""
    collections: list[str] | None = field(
        default_factory=lambda: ["sentinel-1-slc"]
    )
    intersectsWith: str | None = None
    start: str | None = None
    end: str | None = None
    maxResults: int | None = 100
    granule_names: str | list[str] | None = None
    asset_roles: list[str] = field(default_factory=lambda: ["data"])
    verify_checksum: bool = False
    max_workers: int = 3
    ssl_verify: bool = True
    auth_token: str | None = None       # Authorization: Bearer …
    basic_user: str | None = None
    basic_password: str | None = None
    workdir: Path | str = field(default_factory=lambda: Path.cwd())

    _ui_groups = [  # 即便 gui_hidden，保留 schema 供日后揭开 GUI / 文档生成
        {"label": "STAC", "fields": [
            "stac_api_url", "collections", "verify_checksum",
            "max_workers", "ssl_verify", "maxResults",
        ]},
    ]
```

`search()` 若 `stac_api_url` 为空 → 立即 `ValueError`，提示设置 URL（文档给示例，例如对端常用 `http://127.0.0.1:<port>/stac` 或直连 app 端口——**以 stac-ingest 联调说明为准**）。

### 6.1 配置入口（MVP，GUI 隐藏时）

| 方式 | 说明 |
|---|---|
| Python | `STAC_API_Config(stac_api_url="http://…", …)` |
| CLI | 该下载器的 config / 环境变量（实现时与现有 CLI 配置机制对齐） |
| 环境变量（建议） | `INSARHUB_STAC_API_URL` 若设则覆盖空默认（实现可选，写入 advanced 文档） |

注意：GUI 在下载器类型不一致时只会透传 `max_workers` / `ssl_verify`；`stac_api_url` **不会**从 `S1_SLC` 设置泄漏——对 CLI 优先 MVP 无影响。

`STAC_API_Config(STAC_Base_Config)`：`name = "STAC_API_Config"`。

`search_filter_schema`：仍可声明 flightDirection / path / frame，供 CLI 与日后 GUI；缺扩展字段时过滤结果可能为空，属预期。

---

## 7. Search / Download / 其它方法

### 7.1 `search()`

1. 规范化 `stac_api_url`（去尾 `/`）。  
2. Body（STAC API Item Search）：

```json
{
  "collections": ["sentinel-1-slc"],
  "intersects": { "type": "Polygon", "coordinates": [ … ] },
  "datetime": "2020-01-01T00:00:00Z/2020-12-31T23:59:59Z",
  "limit": 100
}
```

- 有 `intersectsWith`：WKT → GeoJSON（复用现有 `_to_wkt` / shapely）。  
- 无几何时可省略 `intersects`。  
- `granule_names`：若可解析为 Item `ids` 则带 `ids`；否则先宽搜再按 `sceneName`/`fileID` 过滤。  
3. `POST …/search`；读 `features`；若 `links[rel=next]` 存在则继续，直到凑满 `maxResults` 或无 next。  
4. 每个 feature → `_format_item`；跳过无 `startTime` 者。  
5. 分组 → `self.results`；清空 `_subset`。  
6. HTTP 非 2xx / JSON 无效 → 抛清晰错误（含 URL 与 status）。

### 7.2 `download(…)`

签名尽量对齐 ASF（便于 GUI 复用）：

```python
def download(self, save_path=None, max_workers=None,
             stop_event=None, on_progress=None, scenes=None, merge=False):
```

- `scenes`：同 ASF 的 scene 名 / pairs 解析（可抽共用 `_parse_scene_filter` 或复制精简版）。  
- `merge`：MVP 若未实现则 `merge=True` 时 `ValueError` 说明。  
- 流式写入目标路径；已存在且 `bytes` 一致则跳过。  
- `verify_checksum=True`：校验 Multihash（至少支持 sha2-256 前缀 `1220`）；失败删文件并记入错误。  
- 并发：`ThreadPoolExecutor`，`max_workers` 来自参数或 config。

### 7.3 `filter` / `reset` / `summary` / `footprint`

行为对齐 `ASF_Base_Downloader` 的用户语义（path/frame、日期、方向、打印、覆盖图），实现独立、不调用 asf_search。  
`footprint`：可用已有 shapely/matplotlib 路径；若依赖过重，MVP 可仅返回 GeoJSON 到文件（实现时选与 ASF 一致的最小子集）。

### 7.4 `select_pairs`

MVP：**显式不支持**（GUI 已 `gui_hidden`，主要防 CLI/脚本误调）。

```python
def select_pairs(self, *args, **kwargs):
    raise NotImplementedError(
        "STAC_API does not provide ASF baseline stacks; "
        "pair selection will be added when perpendicular baseline is available."
    )
```

---

## 8. 错误与鉴权

| 情况 | 行为 |
|---|---|
| 连接拒绝 / DNS | 提示检查 `stac_api_url` 与 Docker 是否启动 |
| 401 / 403 | 提示配置 `auth_token` 或 basic |
| 404 on search | 检查 API 根路径是否需 `/stac` 前缀 |
| Asset 404 | 该场景失败，不中断其它（汇总） |
| checksum 失败 | 删除不完整文件；该场景失败 |
| 空结果 | `ValueError`（与 ASF「无结果」习惯一致） |

Session：`requests.Session()`；`verify=ssl_verify`；headers 带可选 Bearer。

---

## 9. 测试计划（贡献指南）

| 用例 | 断言 |
|---|---|
| `_format_item` 含可选增强字段的样例 | `pathNumber` / `flightDirection` 等 |
| `_format_item` 仅必填字段 | 无 path/frame 时仍产出；分组 `("local", …)` |
| mock `POST /search` | `results` 分组、`active_results` |
| mock asset `GET` | 文件落在 `…/slc/<basename>` |
| 缺 `datetime` | Item 被跳过 |
| `file://` href | download 拒绝 |
| 注册 | `"STAC_API" in Downloader._registry` |
| **gui_hidden** | `STAC_API.gui_hidden is True`（满足「无消费者则隐藏」测试） |
| `_format_item` 仅必填字段 | 无 path/frame 时仍产出 product，分组为 `("local", …)` |
| 配置 UI 字段 | `_ui_fields` 键 ⊆ dataclass 字段（沿用 tier2 惯例） |

命令：`pytest -m "basic or regression"`（含新文件）。  
**不**把真 Docker 当 CI 门禁。

---

## 10. 对上游 PR 的范围（日后）

| 纳入 PR | 不纳入 |
|---|---|
| `stac_*.py` / config / paths / tests / CHANGELOG / `docs/advanced/downloader*` 短说明 | `.cursor/` |
| 可选：`docs/design/local-stac-downloader.zh.md` 精简后的 InSARHub 侧说明 | **`docs/design/refs/stac-ingest-design_v2.zh.md` 全文**（PR 前删除或改 `.gitignore` / 移出分支） |

---

## 11. 实现顺序（仅在方案确认后执行）

1. Config（空默认 URL）+ `STACPaths` + `STACProduct` + 分档 `_format_item` + 单测  
2. `_load_items` + `search` / `filter` / `reset` / `summary`  
3. `download` + 可选 checksum；拒绝非 http(s)  
4. `STAC_API` 注册且 **`gui_hidden=True`**  
5. advanced 文档（CLI 示例）+ CHANGELOG  
6. `pytest -m "basic or regression"`  

---

## 12. 复盘结论摘要

| 项 | v0.4 处理 |
|---|---|
| 无 Processor 却上架 GUI → 测试失败 | **`gui_hidden=True`，CLI 优先** |
| GUI Add Job / select_pairs | MVP 不触及；二期再议 |
| 属性映射过乐观 | **必填 / 建议 / 可选增强** 分档 |
| API 端口未定 | **默认空 URL**，联调后写文档推荐值 |
| 同机二次下载 | 接受并写明局限 |
| refs 外仓设计进 PR | **明确排除** |

---

## 13. 待你确认的清单

请逐条回复 **同意 / 修改**：

1. MVP：`gui_hidden=True`，主入口 CLI/Python — 同意？  
2. 注册名 `STAC_API`、仅 `requests`、默认 Collection `sentinel-1-slc` — 维持？  
3. 落盘 `p{path}_f{frame}/slc/`（退化键用 `local` 目录）— 同意？  
4. 不支持 `select_pairs` / `merge` — 同意？  
5. `verify_checksum` 默认 False — 同意？  
6. `stac_api_url` 默认空，调用前必填 — 同意？  
7. 鉴权：Bearer + basic 足够？  
8. PR 排除 `refs/stac-ingest-design_v2.zh.md` — 同意？  

---

**请确认或批注本 v0.4。** 收到「确认方案，开写代码」之前，**不修改** `src/` 业务代码。
