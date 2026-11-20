"""仪表盘界面模块：整个应用的主窗口就定义在这里。

在整个项目中的位置（谁调用它、它调用谁）：
    调用它的：``tray.py``（托盘）里会 ``Dashboard(self.monitor)`` 创建主窗口，
    并在用户点托盘图标时把它显示出来；``main.py``（程序入口）则在首次运行时
    弹出本文件里的 ``_FirstRunWizard`` 引导窗口。
    它调用的：所有历史统计数据的读取、设置项的读写都来自 ``storage.py``；
    应用图标由 ``icons.py`` 画出来；开机自启的开关交给 ``autostart.py``；
    暂停/继续统计则直接操作构造时传进来的 ``monitor``（``monitor.py``）。

界面由三个标签页拼成：
    今日（每小时柱状图 + 应用使用排行）、
    本周（每日柱状图 + 应用使用排行）、
    设置（隐私说明、无操作休眠、开机自启）。

阅读提示：本文件里的图表全部是「手绘」的——没有使用任何图表库，而是在
``paintEvent`` 里用 ``QPainter`` 一笔一笔画出来的。好处是可以完全控制外观，
代价是所有坐标都得自己算，所以下面的注释会重点解释坐标是怎么来的。
"""

import os
import threading
from datetime import date, timedelta, datetime
from core.timeutil import beijing_now, beijing_today

# PySide6 是 Qt 的官方 Python 绑定。QtWidgets 里装的是「看得见的控件」：
# QWidget 是所有可视控件的基类（本文件的好几个自定义图表都继承它），
# QVBoxLayout / QHBoxLayout 是布局管理器（垂直 / 水平排列），
# QLabel 显示文字，QPushButton 是按钮，QFrame 是能显示背景的矩形框
# （本文件用它做「卡片」），QScrollArea 提供滚动，QCheckBox 是勾选框，
# QSlider 是滑块，QDialog 是独立的小对话框，QMessageBox 是系统提示框。
# QLayout 是所有布局的基类（_WaterfallLayout 要继承它）。
from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QLabel, QScrollArea,
    QPushButton, QFrame, QMessageBox, QDialog, QCheckBox,
    QComboBox, QLineEdit, QStackedWidget, QSlider, QApplication,
    QFileDialog, QDateEdit, QLayout, QToolTip,
)
# QtCore 是「不涉及界面」的基础设施：Qt 是各种枚举常量集合
# （如 Qt.AlignCenter 居中、Qt.NoPen 不要描边），QTimer 是定时器，
# QRect 是矩形（位置 + 尺寸），QSize 是宽高。
from PySide6.QtCore import Qt, QTimer, QDate, QRect, QSize
# QtGui 里是「画笔、颜色、字体、位图」这类绘图资源：
# QPainter 负责真正把像素画到屏幕上，QColor 是颜色，QFont 是字体，
# QPixmap / QImage 是两种位图（前者用于显示，后者用于包装原始像素），
# QFontDatabase 可以查询系统装了哪些字体。
from PySide6.QtGui import QFont, QColor, QLinearGradient, QPainter, QPixmap, QImage, QFontDatabase, QPalette

# Pillow：图标是 icons.py 用 Pillow 画的，所以这里要拿到它的 Image 模块
# 做「缩放 + 取像素」的操作。
from PIL import Image

# storage.py 是数据层。本文件只负责「把数据画出来」，从不直接碰数据库；
# 需要什么数据就调下面这些函数，具体存哪、怎么算都在 storage.py 里。
# 另外注意 storage 里的时间计算（比如一周从哪天算起）由它负责，
# 界面这边只传一个日期字符串（ISO 格式，如 "2026-09-22"）过去。
from core.storage import (
    DB_PATH,
    get_stats, get_summary_range, get_daily_total, get_available_dates,
    get_week_stats, get_week_app_stats,
    get_hourly_stats, get_fragments_for_date,
    format_duration, format_duration_short, format_time_short,
    clear_all_data, get_ignored_apps,
    add_ignored_app, remove_ignored_app,
    get_setting, set_setting, get_display_name,
)
# 根据应用名 / 可执行文件路径取出对应的应用图标（返回 PIL 图片）
from system.icons import get_icon
# 自动导出相关的工具。界面这边的「导出 CSV」按钮和后台的定时导出共用
# 同一个写文件函数，保证导出的格式完全一致。
from core.exporter import write_summary_csv, export_all, data_dir

# 数据上传：设置页里的「自动上传」「立即上传」「测试连接」用到。
# 界面这边只负责开关和触发，真正的上传逻辑全在 net/uploader.py 里 ——
# 那个模块刻意不依赖 Qt，因为它要跑在后台线程里。
# BgWorker 负责把网络操作挪到后台线程、再把结果送回界面线程（见 bgworker.py）。
# config 是从 core 包导入的客户端配置（服务端地址、端口都在那儿）。
from core import config
from net import uploader
from ui.bgworker import BgWorker

# ── 字体探测与创建 ────────────────────────────────────────────
# 单独写一节的原因：中文在不同操作系统上可用的字体差别很大，而 Qt 的默认字体在 Windows 上渲染中文可能发虚。
# 所以先探测出一个「系统里确实装了」的中文字体，之后所有字体对象都基于它创建。
def _detect_font():
    """在候选列表里挑出当前系统真正安装了的第一个中文字体，返回字体名。

    返回值：字体名字符串；若一个候选都没找到，返回空字符串 ""，
    调用方（_font）会据此退回 Qt 默认字体。

    为什么需要「候选列表」：这个程序要在 Windows / macOS / Linux 上都能跑，
    而它们自带的中文字体完全不同（macOS 是苹方，Windows 常见微软雅黑，
    Linux 常见思源黑体、文泉驿），无法写死一个名字，只能按顺序挨个试。
    """
    # QFontDatabase.families() 返回系统里所有可用字体名。转成 set 是为了让下面的 `in` 判断变成 O(1)：字体可能有上千个，用 list 逐个比对会明显变慢。
    available = set(QFontDatabase.families())
    # 候选名单（大致按「好看程度 + 各系统常见程度」排列，先试到的先用）：
    # 前三个是 macOS 的苹方，中间是 Windows 的微软雅黑，其余是 Linux 常见的开源中文字体。
    candidates = [
        "PingFang SC", "PingFang HK", "PingFang TC",
        "Microsoft YaHei", "微软雅黑",
        "Noto Sans SC", "Noto Sans CJK SC",
        "Source Han Sans SC", "思源黑体",
        "SimHei", "黑体",
        "WenQuanYi Micro Hei",
        "Hiragino Sans GB",
    ]
    # 挨个问「系统里装了吗」，第一个装了的就直接返回
    for name in candidates:
        if name in available:
            return name
    # 走到这里说明一个候选都没装上（比如精简版 Linux），返回空串表示放弃
    return ""


# 模块加载时只探测一次并缓存起来：字体列表查询本身不算便宜，而界面会创建几十个字体对象，没必要每次都重新扫一遍系统。
_FONT_FAMILY = _detect_font()


def _font(size, bold=False):
    """统一创建字体对象。全项目的文字都通过它拿字体，方便一处换字体全局生效。

    参数：
        size：字号（近似于像素高度的磅值，9~13 是界面常用值，28 是总时长的
            大字号）。
        bold：是否加粗，默认不加粗。标题、数字、当前选中项一般传 True。
    返回值：配置好的 QFont 对象，可直接交给 setFont() 使用。
    """
    if _FONT_FAMILY:
        f = QFont(_FONT_FAMILY, size)
    else:
        # 兜底分支：没找到任何中文字体时用 Qt 默认字体。
        # 注意这里不能用 QFont(None, size) 后设像素大小，因为默认构造函数不带字族，只能事后用 setPixelSize 指定「像素高度」；
        # 乘 1.2 只是让像素高度与上面「磅值」的视觉大小大致对齐。
        f = QFont()
        f.setPixelSize(int(size * 1.2))
    f.setBold(bold)
    return f


# ── 主题配色（浅色 / 深色两套）───────────────────────────────
# 整个界面的配色集中定义在这里，想换配色只改这一段就够了。
# 这些值后面会被拼进 Qt 样式表（setStyleSheet）里用，格式就是普通的 #RRGGBB。
# 为了支持 Windows 的深色模式，这里准备了两套配色：_LIGHT 是默认的浅色，
# _DARK 是深色。程序启动时会读系统设置、调用 apply_theme() 选中一套，
# 把下面这组「模块级变量」（BG、CARD、TEXT……）整体换成对应色值。
# 因为界面里所有地方引用的是这些变量名、而不是写死颜色，所以换一套色值
# 就全局生效，不需要逐个控件改样式。

_LIGHT = {
    "BG":       "#FFFFFF",   # 页面背景（白）
    "CARD":     "#F2F2F7",   # 卡片底色（浅灰），卡片内的小按钮默认也用它
    "HOVER":    "#E5E5EA",   # 鼠标悬停时的高亮底色
    "BORDER":   "#E5E5EA",   # 描边 / 分隔线颜色
    "ACCENT":   "#007AFF",   # 主题色（苹果蓝），用于强调、当前选中、今天的柱子和总时长
    "BLUE_LT":  "#409CFF",   # 主题色的浅色版，用作按钮悬停态
    "TEXT":     "#1C1C1E",   # 主要文字颜色（近黑）
    "GRAY":     "#8E8E93",   # 次要文字颜色（灰）
    "MUTED":    "#AEAEB2",   # 更淡的颜色，用于禁用态 / 空数据提示
    "CHART_BG": "#E5E5EA",   # 图表里「非当前」柱子的颜色
    "GREEN":    "#34C759",   # 正向状态（隐私说明前的对勾）
    "RED":      "#FF3B30",   # 危险操作（清空数据、已忽略标记）
    "AMBER":    "#C77700",   # 「存下了但还没生效」这类中间状态（订阅待同步）
}

# 深色主题：配色参考苹果 iOS 的深色模式，保证白字在黑底上也有足够对比度。
_DARK = {
    "BG":       "#1C1C1E",
    "CARD":     "#2C2C2E",
    "HOVER":    "#3A3A3C",
    "BORDER":   "#3A3A3C",
    "ACCENT":   "#0A84FF",
    "BLUE_LT":  "#409CFF",
    "TEXT":     "#F2F2F7",
    "GRAY":     "#AEAEB2",
    "MUTED":    "#8E8E93",
    "CHART_BG": "#3A3A3C",
    "GREEN":    "#30D158",
    "RED":      "#FF453A",
    "AMBER":    "#FFB340",
}

# 应用排行里给每个应用轮换分配的颜色。时间线视图也用同一套，
# 保证同一个应用在不同视图里颜色一致。用法是「下标对长度取模」。
APP_COLORS = [
    "#007AFF", "#34C759", "#FF9500", "#FF3B30", "#AF52DE",
    "#FF2D55", "#5AC8FA", "#FF6723", "#30B0C7", "#5856D6",
    "#A2845E", "#FF6482", "#0A84FF", "#BF5AF2", "#64D2FF",
]


def apply_theme(dark):
    """把整套配色切换成浅色或深色，并覆盖到模块级颜色变量上。

    参数：
        dark：True 用深色主题，False 用浅色主题。

    原理：界面里所有 setStyleSheet / QColor() 引用的都是 BG、TEXT 这些
    模块级变量名，Python 在运行到那一行时才去查它们的值。所以只要在
    「创建任何控件之前」调一次本函数，把变量重新赋值成深色那套，整个
    界面就会统一变深，无需逐个控件改样式。
    """
    pal = _DARK if dark else _LIGHT
    for name, value in pal.items():
        globals()[name] = value   # 例如把全局变量 TEXT 改成 "#F2F2F7"


def system_is_dark():
    """判断 Windows 当前是否处于深色模式，返回 True / False。

    原理：Windows 10/11 把「应用显示为浅色还是深色」存在注册表里，键值名
    叫 AppsUseLightTheme：0 表示深色、1 表示浅色。读不到（旧系统 / 键不
    存在 / 无权限）就按浅色处理，返回 False。
    """
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
        )
        value, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
        return value == 0
    except Exception:
        return False


# 默认先套浅色，保证这些变量名在模块一加载就存在（例如单独调试界面、
# 测试时没走 main.py 的 apply_theme 也不会报「变量未定义」）。
apply_theme(False)

# 星期几的中文名。DAY_LABELS 用下标取（DAY_LABELS[0] 就是周一，
# 恰好对上 datetime.weekday() 的约定：周一 = 0，周日 = 6）。
DAY_LABELS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
# _CN_DAY 表达同样的映射，只是换成字典，方便「取不到就返回空串」的写法。
_CN_DAY = {0: "周一", 1: "周二", 2: "周三", 3: "周四",4: "周五", 5: "周六", 6: "周日"}

# 产品名 / 窗口标题 / 首次运行引导里的标题都用它
APP_TITLE = "PcTime · 屏幕时间统计"


def _pil_to_pixmap(pil_img, size=24):
    """把 Pillow 的图片对象转换成 Qt 能显示的 QPixmap。

    参数：
        pil_img：icons.py 返回的 PIL.Image，通常是带透明通道的 RGBA 图标。
        size：要缩放成的正方形边长（像素），默认 24。
    返回值：可直接交给 QLabel.setPixmap() 显示的 QPixmap。

    为什么需要转换：
    图标是 icons.py 用 Pillow 画的，而 Qt 只认自己的图像类型，两边不通用，所以中间得把像素数据「搬运」一次。
    """
    # 统一缩放成正方形。LANCZOS 是一种质量较高的重采样算法，
    # 缩小时比默认算法更清晰，图标不会糊。
    pil_img = pil_img.resize((size, size), Image.LANCZOS)
    # 取出原始像素字节。RGBA 表示每个像素占 4 个字节：红、绿、蓝、透明度。
    data = pil_img.tobytes("raw", "RGBA")
    # QImage 直接引用上面这块字节缓冲（不拷贝），所以每行字节数必须正好是
    # size*4，也就是 Format_RGBA8888 所描述的内存布局。
    qimg = QImage(data, size, size, QImage.Format_RGBA8888)
    return QPixmap.fromImage(qimg)


# 说明：原来这里有个 _write_summary_csv()，现在搬到了 exporter.py 里的
# write_summary_csv()。搬家的原因：自动导出功能也要写同样的 CSV，放在
# exporter.py 才能让「手动导出」和「自动导出」共用一份实现，不会出现
# 两边列名不一致的情况。


def _flat_button(text, slot=None):
    """卡片里那种「浅灰底、无边框」的小按钮。

    和 _Card 一样是模块内的私有工具。参数：
        text：按钮上的文字。
        slot：点击时调用的函数；不传就只造一个按钮，调用方自己后面再连。
    """
    btn = QPushButton(text)
    btn.setFixedHeight(30)
    btn.setCursor(Qt.PointingHandCursor)   # 鼠标移上去变成「小手」
    btn.setStyleSheet(
        f"QPushButton{{background:{CARD};color:{TEXT};border:none;"
        f"border-radius:8px;padding:0 14px;font-size:12px;}}"
        f"QPushButton:hover{{background:{HOVER};}}"
        # 禁用态（上传中）要能看出「现在点不了」，否则用户会反复点
        f"QPushButton:disabled{{color:{MUTED};}}")
    if slot is not None:
        btn.clicked.connect(slot)
    return btn


class _Card(QFrame):
    """通用「卡片」容器：一个带圆角、浅灰底色的矩形块。

    界面里所有「一块一块」的区域（顶部的总时长卡、图表卡、应用列表卡、设置页的每个小节）都是它。
    继承 QFrame 只是因为 QFrame 本身能显示背景，把它塞进布局、再往里面放子控件就行。

    类名以下划线开头是 Python 的约定：表示这是本模块内部使用的「私有」类，
    外部模块不应该直接引用它。
    """

    def __init__(self, parent=None):
        """创建一个卡片。

        参数：
            parent：Qt 的父子对象参数。传了 parent，父子关系就建立起来，
                parent 销毁时它会被一起销毁。这里通常不传（None），
                因为卡片随后会被放进某个布局，布局会接管它的归属和显示。
        """
        super().__init__(parent)
        # setStyleSheet 用的是 Qt 样式表（QSS），语法和网页的 CSS 很像，
        # 但选择器要写 Qt 的类名。这段里：
        #   QFrame{...}  表示「作用于这个 QFrame 自身」
        #   background   背景色，border:none 去掉 QFrame 默认的边框
        #   border-radius:12px 把四个角磨圆
        # 外层那对花括号写成 {{ }} 是因为这是 f-string：单个花括号会被当成
        # 占位符，写两个才表示字面上的一对花括号。
        self.setStyleSheet(f"QFrame{{background:{CARD};border:none;border-radius:12px;}}")


