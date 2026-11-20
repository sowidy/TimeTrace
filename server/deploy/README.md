# 独立部署与客户端同步

服务器只需 Python 3.10+、MySQL 和整个 `server/` 文件夹；无需客户端、浏览器插件、
根目录 `tools/`、Qt 或 Windows 组件。客户端只需自己的 `client/`：统计、SQLite、
排行、时间线和导出均在本地完成，上传在后台运行，服务器不可用时继续记录。

## Linux / 宝塔使用现有 MySQL

```bash
cd /opt/projects/TimeTrace
python3 -m venv .venv                     # 已有虚拟环境可跳过
.venv/bin/python -m pip install -r server/requirements.txt
# 只在首次安装复制模板；升级时保留现有 server/.env。
test -f server/.env || cp server/.env.example server/.env
# 编辑 server/.env，填写 MySQL 连接和密码；SMTP 可不配置。
.venv/bin/python server/check.py         # 检查全部运行模块和路由，不连接数据库/发邮件
.venv/bin/python server/check.py --database  # 初始化/检查配置的数据库，不发邮件
.venv/bin/python server/run.py           # 前台试运行；正式运行由宝塔或 systemd 托管
```

仅复制 `server/` 时，也可在该文件夹里创建虚拟环境，执行 `python check.py`、
`python run.py`。入口读取 `.env` 的 `TIMETRACE_HOST` / `TIMETRACE_PORT`，与启动时
工作目录无关。使用一个 worker，避免邮件定时器被多个 worker 同时启动。

宝塔可使用项目目录 `/opt/projects/TimeTrace/server`、启动文件 `run.py`、原虚拟环境；
若原项目配置的是 ASGI，则继续用 `app:app`。两种方式选一种。
`timetrace.service.example` 是可选 systemd 模板，需先创建其中配置的用户并授予项目
读取权限，不要与宝塔重复托管同一端口。

MySQL 建表账号需本库建表权限，首次自动建齐 10 张表。新表显式使用 InnoDB/utf8mb4；
已有 `users.user_key` 的字符集/排序规则会用于新外键列，不会为此改写旧统计数据。
其中 `website_hourly_usage` 保存客户端上传的真实网站分时用量，日报每小时堆叠柱与周报热力图依赖该表；
周报新增的网站每日堆叠柱直接使用已有 `website_usage_records` 的每日域名汇总，不需要额外采集字段；
`website_intervals` 保存仅含域名和当天起止秒数的真实网站片段，日报甘特图依赖该表，缺失时不会用汇总数据推造访问时段；
`app_timeline_intervals` 保存仅含应用名和当天起止秒数的真实软件片段，日报软件时段图依赖该表；
已有客户端只上传每日汇总时，报告会明确标记分时数据缺失。升级客户端后会自动重传最近 14 天本地记录；
更早日期若没有分时记录，仍显示为缺失，不会由每日总量推算。
先升级服务端、确认 `website_intervals` 与 `app_timeline_intervals` 表已建好，再升级客户端。旧服务端拒绝新上传字段，客户端会保留本地数据并在下次运行时重试。
已有数据库不再执行建库语句。初始化后可设 `TIMETRACE_DB_INIT_SCHEMA=0`，切换到仅
SELECT/INSERT/UPDATE/DELETE 的账号；此模式仍检查缺表，不会把缺表问题留到上传时。

## 更新已有服务器

先备份数据库、`server/.env` 和当前代码，检查 `git status --short`。
若有上次部署的兼容补丁，先保存差异并逐项核对；不要强制覆盖配置或补丁。
工作区干净后：

旧 `server/.env` 的 `USAGETIME_*` 键仍可用；新 `TIMETRACE_*` 同名键优先。不要因改名而
新建空数据库：已有生产库名或账号若仍为 `usagetime`，继续把
`TIMETRACE_DB_NAME=usagetime` / `TIMETRACE_DB_USER=usagetime` 指向它，核对后再重启。

```bash
git pull --ff-only origin main
.venv/bin/python -m pip install -r server/requirements.txt
.venv/bin/python server/check.py
.venv/bin/python server/check.py --database
# 全部成功后，在宝塔点击该项目“重启”。
curl --fail http://127.0.0.1:8000/api/v1/health
```

