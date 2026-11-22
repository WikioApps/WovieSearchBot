"""Local operator catalog browser. Run on a desktop, not the small headless VPS."""

from __future__ import annotations

import argparse
import asyncio
from concurrent.futures import Future
from pathlib import Path
import sys
import threading

from .config import Settings
from .store import Store, clean_error


class Operator:
    """Own all async clients on one background event loop."""

    def __init__(self, settings):
        self.settings = settings
        self.store = Store(settings)
        self.client = None
        self.child = None

    async def connect(self):
        from telethon import TelegramClient
        from telethon.sessions import StringSession

        if not self.settings.session_string:
            raise ValueError(
                "The desktop needs an operator SESSION_STRING, API_ID and API_HASH."
            )
        self.client = TelegramClient(
            StringSession(self.settings.session_string),
            self.settings.api_id,
            self.settings.api_hash,
            receive_updates=False,
            flood_sleep_threshold=0,
        )
        await self.client.connect()
        if not await self.client.is_user_authorized():
            raise ValueError("The operator session has expired. Create a new session.")
        me = await self.client.get_me()
        if me.bot or me.id not in self.settings.admins:
            raise ValueError("The session must belong to a user listed in ADMINS.")
        await self.store.connect()
        return (
            f"Connected as operator {me.id}. Catalog metadata stays on this computer."
        )

    async def search(self, query, page):
        # Recheck authorization, including session revocation, before every catalog read.
        me = await self.client.get_me()
        if not me or me.bot or me.id not in self.settings.admins:
            raise ValueError("Operator session is no longer authorized.")
        return await self.store.search(query, page=page)

    async def transfer(self, record, target, name, mode, work_dir, max_mib):
        # Isolate work-file downloads from the GUI; no shell interpolation is used.
        command = [
            sys.executable,
            "-m",
            "wovie.media",
            str(record["source_chat"]),
            str(record["message_id"]),
            str(target),
            "--mode",
            mode,
            "--work-dir",
            str(work_dir),
            "--max-mib",
            str(max_mib),
        ]
        if name:
            command.extend(["--name", name])
        self.child = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            stdout, stderr = await self.child.communicate()
            if self.child.returncode:
                # Only show a bounded sanitized error, never a whole traceback or environment.
                lines = stderr.decode(errors="replace").strip().splitlines()
                raise ValueError(
                    clean_error(lines[-1] if lines else "Transfer failed.")
                )
            return (
                stdout.decode(errors="replace").strip()[-500:] or "Transfer completed."
            )
        finally:
            self.child = None

    async def close(self):
        if self.child and self.child.returncode is None:
            self.child.terminate()
            try:
                await asyncio.wait_for(self.child.wait(), 5)
            except asyncio.TimeoutError:
                self.child.kill()
                await self.child.wait()
        if self.client:
            await self.client.disconnect()
        await self.store.close()


