# InSARHub 本地 STAC 数据源扩展详细设计说明书

**文档版本：V1.0（现行唯一设计基线）**  
**状态：取代并废除 `local-stac-downloader.zh.md`（v0.4.1）**  
**项目性质：InSARHub Fork 二次开发**  
**目标：在原 InSARHub 基础上增加标准 STAC API 数据源支持**  
**计算端部署：Docker**  
**STAC 服务端：独立部署于存储服务器的 Docker STAC 平台**  
**部署拓扑：存储服务器与 InSAR 计算服务器为两台独立设备**

---

## 1. 文档定位

本文是 InSARHub 本地 STAC 数据源扩展的**正式实施设计、开发交接、联调和验收基线**。


> 本文是 InSARHub 侧 STAC 扩展的唯一设计。v0.4.1 短方案已废除。`refs/stac-ingest-design_v2.zh.md` 只作存储端对照，不是第二套实现方案。
>
> 相对已废除稿仍须遵守、而正文图示容易看漏的约束：
>
> 1. **MVP 不含 ISCE2/MintPy 联通。** 文中数据流图画到处理器，是目标拓扑；当前验收停在下载、校验与 Cache。`gui_hidden=True`，不实现 `select_pairs` / `merge`。
> 2. **路径只用 `config/paths.py`。** Cache 目录由数据类给出，禁止在下载器里硬编码 `workdir / "cache"`。Cache 不是现有处理器的 `p{path}_f{frame}/slc/` 布局；对接处理器时另定拷贝/链接规则，不在 MVP 假装已对齐。
> 3. **注册与测试。** 叶子类设 `name`；`downloader/__init__.py` 必须 import；`gui_hidden=True` 否则 `test_gui_offers_only_downloaders_a_processor_can_consume` 失败。行为测试放 `test/tier2_basic/`，不新增依赖（HTTP 用已有 `requests`）。
> 4. **`sceneName`。** 本文映射为 `Item.id`。若 id 带 `asf:` 前缀，下载文件名仍用 Asset basename，不要把带前缀的 id 当成本地文件名。


本项目不是重新开发 STAC Server，也不是新增一个独立 STAC Client 服务，而是在已有 InSARHub Fork 中增加一个新的数据源适配器：

```text
原 InSARHub
    +
STAC API 数据源适配
    =
InSARHub + STAC
```

项目最终形成：

```text
存储服务器
    └── STAC 服务
          ├── STAC API
          ├── PgSTAC
          └── Asset HTTP

计算服务器
    └── InSARHub Docker
          ├── 原有 Downloader / Processor
          └── STAC_API 数据源
```

InSARHub 只消费标准 STAC API，不感知存储服务器的物理文件系统。

---

# 2. 规范基线

本项目必须区分 STAC Core 与 STAC API 两套规范版本：

| 规范 | 本项目基线 |
|---|---|
| STAC Core / Item / Collection | **1.1.0** |
| STAC API | **1.0.0** |
| STAC File Extension | **2.1.0** |
| STAC SAR Extension | 按服务端实际声明版本 |
| STAC Satellite Extension | 按服务端实际声明版本 |
| Processing Extension | 仅在服务端实际提供时消费 |

STAC 1.1.0 是当前 STAC Core 稳定版本；STAC API 当前稳定发布为 1.0.0。STAC 官方强调 Catalog、Collection、Item 的标准对象与 API 可以组合使用，并允许扩展。

本项目不把某个 Extension 当成“永远存在”。客户端应优先读取服务端的 `stac_extensions` / `conformsTo`，仅使用服务端明确声明并且客户端支持的能力。

---

# 3. 项目目标

## 3.1 功能目标

新增 `STAC_API` Downloader，使 InSARHub 支持：

1. 通过 HTTP(S) 访问 STAC API Landing Page；
2. 自动发现 Search Endpoint；
3. 查询 Collection；
4. 使用标准 Item Search；
5. 支持：
   - `collections`
   - `bbox`
   - `intersects`
   - `datetime`
   - `ids`
6. 跟随标准分页 Link；
7. 解析标准 STAC Item；
8. 根据 Asset 语义选择下载资产；
9. 通过 Asset `href` 下载；
10. 在计算服务器创建本地缓存；
11. 校验文件大小；
12. 校验 SHA-256 Multihash；
13. 支持断点续传；
14. 支持有限重试和退避；
15. 将下载结果转换成现有 InSARHub 数据对象；
16. 保存 STAC Item / Asset 来源信息，便于后续 InSAR 任务追踪。

---

## 3.2 非目标

本阶段明确不做：

| 不做 | 原因 |
|---|---|
| STAC Server | 已由另一项目提供 |
| PgSTAC 访问 | InSARHub 只通过 STAC API |
| Catalog 文件读取 | 跨服务器部署不应依赖文件共享 |
| `file://` 远程资产 | 计算服务器无法访问存储服务器本地路径 |
| 直接挂载存储服务器 DATA_ROOT | 破坏服务边界 |
| Transactions | InSARHub 是消费者，不写 Catalog |
| STAC Item 修改 | Catalog 生命周期属于存储端 |
| STAC 数据生产 | 属于 stac-ingest |
| 直接操作 S3 | 存储实现属于 STAC 服务端 |
| 新建独立 STAC Client 容器 | STAC Client 是 InSARHub 内部模块 |
| Redis/Celery/Airflow | 当前部署不需要分布式任务系统 |
| 重写 `select_pairs` | 当前 STAC 数据契约不足以复制 ASF 配对语义 |
| GUI 上架 | 当前无兼容 Processor，先保持隐藏 |

