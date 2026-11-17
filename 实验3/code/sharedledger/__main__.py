"""Start the desktop application with python -m sharedledger."""

import argparse
from pathlib import Path
import tkinter as tk

from .application import Application
from .desktop import Desktop


def main():
    parser = argparse.ArgumentParser(description="多人共享记账桌面应用")
    parser.add_argument("--db", type=Path, default=Path("shared-ledger.sqlite3"))
    args = parser.parse_args()
    root = tk.Tk()
    Desktop(root, Application(args.db))
    root.mainloop()


if __name__ == "__main__":
    main()
