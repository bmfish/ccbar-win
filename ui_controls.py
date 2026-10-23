"""设置页用的自绘控件：胶囊开关、主题色卡。

tkinter 原生的 Checkbutton 是方框 + 勾，和暗色界面不搭；这里用 Canvas
画一个 iOS 风格的胶囊开关。主题色卡让用户直接点色块选主题，
不用去下拉菜单里找名字。
"""
import tkinter as tk

from ui_kit import Design, ChartCanvas, blend


class ToggleSwitch(tk.Canvas):
    """独立的胶囊开关（不带文案），给数据源那种"开关 + 名称 + 路径"的行用。"""

    W = 38
    H = 20

    def __init__(self, master=None, var=None, bg=None, **kwargs):
        bg = bg or Design.BACKGROUND
        super().__init__(master, width=self.W, height=self.H, bg=bg,
                         highlightthickness=0, cursor="hand2", **kwargs)
        self.var = var if var is not None else tk.BooleanVar(value=False)
        self.bind("<Button-1>", self._toggle)
        try:
            self.var.trace_add("write", lambda *args: self._draw())
        except Exception:
            pass
        self._draw()

    def _toggle(self, _event=None):
        try:
            self.var.set(not bool(self.var.get()))
        except Exception:
            pass

    def _draw(self):
        try:
            self.delete("all")
            on = bool(self.var.get())
        except Exception:
            return
        track = Design.BRAND if on else Design.CARD_BORDER
        ChartCanvas.round_rect(self, 0, 2, self.W, self.H - 2,
                               (self.H - 4) / 2, fill=track)
        knob = self.H - 6
        x1 = self.W - knob - 3 if on else 3
        self.create_oval(x1, 3, x1 + knob, 3 + knob, fill="#FFFFFF", outline="")


class ToggleRow(tk.Frame):
    """一行开关：左边文案，右边胶囊开关；点文案或开关都能切换。

    直接读写传入的 BooleanVar，调用方保存逻辑不用改。
    """

    def __init__(self, master=None, text="", var=None, bg=None, **kwargs):
        bg = bg or Design.BACKGROUND
        super().__init__(master, bg=bg, **kwargs)
        self.var = var if var is not None else tk.BooleanVar(value=False)
        self._bg = bg
        self._text = text

        self.label = tk.Label(self, text=text, fg=Design.TEXT_PRIMARY, bg=bg,
                              font=Design.FONT_UI, anchor='w')
        self.label.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.switch = ToggleSwitch(self, var=self.var, bg=bg)
        self.switch.pack(side=tk.RIGHT)
        self.label.bind("<Button-1>", lambda e: self.switch._toggle())


class ThemeSwatches(tk.Frame):
    """主题色卡网格：每张小卡用主题强调色渲染，点一下即切换。"""

    TILE_W = 54
    TILE_H = 40
    GAP = 8

    def __init__(self, master=None, themes=(), on_pick=None, bg=None, **kwargs):
        bg = bg or Design.BACKGROUND
        super().__init__(master, bg=bg, **kwargs)
        self._on_pick = on_pick
        self._selected = None
        self._tiles = []      # [(name, canvas, accent)]
        for name, accent in themes:
            tile = tk.Canvas(self, width=self.TILE_W, height=self.TILE_H, bg=bg,
                             highlightthickness=0, cursor="hand2")
            tile.pack(side=tk.LEFT, padx=(0, self.GAP))
            tile.bind("<Button-1>", lambda e, n=name: self._pick(n))
            self._tiles.append((name, tile, accent))
        self._draw()

    def _pick(self, name):
        self.set_selected(name)
        if self._on_pick:
            self._on_pick(name)

    def set_selected(self, name):
        self._selected = name
        self._draw()

    def _draw(self):
        for name, tile, accent in self._tiles:
            tile.delete("all")
            active = name == self._selected
            fill = blend(accent, Design.BACKGROUND, 0.18)
            border = accent if active else Design.CARD_BORDER
            ChartCanvas.round_rect(tile, 0, 0, self.TILE_W, self.TILE_H, 8, fill=border)
            ChartCanvas.round_rect(tile, 1, 1, self.TILE_W - 1, self.TILE_H - 1, 7, fill=fill)
            # 中间一个强调色圆点，选中时右侧加一个小勾
            tile.create_oval(10, 15, 24, 29, fill=accent, outline="")
            tile.create_text(30, 22, text="✓" if active else "",
                             fill=Design.TEXT_PRIMARY, font=Design.FONT_UI_SMALL)
