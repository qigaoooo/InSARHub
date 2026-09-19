# 技术方案：InSARHub STAC 下载器（对接本地 Docker STAC）

| 项 | 内容 |
|---|---|
| 状态 | **决策已锁定；待「开写」**（对端 STAC Docker 仍在开发） |
| 对接对象 | 用户侧 **stac-ingest** 编排的 Docker STAC（官方 `stac-fastapi-pgstac` + `/assets/`） |
| 上游设计 | `docs/design/refs/stac-ingest-design_v2.zh.md`（stac-ingest V2.0） |
| InSARHub 依据 | `CONTRIBUTING.md`、`docs/contributing/backend.md` |
| 日期 | 2026-09-19 |

## 1. 结论（相对前一版的修正）

前一版默认「读磁盘上的 `catalog.json`」**不符合**你的实际部署。正确边界如下：

| 层 | 谁负责 | InSARHub 做什么 |
|---|---|---|
| STAC API / PgSTAC | stac-ingest 的官方镜像 | **HTTP Search / 读 Item** |
| Asset `href` | 绝对 `https://…/assets/<asset_key>` | **HTTP GET/HEAD 下载**（非 `file://`、非本机硬链） |
| 入库 / Transactions | 仅 stac-ingest | **InSARHub 不写 Catalog** |
| InSAR 处理 | InSARHub | 下载后走现有 Processor |

因此 `stac_base.py` 的职责是：