---

# 4. 总体部署架构

## 4.1 物理拓扑

```text
                         内网 / HTTPS
                              │
                              │
            ┌─────────────────▼─────────────────┐
            │            存储服务器              │
            │                                   │
            │  Docker STAC Platform             │
            │                                   │
            │  ┌─────────────────────────────┐  │
            │  │ STAC API                    │  │
            │  │ stac-fastapi-pgstac         │  │
            │  └──────────────┬──────────────┘  │
            │                 │                 │
            │          ┌──────▼──────┐          │
            │          │   PgSTAC    │          │
            │          └─────────────┘          │
            │                                   │
            │  ┌─────────────────────────────┐  │
            │  │ Asset HTTP Gateway           │  │
            │  │ /assets/...                  │  │
            │  └──────────────┬──────────────┘  │
            │                 │                 │
            │                 ▼                 │
            │       /mnt/diskrsdata             │
            │       遥感数据文件                 │
            └─────────────────┬─────────────────┘
                              │
                              │ HTTP(S)
                              │ STAC API
                              │ Asset href
                              │
            ┌─────────────────▼─────────────────┐
            │            计算服务器              │
            │                                   │
            │  Docker InSARHub                 │
            │                                   │
            │  ┌─────────────────────────────┐  │
            │  │ 原 InSARHub                 │  │
            │  │                             │  │
            │  │ + STAC_Base                 │  │
            │  │ + STAC_API                  │  │
            │  │ + Cache                     │  │
            │  │ + Downloader                │  │
            │  └──────────────┬──────────────┘  │
            │                 │                 │
            │                 ▼                 │
            │              ISCE2                 │
            │                 │                 │
            │                 ▼                 │
            │              MintPy                │
            └───────────────────────────────────┘
```

---

# 5. 系统边界

## 5.1 存储服务器

负责：

- 数据文件保存；
- Collection 管理；
- Item 管理；
- Asset 元数据；
- STAC Search；
- STAC Item API；
- Asset HTTP 访问。

## 5.2 InSARHub

负责：

- STAC API 发现；
- STAC Search；
- Item 解析；
- Asset 选择；
- 文件下载；
- 本地缓存；
- 完整性校验；
- 转换为 InSARHub 场景对象；
- 提供给 Processor。

## 5.3 严格禁止

InSARHub 不得：

```text
访问 /mnt/diskrsdata
访问 PgSTAC
读取 PostgreSQL
猜测 Asset URL
自己拼接 /assets/
修改 STAC Item
使用 file://
```

---

# 6. STAC Server 端契约

存储服务器必须提供以下能力。

## 6.1 Landing Page

提供标准 STAC API Landing Page：

```http
GET /
```

客户端必须首先获取 Landing Page。

客户端从：

```json
{
  "links": [
    {
      "rel": "search",
      "href": "...",
      "method": "POST"
    }
  ]
}
```

发现 Search Endpoint。

**客户端不得默认假设 `/search` 一定存在。**

在当前 stac-fastapi 部署中通常可能是 `/search`，但这属于实际服务部署结果，不应作为客户端协议硬编码。STAC API 通过标准 links 描述 API 资源与能力。

---

## 6.2 Collections

客户端使用：

```http
GET /collections
```

或者从 Landing Page / API links 发现 Collections Endpoint。

默认目标：

```text
sentinel-1-slc
```

但该 Collection 是业务约定，不属于 STAC 固定标准。

---

## 6.3 Item

服务端应返回合法 STAC Item：

```text
type = Feature
stac_version = 1.1.0
id
geometry
properties
assets
links
```

Item 是 STAC 中描述原子数据对象的核心对象。

---

# 7. Asset HTTP 访问设计

## 7.1 Asset href

Asset 必须提供：

```text
http://...
```

或：

```text
https://...
```

计算服务器直接 GET 该 URI。

正确：

```text
https://stac-storage.example.local/assets/rs_raw/sar_raw/S1A_xxx.zip
```

禁止：

```text
file:///mnt/diskrsdata/...
/mnt/diskrsdata/...
s3://bucket/...
```

原因：

Asset 的 `href` 应是客户端可解析的资产 URI；当前为跨服务器部署，必须使用计算服务器可访问的 HTTP(S) URI。STAC Asset 是 Item 的标准资产对象，使用 URI 指向实际数据。

---

## 7.2 Asset Gateway

Asset Gateway 属于存储服务器，不属于 InSARHub。

可以采用：

- Nginx；
- Caddy；
- 现有 Web 文件服务；
- 其他只读 HTTP 服务。

推荐统一暴露：

```text
https://stac-storage.example.local/

    /search
    /collections/...
    /assets/...
```

Asset Gateway 必须：

- 只读；
- 支持大文件；
- 支持 Range；
- 正确返回 `Content-Length`；
- 支持流式传输；
- 不允许目录遍历；
- 不允许访问 `_state`；
- 不允许访问 `_reports`；
- 不允许访问数据库文件；
- 不允许软链接逃逸；
- 仅映射允许的 DATA_ROOT 子树。

