# stac-ingest

程序名：**stac-ingest**。命令行入口同名；Python 包名为 `stac_ingest`。

仓库名也是 **stac-ingest**。根目录 [`compose.yml`](compose.yml) 是本项目的安装与运行示例：拉官方预构建镜像，不从本仓库编译 STAC API。[`stac_fastapi/`](stac_fastapi/) 是上游官方对照代码，**不修改、不由此构建**。stac-ingest 经 HTTP 与该 API 兼容外挂，不嵌入、不改写官方进程。

| | |
|---|---|
| **版本** | V2.0（2026-09-19） |
| **状态** | **编码基线** |
| **角色** | stac-ingest 的唯一设计入口 |
| **编排** | [`compose.yml`](compose.yml) |
| **官方对照** | [`stac_fastapi/`](stac_fastapi/) |
| **业务代码** | [`ingest/src/stac_ingest/`](ingest/src/stac_ingest/) |

> 本版按“单服务器 + 单数据盘 + 当前本地文件系统 + 未来 S3”的实际边界设计。目标不是堆组件，而是做到：**可靠入库、可恢复、可对账、STAC 语义正确、未来可切 S3**。
>
> **STAC 基线：1.1.0。** 当前 STAC 规范的稳定版本为 1.1.0；Asset `href` 在动态 Catalog 中采用绝对 URI，当前文件系统仅作为 Storage 后端，不把服务器绝对路径写入 Catalog。

正文均为现行约定。以下原则高于历史实现：

- 不修改官方 `stac-fastapi-pgstac` 内核。
- 不直写 PgSTAC。
- 不把 cron、JSON 报告或退出码作为恢复状态。
- 不把服务器绝对路径、`file://`、`s3://` 写入 STAC Asset `href`。
- 不把文件名当 Item 身份。
- 不在本仓实现波段、InSAR、倾斜、点云、DEM 等生产算法。
- 不为了“未来分布式”提前引入 Redis、Celery、Airflow、Kafka 等中间件。

---

## 目录

