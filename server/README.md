# TimeTrace 服务端

接收客户端上传的每日使用记录（「应用名 + 使用时长」+ 启动次数），存进 MySQL，
并按订阅把使用报告用邮件发出去。技术栈是 FastAPI + PyMySQL，部署成一棵
`server/` 为根的项目，启动命令就是 `cd server && uvicorn app:app`。

**它只做三件事**：接数据、查数据、发报告邮件。客户端不依赖它也能跑 —— 断网时
数据留在本机，联网后自动补传（见 [client/README.md](../client/README.md)）。

> 仓库总览、隐私声明、License 在[根 README](../README.md)。

独立部署时也请保留本目录的 `LICENSE`、`NOTICE`、`THIRD_PARTY_NOTICES.md` 和
`licenses/`；项目来源包括 DABIAN-afk/usagetime 和 Planshit/Tai，许可细节及待确认项见
[第三方声明](THIRD_PARTY_NOTICES.md)。

独立部署、宝塔升级与客户端验收，以 [deploy/README.md](deploy/README.md) 为准；
该文档包含本次修复后的入口、数据库兼容处理和隔离验证命令。

## 邮件效果预览

以下为合成演示数据的周报：软件按日期堆叠、网站按每日域名时长堆叠并叠加总用时折线，另有软件与网站各自的「日期 × 小时」热力图和网站类型占比；周报不包含旧软件时段列表。
邮件中的常见网站使用离线中/英文名称映射显示；未知域名仍显示域名，不改变原始统计或上传数据。
网站堆叠柱动态展开，优先覆盖至少 95% 时长，至多展示 80 个网站；余下网站归入“其他”。网站类型饼图直接标注百分比，图例只列类型与时长。
点击图片可查看大图；[日报示例](docs/images/mail-daily.png)包含软件使用时段和时长图、网站使用甘特图、带每小时总时长标注的堆叠柱状图和网站类型饼图。
日报网站时段图不设网站数量上限，仅省略当天累计不足 1 分钟的网站；省略部分仍计入网站总时长。
图片由 HTML 模板离线渲染，不同邮箱的布局和渐变显示可能略有不同。

[![周报邮件预览](docs/images/mail-weekly.png)](docs/images/mail-weekly.png)

网站图表使用客户端保存的真实「域名 × 起止时段」片段及由其切分的「域名 × 小时」数据，和每日网站汇总一起上传。
服务端校验各自合计后写入 `website_intervals` 与 `website_hourly_usage`；旧客户端若只发送每日汇总，
邮件会标明缺少分时记录，不会猜测访问时段或每小时使用量。网站类型仅按域名本地规则归类，
未知域名归入“其他”，不上传路径、标题或页面内容。

## 目录结构

```
server/                       FastAPI 服务端
├── app.py                    入口：建 FastAPI 实例 + lifespan + 挂 API router
├── api/                      HTTP 接口（按资源分文件）
│   ├── deps.py               管理口令校验
│   ├── schemas.py            请求与响应的数据结构（Pydantic）
│   ├── health.py             GET  /                GET  /api/v1/health
│   ├── upload.py             POST /api/v1/upload（核心接口）
│   ├── usage.py              GET  /usage/{user_key}    GET /summary/{user_key}
│   ├── users.py              GET  /users、/users/{user_key}
│   │                         POST /subscription/{user_key}
│   └── admin_mail.py         POST /admin/daily-mail    GET /admin/mail-log
├── db/                       MySQL 数据层（一张表一个文件）
│   ├── conn.py               连接、建库建表、行转 JSON
│   ├── schema.sql            表结构（唯一的定义处，conn.py 启动时读它）
│   ├── users.py              users 表
│   ├── usage_records.py      usage_records 表
│   └── mail_log.py           mail_log 表
├── mail/                     报告邮件推送（按责任分文件）
│   ├── report.py             正文渲染（纯文本 + HTML）
│   ├── sender.py             SMTP 发信
│   └── scheduler.py          次日 00:03 进程内定时器
├── config.py                 读 .env + 环境变量，再退回内置默认值
├── .env                      本机真实配置（明文密码，.gitignore 已忽略）
├── .env.example              配置模板，照抄一份改名 .env 就行
└── requirements.txt
```