---

# 8. InSARHub 代码结构

基于已有 InSARHub Fork 增量修改。

推荐：

```text
src/insarhub/
└── downloader/
    ├── stac_base.py
    └── stac_api.py
```

配置：

```text
src/insarhub/config/defaultconfig.py
src/insarhub/config/__init__.py
src/insarhub/config/paths.py
```

注册：

```text
src/insarhub/downloader/__init__.py
```

测试：

```text
test/tier2_basic/test_stac_base.py
test/tier2_basic/test_stac_api.py

test/tier2_basic/fixtures/stac/
```

文档：

```text
docs/advanced/downloader.md
docs/advanced/downloader.zh.md
```

---

# 9. 类设计

```text
BaseDownloader
      │
      ▼
STAC_Base
      │
      ▼
STAC_API
```

## 9.1 STAC_Base

负责：

- STAC API discovery；
- Search request；
- Pagination；
- Item 解析；
- Asset 选择；
- `STACProduct`；
- 通用下载流程；
- 本地 Filter。

## 9.2 STAC_API

负责：

- `name = "STAC_API"`；
- HTTP Client；
- 认证配置；
- InSARHub Downloader 注册；
- 与 InSARHub CLI 接口兼容。

---

# 10. STACProduct

建议：

```python
@dataclass
class STACProduct:
    item_id: str
    collection: str | None
    geometry: dict | None
    properties: dict
    assets: dict
    raw_item: dict
```

原则：

> 不要把原始 STAC Item 转成一份“自定义 JSON 后再丢掉原数据”。

必须保留：

```text
raw_item
assets
properties
```

这样未来增加 STAC Extension 不需要重构数据层。

---

# 11. Search 设计

## 11.1 请求

标准：

```http
POST {search_href}
Content-Type: application/json
```

示例：

```json
{
  "collections": [
    "sentinel-1-slc"
  ],
  "bbox": [
    118.5,
    31.8,
    119.2,
    32.3
  ],
  "datetime": "2025-01-01T00:00:00Z/2025-06-30T23:59:59Z",
  "limit": 100
}
```

---

## 11.2 搜索参数映射

| InSARHub | STAC API |
|---|---|
| Collection | `collections` |
| bbox | `bbox` |
| WKT | 转换为 GeoJSON `intersects` |
| start/end | `datetime` |
| scene ID | `ids` |
| page size | `limit` |

---

# 12. 时间处理

内部时间必须规范化为：

```text
UTC
RFC3339
```

例如：

```text
2025-01-01T00:00:00Z
```

时间区间：

```text
2025-01-01T00:00:00Z/2025-06-30T23:59:59Z
```

禁止：

```text
2025/01/01
2025-01-01 00:00:00
```

直接作为 STAC datetime。

---

# 13. 空间查询

如果 InSARHub 使用：

```text
intersectsWith = WKT
```

则：

```text
WKT
 ↓
Shapely
 ↓
GeoJSON Geometry
 ↓
STAC intersects
```

不允许把 WKT 原文发送给：

```text
intersects
```

---

# 14. Pagination

这是必须严格实现的能力。

禁止：

```python
page += 1
```

禁止：

```text
自行猜 page 参数
```

必须读取响应：

```json
{
  "links": [
    {
      "rel": "next",
      "href": "...",
      "method": "POST",
      "headers": {},
      "body": {}
    }
  ]
}
```

并使用下一页 Link。

STAC API 1.0.0 的分页机制通过 `next` Link 表达下一页，相关请求方法、Header 和 Body 可以随 Link 一起提供。

---

## 14.1 防死循环

客户端维护：

```text
visited_request_signature
```

若 next 请求与已访问请求重复：

```text
STAC-015 pagination_loop
```

同时限制：

```text
MAX_PAGES
```

推荐默认：

```text
1000
```

超过限制：

```text
STAC-016 pagination_limit
```

---

# 15. Search 响应验证

必须检查：

```text
HTTP 2xx
Content-Type
JSON
type
features
links
```

如果：

```text
type != FeatureCollection
```

失败。

Feature 至少检查：

```text
type = Feature
id 非空
properties 存在
assets 存在
```

---

# 16. Asset 选择策略

## 16.1 第一优先级：显式 asset_key

配置：

```yaml
asset_key: data
```

则：

```text
assets[data]
```

必须存在。

不存在：

```text
STAC-007 asset_not_found
```

---

## 16.2 第二优先级：roles=data

如果没有显式 key：

```text
roles contains data
```

如果：

```text
1 个
```

直接使用。

如果：

```text
>1 个
```

报：

```text
STAC-008 ambiguous_asset
```

禁止随机选择。

---

## 16.3 第三优先级：单一非预览 Asset

仅当：

```text
没有 data role
```

且排除：

```text
thumbnail
overview
visual
```

之后只剩一个 Asset，才允许使用。

否则：

```text
ambiguous_asset
```

---

# 17. Asset URI 安全校验

下载前：

```text
parse URL
    ↓
scheme check
    ↓
hostname check
    ↓
port check
    ↓
allowlist check
    ↓
download
```

只允许：

