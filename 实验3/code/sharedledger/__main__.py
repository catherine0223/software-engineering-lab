"""Start the desktop application with python -m sharedledger."""

import argparse
from pathlib import Path
import tkinter as tk

from .application import Application
from .demo import DEMO_ACCOUNTS, demo_login
from .desktop import Desktop
from .storage import RuleError


DEMO_PATH = Path(__file__).resolve().parent.parent / ".sharedledger-demo" / "ledger.sqlite3"


def main():
    """Parse startup options and run the desktop application."""
    parser = argparse.ArgumentParser(description="多人共享记账桌面应用")
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--db", type=Path, help="指定普通模式使用的数据库")
    target.add_argument(
        "--demo", choices=DEMO_ACCOUNTS, help="使用独立演示库直接进入管理员或记账成员账号"
    )
    args = parser.parse_args()
    path = DEMO_PATH if args.demo else args.db or Path("shared-ledger.sqlite3")
    app = Application(path)
    try:
        token = demo_login(app, args.demo) if args.demo else None
    except RuleError as error:
        parser.exit(1, f"{error}\n")
    root = tk.Tk()
    desktop = Desktop(root, app)
    if token:
        root.title("多人共享记账 · 演示模式")
        desktop.token = token
        desktop.show_workspace(pick_ledger=len(app.ledgers.list(token)) != 1)
    root.mainloop()


if __name__ == "__main__":
    main()
