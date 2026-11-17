"""Tkinter desktop screens for the shared ledger workflow."""

from calendar import Calendar
from collections import Counter
from dataclasses import replace
from datetime import date, datetime
import json
from math import isqrt
import sqlite3
import tkinter as tk
from tkinter import simpledialog, ttk
from tkinter.font import Font
from uuid import uuid4

from .accounts import observe_only
from .entries import EDITABLE, DuplicateEntry, EntryInput, allocate_evenly, cents, money
from .queries import EntryFilter
from .storage import BEIJING, RuleError, beijing_datetime, beijing_today


ROLE = {"admin": "管理员", "writer": "记账成员", "reader": "只读成员"}
STATUS = {
    "draft": "草稿", "pending": "待审核", "approved": "已通过",
    "rejected": "被退回", "withdrawn": "已撤回", "voided": "已作废",
}
DEFAULT_ENTRY_FILTER = EntryFilter(status="approved")
ALL_STATUSES = "全部状态"
ENTRY_STATUS_CHOICES = {**STATUS, "reversed": "已冲销"}
ENTRY_TYPE = {"expense": "支出", "income": "收入"}
CATEGORIES = (
    "日用品", "饮食", "零食", "交通", "居住", "水电费", "购物", "娱乐",
    "医疗", "工资", "奖金", "兼职收入", "报销", "退款", "红包礼金", "利息",
    "租金收入", "二手转卖", "奖学金", "其他",
)
INCLUSION = {
    "history": "手动纳入历史未结算项",
    "period": "期间内",
    "paired": "与原账成组",
    "paired_outside_period": "日期超出范围，随原账成组纳入",
}

PAPER = "#faf6eb"
CARD = "#fffdf8"
HIGHLIGHT = "#fff1c8"
GOLD = "#c7a34c"
GOLD_INK = "#785815"
INK = "#403728"
MUTED = "#75674e"
LINE = "#e9ddc2"
GREEN = "#216e57"
NUMERIC = "#203046"
ACCENT_FILL = "#eef4ee"
ACCENT_LINE = "#c8ddcf"
FAINT = "#b6a88f"
FONT = "Microsoft YaHei UI"
PAYMENT_NOTICE = "注意，真实付款情况未记录"
AUDIT_TIMESTAMPS = {"approved_at", "created_at", "expires_at", "joined_at", "left_at",
                    "occurred_at", "used_at"}
ENTRY_ACTIONS = {
    "draft": "保存草稿", "submit": "提交审核", "edit": "编辑账目", "withdraw": "撤回",
    "confirm_duplicate": "确认提交相似账目",
    "void": "作废", "approved": "审核通过", "rejected": "审核退回",
    "request_reversal": "申请冲销",
}
MEMBER_ACTIONS = {"join": "加入账本", "leave": "退出账本", "remove": "移除成员", "role": "调整角色"}
AUDIT_ACTIONS = {
    **ENTRY_ACTIONS, **MEMBER_ACTIONS, "create_ledger": "创建账本", "invite": "生成邀请",
    "dissolve": "解散账本", "confirm_offline": "确认线下处理",
    "budget": "设置预算", "settle": "保存结算",
}


def display_time(timestamp):
    if not timestamp:
        return ""
    return beijing_datetime(timestamp).strftime("%Y-%m-%d %H:%M:%S")


def menu_state(enabled):
    return "normal" if enabled else "disabled"


def rounded_surface(root, fill, *, radius, outline=None):
    size = radius * 2 + 3
    surface = tk.PhotoImage(master=root, width=size, height=size)

    def paint(color, inset):
        corner = radius - inset
        for row in range(inset, size - inset):
            distance = max(radius - row, row - (size - radius - 1), 0)
            edge = radius - isqrt(corner * corner - distance * distance)
            surface.put(color, to=(edge, row, size - edge, row + 1))

    if outline:
        paint(outline, 0)
    paint(fill, 1 if outline else 0)
    return surface


def apply_button_styles(root, style):
    # A nine-slice image keeps the corners fixed as ttk stretches the button.
    images = []

    def background(name, colors, radius, outline=None):
        surfaces = [
            rounded_surface(root, color, radius=radius, outline=outline)
            for color in colors
        ]
        images.extend(surfaces)
        normal, hover, pressed, disabled = surfaces
        style.element_create(
            name, "image", normal, ("disabled", disabled), ("pressed", pressed),
            ("active", hover), border=radius + 1, padding=0, sticky="nsew",
        )

    background("RoundButton.surface", (CARD, HIGHLIGHT, "#f3e6c4", "#f1ece1"), 12, LINE)
    background("GoldButton.surface", (GOLD, "#d6b863", "#b79037", "#e4d6b7"), 16)
    content = style.layout("TButton")[0][1]["children"]
    for name, element in (
        ("TButton", "RoundButton.surface"),
        ("Primary.TButton", "GoldButton.surface"),
    ):
        style.layout(name, [(element, {"sticky": "nsew"}), *content])
    style.configure("TButton", padding=(13, 7))
    style.configure("Primary.TButton", foreground="#292013")
    style.map("TButton", foreground=[("disabled", MUTED)])

    selected = rounded_surface(root, HIGHLIGHT, radius=13)
    hover = rounded_surface(root, CARD, radius=13)
    transparent = tk.PhotoImage(master=root, width=29, height=29)
    images.extend((selected, hover, transparent))
    style.element_create(
        "RoundNav.surface", "image", transparent, ("selected", selected), ("active", hover),
        border=14, padding=0, sticky="nsew",
    )
    style.configure("Nav.TRadiobutton", padding=(12, 10))
    style.layout("Nav.TRadiobutton", [
        ("RoundNav.surface", {"sticky": "nsew"}),
        ("Radiobutton.padding", {"sticky": "nsew", "children": [
            ("Radiobutton.label", {"sticky": "w"})
        ]})
    ])
    style.map("Nav.TRadiobutton", foreground=[("selected", GOLD_INK)])
    # Tcl's image elements do not retain the Python image objects.
    root.button_surfaces = images


def apply_theme(root):
    style = ttk.Style(root)
    style.theme_use("clam")
    root.configure(background=PAPER)
    root.option_add("*Toplevel.background", PAPER)
    root.option_add("*Frame.background", PAPER)
    root.option_add("*Font", (FONT, 10))
    style.configure(".", font=(FONT, 10), background=PAPER, foreground=INK)
    style.configure("TFrame", background=PAPER)
    style.configure("Card.TFrame", background=CARD)
    style.configure("Summary.TFrame", background=HIGHLIGHT)
    style.configure("Title.TLabel", font=(FONT, 24, "bold"))
    style.configure("Section.TLabel", font=(FONT, 14, "bold"))
    style.configure("Subtle.TLabel", foreground=MUTED)
    style.configure("Card.TLabel", background=CARD)
    style.configure("CardTitle.TLabel", background=CARD, font=(FONT, 14, "bold"))
    style.configure("Notice.TLabel", background="#ffe4b0", foreground="#643b12",
                    font=(FONT, 14, "bold"), padding=(12, 8))
    style.configure("Summary.TLabel", background=HIGHLIGHT, foreground=MUTED)
    style.configure("Amount.TLabel", background=HIGHLIGHT, font=(FONT, 20, "bold"))
    apply_button_styles(root, style)
    for widget in ("TEntry", "TCombobox"):
        style.configure(widget, padding=6, fieldbackground=CARD, bordercolor=LINE)
    style.map("TCombobox", fieldbackground=[("readonly", CARD)],
              foreground=[("disabled", MUTED), ("readonly", INK)],
              selectbackground=[("readonly", CARD)], selectforeground=[("readonly", INK)])
    style.configure("TLabelframe", background=CARD, bordercolor=LINE)
    style.configure("TLabelframe.Label", background=CARD, font=(FONT, 10, "bold"))
    style.configure("Gold.Horizontal.TProgressbar", background=GOLD, troughcolor="#f0e7d2",
                    bordercolor=LINE, lightcolor=GOLD, darkcolor=GOLD)


class SummaryCard(ttk.Frame):
    """Rounded totals banner shared by the ledger and statistics screens."""

    def __init__(self, parent):
        super().__init__(parent)
        self.caption = ""
        self.income = 0
        self.expense = 0
        self.padding = 14
        self.gap = 8
        self.caption_font = Font(root=self, family=FONT, size=10)
        self.amount_font = Font(root=self, family=FONT, size=16, weight="bold")
        self.caption_line = self.caption_font.metrics("linespace")
        self.amount_line = self.amount_font.metrics("linespace")
        self.height = self.padding * 2 + self.caption_line + self.gap + self.amount_line
        self.canvas = tk.Canvas(self, background=PAPER, highlightthickness=0,
                                height=self.height + 4)
        self.canvas.pack(fill="x")
        self.canvas.bind("<Configure>", self.draw)
        self.pack(fill="x", pady=(0, 10))

    def show_totals(self, caption, totals):
        """Replace the caption and repaint the displayed totals."""
        self.caption = caption
        self.income = totals["income"]
        self.expense = totals["expense"]
        self.draw()

    def draw(self, _event=None):
        """Redraw the summary using the current canvas width."""
        canvas = self.canvas
        canvas.delete("all")
        width = max(120, canvas.winfo_width() - 4)
        rounded_card(canvas, 2, 2, width, self.height + 2, tag="summary",
                     fill=HIGHLIGHT, outline="#ecd9a4")
        top = 2 + self.padding
        caption_y = top + self.caption_line // 2
        amount_y = top + self.caption_line + self.gap + self.amount_line // 2
        column = (width - 44) // 2
        canvas.create_text(22, caption_y, anchor="w", text=self.caption,
                           fill=MUTED, font=self.caption_font)
        for offset, label, color, amount in (
            (0, "收入", GOLD_INK, self.income),
            (column, "支出", INK, self.expense),
        ):
            x = 22 + offset
            canvas.create_text(x, amount_y, anchor="w", text=label,
                               fill=MUTED, font=self.caption_font)
            canvas.create_text(
                x + self.caption_font.measure(label) + 10, amount_y, anchor="w",
                text=f"¥{money(amount)}", fill=color, font=self.amount_font,
            )


def code_for(mapping, label):
    return next((key for key, value in mapping.items() if value == label), "")


def audit_object(event):
    action = event["action"]
    object_id = event["object_id"]
    if action in ENTRY_ACTIONS:
        return "entry", object_id
    if action in MEMBER_ACTIONS:
        return "member", object_id
    if action == "settle":
        return "batch", object_id
    if action == "budget":
        return "budget", json.loads(event["after_content"] or "{}").get("month", "")
    if action in {"create_ledger", "invite", "dissolve", "confirm_offline"}:
        return "ledger", object_id
    return action, object_id


def optional_amount(value):
    return cents(value) if value.strip() else None


def member_labels(rows):
    names = {}
    for row in rows:
        name = row["username"] or "未设置用户名"
        names[row["user_id"]] = f"{name}（尾号 {row['phone'][-4:]}）"
    totals = Counter(names.values())
    occurrences = Counter()
    labels = {}
    for user_id, name in sorted(names.items()):
        occurrences[name] += 1
        labels[user_id] = f"{name} · 同名 {occurrences[name]}" if totals[name] > 1 else name
    return labels


def selected_id(listing, label):
    selected = listing.selection()
    if not selected:
        raise RuleError(f"请先选择{label}")
    return int(selected[0])


def rounded_card(canvas, x, top, right, bottom, *, fill, outline, tag, border_width=1,
                 radius=18):
    return canvas.create_polygon(
        x, top + radius, x, top, x + radius, top,
        right - radius, top, right, top, right, top + radius,
        right, bottom - radius, right, bottom, right - radius, bottom,
        x + radius, bottom, x, bottom, x, bottom - radius,
        smooth=True, splinesteps=16, tags=tag, fill=fill, outline=outline, width=border_width,
    )


