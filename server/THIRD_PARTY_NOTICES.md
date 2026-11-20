# 第三方来源与许可证

核对日期：2026-10-25。本项目仓库为 https://github.com/sowidy/TimeTrace。
项目新增贡献按随附 `LICENSE` 的 MIT 条款提供；上游代码、图片和依赖保留其原有权利，
本项目的许可证不替代第三方许可证，也不代表所有第三方内容已完成授权确认。

## DABIAN-afk/usagetime：客户端基础来源

- 来源：https://github.com/DABIAN-afk/usagetime
- 核对版本：`0928decb6a576c0276bbd001cf8c615553109fb1`。
- 本项目客户端在其应用统计、SQLite 存储、界面、托盘、图标处理和打包基础上重构、扩展。
  例如原 `storage.py` 对应 `client/core/storage.py`，`dashboard.py` 对应
  `client/ui/dashboard.py`，`monitor.py` 对应 `client/system/monitor.py`。
- 该版本 README 的 License 部分声明 **MIT**，但未找到独立的许可证全文和版权署名行。
  本项目保留声明及证据于 `licenses/DABIAN-afk-usagetime-NOTICE.txt`，不虚构上游年份、
  版权人或授权确认。当前取得的是 README 声明，完整上游许可记录仍待补充。
- 客户端自带 `图标.png` 等素材的独立权属未完成核对，不因本项目新增 MIT 文件而
  自动取得新的授权；正式发行前应确认素材许可或使用自有、明确许可的替代素材。

## Planshit/Tai：网站统计设计参考

- 来源：https://github.com/Planshit/Tai
- 参考版本：`566062dace9591990be9b94190b0f86c34497905`。
- 参考浏览器扩展报告活动网页、桌面端结合前台与空闲状态计算使用时长的设计。
  当前网站功能采用本项目的 Python/JavaScript 实现；此处不声称对全仓库完成逐行来源审计。
- 上游许可证：**MIT**；原始版权行：`Copyright (c) 2022 Plan shit`。
- 完整原文保存在 `licenses/Tai-MIT.txt`，保留版权、许可条件和免责声明。

两个来源项目均未被表示为本项目的背书方。本项目自建上传、服务端与邮件的行为，
不能用上游“纯本地、不上传”的隐私说明代替，应以本项目实际配置和隐私说明为准。

## 运行和打包依赖

以下为核对时的主要依赖清单，实际发行以构建时安装的具体版本及其原文为准。
发行物中的 `licenses/dependencies/` 由打包脚本从已安装包中收集许可证及声明，
`DEPENDENCIES.json` 记录该次构建的名称、版本、许可元数据和收集文件。

| 组件 | 上游许可范围 | 用途 |
| --- | --- | --- |
| PySide6 / Qt / Shiboken | LGPLv3 / GPL 或商业条款，按具体模块核对 | 桌面界面、WebSocket |
| Pillow | MIT-CMU，附带库可能有其他许可 | 客户端图片和图标处理、服务端邮件 PNG 图表 |
| psutil | BSD-3-Clause | 进程信息 |
| pywin32 | 包内 PSF 等许可及版权声明 | Windows API |
| FastAPI / Pydantic / PyMySQL | MIT | API、校验和 MySQL 驱动 |
| Uvicorn | BSD-3-Clause | HTTP 服务 |
| cryptography | Apache-2.0 或 BSD-3-Clause，附带库另有声明 | 数据库认证等加密支持 |
| Python | PSF 及其所含第三方许可 | 解释器与标准库 |
| PyInstaller | GPLv2-or-later + 打包例外，少量文件 Apache-2.0 | 可执行文件构建 |

PyInstaller 的打包例外不替代 Qt 等其他依赖的要求。MySQL、Docker 基础镜像、系统库
和系统提取的第三方应用图标仍由其各自许可证或权利约束；不得把它们统一标为 MIT。

## Qt 与发行要求

Qt/PySide6 不属于本项目 MIT 授权。选择 LGPLv3 路径时，应保留库的版权声明和
LGPLv3/GPLv3 原文，允许用户替换、修改和调试所用动态库，不额外禁止为修改库而进行
的逆向工程。客户端采用 PyInstaller onedir；实际替换兼容库的可运行性仍需按发行环境验证。
发行时还需提供所用库的完整对应源码及必要构建/安装信息，或采用许可证允许的有效
源码提供方式；一个项目主页链接或这一份声明本身不能代替该义务。Qt 所含第三方库
和仅 GPL 的模块也应按实际打入的文件逐项核对。

官方说明：https://www.qt.io/development/open-source-lgpl-obligations
和 https://doc.qt.io/qtforpython-6/licenses.html 。
PyInstaller 说明：https://pyinstaller.org/en/stable/license.html 。

## 发布时保留的文件

源码及独立服务端、客户端、扩展包应保留 `LICENSE`、`NOTICE`、本文件和 `licenses/`。
单独复制组件时请一起复制该组件内的法律文件；不要只在网页上致谢而从发行包中删掉。
本次补齐的是许可与来源记录、自动携带声明的打包路径；DABIAN-afk 上游完整许可记录、
素材权属及具体 Qt 二进制发行的源码提供仍应在相应发行前落实。