`api/tracking.py`、`api/tracking_schemas.py`、`db/tracking.py`、
`mail/tracking_report.py` 是运行模块，必须随版本提交和部署，不得用空实现替代。
服务端仓库可以不包含 `client/`。部署前可手工运行本文末尾的 MySQL 验证命令。
仅当同时具备客户端源码时，才增加 `--client client` 验证客户端联调；手工指定
`--client` 时必须提供有效目录。

## Docker（可选）

在 `server/` 下复制 `.env.example` 为 `.env`，填写 `TIMETRACE_DB_PASSWORD`，运行
`docker compose up -d --build`。此栈启动独立 MySQL，数据保存在命名卷，不发布数据库
端口；API 默认发布到 127.0.0.1:8000，可交给反向代理。需局域网直连时，在 `.env`
设置 `TIMETRACE_PUBLISH_HOST=0.0.0.0`。这不会自动迁移宝塔现有数据库。
旧 Docker Compose 数据卷升级时，先备份卷和 `.env`，将旧库名、用户名、密码明确写成
`TIMETRACE_DB_NAME`、`TIMETRACE_DB_USER`、`TIMETRACE_DB_PASSWORD`；否则新默认值 `timetrace`
可能指向空库。`TIMETRACE_*` 是 Compose 模板插值所需，不仅是 Python 进程的配置回退。
关闭时保留数据卷，勿用 `down -v` 删除数据。

## 客户端连接与验收

客户端程序旁 `.env`：

```dotenv
TIMETRACE_SERVER_URL=http://服务器IP:8000
TIMETRACE_UPLOAD_CHECK_INTERVAL=60
```

完整 URL 不要包含 `/api/v1`。应用汇总、网站与应用时段上传都支持配置的 HTTP 或
HTTPS 地址；HTTP 传输未加密，公网部署建议使用受信任 HTTPS。网站时长已包含在
浏览器应用中，不额外加到总时长。

重启客户端，在“同步与邮件”启用自动上传，或点“立即上传”手动同步。
网站邮件图表需打开“邮件中加入网站图表”；手动上传会同时补传已授权的历史明细，
并分别显示应用、网站与应用时段的结果。开启“邮件中加入软件时段图”后，
客户端也会上传真实软件使用起止时段；仅有旧版 15 分钟汇总时，日报会标明时段数据未收到。
但不再加入日报或周报邮件。
服务端按设备 UUID + 日期覆盖，失败保留 SQLite 记录和待传进度，下次检查补传。
每次启动会补传，跨日约 00:00:30 后再传一次。离线或关机时无法保证赶上 00:03，
邮件使用发送时已收到的数据；晚到的补传不会自动重发已发送成功的邮件。

健康接口只证明 API/数据库可用，还需用客户端显示的设备 ID 读回数据并比较总时长：

```bash
curl --fail 'http://127.0.0.1:8000/api/v1/summary/设备UUID?day=2026-10-24'
```

设备 ID 是访问凭据，勿公开。SMTP 可选配置见 `.env.example`，不配邮件也可接收上传。
默认以服务器本地时区每天 00:03 发昨天报告。原生/宝塔需确认系统时区为 Asia/Shanghai，
Docker 模板已设置该时区。

## 可复现隔离验证与打包

验证使用临时 MySQL 数据目录和临时 SQLite，不使用实际 `.env`，不会向正式服务器
上传或发送邮件。需要本机 MySQL 8 的 `mysqld` 可执行文件；命令在仓库根目录运行：

```bash
python server/tools/verify_mysql.py --mysqld /path/to/mysqld
# 可选：加入真实客户端的离线记录、恢复补传和额外数据序列化验证。
python server/tools/verify_mysql.py --mysqld /path/to/mysqld --client client
python server/tools/package.py --output artifacts/release/timetrace-server.zip
```

覆盖：server-only 启动、上传鉴权、幂等、读回、重复建表、新库默认 latin1、旧用户表
general_ci、仅 DML 权限的运行检查。打包排除 `.env`、缓存、数据库和本地日志。
发行包同时带上本组件的许可证、来源声明，以及构建环境中依赖包的许可证文件和
版本清单。维护法律文件时应同步仓库根、各独立组件和扩展中的副本。