```text
http
https
```

---

# 18. SSRF 防护

配置：

```yaml
asset_allowed_hosts:
  - stac-storage.example.local
  - 192.168.10.20
```

禁止 Asset 指向：

```text
127.0.0.1
localhost
0.0.0.0
169.254.169.254
```

或其他非允许目标。

原因：

STAC Item 是外部输入。

因此客户端绝不能：

```text
看到 href
 →
无条件 requests.get()
```

---

# 19. Redirect 安全

下载：

```text
allow_redirects=False
```

客户端自行处理 Redirect。

每次跳转重新检查：

- scheme；
- hostname；
- port；
- allowlist。

最大：

```text
5
```

超过：

```text
STAC-017 redirect_limit
```

防止：

```text
可信 Asset Host
     ↓
恶意 Redirect
     ↓
任意内网地址
```

---

# 20. 认证

STAC API 与 Asset 可以使用：

```text
Bearer
Basic
```

但认证目标必须分别控制。

配置：

```yaml
stac:
  auth:
    type: bearer

asset:
  auth:
    type: bearer
```

不要自动把 STAC Token 转发给未知 Asset Host。

Basic：

> 只允许 HTTPS。

生产环境：

```text
ssl_verify = true
```

禁止作为生产默认：

```text
ssl_verify = false
```

---

# 21. Download 设计

下载流程：

```text
STAC Item
    ↓
Asset
    ↓
Cache lookup
    ↓
Cache Hit?
   ├── Yes → verify → use
   └── No
          ↓
       download
          ↓
        .part
          ↓
        size
          ↓
      checksum
          ↓
     atomic rename
          ↓
       READY
```

---

# 22. 本地缓存

由于：

```text
存储服务器 ≠ 计算服务器
```

计算服务器必须提供本地 Cache。

推荐：

```text
/data/

├── cache/
│   └── stac/
├── work/
└── output/
```

Docker Volume：

```yaml
volumes:
  - ./data/cache:/data/cache
  - ./data/work:/data/work
  - ./data/output:/data/output
```

---

# 23. Cache 目录结构

推荐：

```text
/data/cache/stac/
└── <collection>/
    └── <safe-item-key>/
        └── <asset-key>/
            └── <filename>
```

`safe-item-key` 不直接使用未经处理的 Item ID。

必须防止：

```text
/
..
\
:
过长字符串
归一化碰撞
```

---

# 24. Cache 命中条件

必须同时：

```text
本地文件存在
AND
size == file:size
AND
checksum == file:checksum
```

才算：

```text
CACHE_HIT
```

仅：

```text
文件存在
```

不能算命中。

仅：

```text
文件大小相同
```

也不能算可靠命中。

---

# 25. 下载完整性

下载正式文件之前必须写：

```text
filename.part
```

完成之后：

```text
size verify
checksum verify
atomic rename
```

必须保证：

> 正式文件不代表“下载了一半”。

---

# 26. Checksum 设计

## 26.1 本项目不实现通用 Multihash Runtime

STAC File Extension 使用自描述 hash（Multihash）表达 `file:checksum`。本项目为了降低工程复杂度，只支持：

```text
SHA-256 Multihash
```

不引入第三方 Multihash 依赖。

---

## 26.2 SHA-256 Multihash 格式

当前支持的前缀：

```text
1220
```

含义：

```text
0x12 = SHA-256
0x20 = 32-byte digest
```

因此：

```text
1220 + 64 个十六进制字符
```

即：

```text
SHA-256 Multihash
```

---

## 26.3 客户端实现

使用 Python 标准库：

```python
hashlib.sha256()
```

解析：

```text
checksum
   ↓
检查前缀 1220
   ↓
检查总长度 68 hex
   ↓
取 digest 部分
   ↓
计算本地 SHA-256
   ↓
比较
```

不引入：

```text
multihash
multiformats
```

等第三方包。

STAC File Extension 规定 checksum 使用 self-identifying hash；本项目只是把服务端/客户端交换算法收敛到 SHA-256，以控制实现范围。

---

# 27. Checksum 默认策略

生产默认：

```text
verify_checksum = True
```

对于：

```text
sentinel-1-slc
```

服务端必须提供：

```text
file:checksum
```

否则：

```text
不允许进入 VERIFIED
```

并报告：

```text
STAC-012 checksum_missing
```

---

# 28. Size 校验

至少检查：

```text
实际 size
=
file:size
```

如果不一致：

```text
STAC-013 size_mismatch
```

注意：

> Size 是辅助校验，不替代 checksum。

---

# 29. 断点续传

存在：

```text
filename.part
```

则：

```http
Range: bytes=<current_size>-
```

如果：

```http
206 Partial Content
```

继续下载。

如果：

```http
200 OK
```

说明服务端没有按 Range 返回：

```text
删除 .part
重新完整下载
```

禁止把 200 响应直接追加到已有 `.part`。

---

# 30. 重试策略

| 情况 | 重试 |
|---|---|
| connect timeout | 是 |
| read timeout | 是 |
| connection reset | 是 |
| HTTP 429 | 是 |
| HTTP 500/502/503/504 | 是 |
| checksum mismatch | 是 |
| 401 | 否 |
| 403 | 否 |
| 404 | 否 |
| 参数错误 | 否 |
| 磁盘不足 | 否 |
| SSRF / host forbidden | 否 |