class _WaterfallLayout(QLayout):
    """瀑布流布局：卡片按顺序放进「当前最矮的那一列」，窗口一变就重排。

    为什么不用 QVBoxLayout / QHBoxLayout：它们只能做一维排队，所以设置页
    原本是**写死**的左右两栏，每张卡片固定属于某一栏。麻烦在于各卡片高度
    差很多（「数据上传」带着订阅那块，比「系统」高一大截），矮的那一栏
    底下就空出一大块，怎么调都填不上。

    瀑布流不预设「谁在哪一栏」，而是边放边挑最矮的一列 —— 空隙自然被后面的
    卡片填掉。窗口拉宽/收窄也会重新挑列，不用手工调。

    实现时必须回答两个问题，下面两个方法就是干这个的：

        * 「这个宽度下要分几列」→ _columns()。列数由可用宽度算出来，
          窗口窄了自动退回一列，不写死；上限 max_cols 是因为卡片里成段的
          中文说明折成太窄的竖条会很难读。
        * 「这张卡片在这么宽的地方要占多高」→ _card_height()。卡片里放的是
          会折行的 QLabel，同一个卡片在 400px 和 700px 宽下高度完全不同，
          所以高度**不能缓存**，每次重排都得按当前列宽重新问一遍。

    所有卡片一视同仁，没有「通栏」这种特殊项 —— 全部一起流。
    """

    def __init__(self, parent=None, spacing=14, min_col_width=380, max_cols=2):
        """创建布局。

        参数：
            parent：父控件，布局会自动挂到它身上并接管其子控件的摆放。
            spacing：卡片之间的横竖间隙。
            min_col_width：每一列**最少**多宽。可用宽度放不下这么多列时
                就减少列数，少于两列就只剩一列。
            max_cols：最多分几列。
        """
        super().__init__(parent)
        self._spacing = spacing
        self._min_col_width = min_col_width
        self._max_cols = max_cols
        # 每一项是一个 QLayoutItem（其实就是对某个控件的包装），
        # 顺序就是卡片加入的顺序
        self._items = []
        # 这里**故意不调** setContentsMargins()：QLayout 在没人显式设过边距时
        # 会自动用系统样式的默认布局边距，和 QVBoxLayout 一模一样。一旦调了，
        # 就等于把它锁成固定值，换主题/换系统时就不跟着走了。

    # ── QLayout 规定必须实现的几个接口 ─────────────────────────
    # Qt 的布局引擎只会通过下面这几个方法来摆放控件，少实现一个就白排。
    # addWidget() 不用自己写：QLayout 自带的实现就是「建立父子关系 +
    # 造一个 QWidgetItem 调 addItem()」，够用。

    def addItem(self, item):
        """往布局里加一项（addWidget 最终也会走到这里）。"""
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):
        if 0 <= index < len(self._items):
            return self._items[index]
        return None

    def takeAt(self, index):
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self):
        """不主动撑大：卡片之间的空隙由「挑最矮的列」消化，不需要再横向拉伸。"""
        return Qt.Orientations(Qt.Orientation(0))

    def hasHeightForWidth(self):
        """必须如实上报：卡片高度随列宽变化。

        外层的 QScrollArea 就是靠这个标志决定「要不要按高度随宽度算」的 ——
        返回 False 的话它会拿一个固定的 sizeHint 当内容高度，窗口一窄
        排在最后的卡片就被裁掉了。
        """
        return True

    def heightForWidth(self, width):
        """干排一遍，只报「这么宽要这么高」，不动任何控件。"""
        return self._reflow(QRect(0, 0, width, 0), dry_run=True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._reflow(rect, dry_run=False)

    def sizeHint(self):
        # 宽度上没什么偏好，交给外层；高度由 heightForWidth 负责
        return self.minimumSize()

    def minimumSize(self):
        """最少要多大。取各卡片最小尺寸的并集，够 Qt 判断「能不能再缩」了。"""
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    # ── 真正干活的地方 ─────────────────────────────────────────

    def _columns(self, width):
        """按可用宽度算该分几列：每列不低于 min_col_width，最多 max_cols 列。

        算式里的 + spacing 是因为 n 列之间有 (n-1) 个间隙，反推时要补上，
        否则「刚好放得下两列」的宽度会被算成三列。
        """
        n = (width + self._spacing) // (self._min_col_width + self._spacing)
        return int(max(1, min(self._max_cols, n)))

    def _card_height(self, widget, width):
        """问卡片「给你这么宽，你要多高」。

        卡片里是会折行的 QLabel，它自己能算（heightForWidth）。万一某张卡片
        没实现这一套，就退回 sizeHint 的高度 —— 宁可排得松一点，也不能排矮了，
        排矮了卡片里的字会被直接裁掉。
        """
        if widget.hasHeightForWidth():
            h = widget.heightForWidth(width)
            if h > 0:
                return h
        return widget.sizeHint().height()

    def _reflow(self, rect, dry_run):
        """把卡片从左上角开始摆一遍，返回这批卡片一共占的高度。

        dry_run=True 时只算高度、不真的移动控件（heightForWidth 走这条）。
        """
        m = self.contentsMargins()
        # 扣掉布局自己的边距，剩下的才是能放卡片的地方
        area = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        if not self._items:
            return 0

        cols = self._columns(area.width())
        # 整除：余下的几个像素宁可不要，也不能让最后一列越界
        col_w = (area.width() - self._spacing * (cols - 1)) // cols
        heights = [0] * cols   # 每一列目前堆到多高（不含尚未产生的间隙）

        for item in self._items:
            widget = item.widget()
            if widget is None:
                continue

            height = self._card_height(widget, col_w)
            col = heights.index(min(heights))   # 挑最矮的那一列
            top = heights[col]
            left = area.left() + col * (col_w + self._spacing)

            if not dry_run:
                item.setGeometry(QRect(left, area.top() + top, col_w, height))

            heights[col] = top + height + self._spacing

        # 最后一张卡片下面不需要间隙，减掉；再加上下边距
        return max(heights) - self._spacing + m.top() + m.bottom()


class _BarChart(QWidget):
    """本周「每日使用时长」柱状图，一共 7 根柱子。

    这是一个完全自定义的控件：没有使用任何现成图表控件，而是继承 QWidget
    并重写 paintEvent，自己用 QPainter 把柱子画出来，所有坐标都在
    paintEvent 里现算。

    交互：鼠标移上去时对应柱子会被高亮，系统提示显示日期和具体时长。
    实现方式是 self._hovered 记住「当前悬停第几根」，数据一变就调用 update() 触发重绘。
    """

    def __init__(self, parent=None):
        """创建空的柱状图控件，随后用 set_data() 喂数据。"""
        super().__init__()
        # 设置最小高度，避免被布局压扁到看不见柱子
        self.setMinimumHeight(130)
        self._data = []      # 本周每天的数据，元素是 storage 返回的统计字典
        self._hovered = -1   # 当前鼠标悬停在几号柱子上，-1 表示没有悬停
        self._bar_rects = []  # 每根柱子的矩形区域和数值，供鼠标命中判断使用
        # 打开鼠标跟踪：默认情况下只有「按住鼠标键移动」才会收到
        # mouseMoveEvent；打开这个开关后，不按键、单纯移动鼠标也会收到事件，
        # 悬停高亮才能生效。
        self.setMouseTracking(True)

    def set_data(self, week_stats, today_str):
        """喂入数据并请求重绘。

        参数：
            week_stats：连续 7 天的统计列表，每天一个字典，至少包含
                "date"（ISO 格式字符串，如 "2026-09-22"）
                和 "total_seconds"（当天总使用秒数）。
            today_str：今天的 ISO 日期字符串，用来把「今天」那根柱子
                涂成主题色，让它一眼可辨。
        """
        self._data = week_stats
        self._today = today_str
        self._hovered = -1
        # 注意：这里并没有直接画图。改完数据只需要调用 update()，Qt 会在
        # 合适的时机自动回调 paintEvent —— 这是 Qt 绘图的基本节奏：
        # 「改数据 → update() → 等 Qt 叫你 → 在 paintEvent 里画」。
        self.update()

    def mouseMoveEvent(self, event):
        """鼠标在本控件上移动时由 Qt 自动调用：判断光标落在了哪根柱子上。

        事件处理函数不需要我们手动调用，Qt 会把鼠标事件分发过来。
        """
        # position() 返回相对本控件左上角的坐标（浮点数）。
        # 注意：Qt 的坐标原点是左上角，y 轴「向下」增长。
        pos = event.position()
        found = -1
        for i, (rx, ry, rw, rh, _) in enumerate(self._bar_rects):
            # 矩形命中测试：光标的 x、y 同时落在柱子的范围里就算命中
            if rx <= pos.x() <= rx + rw and ry <= pos.y() <= ry + rh:
                found = i
                break
        # 只有悬停目标真的变了才重绘；否则鼠标每移动一个像素都重画一次，
        # 会白白浪费性能。
        if found != self._hovered:
            self._hovered = found
            self.update()
        if found >= 0:
            day = self._data[found]
            QToolTip.showText(event.globalPosition().toPoint(),
                              f"{day['date']}\n应用使用 {format_duration_short(day['total_seconds'])}", self)
        else:
            QToolTip.hideText()

    def leaveEvent(self, event):
        """鼠标离开本控件时由 Qt 自动调用：清掉悬停状态，取消高亮。"""
        if self._hovered != -1:
            self._hovered = -1
            self.update()
        QToolTip.hideText()

    def paintEvent(self, event):
        """重绘控件。真正「画图」的代码全在这里。

        只要控件需要重新显示（第一次出现、窗口被拉伸、代码里调用了update() 等），Qt 就会调用这个函数。
        重写它是自定义绘图控件的标准做法。
        整体流程是：先把所有坐标算好，再按「刻度 → 柱子 → 悬停提示」的顺序依次画。
        """
        # 没有数据就什么都不画（也避免下面的 max() 在空序列上报错）
        if not self._data:
            return
        # QPainter 是「画笔」。创建时就绑定了绘制目标（self，也就是本控件）。
        p = QPainter(self)
        # 开启抗锯齿，圆角柱子的边缘才不会出现锯齿
        p.setRenderHint(QPainter.Antialiasing)

        w = self.width()
        h = self.height()
        # 找出最大值，把它当作「满格柱子的高度」的参考。
        # default=1 防止列表为空时报错；末尾的 `or 1` 防止最大值为 0
        # （一整天都没使用）时下面出现除零错误。
        max_val = max((d["total_seconds"] for d in self._data), default=1) or 1

        # 左侧留 55px 给时间刻度
        margin_left = 55
        n = len(self._data)
        gap = 20
        avail = w - margin_left - 20            # 扣掉左右留白后可用于画柱子的宽度
        bar_w = max(14, (avail - gap * (n - 1)) // n)  # 单根柱子宽度（下限 14px）
        total_w = bar_w * n + gap * (n - 1)     # 所有柱子加间隔的总宽度
        sx = margin_left + (avail - total_w) // 2  # 起始 x：让整组柱子水平居中

        # Qt 的 y 轴是「向下」增长的，原点在左上角。所以画柱子的关键是：
        #   chart_b —— 图表的底边，同时也是数值为 0 的那条基准线；
        #   chart_t —— 图表的顶边；
        #   chart_h —— 可用的纵向高度。
        # 后面凡是算「柱子的顶端」，都写成 chart_b - bar_h，含义是
        # 「从底边往上量出高度」，因为往上走 y 是变小的。
        chart_b = h - 20   # 底部留 20px 给日期文字
        chart_t = 8
        chart_h = chart_b - chart_t

        # 每次重画都重新记录柱子矩形，保证记录的是当前尺寸下的坐标
        self._bar_rects = []

        # 左侧时间刻度（按 1 小时分档）
        p.setFont(_font(9))
        p.setPen(QColor(GRAY))
        max_hours = max(1, int((max_val + 3599) // 3600))  # 向上取整到小时
        for h in range(0, max_hours + 1):
            t = h * 3600   # 这条刻度代表多少秒（h 小时）
            # 把「秒」按比例换算成「距底边多少像素」，再从底边往上减出 y 坐标。
            # 因为 y 轴向下，所以是减法：值越大，y 越小（越靠上）。
            ty = chart_b - int(t / max_val * chart_h) if max_val > 0 else chart_b
            label = f"{h}h"
            p.drawText(0, ty - 7, margin_left - 6, 14,
                       Qt.AlignRight | Qt.AlignVCenter, label)
            # 刻度线
            p.setPen(QColor(BORDER))
            p.drawLine(margin_left - 2, ty, margin_left, ty)
            p.setPen(QColor(GRAY))

        # 逐天把柱子画出来
        for i, day in enumerate(self._data):
            x = sx + i * (bar_w + gap)   # 第 i 根柱子的左边界
            val = day["total_seconds"]
            d = date.fromisoformat(day["date"])   # "2026-09-22" → date 对象
            day_name = DAY_LABELS[d.weekday()]    # weekday()：周一 = 0，周日 = 6
            is_today = day["date"] == self._today

            # 按「数值占最大值的比例」算柱子高度；数值为 0 就高度记 0（不画）
            if val > 0:
                bar_h = max(4, int(val / max_val * chart_h))  # 至少 4px，否则看不见
            else:
                bar_h = 0
            # 柱子是「从底边往上长」的：bar_h 是高度，它的顶端 y 坐标是
            # 底边 y 减去高度。高度为 0 时顶端就是底边本身。
            y_top = chart_b - bar_h if bar_h > 0 else chart_b

            # 记下这根柱子的矩形区域和数值，供 mouseMoveEvent 做悬停判断
            self._bar_rects.append((x, y_top, bar_w, bar_h, val))

            # 柱子：今天用主题色，其它天用浅灰
            color = QColor(ACCENT if is_today else CHART_BG)
            if val > 0:
                p.setBrush(color)    # brush 是「填充」用的颜色/图案
                p.setPen(Qt.NoPen)   # pen 是「描边」用的，NoPen 表示不描边
                p.drawRoundedRect(x, y_top, bar_w, bar_h, 4, 4)

            # 悬停高亮：在柱子外面再套一圈很淡的半透明主题色光晕
            if i == self._hovered and val > 0:
                hl = QColor(ACCENT)
                hl.setAlpha(40)      # alpha 是不透明度，范围 0~255，40 很淡
                p.setBrush(hl)
                p.setPen(Qt.NoPen)
                p.drawRoundedRect(x - 2, y_top - 2, bar_w + 4, bar_h + 4, 6, 6)
                # 光晕画完后，把柱子本身重画一遍盖在光晕上（否则会被盖住）
                p.setBrush(color)
                p.drawRoundedRect(x, y_top, bar_w, bar_h, 4, 4)

            # 柱底下的星期几。今天那根用主题色 + 加粗，起「当前」的提示作用
            p.setFont(_font(9, is_today))
            p.setPen(QColor(ACCENT if is_today else GRAY))
            p.drawText(x, chart_b + 2, bar_w, 16,
                       Qt.AlignCenter, day_name)

        # 结束绘制。QPainter 用完要 end() 显式收尾，否则 Qt 会提示
        # 「painter 未正确结束」的警告。
        p.end()


class _HourlyChart(QWidget):
    """24 小时柱状图，每根柱子代表 1 小时。

    和 _BarChart 一样是手绘控件，区别是柱子固定 24 根、更细，
    并且当前所在的那一小时用主题色高亮（见下面的 is_now 判断）。
    用在「今日」标签页里。
    """

    def __init__(self, fixed_labels=False, parent=None):
        """创建控件。

        参数：
            fixed_labels：控制左侧刻度的取值方式。
                True —— 刻度固定写成 0 / 30m / 60m。今日页每小时最多 3600 秒，
                        用固定刻度更好读，所以实参传的是 fixed_labels=True。
                False —— 按实际最大值自动分成 0、25%、50%、75%、100% 五档。
        """
        super().__init__()
        self.setMinimumHeight(120)
        self._hourly = [0.0] * 24   # 24 个小时各自的秒数，下标就是小时数
        self._hovered = -1
        self._bar_rects = []        # 每根柱子的矩形区域（本图不需要数值）
        self._fixed_labels = fixed_labels
        self.setMouseTracking(True)  # 同上：不按键也能收到鼠标移动事件

    def set_data(self, hourly):
        """喂入 24 个元素的列表（每个元素是该小时的秒数）并请求重绘。"""
        self._hourly = hourly
        self._hovered = -1
        self.update()

    def mouseMoveEvent(self, event):
        """鼠标移动时更新悬停的柱子下标（逻辑与 _BarChart 相同）。"""
        pos = event.position()
        found = -1
        for i, (rx, ry, rw, rh) in enumerate(self._bar_rects):
            if rx <= pos.x() <= rx + rw and ry <= pos.y() <= ry + rh:
                found = i
                break
        if found != self._hovered:
            self._hovered = found
            self.update()
        if found >= 0:
            QToolTip.showText(event.globalPosition().toPoint(),
                              f"时段：{found:02d}:00–{found + 1:02d}:00\n"
                              f"应用用时：{format_duration_short(self._hourly[found])}", self)
        else:
            QToolTip.hideText()

    def leaveEvent(self, event):
        """鼠标离开控件时清除悬停高亮。"""
        if self._hovered != -1:
            self._hovered = -1
            self.update()
        QToolTip.hideText()

    def paintEvent(self, event):
        """重绘 24 根小时柱。坐标算法与 _BarChart 一致，可对照阅读。"""
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        w = self.width()
        h = self.height()
        # 最大值为 0（当天完全没用电脑）时用 3600 秒兜底，避免下面除零
        max_val = max(self._hourly) if max(self._hourly) > 0 else 3600

        margin_left = 40
        margin_bottom = 20   # 底部留给小时数字
        margin_top = 6
        gap = 2              # 柱子之间的缝隙，比 _BarChart 窄
        avail = w - margin_left - 10
        n = 24
        bar_w = max(4, (avail - gap * (n - 1)) // n)
        total_w = bar_w * n + gap * (n - 1)
        sx = margin_left + (avail - total_w) // 2   # 整组柱子水平居中

        # 同样：y 轴向下增长，chart_b 是底边（数值 0 的基准线）
        chart_b = h - margin_bottom
        chart_h = chart_b - margin_top

        self._bar_rects = []

        # 左侧刻度。ticks 里每项是「(占最大值的比例, 要显示的文字)」，
        # 比例 0 在底边、1 在顶边，中间值按比例插。
        p.setFont(_font(8))
        p.setPen(QColor(GRAY))
        if self._fixed_labels:
            ticks = [(0, "0"), (0.5, "30m"), (1, "60m")]
        else:
            ticks = [(0, "0"), (0.25, format_duration_short(max_val * 0.25)),
                     (0.5, format_duration_short(max_val * 0.5)),
                     (0.75, format_duration_short(max_val * 0.75)),
                     (1, format_duration_short(max_val))]
        for frac, label in ticks:
            # 比例换算成距底边的像素高度，再用减法从底边往上量出 y 坐标
            ty = chart_b - int(frac * chart_h)
            p.drawText(0, ty - 6, margin_left - 4, 12,
                       Qt.AlignRight | Qt.AlignVCenter, label)

        for i in range(24):
            x = sx + i * (bar_w + gap)
            val = self._hourly[i]
            pct = val / max_val if max_val > 0 else 0
            bar_h = max(0, int(pct * chart_h))
            # 顶端 y = 底边 y - 高度（y 轴向下，所以要减）
            y_top = chart_b - bar_h

            self._bar_rects.append((x, y_top, bar_w, bar_h))

            # 柱子：当前所在小时用主题色，其余小时用浅灰
            is_now = i == beijing_now().hour
            if val > 0:
                color = QColor(ACCENT if is_now else CHART_BG)
                p.setBrush(color)
                p.setPen(Qt.NoPen)
                p.drawRoundedRect(x, y_top, bar_w, bar_h, 2, 2)

            # 悬停高亮：外面套一圈淡主题色，再把柱子重画一遍盖上去
            if i == self._hovered and val > 0:
                hl = QColor(ACCENT)
                hl.setAlpha(40)
                p.setBrush(hl)
                p.setPen(Qt.NoPen)
                p.drawRoundedRect(x - 1, y_top - 1, bar_w + 2, bar_h + 2, 3, 3)
                color = QColor(ACCENT if is_now else CHART_BG)
                p.setBrush(color)
                p.drawRoundedRect(x, y_top, bar_w, bar_h, 2, 2)

            # 整点标签：每 3 小时才标一个数字，24 个全标就挤成一团了
            if i % 3 == 0:
                p.setFont(_font(7))
                p.setPen(QColor(GRAY if not is_now else ACCENT))
                p.drawText(x, chart_b + 2, bar_w, 14,
                           Qt.AlignCenter, f"{i}")

        p.end()


class _TimelineView(QWidget):
    """时间线视图：一个应用占一行，横向是当天 0~24 点，用色块标出使用时段。

    和 _BarChart / _HourlyChart 的差别：它们按固定刻度画柱子，这里则是把
    每个应用在一天中的「使用区间」按时间比例画成一条条横条，类似甘特图。

    为什么按应用分行、而不是按片段分行：
        一天可能有几千条片段，逐条画会画出上千行；按应用分行后行数等于
        应用数量（通常二三十个），既紧凑又能一眼看出「几点在用什么软件」。
    """

    # 布局常量：左侧应用名宽度 / 右侧边距 / 顶部刻度高度 / 每行高度
    _MARGIN_LEFT = 110
    _MARGIN_RIGHT = 10
    _HEADER_H = 18
    _LANE_H = 24
    _MIN_SPAN = 600.0     # 放大上限：时间窗口最少显示 10 分钟
    _MAX_SPAN = 86400.0   # 缩小上限：最多显示一整天

    def __init__(self, parent=None, label_key="app_name", empty_text="暂无时间线数据"):
        super().__init__(parent)
        self._label_key = label_key
        self._empty_text = empty_text
        if label_key == "host":
            self._MARGIN_LEFT = 155
            self._LANE_H = 27
        self._lanes = []   # 预处理好的「每个应用一行」数据，见 set_data
        # 当前可见的时间窗口（当天 0 点起算的秒数），默认一整天
        self._window_start = 0.0
        self._window_end = 86400.0
        # 左右拖拽平移的状态（见 mousePressEvent / mouseMoveEvent）
        self._panning = False
        self._pan_start_x = 0.0
        self._pan_start_window = 0.0
        self.setMouseTracking(True)

    def set_data(self, fragments, reset_view=True):
        """喂入片段列表，预处理成按应用分行的数据，再按内容调整高度并重绘。

        参数：
            fragments：某天的片段列表（由 storage.get_fragments_for_date 提供），
                每项至少含 "app_name"、"start_time"、"end_time"（ISO 字符串）。
        """
        # 换数据（比如翻到另一天）时，把缩放重置回「一整天」
        if reset_view:
            self._window_start = 0.0
            self._window_end = self._MAX_SPAN
        self._update_cursor()
        self._lanes = self._build_lanes(fragments or [])
        # 控件高度随应用数量增长，外层用 QScrollArea 包着，超高时能滚动。
        # 至少留 200px，空数据时也能正常显示提示文字。
        self.setMinimumHeight(max(120, self._HEADER_H + len(self._lanes) * self._LANE_H + 10))
        self.update()

    def _build_lanes(self, fragments):
        """把片段列表整理成「每个应用一行」的结构。

        返回列表，按总使用时长从多到少排序，每项：
            {"app_name", "display_name", "intervals": [(起秒, 止秒), ...]}
        其中起止秒是「当天 0 点起算的秒数」，方便直接按比例映射到像素。
        """
        per_app = {}   # app_name -> 该应用当天的所有 (起秒, 止秒) 区间
        for f in fragments:
            try:
                s = datetime.fromisoformat(f["start_time"])
                e = datetime.fromisoformat(f["end_time"])
            except Exception:
                continue   # 时间格式坏掉的片段跳过
            day0 = s.replace(hour=0, minute=0, second=0, microsecond=0)
            s_sec = (s - day0).total_seconds()
            e_sec = (e - day0).total_seconds()
            e_sec = min(e_sec, 86400.0)   # 跨到次日的片段截断到当天末尾
            if e_sec <= s_sec:
                continue
            name = f.get(self._label_key)
            if name:
                per_app.setdefault(name, []).append((s_sec, e_sec))

        lanes = []
        for app, intervals in per_app.items():
            intervals.sort()
            # 把相邻或重叠的区间合并成一条，避免同一应用在时间线上出现碎条。
            merged = []
            for s, e in intervals:
                if merged and s <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(merged[-1][1], e))
                else:
                    merged.append([s, e])
            total = sum(e - s for s, e in merged)
            lanes.append({
                "app_name": app,
                "display_name": get_display_name(app) if self._label_key == "app_name" else app,
                "intervals": merged,
                "total": total,
            })
        # 用得最多的应用排最上面，和排行榜一致
        lanes.sort(key=lambda x: -x["total"])
        return lanes

    def paintEvent(self, event):
        """重绘时间线；没有数据时在正中间显示一句「暂无时间线数据」。"""
        if not self._lanes:
            p = QPainter(self)
            p.setRenderHint(QPainter.Antialiasing)
            p.setPen(QColor(MUTED))
            p.setFont(_font(12))
            p.drawText(self.rect(), Qt.AlignCenter, self._empty_text)
            p.end()
            return

        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)

        w = self.width()
        h = self.height()
        margin_left = self._MARGIN_LEFT
        margin_right = self._MARGIN_RIGHT
        header_h = self._HEADER_H
        lane_h = self._LANE_H
        plot_w = max(10, w - margin_left - margin_right)  # 时间轴可用宽度
        win_start = self._window_start
        win_span = self._window_end - self._window_start   # 当前可见时间跨度（秒）

        # 顶部刻度：按当前窗口跨度自适应选一个「整」刻度间隔，避免太密或太疏
        step = self._nice_step(win_span)
        p.setFont(_font(8))
        p.setPen(QColor(GRAY))
        t = int(win_start // step) * step   # 对齐到刻度间隔的整数倍
        while t <= self._window_end:
            x = margin_left + int((t - win_start) / win_span * plot_w)
            p.drawText(x - 22, 0, 44, header_h, Qt.AlignCenter, self._format_tick(t))
            # 淡灰色竖线做参考网格
            p.setPen(QColor(BORDER))
            p.drawLine(x, header_h - 2, x, h)
            p.setPen(QColor(GRAY))
            t += step

        y = header_h
        for i, lane in enumerate(self._lanes):
            if self._label_key == "host" and i % 2 == 0:
                stripe = QColor(BORDER)
                stripe.setAlpha(30)
                p.fillRect(0, y, w, lane_h, stripe)
            # 左侧应用名（超长会被矩形自动裁剪，不会越界）
            p.setPen(QColor(TEXT))
            p.setFont(_font(9))
            name = p.fontMetrics().elidedText(lane["display_name"], Qt.ElideRight, margin_left - 14)
            p.drawText(4, y, margin_left - 10, lane_h,
                       Qt.AlignLeft | Qt.AlignVCenter, name)

            color = QColor(APP_COLORS[i % len(APP_COLORS)])
            if self._label_key == "host":
                color = QColor(("#83B5E5", "#97CFB5", "#F1BC8E", "#C4AFE5",
                                "#E5A4B6", "#8BC9D1", "#D7C787")[i % 7])
            p.setPen(Qt.NoPen)
            p.save()
            p.setClipRect(margin_left, header_h, plot_w, max(0, h - header_h))
            for s_sec, e_sec in lane["intervals"]:
                # 绘制与悬浮命中共用相同矩形，短至 1 秒的色块也能命中。
                rect = self._interval_rect(s_sec, e_sec, i)
                if self._label_key == "host":
                    gradient = QLinearGradient(rect.topLeft(), rect.bottomLeft())
                    gradient.setColorAt(0, color.lighter(120))
                    gradient.setColorAt(1, color)
                    p.setBrush(gradient)
                else:
                    p.setBrush(color)
                p.drawRoundedRect(rect, 3, 3)
            p.restore()

            y += lane_h   # 换下一行

        p.end()

    def _interval_rect(self, start, end, lane_index):
        """Pixel bounds of an interval in the current zoom/pan window."""
        plot_w = max(10, self.width() - self._MARGIN_LEFT - self._MARGIN_RIGHT)
        span = self._window_end - self._window_start
        x = self._MARGIN_LEFT + int((start - self._window_start) / span * plot_w)
        width = max(2, int((end - start) / span * plot_w))
        y = self._HEADER_H + lane_index * self._LANE_H + 3
        return QRect(x, y, width, self._LANE_H - 6)

    def _nice_step(self, span):
        """按时间窗口跨度选一个「整」刻度间隔（秒），让刻度数量在 4~8 个左右。"""
        for step in (60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200):
            if span / step <= 8:
                return step
        return 43200

    def _format_tick(self, seconds_of_day):
        """把「当天秒数」格式化成刻度文字：整点显示「N时」，否则显示「HH:MM」。"""
        hh = int(seconds_of_day) // 3600
        mm = int(seconds_of_day) % 3600 // 60
        if mm == 0:
            return f"{hh}时"
        return f"{hh:02d}:{mm:02d}"

    def wheelEvent(self, event):
        """按住 Ctrl 滚动滚轮 → 放大/缩小时间轴；不按 Ctrl 则照常滚动。"""
        if not (event.modifiers() & Qt.ControlModifier):
            # 没按 Ctrl：交给父级（滚动区）正常上下滚动
            super().wheelEvent(event)
            return
        delta = event.angleDelta().y()
        if delta == 0:
            return

        # 以鼠标所在位置为锚点：缩放前后，光标指着的那个时刻尽量保持不动
        margin_left = self._MARGIN_LEFT
        margin_right = self._MARGIN_RIGHT
        plot_w = max(10, self.width() - margin_left - margin_right)
        frac = (event.position().x() - margin_left) / plot_w
        frac = max(0.0, min(1.0, frac))
        anchor_sec = self._window_start + frac * (self._window_end - self._window_start)

        span = self._window_end - self._window_start
        if delta > 0:      # 向上滚 = 放大（时间窗口变小）
            new_span = span / 1.25
        else:              # 向下滚 = 缩小（时间窗口变大）
            new_span = span * 1.25
        new_span = max(self._MIN_SPAN, min(self._MAX_SPAN, new_span))

        # 反推新的窗口起点，并夹在 [0, 24h - 窗口] 之间，避免窗口越出一天
        new_start = anchor_sec - frac * new_span
        new_start = max(0.0, min(self._MAX_SPAN - new_span, new_start))
        self._window_start = new_start
        self._window_end = new_start + new_span
        self._update_cursor()
        self.update()
        event.accept()

    def _update_cursor(self):
        """缩放后才能左右平移，用鼠标手势提示：能平移显示小手，否则普通箭头。"""
        zoomed = (self._window_end - self._window_start) < self._MAX_SPAN - 1
        self.setCursor(Qt.OpenHandCursor if zoomed else Qt.ArrowCursor)

    def mouseDoubleClickEvent(self, event):
        """双击 → 把缩放复位回一整天。"""
        self._window_start = 0.0
        self._window_end = self._MAX_SPAN
        self._update_cursor()
        self.update()
        event.accept()

    def mousePressEvent(self, event):
        """按下左键 → 开始左右平移（只在缩放后有意义）。"""
        if event.button() == Qt.LeftButton:
            QToolTip.hideText()
            self._panning = True
            self._pan_start_x = event.position().x()
            self._pan_start_window = self._window_start
            self.setCursor(Qt.ClosedHandCursor)
            event.accept()   # 吃掉事件，避免触发外层窗口的拖动

    def mouseMoveEvent(self, event):
        """按住左键拖动 → 按鼠标位移平移时间窗口。"""
        if self._panning:
            margin_left = self._MARGIN_LEFT
            margin_right = self._MARGIN_RIGHT
            plot_w = max(10, self.width() - margin_left - margin_right)
            span = self._window_end - self._window_start
            dx = event.position().x() - self._pan_start_x
            # 向右拖 = 看更早的时间（窗口整体左移），与地图拖拽的方向一致
            delta_sec = dx / plot_w * span
            new_start = self._pan_start_window - delta_sec
            new_start = max(0.0, min(self._MAX_SPAN - span, new_start))
            self._window_start = new_start
            self._window_end = new_start + span
            self.update()
            event.accept()
            return
        detail = self._hover_detail(event.position().x(), event.position().y())
        if detail:
            QToolTip.showText(event.globalPosition().toPoint(), detail, self)
        else:
            QToolTip.hideText()

    def _hover_detail(self, x, y):
        """Return the actual interval under the cursor, also after pan/zoom."""
        lane_index = int((y - self._HEADER_H) // self._LANE_H)
        if (x < self._MARGIN_LEFT or x > self.width() - self._MARGIN_RIGHT
                or y < self._HEADER_H or lane_index < 0 or lane_index >= len(self._lanes)):
            return None
        lane = self._lanes[lane_index]
        for start, end in lane["intervals"]:
            rect = self._interval_rect(start, end, lane_index)
            if rect.left() <= x < rect.left() + rect.width() and rect.top() <= y < rect.top() + rect.height():
                def clock(value):
                    value = int(value)
                    return f"{value // 3600:02d}:{value % 3600 // 60:02d}:{value % 60:02d}"
                kind = "网站" if self._label_key == "host" else "应用"
                return (f"{kind}：{lane['display_name']}\n"
                        f"时段：{clock(start)}–{clock(end)}\n"
                        f"用时：{format_duration_short(end - start)}")
        return None

    def leaveEvent(self, event):
        QToolTip.hideText()
        super().leaveEvent(event)

    def mouseReleaseEvent(self, event):
        """松开左键 → 结束平移。"""
        if self._panning:
            self._panning = False
            self._update_cursor()
            event.accept()


class _FirstRunWizard(QDialog):
    """首次运行引导对话框。

    只在程序第一次启动时弹出（是否首次运行由 main.py 调 storage.is_first_run()判断），
    让用户一次性把初始设置定好：无操作判定时间、是否开机自启、是否记录窗口标题。用户点「开始使用」后写入设置并关闭窗口。

    它比 Dashboard 简单得多：一个 QDialog（独立小窗口）+ 垂直布局，不需要无边框、没有标签页。
    """

    def __init__(self, parent=None):
        """创建引导窗口。参数同 _Card：parent 为 Qt 父子对象，通常不传。"""
        super().__init__(parent)
        self.setWindowTitle(APP_TITLE)
        self.setMinimumWidth(420)
        # 整窗统一设样式表，一次性覆盖所有同类控件：
        #   QDialog{...}         窗口背景
        #   QLabel{...}          所有标签的文字颜色和字号
        #   QComboBox{...}       下拉框
        #   QSlider::handle      滑块上那个可以拖的圆钮（子控件选择器）
        #   QSlider::groove      滑块的滑轨
        # 「::子控件名」是 Qt 样式表特有的写法，用 CSS 的思维去套就行。
        self.setStyleSheet(
            f"QDialog{{background:{BG};}}"
            f"QLabel{{color:{TEXT};font-size:13px;}}"
            f"QComboBox{{background:{CARD};color:{TEXT};border:1px solid {BORDER};"
            f"border-radius:8px;padding:6px 10px;font-size:13px;}}"
            f"QSlider::handle:horizontal{{background:{ACCENT};width:16px;height:16px;"
            f"border-radius:8px;margin:-5px 0;}}"
            f"QSlider::groove:horizontal{{background:{BORDER};height:4px;border-radius:2px;}}"
        )
        # 把「搭界面」和「设样式」分开，函数短一点好读
        self._build_ui()

    def _build_ui(self):
        """从上到下搭建引导页的界面（纯 UI 代码，不含业务逻辑）。"""
        # 垂直布局：子控件按加入顺序自上而下排列。
        # 参数含义：setSpacing 是控件之间的间距，
        # setContentsMargins 是布局四周的内边距，顺序是 左、上、右、下。
        lay = QVBoxLayout(self)
        lay.setSpacing(16)
        lay.setContentsMargins(28, 24, 28, 24)

        title = QLabel(APP_TITLE)
        title.setFont(_font(22, True))
        title.setStyleSheet(f"color:{TEXT};")
        title.setAlignment(Qt.AlignCenter)
        lay.addWidget(title)

        # 字符串里写 \n 是硬换行，Qlabel 会照原样分成两行显示
        desc = QLabel(
            "只统计应用使用时间\n"
            "不会记录键盘输入 | 不会截图"
        )
        desc.setFont(_font(12))
        desc.setStyleSheet(f"color:{GRAY};")
        desc.setAlignment(Qt.AlignCenter)
        lay.addWidget(desc)

        # addSpacing 插入一段固定高度的空白（区别于 addStretch 那种可伸缩的弹簧）
        lay.addSpacing(8)

        idle_lbl = QLabel("无操作判定时间：")
        idle_lbl.setFont(_font(13))
        lay.addWidget(idle_lbl)

        # 下拉选择框。下面按 value 取秒数时用下标换算：
        # 0→180 秒、1→300 秒、2→600 秒（见 _on_start 里的 idle_map）。
        self.idle_combo = QComboBox()
        self.idle_combo.addItems(["3 分钟", "5 分钟", "10 分钟"])
        self.idle_combo.setCurrentIndex(1)   # 默认选中「5 分钟」
        lay.addWidget(self.idle_combo)

        self.chk_autostart = QCheckBox("开机自动启动")
        self.chk_autostart.setStyleSheet(f"color:{TEXT};font-size:13px;spacing:8px;")
        lay.addWidget(self.chk_autostart)

        self.chk_save_title = QCheckBox("保存窗口标题（更详细的使用记录）")
        self.chk_save_title.setStyleSheet(f"color:{TEXT};font-size:13px;spacing:8px;")
        lay.addWidget(self.chk_save_title)

        # 上传开关。必须在用户第一次打开程序时就问过 —— 这个功能默认是开的，
        # 如果默认开着却不告诉用户，等于是偷偷把数据发出去了。宁可这里多一行字。
        self.chk_upload = QCheckBox("每天把使用记录上传到服务器")
        self.chk_upload.setStyleSheet(f"color:{TEXT};font-size:13px;spacing:8px;")
        self.chk_upload.setChecked(True)   # 默认开启，但用户看得到、能立刻关掉
        lay.addWidget(self.chk_upload)

        upload_hint = QLabel(
            f"基础上传内容是「应用名 + 使用时长」，发往 {config.base_url()}。\n"
            "网站域名和应用时间分布需在设置中单独启用；不上传窗口标题或键盘输入。")
        upload_hint.setFont(_font(11))
        upload_hint.setStyleSheet(f"color:{GRAY};")
        upload_hint.setWordWrap(True)
        lay.addWidget(upload_hint)

        lay.addSpacing(8)

        # 主按钮
        btn = QPushButton("开始使用")
        btn.setFixedHeight(40)
        btn.setStyleSheet(
            f"QPushButton{{background:{ACCENT};color:white;border:none;"
            f"border-radius:10px;font-size:15px;font-weight:bold;}}"
            f"QPushButton:hover{{background:{BLUE_LT};}}"
        )
        # 信号槽：Qt 控件对外「发信号」，我们把自己的函数「连」上去，
        # 事件发生时 Qt 就会调用它。这里 click（点击）→ self._on_start。
        # 注意 connect 里写的是函数名本身（不带括号），加括号会立刻执行。
        btn.clicked.connect(self._on_start)
        lay.addWidget(btn)

    def _on_start(self):
        """点「开始使用」后调用：把界面上的选择写入设置，然后关闭窗口。"""
        # 下拉框显示的是「3/5/10 分钟」，这里把下标映射成真正的秒数。
        # get 的第二个参数 300 是兜底值：万一取到意料之外的下标也不会崩。
        idle_map = {0: 180, 1: 300, 2: 600}
        idle_val = idle_map.get(self.idle_combo.currentIndex(), 300)
        # 设置项统一以字符串形式存进 storage（"1"/"0" 代表真/假）
        set_setting("idle_threshold", str(idle_val))
        set_setting("autostart", "1" if self.chk_autostart.isChecked() else "0")
        set_setting("save_window_title", "1" if self.chk_save_title.isChecked() else "0")
        # 上传开关也在这里定下来。用户没勾，就从第一天起不联网 —— 而不是
        # 「先传着，等他想起来去设置里关」。
        set_setting("upload_enabled", "1" if self.chk_upload.isChecked() else "0")
        # 打上「已引导过」的标记，下次启动就不会再弹这个窗口了
        set_setting("first_run_done", "1")

        if self.chk_autostart.isChecked():
            # 延迟导入：autostart 要动 Windows 注册表，成功与否都不该影响
            # 本窗口的关闭流程，所以放进 try/except 里静默失败。
            try:
                from system.autostart import enable_autostart
                enable_autostart()
            except Exception:
                pass

        # accept() 是 QDialog 的方法：关闭对话框并返回「确定」结果。
        # 外面 main.py 用 wizard.exec() 阻塞等待，此时就会收到这个结果继续往下走。
        self.accept()


class Dashboard(QWidget):
    """主窗口：一个无边框、圆角、带三个标签页的仪表盘。

    它同时扮演「视图」和「控制器」两个角色：
        视图 —— 负责组装所有控件（见 _build_ui 与 _build_settings_page）；
        控制器 —— 响应按钮点击、切换标签页、读数据并刷新界面。

    三个标签页共用同一批「统计控件」（顶部的总时长卡、图表卡、应用列表），
    通过 hide()/setVisible() 决定显示哪一个（见 _switch_tab），
    而不是为每个标签页各建一套控件。
    """

    def __init__(self, monitor=None):
        """创建主窗口。

        参数：
            monitor：后台采集器对象（monitor.py 里的 Monitor），暂停/继续
                统计按钮会直接操作它。允许传 None —— 单独调试界面时不传也能
                正常显示，只是暂停按钮点了没反应（见 _toggle_pause）。
        """
        super().__init__()
        # 无边框窗口：去掉操作系统自带的标题栏，这样才能做出圆角的整体外观。
        # 代价是「拖动窗口」和「最小化/关闭按钮」都要自己实现，
        # 前者见本类的 mousePressEvent / mouseMoveEvent / mouseReleaseEvent。
        # 竖线 | 表示把两个标志位「或」在一起；Qt.Window 说明它是独立的顶层窗口。
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window)
        # 让窗口自身的背景透明。不加这句，圆角之外的那四个角会露出一个不透明的方形底色，圆角就白做了。
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.monitor = monitor
        self._current_tab = "daily"     # 当前标签页：daily / weekly / settings
        self._icon_cache = {}           # 预留的图标缓存字段（目前未使用）
        self._view_date = beijing_today()  # 当前查看的日期，可左右翻页
        self._drag_pos = None           # 拖窗口时记录「鼠标 - 窗口左上角」的偏移

        # 后台上传/探活用的执行器（见 ui/bgworker.py）。必须在这里建好并连上
        # 信号，因为 _build_ui() → _build_settings_page() 里会用到它。
        self._worker = BgWorker()
        self._worker.done.connect(self._on_bg_done)

        self._build_ui()

    def _build_ui(self):
        """一次性搭好主窗口的界面结构。

        控件的层级关系大致是：
            外层布局 outer（四周留 12px，露出圆角效果）
              └─ content：白色圆角 QFrame
                   ├─ 自定义标题栏（标题 + 时钟 + 最小化/关闭按钮）
                   ├─ 标签页切换按钮（今日 / 本周 / 设置）
                   ├─ stats_container 统计区（今日与本周共用，按需显示图表）
                   └─ settings_scroll 设置页（见 _build_settings_page，外面包了滚动区）

        所有控件都挂到 self 上，是为了后面能在别的函数里通过 self.xxx 访问到。
        """
        self.setWindowTitle(APP_TITLE)
        self.setMinimumSize(920, 600)
        self.resize(1160, 700)   # 横屏：左右两栏（左图表、右排行榜），用户之后可自由缩放

        # 外层容器（圆角 + 阴影效果通过样式实现）
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)   # 这个 12px 就是为了露出圆角
        outer.setSpacing(0)

        # 内容容器：真正放东西的白色圆角块
        content = QFrame()
        content.setStyleSheet(
            f"QFrame{{background:{BG};border-radius:16px;}}"
        )
        content_lay = QVBoxLayout(content)
        content_lay.setContentsMargins(20, 0, 20, 16)
        content_lay.setSpacing(10)

        # ── 自定义标题栏 ──
        # 因为窗口是无边框的，系统标题栏不存在，所以标题文字和最小化/关闭按钮都得自己用布局拼出来。
        title_bar = QHBoxLayout()
        title_bar.setContentsMargins(0, 8, 0, 4)
        title_bar.setSpacing(8)

        lbl_title = QLabel(APP_TITLE)
        lbl_title.setFont(_font(13, True))
        lbl_title.setStyleSheet(f"color:{TEXT};")
        title_bar.addWidget(lbl_title)

        # 实时时钟：QLabel 本身只会显示静态文字，靠下面的定时器每秒更新
        self.lbl_clock = QLabel()
        self.lbl_clock.setFont(_font(11))
        self.lbl_clock.setStyleSheet(f"color:{GRAY};")
        self.lbl_clock.setAlignment(Qt.AlignCenter)
        title_bar.addWidget(self.lbl_clock)
        self._update_clock()   # 先立刻显示一次，否则要等 1 秒才有文字
        self._clock_timer = QTimer(self)
        # QTimer 每隔一段时间发出一次 timeout 信号，把它连到 _update_clock，
        # 就实现了「时钟」效果（Qt 里没有现成的时钟控件）。
        self._clock_timer.timeout.connect(self._update_clock)
        self._clock_timer.start(1000)   # 参数单位是毫秒，1000 = 每秒一次

        # addStretch() 加一个可伸缩的「弹簧」，把左右两组控件撑开到两端
        title_bar.addStretch()

        # 最小化按钮。因为窗口无边框，系统的按钮没有了，只能自己画一个。
        # showMinimized 是 QWidget 自带的方法，签名正好符合槽函数的要求（无参数、无返回值），所以可以直接把方法名连上去。
        self.btn_min = QPushButton("─")
        self.btn_min.setFixedSize(28, 28)
        self.btn_min.setStyleSheet(
            f"QPushButton{{background:transparent;color:{GRAY};border:none;"
            f"border-radius:14px;font-size:14px;}}"
            f"QPushButton:hover{{background:{HOVER};color:{TEXT};}}"
        )
        self.btn_min.clicked.connect(self.showMinimized)

        # 关闭按钮。注意连的是 hide 而不是 close：本程序要常驻托盘继续统计，
        # 关闭窗口只是把它藏起来，不能真的退出程序（托盘菜单里才有「退出」）。
        self.btn_close = QPushButton("✕")
        self.btn_close.setFixedSize(28, 28)
        self.btn_close.setStyleSheet(
            f"QPushButton{{background:transparent;color:{GRAY};border:none;"
            f"border-radius:14px;font-size:13px;}}"
            f"QPushButton:hover{{background:{RED};color:white;}}"
        )
        self.btn_close.clicked.connect(self.hide)

        title_bar.addWidget(self.btn_min)
        title_bar.addWidget(self.btn_close)
        content_lay.addLayout(title_bar)

        # ── 标签页切换按钮（今日 / 本周 / 设置）──
        toggle_bar = QHBoxLayout()
        toggle_bar.setAlignment(Qt.AlignCenter)   # 这组按钮整体居中
        toggle_bar.setSpacing(4)
        self.btn_daily = QPushButton("今日")
        self.btn_weekly = QPushButton("本周")
        self.btn_settings = QPushButton("设置")
        # 三个按钮样式完全一样，循环里统一设置，省得写三遍
        for btn in (self.btn_daily, self.btn_weekly, self.btn_settings):
            btn.setCursor(Qt.PointingHandCursor)   # 鼠标移上去变成「小手」
            btn.setFixedHeight(32)
            btn.setMinimumWidth(64)
            btn.setStyleSheet(
                f"QPushButton{{background:transparent;color:{GRAY};border:none;"
                f"border-radius:8px;padding:0 16px;font-size:13px;}}"
                f"QPushButton:hover{{background:{HOVER};}}"
            )
        # 为什么要包一层 lambda：clicked 信号不带参数，如果直接写 self._switch_tab("daily")，
        # 那么这行代码在「创建界面时」就会被调用一次（括号就是调用），而不是点击时。
        # 包成 lambda 后，函数体要等到点击才执行，于是就能把不同的标签名传给同一个处理函数。
        self.btn_daily.clicked.connect(lambda: self._switch_tab("daily"))
        self.btn_weekly.clicked.connect(lambda: self._switch_tab("weekly"))
        self.btn_settings.clicked.connect(lambda: self._switch_tab("settings"))
        toggle_bar.addWidget(self.btn_daily)
        toggle_bar.addWidget(self.btn_weekly)
        toggle_bar.addWidget(self.btn_settings)
        content_lay.addLayout(toggle_bar)

        # ── 统计区容器：今日/本周两个标签页共用这一套控件 ──
        # 上半是总时长卡（hero）和操作按钮行；下半是左右两栏：
        # 左栏放图表（今日=每小时图+时间线，本周=每日柱状图），右栏放应用排行。
        self.stats_container = QWidget()
        stats_lay = QVBoxLayout(self.stats_container)
        stats_lay.setContentsMargins(0, 0, 0, 0)
        stats_lay.setSpacing(10)

        # ── 总时长卡（最上面那张，显示日期和总时长）──
        self.hero_card = _Card()
        hero_lay = QVBoxLayout(self.hero_card)
        hero_lay.setContentsMargins(20, 16, 20, 16)
        hero_lay.setSpacing(2)

        # 第一行：左边写「屏幕使用时间」，右边写「N 个应用」
        top_row = QHBoxLayout()
        self.lbl_title = QLabel("屏幕使用时间")
        self.lbl_title.setFont(_font(12, True))
        self.lbl_title.setStyleSheet(f"color:{GRAY};")
        top_row.addWidget(self.lbl_title)
        top_row.addStretch()   # 弹簧：把后面的「N 个应用」顶到最右边
        self.lbl_apps = QLabel()
        self.lbl_apps.setFont(_font(12))
        self.lbl_apps.setStyleSheet(f"color:{GRAY};")
        self.lbl_apps.setAlignment(Qt.AlignRight)
        top_row.addWidget(self.lbl_apps)
        hero_lay.addLayout(top_row)

        # 第二行：◀ 日期 ▶ ，以及右侧的大号总时长
        mid_row = QHBoxLayout()

        # 左箭头（看前一天 / 前一周）。点击 → _prev_period 里改日期再刷新。
        # 说明：值都写在 self 上（而不是局部变量），下面刷新数据时才找得到它们。
        self.btn_prev = QPushButton("◀")
        self.btn_prev.setFixedSize(36, 36)
        self.btn_prev.setCursor(Qt.PointingHandCursor)
        self.btn_prev.setStyleSheet(
            f"QPushButton{{background:{CARD};color:{ACCENT};border:1px solid {BORDER};"
            f"border-radius:18px;font-size:15px;font-weight:bold;}}"
            f"QPushButton:hover{{background:{ACCENT};color:white;border-color:{ACCENT};}}"
        )
        self.btn_prev.clicked.connect(self._prev_period)
        mid_row.addWidget(self.btn_prev)

        # 日期显示：上面一行大字（今日 / 周一 / 本周），下面一行小字（具体日期）
        # 这里用「垂直布局 + 居中」，让两行文字叠在中间
        left_col = QVBoxLayout()
        left_col.setAlignment(Qt.AlignCenter)
        self.lbl_day = QLabel()
        self.lbl_day.setFont(_font(18, True))
        self.lbl_day.setStyleSheet(f"color:{TEXT};")
        self.lbl_day.setAlignment(Qt.AlignCenter)
        self.lbl_date = QLabel()
        self.lbl_date.setFont(_font(12))
        self.lbl_date.setStyleSheet(f"color:{GRAY};")
        self.lbl_date.setAlignment(Qt.AlignCenter)
        left_col.addWidget(self.lbl_day)
        left_col.addWidget(self.lbl_date)
        mid_row.addLayout(left_col)

        # 右箭头（看后一天 / 后一周）。不能翻到未来，所以到期时会被置灰，
        # 具体见 _update_arrow_state。
        self.btn_next = QPushButton("▶")
        self.btn_next.setFixedSize(36, 36)
        self.btn_next.setCursor(Qt.PointingHandCursor)
        self.btn_next.setStyleSheet(
            f"QPushButton{{background:{CARD};color:{ACCENT};border:1px solid {BORDER};"
            f"border-radius:18px;font-size:15px;font-weight:bold;}}"
            f"QPushButton:hover{{background:{ACCENT};color:white;border-color:{ACCENT};}}"
        )
        self.btn_next.clicked.connect(self._next_period)
        mid_row.addWidget(self.btn_next)

        mid_row.addStretch()   # 弹簧：把最右边的大号总时长顶到右端
        self.lbl_total = QLabel()
        self.lbl_total.setFont(_font(28, True))   # 28 号大字，视觉焦点
        self.lbl_total.setStyleSheet(f"color:{ACCENT};")
        self.lbl_total.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        mid_row.addWidget(self.lbl_total)
        hero_lay.addLayout(mid_row)

        stats_lay.addWidget(self.hero_card)

        # ── 操作按钮行（暂停 / 清空 / 忽略列表）──
        act_bar = QHBoxLayout()
        act_bar.setSpacing(8)
        self.btn_pause = QPushButton("暂停统计")
        self.btn_clear = QPushButton("清空数据")
        self.btn_ignore = QPushButton("忽略列表")
        self.btn_export = QPushButton("导出 CSV")
        for btn in (self.btn_pause, self.btn_clear, self.btn_ignore, self.btn_export):
            btn.setCursor(Qt.PointingHandCursor)
            btn.setFixedHeight(28)
            btn.setStyleSheet(
                f"QPushButton{{background:{CARD};color:{GRAY};border:none;"
                f"border-radius:8px;padding:0 14px;font-size:12px;}}"
                f"QPushButton:hover{{color:{TEXT};}}"
            )
        # 三个按钮各连一个处理函数：暂停键的文字会在两种状态间来回切换（见 _update_pause_button）
        self.btn_pause.clicked.connect(self._toggle_pause)
        self.btn_clear.clicked.connect(self._clear_data)
        self.btn_ignore.clicked.connect(self._show_ignore_dialog)
        self.btn_export.clicked.connect(self._export_csv)
        act_bar.addWidget(self.btn_pause)
        act_bar.addWidget(self.btn_clear)
        act_bar.addWidget(self.btn_ignore)
        act_bar.addWidget(self.btn_export)
        act_bar.addStretch()   # 弹簧：让按钮靠左排，不要被拉伸
        stats_lay.addLayout(act_bar)

        # ── 主体：左右两栏（横屏布局）──
        # 左边一列放图表，右边放排行榜。这样排行榜能拿到一整列的高度，
        # 比原来挤在底部能多显示很多行。
        body_row = QHBoxLayout()
        body_row.setSpacing(10)
        left_col = QVBoxLayout()
        left_col.setSpacing(10)

        # ── 每日柱状图卡（只在「本周」页显示）──
        # 提示：卡片和图表都先 hide()，具体显示哪一个由 _switch_tab 决定。
        self.chart_card = _Card()
        chart_lay = QVBoxLayout(self.chart_card)
        chart_lay.setContentsMargins(14, 10, 14, 10)
        lbl_chart = QLabel("每日使用")
        lbl_chart.setFont(_font(11, True))
        lbl_chart.setStyleSheet(f"color:{GRAY};")
        chart_lay.addWidget(lbl_chart)
        self.bar_chart = _BarChart()   # 自定义手绘控件，见文件开头
        chart_lay.addWidget(self.bar_chart)
        self.chart_card.hide()
        left_col.addWidget(self.chart_card)

        # ── 每小时柱状图卡（只在「今日」页显示）──
        self.hourly_card = _Card()
        hourly_lay = QVBoxLayout(self.hourly_card)
        hourly_lay.setContentsMargins(14, 10, 14, 10)
        lbl_hourly = QLabel("每小时使用")
        lbl_hourly.setFont(_font(11, True))
        lbl_hourly.setStyleSheet(f"color:{GRAY};")
        hourly_lay.addWidget(lbl_hourly)
        # fixed_labels=True：左侧刻度固定为 0/30m/60m（因为每小时最多 3600 秒）
        self.hourly_chart = _HourlyChart(fixed_labels=True)
        hourly_lay.addWidget(self.hourly_chart)
        self.hourly_card.hide()
        left_col.addWidget(self.hourly_card)

        # ── 应用使用时间线卡（只在「今日」页显示）──
        self.timeline_card = _Card()
        timeline_lay = QVBoxLayout(self.timeline_card)
        timeline_lay.setContentsMargins(14, 10, 14, 10)
        lbl_timeline = QLabel("应用使用时间线（Ctrl+滚轮缩放）")
        lbl_timeline.setFont(_font(11, True))
        lbl_timeline.setStyleSheet(f"color:{GRAY};")
        timeline_lay.addWidget(lbl_timeline)
        # 时间线内容高度随应用数量变化，用 QScrollArea 包一层，超高时内部滚动。
        # setFixedHeight 固定可视高度，避免它把下面的排行榜挤没了。
        self.timeline_view = _TimelineView()
        tl_scroll = QScrollArea()
        tl_scroll.setWidgetResizable(True)
        tl_scroll.setFrameShape(QFrame.NoFrame)
        tl_scroll.setStyleSheet("QScrollArea{border:none;background:transparent;}")
        # 关键：把滚动区的「视口」也设为透明。否则在 Windows 深色模式下，
        # 视口会用自己的系统底色（深色）盖住卡片，时间线文字就糊成一片。
        tl_scroll.viewport().setAutoFillBackground(False)
        tl_scroll.setWidget(self.timeline_view)
        self.timeline_view.setAutoFillBackground(False)
        tl_scroll.setFixedHeight(220)
        timeline_lay.addWidget(tl_scroll)
        self.timeline_card.hide()
        left_col.addWidget(self.timeline_card)
        from ui.browser_panel import WebsiteUsagePanel
        self.website_card = _Card()
        website_layout = QVBoxLayout(self.website_card)
        website_layout.setContentsMargins(14, 14, 14, 14)
        self.website_panel = WebsiteUsagePanel(self.website_card)
        website_layout.addWidget(self.website_panel)
        # 网站时间线保持页面内展示，排行独占右栏，避免小窗口只剩一两行。
        left_col.insertWidget(left_col.indexOf(self.timeline_card), self.website_card)
        left_col.addStretch()   # 图表靠上排列，左栏下方多余空间留白

        # ── 应用使用排行卡（今日 / 本周两个页面都会用）──
        list_card = _Card()
        list_lay = QVBoxLayout(list_card)
        list_lay.setContentsMargins(0, 10, 0, 10)
        list_lay.setSpacing(0)

        self.lbl_list_title = QLabel("  应用使用排行")
        self.lbl_list_title.setFont(_font(13, True))
        # 标题文字前面那两个空格是故意留的缩进（这里不便用 padding 影响其他元素）
        self.lbl_list_title.setStyleSheet(f"color:{GRAY};padding:0 14px;")
        list_lay.addWidget(self.lbl_list_title)

        # 排行榜会很长，用 QScrollArea 包一层，内容超长时自动出现滚动条。
        # 样式表里 QScrollBar::handle 是滑块、::add-line/::sub-line 是滚动条
        # 两端的箭头按钮（这里高度设成 0，等于把箭头藏掉）。
        scroll = QScrollArea()
        # setWidgetResizable(True)：让内部控件宽度跟随滚动区自动拉伸，
        # 否则列表会缩成一条窄窄的竖条。
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet(
            f"QScrollArea{{border:none;background:transparent;}}"
            f"QScrollBar:vertical{{background:transparent;width:6px;border-radius:3px;}}"
            f"QScrollBar::handle:vertical{{background:{BORDER};border-radius:3px;min-height:20px;}}"
            f"QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{{height:0;}}"
        )
        # 排行榜滚动区的视口同样要透明，理由同上面的时间线滚动区。
        scroll.viewport().setAutoFillBackground(False)
        # 排行榜的每一行不是固定的控件，而是每次刷新时动态生成的（见 _populate_list）。
        # 所以这里只准备一个空的容器 + 垂直布局，往里面 insertWidget 加行即可。
        self.list_container = QWidget()
        self.list_layout = QVBoxLayout(self.list_container)
        self.list_layout.setContentsMargins(0, 0, 0, 0)
        self.list_layout.setSpacing(1)   # 行与行之间的细缝
        # 末尾先放一个弹簧：这样行会紧贴顶部排列，而 _populate_list 里
        # 判断「该删几行」也依赖这个弹簧（保留最后一项）。
        self.list_layout.addStretch()
        scroll.setWidget(self.list_container)
        self.list_container.setAutoFillBackground(False)
        list_lay.addWidget(scroll)

        # 组装左右两栏：左栏图表列（伸缩 1）、右栏排行榜卡（伸缩 1，各占约一半宽）
        graphs = QWidget()
        graphs.setLayout(left_col)
        left_col.setContentsMargins(0, 0, 4, 0)
        graph_scroll = QScrollArea()
        graph_scroll.setWidgetResizable(True)
        graph_scroll.setFrameShape(QFrame.NoFrame)
        graph_scroll.setStyleSheet(
            f"QScrollArea{{border:none;background:transparent;}}"
            f"QScrollBar:vertical{{background:transparent;width:6px;}}"
            f"QScrollBar::handle:vertical{{background:{BORDER};border-radius:3px;min-height:24px;}}"
            "QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{height:0;}"
        )
        graph_scroll.viewport().setAutoFillBackground(False)
        graph_scroll.setWidget(graphs)
        graphs.setAutoFillBackground(False)
        body_row.addWidget(graph_scroll, 1)
        body_row.addWidget(list_card, 1)
        # 主体两栏吃满剩余高度（伸缩 1），窗口变高时排行榜跟着变高
        stats_lay.addLayout(body_row, 1)

        # 把整个统计区放进内容容器
        content_lay.addWidget(self.stats_container)

        # ── 设置页（内容和统计区并列，靠显示/隐藏切换）──
        # 也套一层 QScrollArea：设置项是越加越多的（无操作休眠、数据上传、
        # 开机自启、数据存储、自动导出……），窗口被用户拖小之后总高度会超出，
        # 不套滚动区的话底部的「隐私与安全」会被直接裁掉，用户根本看不到。
        self.settings_widget = self._build_settings_page()
        settings_scroll = QScrollArea()
        settings_scroll.setWidgetResizable(True)   # 内容宽度跟随滚动区拉伸
        settings_scroll.setStyleSheet(
            f"QScrollArea{{border:none;background:transparent;}}"
            f"QScrollBar:vertical{{background:transparent;width:6px;border-radius:3px;}}"
            f"QScrollBar::handle:vertical{{background:{BORDER};border-radius:3px;min-height:20px;}}"
            f"QScrollBar::add-line:vertical,QScrollBar::sub-line:vertical{{height:0;}}"
        )
        # 视口也要透明，否则滚动区会盖上一层默认底色，圆角窗口里很显眼
        settings_scroll.viewport().setAutoFillBackground(False)
        settings_scroll.setWidget(self.settings_widget)
        self.settings_widget.setAutoFillBackground(False)
        settings_scroll.hide()
        self.settings_scroll = settings_scroll   # _switch_tab 里要显示/隐藏它
        content_lay.addWidget(settings_scroll)

        outer.addWidget(content)
        # 界面搭完后切到「今日」页，顺便完成第一次数据加载
        self._switch_tab("daily")

    # ── 窗口拖动 ──────────────────────────────────────────────
    # 因为窗口是无边框的（FramelessWindowHint），系统不再提供「按住标题栏拖动窗口」的功能，所以下面三个事件处理函数要自己实现一套。
    # 原理：按下鼠标时记下「光标相对窗口左上角的偏移」，之后光标移动时，
    # 把窗口左上角挪到「当前光标位置 - 那个偏移」，窗口就跟着手走了。

    def mousePressEvent(self, event):
        """鼠标在本窗口上按下时由 Qt 调用：记下拖动起点。"""
        if event.button() == Qt.LeftButton:
            # globalPosition() 是相对整个屏幕的坐标，self.pos() 是窗口左上角
            # 的屏幕坐标，两者相减就得到「光标离窗口左上角有多远」。
            # toPoint() 把浮点坐标取整成整数坐标，move() 需要整数。
            self._drag_pos = event.globalPosition().toPoint() - self.pos()
            # accept() 表示这个事件我处理了，不要再往父控件传递
            event.accept()

    def mouseMoveEvent(self, event):
        """鼠标移动时由 Qt 调用：如果正在按住左键拖动，就搬动窗口。"""
        # event.buttons() 是「此刻按住的键」，& Qt.LeftButton 判断左键是否在按下状态；
        # 注意不能写成 button()，那个只在「按下/松开」那一刻才有值。
        if self._drag_pos is not None and event.buttons() & Qt.LeftButton:
            self.move(event.globalPosition().toPoint() - self._drag_pos)
            event.accept()

    def mouseReleaseEvent(self, event):
        """松开鼠标时由 Qt 调用：清掉拖动状态，结束本次拖动。"""
        self._drag_pos = None

    def _build_settings_page(self):
        """搭建「设置」标签页的界面，并把整体返回给调用方。

        返回值：按常规、网站记录、同步与邮件、隐私分区的单列设置页。

        注意它只是「搭出来」，并不负责显示；显示与否由 _switch_tab 控制。
        """
        widget = QWidget()   # 用一个空 QWidget 当整页的容器
        widget.setFont(_font(10))
        widget.setStyleSheet(
            f"QLabel{{color:{TEXT};}}"
            f"QLineEdit,QComboBox{{background:{BG};color:{TEXT};border:1px solid {BORDER};"
            "border-radius:8px;padding:7px 10px;}"
        )
        flow = QVBoxLayout(widget)
        flow.setContentsMargins(0, 4, 0, 10)
        flow.setSpacing(14)
        heading = QLabel("偏好设置")
        heading.setFont(_font(17, True))
        heading.setStyleSheet(f"color:{TEXT};padding:4px 0;")
        flow.addWidget(heading)
        nav = QHBoxLayout()
        nav.setSpacing(8)
        self._settings_buttons = {}
        for key, title in (("general", "常规"), ("web", "网站记录"),
                           ("sync", "同步与邮件"), ("privacy", "隐私与安全")):
            button = QPushButton(title)
            button.setCheckable(True)
            button.setCursor(Qt.PointingHandCursor)
            button.setStyleSheet(
                f"QPushButton{{background:{CARD};color:{GRAY};border:none;border-radius:9px;padding:10px 16px;}}"
                f"QPushButton:checked{{background:{ACCENT};color:white;}}"
                f"QPushButton:hover:!checked{{background:{HOVER};color:{TEXT};}}"
            )
            button.clicked.connect(lambda checked, section=key: self._show_settings_section(section))
            self._settings_buttons[key] = button
            nav.addWidget(button)
        nav.addStretch()
        flow.addLayout(nav)

        # 勾选框的统一样式，本页所有勾选框（自动上传 / 开机自启 / 自动导出）共用一份，
        # 避免写三遍改漏一个。
        # QCheckBox::indicator 是勾选框那个小方块本身，checked 是「已勾选」状态。
        # 小方块的底色用 BG 而不是写死 white，这样深色主题下才不会白得刺眼。
        chk_style = (
            f"QCheckBox{{color:{TEXT};spacing:8px;}}"
            f"QCheckBox::indicator{{width:16px;height:16px;border-radius:8px;"
            f"border:2px solid {BORDER};background:{BG};}}"
            f"QCheckBox::indicator:checked{{background:{ACCENT};border-color:{ACCENT};}}"
        )

        # ── 隐私说明（通栏，放在最底部当页脚）──
        priv_card = _Card()
        # 隐私说明里有两行是「随状态变化」的：一行是会不会联网（跟着上传开关走），
        # 一行是会不会把邮箱发出去（跟着订阅开关走）。它们建好之后还要能被改，
        # 所以按 key 存下控件引用 —— 比事后按「第几个布局、第几个控件」去翻
        # 结构稳得多，加一行也不会把下标全打乱。
        self._priv_labels = {}
        priv_lay = QVBoxLayout(priv_card)
        priv_lay.setContentsMargins(20, 18, 20, 18)
        priv_lay.setSpacing(10)

        priv_title = QLabel("  隐私与安全")
        priv_title.setFont(_font(13, True))
        priv_title.setStyleSheet(f"color:{TEXT};")
        priv_lay.addWidget(priv_title)

        # 第一行随「数据上传」开关变化：开了就要如实说数据会出本机，
        # 不能等用户自己发现。这一行是整个隐私说明里最要紧的一条 ——
        # 之前这里写的是「不联网，无任何网络请求」，加了上传功能之后
        # 那句话就不成立了，留着等于骗人。
        if get_setting("upload_enabled", "1") == "1":
            net_line = (ACCENT, "•",
                        f"数据上传已开启：每天会把「应用名 + 使用时长」发往服务器，用于生成每日报告")
        else:
            net_line = (GREEN, "✓", "数据上传已关闭，程序不会发出任何网络请求")

        # 每项是「(行标识, (图标颜色, 图标, 说明文字))」，用 for 循环逐行生成，
        # 以后要加一条声明只要往这个列表里加一项。
        #
        # 注意这里**没有**「不收集任何个人信息」这一条：能填订阅邮箱之后那句
        # 就不成立了，邮箱就是个人信息。现在改成由下面那行如实说明「填了才会
        # 发出去、不填就不发」—— 留着原来那句等于骗人。
        items = [
            ("net", net_line),
            ("data", (GREEN, "✓", "上传应用统计，以及你单独开启的网站汇总和时间线；"
                                  "不包含窗口标题、键盘输入或屏幕截图。")),
            ("mail", self._privacy_mail_line()),
            ("bg", (GREEN, "✓", "关闭软件后不会在后台运行任何数据采集")),
        ]
        for key, (color, icon, text) in items:
            # 每一行是一个「水平布局」：图标 + 说明文字
            row = QHBoxLayout()
            row.setSpacing(8)
            icon_lbl = QLabel(icon)
            icon_lbl.setFont(_font(12))
            icon_lbl.setStyleSheet(f"color:{color};")
            icon_lbl.setFixedWidth(16)   # 固定宽度，让所有文字左边缘对齐
            text_lbl = QLabel(text)
            text_lbl.setFont(_font(11))
            text_lbl.setStyleSheet(f"color:{GRAY};")
            # 允许文字自动折行，否则窄窗口下会被截断
            text_lbl.setWordWrap(True)
            row.addWidget(icon_lbl)
            row.addWidget(text_lbl, 1)   # 伸缩因子 1：文字占满剩余宽度
            priv_lay.addLayout(row)
            # 会随状态变化的那两行要能被改回来，把控件存下来
            if key in ("net", "mail"):
                self._priv_labels[key] = [icon_lbl, text_lbl]

        # 隐私说明**最后才加** —— 瀑布流是按加入顺序摆放的，在这里加就等于
        # 把它塞到所有卡片中间了。真正加在函数末尾（见 flow.addWidget(priv_card)），
        # 这里只是提前把内容建好，顺便让 _privacy_mail_line() 能拿到控件引用。

        # ── 无操作休眠 ──
        idle_card = _Card()
        idle_lay = QVBoxLayout(idle_card)
        idle_lay.setContentsMargins(20, 18, 20, 18)
        idle_lay.setSpacing(12)

        idle_title = QLabel("  无操作休眠")
        idle_title.setFont(_font(13, True))
        idle_title.setStyleSheet(f"color:{TEXT};")
        idle_lay.addWidget(idle_title)

        idle_desc = QLabel("超过这段时间没有键鼠操作，就暂停累计使用时长。")
        idle_desc.setWordWrap(True)
        idle_desc.setFont(_font(11))
        idle_desc.setStyleSheet(f"color:{GRAY};")
        idle_lay.addWidget(idle_desc)

        # 读出用户上次设置的秒数（storage 里存的是字符串，所以要转成 int）。
        # 第二个参数 "300" 是「键不存在时用的默认值」。
        current_idle = int(get_setting("idle_threshold", "300"))

        # 快捷按钮行：点一下就等于把滑块拨到对应秒数
        preset_row = QHBoxLayout()
        preset_row.setSpacing(8)
        self._idle_presets = []   # 记下这几个按钮，之后要高亮「当前选中」的那个
        for label, secs in [("1 分钟", 60), ("3 分钟", 180), ("5 分钟", 300), ("10 分钟", 600)]:
            btn = QPushButton(label)
            btn.setFixedHeight(30)
            btn.setCursor(Qt.PointingHandCursor)
            # setProperty 给控件挂一个自定义属性，把「这个按钮代表多少秒」
            # 存在按钮自己身上，回调时用 btn.property("idle_secs") 取出来，
            # 这样四个按钮可以共用同一个处理函数。
            btn.setProperty("idle_secs", secs)
            # 经典的 Python 闭包陷阱：循环里的 btn 变量在循环结束后是最后一个按钮，
            # 如果写成 lambda checked, b=btn: ... 之外的写法（比如直接引用 btn），
            # 四个按钮点下去都会以为是第 4 个。
            # 把 btn 作为「默认参数 b=btn」在定义时就绑死，每次都记住自己那一个。
            # 前面的 checked 参数是必须的：clicked 信号会额外传一个布尔值过来，
            # 形参列表里得留个位置接住它。
            btn.clicked.connect(lambda checked, b=btn: self._on_idle_preset(b))
            self._idle_presets.append(btn)
            preset_row.addWidget(btn)
        # 按当前值给选中的那个按钮涂上主题色
        self._update_idle_preset_style(current_idle)
        idle_lay.addLayout(preset_row)

        # 滑块：可以连续调节，和上面的快捷按钮是同一份数据（共用 idle_threshold 设置项）
        slider_row = QHBoxLayout()
        slider_row.setSpacing(12)
        self.idle_slider = QSlider(Qt.Horizontal)   # 水平方向的滑块
        self.idle_slider.setMinimum(60)        # 最少 1 分钟
        self.idle_slider.setMaximum(600)       # 最多 10 分钟
        self.idle_slider.setSingleStep(60)     # 每按一次方向键走 60 秒
        self.idle_slider.setTickInterval(60)   # 刻度间隔也是 60 秒
        self.idle_slider.setValue(current_idle)  # 初始位置 = 用户当前设置
        # 值变化 → _on_idle_changed：更新右边文字、按钮高亮，并存进 storage
        self.idle_slider.valueChanged.connect(self._on_idle_changed)
        self.lbl_idle_val = QLabel(f"{current_idle // 60} 分钟")   # // 是整除，把秒换成分钟
        self.lbl_idle_val.setFont(_font(12, True))
        self.lbl_idle_val.setStyleSheet(f"color:{ACCENT};min-width:48px;")
        self.lbl_idle_val.setAlignment(Qt.AlignCenter)
        slider_row.addWidget(self.idle_slider)
        slider_row.addWidget(self.lbl_idle_val)
        idle_lay.addLayout(slider_row)

        flow.addWidget(idle_card)

        # ── 数据上传 ──
        upload_card = _Card()
        up_lay = QVBoxLayout(upload_card)
        up_lay.setContentsMargins(20, 18, 20, 18)
        up_lay.setSpacing(8)

        up_title = QLabel("同步与邮件")
        up_title.setFont(_font(13, True))
        up_title.setStyleSheet(f"color:{TEXT};")
        up_lay.addWidget(up_title)

        up_desc = QLabel("自动同步使用统计，用于生成每日报告。网站和时间线上传在「网站记录」中单独开启。")
        up_desc.setFont(_font(11))
        up_desc.setStyleSheet(f"color:{GRAY};")
        up_desc.setWordWrap(True)
        up_lay.addWidget(up_desc)

        self.chk_upload = QCheckBox("自动上传（每天一次）")
        self.chk_upload.setFont(_font(11))
        self.chk_upload.setStyleSheet(chk_style)
        self.chk_upload.setChecked(get_setting("upload_enabled", "1") == "1")
        self.chk_upload.toggled.connect(self._on_upload_toggled)
        up_lay.addWidget(self.chk_upload)

        device_info = QWidget()
        device_layout = QVBoxLayout(device_info)
        device_layout.setContentsMargins(10, 4, 10, 10)
        device_info.hide()
        device_toggle = QPushButton("+  设备与连接详情")
        device_toggle.setCheckable(True)
        device_toggle.setStyleSheet(f"QPushButton{{color:{GRAY};text-align:left;background:transparent;border:none;padding:6px 0;}}")
        device_toggle.toggled.connect(device_info.setVisible)
        device_toggle.toggled.connect(lambda checked: device_toggle.setText(("-  " if checked else "+  ") + "设备与连接详情"))
        up_lay.addWidget(device_toggle)
        up_lay.addWidget(device_info)
        # 设备 ID。用 get_or_create_user_key() 而不是只读设置项：第一次打开
        # 设置页时它就该生成好，这样用户能立刻看到自己是谁。
        id_sub = QLabel("设备 ID")
        id_sub.setFont(_font(10, True))
        id_sub.setStyleSheet(f"color:{GRAY};")
        device_layout.addWidget(id_sub)

        self.lbl_device_id = QLabel(uploader.get_or_create_user_key())
        self.lbl_device_id.setFont(_font(10))
        self.lbl_device_id.setStyleSheet(f"color:{MUTED};")
        self.lbl_device_id.setWordWrap(True)
        # 允许选中复制 —— 想知道服务端哪份数据是自己的，只有这个办法
        self.lbl_device_id.setTextInteractionFlags(Qt.TextSelectableByMouse)
        device_layout.addWidget(self.lbl_device_id)

        id_hint = QLabel("服务器靠它区分机器。它同时是读取数据的凭据，请不要发给别人。")
        id_hint.setFont(_font(9))
        id_hint.setStyleSheet(f"color:{MUTED};")
        id_hint.setWordWrap(True)
        device_layout.addWidget(id_hint)

        # 状态行：最近一次上传时间；下面那行显示「测试连接」的结果
        self.lbl_upload_status = QLabel()
        self.lbl_upload_status.setFont(_font(10))
        self.lbl_upload_status.setStyleSheet(f"color:{GRAY};")
        self.lbl_upload_status.setWordWrap(True)
        device_layout.addWidget(self.lbl_upload_status)
        self.lbl_upload_summary = QLabel()
        self.lbl_upload_summary.setFont(_font(10))
        self.lbl_upload_summary.setStyleSheet(f"color:{GRAY};")
        up_lay.addWidget(self.lbl_upload_summary)

        self.lbl_conn = QLabel()
        self.lbl_conn.setFont(_font(10))
        self.lbl_conn.setStyleSheet(f"color:{GRAY};")
        self.lbl_conn.setWordWrap(True)
        up_lay.addWidget(self.lbl_conn)
        self.lbl_conn.hide()
        self._refresh_upload_status()   # 先把「最近上传」那行填上

        # 两个按钮并排
        up_btn_row = QHBoxLayout()
        up_btn_row.setSpacing(8)
        self.btn_upload = _flat_button("立即上传", self._upload_now)
        self.btn_conn = _flat_button("测试连接", self._test_connection)
        up_btn_row.addWidget(self.btn_upload)
        up_btn_row.addWidget(self.btn_conn)
        up_btn_row.addStretch()   # 弹簧：让按钮靠左排，不被拉开
        up_lay.addLayout(up_btn_row)

        # ── 每日报告邮件（订阅）──
        # 和上面的「自动上传」是两件事，所以给一个小标题分开，
        # 免得用户以为关掉上传就等于退订了。
        sub_head = QLabel("每日报告邮件")
        sub_head.setFont(_font(10, True))
        sub_head.setStyleSheet(f"color:{GRAY};")
        up_lay.addWidget(sub_head)

        sub_desc = QLabel("每天 00:03 发送昨日使用报告；修改邮箱后请点击保存。")
        sub_desc.setFont(_font(9))
        sub_desc.setStyleSheet(f"color:{MUTED};")
        sub_desc.setWordWrap(True)
        up_lay.addWidget(sub_desc)

        self.chk_subscribe = QCheckBox("订阅每日报告（要填邮箱）")
        self.chk_subscribe.setFont(_font(11))
        self.chk_subscribe.setStyleSheet(chk_style)
        self.chk_subscribe.setChecked(uploader.is_subscribed())
        self.chk_subscribe.toggled.connect(self._on_subscribe_toggled)
        up_lay.addWidget(self.chk_subscribe)

        # 输入框和「保存」并排：邮箱按回车也该能保存（见下面的 returnPressed），
        # 这是输入框的肌肉记忆，不接的话用户会以为程序卡了。
        sub_row = QHBoxLayout()
        sub_row.setSpacing(8)
        self.edit_email = QLineEdit()
        self.edit_email.setObjectName("subscriptionEmail")
        self.edit_email.setAccessibleName("每日报告收件邮箱")
        self.edit_email.setPlaceholderText("you@example.com")
        self.edit_email.setText(uploader.get_email() or "")
        self.edit_email.setFont(_font(11))
        self.edit_email.setStyleSheet(
            f"QLineEdit{{background:{BG};color:{TEXT};border:1px solid {BORDER};"
            f"border-radius:8px;padding:6px 10px;font-size:13px;}}"
            f"QLineEdit:focus{{border-color:{ACCENT};}}"
            f"QLineEdit{{selection-background-color:{ACCENT};selection-color:white;}}"
            f"QLineEdit:disabled{{background:{CARD};color:{TEXT};border-color:{BORDER};}}"
        )
        email_palette = self.edit_email.palette()
        email_palette.setColor(QPalette.Base, QColor(BG))
        email_palette.setColor(QPalette.Text, QColor(TEXT))
        email_palette.setColor(QPalette.PlaceholderText, QColor(GRAY))
        email_palette.setColor(QPalette.Disabled, QPalette.Base, QColor(CARD))
        email_palette.setColor(QPalette.Disabled, QPalette.Text, QColor(TEXT))
        self.edit_email.setPalette(email_palette)
        self.edit_email.setEnabled(self.chk_subscribe.isChecked())
        self.edit_email.returnPressed.connect(self._save_subscription)
        # 输入框里有没有字决定「保存」能不能点（用来退订），所以要跟着打字走
        self.edit_email.textChanged.connect(self._sync_subscribe_buttons)
        sub_row.addWidget(self.edit_email, 1)   # 伸缩因子 1：输入框占满剩余宽度
        self.btn_subscribe = _flat_button("保存", self._save_subscription)
        self.btn_subscribe.setEnabled(self.chk_subscribe.isChecked())
        sub_row.addWidget(self.btn_subscribe)
        up_lay.addLayout(sub_row)

        self.lbl_subscribe = QLabel()
        self.lbl_subscribe.setFont(_font(10))
        self.lbl_subscribe.setStyleSheet(f"color:{GRAY};")
        self.lbl_subscribe.setWordWrap(True)
        up_lay.addWidget(self.lbl_subscribe)
        self._refresh_subscribe_hint()

        flow.addWidget(upload_card)
        from ui.browser_panel import BrowserSettingsPanel
        browser_card = _Card()
        browser_layout = QVBoxLayout(browser_card)
        browser_layout.setContentsMargins(20, 18, 20, 18)
        self.browser_settings = BrowserSettingsPanel(browser_card)
        browser_layout.addWidget(self.browser_settings)
        flow.addWidget(browser_card)

        # ── 系统（开机自启 + 数据存储 + 自动导出，几张稀疏的合并成一张）──
        sys_card = _Card()
        sys_lay = QVBoxLayout(sys_card)
        sys_lay.setContentsMargins(20, 18, 20, 18)
        sys_lay.setSpacing(10)

        sys_title = QLabel("  系统")
        sys_title.setFont(_font(13, True))
        sys_title.setStyleSheet(f"color:{TEXT};")
        sys_lay.addWidget(sys_title)

        # 开机自启（一行勾选框）
        self.chk_autostart = QCheckBox("开机时自动启动并最小化到系统托盘")
        self.chk_autostart.setFont(_font(11))
        self.chk_autostart.setStyleSheet(chk_style)
        # 真实状态以系统注册表为准（用户可能在别处改过），所以要问 autostart；
        # 读不到就当作没开启，不能让设置页因为读注册表失败而打不开。
        try:
            from system.autostart import is_autostart_enabled
            self.chk_autostart.setChecked(is_autostart_enabled())
        except Exception:
            self.chk_autostart.setChecked(False)
        # toggled 信号在勾选状态改变时发出（带一个「现在是否勾选」的布尔值）
        self.chk_autostart.toggled.connect(self._on_autostart_toggled)
        sys_lay.addWidget(self.chk_autostart)

        # 数据存储小节：路径 + 大小 + 打开文件夹按钮
        data_sub = QLabel("数据文件位置")
        data_sub.setFont(_font(10, True))
        data_sub.setStyleSheet(f"color:{GRAY};")
        sys_lay.addWidget(data_sub)

        path_lbl = QLabel(DB_PATH)
        path_lbl.setFont(_font(10))
        path_lbl.setStyleSheet(f"color:{GRAY};")
        path_lbl.setWordWrap(True)
        # 允许用鼠标选中路径文字，方便复制
        path_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        sys_lay.addWidget(path_lbl)

        size_lbl = QLabel(self._db_size_text())
        size_lbl.setFont(_font(10))
        size_lbl.setStyleSheet(f"color:{MUTED};")
        sys_lay.addWidget(size_lbl)

        open_btn = _flat_button("打开所在文件夹", self._open_data_folder)
        sys_lay.addWidget(open_btn, alignment=Qt.AlignLeft)

        # 自动导出小节：开关 + 导出目录 + 两个操作按钮
        # 后台的 ExportScheduler 会定期把使用时间写成 CSV 放进这个目录
        export_sub = QLabel("自动导出")
        export_sub.setFont(_font(10, True))
        export_sub.setStyleSheet(f"color:{GRAY};")
        sys_lay.addWidget(export_sub)

        self.chk_export = QCheckBox("自动导出为 CSV（按 年/月/日 存放）")
        self.chk_export.setFont(_font(11))
        self.chk_export.setStyleSheet(chk_style)
        # 设置值统一以字符串存在库里，所以这里和 "1" 比较
        self.chk_export.setChecked(get_setting("export_enabled", "1") == "1")
        self.chk_export.toggled.connect(self._on_export_toggled)
        sys_lay.addWidget(self.chk_export)

        export_path_lbl = QLabel(data_dir())
        export_path_lbl.setFont(_font(10))
        export_path_lbl.setStyleSheet(f"color:{GRAY};")
        export_path_lbl.setWordWrap(True)
        export_path_lbl.setTextInteractionFlags(Qt.TextSelectableByMouse)
        sys_lay.addWidget(export_path_lbl)

        # 两个按钮并排。样式一样，所以用循环建，省得复制粘贴两遍
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        for text, slot in (("打开导出文件夹", self._open_export_folder),
                           ("立即导出", self._export_now)):
            btn_row.addWidget(_flat_button(text, slot))
        btn_row.addStretch()   # 弹簧：让两个按钮靠左排，不被拉开
        sys_lay.addLayout(btn_row)

        flow.addWidget(sys_card)

        # 隐私说明最后加，跟其它卡片一样参与瀑布流（不分栏、不通栏）
        flow.addWidget(priv_card)
        self._settings_groups = {"general": (idle_card, sys_card), "web": (browser_card,),
                                 "sync": (upload_card,), "privacy": (priv_card,)}
        flow.addStretch()
        self._show_settings_section("general")
        return widget

    def _show_settings_section(self, section):
        for key, cards in self._settings_groups.items():
            self._settings_buttons[key].setChecked(key == section)
            for card in cards:
                card.setVisible(key == section)

    def _db_size_text(self):
        """把数据库文件大小格式化成人类可读的文本，如「大小：10.1 MB」。"""
        try:
            size = os.path.getsize(DB_PATH)
        except OSError:
            return "大小：未知"
        if size < 1024:
            return f"大小：{size} B"
        if size < 1024 * 1024:
            return f"大小：{size / 1024:.1f} KB"
        return f"大小：{size / 1024 / 1024:.1f} MB"

    def _open_data_folder(self):
        """在系统资源管理器里打开数据库文件所在的文件夹。"""
        folder = os.path.dirname(DB_PATH)
        self._open_folder(folder)

    def _open_export_folder(self):
        """在系统资源管理器里打开自动导出的 CSV 目录。"""
        self._open_folder(data_dir())

    def _open_folder(self, folder):
        """用资源管理器打开一个目录。两个「打开文件夹」按钮共用这段逻辑。

        参数：
            folder：要打开的目录路径。
        """
        try:
            # os.startfile 是 Windows 专用：对文件夹调用会在资源管理器里打开它
            os.startfile(folder)
        except OSError:
            QMessageBox.warning(self, "打开失败", f"无法打开文件夹：\n{folder}")

    def _on_export_toggled(self, checked):
        """勾选/取消「自动导出」：把开关写进设置表。

        不用通知后台调度器 —— 它每轮开始前都会重新读这个设置，
        所以改完最多等一轮（默认半小时）就会按新值生效。
        """
        set_setting("export_enabled", "1" if checked else "0")

    def _export_now(self):
        """点「立即导出」：把库里所有有数据的日期整体导一遍。

        和后台自动导出共用 exporter 里的逻辑，所以文件的命名和存放位置
        完全一致，只是这里不理会「水位线」，强制全部重导一次。
        """
        try:
            written = export_all()
        except Exception as e:
            QMessageBox.warning(self, "导出失败", f"导出过程中出错：\n{e}")
            return
        QMessageBox.information(
            self, "导出完成",
            f"已生成 {len(written)} 个 CSV 文件：\n{data_dir()}")

    # ── 数据上传 ──────────────────────────────────────────────

    def _on_upload_toggled(self, checked):
        """勾选/取消「自动上传」：把开关写进设置表。

        和自动导出一样，不需要通知后台调度器 —— 它每轮开始前都会重新读
        这个设置，所以改完最多等一轮（默认一小时）就按新值生效。
        """
        set_setting("upload_enabled", "1" if checked else "0")
        # 隐私说明那一行是跟着这个开关走的，勾了之后要立刻改过来，
        # 不能让用户以为「关了但说明还写着会上传」。重建整页代价太大，
        # 所以只把那一行的文字和颜色换掉（它是 priv_lay 里的第一个布局）。
        self._update_privacy_line(checked)

    def _update_privacy_line(self, upload_on):
        """同步隐私说明里「会不会联网」那一行。

        这一行的文字和颜色是建页面时按当时的开关状态定死的，用户改了开关
        就得跟着改 —— 否则关掉上传之后，说明里还写着「每天会把数据发往…」，
        等于在骗人（反过来更糟：开着上传却写着「不会发出任何网络请求」）。
        """
        pair = self._priv_labels.get("net")
        if not pair:
            return   # 界面还没建好（理论上不会发生），安静跳过
        icon_lbl, text_lbl = pair
        if upload_on:
            icon_lbl.setText("•")
            icon_lbl.setStyleSheet(f"color:{ACCENT};")
            text_lbl.setText(f"基础上传已开启：将「应用名 + 使用时长」发往 "
                             f"{config.base_url()}；网站域名与时间线上传由独立开关控制")
        else:
            icon_lbl.setText("✓")
            icon_lbl.setStyleSheet(f"color:{GREEN};")
            text_lbl.setText("数据上传已关闭，程序不会发出任何网络请求")

    def _privacy_mail_line(self):
        """隐私说明里「会不会把邮箱发出去」那一行，按当前订阅状态生成。

        这行的存在是因为「不收集任何个人信息」那句在能填邮箱之后就不成立了。
        现在的写法把话说全：填了才发，不填就不发。
        """
        if uploader.is_subscribed() and uploader.get_email():
            return (ACCENT, "•",
                    f"已订阅每日邮件：你的邮箱（{uploader.get_email()}）会随"
                    f"使用记录一起发给服务器")
        return (GREEN, "✓", "没有订阅每日邮件，你的邮箱不会发给服务器")

    def _update_privacy_mail_line(self):
        """订阅开关变了之后，把隐私说明里那一行改过来。"""
        pair = self._priv_labels.get("mail")
        if not pair:
            return
        color, icon, text = self._privacy_mail_line()
        icon_lbl, text_lbl = pair
        icon_lbl.setText(icon)
        icon_lbl.setStyleSheet(f"color:{color};")
        text_lbl.setText(text)

    # ── 订阅（每日报告邮件） ──────────────────────────────────

    def _on_subscribe_toggled(self, checked):
        """拨「订阅」开关：只控制输入框能不能填，**不**立刻发请求。

        为什么不立刻保存：光勾上但邮箱还没填是一个中间状态，这时候往服务端
        发一次「订阅但没有邮箱」只会被拒。等用户点「保存」再一起提交。
        """
        self.edit_email.setEnabled(checked)
        self._sync_subscribe_buttons()
        self._refresh_subscribe_hint()
        if checked:
            self.edit_email.setFocus()

    def _sync_subscribe_buttons(self, *_):
        """决定「保存」按钮能不能点。

        勾了订阅当然能点（提交邮箱）；没勾但输入框里有字也能点 ——
        那是在退订（把邮箱留下来，只把 subscribed 关掉）。
        textChanged 会带上新文本当参数，所以这里收 *_ 把它吃掉。
        """
        self.btn_subscribe.setEnabled(
            self.chk_subscribe.isChecked() or bool(self.edit_email.text().strip()))

    def _refresh_subscribe_hint(self):
        """订阅那块下面那行小字，说明当前是什么状态。

        一定要区分「本机记下了」和「服务端知道了」：待同步时用琥珀色 +
        说清什么时候会补上，不能让用户以为已经生效了、然后等一晚上没收到邮件。
        """
        pending = uploader.subscription_pending()
        email = uploader.get_email()
        if uploader.is_subscribed() and email:
            if pending:
                self.lbl_subscribe.setStyleSheet(f"color:{AMBER};")
                self.lbl_subscribe.setText(
                    f"本机记着 {email}，还没同步到服务端 —— 下次上传数据时会自动补上。")
            else:
                self.lbl_subscribe.setStyleSheet(f"color:{GREEN};")
                self.lbl_subscribe.setText(
                    f"当前已订阅：{email}（改完记得点「保存」）")
        elif self.chk_subscribe.isChecked():
            self.lbl_subscribe.setStyleSheet(f"color:{GRAY};")
            self.lbl_subscribe.setText("填好邮箱后点「保存」才会生效。")
        elif pending:
            self.lbl_subscribe.setStyleSheet(f"color:{AMBER};")
            self.lbl_subscribe.setText(
                "已在本机退订，还没同步到服务端 —— 下次上传数据时会自动补上。")
        else:
            self.lbl_subscribe.setStyleSheet(f"color:{MUTED};")
            self.lbl_subscribe.setText("未订阅。服务器不会给你发邮件。")

    def _save_subscription(self):
        """点「保存」（或在输入框里回车）：把邮箱和订阅开关同步给服务端。"""
        email = self.edit_email.text().strip()
        subscribed = self.chk_subscribe.isChecked()

        # 先本地拦一道，省一次没必要的往返。规则和 server/api/schemas.py 里的一致，
        # 所以这里判过的，服务端一定也认（详见 uploader.looks_like_email）。
        if subscribed and not uploader.looks_like_email(email):
            QMessageBox.warning(self, "邮箱",
                                "请填一个有效的邮箱地址，例如 you@example.com")
            self.edit_email.setFocus()
            return

        self.btn_subscribe.setEnabled(False)
        self.btn_subscribe.setText("保存中…")
        self.lbl_subscribe.setStyleSheet(f"color:{GRAY};")
        self.lbl_subscribe.setText("正在保存…")
        self._start_bg(
            "subscribe",
            lambda: uploader.update_subscription(email, subscribed))

    def _refresh_upload_status(self):
        """刷新「最近上传」那一行。"""
        lines = [f"服务器：{config.base_url()}"]
        # 只有真读到 .env 才多这一行。客户端没有日志文件，而 .env 有两个
        # 候选位置（exe 旁边 / %APPDATA%），「改了一份却没生效」几乎总是
        # 改错了那一份 —— 把实际生效的显示出来，这种问题一眼就能看出来。
        source = config.env_file_label()
        if source:
            lines.append(f"配置：{source}")
        upload_lines = uploader.upload_status_lines()
        lines.extend(upload_lines)
        self.lbl_upload_status.setText("\n".join(lines))
        self.lbl_upload_summary.setText("\n".join(upload_lines))
        # 上面是短标签（免得长路径把设置页撑变形），完整路径挂在这里
        self.lbl_upload_status.setToolTip(config.ENV_FILE or "")

    def _start_bg(self, name, fn):
        """把 fn 丢到后台线程里跑，结果回到 _on_bg_done。

        上传和探活都要走网络，最坏情况会等十几秒（超时 × 重试），直接在
        按钮的槽函数里调用会把界面冻住，所以一律走后台。
        """
        threading.Thread(target=self._worker.run, args=(name, fn),
                         daemon=True).start()

    def _on_bg_done(self, name, result):
        """后台任务完成。**这个方法跑在界面线程里**（Qt 的队列连接保证的），
        所以下面直接改控件是安全的。"""
        if name == "upload":
            self.btn_upload.setEnabled(True)
            self.btn_upload.setText("立即上传")
            self._refresh_upload_status()
            msg = uploader.describe_result(result)
            if result.get("ok"):
                QMessageBox.information(self, "上传", msg)
            else:
                QMessageBox.warning(self, "上传失败", msg)
        elif name == "health":
            self.btn_conn.setEnabled(True)
            self.btn_conn.setText("测试连接")
            ok = result.get("ok")
            self.lbl_conn.setStyleSheet(f"color:{GREEN if ok else RED};")
            self.lbl_conn.setText(result.get("message", ""))
            self.lbl_conn.setVisible(bool(self.lbl_conn.text()))
        elif name == "subscribe":
            self.btn_subscribe.setEnabled(True)
            self.btn_subscribe.setText("保存")
            msg = uploader.describe_subscription(result)
            if result.get("ok"):
                # 服务端收下了才把开关状态往回同步一次：用户可能勾了「订阅」
                # 但服务端拒了（比如还没上传过），这时候本地状态得跟着服务端走，
                # 不然界面显示「已订阅」而服务端一无所知。
                #
                # 同步控件时先屏蔽信号：setChecked 会触发 _on_subscribe_toggled，
                # 而那个槽会把 lbl_subscribe 改成通用提示、还会把焦点抢到输入框，
                # 盖掉下面这句「已订阅：…」的结果。
                self.chk_subscribe.blockSignals(True)
                self.chk_subscribe.setChecked(uploader.is_subscribed())
                self.chk_subscribe.blockSignals(False)
                self.edit_email.blockSignals(True)
                self.edit_email.setText(uploader.get_email() or "")
                self.edit_email.blockSignals(False)
                self.edit_email.setEnabled(self.chk_subscribe.isChecked())
                self._sync_subscribe_buttons()
                # 颜色分三档：本机存下了但服务端还不知道（synced=False）用琥珀色 ——
                # 既不是成功也不是失败，不能涂成绿色让用户以为已经生效了。
                if result.get("synced", True):
                    self.lbl_subscribe.setStyleSheet(f"color:{GREEN};")
                else:
                    self.lbl_subscribe.setStyleSheet(f"color:{AMBER};")
                self.lbl_subscribe.setText(msg)
                self._update_privacy_mail_line()
            else:
                self.lbl_subscribe.setStyleSheet(f"color:{RED};")
                self.lbl_subscribe.setText(msg)

    def _upload_now(self):
        """点「立即上传」：不管有没有新数据，强制传一次。

        force=True 是有意的：自动上传关掉之后，用户仍然应该能手动传一次。
        这个动作本身很轻（几十 KB），所以不做二次确认。
        """
        self.btn_upload.setEnabled(False)
        self.btn_upload.setText("上传中…")
        self.lbl_conn.setText("")   # 清掉上一次的连接测试结果，免得看混
        self.lbl_conn.hide()
        self._start_bg("upload", lambda: uploader.upload_all(force=True))

    def _test_connection(self):
        """点「测试连接」：探一下服务端活着没有（只读，不上传任何数据）。"""
        self.btn_conn.setEnabled(False)
        self.btn_conn.setText("测试中…")
        self.lbl_conn.setStyleSheet(f"color:{GRAY};")
        self.lbl_conn.setText("正在连接…")
        self.lbl_conn.show()
        self._start_bg("health", uploader.fetch_health)

    def _update_idle_preset_style(self, current_secs):
        """把「等于当前设置」的那个快捷按钮涂成主题色，其余恢复灰色。

        参数：
            current_secs：当前生效的无操作秒数，用来判断哪个按钮该高亮。
        """
        for btn in self._idle_presets:
            # 取出建按钮时存进去的秒数（见 _build_settings_page）
            secs = btn.property("idle_secs")
            if secs == current_secs:
                btn.setStyleSheet(
                    f"QPushButton{{background:{ACCENT};color:white;border:none;"
                    f"border-radius:8px;padding:0 14px;font-size:11px;}}"
                )
            else:
                btn.setStyleSheet(
                    f"QPushButton{{background:{CARD};color:{GRAY};border:none;"
                    f"border-radius:8px;padding:0 14px;font-size:11px;}}"
                    f"QPushButton:hover{{background:{HOVER};color:{TEXT};}}"
                )

    def _on_idle_preset(self, btn):
        """点了某个「N 分钟」快捷按钮：把滑块拨到该按钮对应的秒数。

        参数：
            btn：被点击的那个按钮（由 lambda 的默认参数绑好传进来）。
        这里不用自己写保存逻辑——滑块的值一变就会触发 _on_idle_changed，
        由它统一负责显示和存档。
        """
        secs = btn.property("idle_secs")
        self.idle_slider.setValue(secs)

    def _update_clock(self):
        """刷新标题栏上的时钟文字。由 QTimer 每秒调用一次。"""
        now = beijing_now()
        # strftime 是「时间 → 字符串」的格式化函数：%H 时、%M 分、%S 秒
        self.lbl_clock.setText(now.strftime("%H:%M:%S"))

    def _on_idle_changed(self, val):
        """滑块被拖动时调用：把秒数取整到分钟、更新文字、高亮按钮并存档。

        参数：
            val：滑块当前的值（秒），由 valueChanged 信号传进来。
        """
        # 滑块虽然能拖出任意秒数，但界面上只显示分钟，
        # 所以先向下取整到整分钟，再用 max 保证不小于 60 秒
        rounded = max(60, (val // 60) * 60)
        self.lbl_idle_val.setText(f"{rounded // 60} 分钟")
        self._update_idle_preset_style(rounded)
        # 立刻写回设置，用户不用点「保存」（本程序没有保存按钮）
        set_setting("idle_threshold", str(rounded))

    def _on_autostart_toggled(self, checked):
        """勾选框状态变化时调用：真正去开启/关闭开机自启。

        参数：
            checked：现在的勾选状态（True 表示用户刚勾上）。
        """
        try:
            from system.autostart import enable_autostart, disable_autostart
            if checked:
                enable_autostart()
            else:
                disable_autostart()
            set_setting("autostart", "1" if checked else "0")
        except Exception:
            # 写注册表可能被安全软件拦截，这里静默失败：
            # 勾选框仍会显示用户的选择，但设置项没变
            pass

    # ── 标签页切换 ────────────────────────────────────────────

    def _switch_tab(self, tab):
        """切换到指定标签页：换按钮高亮、显示/隐藏对应内容、刷新数据。

        参数：
            tab：标签名，取值 "daily"（今日）/ "weekly"（本周）/ "settings"（设置）。

        做法是「同一批控件按需显示」：统计区只建了一份，
        今日页显示每小时图，本周页显示每日柱状图。
        """
        self._current_tab = tab
        # 每次切页都回到今天，避免上次翻到几天前后再进来看到的是旧日期
        self._view_date = beijing_today()
        # 选中态 / 未选中态两套样式，待会儿按标签名选用
        active_style = (
            f"QPushButton{{background:{ACCENT};color:white;border:none;"
            f"border-radius:8px;padding:0 16px;font-size:13px;font-weight:bold;}}"
        )
        inactive_style = (
            f"QPushButton{{background:transparent;color:{GRAY};border:none;"
            f"border-radius:8px;padding:0 16px;font-size:13px;}}"
            f"QPushButton:hover{{background:{HOVER};}}"
        )

        # 先把三个按钮都复位成未选中，再把选中的那个单独涂上主题色。
        # 这种「全部重置 + 设置当前」的写法比逐个判断省事，也不容易漏掉状态。
        for btn in (self.btn_daily, self.btn_weekly, self.btn_settings):
            btn.setStyleSheet(inactive_style)

        # 标签名 → 对应按钮 的映射表，省掉一串 if/elif
        tab_map = {
            "daily": self.btn_daily,
            "weekly": self.btn_weekly,
            "settings": self.btn_settings,
        }
        tab_map[tab].setStyleSheet(active_style)

        # 按标签名决定每块内容的显示与隐藏
        show_stats = tab in ("daily", "weekly")
        self.stats_container.setVisible(show_stats)          # 设置页不显示统计区
        self.chart_card.setVisible(tab == "weekly")          # 每日柱状图只在「本周」
        self.hourly_card.setVisible(tab == "daily")          # 每小时柱状图只在「今日」
        self.timeline_card.setVisible(tab == "daily")        # 应用时间线只在「今日」
        # 注意显示/隐藏的是外面那层滚动区，不是设置页本身 ——
        # 设置页被套在 QScrollArea 里（见 _build_ui），藏里面那层不管用。
        self.settings_scroll.setVisible(tab == "settings")

        if show_stats:
            # 只有统计页才需要读数据库；每次切页都重新读一遍，数据总是最新的
            self._refresh()
        self._update_pause_button()

    # ── 前后翻页导航 ──────────────────────────────────────────

    def _prev_period(self):
        """点左箭头：往前看一天（今日页）或一周（本周页），然后刷新。"""
        if self._current_tab == "daily":
            # timedelta(days=1) 表示「一天」这个时间跨度，减掉就是前一天。
            # date 对象支持直接做加减法，不用自己算月份和闰年。
            self._view_date -= timedelta(days=1)
        elif self._current_tab == "weekly":
            self._view_date -= timedelta(days=7)
        self._refresh()

    def _next_period(self):
        """点右箭头：往后看一天/一周，但不会越过今天（未来没有数据）。"""
        today = beijing_today()
        if self._current_tab == "daily":
            new_date = self._view_date + timedelta(days=1)
            if new_date <= today:      # 超过今天就原样不动
                self._view_date = new_date
        elif self._current_tab == "weekly":
            new_date = self._view_date + timedelta(days=7)
            if new_date <= today:
                self._view_date = new_date
        self._refresh()

    def _update_arrow_state(self):
        """根据当前查看的日期决定右箭头是否可用，并切换它的配色。

        已经翻到「今天/本周」时右箭头置灰并禁用，防止用户翻到没有数据的未来。
        """
        today = beijing_today()
        is_today = self._view_date >= today
        if self._current_tab == "weekly":
            # 本周页：只要「当前查看的这一周末尾」还没到今天，就还能往后翻
            is_today = self._view_date + timedelta(days=6) >= today
        # setEnabled(False) 之后控件变灰且不再响应点击
        self.btn_next.setEnabled(not is_today)
        if is_today:
            # 禁用外观：文字用更淡的 MUTED 色
            self.btn_next.setStyleSheet(
                f"QPushButton{{background:{CARD};color:{MUTED};border:1px solid {BORDER};"
                f"border-radius:18px;font-size:15px;font-weight:bold;}}"
            )
        else:
            # 可用外观：主题色文字 + 悬停变色
            self.btn_next.setStyleSheet(
                f"QPushButton{{background:{CARD};color:{ACCENT};border:1px solid {BORDER};"
                f"border-radius:18px;font-size:15px;font-weight:bold;}}"
                f"QPushButton:hover{{background:{ACCENT};color:white;border-color:{ACCENT};}}"
            )

    # ── 数据刷新 ──────────────────────────────────────────────

    def _refresh(self):
        """按当前标签页重新读取数据并更新界面（今日和本周走不同的函数）。"""
        if self._current_tab == "daily":
            self._refresh_daily()
        elif self._current_tab == "weekly":
            self._refresh_weekly()
        # 「最近上传」那行是设置页里的，顺手刷新一下：后台调度器可能刚传过
        self._refresh_upload_status()
        # 理论上订阅状态只在设置页里改，但服务端拒过之后本地会被改写，
        # 所以每轮刷新都重新读一次，免得界面显示的和文件里的不一致。
        self._refresh_subscribe_hint()
        self._update_pause_button()

    def _refresh_daily(self):
        """刷新「今日」页：日期文字、总时长、应用数、排行榜、每小时柱状图。"""
        # storage 的接口都收 ISO 格式的日期字符串（"2026-09-22"）
        target = self._view_date.isoformat()
        stats = get_stats(target)             # 各应用当天的统计，已按用时降序
        total = get_daily_total(target)       # 当天总秒数
        hourly = get_hourly_stats(target)     # 24 个小时各自的秒数

        d = self._view_date
        is_today = d == beijing_today()
        # 看的是今天就写「今日」，否则写星期几（_CN_DAY 取值，兜底空串）
        self.lbl_day.setText("今日" if is_today else _CN_DAY.get(d.weekday(), ""))
        self.lbl_date.setText(f"{d.year}年{d.month}月{d.day}日")

        self.lbl_total.setText(format_duration_short(total))
        self.lbl_apps.setText(f"{len(stats)} 个应用")
        self._populate_list(stats, total)     # 重建排行榜（会先清掉旧行）
        self.hourly_chart.set_data(hourly)    # 把数据交给图表，图表自己重绘
        # 时间线：当天所有片段（按应用分行，见 _TimelineView）
        self.timeline_view.set_data(get_fragments_for_date(target))
        self.website_panel.set_range(target, target)
        self._update_arrow_state()

    def _refresh_weekly(self):
        """刷新「本周」页：最近 7 天（含查看日）的总览、排行和每日柱状图。"""
        d = self._view_date
        week_end = d                        # 这一周的最后一天就是正在看的日期
        week_start = d - timedelta(days=6)  # 往前数 6 天，连同今天共 7 天
        week_stats = get_week_stats(week_end.isoformat())       # 逐日汇总（画柱子用）
        app_stats = get_week_app_stats(week_end.isoformat())    # 按应用汇总（排行榜用）
        # 七天相加得到本周总时长。注意这里的 d 是列表里的字典，和上面同名的
        # 变量 d（date 对象）只是恰好重名，作用域上互不影响。
        week_total = sum(d["total_seconds"] for d in week_stats)

        is_this_week = week_end >= beijing_today()
        # 无论是不是本周都显示「本周」：本页展示的始终是「以选中日期结尾的
        # 那七天」，用「本周」比「上周」之类的说法更不容易歧义。
        self.lbl_day.setText("本周" if is_this_week else "本周")
        self.lbl_date.setText(f"{week_start.month}月{week_start.day}日 - {week_end.month}月{week_end.day}日")
        self.lbl_total.setText(format_duration_short(week_total))
        self.lbl_apps.setText(f"{len(app_stats)} 个应用")

        # 柱状图需要额外知道「哪一天是今天」，好把那根柱子标成主题色
        self.bar_chart.set_data(week_stats, week_end.isoformat())
        self._populate_list(app_stats, week_total)
        self.website_panel.set_range(week_start, week_end)
        self._update_arrow_state()

    def _populate_list(self, stats, total):
        """重建「应用使用排行」列表（先清空旧行，再按数据逐行生成）。

        参数：
            stats：应用统计列表，每项至少有 app_name / display_name /
                total_seconds 字段，已经排好序。
            total：用于计算百分比的基准总秒数。
        """
        # 清掉上一次留下的行。列表布局的最后一项是 addStretch() 加的弹簧
        # （见 _build_ui），它要保留，所以条件是 count() > 1 而不是 > 0。
        while self.list_layout.count() > 1:
            item = self.list_layout.takeAt(0)   # 从布局里摘掉第 0 项
            if item.widget():
                # 关键：这里用的是 deleteLater() 而不是立即销毁。
                # 这个函数常常是在「某个控件发出信号的处理过程中」被调用的
                # （比如刚点完某个按钮），如果当场就把控件删掉，Qt 稍后可能
                # 还会访问到这块已释放的内存，导致崩溃。
                # deleteLater() 的含义是「等回到事件循环、当前这轮处理彻底
                # 结束后再销毁」，这样才安全。
                item.widget().deleteLater()

        if not stats:
            # 当天/本周没有任何数据时，显示一句居中的提示
            lbl = QLabel("暂无使用数据")
            lbl.setAlignment(Qt.AlignCenter)
            lbl.setFont(_font(13))
            lbl.setStyleSheet(f"color:{MUTED};padding:40px;")
            # 插到末尾弹簧之前（index 0）
            self.list_layout.insertWidget(0, lbl)
            return

        for i, s in enumerate(stats):
            # 颜色按排名循环取，保证前后两次刷新同一应用的颜色不会乱跳
            color = APP_COLORS[i % len(APP_COLORS)]
            # 百分比：总时长为 0 时直接算 0，避免除零
            pct = (s["total_seconds"] / total * 100) if total > 0 else 0
            row = self._make_row(
                s["app_name"], s["display_name"],
                s["total_seconds"], pct, color, s.get("exe_path", ""))
            # 插到末尾弹簧之前（count()-1 就是弹簧的位置），这样新行依次追加在后
            self.list_layout.insertWidget(self.list_layout.count() - 1, row)

    def _make_row(self, app_name, display_name, seconds, pct, color,
                  exe_path=""):
        """生成排行榜里的一行：图标 + 应用名 + 占比条 + 时长 + 百分比。

        参数：
            app_name：程序内部标识名（取图标时用），一般为 exe 名。
            display_name：给用户看的应用名（可能是中文友好名）。
            seconds：该应用这段时间内的使用秒数。
            pct：占总时长的百分比，取值 0~100。
            color：这一行占比条的颜色（由调用方按排名分配）。
            exe_path：可执行文件完整路径，取图标时会用到；可以是空字符串。
        返回值：组装好的 QFrame，可直接插进列表布局。
        """
        # 整行是一个 QFrame。样式里的 :hover 让鼠标划过时有淡灰高亮，
        # 悬停效果靠样式表实现，不需要写任何鼠标事件代码。
        row = QFrame()
        hover_text = (f"{display_name}\n使用 {format_duration_short(seconds)}\n"
                      f"占总时长 {pct:.1f}%")
        row.setToolTip(hover_text)
        row.setStyleSheet(
            f"QFrame{{background:transparent;border:none;border-radius:8px;}}"
            f"QFrame:hover{{background:{HOVER};}}"
        )
        # 一行从左到右排：用水平布局，内含三个部分（图标 / 名称+条 / 时长+百分比）
        lay = QHBoxLayout(row)
        lay.setContentsMargins(14, 8, 14, 8)
        lay.setSpacing(10)

        # 图标
        icon_lbl = QLabel()
        icon_lbl.setToolTip(hover_text)
        icon_lbl.setFixedSize(28, 28)
        # icons.py 负责画出应用图标（PIL 图片），这里再转成 Qt 能显示的类型
        pil_img = get_icon(
            app_name, display_name, size=28, exe_path=exe_path
        )
        px = _pil_to_pixmap(pil_img, 28)
        icon_lbl.setPixmap(px)
        lay.addWidget(icon_lbl)

        # 中间：上面是应用名，下面是一条细长的占比条
        mid = QVBoxLayout()
        mid.setSpacing(2)
        name_lbl = QLabel(display_name)
        name_lbl.setToolTip(hover_text)
        name_lbl.setFont(_font(12))
        name_lbl.setStyleSheet(f"color:{TEXT};")
        mid.addWidget(name_lbl)

        # 用布局按比例分配轨道宽度，缩放窗口时占比条同步变化。
        bar_bg = QFrame()
        bar_bg.setToolTip(hover_text)
        bar_bg.setFixedHeight(3)
        bar_bg.setStyleSheet(f"background:{CHART_BG};border-radius:1px;")
        bar_fg = QFrame(bar_bg)
        bar_fg.setFixedHeight(3)
        # frac 是 0~1 的比例。下限 0.005 是为了让占比极小的应用也留一点点痕迹，
        # 否则看起来像没画出来。
        frac = max(0.005, pct / 100)
        bar_fg.setStyleSheet(f"background:{color};border-radius:1px;")
        track_layout = QHBoxLayout(bar_bg)
        track_layout.setContentsMargins(0, 0, 0, 0)
        track_layout.setSpacing(0)
        filled = min(1000, max(1, round(frac * 1000)))
        track_layout.addWidget(bar_fg, filled)
        if filled < 1000:
            track_layout.addStretch(1000 - filled)
        mid.addWidget(bar_bg)

        # 第二个参数 1 是伸缩因子：中间部分占据剩余宽度，
        # 图标和右侧的时长就自然被挤到两端
        lay.addLayout(mid, 1)

        # 右侧：上面是时长，下面是百分比
        right = QVBoxLayout()
        right.setSpacing(2)
        time_lbl = QLabel(format_duration_short(seconds))
        time_lbl.setToolTip(hover_text)
        time_lbl.setFont(_font(12, True))
        time_lbl.setStyleSheet(f"color:{TEXT};")
        time_lbl.setAlignment(Qt.AlignRight)
        pct_lbl = QLabel(f"{pct:.1f}%")   # :.1f 表示保留一位小数
        pct_lbl.setToolTip(hover_text)
        pct_lbl.setFont(_font(9))
        pct_lbl.setStyleSheet(f"color:{MUTED};")
        pct_lbl.setAlignment(Qt.AlignRight)
        right.addWidget(time_lbl)
        right.addWidget(pct_lbl)
        lay.addLayout(right)

        return row

    # ── 操作按钮（暂停 / 清空 / 忽略列表）─────────────────────

    def _toggle_pause(self):
        """点「暂停统计 / 继续统计」：把采集器的状态反过来。

        真正的采集逻辑在 monitor.py 里，这里只是调用它的 pause()/resume()。
        """
        # 没有采集器（单独调试界面时）就直接返回，避免报错
        if self.monitor is None:
            return
        if self.monitor.is_paused:
            self.monitor.resume()
        else:
            self.monitor.pause()
        # 状态变了，按钮的文字和配色要跟着更新
        self._update_pause_button()

    def _update_pause_button(self):
        """按采集器当前是否暂停，更新暂停按钮的文字和样式。

        暂停时按钮变成醒目的「继续统计」，未暂停时是低调的「暂停统计」。
        """
        if self.monitor and self.monitor.is_paused:
            self.btn_pause.setText("继续统计")
            self.btn_pause.setStyleSheet(
                f"QPushButton{{background:{ACCENT};color:white;border:none;"
                f"border-radius:8px;padding:0 14px;font-size:12px;}}")
        else:
            self.btn_pause.setText("暂停统计")
            self.btn_pause.setStyleSheet(
                f"QPushButton{{background:{CARD};color:{GRAY};border:none;"
                f"border-radius:8px;padding:0 14px;font-size:12px;}}"
                f"QPushButton:hover{{color:{TEXT};}}")

    def _clear_data(self):
        """点「清空数据」：先弹确认框，用户点「是」才真的删库并刷新。"""
        # QMessageBox.question 会阻塞在这里，直到用户点了某个按钮，
        # 返回值就是被点击的那个按钮。第 4 个参数是「默认选中哪个按钮」，
        # 这里默认 No，避免用户一路回车误删。
        ret = QMessageBox.question(
            self, "确认清空", "确定要清空所有统计数据吗？此操作不可恢复。",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
        if ret == QMessageBox.Yes:
            clear_all_data()
            self._refresh()

    def _export_csv(self):
        """点「导出 CSV」：选日期范围 → 选保存位置 → 写出每日汇总 CSV。

        分三步，任何一步被取消或没有数据就静默返回，不报错、不改动任何数据。
        """
        dates = get_available_dates()
        if not dates:
            QMessageBox.information(self, "导出 CSV", "还没有统计数据，无法导出。")
            return
        # 有数据的日期是从新到旧排的：第一个是最新，最后一个是历史最早
        newest = date.fromisoformat(dates[0])
        oldest = date.fromisoformat(dates[-1])

        # ── 第一步：日期范围选择对话框 ──
        dlg = QDialog(self)
        dlg.setWindowTitle("选择导出日期范围")
        dlg.setMinimumWidth(300)
        lay = QVBoxLayout(dlg)
        lay.setSpacing(12)
        lay.setContentsMargins(20, 18, 20, 18)

        start_edit = QDateEdit(QDate(oldest.year, oldest.month, oldest.day))
        end_edit = QDateEdit(QDate(newest.year, newest.month, newest.day))
        for edit in (start_edit, end_edit):
            edit.setCalendarPopup(True)      # 点开弹日历，而不是手工敲日期
            edit.setDisplayFormat("yyyy-MM-dd")

        def _row(label, edit):
            row = QHBoxLayout()
            lbl = QLabel(label)
            lbl.setFont(_font(12))
            lbl.setFixedWidth(40)
            row.addWidget(lbl)
            row.addWidget(edit, 1)
            return row

        lay.addLayout(_row("从", start_edit))
        lay.addLayout(_row("到", end_edit))

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        ok_btn = QPushButton("确定")
        cancel_btn = QPushButton("取消")
        for b in (ok_btn, cancel_btn):
            b.setFixedHeight(30)
            b.setCursor(Qt.PointingHandCursor)
        ok_btn.setStyleSheet(
            f"QPushButton{{background:{ACCENT};color:white;border:none;"
            f"border-radius:8px;padding:0 18px;font-size:13px;}}")
        cancel_btn.setStyleSheet(
            f"QPushButton{{background:{CARD};color:{GRAY};border:none;"
            f"border-radius:8px;padding:0 18px;font-size:13px;}}")
        ok_btn.clicked.connect(dlg.accept)
        cancel_btn.clicked.connect(dlg.reject)
        btn_row.addWidget(ok_btn)
        btn_row.addWidget(cancel_btn)
        lay.addLayout(btn_row)

        if dlg.exec() != QDialog.Accepted:
            return
        start = start_edit.date().toString("yyyy-MM-dd")
        end = end_edit.date().toString("yyyy-MM-dd")
        if start > end:
            QMessageBox.warning(self, "导出 CSV", "开始日期不能晚于结束日期。")
            return

        # ── 第二步：取数据 ──
        rows = get_summary_range(start, end)
        if not rows:
            QMessageBox.information(self, "导出 CSV", "所选日期范围内没有统计数据。")
            return

        # ── 第三步：选保存位置并写文件 ──
        default_name = f"使用时间统计_{start}_{end}.csv"
        path, _ = QFileDialog.getSaveFileName(
            self, "导出 CSV", default_name, "CSV 文件 (*.csv)")
        if not path:
            return
        try:
            write_summary_csv(path, rows)
        except Exception as e:
            QMessageBox.warning(self, "导出失败", f"写入文件失败：\n{e}")
            return
        QMessageBox.information(
            self, "导出成功", f"已导出 {len(rows)} 行数据到：\n{path}")

    def _show_ignore_dialog(self):
        """点「忽略列表」：弹出一个对话框，逐个应用切换「忽略 / 已忽略」。

        被加入忽略名单的应用，之后的统计里就不会再计入（判定在采集侧完成）。
        这里不缓存对话框，每次点击都新建一个，用完即弃，避免状态残留。
        """
        # QDialog(self) 传了 self，表示它是主窗口的子对话框，
        # 会跟着主窗口一起被销毁；不传会变成独立窗口。
        dlg = QDialog(self)
        dlg.setWindowTitle("忽略列表")
        dlg.setMinimumWidth(320)
        dlg.setStyleSheet(
            f"QDialog{{background:{BG};}}"
            f"QLabel{{color:{TEXT};font-size:13px;}}"
        )

        lay = QVBoxLayout(dlg)
        lay.setSpacing(8)

        lbl = QLabel("勾选的应用将不计入统计：")
        lbl.setFont(_font(13))
        lay.addWidget(lbl)

        # 用「所有出现过的应用」× 「已忽略名单」生成每一行
        all_stats = get_stats()
        ignored = get_ignored_apps()
        for s in all_stats:
            cb = QLabel(s["display_name"])
            cb.setFont(_font(13))
            cb_name = s["app_name"]
            # ignored 里存的是小写名字，所以比对前也统一转小写
            is_ignored = cb_name.lower() in ignored
            toggle_btn = QPushButton("已忽略" if is_ignored else "忽略")
            toggle_btn.setFixedHeight(28)
            # 已忽略的按钮涂成红色，一眼能看出哪些被排除了
            toggle_btn.setStyleSheet(
                f"QPushButton{{background:{RED if is_ignored else CARD};"
                f"color:white;border:none;border-radius:6px;"
                f"padding:0 12px;font-size:12px;}}")
            # 一行：应用名 + 弹簧 + 切换按钮
            row = QHBoxLayout()
            row.addWidget(cb)
            row.addStretch()
            row.addWidget(toggle_btn)

            # 又是一个闭包陷阱的解法，只不过这次用的是「工厂函数」而不是默认参数：
            # make_toggle 每轮被调用时，name / btn / is_ign 都是当轮的实参，
            # 于是返回的那个 toggle 函数就把当轮的值「关」在了自己的作用域里。
            # 如果不用工厂函数、直接在循环里定义 toggle，那么所有按钮回调
            # 看到的都会是循环结束后的最后一个应用。
            def make_toggle(name, btn, is_ign):
                def toggle():
                    if is_ign:
                        # 当前是「已忽略」→ 取消忽略，按钮恢复成灰色「忽略」
                        remove_ignored_app(name)
                        btn.setText("忽略")
                        btn.setStyleSheet(
                            f"QPushButton{{background:{CARD};color:{TEXT};"
                            f"border:none;border-radius:6px;"
                            f"padding:0 12px;font-size:12px;}}")
                    else:
                        # 当前是「忽略」→ 加入忽略名单，按钮变成红色「已忽略」
                        add_ignored_app(name)
                        btn.setText("已忽略")
                        btn.setStyleSheet(
                            f"QPushButton{{background:{RED};color:white;"
                            f"border:none;border-radius:6px;"
                            f"padding:0 12px;font-size:12px;}}")
                return toggle

            # 把「写操作 + 改按钮外观」都放在回调里：点一下马上生效，无需保存按钮
            toggle_btn.clicked.connect(make_toggle(cb_name, toggle_btn, is_ignored))
            lay.addLayout(row)

        btn_box = QPushButton("关闭")
        btn_box.setStyleSheet(
            f"QPushButton{{background:{CARD};color:{GRAY};border:none;"
            f"border-radius:8px;padding:8px 24px;font-size:12px;}}"
            f"QPushButton:hover{{color:{TEXT};}}")
        # dlg.close 是 QDialog 自带的无参方法，可以直接当槽函数用
        btn_box.clicked.connect(dlg.close)
        # alignment 让按钮在布局里居中（而不是被拉伸到整行宽）
        lay.addWidget(btn_box, alignment=Qt.AlignCenter)

        # exec() 以「模态」方式弹出对话框：它会阻塞在这里，直到对话框被关闭，
        # 期间主窗口不能操作。这样调用方（_show_ignore_dialog）后面的代码
        # 自然就在对话框关闭之后才执行。
        dlg.exec()
