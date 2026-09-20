# STAC_API 下载器设计

| | |
|---|---|
| 地位 | InSARHub 侧 STAC 扩展的唯一设计 |
| 依据 | [后端贡献指南](https://jldz9.github.io/InSARHub/v0.4.2/zh/contributing/backend/)、`CONTRIBUTING.md` |
| 存储端对照 | [`refs/stac-ingest-design_v2.zh.md`](refs/stac-ingest-design_v2.zh.md)，不在本仓库实现 |

InSARHub 增加一个下载器，通过标准 STAC API 检索场景，并把 Asset 下载到现有工作目录。存储服务由另一项目提供。本仓库不实现 STAC Server，也不另建客户端。

---

## 1. 边界

存储服务器与计算服务器分开。InSARHub 只通过 HTTP(S) 访问 STAC API 和 Asset `href`，不挂载对方磁盘，不读 PgSTAC，不写 Catalog。

```text
存储服务器                         计算服务器
stac-fastapi-pgstac                InSARHub
Asset HTTP  ←── href ──  STAC_API Downloader
                              │
                              ▼
                         config.workdir
                         StackPaths …/slc/
```

| 做 | 不做 |
|---|---|
| `STAC_API` 下载器：检索、过滤、下载 | 新容器、新进程、新 Python 包 |
| 结果放进 `active_results` | 新 FastAPI 路由 |
| 文件写入现有 workdir | SQLite、任务状态机、独立缓存目录 |
| 沿用 CLI、Config、`StackPaths` | 修改 `BaseDownloader`，或继承 `ASF_Base_Downloader` |
| `gui_hidden = True` | 上架 GUI、`select_pairs`、`merge` |
| tier2 测试与 `[Unreleased]` | 新依赖（HTTP 用已有 `requests`） |

`STAC_Base` 不是必选项。只有第二个 STAC 下载器要共用协议代码时才抽出无 `name` 的基类。当前只实现 `STAC_API`。

---

## 2. 代码位置

```text
src/insarhub/downloader/stac_api.py     # name = "STAC_API"
src/insarhub/downloader/__init__.py     # import，否则不注册
src/insarhub/config/defaultconfig.py    # STAC_API_Config
src/insarhub/config/__init__.py         # 导出
test/tier2_basic/test_stac_api.py
docs/advanced/downloader.md
docs/advanced/downloader.zh.md
CHANGELOG.md                            # [Unreleased]
```

不改 `core/base.py`、`app/routes/` 和前端。CLI 已通过 `commands/downloader.py` 调用任意下载器。

`STAC_API` 实现六个抽象方法：`search`、`download`、`filter`、`footprint`、`summary`、`reset`。`select_pairs` 与 `merge=True` 抛出 `NotImplementedError`：STAC Item 不保证垂直基线，不能假装与 ASF 配对等价。

```python
class STAC_API(BaseDownloader):
    name = "STAC_API"
    gui_hidden = True
    default_config = STAC_API_Config
```

没有 Processor 声明 `compatible_downloader = "STAC_API"` 时必须隐藏。否则 `test_gui_offers_only_downloaders_a_processor_can_consume` 失败，界面也会在下载之后无路可走。

---

## 3. 规范

| 规范 | 版本 |
|---|---|
| STAC Item / Collection | 1.1.0 |
| STAC API | 1.0.0 |
| File Extension | 2.1.0（仅当 Item 声明了才读 `file:size` / `file:checksum`） |

SAR、Satellite、Processing 等扩展同样只消费服务端已经声明的字段。不假设 CQL2、Query、Sort、Fields。

`stac_api_url` 是 Landing Page，不是 `/search`。客户端先 `GET` 该地址，再从 `links` 里取 `rel=search` 与 `rel=conformance`（或落地页上的 `conformsTo`）。搜索地址、方法和 body 以 Link 为准，不把 `/search` 写死。

---

## 4. 检索

`POST`（或 Link 指定的方法）Item Search。请求只使用 STAC API 核心字段：

| 配置 | 请求字段 |
|---|---|
| `collections` | `collections`，默认 `["sentinel-1-slc"]` |
| `intersectsWith`（WKT） | 转为 GeoJSON 后放入 `intersects`；不把 WKT 原文发出去 |
| `bbox` | `bbox`。与 `intersectsWith` 同时存在时只用后者 |
| `start` / `end` | `datetime`，规范为 UTC RFC3339。纯日期的 `end` 取当日 `23:59:59Z` |
| `granule_names` | `ids`。兼容 `asf:…` 与裸 id |
| `maxResults` | 累计上限。单页 `limit` 不超过服务端允许值 |

分页只跟随响应里 `rel=next` 的 Link，沿用其中的 method、href、headers、body。不自增页码。同一 href 重复出现，或页数超过上限，则停止并报错。

无 `datetime` 的 Item 跳过。`stac_api_url` 为空时，`search()` 直接失败。

`search()` 清空 `_subset`，把结果写入：

```python
self.results: dict[tuple, list]   # 与现有下载器相同
```

`active_results` 在有 `_subset` 时返回子集，否则返回 `self.results`。

---

## 5. 场景对象

每个 Item 变成带 `.properties` 和 `.geometry` 的对象，供现有摘要、覆盖和 `_to_geojson` 使用。原始 Feature 可留在对象上，不另存文件。

无 `startTime` 或无合格主 Asset 的 Item 不进入结果。

### 5.1 属性

| `properties` | 来源 | 缺失时 |
|---|---|---|
| `sceneName`、`fileID` | `Item.id` | 跳过该 Item |
| `startTime` | `datetime`，否则 `start_datetime` | 跳过 |
| `url` | 主 Asset 的绝对 `http`/`https` `href` | 跳过 |
| `fileName` | 该 `href` 的 basename | 与 `url` 同时要求 |
| `collection` | `collection` | 可空 |
| `stopTime` | `end_datetime` | 可空 |
| `bytes` | 主 Asset `file:size` | 可空 |
| `pathNumber` | `sat:relative_orbit`，否则已有 `pathNumber` | 可空 |
| `frameNumber` | 仅当 Item 自己带了 `frameNumber` | 可空；不把别的字段假装成 frame |
| `flightDirection` | `sat:orbit_state` → `ASCENDING` / `DESCENDING` | 可空 |
| `platform` | `platform` | 可空 |
| `polarization` | `sar:polarizations` | 可空 |

`geometry` 使用 Item 的 GeoJSON geometry。

### 5.2 分组

```text
path 与 frame 都有  → (pathNumber, frameNumber)
只有 path           → (pathNumber, "unknown")
都没有              → ("local", collection 或 "default")
```

这个键只用于 InSARHub 内部分组和目录，不写回 STAC。

### 5.3 主 Asset

1. `roles` 含配置的 `asset_key`（默认 `data`）；
2. 否则取第一个角色不是 `thumbnail`、`overview`、`visual` 的 Asset。

`href` 必须是 `http` 或 `https`。`file://`、`s3://` 和相对路径都拒绝。客户端不自己拼接 `/assets/`。

---

## 6. 下载

`download()` 遍历 `active_results`（或 `scenes=` 子集），把主 Asset 写到已有路径：

```text
StackPaths(workdir).stack_dir(path, frame) / "slc" / <fileName>
```

退化键的目录仍由 `config/paths.py` 的现有方法生成，不在下载器里写 `workdir / "stac_cache"`。

| 情况 | 行为 |
|---|---|
| 目标已存在且与 `file:size` 一致 | 跳过 |
| `verify_checksum=True` 且有 SHA-256 Multihash（`1220` + 64 位十六进制） | 不一致则删除并视为失败 |
| 无 `file:checksum` | 不阻断下载 |
| 传输中断 | 不把半截文件留在正式文件名上 |

`verify_checksum` 默认 `False`。并发数用配置里的 `max_workers`。不建缓存表，不建任务库。场景身份已经在 `properties` 和 `insarhub_config.json` 的 downloader 配置里。

---

## 7. 过滤、覆盖、摘要

`filter()` 在已有结果上按 path、frame、飞行方向缩小集合，写入 `_subset`。这些条件不默认发给 STAC。`reset()` 丢掉 `_subset`。

`summary()` 按栈打印数量、日期和方向。`footprint()` 用 `geometry` 产出与现有下载器相同用途的覆盖，不引入新的出图依赖。

---

## 8. 配置

`STAC_API_Config` 放在 `defaultconfig.py`，并从 `config/__init__.py` 导出。字段形状与现有配置相同：

```python
_ui_groups = [{"label": "STAC", "fields": [
    "stac_api_url", "collections", "max_workers", "ssl_verify", "verify_checksum",
]}]
_ui_fields = {
    "stac_api_url": {"type": "text", "hint": "STAC API Landing Page"},
    "max_workers": {"type": "number", "min": 1, "max": 16},
    "ssl_verify": {"type": "bool"},
    "verify_checksum": {"type": "bool"},
}
```

| 字段 | 默认 | 说明 |
|---|---|---|
| `stac_api_url` | `""` | Landing Page。空则不能搜索 |
| `collections` | `["sentinel-1-slc"]` | |
| `intersectsWith` | `None` | WKT，与其它下载器相同 |
| `start` / `end` | `None` | |
| `maxResults` | `100` | |
| `granule_names` | `None` | |
| `asset_key` | `"data"` | 主 Asset 的 role |
| `verify_checksum` | `False` | |
| `max_workers` | `3` | |
| `ssl_verify` | `True` | |
| `auth_token` | `None` | `Authorization: Bearer`。不写入仓库 |
| `workdir` | 当前目录 | 与其它下载器相同 |

即使 GUI 隐藏，这些字段也要齐全，供 CLI 使用。本期不改 React。

---

## 9. 访问安全

Asset 主机必须与 `stac_api_url` 相同，或落在配置允许的主机列表里。跳转后仍做同样检查，并限制跳转次数。Token 不写入日志。

计算容器里的 `127.0.0.1` 指向自己。存储服务在另一台机器上时，`stac_api_url` 必须是那台机器的地址。

---

## 10. 测试与交接

| 层级 | 内容 |
|---|---|
| `tier2_basic` | 注册、`gui_hidden`、字段映射、分组、mock 搜索与分页、拒绝 `file://`、落盘路径 |
| `tier3_e2e` | 不默认运行。存储端可用后再做手工联调 |
| `tier4_regression` | 仅在修复具体缺陷时按缺陷补一条 |
| 合并前 | `pytest -m "basic or regression"` |

开发在 `feat/stac-api`。一次提交一件事。用户可见的行为写入 `CHANGELOG.md` 的 `[Unreleased]`，并更新 `docs/advanced/downloader.md` 与中文版。向 `main` 开 PR。PR 不包含 `.cursor/`，也不包含存储端设计全文。

实现顺序：

1. 配置、注册、`gui_hidden`、六个方法的可导入骨架；
2. 检索、分页、`active_results`；
3. 下载到 `StackPaths` 的 `slc/`；
4. tier2 与文档、CHANGELOG。