`db/` 里那张表的唯一注释在 `schema.sql`，`conn.py` 启动时读它建表 —— 改表结构
新建表定义改这一个文件；已有表的结构变更需另行迁移，IF NOT EXISTS 不会改已有表。

## 导入约定（改代码时必看）

`server/` 是一棵**导入树的根**（`api` / `db` / `mail`），包内一律用以它为起点的
绝对导入：

```python
from db import users                         # ✅
from api.schemas import UploadRequest        # ✅
from server.db import users                  # ❌ server/ 不是包
```

以 `server/` 为根的直接好处是启动命令不用改：`cd server && uvicorn app:app`。

`server/` 里还有两条规矩。它们不是风格问题 —— **写错了不会报错，只会静默坏掉**：

1. **配置一律写 `config.XXX` 属性访问，绝不 `from config import XXX`。**
   测试是靠 `mock.patch.object(config, "MAIL_ENABLED", True)` 换配置的。
   `from ... import` 会在导入时把值复制成一份，patch 再也不生效，而且不报错 ——
   只会让测试悄悄测到旧值、把 bug 放过去。
   同理，`.env` 和环境变量是**导入期读一次**的，运行中改文件不会生效；测试里要换
   配置也只能靠 patch，别去动 `os.environ`（会顺着 `setdefault` 污染后面的测试）。

2. **跨模块调函数时，patch 的目标必须和被测代码查找的位置完全一致。**
   `db/users.py` 里写的是 `from db import conn` 再 `conn.connect()`，
   所以 patch 要打在 `db.conn.connect` 上。要是写成
   `from db.conn import connect`，函数查的就是自己模块里那个副本，
   `patch.object(db.conn, "connect")` 会**成功但无效** —— 测试以为挡住了数据库，
   实际会真去连线上那台 MySQL。所以 `db/__init__.py` 刻意**不做 re-export 门面**：
   每个函数只有一个绑定位置，打错地方会直接 `AttributeError` 响亮地失败。

`.env` 的解析器在服务端和客户端各有一份（`server/config.py` 和
`client/core/envfile.py`）。这是**刻意复制的，不是漏抽公共模块**，理由是打包；
代价靠 `tests/test_envfile.py` 的 `TestParsersAgree` 兜住 —— 同一批输入喂给两份
实现、要求结果逐字相同，只改一边就会红。

## 部署

```bash
# 1. 装依赖
pip install -r server/requirements.txt

# 2. 建库（表不用管，服务端启动时会按 db/schema.sql 自动建）
mysql -h 127.0.0.1 -u root -p \
  -e "CREATE DATABASE timetrace DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;"

# 3. 启动
cd server
uvicorn app:app --host 0.0.0.0 --port 8000
```

（`.venv` 建在仓库根目录即可，`client/` 和 `server/` 共用；跑测试、跑脚本都用它。）

### 本地跑服务端、数据库在云上

开发阶段可以只把 MySQL 放云上，服务端在本机跑，改代码不用重新部署：

```bash
pip install -r server/requirements.txt
cd server
uvicorn app:app --host 0.0.0.0 --port 8000
# 再把 client/.env 里的 TIMETRACE_SERVER_HOST 指向本机
```

数据库地址和口令在 `server/.env` 里（模板 `server/.env.example`），服务端启动时
自己读，不用额外传参。这条路能走通的前提是
**云端得有一个允许远程登录的账号**：

```sql
-- 只给这一个库的权限，比放开 root 远程安全得多
CREATE USER 'timetrace'@'%' IDENTIFIED BY '<强密码>';
GRANT ALL PRIVILEGES ON timetrace.* TO 'timetrace'@'%';
FLUSH PRIVILEGES;
```

> **`1045 Access denied for user 'root'@'<你的公网IP'`** 不是密码错，最常见的原因是
> `root` 只建了 `localhost` 这一条（MySQL 装完默认如此），远程连过来根本匹配不上。
> 用 `SELECT user, host FROM mysql.user;` 一看就知道。**不要**为了图省事去
> `UPDATE mysql.user SET host='%'` 改 root —— 一个对公网开放、又只有弱口令的
> MySQL，被扫到之后撑不了多久。建专用账号，只给它需要的那个库。
>
> MySQL 5.7 和 8 的初始化命令不一样，`CREATE USER ... IDENTIFIED BY` 在两边都能用，
> 别用 `GRANT ... IDENTIFIED BY`（8.0 已经删掉了那个语法）。

