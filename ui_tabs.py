"""胶囊分段页签 / 左侧竖向导航：替代 ttk.Notebook 的观感，保留 Notebook 的常用 API。

洞察中心原来用 ttk.Notebook，页签是方角方块；这里自绘一排胶囊，
选中项用品牌色填充。只实现窗口代码实际用到的那部分 API
（add / tabs / index / select / tab / nametowidget / <<NotebookTabChanged>>），
测试里的页签遍历也因此不用改写法。

vertical=True 时切换成原型图的左侧导航栏：Logo + 图标 + 文字，选中项左侧
有品牌色指示条；内容页仍然从 nb 里 pack 出来，对外 API 完全一致。
"""
import tkinter as tk
import tkinter.font as tkfont

from ui_kit import Design, ChartCanvas, blend


class PillTabs(tk.Frame):
    """横向胶囊页签；vertical=True 时是左侧竖向导航。"""

    HEIGHT = 32
    GAP = 6
    PAD_X = 14

    RAIL_W = 96
    ITEM_H = 52
    LOGO_TOP = 10
    ITEMS_TOP = 58
    ICON_FONT = ("Segoe UI Symbol", 15)
    LOGO_GLYPH = "⚡"

    def __init__(self, master=None, vertical=False, **kwargs):
        kwargs.setdefault("bg", Design.BACKGROUND)
        super().__init__(master, **kwargs)
        self.vertical = vertical
        self._frames = []        # [[tab_id, frame, title, icon], ...]
        self._selected = None
        self._hover = -1
        self._handler = None
        self._hit = []           # 横向: (x1,x2,i)；纵向: (y1,y2,i)
        if vertical:
            self._bar = tk.Canvas(self, width=self.RAIL_W, bg=Design.BACKGROUND,
                                  highlightthickness=0)
            self._bar.pack(side=tk.LEFT, fill=tk.Y, padx=(10, 6), pady=(6, 10))
        else:
            self._bar = tk.Canvas(self, height=self.HEIGHT + 12,
                                  bg=Design.BACKGROUND, highlightthickness=0)
            self._bar.pack(fill=tk.X, padx=2, pady=(0, 8))
        self._bar.bind("<Configure>", lambda e: self._draw())
        self._bar.bind("<Button-1>", self._on_click)
        self._bar.bind("<Motion>", self._on_motion)
        self._bar.bind("<Leave>", lambda e: self._set_hover(-1))

    # ---------------------------------------------------------- Notebook 兼容 API

    def add(self, frame, text="", icon=""):
        tab_id = "tab%d" % len(self._frames)
        self._frames.append([tab_id, frame, text, icon])
        frame.pack_forget()
        return tab_id

    def tabs(self):
        return tuple(f[0] for f in self._frames)

    def index(self, tab_id):
        if isinstance(tab_id, int):
            if 0 <= tab_id < len(self._frames):
                return tab_id
            raise tk.TclError("tab index out of range")
        for i, item in enumerate(self._frames):
            if item[0] == tab_id:
                return i
        raise tk.TclError("bad tab id %r" % (tab_id,))

    def select(self, tab=None):
        if tab is None:
            return self._selected
        idx = self.index(tab)
        self._selected = self._frames[idx][0]
        for i, (_tab_id, frame, _title, _icon) in enumerate(self._frames):
            if i == idx:
                frame.pack(fill=tk.BOTH, expand=True)
            else:
                frame.pack_forget()
        self._draw()
        if self._handler is not None:
            try:
                self._handler()
            except Exception:
                pass
        return self._selected

    def tab(self, tab, option=None):
        title = self._frames[self.index(tab)][2]
        if option in (None, "text"):
            return title
        raise tk.TclError("unsupported tab option %r" % (option,))

    def nametowidget(self, tab_id):
        return self._frames[self.index(tab_id)][1]

    def bind(self, sequence=None, func=None, add=None):
        # 窗口代码用虚拟事件名挂"切页后记住页签"的回调
        if sequence == "<<NotebookTabChanged>>":
            self._handler = func
            return None
        return super().bind(sequence, func, add)

    # ---------------------------------------------------------- 绘制

    def _draw(self):
        if self.vertical:
            self._draw_vertical()
        else:
            self._draw_horizontal()

    def _draw_horizontal(self):
        bar = self._bar
        bar.delete("all")
        self._hit = []
        if not self._frames:
            return
        width = bar.winfo_width()
        if width <= 1:
            return
        font = Design.FONT_UI_SMALL
        try:
            measure = tkfont.Font(root=self, font=font)
        except Exception:
            measure = None
        h = self.HEIGHT
        y1 = (int(bar.cget("height")) - h) / 2
        x = 2
        for i, (tab_id, _frame, title, _icon) in enumerate(self._frames):
            text_w = measure.measure(title) if measure is not None else len(title) * 9
            w = text_w + self.PAD_X * 2
            active = tab_id == self._selected
            if active:
                fill, fg = Design.BRAND, Design.TEXT_PRIMARY
            elif i == self._hover:
                fill, fg = Design.BTN_BG_HOVER, Design.TEXT_PRIMARY
            else:
                fill, fg = Design.CARD_FILL, Design.TEXT_SECONDARY
            ChartCanvas.round_rect(bar, x, y1, x + w, y1 + h, h / 2, fill=fill)
            bar.create_text(x + w / 2, y1 + h / 2, text=title, fill=fg, font=font)
            self._hit.append((x, x + w, i))
            x += w + self.GAP

    def _draw_vertical(self):
        bar = self._bar
        bar.delete("all")
        self._hit = []
        width = bar.winfo_width()
        if width <= 1:
            width = self.RAIL_W
        # 顶部 Logo：品牌色圆角方块 + 闪电
        cx = width / 2
        ChartCanvas.round_rect(bar, cx - 17, self.LOGO_TOP, cx + 17,
                               self.LOGO_TOP + 34, 10, fill=Design.BRAND)
        bar.create_text(cx, self.LOGO_TOP + 17, text=self.LOGO_GLYPH,
                        fill="#FFFFFF", font=("Segoe UI Symbol", 16))
        y = self.ITEMS_TOP
        for i, (tab_id, _frame, title, icon) in enumerate(self._frames):
            active = tab_id == self._selected
            if active:
                ChartCanvas.round_rect(bar, 6, y, width - 6, y + self.ITEM_H, 9,
                                       fill=blend(Design.BRAND, Design.BACKGROUND, 0.16))
                ChartCanvas.round_rect(bar, 0, y + 14, 3, y + self.ITEM_H - 14, 1.5,
                                       fill=Design.BRAND)
                fg = Design.BRAND
            elif i == self._hover:
                ChartCanvas.round_rect(bar, 6, y, width - 6, y + self.ITEM_H, 9,
                                       fill=Design.BTN_BG_HOVER)
                fg = Design.TEXT_PRIMARY
            else:
                fg = Design.TEXT_SECONDARY
            if icon:
                bar.create_text(cx, y + 18, text=icon, fill=fg,
                                font=self.ICON_FONT)
            bar.create_text(cx, y + 38, text=title, fill=fg,
                            font=Design.FONT_UI_SMALL)
            self._hit.append((y, y + self.ITEM_H, i))
            y += self.ITEM_H + 2

    # ---------------------------------------------------------- 交互

    def _index_at(self, pos):
        for a, b, i in self._hit:
            if a <= pos <= b:
                return i
        return -1

    def _on_click(self, event):
        pos = event.y if self.vertical else event.x
        i = self._index_at(pos)
        if i >= 0:
            self.select(i)

    def _set_hover(self, i):
        if i != self._hover:
            self._hover = i
            try:
                self._bar.configure(cursor="hand2" if i >= 0 else "")
            except Exception:
                pass
            self._draw()

    def _on_motion(self, event):
        self._set_hover(self._index_at(event.y if self.vertical else event.x))