class PopupMenu:
    """A rounded context menu drawn in the app's warm card style."""

    transparent = "#ff00fe"
    side = 16
    top_gap = 6
    row_pad = 5
    separator_height = 7
    border = "#ddcaa6"

    def __init__(self, root):
        self.root = root
        self.items = []
        self.window = None
        self.canvas = None
        self.previous_focus = None
        self.previous_grab = None
        self.hovered = None
        self.font = Font(root=root, family=FONT, size=10)
        self.row_height = self.font.metrics("linespace") + self.row_pad * 2
        self.width = 0
        self.height = 0
        self.tops = []
        self.steps = []

    def add_command(self, *, label, command, state="normal"):
        self.items.append(("command", label, command, state != "disabled"))

    def add_separator(self):
        self.items.append(("separator",))

    def close(self, _event=None):
        window, self.window = self.window, None
        if window is None:
            return
        try:
            window.grab_release()
        except tk.TclError:
            pass
        try:
            window.destroy()
        except tk.TclError:
            pass
        previous_grab, self.previous_grab = self.previous_grab, None
        if previous_grab is not None and previous_grab.winfo_exists():
            previous_grab.grab_set()
        if self.previous_focus is not None:
            try:
                self.previous_focus.focus_set()
            except tk.TclError:
                pass
            self.previous_focus = None

    def row_at(self, y):
        for index, (top, step) in enumerate(zip(self.tops, self.steps)):
            if top <= y < top + step:
                return index
        return None

    def show(self, event):
        self.width = max(
            (self.font.measure(item[1]) for item in self.items if item[0] == "command"),
            default=120,
        ) + self.side * 2
        self.width = max(self.width, 168)
        self.tops, self.steps = [], []
        top = self.top_gap
        for item in self.items:
            step = self.separator_height if item[0] == "separator" else self.row_height
            self.tops.append(top)
            self.steps.append(step)
            top += step
        self.height = top + self.top_gap
        window = tk.Toplevel(self.root)
        self.window = window
        self.previous_focus = self.root.focus_get()
        self.previous_grab = self.root.grab_current()
        window.overrideredirect(True)
        window.attributes("-topmost", True)
        background = PAPER
        try:
            window.attributes("-transparentcolor", self.transparent)
            background = self.transparent
        except tk.TclError:
            pass
        window.configure(background=background)
        canvas = tk.Canvas(
            window, width=self.width, height=self.height,
            background=background, highlightthickness=0,
        )
        canvas.pack()
        self.canvas = canvas
        rounded_card(canvas, 1, 1, self.width - 1, self.height - 1, tag="card",
                     fill=CARD, outline=self.border, radius=14)
        for item, top in zip(self.items, self.tops):
            if item[0] == "separator":
                middle = top + self.separator_height // 2
                canvas.create_line(
                    self.side, middle, self.width - self.side, middle,
                    fill=LINE, tags=("label",),
                )
                continue
            _, label, _command, enabled = item
            canvas.create_text(
                self.side, top + self.row_height // 2, anchor="w", text=label,
                font=self.font, tags=("label",), fill=INK if enabled else FAINT,
            )
        x = min(max(4, event.x_root), self.root.winfo_screenwidth() - self.width - 4)
        y = min(max(4, event.y_root), self.root.winfo_screenheight() - self.height - 4)
        window.geometry(f"{self.width}x{self.height}+{x}+{y}")
        window.bind("<Button-1>", self.on_click)
        window.bind("<Button-3>", lambda _event: self.close())
        window.bind("<Motion>", self.on_motion)
        window.bind("<Escape>", self.close)
        window.bind("<FocusOut>", self.close)
        window.lift()
        window.focus_force()
        try:
            window.grab_set()
        except tk.TclError:
            pass

    def on_motion(self, event):
        index = self.row_at(event.y)
        if index == self.hovered:
            return
        self.hovered = index
        self.canvas.delete("hover")
        if index is None:
            return
        item = self.items[index]
        if item[0] != "command" or not item[3]:
            return
        top = self.tops[index]
        rounded_card(
            self.canvas, self.side - 6, top + 1,
            self.width - self.side + 6, top + self.row_height - 1,
            tag="hover", fill=HIGHLIGHT, outline=HIGHLIGHT, radius=10,
        )
        self.canvas.tag_lower("hover", "label")

    def on_click(self, event):
        inside = 0 <= event.x < self.width and 0 <= event.y < self.height
        index = self.row_at(event.y) if inside else None
        item = self.items[index] if index is not None else None
        self.close()
        if item is not None and item[0] == "command" and item[3]:
            self.root.after_idle(item[2])


class SlimScrollbar(tk.Canvas):
    """A slim rounded scrollbar that matches the warm card styling."""

    def __init__(self, parent, command, *, background=PAPER, width=14):
        super().__init__(parent, width=width, height=1, background=background,
                         highlightthickness=0, bd=0, takefocus=0)
        self.command = command
        self.first = 0.0
        self.last = 1.0
        self.thumb = None
        self.top = 0
        self.bottom = 0
        self.offset = 0.0
        self.dragging = False
        self.hover = False
        self.bind("<Configure>", self.draw)
        self.bind("<Enter>", self.enter)
        self.bind("<Leave>", self.leave)
        self.bind("<Button-1>", self.press)
        self.bind("<B1-Motion>", self.drag)
        self.bind("<ButtonRelease-1>", self.release)

    def set(self, first, last):
        first, last = float(first), float(last)
        if (first, last) == (self.first, self.last):
            return
        self.first, self.last = first, last
        self.draw()

    def fraction(self, y):
        return min(1.0, max(0.0, (y - 3) / max(1, self.winfo_height() - 6)))

    def draw(self, _event=None):
        self.delete("all")
        self.thumb = None
        height, width = self.winfo_height(), self.winfo_width()
        if height <= 1 or self.last - self.first >= 0.999:
            return
        top = 3 + (height - 6) * self.first
        bottom = 3 + (height - 6) * self.last
        minimum = 24
        if bottom - top < minimum:
            top = max(3, (top + bottom) / 2 - minimum / 2)
            bottom = min(height - 3, top + minimum)
            top = bottom - minimum
        color = "#c7b184" if self.hover else "#ded1b4"
        radius = max(2, (width - 6) / 2)
        self.thumb = self.create_polygon(
            3, top + radius, 3, top, 3 + radius, top,
            width - 3 - radius, top, width - 3, top, width - 3, top + radius,
            width - 3, bottom - radius, width - 3, bottom, width - 3 - radius, bottom,
            3 + radius, bottom, 3, bottom, 3, bottom - radius,
            smooth=True, splinesteps=12, fill=color, outline=color,
        )
        self.top, self.bottom = top, bottom

    def enter(self, _event):
        self.hover = True
        self.draw()

    def leave(self, _event):
        self.hover = False
        self.draw()

    def press(self, event):
        self.dragging = False
        if self.thumb is None:
            return
        if self.top <= event.y <= self.bottom:
            self.dragging = True
            self.offset = self.fraction(event.y) - self.first
        else:
            self.command("scroll", -1 if event.y < self.top else 1, "pages")

    def drag(self, event):
        if not self.dragging:
            return
        target = self.fraction(event.y) - self.offset
        self.command("moveto", max(0.0, min(target, 1.0 - (self.last - self.first))))

    def release(self, _event):
        self.dragging = False