建议：

```text
MAX_RETRIES = 3
```

退避：

```text
1s
2s
4s
+jitter
```

429：

> 优先使用 `Retry-After`。

---

# 31. HTTP 超时

必须拆分：

```text
connect timeout
read timeout
```

推荐：

```text
connect = 10s
read = 3600s
```

原因：

SLC 等数据可能非常大。

禁止把：

```text
timeout = 30s
```

作为大文件默认整体超时。

---

# 32. 并发

默认：

```text
max_workers = 3
```

理由：

- 控制存储服务器压力；
- 控制计算服务器网络；
- 避免多个大文件同时争抢磁盘；
- 防止突发大量下载。

---

# 33. 磁盘空间检查

下载前：

```text
expected_size + reserve
<
free_space
```

建议 reserve：

```text
10%
```

或者：

```text
STAC_MIN_FREE_BYTES
```

过程中跌破安全阈值：

```text
停止新增下载
```

不进入无限 retry。

---

# 34. InSARHub Job 数据关联

STAC 数据下载不能只得到一个本地路径。

至少需要能够关联：

```text
InSAR Job
    ↓
STAC Collection
    ↓
STAC Item.id
    ↓
Asset key
    ↓
Asset href
    ↓
Local Cache path
```

建议 Job 元数据保存：

```text
stac_collection
stac_item_id
stac_asset_key
stac_asset_href
asset_size
asset_checksum
local_cache_path
```

如果当前 InSARHub 数据模型暂时不适合直接增加数据库字段，则至少要在运行上下文/任务产物中保存相同信息。

---

# 35. 来源追踪

对于每个实际用于 InSAR 计算的文件，必须能回答：

```text
这个文件来自哪个 STAC Item？
哪个 Collection？
哪个 Asset？
哪个 href？
下载时的 checksum 是什么？
实际文件 checksum 是什么？
```

这是以后：

- 结果复现；
- 任务追踪；
- 数据治理；

的基础。

---

# 36. 不保存永久远程路径映射

不要把：

```text
local_path
```

当成数据身份。

正确：

```text
STAC Item identity
        +
Asset identity
        +
local cache
```

本地 Cache 可以：

- 删除；
- 重建；
- 迁移；
- 清理。

STAC 来源身份不能因此改变。

---

# 37. STAC Item 身份

InSARHub 只认：

```text
Item.id
```

作为 STAC 数据对象标识。

不自己通过：

```text
filename
```

构造 Item ID。

---

# 38. InSARHub 字段映射

## 38.1 核心字段

| InSARHub | STAC |
|---|---|
| sceneName | `Item.id` |
| fileID | `Item.id` |
| url | 主 Asset `href` |
| fileName | Asset href basename |
| startTime | `properties.datetime` |
| geometry | Item geometry |
| collection | `Item.collection` |

---

## 38.2 推荐字段

| InSARHub | STAC |
|---|---|
| stopTime | `start_datetime/end_datetime` |
| bytes | `file:size` |
| checksum | `file:checksum` |
| platform | `platform` |
| polarization | `sar:polarizations` |
| flightDirection | `sat:orbit_state` |
| pathNumber | `sat:relative_orbit` |

---

# 39. frameNumber

`frameNumber` 不属于 STAC Core。

因此：

> 不把 `frameNumber` 假装成标准字段。

如果服务端确实提供：

```text
frameNumber
```

客户端可以读取自定义属性，但必须保持：

```text
STAC 原字段
+
InSARHub 内部兼容字段
```

两层分离。

---

# 40. Stack 分组

内部兼容现有 InSARHub：

```text
(pathNumber, frameNumber)
```

两者都存在：

```text
(pathNumber, frameNumber)
```

仅 path：

```text
(pathNumber, "unknown")
```

都不存在：

```text
("local", collection_id)
```

重要：

> 该 tuple 只是 InSARHub 内部数据组织方式，不写回 STAC。

---

# 41. Filter

STAC API 原生负责：

```text
collections
bbox
intersects
datetime
ids
```

InSARHub 本地可以继续提供：

```text
pathNumber
frameNumber
flightDirection
```

但不应在客户端默认假设服务端支持：

```text
CQL2
Query Extension
Sort Extension
Fields Extension
```

只有在 Landing Page `conformsTo` 明确声明并且客户端实现相应协议时，才允许使用。

---

# 42. GUI

MVP：

```python
gui_hidden = True
```

CLI：

```text
可用
```

Python API：

```text
可用
```

GUI：

```text
隐藏
```

原因：

当前没有 Processor 明确声明：

```text
compatible_downloader = "STAC_API"
```

并且：

```text
select_pairs
merge
```

尚未形成与现有 InSARHub 处理链的可靠契约。

---

# 43. select_pairs

MVP：

```python
def select_pairs(...):
    raise NotImplementedError(...)
```

理由：

当前 STAC 数据不保证存在：

```text
perpendicular baseline
temporal baseline
```

等 ASF 配对所需的信息。

因此不能伪造“与 ASF 完全等价”的配对能力。

---

# 44. merge

MVP：

```text
merge=True
```

直接拒绝。

待后续：

```text
STAC
+
Processor
+
Stack
```