1. [一页结论](#1-一页结论)
2. [系统边界](#2-系统边界)
3. [数据与 STAC 模型](#3-数据与-stac-模型)
4. [运行与可靠性](#4-运行与可靠性)
5. [配置与部署](#5-配置与部署)
6. [硬约束与安全](#6-硬约束与安全)
7. [实现顺序与验收](#7-实现顺序与验收)
8. [规范依据](#8-规范依据)

---

## 1. 一页结论

### 1.1 仓库与运行形态

仓库名 **stac-ingest**。`stac_fastapi/` 仅为官方对照，本次开发不改其源码。

日常启动使用根目录编排：

```bash
docker compose up -d --build
docker compose exec ingest stac-ingest plan /tasks/examples/local_demo.yaml
docker compose exec ingest stac-ingest sync /tasks/examples/local_demo.yaml
```

该应用使用官方镜像提供 Catalog / Search / Transactions。`ingest` 是唯一业务容器，经 HTTP 做「发现数据 → 下载/接收 → 完整性验证 → 原子落盘 → 构建 STAC Item → 入库」，不替换官方进程。

处理算法不进入本程序。

### 1.2 总体架构

```text
                  ┌──────────────────────────────┐
                  │      STAC API / PgSTAC       │
                  │  官方镜像；Search/Transactions │
                  └───────────────▲──────────────┘
                                  │ HTTP
                                  │
┌───────────────────┐     ┌──────┴────────────────────┐
│ SyncTask / CLI    │────▶│        ingest 包内模块     │
│ plan/sync/index   │     │ provider/planner/task/... │
└─────────┬─────────┘     └──────┬────────────────────┘
          │                       │
          │                       ├── SQLite 状态机
          │                       ├── LocalStorage
          │                       ├── checksum / atomic commit
          │                       └── STAC Schema / ItemBuilder
          │
          ▼
┌─────────────────────────────────────────────────────┐
│ LocalStorage: DATA_ROOT=/mnt/diskrsdata              │
│ rs_raw / rs_product                                 │
└─────────────────────────┬───────────────────────────┘
                          │
                          │ asset_key
                          ▼
                ┌──────────────────────┐
                │ 只读资产访问映射       │
                │ /assets/<asset_key>   │
                │ 由现有反向代理提供     │
                └──────────┬───────────┘
                           │
                           ▼
                  STAC Asset href
                  绝对 HTTP(S) URI

未来：LocalStorage → S3Storage
      asset_key 不变，Asset href 仍保持稳定访问 URL
```

### 1.3 四个核心事实

| 事项 | 正式约定 |
|---|---|
| 当前数据实体 | `DATA_ROOT` 下本地文件 |
| Catalog | STAC/PgSTAC 保存时空元数据、资产引用、血缘及文件完整性信息 |
| 任务恢复 | `{STATE_ROOT}/runs.sqlite`，SQLite 为唯一运行状态源 |
| 存储演进 | 上层只认 `asset_key` + Storage Adapter；未来增加 S3，不改业务状态机和 STAC 数据模型 |

### 1.4 最关键的 STAC 规则

**STAC Asset `href` 与内部 Storage Key 分离。**

内部使用：

```text
asset_key = rs_raw/sar_raw/S1A_xxx.zip
```

Catalog 写入：

```text
href = https://<stac-host>/assets/rs_raw/sar_raw/S1A_xxx.zip
```

其中 `/assets/` 是**只读资产访问映射**，而不是新的业务数据服务。单服务器环境优先由现有 Nginx/Caddy/反向代理完成；没有现成反向代理时，才增加一个极简只读静态映射基础设施。

这样同时满足：

- 动态 STAC 使用绝对 Asset href；
- 浏览器 / STAC Browser 可以访问资产；
- 不把 `/mnt/diskrsdata` 泄露到 Catalog；
- 未来切 S3 时不需要修改历史 Item 的 `href`。

STAC 官方最佳实践明确建议：动态 Catalog 使用绝对链接；当资产位于 STAC 对象之外时，Asset href 应使用绝对 URI。

---

## 2. 系统边界

### 2.1 谁做什么

| 层级 | 载体 | 职责 | 原则 |
|---|---|---|---|
| STAC API | `stac-fastapi-pgstac` 官方镜像 | Search、Transactions | **复用，不改内核** |
| 元数据库 | `pgstac` 官方镜像 | Collection / Item 持久化与检索 | **复用，不另建 Catalog DB** |
| 浏览 | `stac-browser` | 人工浏览 | 可选 |
| 业务 | `ingest/` Python 包 | Provider、任务、下载、校验、索引、对账 | **唯一业务代码区** |
| 任务状态 | `runs.sqlite` | TaskRun / SceneProgress / Event | **唯一恢复源** |
| 存储 | `LocalStorage` | 当前单机文件存储 | P0 |
| 资产访问 | 现有反向代理 / 只读静态映射 | 将 `asset_key` 暴露为 HTTP(S) URL | 基础设施，不承担业务状态 |
| 未来存储 | `S3Storage` | S3/兼容 S3 | P4 |

### 2.2 写入原则

Catalog 唯一业务写入口：

```text
ingest Indexer
    ↓
STAC Item Schema 校验
    ↓
STAC Transactions HTTP
    ↓
PgSTAC
```

禁止：

- ingest 直接 SQL 写 PgSTAC；
- 下游业务绕过 ingest 直接修改本项目管理的 Item；
- 用文件扫描结果直接覆盖 Catalog；
- 用 reconcile 自动删除 Item。

如确有人工维护需求，必须显式走管理员工具或 ingest 的管理命令，并留下审计记录。

### 2.3 包结构

```text
ingest/src/stac_ingest/
├── provider/       # ASF / EODAG / NASA 等数据源适配
├── planner/        # 搜索、过滤、去重、计划
├── task/           # SyncTask / TaskRun / SceneProgress / SQLite
├── storage/        # StorageAdapter / LocalStorage；S3 仅接口
├── download/       # resume / retry / checksum / atomic commit
├── metadata/       # SceneManifest / ProductManifest
├── stac/           # ItemBuilder / Schema / Transactions / AssetURL
├── reconcile/      # STAC ↔ Storage 双向对账
└── cli/            # plan / sync / index / reconcile / task
```

全部为同一个 Python 包，不拆业务容器。

### 2.4 明确不做

| 不做 | 原因 |
|---|---|
| 修改 `stac_fastapi/` 中的官方源码，或从本仓库 `build` 官方镜像 | 对照代码保持可同步；运行只用官方预构建镜像 |
| 自研第二套 STAC API / 元数据库 | 与官方能力重复 |
| Redis / Celery / Airflow | 单机阶段无必要；SQLite 足够 |
| 波段、InSAR、倾斜、点云、高斯、DEM 生产 | 属于下游处理域 |
| 自动下载类别 3/4/5、轨道、成果 | 当前业务约束 |
| 修改提供方原始文件名 | 破坏源数据追溯 |
| 将绝对本机路径写入 STAC | 破坏 Catalog 可移植性 |
| 将 `file://` / `s3://` 写成业务 Catalog 的规范 href | 使 Web / STAC Client 互操作性变差；统一使用稳定 HTTP(S) Asset URL |
| 把 checksum 只放 SQLite | STAC Catalog 本身也必须表达文件完整性 |
| 把 cron 当任务状态机 | 无法可靠恢复 |

---

## 3. 数据与 STAC 模型

### 3.1 STAC 版本基线

本项目 Catalog / Collection / Item 的目标规范版本统一为：

```text
STAC 1.1.0
```

当前稳定 STAC 规范为 1.1.0。

实现要求：

1. 官方 `stac-fastapi-pgstac` 镜像版本必须**显式锁定版本/摘要**，禁止 `latest` 作为生产依赖。
2. 编码前验证该版本对 STAC 1.1.0 的支持情况。
3. 如果上游当前镜像不支持 1.1.0，不得伪造 `stac_version=1.1.0`；应整体锁定到实际支持的稳定版本，并同步修改本文。
4. 同一 Collection 内保持 STAC 版本一致，不混用不同版本。

### 3.2 五类主数据

| # | 大类 | 原始 | 成果 | 自动下载原始 |
|---|---|---|---|---|
| 1 | 光学影像 | Landsat 8/9 L2、Sentinel-2 L2A 等 | 波段组合等 | 是 |
| 2 | SAR 影像 | Sentinel-1 等（如 SLC） | 干涉、形变等 | 是 |
| — | 轨道辅助 | EOF / POE / RES 等 | — | 否 |
| 3 | 倾斜摄影 | 倾斜原始 | 实景三维、DOM/DSM | 否 |
| 4 | 点云 / 高斯泼溅 | LAS / LAZ | 3DGS 等 | 否 |
| 5 | DEM | DEM 栅格 | DEM 派生成果 | 否 |

轨道属于 SAR 辅助数据，不升为第 6 类；但使用独立 Collection，例如 `orbit-aux-s1`，不得和 `sentinel-1-slc` 混在一个 Collection。

### 3.3 LocalStorage 目录

`DATA_ROOT` 为 LocalStorage 的根；现场：

```text
/mnt/diskrsdata
```

开发环境：

```text
ingest/data
```

目录约定：

```text
{DATA_ROOT}/
├── rs_raw/
│   ├── optical_raw/
│   ├── sar_raw/
│   ├── oblique_photo_raw/
│   ├── lidar_raw/
│   ├── dem_raw/
│   └── orbit_aux_raw/
└── rs_product/
    ├── optical_product/
    ├── sar_product/
    ├── oblique_photo_product/
    ├── lidar_product/          # 含高斯泼溅
    └── dem_product/            # 有成果时再创建
```

这棵树是 **LocalStorage 实现细节**，不是 STAC 数据模型本身。业务代码不得把目录树写死到 Provider、Task、Indexer 中；目录映射统一由 Storage Adapter / Collection Contract 管理。

### 3.4 Asset Key、Storage、STAC href 三层分离

这是本版最重要的数据模型设计。

#### ① Asset Key：业务稳定逻辑键

例如：

```text
rs_raw/sar_raw/S1A_IW_SLC__1SDV_20200115T102030_....zip
```

要求：

- 使用 `/`；
- 禁止 `..`、空段、反斜杠、盘符；
- 必须位于允许的 Collection 域内；
- 不包含服务器绝对路径；
- 不包含 bucket 名；
- 未来 LocalStorage → S3 时保持不变。

#### ② Storage URI：运行时定位

当前：

```text
LocalStorage + DATA_ROOT + asset_key
```

未来：

```text
S3Storage + bucket/prefix + asset_key
```

Storage URI **不写入 STAC**。

#### ③ STAC Asset href：真正的资源 URI

当前统一：

```text
https://<asset-host>/assets/<url-encoded-asset-key>
```

未来切 S3 时仍保持同一个访问入口：

```text
https://<asset-host>/assets/<url-encoded-asset-key>
```

由资产访问层在后端把请求映射到 S3。

这样 STAC Catalog 不感知底层存储介质。

STAC Asset 的 `href` 本质是指向关联数据的 URI；相对/绝对都可在规范层成立，但对动态 Catalog 和与 STAC 对象分离的资产，官方最佳实践推荐绝对 Asset href。

### 3.5 资产访问层

**不是业务服务，不负责任务和元数据。** 只做只读映射：

```text
GET  /assets/<asset-key>
HEAD /assets/<asset-key>
```

生产要求：

- 只读；
- 不允许上传、删除、目录浏览；
- 支持 Range 请求，适合大文件；
- 正确返回 `Content-Type`、`Content-Length`、`ETag` / `Last-Modified`；
- 严格拒绝路径穿越；
- 请求路径只能映射到合法 `asset_key`；
- 与 STAC API 使用同一认证边界；
- 同域部署优先，避免浏览器 CORS 复杂度。

推荐入口：

```text
/stac/      → stac-fastapi-pgstac
/assets/    → 只读映射 DATA_ROOT
/browser/   → stac-browser
```

如果现场已经有 Nginx/Caddy，则直接利用；没有时才增加一个极简的基础设施静态映射。**不新增 ingest 业务服务。**

`file://` 只允许作为开发机/离线脚本的内部读取方式，**禁止写入生产 STAC Asset href**。

### 3.6 Collection 设计

Collection 是语义边界，不是简单目录别名。

初始 Collection：

| 大类 | 角色 | Collection |
|---|---|---|
| 光学原始 | L2 | `landsat-8-c2-l2` |
| 光学原始 | L2 | `landsat-9-c2-l2` |
| 光学原始 | L2A | `sentinel-2-l2a` |
| SAR 原始 | SLC | `sentinel-1-slc` |
| SAR 轨道 | 辅助 | `orbit-aux-s1` |
| 光学成果 | 示例 | `optical-product-bandcombo` |
| SAR 成果 | 形变 | `sar-product-velocity` |
| SAR 成果 | 干涉 | `sar-product-interferogram` |
| 倾斜成果 | DOM | `oblique-product-dom` |
| 点云成果 | Gaussian Splat | `gaussian-splat` |
| DEM | 原始/成果按产品定义 | `dem-*` |

新增产品类型时：

1. 先补 Collection Contract；
2. 再补 Manifest 示例；
3. 再补 Schema；
4. 最后允许登记。

### 3.7 Item 身份

STAC 1.1.0 中 Item `id` 是字符串，并且 Item ID 在 Collection 内应唯一。

本项目采用：

```text
Item.id = provider-qualified stable ID
```

例如：

```text
asf:S1A_IW_SLC__1SDV_20200115T102030_...
eodag:S2A_MSIL2A_20200115T...
local:projA:track100_20200115_vel
```

规则：

- `ingest:provider_product_id` 保存**原始提供方 ID**，不规范化、不改写；
- `Item.id` 不再做“非法字符替换”，因为 STAC ID 本身就是字符串，不需要为了路径而伪造 slug；
- 同一 Collection 中不得出现两个相同 Item.id；
- 不允许用 basename 冒充产品身份；
- 本地成果必须由项目方提供稳定 ID。

因此 Item 幂等键明确为：

```text
(collection_id, Item.id)
```

业务身份校验则同时比较：

```text
provider
provider_product_id
collection
```

### 3.8 文件命名

类别 1、2 原始数据：

- 文件名 = 提供方原名；
- 不改名；
- 不重编码；
- 不增加业务前缀；
- Asset basename 与提供方原名一致。

类别 3/4/5、轨道、成果：

- 项目侧命名；
- 文件名只是存储对象名；
- 不参与 Item 身份。

### 3.9 文件完整性与 STAC File Extension

生产 STAC Item 必须启用稳定的 File Info Extension：

```text
https://stac-extensions.github.io/file/v2.1.0/schema.json
```

该扩展用于表达 Asset 的文件大小和 checksum；`file:checksum` 使用符合 Multihash 规范的自识别十六进制值。

Asset 示例：

```json
{
  "href": "https://<asset-host>/assets/rs_raw/sar_raw/S1A_xxx.zip",
  "type": "application/zip",
  "roles": ["data"],
  "file:size": 824123412,
  "file:checksum": "1220..."
}
```

内部任务数据库还必须保存：

```text
expected_provider_checksum
expected_provider_checksum_algorithm
actual_sha256
actual_size
verification_status
```

说明：

- STAC `file:checksum` 采用本项目最终实际计算的 SHA-256 Multihash；
- 如果 Provider 只提供 MD5，则下载验证阶段计算 MD5 与 Provider 值比对；同时计算 SHA-256，写入 STAC `file:checksum`；
- 没有 Provider checksum 时仍可计算本地 SHA-256，但该文件只能标记为 `provider_unverified`，是否允许自动下载由 Provider 白名单决定；
- 自动下载 Provider 默认必须能提供可核验的预期 checksum，不能把“size 相等”当作完整性证明。

### 3.10 成果血缘与处理信息

STAC 原生使用：

```text
links[].rel = derived_from
```

指向产生该成果所使用的 STAC Item。STAC 官方最佳实践将 `derived_from` 作为表达成果来源关系的标准关系类型。

成果 Item 必须至少包含：

- `derived_from` 至少 1 条；
- `processing:software`；
- `processing:version`；
- `processing:datetime`；
- `ingest:processing_parameters`；
- `processing:level`（适用时）。

其中 Processing Extension 当前 v1.2.0 为 Candidate 扩展；使用时必须显式声明：

```text
https://stac-extensions.github.io/processing/v1.2.0/schema.json
```

`processing:expression` **只在确实存在可描述的处理表达式/处理链时使用**，不得拿它充当任意参数字典。

因此本项目的任意业务参数统一放在自有扩展：

```text
ingest:processing_parameters
```

而不是伪装成 `processing:expression`。

### 3.11 本仓自有 STAC Extension

定义本仓扩展：

```text
https://<catalog-host>/stac-extensions/ingest/v1.0.0/schema.json
```

命名空间：

```text
ingest:
```

只允许定义本项目真正需要的字段，例如：

```text
ingest:provider
ingest:provider_product_id
ingest:product_type
ingest:domain
ingest:role
ingest:processing_parameters
```

所有非 STAC Core、非官方 Extension、非本仓 Extension 的字段一律拒绝。

注意：这不是“拒绝所有 STAC 外部字段”，而是**建立明确的 namespace + schema**，避免出现无前缀、无契约的临时字段。

### 3.12 成果 Manifest

3/4/5、轨道和全部成果必须先形成 Manifest，再生成 Item。

示例：

```yaml
collection: sar-product-velocity
item_id: "local:projA:track100_20200115_vel"
datetime: "2020-01-15T00:00:00Z"
geometry:
  type: Polygon
  coordinates: [[[...]]]
assets:
  velocity:
    asset_key: rs_product/sar_product/projA/velocity.tif
    type: image/tiff
    roles: [data]
properties:
  ingest:provider: local
  ingest:provider_product_id: "projA_track100_20200115_vel"
  ingest:product_type: velocity
  ingest:domain: sar
  ingest:role: product
  processing:software: InSARHub
  processing:version: "1.4.0"
  processing:datetime: "2026-09-19T01:20:00Z"
  ingest:processing_parameters:
    looks: 3
    filter: goldstein
links:
  - rel: derived_from
    collection: sentinel-1-slc
    item_id: "asf:S1A_IW_SLC__1SDV_..."
```

生成真正 STAC Item 时：

- `asset_key` 只进入内部构建流程；
- `assets[].href` 生成稳定的绝对 `https://<asset-host>/assets/...`；
- `file:size` / `file:checksum` 写入 Asset；
- `links[].rel=derived_from` 解析成规范 STAC Link；
- Item/Collection 自己的 `self` / `collection` / `root` / `parent` 等链接按动态 API 规则生成绝对 URL。

---

## 4. 运行与可靠性

### 4.1 能力清单

| ID | 能力 | 阶段 |
|---|---|---|
| C1 | SyncTask YAML | P0 |
| C2 | `plan` | P0 |
| C3 | Provider 下载 | P1 |
| C4 | LocalStorage | P0 |
| C5 | Storage Adapter | P0 |
| C6 | checksum + atomic commit | P0 |
| C7 | SQLite 状态机 | P0 |
| C8 | STAC Schema / ItemBuilder | P0 |
| C9 | Transactions 索引 | P0 |
| C10 | 成果 Manifest | P2 |
| C11 | Reconcile | P2 |
| C12 | cron 仅触发 CLI | P3 |
| C13 | metrics / backup | P3 |
| C14 | S3Storage | P4 |

### 4.2 自动同步流程

```text
SyncTask
  ↓
plan
  ↓
SceneProgress = discovered
  ↓
检查磁盘余量
  ↓
download
  ↓
.part
  ↓
resume / retry
  ↓
size verify
  ↓
checksum verify
  ↓
atomic rename
  ↓
SceneProgress = downloaded + verified
  ↓
Build STAC Item
  ↓
Schema validate
  ↓
Transactions
  ↓
SceneProgress = indexed
```

**任何一步未完成，不得进入下一步。**

### 4.3 原子落盘

正式文件绝不在下载过程中出现。

规则：

```text
最终：xxx.zip
下载中：xxx.zip.part
```

成功顺序：

1. 写 `.part`；
2. 传输结束；
3. size 校验；
4. checksum 校验；
5. `fsync` / 安全关闭；
6. `.part` 原子 rename 为正式文件；
7. 更新状态为 `verified`；
8. 允许 Index。

index 失败时：

```text
正式文件保留
SceneProgress.index_status = failed
```

恢复时只补 index，不重新下载。

### 4.4 断点续传

`download_one(scene)` 必须支持：

- Range resume（Provider 支持时）；
- `.part` 状态识别；
- Provider 不支持 Range 时自动退化为整文件重下；
- `.part` 本身不作为已成功资产；
- `.part` checksum 不通过时直接删除并重新开始。

### 4.5 错误分类与重试

| 错误 | 默认行为 |
|---|---|
| timeout / connection reset | 有限重试 + 指数退避 + jitter |
| HTTP 5xx | 有限重试 + 指数退避 |
| HTTP 429 | 尊重 `Retry-After` + 有限重试 |
| checksum mismatch | 删除损坏文件后有限重试 |
| 401 / 403 | 不重试，标记凭证错误 |
| 404 | 不重试，记录源数据不存在 |
| 磁盘满 | 不重试，停止当前 Run |
| 配置错误 | 不重试 |
| 未分类异常 | 默认不无限重试，进入 failed |

建议初值：

```text
MAX_RETRIES_TRANSIENT = 5
MAX_RETRIES_429 = 5
MAX_RETRIES_CHECKSUM = 2
BACKOFF = exponential + jitter
```

### 4.6 磁盘空间保护

Run 开始前检查：

```text
available >= required_total + safety_reserve
```

每个文件正式下载前再次检查：

```text
available >= expected_size * 1.05 + safety_reserve
```

运行中如果跌破 `DISK_MIN_FREE_BYTES` 或 `DISK_MIN_FREE_RATIO`：

```text
停止启动新的下载
→ 当前安全收尾
→ Run = failed / interrupted
```

不对“磁盘满”做重试。

### 4.7 SQLite 状态机

权威状态库：

```text
{STATE_ROOT}/runs.sqlite
```

**STATE_ROOT 默认不放在 DATA_ROOT 数据盘上。**

推荐：

```text
/var/lib/stac-ingest/runs.sqlite
```

或 Docker named volume。

原因：数据盘满时仍应能写任务状态，避免“数据盘满 → SQLite 无法提交 → 恢复状态反而损坏”。

SQLite 建议：

```text
journal_mode = WAL
synchronous = FULL
foreign_keys = ON
busy_timeout = 5000ms
```

核心表：

```text
Task
TaskRun
SceneProgress
RunEvent
StorageEvent
```

### 4.8 状态定义

TaskRun：

```text
pending
running
succeeded
failed
cancelled
interrupted
```

SceneProgress：

```text
discovered
downloading
downloaded
verifying
verified
indexing
indexed
skipped
failed
```

关键字段：

```text
task_id
run_id
provider
provider_product_id
collection_id
item_id
asset_key
filename
expected_size
expected_provider_checksum
expected_provider_checksum_algorithm
actual_size
actual_sha256
 download_status
checksum_status
index_status
attempts
last_error
updated_at
```

不保存服务器绝对路径。

### 4.9 Run Lease / Heartbeat

仅靠 `running` 不足以恢复服务器断电后的 Run，因此 TaskRun 必须有：

```text
owner_id
heartbeat_at
lease_timeout
```

规则：

```text
running + heartbeat 超时
→ interrupted
```

下一次 `sync`：

```text
发现 interrupted Run
→ 默认 resume
```

同一 `task_id` 同时只允许一个 active Run。使用：

```text
BEGIN IMMEDIATE
```

取得任务锁。

第二个进程：

- 若发现 active Run 且 heartbeat 有效：退出并提示已有任务运行；
- 若 heartbeat 失效：接管并恢复；
- 不新建第二个并发 Run。

### 4.10 Resume 规则

| 状态 | 恢复动作 |
|---|---|
| 正式文件通过 size + checksum | 跳过下载 |
| 正式文件完整但 index 未成功 | 只补 index |
| 只有 `.part` | 尝试 resume；失败则重下 |
| `.part` 损坏 | 删除后重下 |
| 正式文件 checksum 失败 | 隔离/删除后重下 |
| index 已成功 | 跳过 |
| Run 已 interrupted | 继续原 Run |
| Run succeeded | 不复用旧 run_id；重跑创建新 Run |

### 4.11 Catalog 写入与幂等

禁止简单地：

```text
POST
409
PUT
```

固定流程：

```text
POST Item
  ↓
成功 → indexed
  ↓
409
  ↓
GET 已存在 Item
  ↓
检查 immutable identity
  ├─ 完全一致 → no-op / indexed
  ├─ 允许更新字段变化 → PUT
  └─ 身份冲突 → identity_conflict
```

Immutable identity 至少包括：

```text
collection
Item.id
ingest:provider
ingest:provider_product_id
```

如果身份一致但资产路径发生变化：

```text
允许更新 assets.href
```

如果出现同 ID 不同产品：

```text
拒绝覆盖
```

### 4.12 Run 是历史，不回写成当前资产状态

下载、索引、移动等操作都产生 `RunEvent` / `StorageEvent`。

例如：

```text
Run-001
  下载到 asset_key=A

StorageEvent-015
  move A → B

当前 Item
  href=B
```

禁止为了“当前正确”而修改历史 Run，使其失去历史事实意义。

### 4.13 本地登记

3/4/5、轨道、全部成果不自动下载，使用：

```bash
stac-ingest index --local --manifest xxx.yaml
stac-ingest product register --manifest xxx.yaml
```

登记前必须：

1. 文件存在；
2. asset_key 合法；
3. Collection 已定义；
4. Manifest 通过本仓 Schema；
5. `derived_from` 源 Item 存在（迁移模式除外）；
6. checksum / size 写入 Asset；
7. 再进入 Transactions。

### 4.14 Reconcile

```bash
stac-ingest reconcile
```

默认只报告，不自动修复。

#### Catalog → Storage

检查：

```text
asset href 是否属于 ASSET_BASE_URL
asset_key 是否合法
文件是否存在
size 是否一致
file:checksum 是否一致
```

结果：

```text
ok
missing_file
size_mismatch
checksum_mismatch
href_invalid
storage_unreadable
```

#### Storage → Catalog

只扫描正式文件，不扫描 `.part`：

```text
registered_ok
unregistered
ambiguous
```

`ambiguous` 禁止猜测 Item。

`reconcile --repair` 仍然必须遵守：

- 不自动删除正式数据；
- 不自动覆盖已有 Item；
- 修复动作写入 `StorageEvent` / `RunEvent`。

### 4.15 Move / Delete

| 动作 | 规则 |
|---|---|
| 删除 `.part` | 允许 |
| 删除正式文件 | 默认禁止，需显式命令 |
| 删除有 `derived_from` 下游的数据 | 拒绝，除非 `--force` |
| 同域移动 | `StorageAdapter.move` + 更新 Item href + 写 StorageEvent |
| 跨 Collection | 禁止静默移动，走 migrate |
| 修改 Item.id | 视为新 Item，不是 move |
| 修改 DATA_ROOT | 不是资产 move，只改变 LocalStorage 配置 |
| 切换到 S3 | 不是资产 move，asset_key 保持不变 |

---

## 5. 配置与部署

### 5.1 核心配置

| 变量 | 用途 |
|---|---|
| `STAC_API_URL` | 内部 STAC API 地址，例如 `http://127.0.0.1:8082` |
| `STAC_PUBLIC_BASE_URL` | 对外/客户端使用的 STAC 根 URL |
| `ASSET_BASE_URL` | Asset 绝对 HTTP(S) 基址，例如 `https://host/assets/` |
| `DATA_ROOT` | LocalStorage 根，现场 `/mnt/diskrsdata` |
| `STATE_ROOT` | SQLite 状态根，默认 `/var/lib/stac-ingest` |
| `STORAGE_BACKEND` | `local`；P4 才实现 `s3` |
| `DISK_MIN_FREE_BYTES` | 最低剩余空间 |
| `DISK_MIN_FREE_RATIO` | 最低剩余空间比例 |
| `MAX_RETRIES_TRANSIENT` | 瞬态网络错误重试 |
| `MAX_RETRIES_429` | 429 重试 |
| `MAX_RETRIES_CHECKSUM` | checksum 失败重试 |

### 5.2 凭证

密钥不入 Git：

```text
Earthdata .netrc
EODAG credentials
数据库密码
S3 credentials（P4）
```

容器挂载只读优先。

### 5.3 Provider 能力声明

每个 Provider 必须显式声明：

```text
supports_resume
provides_expected_size
provides_expected_checksum
checksum_algorithm
source_id_field
```

例如：

```yaml
provider: asf
supports_resume: true
provides_expected_checksum: true
checksum_algorithm: md5
source_id_field: scene_id
```

Provider 能力不足时不得由 Downloader “猜测”。

### 5.4 Storage Adapter 接口

上层只调用抽象接口，例如：

```text
exists(asset_key)
stat(asset_key)
open_read(asset_key)
open_write_partial(asset_key)
resume(asset_key)
checksum(asset_key)
finalize_part(asset_key)
move(src_key, dst_key)
delete(asset_key)
```

禁止业务层出现：

```python
open(...)
Path(...)
os.path...
shutil...
boto3...
```

LocalStorage 负责把 `asset_key` 解析为 `DATA_ROOT / asset_key`，并执行路径安全校验。

S3Storage 未来把同一个 `asset_key` 映射为 `bucket/prefix/asset_key`。

### 5.5 S3 演进

P4 才实现。

未来结构：

```text
             asset_key
                │
        ┌───────┴────────┐
        │ StorageAdapter │
        └───────┬────────┘
             ┌──┴──┐
             │     │
           Local   S3
             │     │
          DATA_ROOT bucket
```

STAC Catalog 不改变：

```text
assets[].href = https://<asset-host>/assets/<asset-key>
```

只改变 `/assets/` 后面的实际读取后端。

这样不会发生“切 S3 → 重写全部 STAC Item”的灾难性迁移。

### 5.6 单独的 Docker 应用

日常只启动根目录这一份：

```bash
docker compose up -d --build
docker compose exec ingest stac-ingest sync /tasks/examples/local_demo.yaml
docker compose exec ingest stac-ingest task status
```

这是与官方兼容的外挂应用：`app` / `database` / `browser` 均为官方镜像，业务代码不打进这些镜像。`ingest` 是唯一业务容器，通过 `STAC_API_URL` 调用其中的 Transactions。`assets` 只读映射 `DATA_ROOT`，不进入 `stac_ingest`。容器内 `DATA_ROOT=/data`，`STATE_ROOT=/var/lib/stac-ingest`；对外 Asset 基址是 `http://localhost:36610/assets/`。

资产 HTTP 映射属于基础设施入口：

```text
/stac/     → app
/assets/   → readonly DATA_ROOT
/browser/  → browser
```

如果现场已经有统一反向代理，直接配置；否则增加一个极简只读静态映射组件，不进入 `stac_ingest` 业务逻辑。

Nginx 语义示例（宿主机已有 Nginx 时优先采用；不要直接暴露整个 `DATA_ROOT`）：

```nginx
location /assets/rs_raw/ {
    alias /mnt/diskrsdata/rs_raw/;
    autoindex off;
}

location /assets/rs_product/ {
    alias /mnt/diskrsdata/rs_product/;
    autoindex off;
}
```

实际部署还应在反向代理层统一加认证、`X-Content-Type-Options` 等安全头；不要把 `/_state`、`_reports` 或其他运维目录映射到 `/assets/`。

### 5.7 官方镜像版本

生产环境：

- 禁止 `latest`；
- 镜像使用明确版本，最好 pin digest；
- upstream 升级前运行 Contract Tests；
- 升级后重新跑 P0/P1 验收；
- `stac_fastapi/` 只作源码对照，不自动升级生产镜像。

---

## 6. 硬约束与安全

### 6.1 四条第一原则

必须始终优先保证：

1. **文件完整性**；
2. **任务可恢复**；
3. **Catalog 与实际文件一致**；
4. **业务代码与底层存储解耦**。

### 6.2 绝对禁止

| 禁止 | 原因 |
|---|---|
| 未完成 size + checksum + atomic commit 就 index | 防止半文件进入 Catalog |
| 只靠 size 判断成功 | 不能证明内容完整 |
| 用 JSON/退出码记恢复状态 | 进程崩溃后不可可靠恢复 |
| 第二个 cron 进程启动同一 Task | 防止并发重复下载 |
| 业务层直接 `Path/open/os/shutil` | 破坏 Storage Adapter |
| 把 `/mnt/diskrsdata` 写进 STAC | 泄露实现细节、不可迁移 |
| 把 `file://` 作为生产 Asset href | 浏览器/远程 STAC Client 不可用 |
| 把 `s3://` 直接写成稳定业务 href | 切换后端会迫使 Catalog 改写 |
| 用 basename 作为 Item.id | 文件名不是产品身份 |
| 409 后无脑 PUT | 可能覆盖不同产品 |
| 自动 reconcile 删除正式数据 | 风险过高 |
| 用 `processing:expression` 存任意参数 | 滥用官方 Extension 字段 |
| 无 namespace 的临时自定义字段 | Catalog 不可治理 |
| 公网暴露未鉴权 Transactions | 允许任意改写 Catalog |

### 6.3 Asset 访问安全

`/assets/` 必须是：

```text
GET / HEAD only
```

禁止：

```text
PUT
POST
PATCH
DELETE
```

必须校验：

```text
URL decode
→ 规范化路径
→ 禁止 ..
→ asset_key allowlist
→ DATA_ROOT realpath containment
```

如果使用反向代理：

- 关闭目录浏览；
- 只读挂载；
- 限制路径前缀；
- 根据部署边界配置认证。

### 6.4 STAC Transactions 安全

官方 app 只在内网监听或受反向代理保护：

```text
Browser / Client
      ↓
Reverse Proxy
      ↓
STAC API
```

写接口必须有独立认证边界，至少区分：

```text
read/search
write/index
admin
```

---

## 7. 实现顺序与验收

### 7.1 实现顺序

| 阶段 | 必须交付 |
|---|---|
| **P0** | STAC 1.1.0 基线验证、LocalStorage、Asset URL、SQLite 状态机、ItemBuilder、最小 Schema、checksum + atomic commit、Item identity |
| **P1** | ASF / 光学 Provider、resume、retry、Provider checksum、Collection Contract、Transactions 幂等 |
| **P2** | 3/4/5、轨道、成果 Manifest、Processing / File Extension、Reconcile |
| **P3** | cron 只触发 CLI、metrics、日志、备份/恢复演练 |
| **P4** | S3Storage + `/assets/` 后端切换，不改变 STAC href |

### 7.2 P0 验收

必须全部通过：

1. 下载 → `.part` → size → checksum → atomic rename → index；
2. 任一步失败，正式文件不被视为成功资产；
3. 进程被 kill 后 `--resume` 不重复下载已验证文件；
4. `running` Run 心跳超时后可变成 `interrupted`；
5. 第二个 cron 不会并发运行同一 Task；
6. task status 可显示每个 Scene 的 download / checksum / index 状态；
7. Asset href 为绝对 HTTP(S) URI；
8. Catalog 中不出现 `/mnt/diskrsdata`、`file://`、`s3://`；
9. `file:size` 与真实文件一致；
10. `file:checksum` 符合 File Extension / Multihash；
11. 相同 `(collection, item_id)` 重复 sync 为幂等；
12. 相同 Item ID 但不同 provider/product identity 会被拒绝；
13. 更换 `DATA_ROOT` 不改变 Asset href；
14. 所有 IO 经 StorageAdapter；
15. P0 Schema 校验失败时不发 Transactions；
16. Catalog Item 的 `self` / `collection` / `root` 等链接符合动态 API 绝对 URL 规则。

### 7.3 P1 验收

1. S1 保留提供方原名；
2. 光学数据同样走统一成功判定；
3. 429 正确尊重 `Retry-After`；
4. 401/403/404/磁盘满/配置错误不无限重试；
5. 同一 `provider_product_id` 重复同步不生成重复 Item；
6. 第二次运行发现已验证文件时直接进入 index/no-op；
7. provider checksum 与本地计算值不一致时不得进入 Catalog。

### 7.4 P2 验收

1. 五类主数据与轨道 Collection 不混；
2. Manifest 缺 geometry / asset / type / roles / file / derived_from 时拒绝；
3. `sar-product-velocity` 使用语义化 Asset key，例如 `velocity`，并含 `roles: [data]`；
4. File Extension / Processing Extension 使用前正确声明；
5. Processing 参数进入 `ingest:processing_parameters`，不伪装成 `processing:expression`；
6. `derived_from` 指向真实存在的源 Item；
7. reconcile 双向覆盖 missing / size / checksum / unregistered；
8. reconcile 默认不自动删除或覆盖；
9. move 后当前 Item href 与 Storage 一致，历史 Run 保持不变。

### 7.5 P3 运维验收

必须完成至少一次：

```text
数据库备份
→ 删除测试库
→ 从备份恢复
→ `/search` 恢复
```

以及：

```text
状态库备份
→ 终止 ingest
→ 恢复 runs.sqlite
→ resume 成功
```

建议监测：

```text
disk_free_bytes
task_running
task_failed
download_bytes_total
download_error_total
index_success_total
index_error_total
reconcile_missing_total
```

### 7.6 P4 S3 验收

S3 切换后：

```text
Task 不改
SceneProgress 不改
Item.id 不改
Asset key 不改
STAC href 不改
```

只验证：

```text
LocalStorage → S3Storage
```

后 `/assets/<asset-key>` 能继续 GET/HEAD/Range 访问，且 checksum / size / STAC 查询结果保持一致。

---

## 8. 规范依据

本设计以以下规范作为实现基线：

- STAC Specification 1.1.0：<https://github.com/radiantearth/stac-spec>
- STAC Catalog / Collection Best Practices：<https://github.com/radiantearth/stac-best-practices/blob/main/best-practices-catalog-and-collection.md>
- STAC File Info Extension 2.1.0：<https://github.com/stac-extensions/file>
- STAC Processing Extension 1.2.0：<https://github.com/stac-extensions/processing>
- STAC API：<https://github.com/radiantearth/stac-api-spec>

### 8.1 本设计对 STAC 的核心取舍

| 问题 | 本项目决策 |
|---|---|
| 动态 Catalog 的 Asset href | **绝对 HTTP(S) URI** |
| 当前文件系统 | Storage 后端，不写入 STAC 绝对路径 |
| 未来 S3 | 通过同一资产访问 URL 切换后端 |
| 文件完整性 | STAC File Extension + SQLite 运行状态双记录 |
| 成果来源 | STAC `derived_from` |
| 处理软件 | Processing Extension |
| 任意处理参数 | 本仓 `ingest:` 扩展 |
| Item 身份 | provider-qualified stable ID |
| 状态恢复 | SQLite，不依赖 cron / JSON |
| 目录一致性 | Reconcile 双向检查 |

---

## 最终架构原则

```text
             STAC = 标准目录与语义
                     │
                     │ Asset href = 稳定 HTTP(S) URI
                     ▼
             Asset Access Layer
                     │
                     │ asset_key
                     ▼
              Storage Adapter
               │            │
          LocalStorage    S3Storage
               │            │
          DATA_ROOT      Bucket/Prefix

          ingest Task State
                 │
                 ▼
             SQLite
```

**最终原则只有一句话：**

> **让 STAC 只表达标准的时空资产语义，让 SQLite 负责运行时状态，让 Storage Adapter 负责存储，让资产访问层负责 URI；四者职责分离，但都在单服务器上保持最小化。**

这样既不需要为了当前单机环境引入分布式系统，也不会为了未来 S3 把今天的 Catalog 做成一次性方案。
