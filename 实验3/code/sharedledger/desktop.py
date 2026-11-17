"""Tkinter desktop screens for the shared ledger workflow."""

from datetime import date
import json
import sqlite3
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from .entries import EntryInput, allocate_evenly, cents, money
from .queries import EntryFilter
from .storage import RuleError


ROLE = {"admin": "管理员", "writer": "记账成员", "reader": "只读成员"}
STATUS = {
    "draft": "草稿", "pending": "待审核", "approved": "已通过",
    "rejected": "被退回", "withdrawn": "已撤回", "voided": "已作废",
}
ENTRY_TYPE = {"expense": "支出", "income": "收入"}
INCLUSION = {
    "history": "手动纳入历史未结算项",
    "period": "期间内",
    "paired": "与原账成组",
    "paired_outside_period": "日期超出范围，随原账成组纳入",
}


def code_for(mapping, label):
    return next((key for key, value in mapping.items() if value == label), "")


def optional_id(value):
    if not value.strip():
        return None
    try:
        result = int(value)
    except ValueError as error:
        raise RuleError("成员或对象编号必须是正整数") from error
    if result <= 0:
        raise RuleError("成员或对象编号必须是正整数")
    return result


def optional_amount(value):
    return cents(value) if value.strip() else None


def table(parent, columns, headings, widths):
    panel = ttk.Frame(parent)
    panel.pack(fill="both", expand=True)
    tree = ttk.Treeview(panel, columns=columns, show="headings", selectmode="browse")
    scroll = ttk.Scrollbar(panel, orient="vertical", command=tree.yview)
    tree.configure(yscrollcommand=scroll.set)
    for column, heading, width in zip(columns, headings, widths):
        tree.heading(column, text=heading)
        tree.column(column, width=width, minwidth=45, anchor="w")
    tree.pack(side="left", fill="both", expand=True)
    scroll.pack(side="right", fill="y")
    return tree


def selected_id(tree, label):
    selected = tree.selection()
    if not selected:
        raise RuleError(f"请先选择{label}")
    return int(tree.item(selected[0], "values")[0])


class Form(simpledialog.Dialog):
    """A labeled dialog with inline choices and text fields."""

    def __init__(self, parent, title, fields, hint=""):
        self.fields = fields
        self.hint = hint
        self.variables = {}
        self.result = None
        super().__init__(parent, title)

    def body(self, parent):
        if self.hint:
            ttk.Label(parent, text=self.hint, wraplength=480, justify="left").grid(
                row=0, column=0, columnspan=2, sticky="w", padx=8, pady=(8, 12)
            )
        first = None
        for index, (key, label, default, choices) in enumerate(self.fields, start=1):
            ttk.Label(parent, text=label).grid(
                row=index, column=0, padx=8, pady=5, sticky="w"
            )
            value = tk.StringVar(value=default)
            self.variables[key] = value
            if choices:
                widget = ttk.Combobox(
                    parent, textvariable=value, values=choices, state="readonly", width=36
                )
            else:
                widget = ttk.Entry(
                    parent, textvariable=value, width=39,
                    show="*" if key == "password" else "",
                )
            widget.grid(row=index, column=1, padx=8, pady=5, sticky="ew")
            if first is None:
                first = widget
        parent.columnconfigure(1, weight=1)
        return first

    def buttonbox(self):
        box = ttk.Frame(self)
        ttk.Button(box, text="确定", width=12, command=self.ok).pack(side="left", padx=5)
        ttk.Button(box, text="取消", width=12, command=self.cancel).pack(side="left", padx=5)
        box.pack(pady=10)
        self.bind("<Return>", self.ok)
        self.bind("<Escape>", self.cancel)

    def apply(self):
        self.result = {key: value.get() for key, value in self.variables.items()}