class Desktop:
    def __init__(self, root, settings):
        import tkinter as tk
        from tkinter import ttk

        self.root, self.tk, self.ttk = root, tk, ttk
        self.operator = Operator(settings)
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(
            target=self.loop.run_forever, name="wovie-desktop", daemon=True
        )
        self.thread.start()
        self.rows = {}
        self.page = 0
        self.more = False
        self.busy = False
        self.closing = False
        root.title("Wovie · Operator catalog")
        root.geometry("1040x620")
        root.minsize(760, 480)
        self.status = tk.StringVar(value="Authorizing operator session…")
        self.query = tk.StringVar()
        frame = ttk.Frame(root, padding=14)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame, text="Wovie · Operator catalog", font=("TkDefaultFont", 17, "bold")
        ).pack(anchor="w")
        ttk.Label(
            frame,
            text="Local administration · private results remain in this operator window",
        ).pack(anchor="w", pady=(3, 12))
        search = ttk.Frame(frame)
        search.pack(fill="x")
        entry = ttk.Entry(search, textvariable=self.query)
        entry.pack(side="left", fill="x", expand=True)
        entry.bind("<Return>", lambda _: self.search(0))
        self.search_button = ttk.Button(
            search, text="Search", command=lambda: self.search(0)
        )
        self.search_button.pack(side="left", padx=(8, 0))
        ttk.Label(
            frame,
            text="Examples: Dune year:2021 · lang:hindi quality:1080p · series s01e03",
        ).pack(anchor="w", pady=(5, 8))
        columns = ("title", "year", "quality", "language", "size")
        self.tree = ttk.Treeview(
            frame, columns=columns, show="headings", selectmode="browse", height=14
        )
        for name, title, width in [
            ("title", "Title", 410),
            ("year", "Year", 70),
            ("quality", "Quality", 85),
            ("language", "Languages", 150),
            ("size", "Size", 100),
        ]:
            self.tree.heading(name, text=title)
            self.tree.column(name, width=width, minwidth=50)
        self.tree.pack(fill="both", expand=True)
        navigation = ttk.Frame(frame)
        navigation.pack(fill="x", pady=8)
        self.previous = ttk.Button(
            navigation, text="‹ Previous", command=lambda: self.search(self.page - 1)
        )
        self.previous.pack(side="left")
        self.next = ttk.Button(
            navigation, text="Next ›", command=lambda: self.search(self.page + 1)
        )
        self.next.pack(side="left", padx=6)
        self.page_label = ttk.Label(navigation, text="Page 1")
        self.page_label.pack(side="left", padx=10)
        self.transfer_button = ttk.Button(
            navigation, text="Transfer selected…", command=self.transfer_dialog
        )
        self.transfer_button.pack(side="right")
        ttk.Label(frame, textvariable=self.status, wraplength=980).pack(
            anchor="w", fill="x"
        )
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.submit(self.operator.connect(), lambda message: self.connected(message))

    def connected(self, message):
        self.status.set(message)
        self.search(0)

    def controls(self, enabled):
        for widget in (self.search_button, self.transfer_button):
            widget.configure(state="normal" if enabled else "disabled")
        self.previous.configure(
            state="normal" if enabled and self.page > 0 else "disabled"
        )
        self.next.configure(state="normal" if enabled and self.more else "disabled")

    def submit(self, coroutine, on_success):
        if self.busy or self.closing:
            coroutine.close()
            return
        self.busy = True
        self.controls(False)
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        self.root.after(100, lambda: self.poll(future, on_success))

    def poll(self, future: Future, on_success):
        if self.closing:
            return
        if not future.done():
            self.root.after(100, lambda: self.poll(future, on_success))
            return
        self.busy = False
        try:
            result = future.result()
        except Exception as exc:
            self.status.set(clean_error(exc))
        else:
            on_success(result)
        self.controls(not self.busy)

    def search(self, page):
        if page < 0 or self.busy:
            return
        self.status.set("Searching…")

        def display(result):
            rows, self.more = result
            self.page = page
            self.rows = {record["_id"]: record for record in rows}
            self.tree.delete(*self.tree.get_children())
            for record in rows:
                size = record.get("file_size", 0) / 1048576
                self.tree.insert(
                    "",
                    "end",
                    iid=record["_id"],
                    values=(
                        record.get("title") or record["filename"],
                        record.get("year") or "—",
                        record.get("quality") or "—",
                        ", ".join(record.get("languages", [])) or "—",
                        f"{size:.1f} MiB",
                    ),
                )
            self.page_label.configure(text=f"Page {page + 1}")
            self.status.set(
                f"{len(rows)} results. Select a file for an authorized transfer."
            )

        self.submit(self.operator.search(self.query.get().strip(), page), display)

    def transfer_dialog(self):
        from tkinter import filedialog, messagebox, simpledialog

        selected = self.tree.selection()
        if self.busy or not selected:
            return
        record = self.rows[selected[0]]
        target = simpledialog.askstring(
            "Target",
            "Numeric Telegram destination ID (your session needs write access):",
            parent=self.root,
        )
        if target is None:
            return
        try:
            target_id = int(target)
            if not target_id:
                raise ValueError
        except ValueError:
            messagebox.showerror(
                "Invalid target",
                "Enter a nonzero numeric Telegram chat ID.",
                parent=self.root,
            )
            return
        name = simpledialog.askstring(
            "Filename", "New filename, or leave blank to preserve it:", parent=self.root
        )
        if name is None:
            return
        mode = simpledialog.askstring(
            "Operation",
            "copy, thumbnail or transcode:",
            initialvalue="copy",
            parent=self.root,
        )
        if mode is None:
            return
        if mode not in ("copy", "thumbnail", "transcode"):
            messagebox.showerror(
                "Invalid operation",
                "Use copy, thumbnail or transcode.",
                parent=self.root,
            )
            return
        directory = filedialog.askdirectory(
            title="Working directory with space for media", parent=self.root
        )
        if not directory:
            return
        max_mib = simpledialog.askinteger(
            "Maximum size",
            "Maximum input/output size in MiB:",
            initialvalue=256,
            minvalue=1,
            maxvalue=4096,
            parent=self.root,
        )
        if not max_mib:
            return
        if not messagebox.askyesno(
            "Confirm transfer",
            f"Transfer {record['filename'][:140]} to {target_id}?\n"
            "This sends a new Telegram message using your operator session.",
            parent=self.root,
        ):
            return
        self.status.set("Transferring… close this window to stop the worker.")
        self.submit(
            self.operator.transfer(
                record, target_id, name.strip(), mode, Path(directory), max_mib
            ),
            self.status.set,
        )

    def close(self):
        if self.closing:
            return
        self.closing = True
        self.controls(False)
        self.status.set("Closing connections…")
        future = asyncio.run_coroutine_threadsafe(self.operator.close(), self.loop)

        def complete():
            if not future.done():
                self.root.after(100, complete)
                return
            self.loop.call_soon_threadsafe(self.loop.stop)
            self.root.destroy()

        self.root.after(100, complete)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    try:
        import tkinter as tk

        settings = Settings.from_env()
        root = tk.Tk()
    except ImportError:
        parser.exit(2, "Install the Python tkinter package on your desktop computer.\n")
    except (ValueError, OSError) as exc:
        parser.exit(2, clean_error(exc) + "\n")
    except Exception:
        parser.exit(
            2,
            "A graphical desktop/display is required. Run this on your computer, not a headless VPS.\n",
        )
    Desktop(root, settings)
    root.mainloop()


if __name__ == "__main__":
    main()