1. **按 STAC API 标准检索**（官方 [STAC API](https://github.com/radiantearth/stac-api-spec)）；
2. **把 Item 格式化成 InSARHub 场景对象**（对齐现有 ASF 鸭子类型）；
3. **按 Asset `href` 拉取文件**到 workdir。

不修改、不嵌入 stac-fastapi；不直写 PgSTAC。

## 2. 对接约定（来自 stac-ingest V2）

### 2.1 规范基线

- **STAC 1.1.0**（Item / Collection / Asset）
- 动态 Catalog：**Asset `href` = 绝对 HTTP(S) URI**
- Catalog 中**不会**出现服务器绝对路径、`file://`、`s3://`
- Item 身份：`Item.id` = provider-qualified 稳定 ID（如 `asf:S1A_IW_SLC__…`）；幂等键 `(collection_id, Item.id)`
- 常用扩展：`file`（size/checksum）、`ingest:`（provider 等）、SAR 相关官方扩展字段（若 Item 已声明）

### 2.2 典型端点（本地 Docker）

以 stac-ingest 编排为准（示例，实现时用配置，不写死）：

```text
STAC_API_URL     = http://127.0.0.1:<stac-port>     # 或经反代 /stac/
ASSET_BASE_URL   = http://localhost:36610/assets/   # 只读映射；InSARHub 一般只跟 Item 里的绝对 href
```

InSARHub 配置只需：

- `stac_api_url`：API 根（含或不含尾斜杠均可，客户端规范化）
- 可选鉴权头 / 基本认证（与现场反代一致）

### 2.3 与 InSAR 相关的 Collection（首期）

| Collection（示例） | InSARHub 用途 |
|---|---|
| `sentinel-1-slc` | 主路径：SLC 检索与下载 → 后续可对接本地处理器 |
| `orbit-aux-s1` | 可选：轨道辅助（二期） |
| `sar-product-*` | 可选：成果浏览（非本下载器 MVP 重点） |

MVP 叶子下载器可固定默认 `collections=["sentinel-1-slc"]`，并允许配置覆盖。

## 3. 架构（InSARHub 侧）

```
BaseDownloader
    └── STAC_Base                 # downloader/stac_base.py（无 name）
            └── STAC_API          # downloader/stac_api.py；name="STAC_API"（已锁定）
```

贡献指南：非 ASF 存档 → 继承 `BaseDownloader`，**不**继承 `ASF_Base_Downloader`。

### 3.1 `stac_base.py` 内部结构

```text
STAC API /search
      │
      ▼
_load_items(query)          # HTTP：bbox/intersects/datetime/collections/ids/limit/token
      │
      ▼
_format_item(stac_item)     # → STACProduct（InSARHub 形状）
      │
      ▼
_get_group_key(product)     # (pathNumber, frameNumber) 或退化键
      │
      ▼
self.results: dict[tuple, list[STACProduct]]

download():
  对每个 Item 的主 asset（roles 含 data）→ HTTP GET href → workdir
```

### 3.2 `STACProduct`（InSARHub 契约）

与现有 GUI `_to_geojson` / filter / summary 对齐：

| `properties` 键 | 来源（STAC Item） |
|---|---|
| `sceneName` | 优先 `ingest:provider_product_id`；否则去掉 `provider:` 前缀的 `id`；再否则完整 `id` |
| `fileID` | 同 `Item.id`（保留 qualified id） |
| `startTime` | `properties.datetime` / `start_datetime` |
| `stopTime` | `end_datetime` |
| `pathNumber` | `sat:relative_orbit` / `pathNumber` |
| `frameNumber` | `frameNumber` / 可配置扩展字段 |
| `flightDirection` | `sat:orbit_state` → `ASCENDING`/`DESCENDING` |
| `platform` | `platform` / `constellation` |
| `polarization` | `sar:polarizations` |
| `processingLevel` | `sar:product_type` / `processing:level` |
| `bytes` | 主 asset `file:size` |
| `fileName` | 主 asset `href` 的 basename（保留提供方原名） |
| `url` | 主 asset 绝对 `href`（下载用） |
| `collection` | `collection` |
| `_stac_item_id` | `id`（内部） |
| `_checksum` | `file:checksum`（可选校验） |

`geometry` ← Item.geometry。

## 4. 配置草案

```python
@dataclass
class STAC_Base_Config:
    name: str = "STAC_Base_Config"
    stac_api_url: str = "http://127.0.0.1:8082"   # 现场可改；经反代则写公开根
    collections: list[str] | None = field(default_factory=lambda: ["sentinel-1-slc"])
    intersectsWith: str | None = None   # WKT，与 TopBar 一致 → API intersects/bbox
    start: str | None = None            # → datetime 下界
    end: str | None = None              # → datetime 上界（日末规范化，对齐 ASF 习惯）
    maxResults: int | None = 100
    granule_names: str | list[str] | None = None  # → 按 id / provider_product_id 过滤或 ids 查询
    asset_roles: list[str] = field(default_factory=lambda: ["data"])
    verify_checksum: bool = False       # 若有 file:checksum，下载后可选校验
    max_workers: int = 3
    ssl_verify: bool = True
    # 可选：auth_header / basic 用户名密码（勿写入仓库）
    workdir: Path | str = field(default_factory=lambda: Path.cwd())
```

UI：`stac_api_url`、`collections`、`max_workers`、`verify_checksum` 进 Settings；AOI/日期走现有搜索栏。

`search_filter_schema`：`flightDirection`、path/frame（映射到 STAC query 的 `query` 扩展或客户端二次过滤——**优先**用 API `query`/filter；API 不支持的字段再本地过滤）。

## 5. Search / Download 行为

### 5.1 search

1. `POST {stac_api_url}/search`（优先；若仅支持 GET 则降级）  
   Body 对齐 STAC API：`collections`、`intersects` 或 `bbox`、`datetime`、`limit`，分页 `token`/`next`
2. 每个 Feature → `_format_item` → 分组写入 `self.results`
3. `granule_names`：转为 `ids` 查询或结果集过滤（兼容 `asf:…` 与裸 scene id）

### 5.2 download

1. 解析主 asset `href`（已是绝对 HTTP(S)）
2. 流式下载到 `STACPaths(workdir).…`（经 `paths.py`，禁止硬编码）
3. 支持 `stop_event` / `on_progress`；可选 Range（与 stac-ingest `/assets/` Range 能力一致）
4. 若 `verify_checksum`：按 File Extension Multihash 校验；失败则删文件并报错

**不**把 `DATA_ROOT` 挂进 InSARHub；**不**假设能硬链到 `/mnt/diskrsdata`。

### 5.3 与 ASF 下载器的关系

| | ASF | STAC_Base |
|---|---|---|
| 检索 | asf_search | STAC API `/search` |
| 认证 | Earthdata `.netrc` | 现场反代 / 可选 API 凭证 |
| 落盘 | ASF/CDSE URL | Item Asset `href` |
| 配对 | ASF stack / 基线 | MVP：时间窗或声明限制；有 `sat:`/`sar:` 字段后再增强 |

## 6. 已锁定决策（2026-09-19）

| 项 | 决定 |
|---|---|
| 注册名 | **`STAC_API`** |
| 依赖 | **仅用已有 `requests`**（不新增 pystac-client） |
| 默认 Collection | **`sentinel-1-slc`**（可配置覆盖） |
| 对端环境 | **本机 STAC Docker 仍在开发** → InSARHub 实现以 **STAC API 规范 + mock 单测** 先行；`stac_api_url` 占位默认，联调等对端可用后再做 |
| 默认 `stac_api_url` | 占位 `http://127.0.0.1:8082`（对端定稿后改配置即可，不写死业务逻辑） |

## 7. 模块落点

| 文件 | 作用 |
|---|---|
| `src/insarhub/downloader/stac_base.py` | 检索、格式化、`STAC_Base` |
| `src/insarhub/downloader/stac_api.py` | 叶子 **`STAC_API`**（`name="STAC_API"`） |
| `config/defaultconfig.py` / `__init__.py` | `STAC_Base_Config` / `STAC_API_Config` |
| `config/paths.py` | `STACPaths` |
| `test/tier2_basic/test_stac_base.py` | mock HTTP（**不依赖**本机 Docker STAC） |
| `docs/advanced/downloader(.zh).md` | 对接说明 |
| `CHANGELOG.md` | `[Unreleased]` |

## 8. 测试与联调节奏

- **现在即可**（开写后）：tier2 mock `/search` + asset GET，锁格式化契约与分组
- **对端 STAC 可用后**：手工冒烟 `Downloader.create("STAC_API", stac_api_url=...)`
- 对端 API 路径/鉴权若与占位不符，只改 Config 默认值与文档，不改契约层

## 9. 明确不做（InSARHub 本期）

- 调用 STAC **Transactions** 写 Catalog  
- 实现 ingest / reconcile / StorageAdapter  
- 修改官方 stac-fastapi  
- 把本机绝对路径写进任何配置默认值  

## 10. 成功标准

- `Downloader.available()` 含 **`STAC_API`**
- mock 下 `search()` 产出可 `_to_geojson` 的分组；`download()` 能按绝对 `href` 落盘  
- 对端就绪后：真实 Docker STAC 冒烟通过（非合并门禁）  
- `pytest -m "basic or regression"` 通过；无新依赖  

---

**决策已锁定。** 对端 STAC 开发中不影响「先 mock 实现」。回复 **「开写」** 即按本方案编码。
