"""系统托盘图标：右下角那个小图标，以及它的右键菜单。

程序没有传统的主窗口常驻，而是缩在系统托盘里运行。本模块负责：
    * 准备托盘要显示的图标（优先用项目自带的图片，没有就自己画一个）
    * 搭建右键菜单：查看统计 / 暂停统计 / 开机自启 / 退出
    * 把这些菜单项连到具体的动作上


Qt 的信号槽机制：`对象.信号.connect(函数)` 的意思是「当这个信号发出时，
去调用这个函数」。例如 `btn.clicked.connect(self._quit)` 就是点击时执行 _quit。
"""
import os
import threading

from PySide6.QtWidgets import QSystemTrayIcon, QMenu
from PySide6.QtGui import QIcon, QPixmap, QPainter, QColor
from PySide6.QtCore import Qt

from core.paths import asset_path
from system.autostart import is_autostart_enabled, toggle_autostart
# BgWorker 把上传挪到后台线程，完成后把结果送回界面线程（见该模块的说明）。
# 它只依赖 QtCore，所以放在这里 import 是安全的：本模块会在 QApplication
# 创建之前就被 main.py 导入。
from ui.bgworker import BgWorker
from net import uploader


def _create_tray_icon():
    """加载自定义图标，裁剪为圆形，不存在则用默认绘制。

    两种情况：
        1. 项目目录下有「图标.png」→ 读进来，用 QPainter 裁成圆形（去掉方角）
        2. 没有图片 → 用代码画一个蓝底白指针的「时钟」图标当兜底方案

    返回：QIcon（Qt 里表示图标的类型）。
    """
    # 图片现在放在 client/assets/ 下，路径交给 paths.asset_path() 统一解析。
    # 打包后资源会被 PyInstaller 解到解包目录根，asset_path() 对两种模式
    # 已经处理好了，这里不用再自己拼 __file__。
    icon_path = asset_path("图标.png")
    if os.path.exists(icon_path):
        src = QPixmap(icon_path)
        if not src.isNull():  # isNull() 为真说明图片解码失败
            size = 64
            # 先准备一块 64x64 的透明画布
            px = QPixmap(size, size)
            px.fill(QColor(0, 0, 0, 0))  # 全透明填充（第四个参数 0 是 alpha）
            p = QPainter(px)  # QPainter 是 Qt 的画笔，用来往画布上画东西
            p.setRenderHint(QPainter.Antialiasing)          # 抗锯齿，边缘更平滑
            p.setRenderHint(QPainter.SmoothPixmapTransform)  # 缩放图片时平滑处理
            # 圆形裁剪
            # 思路：先画一个白色实心圆，然后把合成模式切成 "只保留重叠部分"，
            # 再把方形原图贴上去——结果就只剩下圆形范围内的那部分图像。
            p.setBrush(QColor(255, 255, 255))  # 画刷 = 填充色
            p.setPen(Qt.NoPen)                 # 画笔 = 边框，NoPen 表示不画边框
            p.drawEllipse(0, 0, size, size)
            p.setCompositionMode(QPainter.CompositionMode_SourceIn)  # 只保留重叠区域
            # 按「短边填满」的方式缩放原图，避免图片被拉伸变形
            scaled = src.scaled(size, size, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            # 缩放后可能比画布大，这里算出居中的起始坐标，相当于居中裁剪
            x = (scaled.width() - size) // 2
            y = (scaled.height() - size) // 2
            p.drawPixmap(0, 0, scaled, x, y, size, size)
            p.end()  # 用完 QPainter 一定要 end()，否则画布内容可能不完整
            return QIcon(px)
    # fallback: 自绘
    # 没有图片资源时的兜底：手画一个时钟图标
    px = QPixmap(64, 64)
    px.fill(QColor(0, 0, 0, 0))
    p = QPainter(px)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QColor(10, 132, 255))  # 蓝色圆底
    p.setPen(Qt.NoPen)
    p.drawEllipse(2, 2, 60, 60)  # 留 2px 边距，相当于内缩一圈
    p.setPen(QColor(255, 255, 255))  # 换成白色画笔来画指针
    p.drawLine(32, 16, 32, 32)  # 分针：从中心往上
    p.drawLine(32, 32, 46, 38)  # 时针：从中心往右下
    p.setBrush(QColor(255, 255, 255))
    p.drawEllipse(29, 29, 6, 6)  # 中心的小圆点
    p.end()
    return QIcon(px)