class EntryCards(ttk.Frame):
    """Scrollable rows shared by the ledger and settlement screens."""

    padding = 12
    gap = 5
    row_gap = 10

    def __init__(self, parent, *, on_select=None, on_open=None, multiple=False,
                 on_menu=None, empty="暂无符合条件的账目"):
        super().__init__(parent)
        self.on_select = on_select
        self.on_open = on_open
        self.multiple = multiple
        self.on_menu = on_menu
        self.empty_text = empty
        self.rows = []
        self.selected = set()
        self.canvas = tk.Canvas(self, background=PAPER, height=1,
                                highlightthickness=0, yscrollincrement=24)
        scroll = SlimScrollbar(self, self.canvas.yview, background=PAPER)
        self.scrollbar = scroll
        scroll.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.configure(yscrollcommand=scroll.set)
        self.canvas.bind("<Configure>", self.draw)
        self.canvas.bind("<MouseWheel>", self.scroll)
        self.canvas.bind("<Button-3>", self.context)
        self.title_font = Font(root=self, family=FONT, size=13, weight="bold")
        self.meta_font = Font(root=self, family=FONT, size=10)
        self.amount_font = Font(root=self, family=FONT, size=15, weight="bold")
        self.head_line = max(self.title_font.metrics("linespace"),
                             self.amount_font.metrics("linespace"))
        self.meta_line = self.meta_font.metrics("linespace")
        self.card_height = self.padding * 2 + self.head_line + 2 * (self.gap + self.meta_line)
        self.row_height = self.card_height + self.row_gap

    def set_rows(self, rows, *, preserve_view=False):
        position = self.canvas.yview()[0]
        self.rows = list(rows)
        if preserve_view:
            self.selected.intersection_update(row[0] for row in self.rows)
        else:
            self.selected.clear()
        self.draw()
        self.canvas.yview_moveto(position if preserve_view else 0)

    def scroll(self, event):
        self.canvas.yview_scroll(-int(event.delta / 120), "units")

    def entry_at(self, event):
        x, y = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        for item in reversed(self.canvas.find_overlapping(x, y, x, y)):
            for tag in self.canvas.gettags(item):
                if tag.startswith("entry_"):
                    return int(tag.split("_", 1)[1])
        return None

    def context(self, event):
        entry_id = self.entry_at(event)
        if entry_id is None:
            return
        if entry_id not in self.selected:
            self.select(entry_id)
        if self.on_menu:
            self.on_menu(event)

    def draw(self, _event=None):
        canvas = self.canvas
        canvas.delete("all")
        right = max(80, canvas.winfo_width() - 8)
        canvas.configure(scrollregion=(0, 0, right, len(self.rows) * self.row_height))
        if not self.rows:
            canvas.create_text(18, 22, text=self.empty_text, anchor="w",
                               fill=MUTED, font=self.meta_font)
        first = self.padding + self.head_line // 2
        second = self.padding + self.head_line + self.gap + self.meta_line // 2
        third = (self.padding + self.head_line + 2 * (self.gap + self.meta_line)
                 - self.meta_line // 2)
        for index, row in enumerate(self.rows):
            entry_id, title, amount, amount_color, meta, people, status, reason = row
            top = index * self.row_height + 5
            tag = f"entry_{entry_id}"
            chosen = entry_id in self.selected
            rounded_card(canvas, 2, top, right, top + self.card_height, tag=tag,
                         fill=HIGHLIGHT if chosen else CARD, outline=GOLD if chosen else LINE)
            amount_width = self.amount_font.measure(amount)
            status_width = self.meta_font.measure(status)
            reason_width = max(0, (right - 52) // 2)
            reason = fit_text(reason, self.meta_font, reason_width) if reason else ""
            meta_width = right - 40 - (self.meta_font.measure(reason) + 16 if reason else 0)
            parts = (
                (20, first, fit_text(title, self.title_font, right - amount_width - 52),
                 "w", self.title_font, INK),
                (right - 20, first, amount, "e", self.amount_font, amount_color),
                (20, second, fit_text(meta, self.meta_font, meta_width), "w",
                 self.meta_font, MUTED),
                (right - 20, second, reason, "e", self.meta_font, "#943b21"),
                (20, third, fit_text(people, self.meta_font, right - status_width - 52),
                 "w", self.meta_font, MUTED),
                (right - 20, third, status, "e", self.meta_font, GOLD_INK),
            )
            for x, y, label, anchor, font, color in parts:
                canvas.create_text(x, top + y, text=label, anchor=anchor,
                                   font=font, fill=color, tags=tag)
            if self.on_select or self.multiple:
                canvas.tag_bind(tag, "<Button-1>", lambda _e, key=entry_id: self.select(key))
            if self.on_open:
                canvas.tag_bind(tag, "<Double-1>", lambda _e, key=entry_id: self.open(key))

    def select(self, entry_id):
        previous = self.selected.copy()
        if self.multiple:
            self.selected.symmetric_difference_update({entry_id})
        else:
            self.selected = {entry_id}
        for key in previous | self.selected:
            surface = self.canvas.find_withtag(f"entry_{key}")[0]
            self.canvas.itemconfigure(surface, fill=HIGHLIGHT if key in self.selected else CARD,
                                      outline=GOLD if key in self.selected else LINE)
        if self.on_select:
            self.on_select()

    def open(self, entry_id):
        if entry_id not in self.selected:
            self.select(entry_id)
        self.on_open()

    def selected_entry_id(self):
        if not self.selected:
            raise RuleError("请先选择账目")
        return next(iter(self.selected))


def fit_text(text, font, width):
    if font.measure(text) <= width:
        return text
    while text and font.measure(text + "…") > width:
        text = text[:-1]
    return text + "…"


class RecordCards(ttk.Frame):
    """Rounded card rows for the batch, member and statistics lists."""

    def __init__(self, parent, *, on_activate=None, on_menu=None, compact=False, background=PAPER,
                 empty="暂无记录", fill=CARD, multiple=False, activate_on_click=False):
        super().__init__(parent)
        self.on_activate = on_activate
        self.activate_on_click = activate_on_click
        self.on_menu = on_menu
        self.empty_text = empty
        self.fill = fill
        self.rows = []
        self.selected = None
        self.multiple = multiple
        self.selected_keys = {}
        self.padding = 9 if compact else 12
        self.gap = 5
        self.canvas = tk.Canvas(
            self, background=background, height=1, highlightthickness=0,
            yscrollincrement=24,
        )
        scroll = SlimScrollbar(self, self.canvas.yview, background=background)
        self.scrollbar = scroll
        scroll.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.configure(yscrollcommand=scroll.set)
        self.canvas.bind("<Configure>", self.draw)
        self.canvas.bind("<MouseWheel>", self.scroll)
        if on_menu:
            self.canvas.bind("<Button-3>", self.open_menu)
        if compact:
            self.title_font = Font(root=self, family=FONT, size=11, weight="bold")
            self.value_font = Font(root=self, family=FONT, size=10, weight="bold")
            self.meta_font = Font(root=self, family=FONT, size=10)
        else:
            self.title_font = Font(root=self, family=FONT, size=13, weight="bold")
            self.value_font = Font(root=self, family=FONT, size=12, weight="bold")
            self.meta_font = Font(root=self, family=FONT, size=10)
        self.head_line = max(self.title_font.metrics("linespace"),
                             self.value_font.metrics("linespace"))
        self.meta_line = self.meta_font.metrics("linespace")
        self.card_height = self.padding * 2 + self.head_line

    def load(self, rows, *, preserve_view=False):
        rows = list(rows)
        if preserve_view and rows == self.rows:
            return
        position = self.canvas.yview()[0]
        selected = self.selected
        self.rows = rows
        self.selected = None
        self.selected_keys.clear()
        if preserve_view and self.get(selected):
            self.selected = selected
        self.draw()
        self.canvas.yview_moveto(position if preserve_view else 0)

    def get(self, key):
        return next((row for row in self.rows if row["key"] == key), None)

    def reveal(self, key):
        heights = [
            self.card_height + (self.gap + self.meta_line if row.get("meta") else 0)
            for row in self.rows
        ]
        total = sum(heights) + 10 * len(heights)
        top = 5
        for row, height in zip(self.rows, heights):
            if row["key"] == key:
                if total > self.canvas.winfo_height():
                    self.canvas.yview_moveto(top / total)
                return
            top += height + 10

    def selection(self):
        if self.multiple:
            return tuple(self.selected_keys)
        return (self.selected,) if self.selected is not None else ()

    def scroll(self, event):
        self.canvas.yview_scroll(-int(event.delta / 120), "units")

    def select(self, key):
        if self.multiple:
            if key in self.selected_keys:
                del self.selected_keys[key]
            else:
                self.selected_keys[key] = None
        elif self.selected == key:
            return
        else:
            self.selected = key
        selected = set(self.selection())
        for position, row in enumerate(self.rows):
            found = self.canvas.find_withtag(f"record_{position}")
            if not found:
                continue
            chosen = row["key"] in selected
            accent = row.get("accent", False)
            self.canvas.itemconfigure(
                found[0],
                fill=HIGHLIGHT if chosen else (ACCENT_FILL if accent else self.fill),
                outline=GOLD if chosen else (ACCENT_LINE if accent else LINE),
            )

    def draw(self, _event=None):
        canvas = self.canvas
        canvas.delete("all")
        width = max(80, canvas.winfo_width() - 8)
        if not self.rows:
            canvas.configure(scrollregion=(0, 0, width, 0))
            canvas.create_text(18, 22, text=self.empty_text, anchor="w",
                               fill=MUTED, font=self.meta_font)
            return
        head = self.padding + self.head_line // 2
        meta = self.padding + self.head_line + self.gap + self.meta_line // 2
        top = 5
        selected = set(self.selection())
        for index, row in enumerate(self.rows):
            note = row.get("meta")
            height = self.card_height + (self.gap + self.meta_line if note else 0)
            tag = f"record_{index}"
            chosen = row["key"] in selected
            accent = row.get("accent", False)
            rounded_card(
                canvas, 2, top, width, top + height, tag=tag,
                fill=HIGHLIGHT if chosen else (ACCENT_FILL if accent else self.fill),
                outline=GOLD if chosen else (ACCENT_LINE if accent else LINE),
            )
            value = row.get("value", "")
            title_width = width - self.value_font.measure(value) - 52
            canvas.create_text(
                20, top + head, anchor="w", tags=tag, font=self.title_font,
                text=fit_text(row["title"], self.title_font, max(40, title_width)),
                fill=GREEN if accent and not chosen else INK,
            )
            if value:
                canvas.create_text(
                    width - 20, top + head, anchor="e", tags=tag,
                    font=self.value_font, text=value,
                    fill=row.get("value_color", GOLD_INK),
                )
            if note:
                canvas.create_text(
                    20, top + meta, anchor="w", tags=tag, font=self.meta_font,
                    text=fit_text(note, self.meta_font, width - 40),
                    fill=MUTED,
                )
            canvas.tag_bind(tag, "<Button-1>", lambda _event, key=row["key"]: self.click(key))
            if self.on_activate and not self.activate_on_click:
                canvas.tag_bind(tag, "<Double-1>", lambda _event: self.on_activate())
            top += height + 10
        canvas.configure(scrollregion=(0, 0, width, top))

    def click(self, key):
        self.select(key)
        if self.activate_on_click and self.on_activate:
            self.on_activate()

    def open_menu(self, event):
        x, y = self.canvas.canvasx(event.x), self.canvas.canvasy(event.y)
        for item in reversed(self.canvas.find_overlapping(x, y, x, y)):
            for tag in self.canvas.gettags(item):
                if tag.startswith("record_"):
                    row = self.rows[int(tag.removeprefix("record_"))]
                    self.select(row["key"])
                    self.on_menu(event, row)
                    return
        self.on_menu(event, None)


def entry_card(row, labels, *, context="", status=None):
    is_income = row["entry_type"] == "income"
    reversal = row["kind"] == "reversal"
    amount = row["amount_cent"]
    positive = is_income != reversal
    value = "" if amount is None else f"{'+' if positive else '−'}¥{money(amount)}"
    meta = [entry_occurrence(row) or "日期未填", ENTRY_TYPE[row["entry_type"]]]
    meta.extend(value for value in (row["category"], context) if value)
    submitter = labels.get(row["submitter_id"], "未提交")
    holder = labels.get(row["fund_holder_id"], "未填写")
    count = row.get("participant_count", len(row.get("allocations", ())))
    people = (
        f"提交：{submitter}   {'代收' if is_income else '垫付'}：{holder}"
        f"   {count} 人{'受益' if is_income else '承担'}"
    )
    reason = ""
    if row["status"] == "rejected" and row.get("reason"):
        reason = "退回原因：" + " ".join(row["reason"].split())
    return (
        row["id"], row["title"] or "未命名账目", value, GREEN if positive else NUMERIC,
        " · ".join(meta), people,
        status or ("已冲销" if row.get("is_reversed") else STATUS[row["status"]]), reason,
    )


def entry_occurrence(row):
    day, minute = row.get("occurred_on"), row.get("occurred_time")
    return f"{day} {minute}" if day and minute else day


class CalendarDialog(simpledialog.Dialog):
    """Pick an ISO date or month independently of existing records."""

    def __init__(self, parent, current, *, month_only, optional, allow_future=False):
        self.current = current
        self.month_only = month_only
        self.optional = optional
        self.today = beijing_today()
        self.latest = date.max if month_only and allow_future else self.today
        selected_date = f"{current}-01" if current and month_only else current
        initial = date.fromisoformat(selected_date) if selected_date else self.today
        initial = min(initial, self.latest)
        self.year, self.month = initial.year, initial.month
        super().__init__(parent, "选择月份" if month_only else "选择日期")

    def body(self, master):
        controls = ttk.Frame(master, padding=(8, 6))
        controls.pack(fill="x")
        self.previous = ttk.Button(controls, text="‹", width=3, command=lambda: self.shift(-1))
        self.previous.pack(side="left")
        self.next = ttk.Button(controls, text="›", width=3, command=lambda: self.shift(1))
        self.next.pack(side="right")
        self.year_choice = ttk.Combobox(controls, state="readonly", width=7)
        self.year_choice.pack(side="left", expand=True)
        self.year_choice.bind("<<ComboboxSelected>>", self.navigate)
        if not self.month_only:
            self.month_choice = ttk.Combobox(controls, state="readonly", width=5)
            self.month_choice.pack(side="left", expand=True)
            self.month_choice.bind("<<ComboboxSelected>>", self.navigate)
        self.days = ttk.Frame(master, padding=8)
        self.days.pack(fill="x")
        self.draw()
        return self.year_choice

    def buttonbox(self):
        box = ttk.Frame(self)
        box.pack(pady=(0, 8))
        ttk.Button(box, text="取消", command=self.cancel).pack(side="left", padx=4)
        if self.optional:
            ttk.Button(box, text="清除", command=lambda: self.choose("")).pack(side="left", padx=4)
        value = self.today.isoformat()
        ttk.Button(
            box, text="本月" if self.month_only else "今天",
            command=lambda: self.choose(value[:7] if self.month_only else value),
        ).pack(side="left", padx=4)
        self.bind("<Escape>", self.cancel)

    def choose(self, value):
        self.result = value
        self.cancel()

    def navigate(self, _event):
        self.year = int(self.year_choice.get().removesuffix("年"))
        if not self.month_only:
            self.month = self.month_choice.current() + 1
        self.draw()

    def shift(self, amount):
        if self.month_only:
            self.year += amount
        else:
            year, month = divmod((self.year - 1) * 12 + self.month - 1 + amount, 12)
            self.year, self.month = year + 1, month + 1
        self.draw()

    def draw(self):
        last_month = self.latest.month if self.year == self.latest.year else 12
        self.month = min(self.month, last_month)
        self.year_choice.configure(values=tuple(
            f"{year}年" for year in range(
                max(1, self.year - 20), min(self.latest.year, self.year + 20) + 1
            )
        ))
        self.year_choice.set(f"{self.year}年")
        first = self.year == 1 and (self.month_only or self.month == 1)
        last = self.year == self.latest.year and (self.month_only or self.month == last_month)
        self.previous.state(["disabled" if first else "!disabled"])
        self.next.state(["disabled" if last else "!disabled"])
        for widget in self.days.winfo_children():
            widget.destroy()
        if self.month_only:
            for month in range(1, last_month + 1):
                row, column = divmod(month - 1, 4)
                self.cell(f"{month}月", f"{self.year:04d}-{month:02d}", row, column)
            return
        self.month_choice.configure(values=tuple(f"{month}月" for month in range(1, last_month + 1)))
        self.month_choice.current(self.month - 1)
        for column, weekday in enumerate("一二三四五六日"):
            ttk.Label(self.days, text=weekday, anchor="center", style="Subtle.TLabel").grid(
                row=0, column=column, sticky="ew"
            )
        for row, week in enumerate(Calendar().monthdayscalendar(self.year, self.month), start=1):
            for column, day in enumerate(week):
                if day:
                    candidate = date(self.year, self.month, day)
                    if candidate <= self.today:
                        self.cell(str(day), candidate.isoformat(), row, column)

    def cell(self, label, value, row, column):
        ttk.Button(
            self.days, text=label, width=5 if self.month_only else 3,
            style="Primary.TButton" if value == self.current else "TButton",
            command=lambda: self.choose(value),
        ).grid(row=row, column=column, padx=2, pady=2, sticky="ew")


class CalendarField(ttk.Button):
    def __init__(self, parent, variable, *, month_only=False, optional=True, allow_future=False):
        self.variable = variable
        self.month_only = month_only
        self.optional = optional
        self.allow_future = allow_future
        super().__init__(parent, command=self.open)
        self.refresh()

    def refresh(self):
        placeholder = "不限日期" if self.optional else "选择月份" if self.month_only else "选择日期"
        self.configure(text=f"{self.variable.get() or placeholder}  ▾")

    def open(self):
        parent = self.winfo_toplevel()
        grab = parent.grab_current()
        try:
            value = CalendarDialog(
                parent, self.variable.get(), month_only=self.month_only, optional=self.optional,
                allow_future=self.allow_future,
            ).result
        finally:
            if grab is not None and grab.winfo_exists():
                grab.grab_set()
        if value is not None:
            self.variable.set(value)
            self.refresh()


class Form(simpledialog.Dialog):
    """A labeled dialog with inline choices and text fields."""

    def __init__(self, parent, title, fields, hint="", *, dates_required=False):
        self.fields = fields
        self.hint = hint
        self.dates_required = dates_required
        self.variables = {}
        self.result = None
        super().__init__(parent, title)

    def body(self, master):
        if self.hint:
            ttk.Label(master, text=self.hint, wraplength=480, justify="left").grid(
                row=0, column=0, columnspan=2, sticky="w", padx=8, pady=(8, 12)
            )
        first = None
        for index, (key, label, default, choices) in enumerate(self.fields, start=1):
            ttk.Label(master, text=label).grid(
                row=index, column=0, padx=8, pady=5, sticky="w"
            )
            value = tk.StringVar(value=default)
            self.variables[key] = value
            if key in {"date", "start", "end"}:
                widget = CalendarField(master, value, optional=not self.dates_required)
            elif choices:
                widget = ttk.Combobox(
                    master, textvariable=value, values=choices, state="readonly", width=36
                )
            else:
                widget = ttk.Entry(
                    master, textvariable=value, width=39,
                    show="*" if key == "password" else "",
                )
            widget.grid(row=index, column=1, padx=8, pady=5, sticky="ew")
            if first is None:
                first = widget
        master.columnconfigure(1, weight=1)
        return first

    def buttonbox(self):
        box = ttk.Frame(self)
        ttk.Button(box, text="取消", width=12, command=self.cancel).pack(side="left", padx=5)
        ttk.Button(box, text="确定", width=12, command=self.ok, style="Primary.TButton").pack(
            side="left", padx=5
        )
        box.pack(pady=10)
        self.bind("<Return>", self.ok)
        self.bind("<Escape>", self.cancel)

    def apply(self):
        self.result = {key: value.get() for key, value in self.variables.items()}


class MessageDialog(simpledialog.Dialog):
    """Show a themed message or ask for confirmation."""

    def __init__(self, parent, title, message, *, confirm=False, confirm_label="确定"):
        self.message = message
        self.confirm = confirm
        self.confirm_label = confirm_label
        previous_grab = parent.grab_current()
        try:
            super().__init__(parent, title)
        finally:
            if previous_grab is not None and previous_grab.winfo_exists():
                previous_grab.grab_set()

    def body(self, master):
        card = ttk.Frame(master, style="Card.TFrame", padding=(16, 12))
        card.pack(fill="x", padx=8, pady=8)
        ttk.Label(
            card, text=self.message, style="Card.TLabel", wraplength=420, justify="left",
        ).pack(anchor="w")

    def buttonbox(self):
        box = ttk.Frame(self)
        if self.confirm:
            ttk.Button(box, text="取消", width=12, command=self.cancel).pack(side="left", padx=5)
        ttk.Button(
            box, text=self.confirm_label, width=12, command=self.ok, style="Primary.TButton"
        ).pack(side="left", padx=5)
        box.pack(pady=(0, 10))
        self.bind("<Return>", self.ok)
        self.bind("<Escape>", self.cancel)

    def apply(self):
        self.result = True


class ShareAmountForm(Form):
    """Collect a nonnegative custom share from the selected participant."""

    def __init__(self, parent, member, share):
        self.amount = None
        super().__init__(
            parent, "填写分配金额", [("share", "分配金额（元）", money(share), ())], member,
        )

    def body(self, master):
        master.configure(background=PAPER)
        first = super().body(master)
        self.error = ttk.Label(master, foreground="#943b21", wraplength=360)
        self.error.grid(row=2, column=0, columnspan=2, sticky="w", padx=8)
        first.selection_range(0, "end")
        return first

    def validate(self):
        try:
            self.amount = cents(self.variables["share"].get())
        except RuleError as error:
            self.error.configure(text=str(error))
            return False
        return True

    def apply(self):
        self.result = self.amount


class AllocationPicker(simpledialog.Dialog):
    """Choose multiple members in click order."""

    def __init__(self, parent, members):
        self.members = members
        super().__init__(parent, "添加成员")

    def body(self, master):
        master.configure(width=360, height=250)
        master.pack_propagate(False)
        ttk.Label(master, text="点击选择多位成员，再点「添加」").pack(anchor="w", padx=8, pady=(8, 0))
        self.cards = RecordCards(master, compact=True, empty="暂无成员", multiple=True)
        self.cards.pack(fill="both", expand=True, padx=8, pady=6)
        self.cards.load([
            {"key": member, "title": label} for member, label in self.members.items()
        ])
        return self.cards.canvas

    def buttonbox(self):
        box = ttk.Frame(self)
        ttk.Button(box, text="取消", width=12, command=self.cancel).pack(side="left", padx=5)
        ttk.Button(
            box, text="添加", width=12, command=self.ok, style="Primary.TButton",
        ).pack(side="left", padx=5)
        box.pack(pady=10)
        self.bind("<Escape>", self.cancel)

    def validate(self):
        if not self.cards.selection():
            MessageDialog(self, "无法添加成员", "请先选择成员")
            return False
        return True

    def apply(self):
        self.result = self.cards.selection()


class EntryForm(Form):
    """Select members in allocation order and preserve amounts on edit."""

    def __init__(self, parent, fields, members, allocations, *, status="draft", title=None,
                 require_time=False):
        self.members = members
        self.ids = {label: member for member, label in members.items()}
        self.status = status
        self.require_time = require_time
        self.entry_result = None
        self.submit_for_review = False
        self.share_menu = None
        self.shares = [
            (row["member_user_id"], row["amount_cent"]) for row in (allocations or ())
        ]
        super().__init__(
            parent, title or ("编辑收支" if allocations is not None else "新增收支"), fields,
            "仅保存会保留当前状态，重新提交审核后才会进入待审核。"
            if status in {"rejected", "withdrawn"} else "",
        )

    def body(self, master):
        first = super().body(master)
        area = ttk.LabelFrame(master, text="分摊 / 收益分配", padding=8, width=296, height=277)
        area.grid(row=1, column=2, rowspan=len(self.fields), sticky="nw", padx=(12, 8), pady=5)
        area.grid_propagate(False)
        self.mode = tk.StringVar(value="自定义" if self.shares else "等分")
        mode = ttk.Combobox(
            area, textvariable=self.mode, values=("等分", "自定义"), state="readonly", width=8,
        )
        mode.grid(row=0, column=0, sticky="w")
        mode.bind("<<ComboboxSelected>>", lambda _event: self.refresh())
        ttk.Button(area, text="添加成员", command=self.add).grid(row=0, column=1, sticky="e")
        self.allocation_cards = RecordCards(
            area, compact=True, background=CARD, empty="暂无成员", fill=HIGHLIGHT,
            on_menu=self.allocation_actions,
        )
        self.allocation_cards.grid(row=1, column=0, columnspan=2, sticky="nsew", pady=5)
        self.feedback = ttk.Label(area, wraplength=260, justify="left", style="Card.TLabel")
        self.feedback.grid(row=2, column=0, columnspan=2, sticky="w", pady=(5, 0))
        area.rowconfigure(1, weight=1)
        area.columnconfigure(1, weight=1)
        self.variables["amount"].trace_add("write", lambda *_args: self.refresh())
        self.refresh()
        return first

    def buttonbox(self):
        box = ttk.Frame(self)
        ttk.Button(box, text="取消", width=12, command=self.cancel).pack(side="left", padx=5)
        save_label = {
            "draft": "保存草稿", "pending": "保存修改",
        }.get(self.status, "仅保存修改")
        ttk.Button(box, text=save_label, width=12, command=lambda: self.save(False)).pack(
            side="left", padx=5
        )
        if self.status != "pending":
            ttk.Button(
                box, text="重新提交审核" if self.status in {"rejected", "withdrawn"} else "提交审核",
                width=14, style="Primary.TButton",
                command=lambda: self.save(True),
            ).pack(side="left", padx=5)
        box.pack(pady=10)
        self.bind("<Escape>", self.cancel)

    def save(self, submit):
        self.submit_for_review = submit
        self.ok()

    def allocation_actions(self, event, row):
        if row is None:
            return
        if self.share_menu is not None:
            self.share_menu.close()
        self.share_menu = PopupMenu(self)
        self.share_menu.add_command(
            label="填写金额", command=self.edit_share, state=menu_state(self.mode.get() == "自定义")
        )
        self.share_menu.add_command(label="移除成员", command=self.remove)
        self.share_menu.show(event)

    def add(self):
        selected = {member for member, _ in self.shares}
        available = {
            member: label for member, label in self.members.items() if member not in selected
        }
        try:
            chosen = AllocationPicker(self, available).result
        finally:
            if self.winfo_exists():
                self.grab_set()
        if chosen:
            self.shares.extend((member, None) for member in chosen)
            self.refresh()

    def remove(self):
        selected = self.allocation_cards.selection()
        if selected:
            removed = selected[0]
            self.shares = [(member, share) for member, share in self.shares if member != removed]
            self.refresh()

    def edit_share(self):
        selected = self.allocation_cards.selection()
        if self.mode.get() != "自定义" or not selected:
            return
        member = selected[0]
        index = next(i for i, (user, _) in enumerate(self.shares) if user == member)
        try:
            share = ShareAmountForm(self, self.members[member], self.shares[index][1]).result
        finally:
            if self.winfo_exists():
                self.grab_set()
        if share is None:
            return
        self.shares[index] = member, share
        self.refresh()

    def allocations(self, amount):
        if not self.shares:
            return ()
        if self.mode.get() == "等分":
            return allocate_evenly(amount, [member for member, _ in self.shares])
        if any(share is None for _, share in self.shares):
            raise RuleError("请右键点击待填写的成员，选择「填写金额」")
        return tuple(self.shares)

    def refresh(self):
        custom = self.mode.get() == "自定义"
        try:
            amount = optional_amount(self.variables["amount"].get())
            allocations = self.allocations(amount)
            shown = dict(allocations)
            total = sum(shown.values())
            hint = f"分配 {money(total)} 元"
            if not custom:
                hint += " · 剩余部分归第一位成员"
            elif amount is not None and amount != total:
                hint += " · 提交前请核对总额"
        except RuleError as error:
            shown = dict(self.shares) if custom else {}
            hint = str(error)
        rows = []
        for member, _ in self.shares:
            label = self.members.get(member, f"成员 {member}")
            share = shown.get(member)
            rows.append({
                "key": member, "title": label,
                "value": f"¥{money(share)}" if share is not None else ("待填写" if custom else ""),
            })
        selected = self.allocation_cards.selection()
        self.allocation_cards.load(rows)
        if selected and self.allocation_cards.get(selected[0]):
            self.allocation_cards.select(selected[0])
            self.allocation_cards.reveal(selected[0])
        self.feedback.configure(text=hint)

    def validate(self):
        values = {key: value.get() for key, value in self.variables.items()}
        submit = self.submit_for_review
        try:
            amount = optional_amount(values["amount"])
            day = values["date"].strip() or None
            minute = values.get("time", "").strip()
            if day and not minute and self.require_time:
                raise RuleError("请填写北京时间，格式为 HH:MM")
            if minute and not day:
                raise RuleError("请先选择账目发生日期")
            if day and minute:
                day = f"{day}T{minute}"
            fields = EntryInput(
                values["title"], values["category"], day, amount,
                code_for(ENTRY_TYPE, values["type"]), self.ids.get(values["holder"]),
                self.allocations(amount), values["notes"],
            ).normalized(submit or self.status != "draft")
        except RuleError as error:
            self.feedback.configure(text=str(error))
            return False
        self.entry_result = fields, submit
        return True

    def apply(self):
        self.result = self.entry_result


class Desktop:
    def __init__(self, root, application):
        self.root = root
        self.app = application
        self.token = None
        self.ledger = None
        self.active_page = 0
        self.selected_entry = None
        self.entry_versions = {}
        self.stats_range = None
        self.filters = DEFAULT_ENTRY_FILTER
        self.ledgers = []
        self.context_menu = None
        self.focus_refresh = None
        self.sync_connection = None
        self.sync_version = None
        self.sync_job = None
        self.live_views = {}
        root.title("多人共享记账")
        root.geometry("1150x740")
        root.minsize(890, 580)
        apply_theme(root)
        root.bind("<FocusIn>", self.schedule_page_refresh, add="+")
        root.bind("<Destroy>", self.on_destroy, add="+")
        self.show_login()

    def on_destroy(self, event):
        if event.widget == self.root:
            self.stop_sync()

    def stop_sync(self):
        if self.sync_job is not None:
            self.root.after_cancel(self.sync_job)
            self.sync_job = None
        if self.sync_connection is not None:
            self.sync_connection.close()
            self.sync_connection = None
        self.sync_version = None

    def start_sync(self):
        if self.sync_job is None:
            self.sync_job = self.root.after(1000, self.poll_changes)

    def input_is_active(self):
        """Detect editable focus and modal grabs before a passive refresh."""
        # Tk can grab a native combobox popdown that has no Python widget.
        if self.root.tk.call("grab", "current", str(self.root)):
            return True
        focus = self.root.tk.call("focus")
        if not focus or str(self.root.tk.call("winfo", "class", focus)) not in {
            "Entry", "TEntry", "Text", "Spinbox", "TSpinbox",
        }:
            return False
        return str(self.root.tk.call(focus, "cget", "-state")) not in {"disabled", "readonly"}

    def poll_changes(self):
        """Refresh visible data after another window commits a change."""
        self.sync_job = None
        if not self.token:
            return
        try:
            self.app.accounts.actor(self.token)
            if self.input_is_active():
                return
            if self.sync_connection is None:
                self.sync_connection = self.app.accounts.database.connect()
                self.sync_connection.execute("PRAGMA busy_timeout = 100")
            version = self.sync_connection.execute("PRAGMA data_version").fetchone()[0]
            if version != self.sync_version:
                with observe_only():
                    self.refresh_current_page()
                    for refresh in tuple(self.live_views.values()):
                        refresh()
                self.sync_version = version
        except RuleError as error:
            if str(error) == "请重新登录":
                self.sign_out()
        except sqlite3.Error:
            # A temporary lock can be retried at the next tick.
            pass
        finally:
            if self.token:
                self.start_sync()

    def schedule_page_refresh(self, _event):
        if self.focus_refresh is None:
            self.focus_refresh = self.root.after_idle(self.refresh_focused_page)

    def refresh_focused_page(self):
        self.focus_refresh = None
        focus = self.root.tk.call("focus")
        if (
            self.token
            and focus and str(self.root.tk.call("winfo", "toplevel", focus)) == str(self.root)
            and not self.input_is_active()
        ):
            try:
                with observe_only():
                    self.refresh_current_page()
            except RuleError as error:
                if str(error) == "请重新登录":
                    self.sign_out()
            except sqlite3.Error:
                # Explicit refresh still reports errors through the normal action handler.
                pass

    def refresh_current_page(self):
        """Refresh the selected page and reflect membership changes."""
        ledgers = self.app.ledgers.list(self.token)
        if ledgers != self.ledgers:
            ledger = self.ledger
            previous = next((row for row in self.ledgers if row["id"] == ledger), None)
            current = next((row for row in ledgers if row["id"] == ledger), None)
            if previous != current or ledger is None:
                self.reload_ledgers(
                    ledger, pick_ledger=current is None, ledgers=ledgers, preserve_view=True
                )
                return
            self.update_ledger_choices(ledgers)
            self.ledger_choice.current(next(
                i for i, row in enumerate(ledgers) if row["id"] == ledger
            ))
        if self.ledger is not None:
            refresh = (
                self.refresh_entries, self.refresh_statistics, self.refresh_batches,
                self.refresh_members,
            )[self.active_page]
            refresh(preserve_view=True)

    def attempt(self, action):
        def run(*args):
            try:
                if self.token:
                    self.app.accounts.actor(self.token)
                return action(*args)
            except RuleError as error:
                MessageDialog(self.root, "操作未完成", str(error))
                if str(error) == "请重新登录":
                    self.sign_out()
            except sqlite3.Error as error:
                MessageDialog(self.root, "数据库操作失败", str(error))
            return None
        return run

    def form(self, title, fields, hint="", *, dates_required=False):
        return Form(self.root, title, fields, hint, dates_required=dates_required).result

    def text_view(self, title, text, *, notice=None, reload_text=None):
        window = tk.Toplevel(self.root)
        window.title(title)
        window.geometry("750x510")
        panel = ttk.Frame(window, padding=14)
        panel.pack(fill="both", expand=True)
        if notice:
            ttk.Label(panel, text=notice, style="Notice.TLabel").pack(fill="x", pady=(0, 10))
        view = tk.Text(panel, wrap="word", font=(FONT, 11), background=CARD, foreground=INK,
                       padx=16, pady=12, relief="flat", spacing3=4)
        scroll = SlimScrollbar(panel, view.yview, background=PAPER)
        view.configure(yscrollcommand=scroll.set)
        view.insert("1.0", text)
        view.configure(state="disabled")
        scroll.pack(side="right", fill="y")
        view.pack(side="left", fill="both", expand=True)
        window.bind("<Escape>", lambda _event: window.destroy())
        if reload_text is not None:
            def refresh(_event=None):
                if not window.winfo_exists():
                    return
                if self.input_is_active():
                    return
                try:
                    with observe_only():
                        latest = reload_text()
                except RuleError:
                    window.destroy()
                    return
                except sqlite3.Error:
                    return
                if latest != view.get("1.0", "end-1c"):
                    position = view.yview()[0]
                    view.configure(state="normal")
                    view.delete("1.0", "end")
                    view.insert("1.0", latest)
                    view.configure(state="disabled")
                    view.yview_moveto(position)

            window.bind("<FocusIn>", refresh, add="+")
            self.live_views[window] = refresh
            window.bind("<Destroy>", lambda event: (
                self.live_views.pop(window, None) if event.widget == window else None
            ), add="+")

    def clear(self):
        for child in self.root.winfo_children():
            child.destroy()

    def show_login(self):
        self.clear()
        panel = ttk.Frame(self.root)
        panel.place(relx=0.5, rely=0.46, anchor="center")
        ttk.Label(panel, text="一起记", style="Title.TLabel").pack(anchor="w")
        ttk.Label(panel, text="多人共享记账", style="Subtle.TLabel").pack(
            anchor="w", pady=(2, 16)
        )
        frame = ttk.Frame(panel, style="Card.TFrame", padding=(26, 22))
        frame.pack(fill="x")
        ttk.Label(frame, text="手机号登录", style="CardTitle.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 6)
        )
        ttk.Label(
            frame,
            text="注册手机号：11 位，不含空格\n密码：至少 8 位，含大小写字母和数字",
            style="Card.TLabel", foreground=MUTED, wraplength=700, justify="left",
        ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 8))
        phone = tk.StringVar()
        username = tk.StringVar()
        password = tk.StringVar()
        fields = [
            ("手机号", phone, ""),
            ("用户名（注册时必填）", username, ""),
            ("密码", password, "*"),
        ]
        inputs = []
        for row, (label, variable, mask) in enumerate(fields, start=2):
            ttk.Label(frame, text=label, style="Card.TLabel").grid(
                row=row, column=0, sticky="w", pady=7
            )
            entry = ttk.Entry(frame, textvariable=variable, width=28, show=mask)
            entry.grid(row=row, column=1, padx=(14, 0), pady=7)
            inputs.append(entry)

        def login():
            self.token = self.app.accounts.login(phone.get(), password.get())
            self.show_workspace(pick_ledger=True)

        def register():
            if not username.get().strip():
                raise RuleError("请输入用户名")
            self.app.accounts.register(phone.get(), password.get(), username.get())
            password.set("")
            MessageDialog(self.root, "注册成功", "请使用刚注册的账号登录。")
            inputs[-1].focus_set()

        actions = ttk.Frame(frame, style="Card.TFrame")
        actions.grid(row=len(fields) + 2, column=0, columnspan=2, sticky="e", pady=12)
        submit_login = self.attempt(login)
        ttk.Button(actions, text="注册账号", command=self.attempt(register)).pack(side="left")
        ttk.Button(
            actions, text="登录", width=12, style="Primary.TButton", command=submit_login,
        ).pack(side="left", padx=(8, 0))
        inputs[-1].bind("<Return>", lambda _event: submit_login())
        inputs[0].focus_set()

    def sign_out(self):
        self.stop_sync()
        self.app.accounts.logout(self.token)
        self.token = self.ledger = self.selected_entry = None
        self.active_page = 0
        self.filters = DEFAULT_ENTRY_FILTER
        self.show_login()

    def show_workspace(self, *, pick_ledger=False):
        self.clear()
        shell = ttk.Frame(self.root, padding=(22, 16))
        shell.pack(fill="both", expand=True)
        header = ttk.Frame(shell)
        header.pack(fill="x")
        ttk.Label(header, text="一起记", style="Title.TLabel").pack(side="left")
        self.back_to_ledgers = ttk.Button(
            header, text="‹ 返回账本列表",
            command=self.attempt(lambda: self.reload_ledgers(self.ledger, pick_ledger=True)),
        )
        self.ledger_choice = ttk.Combobox(header, state="readonly", width=24)
        self.ledger_choice.bind(
            "<<ComboboxSelected>>", lambda _event: self.attempt(self.switch_ledger)()
        )
        self.header_actions = ttk.Frame(shell)
        self.header_actions.pack(fill="x", pady=(8, 14))
        for title, command in (
            ("创建账本", self.create_ledger), ("接受邀请", self.accept_invite),
            ("设置用户名", self.change_username),
        ):
            ttk.Button(self.header_actions, text=title, command=self.attempt(command)).pack(
                side="left", padx=4
            )
        self.ledger_buttons = {}
        for title, command in (
            ("生成邀请", self.invite), ("查看日志", self.audit),
            ("解散账本", self.dissolve_ledger),
        ):
            self.ledger_buttons[title] = ttk.Button(
                self.header_actions, text=title, command=self.attempt(command),
            )
        self.logout_button = ttk.Button(
            self.header_actions, text="退出登录", command=self.attempt(self.sign_out),
        )
        self.logout_button.pack(side="left", padx=4)
        self.body = ttk.Frame(shell)
        self.body.pack(fill="both", expand=True)
        self.reload_ledgers(self.ledger, pick_ledger=pick_ledger)
        self.start_sync()

    def change_username(self):
        name = self.app.accounts.username(self.token)
        values = self.form("设置用户名", [("username", "用户名", name, ())])
        if values:
            self.app.accounts.set_username(self.token, values["username"])
            if self.ledger is not None:
                self.switch_ledger()

    @observe_only()
    def ledger_member_labels(self):
        return member_labels(self.app.ledgers.members(self.token, self.ledger))

    def update_ledger_choices(self, ledgers):
        self.ledgers = ledgers
        self.ledger_choice.configure(
            values=[
                f"{row['name']} · {ROLE[row['role']]}" + (" · 已解散" if row["dissolved"] else "")
                for row in self.ledgers
            ]
        )

    def reload_ledgers(
        self, preferred=None, *, pick_ledger=False, ledgers=None, preserve_view=False,
    ):
        if ledgers is None:
            ledgers = self.app.ledgers.list(self.token)
        self.update_ledger_choices(ledgers)
        index = next(
            (i for i, row in enumerate(self.ledgers) if row["id"] == preferred), 0
        )
        if pick_ledger or not self.ledgers:
            self.show_my_ledgers()
        else:
            self.ledger_choice.current(index)
            self.switch_ledger(preserve_view=preserve_view)

    def show_my_ledgers(self):
        selected = self.ledger
        self.ledger = None
        self.active_page = 0
        self.ledger_choice.set("")
        self.ledger_choice.pack_forget()
        self.back_to_ledgers.pack_forget()
        self.refresh_ledger_buttons()
        for child in self.body.winfo_children():
            child.destroy()
        ttk.Label(self.body, text="我的账本", style="Section.TLabel").pack(anchor="w", pady=(0, 12))
        if not self.ledgers:
            ttk.Label(self.body, text="创建一个账本，或使用邀请加入现有账本。").pack(anchor="w")
            return

        def enter(key):
            index = next(
                (position for position, row in enumerate(self.ledgers)
                 if str(row["id"]) == key),
                None,
            )
            if index is None:
                return
            self.ledger_choice.current(index)
            self.switch_ledger()

        ttk.Label(self.body, text="双击账本进入", style="Subtle.TLabel").pack(
            anchor="w", pady=(0, 10)
        )
        cards = None

        def activate():
            chosen = cards.selection() if cards is not None else ()
            if chosen:
                enter(chosen[0])

        cards = RecordCards(
            self.body, on_activate=self.attempt(activate), empty="还没有账本",
        )
        cards.pack(fill="both", expand=True)
        cards.load([
            {
                "key": str(row["id"]),
                "title": row["name"],
                "value": "已解散 · 只读" if row["dissolved"] else "使用中",
                "value_color": MUTED if row["dissolved"] else GREEN,
                "meta": ROLE[row["role"]],
            }
            for row in self.ledgers
        ])
        if selected is not None:
            cards.select(str(selected))
            cards.reveal(str(selected))

    def switch_ledger(self, *, preserve_view=False):
        index = self.ledger_choice.current()
        current = self.ledgers[index] if index >= 0 else None
        ledger = current["id"] if current is not None else None
        same_ledger = ledger == self.ledger
        month = self.month.get() if preserve_view and same_ledger else None
        if not same_ledger:
            self.active_page = 0
        self.ledger = ledger
        for child in self.body.winfo_children():
            child.destroy()
        self.selected_entry = None
        if not (preserve_view and same_ledger):
            self.filters = DEFAULT_ENTRY_FILTER
        if self.ledger is None:
            self.show_my_ledgers()
            return
        self.refresh_ledger_buttons()
        self.back_to_ledgers.pack(side="left", padx=(16, 8))
        self.ledger_choice.pack(side="left", padx=(0, 6))
        navigation = ttk.Frame(self.body, width=128)
        navigation.pack(side="left", fill="y", padx=(0, 16))
        content = ttk.Frame(self.body)
        content.pack(side="left", fill="both", expand=True)
        active = tk.IntVar(master=self.root, value=self.active_page)
        pages = []

        def select(index):
            changing_page = self.active_page != index
            self.active_page = index
            active.set(index)
            for page in pages:
                page.pack_forget()
            pages[index].pack(fill="both", expand=True)
            if changing_page:
                self.attempt(self.refresh_current_page)()

        for index, (title, draw) in enumerate((
            ("收支账目", self.draw_entries), ("预算与统计", self.draw_statistics),
            ("结算", self.draw_settlements), ("成员与日志", self.draw_members),
        )):
            ttk.Radiobutton(
                navigation, text=title, value=index, variable=active, width=11,
                style="Nav.TRadiobutton", command=lambda index=index: select(index),
            ).pack(fill="x", pady=(0, 5))
            page = ttk.Frame(content)
            pages.append(page)
            ttk.Label(page, text=title, style="Section.TLabel").pack(anchor="w", pady=(0, 10))
            draw(page)
        if month is not None:
            self.month.set(month)
        select(self.active_page)

    def create_ledger(self):
        values = self.form("创建账本", [("name", "账本名称", "", ())])
        if values:
            ledger = self.app.ledgers.create(self.token, values["name"])
            self.reload_ledgers(ledger)

    def accept_invite(self):
        values = self.form("接受邀请", [("token", "邀请口令", "", ())])
        if values:
            ledger = self.app.ledgers.accept(self.token, values["token"])
            self.reload_ledgers(ledger)

    def toolbar(self, parent, actions, *, primary=None):
        frame = ttk.Frame(parent)
        frame.pack(fill="x", pady=(0, 8))
        for label, action in actions:
            style = "Primary.TButton" if label == primary else "TButton"
            ttk.Button(frame, text=label, style=style, command=self.attempt(action)).pack(
                side="left", padx=(0, 7)
            )

    def draw_entries(self, panel):
        self.entry_summary = SummaryCard(panel)
        heading = ttk.Frame(panel)
        heading.pack(fill="x", pady=(6, 8))
        ttk.Label(heading, text="账目明细", style="Section.TLabel").pack(side="left")
        self.new_entry_button = ttk.Button(
            heading, text="＋ 新增收支", style="Primary.TButton",
            command=self.attempt(self.new_entry),
        )
        self.pending_review_button = ttk.Button(
            panel, command=self.attempt(self.show_pending_reviews),
        )
        actions = ttk.Frame(heading)
        actions.pack(side="right")
        self.entry_status_choice = ttk.Combobox(
            actions, values=(ALL_STATUSES, *ENTRY_STATUS_CHOICES.values()),
            state="readonly", width=10,
        )
        self.entry_status_choice.pack(side="left")
        self.entry_status_choice.bind(
            "<<ComboboxSelected>>", lambda _event: self.attempt(self.set_entry_status)()
        )
        for title, command in (
            ("筛选", self.filter_entries),
            ("清除筛选", self.reset_filter),
            ("刷新", self.refresh_entries),
        ):
            ttk.Button(actions, text=title, command=self.attempt(command)).pack(
                side="left", padx=(6, 0)
            )
        self.entries = EntryCards(
            panel, on_select=self.attempt(self.load_selection),
            on_open=self.attempt(self.entry_detail),
            on_menu=self.attempt(self.entry_actions),
            empty="暂无账目",
        )
        self.entries.pack(fill="both", expand=True)
        self.refresh_entries()

    def refresh_entries(self, *, preserve_view=False):
        if not preserve_view:
            self.selected_entry = None
        labels = self.ledger_member_labels()
        entries = self.app.queries.entries(self.token, self.ledger, self.filters)
        self.entry_status_choice.set(ENTRY_STATUS_CHOICES.get(self.filters.status, ALL_STATUSES))
        self.entry_versions = {row["id"]: row["version"] for row in entries}
        batch_numbers = {
            row["id"]: row["batch_number"]
            for row in self.app.settlements.history(self.token, self.ledger)
        }
        month = datetime.now(BEIJING).date().isoformat()[:7]
        totals = self.app.queries.statistics(self.token, self.ledger, month)
        self.entry_summary.show_totals(f"{month} · 已通过账目", totals)
        rows = []
        for row in sorted(entries, key=lambda entry: (entry_occurrence(entry) or "", entry["id"]),
                          reverse=True):
            batch = batch_numbers.get(row["batch_id"])
            context = f"批次 {batch}" if batch is not None else ""
            rows.append(entry_card(row, labels, context=context))
        if not preserve_view or rows != self.entries.rows:
            self.entries.set_rows(rows, preserve_view=preserve_view)
        selected = self.selected_entry
        if not self.entries.selected or (
            selected is not None and self.entry_versions.get(selected["id"]) != selected["version"]
        ):
            self.selected_entry = None
        if self.can_write_entries():
            self.new_entry_button.pack(side="left", padx=(12, 0))
        else:
            self.new_entry_button.pack_forget()
        pending = (
            self.app.queries.pending_review_count(self.token, self.ledger)
            if self.can_manage_ledger() else 0
        )
        if pending:
            self.pending_review_button.configure(text=f"待审核 {pending} 笔")
            self.pending_review_button.pack(before=self.entries, anchor="w", pady=(0, 8))
        else:
            self.pending_review_button.pack_forget()

    @observe_only()
    def load_selection(self):
        entry_id = self.entries.selected_entry_id()
        self.selected_entry = self.app.entries.get(self.token, self.ledger, entry_id)

    def current_entry(self):
        entry_id = self.entries.selected_entry_id()
        if self.selected_entry is None or self.selected_entry["id"] != entry_id:
            self.load_selection()
        return self.selected_entry

    def ledger_role(self):
        return next(
            (row["role"] for row in self.ledgers if row["id"] == self.ledger), "reader"
        )

    def ledger_is_dissolved(self):
        return any(row["id"] == self.ledger and row["dissolved"] for row in self.ledgers)

    def can_write_entries(self):
        return self.ledger_role() in {"admin", "writer"} and not self.ledger_is_dissolved()

    def can_manage_ledger(self):
        return self.ledger_role() == "admin" and not self.ledger_is_dissolved()

    def refresh_ledger_buttons(self):
        for title, button in self.ledger_buttons.items():
            if self.ledger_role() == "admin" and (title == "查看日志" or self.can_manage_ledger()):
                button.pack(side="left", padx=4, before=self.logout_button)
            else:
                button.pack_forget()

    def build_menu(self):
        if self.context_menu is not None:
            self.context_menu.close()
        self.context_menu = PopupMenu(self.root)
        return self.context_menu

    def entry_actions(self, event):
        self.load_selection()
        row = self.selected_entry
        if self.entry_versions.get(row["id"]) != row["version"]:
            self.refresh_entries(preserve_view=True)
            raise RuleError("账目已更新，请核对最新内容后重新打开菜单")
        actor = self.app.accounts.actor(self.token)
        role = self.ledger_role()
        open_ledger = not self.ledger_is_dissolved()
        writable = self.can_write_entries()
        admin = role == "admin" and open_ledger
        approved_original = row["kind"] != "reversal" and row["status"] == "approved"
        has_reversal = approved_original and self.app.entries.has_active_reversal(
            self.token, self.ledger, row["id"]
        )
        retry_reversal = (
            writable and row["kind"] == "reversal" and row["submitter_id"] == actor
            and row["status"] in {"rejected", "withdrawn"}
        )
        editable = (
            writable and row["creator_id"] == actor
            and row["kind"] != "reversal" and row["status"] in EDITABLE
        )
        withdrawable = (
            writable and row["submitter_id"] == actor and row["status"] == "pending"
        )
        if row["status"] == "draft":
            voidable = open_ledger and row["creator_id"] == actor
        elif row["status"] == "approved":
            voidable = admin and row["batch_id"] is None and approved_original and not has_reversal
        else:
            voidable = admin and row["status"] in EDITABLE
        reviewable = (
            admin and row["status"] == "pending" and row["submitter_id"] != actor
        )
        reversible = (
            writable and approved_original and not has_reversal
            and (admin or row["submitter_id"] == actor)
        )
        correctable = writable and row["kind"] == "reversal" and row["status"] == "approved"
        menu = self.build_menu()
        menu.add_command(label="详情与日志", command=self.attempt(self.entry_detail))
        menu.add_separator()
        menu.add_command(
            label="重新提交冲销" if retry_reversal else "编辑 / 提交",
            state=menu_state(editable or retry_reversal),
            command=self.attempt(self.edit_entry),
        )
        menu.add_command(
            label="撤回", state=menu_state(withdrawable),
            command=self.attempt(self.withdraw),
        )
        menu.add_command(
            label="作废", state=menu_state(voidable), command=self.attempt(self.void),
        )
        menu.add_separator()
        menu.add_command(
            label="审核通过", state=menu_state(reviewable),
            command=self.attempt(lambda: self.decide(True, row=row)),
        )
        menu.add_command(
            label="审核退回", state=menu_state(reviewable),
            command=self.attempt(lambda: self.decide(False, row=row)),
        )
        menu.add_separator()
        menu.add_command(
            label="申请冲销", state=menu_state(reversible),
            command=self.attempt(self.reverse),
        )
        menu.add_command(
            label="关联补记", state=menu_state(correctable),
            command=self.attempt(self.correct),
        )
        menu.show(event)

    def entry_detail(self):
        row = self.current_entry()
        ledger = self.ledger
        entry_id = row["id"]

        def reload_text():
            latest = self.app.entries.get(self.token, ledger, entry_id)
            return self.entry_detail_text(ledger, latest)

        self.text_view("账目详情", self.entry_detail_text(ledger, row), reload_text=reload_text)

    def entry_detail_text(self, ledger, row):
        labels = member_labels(self.app.ledgers.members(self.token, ledger))
        lines = [
            f"#{row['id']}  {row['title']}",
            f"{ENTRY_TYPE[row['entry_type']]}  {money(row['amount_cent'])} 元"
            f"  ·  {STATUS[row['status']]}  ·  版本 {row['version']}",
            f"时间（北京时间）：{entry_occurrence(row) or ''}    分类：{row['category']}",
            f"创建人：{labels.get(row['creator_id'], '—')}",
            f"提交人：{labels.get(row['submitter_id'], '—')}",
            f"垫付 / 代收：{labels.get(row['fund_holder_id'], '—')}",
            f"备注：{row['notes']}",
            f"原因：{row['reason'] or '—'}",
            f"原账：{row['original_entry_id'] or '—'}    "
            f"关联冲销：{row['corrected_reversal_id'] or '—'}",
            "", "分配明细",
        ]
        lines.extend(
            f"{labels[item['member_user_id']]}  {money(item['amount_cent'])} 元"
            for item in row["allocations"]
        )
        lines.extend(("", "操作历史（北京时间）"))
        lines.extend(
            f"{display_time(item['occurred_at'])}  "
            f"{labels.get(item['operator_id'], item['operator_id'])}  "
            f"{item['action']}  {item['reason']}"
            for item in self.app.queries.audit(self.token, ledger, entry_id=row["id"])
        )
        return "\n".join(lines)

    def collect_entry(self, row=None, *, correction=False):
        current = row or {}
        current_time = datetime.now(BEIJING)
        labels = self.ledger_member_labels()
        category = current.get("category", "")
        categories = tuple(dict.fromkeys(("", *CATEGORIES, category)))
        fields = [
            ("title", "名称", current.get("title", ""), ()),
            ("category", "分类", category, categories),
            ("type", "收支类型", ENTRY_TYPE[current.get("entry_type", "expense")],
             tuple(ENTRY_TYPE.values())),
            ("date", "日期", current.get("occurred_on", current_time.date().isoformat()), ()),
            ("time", "北京时间（HH:MM）", (current.get("occurred_time") or "") if row else
             current_time.strftime("%H:%M"), ()),
            ("amount", "金额（元）", money(current.get("amount_cent")), ()),
            ("holder", "垫付 / 代收人", labels.get(current.get("fund_holder_id"), ""),
             ("", *labels.values())),
            ("notes", "备注", current.get("notes", ""), ()),
        ]
        allocations = current["allocations"] if row else None
        return EntryForm(
            self.root, fields, labels, allocations,
            status="draft" if correction else current.get("status", "draft"),
            title="关联补记" if correction else None,
            require_time=row is None or bool(current.get("occurred_time")),
        ).result

    def save_entry(self, operation, *args, submit, duplicate_label="仍要提交", **kwargs):
        request_id = uuid4().hex if submit else None
        confirmed = ()
        while True:
            try:
                operation(
                    *args, submit=submit, request_id=request_id, confirmed=confirmed, **kwargs,
                )
                return True
            except DuplicateEntry as error:
                if not MessageDialog(
                    self.root, "疑似重复账目", str(error), confirm=True, confirm_label=duplicate_label
                ).result:
                    return False
                confirmed = error.entry_ids

    def new_entry(self, correction_for=None):
        if correction_for is None:
            data = self.collect_entry()
        else:
            with observe_only():
                reversal = self.app.entries.get(self.token, self.ledger, correction_for)
                if reversal["kind"] != "reversal" or reversal["status"] != "approved":
                    raise RuleError("请先选择已通过的冲销记录")
                original = self.app.entries.get(
                    self.token, self.ledger, reversal["original_entry_id"],
                )
            data = self.collect_entry(original, correction=True)
        if data:
            fields, submit = data
            if self.save_entry(
                self.app.entries.create, self.token, self.ledger, fields, submit=submit,
                correction_for=correction_for, duplicate_label="仍要新增",
            ):
                self.refresh_entries()

    def edit_entry(self):
        row = self.current_entry()
        if row["kind"] == "reversal":
            reason = self.reason("重新提交冲销")
            if reason is not None:
                self.app.review.resubmit_reversal(
                    self.token, self.ledger, row["id"], row["version"], reason
                )
                self.refresh_entries()
            return
        data = self.collect_entry(row)
        if data:
            fields, submit = data
            if not self.save_entry(
                self.app.entries.edit, self.token, self.ledger, row["id"], row["version"],
                fields, submit=submit,
            ):
                return
            self.refresh_entries()
            if submit and row["status"] in {"rejected", "withdrawn"}:
                MessageDialog(self.root, "已重新提交", "这笔账目已进入待审核。")

    def filter_entries(self):
        filters = self.filters
        labels = self.ledger_member_labels()
        choices = ("", *labels.values())
        ids = {label: user for user, label in labels.items()}
        with observe_only():
            visible = self.app.queries.entries(self.token, self.ledger)
        saved_categories = sorted({row["category"] for row in visible if row["category"]})
        categories = tuple(dict.fromkeys(("", *CATEGORIES, *saved_categories, filters.category)))
        values = self.form("组合筛选", [
            ("type", "收支类型", ENTRY_TYPE.get(filters.entry_type, ""), ("", *ENTRY_TYPE.values())),
            ("status", "账目状态", ENTRY_STATUS_CHOICES.get(filters.status, ALL_STATUSES),
             (ALL_STATUSES, *ENTRY_STATUS_CHOICES.values())),
            ("category", "分类", filters.category, categories),
            ("start", "开始日期", filters.start_date, ()),
            ("start_time", "开始时间（HH:MM，可空）", filters.start_time, ()),
            ("end", "结束日期", filters.end_date, ()),
            ("end_time", "结束时间（HH:MM，可空）", filters.end_time, ()),
            ("min", "最低金额", money(filters.minimum), ()),
            ("max", "最高金额", money(filters.maximum), ()),
            ("submitter", "提交人", labels.get(filters.submitter, ""), choices),
            ("holder", "垫付 / 代收人", labels.get(filters.fund_holder, ""), choices),
            ("participant", "分配成员", labels.get(filters.participant, ""), choices),
        ], "按发生时间筛选，北京时间可精确到分钟；日期和时间均包含边界。")
        if values:
            current = self.filters
            self.filters = EntryFilter(
                entry_type=code_for(ENTRY_TYPE, values["type"]),
                status=code_for(ENTRY_STATUS_CHOICES, values["status"]),
                category=values["category"].strip(),
                start_date=values["start"].strip(),
                end_date=values["end"].strip(),
                minimum=optional_amount(values["min"]),
                maximum=optional_amount(values["max"]),
                submitter=ids.get(values["submitter"]),
                fund_holder=ids.get(values["holder"]),
                participant=ids.get(values["participant"]),
                start_time=values["start_time"].strip(),
                end_time=values["end_time"].strip(),
            )
            try:
                self.refresh_entries()
            except RuleError:
                self.filters = current
                self.refresh_entries()
                raise

    def reset_filter(self):
        self.filters = DEFAULT_ENTRY_FILTER
        self.refresh_entries()

    def set_entry_status(self):
        status = code_for(ENTRY_STATUS_CHOICES, self.entry_status_choice.get())
        self.filters = replace(self.filters, status=status)
        self.refresh_entries()

    def show_pending_reviews(self):
        self.filters = EntryFilter(status="pending")
        self.refresh_entries()

    def withdraw(self):
        row = self.current_entry()
        self.app.entries.withdraw(self.token, self.ledger, row["id"], row["version"])
        self.refresh_entries()

    def reason(self, title):
        values = self.form(title, [("reason", "原因", "", ())], "请输入原因")
        return values["reason"] if values is not None else None

    def void(self):
        row = self.current_entry()
        reason = self.reason("作废账目")
        if reason is not None:
            self.app.entries.void(self.token, self.ledger, row["id"], row["version"], reason)
            self.refresh_entries()

    def decide(self, approved, *, row=None):
        row = self.current_entry() if row is None else row
        reason = "" if approved else self.reason("审核退回")
        if reason is not None:
            self.app.review.decide(
                self.token, self.ledger, row["id"], row["version"], approve=approved,
                reason=reason,
            )
            self.refresh_entries()

    def reverse(self):
        row = self.current_entry()
        reason = self.reason("整笔冲销")
        if reason is not None:
            self.app.review.reverse(self.token, self.ledger, row["id"], row["version"], reason)
            self.refresh_entries()

    def correct(self):
        row = self.current_entry()
        if row["kind"] != "reversal" or row["status"] != "approved":
            raise RuleError("请先选择已通过的冲销记录")
        self.new_entry(correction_for=row["id"])

    def draw_settlements(self, panel):
        actions = [("刷新历史", self.refresh_batches)]
        if self.can_manage_ledger():
            actions.insert(0, ("发起结算", self.prepare_settlement))
            actions.append(("结算恢复", self.show_settlement_recovery))
        self.toolbar(panel, actions, primary="发起结算")
        self.batches = RecordCards(
            panel, on_activate=self.attempt(self.batch_detail), empty="暂无结算批次",
            activate_on_click=True,
        )
        self.batches.pack(fill="both", expand=True)
        self.refresh_batches()

    def refresh_batches(self, *, preserve_view=False):
        """Show saved batches with the dates and confirming member."""
        labels = self.ledger_member_labels()
        self.batches.load([
            {
                "key": str(row["id"]),
                "title": f"批次 {row['batch_number']}",
                "value": f"{row['start_date']} 至 {row['end_date']}",
                "value_color": NUMERIC,
                "meta": (
                    f"确认人 {labels.get(row['operator_id'], '未知成员')}"
                    f" · 北京时间 {display_time(row['created_at'])}"
                ),
            }
            for row in self.app.settlements.history(self.token, self.ledger)
        ], preserve_view=preserve_view)

    def prepare_settlement(self):
        today = datetime.now(BEIJING).date().isoformat()
        values = self.form("结算期间", [
            ("start", "开始日期", today[:8] + "01", ()),
            ("end", "结束日期", today, ()),
        ], dates_required=True)
        if not values:
            return
        start, end = values["start"].strip(), values["end"].strip()
        candidates = self.app.settlements.history_candidates(self.token, self.ledger, start, end)
        if candidates:
            history_ids = self.choose_history(candidates)
            if history_ids is None:
                return
        else:
            history_ids = ()
        preview = self.app.settlements.preview(self.token, self.ledger, start, end, history_ids)
        self.show_preview(preview, len(candidates) - len(history_ids))

    def choose_history(self, candidates):
        window = tk.Toplevel(self.root)
        window.title("选择历史未结算项")
        window.geometry("580x385")
        window.transient(self.root)
        window.grab_set()
        area = ttk.Frame(window, padding=12)
        area.pack(fill="both", expand=True)
        ttk.Label(area, text="点击选择要纳入的历史记录，可以多选。").pack(anchor="w")
        labels = self.ledger_member_labels()
        cards = EntryCards(area, multiple=True)
        cards.pack(fill="both", expand=True, pady=10)
        cards.set_rows(
            entry_card(row["snapshot"], labels, context=f"结算依据日期 {row['date']}",
                       status="历史未结算")
            for row in candidates
        )
        result = None

        def accept():
            nonlocal result
            result = tuple(row["id"] for row in candidates if row["id"] in cards.selected)
            window.destroy()

        ttk.Button(area, text="继续预览", command=accept, style="Primary.TButton").pack(side="right")
        ttk.Button(area, text="取消", command=window.destroy).pack(side="right", padx=8)
        self.root.wait_window(window)
        return result

    @staticmethod
    def settlement_summary(balances, transfers, labels):
        def name(member):
            return labels.get(member, f"成员 {member}")

        net = "\n".join(f"{name(member)}  {money(amount)} 元" for member, amount in balances)
        plan = "\n".join(
            f"{name(payer)} → {name(payee)}  {money(amount)} 元"
            for payer, payee, amount in transfers
        )
        return (
            ("成员净额（正数应收，负数应付）", net or "暂无成员净额"),
            ("建议转账", plan or "无需转账"),
        )

    def show_preview(self, preview, omitted):
        window = tk.Toplevel(self.root)
        window.title("结算预览")
        window.geometry("770x570")
        window.transient(self.root)
        window.grab_set()
        panel = ttk.Frame(window, padding=16)
        panel.pack(fill="both", expand=True)
        heading = ttk.Frame(panel, style="Summary.TFrame", padding=(18, 12))
        heading.pack(fill="x")
        ttk.Label(
            heading, text=f"本次纳入 {len(preview.items)} 笔", style="Amount.TLabel"
        ).pack(anchor="w")
        period = f"{preview.start_date} 至 {preview.end_date} · 尚有 {omitted} 条历史项未纳入"
        ttk.Label(heading, text=period, style="Summary.TLabel").pack(anchor="w")
        ttk.Label(panel, text=PAYMENT_NOTICE, style="Notice.TLabel").pack(fill="x", pady=(10, 0))
        self.draw_settlement_details(panel, preview.items, preview.balances, preview.transfers)

        def confirm():
            try:
                batch = self.app.settlements.confirm(self.token, preview)
            except RuleError as error:
                MessageDialog(window, "结算未保存", str(error))
                if "刷新预览" in str(error) or str(error) == "请重新登录":
                    window.destroy()
                    if str(error) == "请重新登录":
                        self.sign_out()
                return
            except sqlite3.Error:
                MessageDialog(
                    window, "保存结果待确认",
                    "可在当前窗口重试，或重启后在「结算恢复」查询这次请求。",
                )
                return
            window.destroy()
            self.refresh_batches()
            self.refresh_entries()
            saved = self.batches.get(str(batch)) or {}
            MessageDialog(self.root, "结算已保存", f"{saved.get('title', '结算批次')} 已保存")

        ttk.Button(
            panel, text="确认并保存结算", style="Primary.TButton", command=confirm
        ).pack(side="right")
        ttk.Button(panel, text="取消", command=window.destroy).pack(side="right", padx=8)

    def show_settlement_recovery(self):
        """Open the original batch or retry the selected durable request."""
        requests = self.app.settlements.requests(self.token, self.ledger)
        if not requests:
            MessageDialog(self.root, "结算恢复", "暂无已发起的结算请求")
            return
        choices = {}
        for row in requests:
            status = "已保存" if row["batch_id"] is not None else "未保存"
            label = f"请求 {row['sequence']} · {row['start_date']} 至 {row['end_date']} · {status}"
            choices[label] = row
        labels = tuple(choices)
        values = self.form("结算恢复", [("request", "结算请求", labels[0], labels)])
        if not values:
            return
        row = choices[values["request"]]
        if row["batch_id"] is not None:
            self.batch_detail(row["batch_id"])
            return
        reason = row["error"] or "上一次请求中断，数据库未保存这个批次。"
        if "刷新预览" in reason:
            MessageDialog(self.root, "结算未保存", reason)
            return
        if MessageDialog(
            self.root, "结算未保存", f"{reason}\n按原预览重试？结算依据发生变化时会要求重新预览。",
            confirm=True, confirm_label="重试原请求",
        ).result:
            batch = self.app.settlements.retry(self.token, self.ledger, row["request_id"])
            self.refresh_batches()
            self.refresh_entries()
            self.batch_detail(batch)

    def batch_detail(self, batch_id=None):
        """Open the selected batch's saved settlement details."""
        if batch_id is None:
            selection = self.batches.selection()
            if not selection:
                raise RuleError("请先选择结算批次")
            batch_id = int(selection[0])
        detail = self.app.settlements.detail(self.token, self.ledger, batch_id)
        operator = self.ledger_member_labels().get(detail["operator_id"], "未知成员")
        window = tk.Toplevel(self.root)
        window.title(f"结算批次 #{detail['batch_number']}")
        window.geometry("790x590")
        panel = ttk.Frame(window, padding=16)
        panel.pack(fill="both", expand=True)
        ttk.Label(
            panel, text=f"{detail['start_date']} 至 {detail['end_date']}", style="Section.TLabel"
        ).pack(anchor="w")
        ttk.Label(
            panel, text=f"确认人 {operator} · {display_time(detail['created_at'])}（北京时间）",
            style="Subtle.TLabel",
        ).pack(anchor="w", pady=(4, 10))
        ttk.Label(panel, text=PAYMENT_NOTICE, style="Notice.TLabel").pack(fill="x")
        self.draw_settlement_details(
            panel, detail["items"], detail["balances"], detail["transfers"],
        )
        window.bind("<Escape>", lambda _event: window.destroy())

    def draw_settlement_details(self, panel, items, balances, transfers):
        labels = self.ledger_member_labels()
        details = ttk.Panedwindow(panel, orient="horizontal")
        details.pack(fill="both", expand=True, pady=10)
        left = ttk.Frame(details)
        right = ttk.Frame(details, padding=(6, 0, 0, 0))
        details.add(left, weight=3)
        details.add(right, weight=2)
        ttk.Label(left, text="结算明细", style="Section.TLabel").pack(anchor="w", pady=(0, 6))
        cards = EntryCards(left)
        cards.pack(fill="both", expand=True)

        def context(item):
            reason = INCLUSION[item["inclusion_reason"]]
            pair = item["paired_entry_id"]
            return f"{reason} · 对应 #{pair}" if pair is not None else reason

        cards.set_rows(
            entry_card(item["snapshot"], labels, context=context(item))
            for item in items
        )
        for index, (title, content) in enumerate(
            self.settlement_summary(balances, transfers, labels)
        ):
            section = ttk.LabelFrame(right, text=title, padding=6)
            section.pack(fill="both", expand=True, pady=(0, 10) if index == 0 else 0)
            summary = tk.Text(
                section, wrap="word", width=22, height=3, background=CARD, foreground=INK,
                padx=7, pady=6, relief="flat", spacing3=5,
            )
            scroll = SlimScrollbar(section, summary.yview, background=CARD)
            summary.configure(yscrollcommand=scroll.set)
            summary.insert("1.0", content)
            summary.configure(state="disabled")
            scroll.pack(side="right", fill="y")
            summary.pack(side="left", fill="both", expand=True)

    def draw_statistics(self, panel):
        self.stats_range = None
        self.month = tk.StringVar(value=datetime.now(BEIJING).date().isoformat()[:7])
        row = ttk.Frame(panel)
        row.pack(fill="x", pady=(0, 8))
        ttk.Label(row, text="月份").pack(side="left")
        month_field = CalendarField(
            row, self.month, month_only=True, optional=False, allow_future=True
        )
        month_field.pack(side="left", padx=7)
        self.range_button = ttk.Button(
            row, text="按日期区间", command=self.attempt(self.toggle_statistics_range)
        )
        self.range_button.pack(side="left", padx=(0, 7))
        if self.can_manage_ledger():
            ttk.Button(row, text="设置预算", command=self.attempt(self.set_budget)).pack(
                side="left", padx=7
            )
        self.stats_summary = SummaryCard(panel)
        budget = ttk.Frame(panel, style="Card.TFrame", padding=(16, 8))
        budget.pack(fill="x", pady=(0, 8))
        self.budget_message = ttk.Label(budget, style="Card.TLabel")
        self.budget_message.pack(anchor="w", pady=(0, 6))
        self.budget_progress = ttk.Progressbar(
            budget, maximum=100, style="Gold.Horizontal.TProgressbar"
        )
        self.budget_progress.pack(fill="x")

        lists = ttk.Frame(panel)
        lists.pack(fill="both", expand=True)
        lists.columnconfigure(0, weight=1)
        for row, title in enumerate(("分类统计", "成员统计")):
            lists.rowconfigure(row, weight=1, uniform="stats")
            card = ttk.Frame(lists, style="Card.TFrame", padding=8)
            card.grid(row=row, column=0, sticky="nsew",
                      pady=(0, 6) if row == 0 else (0, 0))
            ttk.Label(card, text=title, style="CardTitle.TLabel").pack(
                anchor="w", pady=(0, 4)
            )
            listing = RecordCards(
                card, compact=True, background=CARD,
                empty="暂无分类统计" if row == 0 else "暂无成员统计",
            )
            listing.pack(fill="both", expand=True)
            if row == 0:
                self.category_totals = listing
            else:
                self.member_totals = listing
        self.refresh_statistics()

        def month_changed(*_args):
            month_field.refresh()
            if self.stats_range is not None:
                self.stats_range = None
                self.range_button.configure(text="按日期区间")
            self.attempt(self.refresh_statistics)()

        self.month.trace_add("write", month_changed)

    def toggle_statistics_range(self):
        if self.stats_range is not None:
            self.stats_range = None
            self.range_button.configure(text="按日期区间")
            self.refresh_statistics()
            return
        today = datetime.now(BEIJING).date()
        values = self.form(
            "按日期区间统计",
            [
                ("start", "开始日期", today.replace(day=1).isoformat(), ()),
                ("end", "结束日期", today.isoformat(), ()),
            ],
            "按账目发生日期统计，包含起止日期。",
            dates_required=True,
        )
        if values:
            start, end = values["start"].strip(), values["end"].strip()
            if not start or not end or start > end:
                raise RuleError("请选择有效的日期区间，开始日期不能晚于结束日期")
            self.stats_range = start, end
            self.range_button.configure(text="返回月度统计")
            self.refresh_statistics()

    def refresh_statistics(self, *, preserve_view=False):
        """Refresh the active period and omit rows with no remaining activity."""
        if self.stats_range is not None:
            start, end = self.stats_range
            result = self.app.queries.statistics(
                self.token, self.ledger, start=start, end=end
            )
            caption = f"{start} 至 {end} · 已通过账目"
        else:
            month = self.month.get().strip()
            result = self.app.queries.statistics(self.token, self.ledger, month)
            caption = f"{month} · 已通过账目"
        self.stats_summary.show_totals(caption, result)
        budget = result["budget"]
        usage = 100 * result["expense"] / budget if budget else 0
        if self.stats_range is not None:
            message = "区间统计 · 不应用月度预算"
        elif budget is None:
            message = "预算 · 本月预算尚未设置"
        else:
            message = (
                f"预算 · 已支出 ¥{money(result['expense'])} / ¥{money(budget)}"
                f" · 剩余 ¥{money(result['remaining'])}"
            )
            if budget:
                message += f" · 使用 {usage:.0f}%"
        self.budget_message.configure(text=message)
        self.budget_progress.configure(value=max(0, min(100, usage)))
        self.category_totals.load([
            {
                "key": category, "title": category,
                "value": f"净额 ¥{money(amounts['net'])}", "value_color": NUMERIC,
                "meta": f"收入 ¥{money(amounts['income'])} · 支出 ¥{money(amounts['expense'])}",
            }
            for category, amounts in sorted(
                result["categories"].items(),
                key=lambda item: -(item[1]["income"] + item[1]["expense"]),
            )
            if amounts["income"] or amounts["expense"]
        ], preserve_view=preserve_view)
        labels = self.ledger_member_labels()
        self.member_totals.load([
            {
                "key": str(member),
                "title": labels.get(member, f"成员 {member}"),
                "value": self.member_net_label(amounts["net"]),
                "meta": (
                    f"垫付 ¥{money(amounts['paid'])} · 承担 ¥{money(amounts['expense'])}"
                    f" · 代收 ¥{money(amounts['collected'])} · 受益 ¥{money(amounts['income'])}"
                ),
                "value_color": NUMERIC,
            }
            for member, amounts in result["members"].items()
            if any(amounts[key] for key in ("paid", "expense", "collected", "income"))
        ], preserve_view=preserve_view)

    @staticmethod
    def member_net_label(net):
        if net > 0:
            return f"净应收 ¥{money(net)}"
        if net < 0:
            return f"净应付 ¥{money(-net)}"
        return "收支平衡 ¥0.00"

    def set_budget(self):
        values = self.form("设置月度预算", [("amount", "预算金额（元）", "", ())])
        if values:
            self.app.queries.set_budget(
                self.token, self.ledger, self.month.get().strip(), cents(values["amount"])
            )
            if self.stats_range is not None:
                self.stats_range = None
                self.range_button.configure(text="按日期区间")
            self.refresh_statistics()

    def draw_members(self, panel):
        self.members = RecordCards(
            panel, empty="暂无成员", on_menu=self.attempt(self.member_actions)
        )
        self.members.pack(fill="both", expand=True)
        self.refresh_members()

    def member_actions(self, event, row):
        actor = self.app.accounts.actor(self.token)
        menu = self.build_menu()
        if row is not None:
            manageable = self.can_manage_ledger() and row["active"]
            menu.add_command(
                label="调整角色", state=menu_state(manageable),
                command=self.attempt(self.change_role),
            )
            menu.add_command(
                label="移除成员", state=menu_state(manageable and row["user_id"] != actor),
                command=self.attempt(self.remove_member),
            )
            menu.add_separator()
        menu.add_command(
            label="退出账本", command=self.attempt(self.leave_ledger),
            state=menu_state(not self.ledger_is_dissolved()),
        )
        menu.show(event)

    def refresh_members(self, *, preserve_view=False):
        actor = self.app.accounts.actor(self.token)
        rows = []
        for row in self.app.ledgers.members(self.token, self.ledger):
            current = row["user_id"] == actor and row["left_at"] is None
            name = row["username"] or "未设置"
            if current:
                name += "（我）"
            meta = f"手机尾号 {row['phone'][-4:]} · 加入 {display_time(row['joined_at'])}"
            if row["left_at"]:
                meta += f" · 退出 {display_time(row['left_at'])}"
            rows.append({
                "key": str(row["id"]),
                "user_id": row["user_id"],
                "active": row["left_at"] is None,
                "title": name,
                "value": ROLE[row["role"]],
                "meta": meta,
                "accent": current,
            })
        self.members.load(rows, preserve_view=preserve_view)

    def invite(self):
        values = self.form("生成邀请", [("role", "加入后的角色", "记账成员", tuple(ROLE.values()))])
        if values:
            invitation = self.app.ledgers.invite(
                self.token, self.ledger, code_for(ROLE, values["role"])
            )
            self.root.clipboard_clear()
            self.root.clipboard_append(invitation)
            self.text_view("邀请已复制", f"{invitation}\n\n有效期 168 小时，成功加入后失效。")

    def change_role(self):
        member = self.members.get(str(selected_id(self.members, "成员")))["user_id"]
        values = self.form("调整角色", [("role", "新角色", "记账成员", tuple(ROLE.values()))])
        if values:
            self.app.ledgers.change_role(
                self.token, self.ledger, member, code_for(ROLE, values["role"])
            )
            self.reload_ledgers(self.ledger)

    def remove_member(self):
        member = self.members.get(str(selected_id(self.members, "成员")))["user_id"]
        if member == self.app.accounts.actor(self.token):
            raise RuleError("不能移除自己，请使用「退出账本」")
        label = self.ledger_member_labels()[member]
        if MessageDialog(self.root, "移除成员", f"移除 {label}？", confirm=True).result:
            self.app.ledgers.remove(self.token, self.ledger, member)
            self.reload_ledgers(self.ledger)

    def leave_ledger(self):
        if MessageDialog(self.root, "退出账本", "退出当前账本？", confirm=True).result:
            self.app.ledgers.leave(self.token, self.ledger)
            self.reload_ledgers(pick_ledger=True)

    def dissolve_ledger(self):
        info = self.app.ledgers.dissolve_info(self.token, self.ledger)
        if info["members"] > 1:
            MessageDialog(
                self.root, "无法解散账本",
                "仅当账本只剩管理员本人时才能解散。请先让其他成员退出账本。",
            )
            return
        if info["pending"] or info["unsettled"]:
            MessageDialog(
                self.root, "无法解散账本",
                f"仍有 {info['pending']} 笔待审核、{info['unsettled']} 笔未结算账目，"
                "处理完成后才能解散。",
            )
            return
        offline_confirmed = False
        if info["transfers"]:
            if not MessageDialog(
                self.root, "线下处理确认",
                f"该账本有 {info['transfers']} 条涉及退出成员的结算方案。"
                "系统不能确认实际付款情况，请确认相关款项已在线下处理。",
                confirm=True, confirm_label="已线下处理",
            ).result:
                return
            offline_confirmed = True
        if MessageDialog(
            self.root, "解散账本", "解散后账本将只读且无法恢复，确定解散？",
            confirm=True,
        ).result:
            self.app.ledgers.dissolve(
                self.token, self.ledger, offline_confirmed=offline_confirmed
            )
            self.reload_ledgers(self.ledger)

    def audit(self):
        with observe_only():
            events = self.app.queries.audit(self.token, self.ledger)
        members = self.ledger_member_labels()
        operators = {
            members.get(user_id, f"成员 #{user_id}"): user_id
            for user_id in {event["operator_id"] for event in events}
        }
        actions = {
            AUDIT_ACTIONS.get(action, action): action
            for action in {event["action"] for event in events}
        }
        ledger = self.ledger
        with observe_only():
            objects = self.audit_object_labels(ledger, events, members)
        object_keys = {label: key for key, label in objects.items()}
        values = self.form("操作日志筛选", [
            ("start", "开始日期", "", ()),
            ("end", "结束日期", "", ()),
            ("operator", "操作人", "", ("", *sorted(operators))),
            ("object", "操作对象", "", ("", *sorted(object_keys))),
            ("action", "操作类型", "", ("", *sorted(actions))),
        ], "留空表示不限制该条件。日期按北京时间显示。")
        if not values:
            return
        filters = {
            "start": values["start"], "end": values["end"],
            "operator_id": operators.get(values["operator"]),
            "action": actions.get(values["action"], ""),
        }
        object_key = object_keys.get(values["object"])

        def reload_text():
            return self.audit_text(ledger, filters, object_key)

        self.text_view("操作日志（北京时间）", reload_text(), reload_text=reload_text)

    def audit_object_labels(self, ledger, events, members):
        entries = {
            row["id"]: row["title"] or "未命名账目"
            for row in self.app.queries.entries(self.token, ledger)
        }
        batches = {
            row["id"]: row["batch_number"]
            for row in self.app.settlements.history(self.token, ledger)
        }
        ledgers = self.app.ledgers.list(self.token)
        ledger_name = next((row["name"] for row in ledgers if row["id"] == ledger), "账本")
        objects = {}
        for kind, key in dict.fromkeys(audit_object(event) for event in events):
            if kind == "entry":
                label = f"账目 · {entries.get(key, '未命名账目')}（#{key}）"
            elif kind == "member":
                label = f"成员 · {members.get(key, f'成员 #{key}')}"
            elif kind == "batch":
                label = f"结算 · 批次 {batches.get(key, key)}"
            elif kind == "budget":
                label = f"预算 · {key or '月份未记录'}"
            elif kind == "ledger":
                label = f"账本 · {ledger_name}"
            else:
                label = f"{kind} · #{key}"
            objects[kind, key] = label
        return objects

    def audit_text(self, ledger, filters, object_key):
        events = self.app.queries.audit(self.token, ledger, **filters)
        members = member_labels(self.app.ledgers.members(self.token, ledger))
        objects = self.audit_object_labels(ledger, events, members)
        lines = [
            f"{display_time(event['occurred_at'])} · "
            f"{members.get(event['operator_id'], '未知成员')} · "
            f"{AUDIT_ACTIONS.get(event['action'], event['action'])}\n"
            f"{objects.get(audit_object(event), '新记录')} {event['reason']}\n"
            f"{self.format_audit_change(event)}"
            for event in events
            if object_key is None or audit_object(event) == object_key
        ]
        return "\n\n".join(lines) or "没有符合条件的日志。"

    @staticmethod
    def format_audit_change(event):
        changes = []
        for column, label in (("before_content", "修改前"), ("after_content", "修改后")):
            if event[column]:
                data = json.loads(event[column])
                for field in data.keys() & AUDIT_TIMESTAMPS:
                    if data[field]:
                        data[field] = display_time(data[field])
                changes.append(f"{label}  {json.dumps(data, ensure_ascii=False)}")
        return "\n".join(changes)
