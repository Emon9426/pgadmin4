# EM_PGAdmin 变更记录（EM 定制版说明）

> 本文件记录 EM 定制版（Emon fork）相对上游 pgAdmin 4 的所有功能变更。
> 原项目说明请查看 [README.md](README.md)（本文件不覆盖原有文档）。
>
> 注意：由于 Windows 文件系统大小写不敏感，无法在 README.md 旁创建
> readme.md，故使用本文件名 `EM_README.md` 作为变更记录。

## 版本命名规则

发布包命名：`EM_PGAdmin_<上游版本>-em<EM语义版本>.zip`

- **上游版本**：所基于的 pgAdmin 4 官方版本（如 9.18）
- **EM 语义版本**：`em<主版本>.<次版本>.<修订号>`
  - 主版本：包含不兼容的重大变更
  - 次版本：新增功能
  - 修订号：缺陷修复与重新打包

---

## em1.1.0（基于 pgAdmin 4 v9.18）— 2026-10-05

### 新增功能

#### 1. 默认以桌面客户端启动（Electron 壳）

便携包默认启动方式改为**独立桌面窗口应用**（与 pgAdmin 官方桌面安装版
同源的 Electron 运行时），不再依赖系统浏览器：

- 双击 `启动pgAdmin.bat` 直接打开 pgAdmin 桌面窗口（含启动画面、
  原生菜单栏、右键菜单、窗口位置记忆）；
- 桌面模式为免登录直入（内部桌面用户自动登录），后端仍监听
  `127.0.0.1` 随机端口并带一次性安全 key，外部进程无法访问；
- 窗口菜单含 File / Object / Tools / Edit / View / Window / Help，
  与官方桌面版一致；
- 原浏览器方式保留为备用：`启动pgAdmin-网页模式.bat`（在本机
  `127.0.0.1:5050` 起服务并打开默认浏览器），桌面客户端无法运行的
  机器（如缺 GPU 驱动的远程会话）可改用此方式；
- `停止pgAdmin.bat` 会同时结束桌面客户端进程树与其后端服务。

**目录结构调整（相对 em1.0.0）：**

| 目录 | 说明 |
| --- | --- |
| `python/` | 内嵌 Python 运行时与依赖（原 `runtime/` 改名） |
| `web/` | pgAdmin Web 应用（Flask 后端，由壳拉起） |
| `runtime/` | Electron 桌面壳：`pgAdmin4.exe` + `resources/app` |

与官方 Windows 安装包布局一致，后续升级可直接沿用上游 runtime 源码。

### 缺陷修复

#### 2. 便携包依赖完整性修复（影响 em1.0.0）

修复打包脚本的一个缺陷：pip 安装依赖时未隔离构建机的用户级
site-packages，导致 `python-dateutil`、`tzdata`、`pillow`、`six`、
`pywin32-ctypes` 五个依赖在 em1.0.0 的包中**缺失**（在装有 Python
3.13 用户环境且恰好有这些包的机器上被掩盖）。在干净的 Windows 机器上，
em1.0.0 首次启动会因 `No module named 'dateutil'` 失败。em1.1.0 起
所有依赖完整装入包内。

---

## em1.0.0（基于 pgAdmin 4 v9.18）— 2026-09-29

### 新增功能

#### 1. Oracle 数据库连接支持（实验性）

pgAdmin 原生仅支持 PostgreSQL / EDB Advanced Server。本版本新增对
**Oracle 数据库**的基础连接支持，基于 `oracledb`（python-oracledb）
驱动 **thin 模式**，无需安装 Oracle 客户端即可连接。

**使用方法：**

1. 在服务器注册对话框中，将 **Server type（服务器类型）** 选择为
   **Oracle Database**；
2. 端口自动填入 `1521`，**Maintenance database** 填 Oracle 服务名
   （如 `orcl`、`FREEPDB1`，也可使用 `host:port/service` 形式的
   Easy Connect 串）；
3. 输入用户名、密码连接，支持保存密码、SSH 隧道与
   post-connection SQL；
4. 连接成功后浏览器树显示 **Schemas**（来自 `ALL_USERS`，上限 500 个）；
5. 在任一 Schema 节点右键选择 **Query Tool**，即可对 Oracle 执行
   SQL：结果网格、消息面板、事务提交/回滚按钮、取消查询均可用。

**实现说明：**

- 新增 Oracle 驱动层 `web/pgadmin/utils/driver/oracle/`
  （Driver / ServerManager / Connection，实现与 psycopg3 相同的
  BaseConnection 接口）；
- `server` / `sharedserver` 表新增 `server_type` 列并附带 Alembic
  迁移；psycopg3 驱动的 `connection_manager()` 按该列将 Oracle
  服务器透明路由至 Oracle 驱动，原有 PostgreSQL 功能不受影响；
- Server 对话框的"服务器类型"下拉框在新建/编辑时可选择
  PostgreSQL、EDB Advanced Server、Oracle Database；
- 查询工具中的 `BEGIN` / `COMMIT` / `ROLLBACK` 自动映射为 Oracle
  的 autocommit 开关与 commit/rollback 语义。

**已知限制（Oracle 连接）：**

- 对象浏览器仅显示 Schema 列表节点（无表/视图等下级对象树），
  对象管理通过 Query Tool 执行 SQL 完成；
- Dashboard 统计、自动补全、EXPLAIN 按钮向导、可编辑结果集、
  数据网格筛选对话框、Schema Diff、备份/恢复工具等 PG 专属功能
  不适用于 Oracle 连接；
- 不支持 EDB 工具（调试器等）与 PG 复制相关界面；
- `SELECT` 结果集在服务端执行后整体缓存在内存中，超大数据集查询
  请配合 `WHERE`/`FETCH FIRST` 使用。

#### 2. 绿色便携版打包

新增便携版（免安装、解压即用）打包能力：

- 目录结构：内嵌 Python 运行时 + 全部依赖 + pgAdmin Web 源码；
- 通过 `启动pgAdmin.bat` 一键启动，自动打开浏览器；
- 配置与数据（SQLite、日志）保存在程序目录 `data/` 下，
  U 盘/移动硬盘即插即用，不写注册表。

---

### 兼容性

- PostgreSQL / EDB Advanced Server 功能与上游 v9.18 保持一致，
  `server_type` 列对历史数据（NULL）保持原有自动检测行为；
- 单元测试：`web/pgadmin/utils/driver/oracle/tests/`（基于模拟
  对象，无需真实 Oracle 服务器）。