契约明确之后再设计。

---

# 45. 配置设计

推荐：

```python
@dataclass
class STAC_Base_Config:
    name: str = "STAC_Base_Config"

    stac_api_url: str = ""

    collections: list[str] = field(
        default_factory=lambda: ["sentinel-1-slc"]
    )

    bbox: list[float] | None = None

    intersects: dict | None = None

    start: str | None = None

    end: str | None = None

    max_items: int = 1000

    page_limit: int = 100

    asset_key: str | None = "data"

    verify_checksum: bool = True

    max_workers: int = 3

    connect_timeout: int = 10

    read_timeout: int = 3600

    max_retries: int = 3

    ssl_verify: bool = True

    cache_dir: Path | str | None = None

    workdir: Path | str = field(
        default_factory=lambda: Path.cwd()
    )
```

---

# 46. API URL 规则

配置项：

```text
stac_api_url
```

表示：

> STAC API Landing Page URL。

例如：

```text
https://stac-storage.example.local/
```

不表示：

```text
/search
```

客户端从 Landing Page 发现：

```text
rel=search
```

---

# 47. 环境变量

建议：

```text
INSARHUB_STAC_API_URL
INSARHUB_STAC_COLLECTIONS
INSARHUB_STAC_CACHE_DIR
INSARHUB_STAC_MAX_WORKERS
INSARHUB_STAC_VERIFY_CHECKSUM
INSARHUB_STAC_VERIFY_SSL
```

敏感配置：

```text
INSARHUB_STAC_TOKEN
```

生产优先 Docker Secret。

---

# 48. Docker 部署

## 48.1 不新增 STAC Client 容器

不要：

```text
InSARHub
+
stac-client container
```

正确：

```text
InSARHub container
└── STAC_API Python module
```

---

## 48.2 InSARHub Container

必须能够访问：

```text
STAC API
Asset Gateway
```

例如：

```text
http://192.168.10.20
```

禁止：

```text
http://127.0.0.1:xxxx
```

因为 `127.0.0.1` 在计算服务器上指向 InSARHub 自己，而不是存储服务器。

---

# 49. Docker Volume

推荐：

```yaml
services:
  insarhub:
    volumes:
      - ./data/cache:/data/cache
      - ./data/work:/data/work
      - ./data/output:/data/output
```

不挂载：

```text
/mnt/diskrsdata
```

---

# 50. Cache 持久化

容器重建：

```text
Cache 不应全部丢失
```

因此：

```text
/data/cache
```

必须使用持久卷或宿主机目录。

---

# 51. Docker 网络要求

计算服务器：

```text
InSARHub Container
       │
       ├── DNS
       ├── TCP 80/443
       └── STAC Server
```

必须实际验证：

```bash
curl https://stac-storage.example.local/
curl https://stac-storage.example.local/collections
```

以及：

```bash
curl https://stac-storage.example.local/assets/...
```

---

# 52. 日志

所有关键阶段必须记录：

```text
timestamp
level
component
item_id
collection
asset_key
stage
status
duration
error_code
```

示例：

```text
INFO item_id=S1A_xxx stage=download status=started
INFO item_id=S1A_xxx stage=verify status=success
```

不得输出：

```text
Bearer token
Basic password
Cookie
Secret
```

---

# 53. Error Code

建议统一：

```text
STAC-001 invalid_config
STAC-002 api_unreachable
STAC-003 landing_page_invalid
STAC-004 search_link_missing
STAC-005 search_failed
STAC-006 invalid_item
STAC-007 asset_not_found
STAC-008 ambiguous_asset
STAC-009 unsupported_asset_scheme
STAC-010 asset_host_forbidden
STAC-011 download_failed
STAC-012 checksum_missing
STAC-013 size_mismatch
STAC-014 checksum_mismatch
STAC-015 pagination_loop
STAC-016 pagination_limit
STAC-017 redirect_limit
STAC-018 unauthorized
STAC-019 forbidden
STAC-020 disk_full
```

---

# 54. 测试架构

三层：

```text
L1 单元测试
L2 STAC API Mock 契约测试
L3 真机 Docker 跨服务器测试
```

不能只进行 Mock。

---

# 55. L1 单元测试

覆盖：

### URL

- http；
- https；
- file；
- s3；
- localhost；
- 127.0.0.1；
- 169.254.169.254；
- 允许 host；
- 不允许 host。

### Asset

- `roles=data`；
- 多 data Asset；
- 无 data；
- thumbnail；
- overview；
- visual；
- 多候选。

### 时间

- 单时间；
- 时间范围；
- UTC；
- 非法时间。

### checksum

- 合法 `1220...`；
- 长度错误；
- 前缀错误；
- digest 正确；
- digest 错误。

---

# 56. L2 STAC Mock

必须 Mock：

```text
GET /
GET /collections
POST /search
GET /collections/{id}
GET /assets/...
```

至少覆盖：

- Landing Page；
- search link；
- Collections；
- Item；
- Pagination；
- 429；
- 5xx；
- 401；
- 403；
- 404；
- 非法 JSON。

---

# 57. Pagination 测试

必须覆盖：

```text
next.href
next.method
next.headers
next.body
```

以及：

```text
循环 next
无限 next
超过 MAX_PAGES
```