class TrayApp(QSystemTrayIcon):
    """托盘图标控件，继承自 Qt 的 QSystemTrayIcon。

    这里同时扮演两个角色：
        * 显示托盘图标和菜单
        * 作为「仪表盘窗口」的持有者（懒加载：用户第一次点开才创建）
    """

    def __init__(self, monitor, scheduler=None, upload_scheduler=None, parent=None):
        """创建托盘图标。

        参数：
            monitor：WindowMonitor 实例，用来暂停/恢复统计。
            scheduler：ExportScheduler 实例，用来手动触发一次数据导出。
                允许为 None（比如单独测试托盘时），此时「导出数据」菜单项
                点了不会有动作。
            upload_scheduler：UploadScheduler 实例，用来手动触发一次上传。
                同样允许为 None。
            parent：Qt 父子对象，一般不用传。
        """
        super().__init__(parent)
        self.monitor = monitor    # WindowMonitor 实例，用来暂停/恢复统计
        self.scheduler = scheduler  # ExportScheduler 实例，可为 None
        self.upload_scheduler = upload_scheduler  # UploadScheduler 实例，可为 None
        self.dashboard = None     # 仪表盘窗口，第一次打开时才创建（见 _show_dashboard）

        # 手动上传是网络操作，要在后台线程里跑，结果通过信号回到界面线程。
        # 为什么要费这个劲：托盘菜单点了以后如果卡十几秒（网络超时 + 重试），
        # 表现是菜单一直不消失、整个程序像死了 —— 比不做这个功能还糟。
        self._worker = BgWorker()
        self._worker.done.connect(self._on_upload_done)

        self.setIcon(_create_tray_icon())
        self.setToolTip("TimeTrace · 屏幕时间统计")  # 鼠标悬停在图标上时显示的文字

        menu = QMenu()

        act_show = menu.addAction("查看统计")
        act_show.triggered.connect(self._show_dashboard)

        # 存成属性，因为后面要根据暂停状态改它的文字（暂停中 ↔ 继续统计）
        self.act_pause = menu.addAction("暂停统计")
        self.act_pause.triggered.connect(self._toggle_pause)

        # 手动导一次数据（平时是后台自动导，这个用于「我现在就想拿到 CSV」）
        act_export = menu.addAction("立即导出数据")
        act_export.triggered.connect(self._export_now)

        # 手动传一次（平时是后台每天自动传，这个用于「我现在就想传上去」）。
        # 存成属性是因为上传期间要把它禁掉，防止用户连点。
        self.act_upload = menu.addAction("立即上传数据")
        self.act_upload.triggered.connect(self._upload_now)

        self.act_autostart = menu.addAction("开机自启")
        self.act_autostart.setCheckable(True)  # 变成可以打勾的菜单项
        self.act_autostart.setChecked(is_autostart_enabled())  # 初始勾选状态读注册表
        self.act_autostart.triggered.connect(self._toggle_autostart)

        menu.addSeparator()  # 分隔线，把「退出」和上面的功能项隔开

        act_quit = menu.addAction("退出")
        act_quit.triggered.connect(self._quit)

        self.setContextMenu(menu)  # 右键时弹出这个菜单
        self.activated.connect(self._on_activated)  # 处理左键单击等激活事件

    def _show_dashboard(self):
        """打开统计窗口；已经创建过就复用同一个窗口。"""
        if self.dashboard is None:
            # 在函数内部 import，是为了避免 dashboard 和 tray 在模块加载时互相
            # 循环依赖（dashboard 本身不 import tray，但放在这里更省启动时间）
            from ui.dashboard import Dashboard
            self.dashboard = Dashboard(self.monitor)
        self.dashboard._refresh()   # 每次打开都刷新一遍数据，避免显示旧数字
        self.dashboard.show()
        self.dashboard.raise_()          # 从其它窗口底下提到最前面
        self.dashboard.activateWindow()  # 让窗口获得键盘焦点

    def _toggle_pause(self):
        """暂停/恢复统计，并同步更新菜单项文字。"""
        if self.monitor.is_paused:
            self.monitor.resume()
            self.act_pause.setText("暂停统计")
        else:
            self.monitor.pause()
            self.act_pause.setText("继续统计")

    def _export_now(self):
        """点「立即导出数据」：立刻跑一次导出，并用气泡提示结果。

        run_once() 内部已经把所有异常都兜住了，所以这里不用担心它会崩；
        出问题时会记在 scheduler.last_error 里，读出来提示给用户。
        """
        if self.scheduler is None:
            return
        written = self.scheduler.run_once()
        if self.scheduler.last_error:
            self.showMessage("导出失败", self.scheduler.last_error,
                             QSystemTrayIcon.Warning, 5000)
        elif written:
            # 只报个数就够了，完整路径太长，托盘气泡显示不下
            self.showMessage("导出完成", f"已生成 {len(written)} 个 CSV 文件",
                             QSystemTrayIcon.Information, 4000)
        else:
            self.showMessage("导出完成", "没有需要导出的新数据",
                             QSystemTrayIcon.Information, 4000)

    def _upload_now(self):
        """点「立即上传数据」：在后台线程里传一次。

        这里**只负责启动**，结果由 _on_upload_done 回来处理 —— 网络操作
        不能在界面线程里同步做，否则菜单会卡住十几秒（见 __init__ 里的说明）。
        """
        if self.upload_scheduler is None:
            return
        self.act_upload.setEnabled(False)
        self.act_upload.setText("正在上传…")
        # force=True：用户主动点的，不该被「自动上传已关闭」挡住。
        # daemon=True：程序退出时不会因为这个线程而挂住。
        threading.Thread(
            target=self._worker.run,
            args=("upload", lambda: uploader.upload_all(force=True)),
            daemon=True,
        ).start()

    def _on_upload_done(self, name, result):
        """上传完成。**跑在界面线程里**（Qt 的队列连接保证的）。"""
        if name != "upload":
            return
        self.act_upload.setEnabled(True)
        self.act_upload.setText("立即上传数据")
        msg = uploader.describe_result(result)
        self.showMessage(
            "上传" if result.get("ok") else "上传失败", msg,
            QSystemTrayIcon.Information if result.get("ok") else QSystemTrayIcon.Warning,
            5000,
        )

    def _toggle_autostart(self):
        """切换开机自启，并把菜单勾选状态同步成真实的注册表状态。"""
        enabled = toggle_autostart()
        self.act_autostart.setChecked(enabled)

    def _on_activated(self, reason):
        """托盘图标被操作时触发。reason 说明是哪种操作。

        这里只处理「单击」（Trigger，通常是鼠标左键单击），双击或中键不做响应。
        """
        if reason == QSystemTrayIcon.Trigger:
            self._show_dashboard()

    def _quit(self):
        """真正退出程序：关窗口 → 停监控（并落盘）→ 退出 Qt 事件循环。"""
        if self.dashboard:
            self.dashboard.close()
        # stop() 内部会把还没写入数据库的计时片段补写完，不能省略
        self.monitor.stop()
        # 局部 import，避免模块顶部就依赖 QtWidgets 的整个命名空间
        from PySide6.QtWidgets import QApplication
        QApplication.quit()
