# 技术方案：InSARHub `STAC_API` 下载器

| 项 | 内容 |
|---|---|
| 文档版本 | **v0.3（完善稿，待确认）** |
| 状态 | **只迭代文档；你确认前不写业务代码** |
| 分支 | `feat/stac-api`（fork: `qigaoooo/InSARHub`） |
| 对接对象 | 本地 Docker **STAC API**（stac-ingest → 官方 `stac-fastapi-pgstac` + `/assets/`） |
| 上游设计参照 | [`refs/stac-ingest-design_v2.zh.md`](refs/stac-ingest-design_v2.zh.md) |
| InSARHub 依据 | `CONTRIBUTING.md`、`docs/contributing/backend.md`、`test/README.md` |
| 规范 | [STAC 1.1.0](https://github.com/radiantearth/stac-spec)、[STAC API](https://github.com/radiantearth/stac-api-spec)、[File Extension 2.1.0](https://github.com/stac-extensions/file) |

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
| 默认 API？ | 占位 `http://127.0.0.1:8082`（对端定稿只改配置） |
| 处理器兼容？ | MVP **不声明** `compatible_processor`；落盘尽量贴近 `StackPaths` 的 `…/slc/`，便于日后对接 |
| 配对？ | MVP **不实现** ASF 级 `select_pairs`（无垂直基线 API）；GUI/CLI 调用时返回明确错误或跳过 |

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
        └── STAC_API              # downloader/stac_api.py；name="STAC_API"
```

- 中间基类**不设** `name`。  
- `downloader/__init__.py` **必须** `import` 叶子模块，否则不注册。  
- **不**继承 `ASF_Base_Downloader`。

### 3.2 文件落点（获准编码后）

| 路径 | 内容 |
|---|---|
| `src/insarhub/downloader/stac_base.py` | `STACProduct`、`_load_items`、`_format_item`、`STAC_Base` |
| `src/insarhub/downloader/stac_api.py` | `class STAC_API(STAC_Base): name = "STAC_API"` |
| `src/insarhub/downloader/__init__.py` | 增加 import |
| `src/insarhub/config/defaultconfig.py` | `STAC_Base_Config`、`STAC_API_Config` |
| `src/insarhub/config/__init__.py` | 导出 |
| `src/insarhub/config/paths.py` | `STACPaths`（见 §5） |
| `test/tier2_basic/test_stac_base.py` | 格式化 + search/download mock |
| `test/tier2_basic/fixtures/stac/` | 最小 Item / FeatureCollection JSON |
| `docs/advanced/downloader.md` + `.zh.md` | 用法小节 |
| `CHANGELOG.md` | `[Unreleased]` Added 条目 |

CLI：`commands/downloader.py` 已按 `BaseDownloader` 泛型封装，**一般无需改**（注册后自动出现）。  
GUI：下载器下拉来自注册表；Settings 来自 `_ui_*`；**一般无需改 React**。

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

### 4.2 `properties` 映射表

| InSARHub 键 | STAC 来源（优先级从上到下） | 备注 |
|---|---|---|
| `sceneName` | `ingest:provider_product_id` → 去掉首段 `provider:` 后的 `id` → 完整 `id` | 列表/下载子集主键 |
| `fileID` | `Item.id` | 保留 `asf:…` 形式 |
| `startTime` | `datetime` → `start_datetime` | **必须**能解析为 ISO；缺则该 Item 跳过并 warning |
| `stopTime` | `end_datetime` → `datetime` | |
| `pathNumber` | `sat:relative_orbit` → `pathNumber` → `relativeOrbit` | int 化 |
| `frameNumber` | `frameNumber` → `asfFrame` | 无则见 §4.3 |
| `flightDirection` | `sat:orbit_state` | `ascending`/`descending` → `ASCENDING`/`DESCENDING` |
| `platform` | `platform` → `constellation` | |
| `polarization` | `sar:polarizations` | list → `VV+VH` 风格 join |
| `processingLevel` | `sar:product_type` → `processing:level` → `"SLC"` | |
| `bytes` | 主 asset `file:size` | |
| `fileName` | 主 asset `href` basename | 与 stac-ingest「保留提供方原名」一致 |
| `url` | 主 asset 绝对 `href` | 下载入口 |
| `collection` | Item `collection` | |
| `_stac_item_id` | `id` | 内部 |
| `_checksum` | 主 asset `file:checksum` | Multihash；可选校验 |

`geometry` ← Feature.`geometry`。

### 4.3 Stack 分组键

```text
若 pathNumber 与 frameNumber 均可得 → (pathNumber, frameNumber)
否则若仅 pathNumber → (pathNumber, "unknown")
否则 → ("local", collection_id 或 "default")
```

与 ASF 的 `(path, frame)` 打印习惯兼容；`filter(path_frame=…)` 对 `"unknown"` / `"local"` 键按字符串匹配。

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

## 6. 配置与 UI

```python
@dataclass
class STAC_Base_Config:
    name: str = "STAC_Base_Config"
    stac_api_url: str = "http://127.0.0.1:8082"
    collections: list[str] | None = field(
        default_factory=lambda: ["sentinel-1-slc"]
    )
    intersectsWith: str | None = None   # WKT；内部转 GeoJSON 填 intersects
    start: str | None = None
    end: str | None = None              # 纯日期 → 当日 23:59:59（对齐 ASF）
    maxResults: int | None = 100
    granule_names: str | list[str] | None = None
    asset_roles: list[str] = field(default_factory=lambda: ["data"])
    verify_checksum: bool = False
    max_workers: int = 3
    ssl_verify: bool = True
    # 鉴权（均勿写入仓库默认值/示例密钥）
    auth_token: str | None = None       # → Authorization: Bearer …
    basic_user: str | None = None
    basic_password: str | None = None
    workdir: Path | str = field(default_factory=lambda: Path.cwd())

    _ui_groups = [
        {"label": "STAC", "fields": [
            "stac_api_url", "collections", "verify_checksum",
            "max_workers", "ssl_verify", "maxResults",
        ]},
    ]
    # _ui_fields: text / bool / number / auto_number …
```

`STAC_API_Config(STAC_Base_Config)`：`name = "STAC_API_Config"`。

`search_filter_schema`（叶子类上）：

| name | kind | 说明 |
|---|---|---|
| `flightDirection` | select ASCENDING/DESCENDING | 优先 API `query`；否则本地过滤 |
| `relativeOrbit` / path | range 或 text | 同上 |
| frame | range 或 text | 同上 |

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

MVP：**显式不支持**。

```python
def select_pairs(self, *args, **kwargs):
    raise NotImplementedError(
        "STAC_API does not provide ASF baseline stacks; "
        "pair selection will be added when perpendicular baseline is available."
    )
```

若 GUI 硬编码调用，需在联调时确认路由对 `NotImplementedError` 的展示（实现阶段检查 `search.py` / processor 入口）。

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
| `_format_item` 样例 JSON | `sceneName` / `pathNumber` / `url` / `flightDirection` |
| mock `POST /search` | `results` 分组键、`active_results` |
| mock asset `GET` | 文件落在 `…/slc/<basename>` |
| 缺 `datetime` | Item 被跳过 |
| `file://` href | download 拒绝 |
| 注册 | `"STAC_API" in Downloader._registry` |
| 配置 UI 字段 | `_ui_fields` 键 ⊆ dataclass 字段（沿用 tier2 惯例） |

命令：`pytest -m "basic or regression"`（含新文件）。  
**不**把真 Docker 当 CI 门禁。

---

## 10. 对上游 PR 的范围（日后）

| 纳入 PR | 不纳入 |
|---|---|
| `stac_base.py` / `stac_api.py` / config / paths / tests / CHANGELOG / advanced 文档 | `.cursor/` |
| 可选：精简版设计说明链到 `docs/` | `docs/design/refs/stac-ingest-design_v2.zh.md` 全文（外部仓设计，建议 PR 前移出或改为短链接说明） |

---

## 11. 实现顺序（仅在方案确认后执行）

1. Config + `STACPaths` + `STACProduct` + `_format_item` + 单测  
2. `_load_items`（分页）+ `search` / `filter` / `reset` / `summary`  
3. `download` + checksum 可选  
4. `STAC_API` 注册 + footprint（最小）  
5. 文档 + CHANGELOG  
6. `pytest -m "basic or regression"`  

---

## 12. 待你确认的清单

请逐条回复 **同意 / 修改意见**：

1. **注册名** `STAC_API`、**仅 requests**、默认 Collection `sentinel-1-slc` — 维持？  
2. **落盘**采用 `p{path}_f{frame}/slc/`（`StackPaths` 风格）— 同意？  
3. **MVP 不支持** `select_pairs` 与 `merge=True` — 同意？  
4. **`verify_checksum` 默认 False** — 同意？  
5. **占位 API** `http://127.0.0.1:8082` — 是否改为你预期的反代路径（如 `http://127.0.0.1:36610/stac`）？  
6. **PR 时是否保留** `docs/design/refs/stac-ingest-design_v2.zh.md`，还是仅留 InSARHub 本方案、refs 留本机？  
7. 其它：鉴权只要 Bearer + basic 是否足够？

---

**请确认或批注本 v0.3。** 收到「确认方案，开写代码」之前，**不修改** `src/` 业务代码。