必须保证：

> 不死循环。

---

# 58. Download 测试

### 正常

```text
200
→ stream
→ size
→ checksum
→ rename
```

### Range

```text
.part
→ 206
→ continue
```

### 不支持 Range

```text
.part
→ 200
→ discard
→ restart
```

### checksum 错误

```text
download
→ mismatch
→ retry
```

---

# 59. Cache 测试

| 状态 | 结果 |
|---|---|
| 文件不存在 | 下载 |
| 文件存在且 size/checksum 正确 | Cache Hit |
| size 错 | 重新下载 |
| checksum 错 | 重新下载 |
| `.part` 存在 | Resume |
| final 文件损坏 | 重新下载 |

---

# 60. SSRF 测试

以下全部必须拒绝：

```text
http://127.0.0.1/
http://localhost/
http://169.254.169.254/
http://0.0.0.0/
```

以及：

```text
允许 host
→ redirect 到 localhost
```

---

# 61. 真机联调环境

至少需要：

## 存储服务器

```text
STAC API
PgSTAC
Asset Gateway
真实 Sentinel-1 Item
真实 SLC 文件
```

## 计算服务器

```text
InSARHub Docker
```

---

# 62. 真机联调流程

```text
InSARHub
   │
   ▼
GET Landing Page
   │
   ▼
发现 Search Link
   │
   ▼
POST /search
   │
   ▼
STAC Item
   │
   ▼
Asset.href
   │
   ▼
Asset HTTP GET
   │
   ▼
/data/cache/stac
   │
   ▼
checksum
   │
   ▼
ISCE2
```

---

# 63. 真机联调必须验证的事项

### API

- [ ] Landing Page 可访问
- [ ] `conformsTo` 正常
- [ ] `rel=search` 存在
- [ ] Collection 可发现
- [ ] Search 成功
- [ ] Pagination 正常

### Asset

- [ ] href 为 HTTP(S)
- [ ] href 使用存储服务器地址
- [ ] 不出现 `127.0.0.1`
- [ ] 不出现 `/mnt/diskrsdata`
- [ ] 计算服务器可 GET
- [ ] Range 可用
- [ ] Content-Length 正确

### Download

- [ ] SLC 可完整下载
- [ ] checksum 正确
- [ ] Cache 可复用
- [ ] 失败可重试
- [ ] `.part` 可恢复

---

# 64. 原 InSARHub 回归测试

Fork 项目必须保证：

```text
原功能不受影响
```

至少回归：

- ASF Downloader；
- 现有 NISAR Downloader；
- 现有 Processor；
- Job；
- GUI；
- 配置系统；
- CLI。

必须先运行：

```bash
pytest -m "basic or regression"
```

再运行 STAC 专项测试。

---

# 65. CI 门禁

STAC 单元测试必须进入 CI。

Docker 真机测试可以不成为每次 PR 的必跑门禁，但至少要：

```text
开发阶段
→ 真机联调

发布前
→ 完整 Docker 集成测试
```

---

# 66. 兼容性设计

客户端不能依赖：

```text
stac-fastapi 内部 Python 实现
```

不能依赖：

```text
PgSTAC SQL
```

不能依赖：

```text
Docker 容器名
```

不能依赖：

```text
/mnt/diskrsdata
```

不能依赖：

```text
/assets/
```

作为协议路径。

真正的协议依赖只有：

```text
STAC API
+
STAC Item
+
STAC Asset
+
HTTP(S)
```

---

# 67. 与 STAC Ingest 的关系

两项目是生产者和消费者：

```text
                  STAC Ingest
                       │
                       │ 写入
                       ▼
                  STAC Catalog
                       │
                       │ Search
                       ▼
                InSARHub STAC_API
                       │
                       │ Download
                       ▼
                  Local Cache
                       │
                       ▼
                     ISCE2
                       │
                       ▼
                    MintPy
```

### STAC Ingest

负责：

```text
发现数据
下载/登记
生成 Item
写 Catalog
维护 Asset
```

### InSARHub STAC_API

负责：

```text
搜索
解析
下载
缓存
消费
```

两边不共享 Python import，不直接共享数据库。

---

# 68. S3 演进

未来存储服务器从：

```text
LocalStorage
```

切换：

```text
S3
```

不应要求 InSARHub 修改数据消费逻辑。

最终仍：

```text
STAC Item
   ↓
Asset.href
   ↓
HTTP(S)
```

如果服务端未来通过 HTTP(S) Signed URL 对 S3 资产提供访问：

```text
Asset.href
=
https://...
```

InSARHub 无需知道：

```text
s3://bucket/...
```

因此：

> **S3 是 STAC 服务端存储实现的演进，不是 InSARHub 的存储依赖。**

---

# 69. 不建议 InSARHub 当前引入 S3 SDK

本项目不安装：

```text
boto3
minio
s3fs
```

除非后续出现明确需求：

> InSARHub 必须直接访问 S3，而 STAC Server 无法提供 HTTP Asset。

当前架构不允许这个需求绕过 STAC API。

---

# 70. 版本与升级

每次 STAC Server 升级后至少重新执行：

```text
Landing Page
Collection
Search
Item
Asset
Checksum
Range
```

测试。

尤其要验证：

```text
Asset href
```

