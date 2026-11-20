"""集中管理程序用到的各种目录 / 文件路径。

为什么单独抽出这个模块：
    以前「数据库放哪」「图标放哪」这类问题散落在 storage.py、tray.py 等
    各个文件里，各自用 __file__ 拼路径。文件一多，谁都能拼错，而且
    「打包成 exe 后路径该变」这条规则要重复写好几遍。
    现在全部收在这里，其它模块只管调用，不用关心细节。

两种运行模式（贯穿全项目的约定）：
    * 源码模式：直接 python client/main.py 跑。此时数据放在 client/ 目录下，
      跟着客户端代码走，一眼能看到文件。
    * 打包模式（PyInstaller）：sys.frozen 为 True。此时程序目录可能是
      只读的（比如装在 Program Files 下），所以数据和导出结果统一改放到
      用户目录 %APPDATA%\\TimeTrace\\ 下；旧安装目录继续可用。

关于本文件的位置（改目录结构时最容易踩的坑）：
    本文件在 client/core/ 里，比拆分前（src/paths.py）深了一层，所以下面
    「往上退几级」的数字全都跟着调过。client_dir() 是唯一的出口——以后
    再改结构，只需要改那一个函数，不要在别处重新拼 __file__。

注意 asset_path() 是个例外：它不看上面那两种模式。因为打包时图标等资源
会被 PyInstaller 放到临时解包目录，而 __file__ 正好指向那里，所以直接用
__file__ 反而在两种模式下都正确。
"""
import os
import sys

# 打包后数据目录的文件夹名（放在 %APPDATA% 下面）
_APP_DIR_NAME = "TimeTrace"
# 旧目录继续可用：先 PcTime（上一个名字），再 UsageTracker（更早的名字）。
_LEGACY_APP_DIR_NAMES = ("PcTime", "UsageTracker")


def is_frozen():
    """是否运行在 PyInstaller 打包出来的 exe 里。

    返回：True 表示打包模式，False 表示源码模式。
    """
    return getattr(sys, 'frozen', False)


def client_dir():
    """客户端目录（client/）的完整路径。

    本文件在 client/core/ 里，所以从自己的位置往上退两级就是 client/：
        client/core/paths.py -> client/core -> client

    源码模式下的数据库和导出目录都挂在它下面。
    打包模式下这个函数没有意义，调用前请先用 is_frozen() 判断。
    """
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def app_data_dir():
    """放数据的地方（数据库、导出的 CSV 都在它下面）。

    打包模式：新安装使用 %APPDATA%\\TimeTrace\\；如果旧目录已有数据，继续使用旧目录。
    源码模式：client/ 目录本身。
    """
    if is_frozen():
        # os.environ.get 取不到 APPDATA 时退回用户主目录，保证总有个可写的地方
        appdata = os.environ.get('APPDATA', os.path.expanduser('~'))
        d = os.path.join(appdata, _APP_DIR_NAME)
        legacy_dirs = [os.path.join(appdata, name) for name in _LEGACY_APP_DIR_NAMES]
        # 新目录可能因先放 .env 而已存在；只要尚无新数据库，仍以旧数据库为准。
        # 不在运行时搬动 SQLite（包括 WAL），避免升级过程中遗漏未落盘的记录。
        # 旧目录按 PcTime → UsageTracker 的顺序找（越新的旧名越优先）。
        db_legacy = next(
            (legacy for legacy in legacy_dirs
             if os.path.isfile(os.path.join(legacy, "usage.db"))
             and not os.path.isfile(os.path.join(d, "usage.db"))),
            None)
        if db_legacy is not None:
            d = db_legacy
        elif not os.path.exists(d):
            d = next((legacy for legacy in legacy_dirs if os.path.exists(legacy)), d)
        os.makedirs(d, exist_ok=True)   # exist_ok=True：已存在不报错
        return d
    return client_dir()


def program_dir():
    """程序自己所在的那个目录 —— 放「跟着程序走」的文件。

    打包模式：exe 所在目录。用户把压缩包解开就在这一层，想改配置一眼能看见。
    源码模式：client/ 目录，也就是 client_dir()。

    为什么不能像 asset_path() 那样直接用 __file__：打包后 __file__ 指向
    PyInstaller 的临时解包目录，那个目录每次启动都重建，往里放 .env 等于
    白放。要找 exe 本身的位置，只能问 sys.executable。
    """
    if not is_frozen():
        return client_dir()
    exe = getattr(sys, "executable", "") or ""
    # sys.executable 理论上不会为空，真空了就退回数据目录——总比返回 ""
    # 让调用方去拼一个相对路径要好
    return os.path.dirname(os.path.abspath(exe)) if exe else app_data_dir()


def env_file_candidates():
    """.env 的候选位置，按优先级排好，并且去掉重复项。

    先程序目录再生效目录：用户改配置第一个会去翻的地方是「程序在哪」，
    而 %APPDATA% 那边的兜底是为了「程序装在 Program Files 这类只读位置」——
    那时候用户想在程序目录放 .env 得用管理员权限。

    源码模式下 program_dir() 和 app_data_dir() 是同一个目录，去重后只剩一条。
    """
    found = []
    for d in (program_dir(), app_data_dir()):
        candidate = os.path.join(d, ".env")
        if candidate not in found:
            found.append(candidate)
    return found


def db_path():
    """数据库文件 usage.db 的完整路径。"""
    return os.path.join(app_data_dir(), "usage.db")


def data_dir():
    """导出的 CSV 存放根目录，下面会再按 年/月/日 分子目录。

    同样会确保目录存在，这样调用方拿到路径后可以直接写文件。
    """
    d = os.path.join(app_data_dir(), "data")
    os.makedirs(d, exist_ok=True)
    return d


def asset_path(name):
    """取随程序一起发布的资源文件（如「图标.png」）的完整路径。

    参数：
        name：资源文件名，例如 "图标.png"。

    资源统一放在 client/assets/ 下。这里对两种模式用同一套算法是刻意的：
    源码模式下 __file__ 在 client/core/，退两级正好是 client/；打包后
    __file__ 指向 PyInstaller 解包目录里的 core/paths.pyc，退两级正好是
    解包目录根，而 app.spec 的 datas 就是按 assets/ 这个相对路径解包的。
    两种情况因此都能找到。
    """
    return os.path.join(client_dir(), "assets", name)
