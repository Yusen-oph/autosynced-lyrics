"""Window listing every AI translation request: when, which song, which model, how many
lines, tokens, and what it cost - with totals for this month and all time."""
import time
import tkinter as tk
from tkinter import ttk

import ai_translate
import theme


def _money(x: float) -> str:
    if x == 0:
        return "$0"
    if x < 0.0001:
        return "<$0.0001"
    return f"${x:.4f}" if x < 1 else f"${x:,.2f}"


_open = None


def show(master):
    """Open the usage window, or bring the open one to the front."""
    global _open
    if _open is not None and _open.exists():
        _open.focus()
    else:
        _open = UsageWindow(master)


class UsageWindow:
    def __init__(self, master):
        self.win = tk.Toplevel(master)
        self.win.title("AI translation usage")
        self.win.attributes("-topmost", True)
        theme.title_bar(self.win)
        outer = ttk.Frame(self.win, padding=14)
        outer.pack(fill="both", expand=True)

        self.summary = ttk.Frame(outer)
        self.summary.pack(fill="x")
        self.cards = {}
        for key, label in (("month", "This month"), ("all", "All time"), ("lines", "Lines translated")):
            card = ttk.Frame(self.summary, padding=(0, 0, 28, 0))
            card.pack(side="left")
            ttk.Label(card, text=label, foreground=theme.MUTED).pack(anchor="w")
            value = ttk.Label(card, font=("Segoe UI", 16, "bold"))
            value.pack(anchor="w")
            self.cards[key] = value

        cols = ("time", "song", "model", "lines", "tokens", "cost")
        box = ttk.Frame(outer)
        box.pack(fill="both", expand=True, pady=(12, 8))
        self.table = ttk.Treeview(box, columns=cols, show="headings", height=14)
        for col, title, width, anchor in (
                ("time", "Date", 130, "w"), ("song", "Song", 290, "w"), ("model", "Model", 95, "w"),
                ("lines", "Lines", 55, "e"), ("tokens", "Tokens in / out", 120, "e"),
                ("cost", "Cost", 85, "e")):
            self.table.heading(col, text=title, anchor=anchor)
            self.table.column(col, width=width, anchor=anchor, stretch=col == "song")
        sb = ttk.Scrollbar(box, command=self.table.yview)
        self.table.configure(yscrollcommand=sb.set)
        self.table.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        ttk.Label(outer, foreground=theme.MUTED, wraplength=760, justify="left", text=(
            "Costs are worked out from the tokens each request used, at Anthropic's list prices "
            "(Haiku 5.5: $0.10 in / $0.50 out, Sonnet 5.5: $2 in / $10 out, per million tokens). "
            "Each line is translated only once; replays are free. Your exact bill is at "
            "console.anthropic.com.")).pack(fill="x")
        btns = ttk.Frame(outer)
        btns.pack(fill="x", pady=(10, 0))
        ttk.Button(btns, text="Refresh", command=self.refresh).pack(side="left")
        ttk.Button(btns, text="Close", command=self.win.destroy).pack(side="right")
        self.refresh()

    def refresh(self):
        log = ai_translate.usage_log()
        month = time.strftime("%Y-%m")
        this_month = [e for e in log if e.get("time", "").startswith(month)]
        self.cards["month"].configure(
            text=f"{_money(sum(e['cost'] for e in this_month))}  ·  {len(this_month)} song(s)")
        self.cards["all"].configure(text=_money(sum(e["cost"] for e in log)))
        self.cards["lines"].configure(text=f"{sum(e.get('lines', 0) for e in log):,}")
        self.table.delete(*self.table.get_children())
        for e in reversed(log):                         # newest first
            self.table.insert("", "end", values=(
                e.get("time", ""), f"{e.get('title', '')} — {e.get('artist', '')}",
                ai_translate.MODEL_NAMES.get(e.get("model"), e.get("model", "")),
                e.get("lines", 0), f"{e.get('in', 0):,} / {e.get('out', 0):,}", _money(e.get("cost", 0))))
        if not log:
            self.table.insert("", "end", values=("", "No AI translations yet", "", "", "", ""))

    def exists(self) -> bool:
        try:
            return bool(self.win.winfo_exists())
        except tk.TclError:
            return False

    def focus(self):
        self.refresh()
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()