没有因为服务器升级重新变成：

```text
file://
127.0.0.1
/mnt/diskrsdata
```

---

# 71. 开发交接清单

开发人员接手本项目时必须拥有：

## 代码

```text
stac_base.py
stac_api.py
config
paths
tests
fixtures
docs
CHANGELOG
```

## STAC 服务信息

```text
Landing Page URL
Collection
Asset Gateway
认证
允许 Host
TLS
```

## 测试数据

至少：

```text
1 个正常 S1 SLC Item
1 个多 Asset Item
1 个 data Asset Item
1 个错误 checksum 文件
1 个分页 Search fixture
1 个 Redirect fixture
```

---

# 72. 生产上线检查

## STAC

- [ ] STAC Core 1.1.0
- [ ] STAC API 1.0.0
- [ ] Item 合法
- [ ] Collection 合法
- [ ] Asset 合法

## API

- [ ] Landing Page
- [ ] Search link
- [ ] Search
- [ ] Pagination
- [ ] Collections

## Asset

- [ ] HTTP(S)
- [ ] 可访问
- [ ] Range
- [ ] Content-Length
- [ ] checksum
- [ ] size

## Security

- [ ] Host allowlist
- [ ] Redirect allowlist
- [ ] token 不落日志
- [ ] file:// 拒绝
- [ ] localhost 拒绝

## Docker

- [ ] Cache volume 持久化
- [ ] Work volume 持久化
- [ ] Output volume 持久化
- [ ] 计算容器能访问存储服务器

## Regression

- [ ] 原 InSARHub 测试通过
- [ ] STAC 单测通过
- [ ] STAC Mock 通过
- [ ] 真机联调通过

---

# 73. P0 / P1 / P2 实施顺序

## P0：标准客户端基础

完成：

```text
STAC_API
Landing Page discovery
Search
Pagination
Item parser
Asset parser
HTTP Session
STACProduct
```

## P1：可靠下载

完成：

```text
Asset allowlist
streaming
.part
resume
size
SHA-256 Multihash
atomic rename
retry
timeout
```

## P2：InSARHub 集成

完成：

```text
Cache
Job binding
Processor input
CLI
回归测试
真机联调
```

## P3：增强

后续：

```text
GUI
select_pairs
更多 Extension
更多 Collection
```

---

# 74. 最终设计原则

## 原则一

> InSARHub 是 STAC API 消费者，不是 STAC Server。

## 原则二

> InSARHub 不感知存储服务器物理文件路径。

## 原则三

> Asset `href` 必须来自 STAC Server，而不是客户端自己拼接。

## 原则四

> 计算服务器只通过 HTTP(S) 获取远端资产。

## 原则五

> 当前 checksum 仅支持 SHA-256 Multihash，不引入第三方 Multihash 依赖。

## 原则六

> 本地 Cache 是性能和可恢复性的实现，不改变 STAC 数据身份。

## 原则七

> STAC API 的扩展能力必须通过服务端声明发现，而不是客户端假设。

## 原则八

> S3 是服务端未来存储实现，InSARHub 不直接依赖 S3。

---

# 75. 最终数据流

```text
┌────────────────────────────────────────────────────┐
│                   存储服务器                       │
│                                                    │
│  原始/成果数据                                      │
│       │                                            │
│       ▼                                            │
│  stac-ingest                                        │
│       │                                            │
│       ▼                                            │
│  PgSTAC                                             │
│       │                                            │
│       ▼                                            │
│  STAC API                                           │
│       │                                            │
│       └──────── Asset.href ──────────────┐         │
└───────────────────────────────────────────┼─────────┘
                                            │
                                            │ HTTP(S)
                                            ▼
┌────────────────────────────────────────────────────┐
│                   计算服务器                       │
│                                                    │
│  InSARHub                                            │
│       │                                            │
│       ▼                                            │
│  STAC_API                                            │
│       │                                            │
│       ▼                                            │
│  Search → Item → Asset                             │
│       │                                            │
│       ▼                                            │
│  Local Cache                                        │
│       │                                            │
│       ▼                                            │
│  Checksum / Size / Verify                           │
│       │                                            │
│       ▼                                            │
│  ISCE2                                              │
│       │                                            │
│       ▼                                            │
│  MintPy                                             │
└────────────────────────────────────────────────────┘
```

---

# 76. 最终结论

本项目的正确定位是：

> **InSARHub 的 STAC API 数据源扩展。**

不是：

> STAC Server 的二次开发。

不是：

> 独立 STAC 下载服务。

不是：

> 新的数据存储系统。

最终系统只增加一个新的数据来源：

```text
STAC_API
```

并严格遵循：

```text
STAC Core 1.1.0
+
STAC API 1.0.0
+
File Extension 2.1.0
+
HTTP(S) Asset
```

在当前“存储服务器 + 计算服务器 + Docker”的部署模式下，最关键的生产可靠性措施是：

```text
标准 API 发现
+
标准分页
+
Asset href 不重构
+
HTTP Host 安全校验
+
断点续传
+
SHA-256 Multihash
+
size 校验
+
atomic commit
+
本地 Cache
+
InSAR Job 来源追踪
+
原 InSARHub 回归测试
```

以上能力作为 V1.0 的正式开发验收基线。
