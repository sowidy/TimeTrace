"""程序入口：启动「屏幕使用时间统计」这个桌面小工具。

启动流程（按顺序）：
    1. 保证同一时间只有一个程序实例在跑（用 Windows 互斥量实现）
    2. 初始化数据库（建表）
    3. 创建 Qt 应用对象
    4. 如果是第一次运行，弹出引导窗口让用户做初始设置
    5. 启动后台监控线程，开始记录你在用什么软件
    6. 启动两个后台定时器：一个把数据导出成 CSV，一个把数据上传到服务端
    7. 在系统托盘（右下角那一小块区域）显示图标，并进入事件循环

程序常驻在托盘里运行，关闭窗口不会退出程序，只有托盘菜单里的「退出」才会真正结束。
"""
import sys
import os
import ctypes
import ctypes.wintypes as wintypes

# 让下面那些 `from core.storage import ...` 能找到目标模块。
#
# 源码模式：client/ 下的 core / system / ui / net 都是「包」，只有把 client/
# 本身放进模块搜索路径，才能用 `from core.storage import ...` 这种以 client/
# 为根的绝对导入。这也是 app.spec 里 pathex 要写 client/ 的原因——两边必须
# 用同一套根，否则源码能跑、打包后却报找不到模块。
#
# 打包模式：这些模块已经被打进 exe 内部的归档，由 PyInstaller 自带的导入器
# 负责查找，sys.path 其实用不上；这里保留一句只是兜底。
if getattr(sys, 'frozen', False):
    # sys.frozen 为 True 说明当前是 PyInstaller 打包出来的可执行文件
    sys.path.insert(0, os.path.dirname(sys.executable))
else:
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from PySide6.QtWidgets import QApplication

from core.storage import init_db, is_first_run, get_last_unfinished_fragment
from system.monitor import WindowMonitor
from core.exporter import ExportScheduler
from net.uploader import UploadScheduler
from ui.tray import TrayApp, _create_tray_icon


# 互斥量的名字。带 "Local\\" 前缀表示只在当前用户会话内有效。
# 同时持有旧版互斥量，避免升级期间新旧两个程序一起统计同一段时间。
_INSTANCE_MUTEX_NAMES = ("Local\\TimeTrace", "Local\\PcTime", "Local\\DABIAN-afk.UsageTracker")


def _acquire_single_instance():
    """尝试独占一个系统级互斥量，用来防止程序被重复启动。

    原理：Windows 的 CreateMutexW 创建一个具名互斥量，如果这个名字已经被
    创建过，函数会返回一个有效句柄，但 GetLastError() 会变成 183
    （ERROR_ALREADY_EXISTS），据此就能判断「已经有一个我在跑了」。

    返回：
        两个互斥量句柄。如果新旧任一版本已在运行，返回 None。
    """
    kernel32 = ctypes.windll.kernel32
    # ctypes 默认把返回值当成 32 位 int，句柄在 64 位系统上可能被截断，
    # 所以必须显式声明返回类型为 HANDLE。
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    handles = []
    for name in _INSTANCE_MUTEX_NAMES:
        handle = kernel32.CreateMutexW(None, False, name)
        if not handle or kernel32.GetLastError() == 183:
            if handle:
                kernel32.CloseHandle(handle)
            for acquired in handles:
                kernel32.CloseHandle(acquired)
            return None
        handles.append(handle)
    return tuple(handles)


def main():
    instance_mutex = _acquire_single_instance()
    if instance_mutex is None:
        # 已经有另一个实例在运行，本进程直接静默退出
        return

    init_db()  # 建表 + 初始化默认设置，必须在任何数据库读写之前调用
    from system.autostart import migrate_autostart
    migrate_autostart()

    app = QApplication(sys.argv)
    app.setApplicationName("TimeTrace")
    # 默认情况下 Qt 在「最后一个窗口关闭」时会退出程序。
    # 本程序关掉仪表盘窗口后还要继续在托盘里统计，所以要关掉这个行为。
    app.setQuitOnLastWindowClosed(False)
    app.setWindowIcon(_create_tray_icon())

    # 让界面配色跟随系统深浅色模式。必须在创建任何窗口之前完成切换，
    # 因为颜色变量会被界面构造时读走，之后改就晚了。
    # 这里才 import dashboard，是因为该模块加载时会探测系统字体（需要
    # QApplication 已存在，否则 Qt 会崩溃）。
    from ui.dashboard import apply_theme, system_is_dark
    apply_theme(system_is_dark())

    # 首次运行引导
    if is_first_run():
        from ui.dashboard import _FirstRunWizard
        wizard = _FirstRunWizard()
        wizard.exec()  # exec() 会阻塞在这里，直到用户点「开始使用」关掉窗口

    # 崩溃恢复：检测上一次异常退出
    # 上一次异常退出时，最后一条记录在数据库里可能没有正常收尾。
    # 这里只是查一下留个记录点，实际不需要补数据——因为监控线程启动后会从「当前时刻」重新开始计时，历史片段已经在 flush 时写进去了。
    last = get_last_unfinished_fragment()
    if last:
        # 监控启动后会自动从当前时间重新开始计时
        # 上一次未结束的片段已写入 end_time，无需额外处理
        pass

    monitor = WindowMonitor(poll_interval=1.0)  # 每 1 秒采样一次当前前台窗口
    from browser.bridge import BrowserBridge
    app.browser_bridge = BrowserBridge(app)
    app.aboutToQuit.connect(app.browser_bridge.shutdown)
    monitor.activity_callback = app.browser_bridge.activity.emit
    monitor.start()  # 内部会开一个后台守护线程，不阻塞主线程

    # 定时导出：把使用时间自动写成 CSV，按 年/月/日 存到 data 目录下。
    # 同样是后台守护线程，启动时会先立刻导一次（补齐上次关掉程序期间的空档）。
    scheduler = ExportScheduler()
    scheduler.start()

    # 定时上传：把每天的使用记录发到服务端（config.py 里配的地址）。
    # 也是后台守护线程，起来后会先立刻跑一次 —— 所以「关了一周再开机」
    # 这中间漏掉的日子会被自动补传。用户在设置里关掉自动上传后，这个
    # 线程还在转，但每一轮都会读到开关为「关」而什么都不做。
    upload_scheduler = UploadScheduler()
    upload_scheduler.start()

    tray = TrayApp(monitor, scheduler, upload_scheduler)
    tray.show()  # 让托盘图标显示出来

    # 进入 Qt 事件循环：程序会一直停在这一行，直到托盘菜单点「退出」
    # 触发 QApplication.quit()，exec() 才返回。返回值是退出码。
    exit_code = app.exec()
    app.browser_bridge.timer.stop()
    app.browser_bridge.stop()
    # 退出时把后台线程都停掉。导出线程会等一下正在写的那次导出写完，
    # 避免 CSV 写一半；上传线程同理，最多等 5 秒。
    scheduler.stop()
    upload_scheduler.stop()
    # 正常退出时释放互斥量，这样下次启动还能正常抢到
    for handle in instance_mutex:
        ctypes.windll.kernel32.CloseHandle(handle)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