class Desktop:
    def __init__(self, root, application):
        self.root = root
        self.app = application
        self.token = None
        self.ledger = None
        self.selected_entry = None
        self.filters = EntryFilter()
        self.ledgers = []
        root.title("多人共享记账")
        root.geometry("1080x700")
        root.minsize(850, 530)
        style = ttk.Style(root)
        if "clam" in style.theme_names():
            style.theme_use("clam")
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 20, "bold"))
        style.configure("Subtle.TLabel", foreground="#545c65")
        style.configure("Treeview", rowheight=29)
        self.show_login()

    def attempt(self, action):
        def run():
            try:
                return action()
            except RuleError as error:
                messagebox.showwarning("操作未完成", str(error), parent=self.root)
                if str(error) == "请重新登录":
                    self.sign_out()
            except sqlite3.Error as error:
                messagebox.showerror("数据库操作失败", str(error), parent=self.root)
            return None
        return run

    def form(self, title, fields, hint=""):
        return Form(self.root, title, fields, hint).result

    def text_view(self, title, text):
        window = tk.Toplevel(self.root)
        window.title(title)
        window.geometry("750x510")
        panel = ttk.Frame(window, padding=14)
        panel.pack(fill="both", expand=True)
        view = tk.Text(panel, wrap="word", font=("Microsoft YaHei UI", 11))
        scroll = ttk.Scrollbar(panel, orient="vertical", command=view.yview)
        view.configure(yscrollcommand=scroll.set)
        view.insert("1.0", text)
        view.configure(state="disabled")
        view.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        window.bind("<Escape>", lambda _event: window.destroy())

    def clear(self):
        for child in self.root.winfo_children():
            child.destroy()

    def show_login(self):
        self.clear()
        frame = ttk.Frame(self.root, padding=36)
        frame.place(relx=0.5, rely=0.45, anchor="center")
        ttk.Label(frame, text="多人共享记账", style="Title.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w", pady=(0, 18)
        )
        phone = tk.StringVar()
        password = tk.StringVar()
        ttk.Label(frame, text="手机号").grid(row=1, column=0, sticky="w", pady=7)
        phone_entry = ttk.Entry(frame, textvariable=phone, width=28)
        phone_entry.grid(row=1, column=1, padx=(14, 0), pady=7)
        ttk.Label(frame, text="密码").grid(row=2, column=0, sticky="w", pady=7)
        password_entry = ttk.Entry(frame, textvariable=password, width=28, show="*")
        password_entry.grid(row=2, column=1, padx=(14, 0), pady=7)

        def login():
            self.token = self.app.accounts.login(phone.get(), password.get())
            self.show_workspace()

        def register():
            self.app.accounts.register(phone.get(), password.get())
            messagebox.showinfo("注册成功", "请使用刚注册的账号登录。", parent=self.root)

        actions = ttk.Frame(frame)
        actions.grid(row=3, column=0, columnspan=2, sticky="e", pady=12)
        ttk.Button(actions, text="注册", command=self.attempt(register)).pack(side="left")
        ttk.Button(actions, text="登录", command=self.attempt(login)).pack(
            side="left", padx=(8, 0)
        )
        password_entry.bind("<Return>", lambda _event: self.attempt(login)())
        phone_entry.focus_set()

    def sign_out(self):
        self.app.accounts.logout(self.token)
        self.token = self.ledger = self.selected_entry = None
        self.filters = EntryFilter()
        self.show_login()

    def show_workspace(self):
        self.clear()
        shell = ttk.Frame(self.root, padding=18)
        shell.pack(fill="both", expand=True)
        header = ttk.Frame(shell)
        header.pack(fill="x", pady=(0, 14))
        ttk.Label(header, text="共享账本", style="Title.TLabel").pack(side="left")
        self.ledger_choice = ttk.Combobox(header, state="readonly", width=27)
        self.ledger_choice.pack(side="left", padx=16)
        self.ledger_choice.bind(
            "<<ComboboxSelected>>", lambda _event: self.attempt(self.switch_ledger)()
        )
        for title, command in (
            ("创建账本", self.create_ledger), ("接受邀请", self.accept_invite),
            ("退出登录", self.sign_out),
        ):
            ttk.Button(header, text=title, command=self.attempt(command)).pack(
                side="left", padx=4
            )
        self.body = ttk.Frame(shell)
        self.body.pack(fill="both", expand=True)
        self.reload_ledgers(self.ledger)

    def reload_ledgers(self, preferred=None):
        self.ledgers = self.app.ledgers.list(self.token)
        self.ledger_choice.configure(
            values=[
                f"{row['name']} · {ROLE[row['role']]}" + (" · 已解散" if row["dissolved"] else "")
                for row in self.ledgers
            ]
        )
        index = next(
            (i for i, row in enumerate(self.ledgers) if row["id"] == preferred), 0
        )
        if self.ledgers:
            self.ledger_choice.current(index)
        else:
            self.ledger_choice.set("")
        self.switch_ledger()

    def switch_ledger(self):
        for child in self.body.winfo_children():
            child.destroy()
        self.selected_entry = None
        self.filters = EntryFilter()
        index = self.ledger_choice.current()
        self.ledger = self.ledgers[index]["id"] if index >= 0 else None
        if self.ledger is None:
            ttk.Label(
                self.body, text="创建一个账本，或使用邀请加入现有账本。"
            ).pack(pady=28)
            return
        tabs = ttk.Notebook(self.body)
        tabs.pack(fill="both", expand=True)
        for title, draw in (
            ("收支账目", self.draw_entries), ("结算", self.draw_settlements),
            ("预算与统计", self.draw_statistics), ("成员与日志", self.draw_members),
        ):
            panel = ttk.Frame(tabs, padding=12)
            tabs.add(panel, text=title)
            draw(panel)

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

    def toolbar(self, parent, actions):
        frame = ttk.Frame(parent)
        frame.pack(fill="x", pady=(0, 8))
        for label, action in actions:
            ttk.Button(frame, text=label, command=self.attempt(action)).pack(
                side="left", padx=(0, 7)
            )

    def draw_entries(self, panel):
        self.toolbar(panel, [
            ("新增收支", self.new_entry), ("筛选", self.filter_entries),
            ("清除筛选", self.reset_filter), ("刷新", self.refresh_entries),
            ("详情与日志", self.entry_detail), ("编辑 / 提交", self.edit_entry),
            ("撤回", self.withdraw), ("作废", self.void),
        ])
        self.toolbar(panel, [
            ("审核通过", lambda: self.decide(True)),
            ("审核退回", lambda: self.decide(False)),
            ("申请冲销", self.reverse), ("关联补记", self.correct),
        ])
        self.entries = table(
            panel,
            ("id", "title", "type", "amount", "date", "status", "submitter", "holder", "batch"),
            ("编号", "名称", "类型", "金额（元）", "发生日期", "状态", "提交人", "垫付 / 代收", "批次"),
            (55, 198, 56, 96, 105, 79, 60, 95, 56),
        )
        self.entries.bind("<<TreeviewSelect>>", self.attempt_selection)
        self.entries.bind("<Double-1>", lambda _event: self.attempt(self.entry_detail)())
        self.refresh_entries()

    def refresh_entries(self):
        self.selected_entry = None
        for item in self.entries.get_children():
            self.entries.delete(item)
        for row in self.app.queries.entries(self.token, self.ledger, self.filters):
            self.entries.insert("", "end", values=(
                row["id"], row["title"], ENTRY_TYPE[row["entry_type"]],
                money(row["amount_cent"]), row["occurred_on"] or "",
                STATUS[row["status"]], row["submitter_id"] or "",
                row["fund_holder_id"] or "", row["batch_id"] or "",
            ))

    def attempt_selection(self, _event):
        if self.entries.selection():
            self.attempt(self.load_selection)()
        else:
            self.selected_entry = None

    def load_selection(self):
        entry_id = selected_id(self.entries, "账目")
        self.selected_entry = self.app.entries.get(self.token, self.ledger, entry_id)

    def current_entry(self):
        entry_id = selected_id(self.entries, "账目")
        if self.selected_entry is None or self.selected_entry["id"] != entry_id:
            self.load_selection()
        return self.selected_entry

    def entry_detail(self):
        row = self.current_entry()
        lines = [
            f"#{row['id']}  {row['title']}",
            f"{ENTRY_TYPE[row['entry_type']]}  {money(row['amount_cent'])} 元"
            f"  ·  {STATUS[row['status']]}  ·  版本 {row['version']}",
            f"日期：{row['occurred_on'] or ''}    分类：{row['category']}",
            f"创建人：{row['creator_id']}    提交人：{row['submitter_id'] or '—'}"
            f"    垫付 / 代收：{row['fund_holder_id'] or '—'}",
            f"备注：{row['notes']}",
            f"原因：{row['reason'] or '—'}",
            f"原账：{row['original_entry_id'] or '—'}    "
            f"关联冲销：{row['corrected_reversal_id'] or '—'}",
            "", "分配明细",
        ]
        lines.extend(
            f"成员 {item['member_user_id']}  {money(item['amount_cent'])} 元"
            for item in row["allocations"]
        )
        lines.extend(("", "操作历史"))
        lines.extend(
            f"{item['occurred_at']}  成员 {item['operator_id']}  "
            f"{item['action']}  {item['reason']}"
            for item in self.app.queries.audit(self.token, self.ledger, entry_id=row["id"])
        )
        self.text_view("账目详情", "\n".join(lines))

    def collect_entry(self, row=None):
        current = row or {}
        members = self.app.ledgers.members(self.token, self.ledger)
        labels = ", ".join(
            f"{member['user_id']}（尾号 {member['phone'][-4:]}"
            + ("，已退出）" if member["left_at"] else "）")
            for member in members
        )
        stored = ", ".join(
            f"{part['member_user_id']}={money(part['amount_cent'])}"
            for part in current.get("allocations", ())
        )
        options = ["保存修改" if row else "保存草稿"]
        if current.get("status") != "pending":
            options.append("提交审核")
        values = self.form("编辑收支" if row else "新增收支", [
            ("title", "名称", current.get("title", ""), ()),
            ("category", "分类", current.get("category", ""), ()),
            ("type", "收支类型", ENTRY_TYPE[current.get("entry_type", "expense")],
             tuple(ENTRY_TYPE.values())),
            ("date", "日期", current.get("occurred_on") or date.today().isoformat(), ()),
            ("amount", "金额（元）", money(current.get("amount_cent")), ()),
            ("holder", "垫付 / 代收人编号", current.get("fund_holder_id") or "", ()),
            ("allocation", "分配明细", stored, ()),
            ("notes", "备注", current.get("notes", ""), ()),
            ("action", "保存方式", options[0], options),
        ], f"成员编号：{labels}\n等分填 1,2。自定义填 1=50.01,2=50.00。")
        if not values:
            return None
        amount = optional_amount(values["amount"])
        allocation_text = values["allocation"].strip().replace("，", ",")
        parts = [part.strip() for part in allocation_text.split(",")] if allocation_text else []
        allocations = ()
        if parts and any("=" in part for part in parts):
            if any(part.count("=") != 1 for part in parts):
                raise RuleError("自定义分配请统一使用 成员编号=金额")
            allocations = tuple(
                (optional_id(member), cents(share))
                for member, share in (part.split("=") for part in parts)
            )
        elif parts:
            allocations = allocate_evenly(amount, [optional_id(part) for part in parts])
        fields = EntryInput(
            values["title"], values["category"], values["date"].strip() or None,
            amount, code_for(ENTRY_TYPE, values["type"]), optional_id(values["holder"]),
            allocations, values["notes"],
        )
        return fields, values["action"] == "提交审核"

    def new_entry(self, correction_for=None):
        data = self.collect_entry()
        if data:
            fields, submit = data
            self.app.entries.create(
                self.token, self.ledger, fields, submit=submit, correction_for=correction_for
            )
            self.refresh_entries()

    def edit_entry(self):
        row = self.current_entry()
        data = self.collect_entry(row)
        if data:
            fields, submit = data
            self.app.entries.edit(
                self.token, self.ledger, row["id"], row["version"], fields, submit=submit
            )
            self.refresh_entries()

    def filter_entries(self):
        filters = self.filters
        values = self.form("组合筛选", [
            ("type", "收支类型", ENTRY_TYPE.get(filters.entry_type, ""), ("", *ENTRY_TYPE.values())),
            ("status", "审核状态", STATUS.get(filters.status, ""), ("", *STATUS.values())),
            ("category", "分类", filters.category, ()),
            ("start", "开始日期", filters.start_date, ()),
            ("end", "结束日期", filters.end_date, ()),
            ("min", "最低金额", money(filters.minimum), ()),
            ("max", "最高金额", money(filters.maximum), ()),
            ("submitter", "提交人编号", filters.submitter or "", ()),
            ("holder", "垫付 / 代收人编号", filters.fund_holder or "", ()),
            ("participant", "分配成员编号", filters.participant or "", ()),
        ])
        if values:
            current = self.filters
            self.filters = EntryFilter(
                code_for(ENTRY_TYPE, values["type"]), code_for(STATUS, values["status"]),
                values["category"].strip(), values["start"].strip(), values["end"].strip(),
                optional_amount(values["min"]), optional_amount(values["max"]),
                optional_id(values["submitter"]), optional_id(values["holder"]),
                optional_id(values["participant"]),
            )
            try:
                self.refresh_entries()
            except RuleError:
                self.filters = current
                self.refresh_entries()
                raise

    def reset_filter(self):
        self.filters = EntryFilter()
        self.refresh_entries()

    def withdraw(self):
        row = self.current_entry()
        self.app.entries.withdraw(self.token, self.ledger, row["id"], row["version"])
        self.refresh_entries()

    def reason(self, title):
        return simpledialog.askstring(title, "请输入原因", parent=self.root)

    def void(self):
        row = self.current_entry()
        reason = self.reason("作废账目")
        if reason is not None:
            self.app.entries.void(self.token, self.ledger, row["id"], row["version"], reason)
            self.refresh_entries()

    def decide(self, approved):
        row = self.current_entry()
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
        self.toolbar(panel, [
            ("发起结算", self.prepare_settlement), ("批次详情", self.batch_detail),
            ("刷新历史", self.refresh_batches),
        ])
        self.batches = table(
            panel, ("id", "start", "end", "operator", "created"),
            ("批次", "开始日期", "结束日期", "确认人", "保存时间（UTC）"),
            (80, 120, 120, 90, 260),
        )
        self.batches.bind("<Double-1>", lambda _event: self.attempt(self.batch_detail)())
        self.refresh_batches()

    def refresh_batches(self):
        for item in self.batches.get_children():
            self.batches.delete(item)
        for row in self.app.settlements.history(self.token, self.ledger):
            self.batches.insert("", "end", values=(
                row["id"], row["start_date"], row["end_date"],
                row["operator_id"], row["created_at"],
            ))

    def prepare_settlement(self):
        today = date.today().isoformat()
        values = self.form("结算期间", [
            ("start", "开始日期", today[:8] + "01", ()),
            ("end", "结束日期", today, ()),
        ])
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
        listbox = tk.Listbox(area, selectmode="multiple", exportselection=False)
        listbox.pack(fill="both", expand=True, pady=10)
        for row in candidates:
            listbox.insert(
                "end", f"#{row['id']}  {row['date']}  {row['title']}  "
                f"{money(row['amount_cent'])} 元",
            )
        result = None

        def accept():
            nonlocal result
            result = tuple(candidates[index]["id"] for index in listbox.curselection())
            window.destroy()

        ttk.Button(area, text="继续预览", command=accept).pack(side="right")
        ttk.Button(area, text="取消", command=window.destroy).pack(side="right", padx=8)
        self.root.wait_window(window)
        return result

    @staticmethod
    def settlement_text(items, balances, transfers):
        lines = ["结算明细"]
        for item in items:
            row = item["snapshot"]
            pair = item["paired_entry_id"]
            group = f" · 对应 #{pair}" if pair is not None else ""
            reason = INCLUSION[item["inclusion_reason"]]
            lines.append(
                f"#{row['id']}  {row['title']}  {money(row['amount_cent'])} 元 · {reason}{group}"
            )
        lines.extend(("", "成员净额（正数应收，负数应付）"))
        lines.extend(f"成员 {member}  {money(amount)} 元" for member, amount in balances)
        lines.extend(("", "建议转账"))
        if transfers:
            lines.extend(
                f"成员 {payer} → 成员 {payee}  {money(amount)} 元"
                for payer, payee, amount in transfers
            )
        else:
            lines.append("无需转账")
        lines.extend(("", "实际付款情况未记录。"))
        return "\n".join(lines)

    def show_preview(self, preview, omitted):
        window = tk.Toplevel(self.root)
        window.title("结算预览")
        window.geometry("720x520")
        window.transient(self.root)
        window.grab_set()
        panel = ttk.Frame(window, padding=16)
        panel.pack(fill="both", expand=True)
        title = f"{preview.start_date} 至 {preview.end_date} · 尚有 {omitted} 条历史项未纳入"
        ttk.Label(panel, text=title).pack(anchor="w")
        view = tk.Text(panel, wrap="word", height=18)
        view.pack(fill="both", expand=True, pady=10)
        summary = self.settlement_text(preview.items, preview.balances, preview.transfers)
        view.insert("1.0", summary)
        view.configure(state="disabled")

        def confirm():
            try:
                batch = self.app.settlements.confirm(self.token, preview)
            except RuleError as error:
                messagebox.showwarning("结算未保存", str(error), parent=window)
                if "刷新预览" in str(error) or str(error) == "请重新登录":
                    window.destroy()
                    if str(error) == "请重新登录":
                        self.sign_out()
                return
            except sqlite3.Error:
                messagebox.showerror("保存结果待确认", "请用当前窗口重试同一请求。", parent=window)
                return
            window.destroy()
            self.refresh_batches()
            self.refresh_entries()
            messagebox.showinfo("结算已保存", f"结算批次 #{batch}", parent=self.root)

        ttk.Button(panel, text="确认保存", command=confirm).pack(side="right")
        ttk.Button(panel, text="取消", command=window.destroy).pack(side="right", padx=8)

    def batch_detail(self):
        batch = selected_id(self.batches, "结算批次")
        detail = self.app.settlements.detail(self.token, self.ledger, batch)
        summary = self.settlement_text(detail["items"], detail["balances"], detail["transfers"])
        self.text_view(
            f"结算批次 #{batch}",
            f"{detail['start_date']} 至 {detail['end_date']}\n"
            f"确认人 {detail['operator_id']} · {detail['created_at']}\n\n{summary}",
        )

    def draw_statistics(self, panel):
        self.month = tk.StringVar(value=date.today().isoformat()[:7])
        row = ttk.Frame(panel)
        row.pack(fill="x", pady=(0, 8))
        ttk.Label(row, text="月份（YYYY-MM）").pack(side="left")
        ttk.Entry(row, textvariable=self.month, width=14).pack(side="left", padx=7)
        ttk.Button(row, text="查询", command=self.attempt(self.refresh_statistics)).pack(side="left")
        ttk.Button(row, text="设置预算", command=self.attempt(self.set_budget)).pack(
            side="left", padx=7
        )
        self.statistics = tk.Text(panel, wrap="word", font=("Microsoft YaHei UI", 11))
        self.statistics.pack(fill="both", expand=True)
        self.refresh_statistics()

    def refresh_statistics(self):
        result = self.app.queries.statistics(self.token, self.ledger, self.month.get().strip())
        lines = [
            f"已审核收入  {money(result['income'])} 元",
            f"已审核支出  {money(result['expense'])} 元",
        ]
        if result["budget"] is None:
            lines.append("本月预算尚未设置")
        else:
            lines.extend((
                f"月度预算  {money(result['budget'])} 元",
                f"剩余额度  {money(result['remaining'])} 元",
            ))
            if result["budget"]:
                lines.append(f"预算使用率  {result['expense'] / result['budget']:.1%}")
        lines.extend(("", "支出分类"))
        lines.extend(
            f"{category}  {money(value)} 元"
            for category, value in result["categories"].items()
        )
        lines.extend(("", "成员分配"))
        lines.extend(
            f"成员 {member} · 承担 {money(value['expense'])} 元 · 受益 {money(value['income'])} 元"
            for member, value in result["members"].items()
        )
        self.statistics.configure(state="normal")
        self.statistics.delete("1.0", "end")
        self.statistics.insert("1.0", "\n".join(lines))
        self.statistics.configure(state="disabled")

    def set_budget(self):
        values = self.form("设置月度预算", [("amount", "预算金额（元）", "", ())])
        if values:
            self.app.queries.set_budget(
                self.token, self.ledger, self.month.get().strip(), cents(values["amount"])
            )
            self.refresh_statistics()

    def draw_members(self, panel):
        self.toolbar(panel, [
            ("生成邀请", self.invite), ("调整角色", self.change_role),
            ("移除成员", self.remove_member), ("查看日志", self.audit),
            ("退出账本", self.leave_ledger), ("解散账本", self.dissolve_ledger),
        ])
        self.members = table(
            panel, ("id", "phone", "role", "joined", "left"),
            ("成员编号", "手机尾号", "角色", "加入时间（UTC）", "退出时间（UTC）"),
            (82, 84, 105, 220, 220),
        )
        self.refresh_members()

    def refresh_members(self):
        for item in self.members.get_children():
            self.members.delete(item)
        for row in self.app.ledgers.members(self.token, self.ledger):
            self.members.insert("", "end", values=(
                row["user_id"], "尾号 " + row["phone"][-4:], ROLE[row["role"]],
                row["joined_at"], row["left_at"] or "",
            ))

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
        member = selected_id(self.members, "成员")
        values = self.form("调整角色", [("role", "新角色", "记账成员", tuple(ROLE.values()))])
        if values:
            self.app.ledgers.change_role(
                self.token, self.ledger, member, code_for(ROLE, values["role"])
            )
            self.reload_ledgers(self.ledger)

    def remove_member(self):
        member = selected_id(self.members, "成员")
        if messagebox.askyesno("移除成员", f"移除成员 #{member}？", parent=self.root):
            self.app.ledgers.remove(self.token, self.ledger, member)
            self.reload_ledgers(self.ledger)

    def leave_ledger(self):
        if messagebox.askyesno("退出账本", "退出当前账本？", parent=self.root):
            self.app.ledgers.leave(self.token, self.ledger)
            self.reload_ledgers()

    def dissolve_ledger(self):
        if messagebox.askyesno("解散账本", "将当前账本解散并设为只读？", parent=self.root):
            self.app.ledgers.dissolve(self.token, self.ledger)
            self.reload_ledgers(self.ledger)

    def audit(self):
        values = self.form("操作日志筛选", [
            ("start", "开始日期", "", ()), ("end", "结束日期", "", ()),
            ("operator", "操作人编号", "", ()), ("object", "对象编号", "", ()),
            ("action", "操作类型", "", ()),
        ])
        if not values:
            return
        events = self.app.queries.audit(
            self.token, self.ledger, start=values["start"].strip(),
            end=values["end"].strip(), operator_id=optional_id(values["operator"]),
            object_id=optional_id(values["object"]), action=values["action"].strip(),
        )
        lines = [
            f"{event['occurred_at']} · 成员 {event['operator_id']} · {event['action']} "
            f"· 对象 #{event['object_id']} {event['reason']}\n"
            f"{self.format_audit_change(event)}"
            for event in events
        ]
        self.text_view("操作日志", "\n\n".join(lines) or "没有符合条件的日志。")

    @staticmethod
    def format_audit_change(event):
        changes = []
        for column, label in (("before_content", "修改前"), ("after_content", "修改后")):
            if event[column]:
                data = json.loads(event[column])
                changes.append(f"{label}  {json.dumps(data, ensure_ascii=False)}")
        return "\n".join(changes)