> ⚠️ 本地跑服务端时注意：进程**没开 `--reload`**，改完代码必须重启才生效。
> 而重启会立刻触发**启动补发**（见[邮件报告](#邮件报告)），库里只要真有订阅用户
> 和 SMTP 配置，那一轮就是真发邮件。只想验证渲染就加 `dry_run=true`，或者临时设
> `TIMETRACE_MAIL_SCHEDULER=0` 起进程把定时器和补发一起关掉。

## 配置（.env）

连接信息、端口、管理员口令统一放 `server/.env`。取值顺序**先命中的赢**：

1. 真环境变量（命令行 `set`、docker `-e`、systemd `Environment=`）
2. `server/.env` 文件
3. `config.py` 里的默认值

```bash
copy server\.env.example server\.env      # Linux/macOS 用 cp
# 改里面的值，然后重启服务端
```

这套加载是 `server/config.py` 里几十行自己实现的，**不需要装 `python-dotenv`**。
支持的写法（比 python-dotenv 窄，够用）：`KEY=value`、`KEY="value"`（认 `\n` 转义）、
`KEY='value'`（原样保留）、`export KEY=value`、`# 注释`。不支持多行值和 `${OTHER}`
插值。完整清单见 `server/.env.example`。

两条容易踩的：

- **留空等于没设**（`set VAR=` 在 Windows 上很常见，那时候要的是默认值，不是空值）；
- **改完要重启**才生效——配置是导入期算一次的模块级常量。启动日志第一行会报
  「配置来源」，能一眼看出 .env 到底读没读到。

| 配置项（`.env` 里的名字，与环境变量同名） | 含义 | 默认 |
| --- | --- | --- |
| `TIMETRACE_DB_HOST` / `_PORT` / `_USER` / `_PASSWORD` / `_NAME` | MySQL 连接 | `127.0.0.1:3306` `timetrace` / **无默认，必须自己填** / `timetrace` |
| `TIMETRACE_HOST` / `_PORT` | 监听地址 | `127.0.0.1:8000`（部署模板使用 `0.0.0.0`） |
| `TIMETRACE_ADMIN_TOKEN` | 管理接口口令（`/users`、`/admin/*`） | 空 = 管理接口直接 403 |
| `TIMETRACE_MAX_DAYS` / `_APPS` | 单次上传的天数 / 每天应用数上限 | `400` / `2000` |
| `TIMETRACE_SMTP_HOST` | 发信服务器，**留空 = 邮件功能整体关闭** | 空 |
| `TIMETRACE_SMTP_PORT` / `_USER` / `_PASSWORD` | SMTP 账号（密码填**授权码**） | `465` / 空 / 空 |
| `TIMETRACE_SMTP_SSL` | 强制开/关加密；空 = 按端口自动判断（587 走 STARTTLS，其余走 SSL） | 空 |
| `TIMETRACE_MAIL_FROM` / `_NAME` | 发信地址 / 显示名 | 空（= `SMTP_USER`） / `TimeTrace 使用报告` |
| `TIMETRACE_MAIL_ENABLED` | 设 `0` 强制关掉邮件（即使 SMTP 配好了） | `1` |
| `TIMETRACE_MAIL_TIME` | 次日几点发送前一天报告，`HH:MM[:SS]` | `00:03:00` |
| `TIMETRACE_MAIL_CATCHUP_DAYS` | 漏发后回头补几天 | `2` |
| `TIMETRACE_MAIL_SCHEDULER` | 设 `0` 关掉进程内定时器（改用外部 cron） | `1` |
| `TIMETRACE_MAIL_MAX_APPS` | 纯文本应用明细上限；0 为全部，较小正值也限制图表单独展示的类别数 | `60` |
| `TIMETRACE_MAIL_MAX_SITES` | 纯文本网站明细上限；0 为全部 | `40` |
| `TIMETRACE_MAIL_CHART_SERIES` | 软件堆叠柱的类别上限（1–12）；网站柱图以此为最少展示数，再按时长动态展开，最多 80 个网站 | `8` |
| `TIMETRACE_MAIL_WEEKLY_DAY` | 一周里哪一天发周报（`0`=周一 … `6`=周日），其余日子发当天日报 | `6` |
| `TIMETRACE_SMTP_TIMEOUT` | 单次 SMTP 操作超时（秒） | `20` |

真环境变量压过 `.env` 是刻意的：临时试一个值用命令行更顺手，反过来就会出现
「明明 set 了却不生效」这种最难查的故障。

```bash
# 只这一次改掉口令，不动 .env
TIMETRACE_ADMIN_TOKEN=$(python -c "import secrets;print(secrets.token_urlsafe(32))") \
  uvicorn app:app --host 0.0.0.0 --port 8000
```

## 接口一览

浏览器打开 `/docs` 有交互式文档。

> ⚠️ **别拿 `0.0.0.0` 当访问地址。** 上面启动命令里的 `--host 0.0.0.0` 是「在所有
> 网卡上监听」的意思，是给服务端自己用的，不是一个能被访问的地址。浏览器里打开
> `http://0.0.0.0:8000/docs` 连不上，Swagger 的 **Try it out** 会报
> **Failed to fetch**，而它列的三条原因第一条就是 CORS —— 很容易被带偏。
> 本机访问一律写 `http://127.0.0.1:8000`；别人访问你这台机器要用你的局域网/公网 IP。
>
> 还有：在 `/docs` 里调管理接口（下面标着「需要 `X-Admin-Token`」的那几条）之前，
> 要先点右上角 **Authorize** 填入口令。`TIMETRACE_ADMIN_TOKEN` 没配时这些接口一律
> 返回 403，那不是出错，是设计如此。

```
GET  /api/v1/health                   探活（含数据库连通性，连不上返回 503）
POST /api/v1/upload                   客户端上传（核心接口）
GET  /api/v1/usage/{device_id}?start=&end=   读某设备某区间的明细
GET  /api/v1/summary/{device_id}?day=       读某设备某天的汇总（邮件正文的数据源）
GET  /api/v1/users                    列出所有设备      （需要 X-Admin-Token）
GET  /api/v1/users/{device_id}        看单个设备        （需要 X-Admin-Token）
POST /api/v1/subscription/{device_id} 设置邮箱 / 订阅开关
POST /api/v1/admin/daily-mail?day=&catchup=&dry_run=&user_key=  手工触发报告邮件
GET  /api/v1/admin/mail-log?day=      查发送记录
```

「设备 ID」就是客户端首次运行生成的 UUID，它同时是这台设备读取自己数据的凭据 ——
**服务端认 ID 不认人**，没有注册/登录。客户端怎么产生、怎么用见
[client/README.md](../client/README.md)。

上传接口的写入是**按「设备 + 日期」整块覆盖**的，所以客户端重传同一天不会让时长
翻倍；`TIMETRACE_MAX_DAYS` / `_APPS` 是单次请求的兜底上限。

## 邮件报告

周报包含应用每日堆叠柱、网站每日时长堆叠柱与总时长折线、软件与网站各自的「日期 × 小时」热力图、网站类型饼图与整周合计；
日报包含真实软件使用时段和时长图、网站甘特图、带每小时总时长标注的堆叠柱与网站类型饼图。
软件时段图和周报软件热力图需客户端开启对应上传选项；未收到起止时段时明确提示，不从 15 分钟汇总推造精确时刻。周报热力图仅使用真实分时数据，缺失日期显示为灰色。

网站图表要求服务端 `TIMETRACE_MAIL_TRACKING=1`（默认），客户端开启
“邮件中加入网站图表”并手动上传或启用自动上传。客户端手动“立即上传”会同时发送应用和已授权的明细，可补传历史
网站记录；应用、网站和应用时段分别显示结果。网站明细支持配置的 HTTP 或 HTTPS 地址，
HTTP 传输未加密，公网部署建议使用受信任 HTTPS。缺少时段或分时记录时明确标注
“未收到”，不从每日总量推算访问时段或小时用量。网站时长已包含在浏览器应用时长中，
不会再次加到报告总计。周报应用堆叠柱使用真实每日汇总；缺失日期留空，
只有确认完成上传的空日期才显示零。图片不可见时仍可阅读 HTML 图例或纯文本明细
（默认 60 个应用、40 个网站）。

图表采用 **ECharts 风格**：白底、轻虚线网格、柔色渐变柱与圆角顶部、平滑蓝色曲线、
空心数据点和圆点图例。圆点与标注均为真实汇总值；曲线只是视觉连接，不是额外采样或
预测，不越过相邻点取值范围、不穿过缺失日期。共享主题定义在 `mail/chart_theme.py`。
此处仅参考视觉风格，没有引入或运行 ECharts JS；平滑曲线风格参考
[ECharts 官方示例](https://echarts.apache.org/handbook/en/how-to/chart-types/line/smooth-line/)。

图表通过 Pillow 在服务端无界面生成 PNG，实际邮件以 CID 内嵌，不使用 JS、SVG 或
远程图片。升级时执行 `python -m pip install -r requirements.txt` 安装新增依赖；
无需桌面客户端、Qt、浏览器或系统中文字体。预览 HTML 使用同一图片的 data URI。

配好 SMTP 之后，服务端每天 **00:03** 给「已订阅且填了邮箱」的设备发送**前一天**
的一封使用报告。自然日结束后预留 3 分钟，让客户端完成最后的统计和上传，再读取
已收到的数据生成邮件。主题带总时长，正文提供图表与明细（纯文本 + HTML 两版，客户端能
显示 HTML 就用 HTML）。没配 `TIMETRACE_SMTP_HOST` 时这块整体不启用。

发的是日报还是周报，由**那一天是星期几**决定（`TIMETRACE_MAIL_WEEKLY_DAY`，
默认周日）：

| 日子 | 发什么 | 覆盖区间 |
| --- | --- | --- |
| 周一 ~ 周六 | 次日 00:03 发日报 | 该报告日当天 |
| 周日 | 周一 00:03 发周报 | 该报告日往前 7 天（周一 ~ 周日） |

**每天恒定一封**，周日那封就是替代当天日报的，不会再单发一封当天的。周报正文
图表按天展示真实时长构成及总时长趋势，纯文本明细则按应用跨天汇总，并注明
`周期：2026-10-12 ~ 2026-10-18（7 天，有数据 5 天）` —— 「有数据 N 天」是这一周
里真有记录的天数，某天机器没开机、没上传都不算，所以它常常小于 7。

这条规则带来一个省事的结果：**不需要改表。** 报告种类是「这一天是星期几」的纯函数，
所以任何一天都只可能长出一封报告，`mail_log` 那个 `UNIQUE KEY (user_key, day)`
依然管用（详见 `server/mail/__init__.py` 的说明）。想查某一封是哪种，看
`GET /api/v1/admin/mail-log` 里 `detail`（存的是主题）带不带 `周报` 就行，不用加列。

> 改 `TIMETRACE_MAIL_WEEKLY_DAY` **别挑周中改**：那一天的键可能已经被日报占了，
> 改完这周的周报会因为「已经发过」发不出去。要改就在新的一周开始前改。

「已订阅且填了邮箱」这两项由客户端在「设置 → 数据上传 → 每日报告邮件」里设置，
存到服务端 `users` 表的 `email` / `subscribed` 两列。**只填邮箱不勾订阅不会收到
邮件**，这是有意的：不然「填了邮箱」就等于「同意收信」了。

三个容易写错的地方，代码里是这么处理的：

- **触发日不等于报告日。** 定时器记的是「本次唤醒对应的计划时刻」，报告日固定为
  该触发日的前一天，而不是看实际醒来时的日期。即使 00:03 的任务到 00:05 才醒，
  仍然发送昨天，不会误发今天。
- **「今天发过没有」落在数据库里**，不是内存标记（`mail_log` 表，
  `UNIQUE KEY (user_key, day)`）。半夜重启一次就重发一封，是这类功能最常见的毛病。
- **漏了会补。** 启动时和每次发送时会顺带补发前 `TIMETRACE_MAIL_CATCHUP_DAYS`
  天里没发成功过的日子。已经发过的靠上面那个唯一键挡掉，不会重复发。
  启动补发**只补已经到达 00:03 发送期限的日子**：00:03 前重启不会提前发送昨天，
  到点后才把昨天纳入补发，确保 3 分钟数据收集缓冲不被启动流程绕过。
  补发出来的种类也是那一天**该有的那种**：种类只看日期，不看「什么时候跑的」，
  所以补出来的周日那封仍然是周报。

失败不会被吞掉：SMTP 的报错原文写进 `mail_log`（`status=failed`），日志里也有一份，
`GET /api/v1/admin/mail-log` 就能回答「为什么没收到」。失败的日子不算「发过」，
所以下一次运行还会重试；重试成功会覆盖成 `sent`，不会留下两条记录。

两个手工入口（都要 `X-Admin-Token`）：

```bash
# 先看看内容长什么样，不真发（dry_run 不会写任何「已发送」标记）
curl -X POST "http://127.0.0.1:8000/api/v1/admin/daily-mail?dry_run=true&catchup=1" \
     -H "X-Admin-Token: $TOKEN"

# 想预览周报，就把 day 传成过去某个周日（发的是哪个种类由那一天决定）
curl -X POST "http://127.0.0.1:8000/api/v1/admin/daily-mail?day=2026-09-27&dry_run=true" \
     -H "X-Admin-Token: $TOKEN"

# 真发；也可以只给一台设备发，这时忽略它的订阅开关（用户主动要求重发时用）
curl -X POST "http://127.0.0.1:8000/api/v1/admin/daily-mail?user_key=<设备ID>" \
     -H "X-Admin-Token: $TOKEN"
```

外部 cron 部署**不需要知道周报这回事**：照旧调 `POST /api/v1/admin/daily-mail`，
周日那次自然就是周报。记得同时设 `TIMETRACE_MAIL_SCHEDULER=0` 关掉进程内定时器，
免得两处重复发。

> **多 worker 注意**：定时器跑在服务端进程内。用 `uvicorn --workers N` 会有 N 个
> 定时器同时到点，`mail_log` 的唯一键能兜住绝大多数重复，但「查过没发」和
> 「写记录」之间仍有理论上的竞态窗口。要么单 worker 跑（这个量级完全够），要么设
> `TIMETRACE_MAIL_SCHEDULER=0` 关掉它，改用系统 cron 调上面的接口。
>
> **QQ / 163 这类邮箱的 SMTP 密码是「授权码」**，要在邮箱设置里单独开启并生成。
> 填账号登录密码会一直认证失败，而报错通常只说「认证失败」，不容易联想到是这里。

## 上线前必做

> ⚠️ 部署前必做三件事：
> 1. **别用 root 连数据库**。生产环境请建一个只对这个库有增删改查权限的账号，
>    把用户名密码填进 `server/.env`。
> 2. **别把 `.env` 提交上去**。它是明文存密码的，`.gitignore` 里已经忽略了；
>    部署时单独把这份文件传过去，别用 `git add -f` 硬塞。
> 3. **放开端口**。云服务器安全组要放行 8000（客户端上传用）。3306 只在服务端本机用，
>    **不要**对公网开放。
>
> 应用和已授权的网站明细支持 HTTP 或 HTTPS。HTTP 传输未加密；公网部署建议
> 在前面挂一层 Nginx 配 TLS，把客户端 `.env` 中的 `TIMETRACE_SERVER_URL`
> 设为完整 HTTPS 根 URL。支持域名或包含 IP SAN 的公网 IP 证书。
> 网站统计与浏览器配对见 [客户端说明](../client/README.md)，服务器升级见 [独立部署说明](deploy/README.md)。

## 技术栈

- **Web 框架**: FastAPI + uvicorn
- **数据库**: MySQL 5.7 / 8（驱动选 PyMySQL，纯 Python，Windows 上装得省事）。
  实测过的云端那台是 **5.7.43**，所以 `db/` 里（含 `db/schema.sql`）都刻意只用了
  5.7 也支持的写法：不写 `utf8mb4_0900_ai_ci`，`ON DUPLICATE KEY UPDATE`
  用兼容性更好的 `VALUES()` 而不是 8.0.19+ 才有的行别名语法
- **数据校验**: Pydantic v2

## 测试

服务端相关的测试在仓库根目录的 `tests/` 下（`test_routes.py`、`test_db.py`、
`test_mailer.py`、`test_config.py`、`test_envfile.py`），跑法见
[根 README 的「测试」](../README.md#测试)。

> 这套测试**不碰数据库、不发真邮件** —— `db` 包里每个函数都被换成了假实现，
  `smtplib` 和 `sender.send_mail` 也都是假的。跑完不该产生任何网络请求。
