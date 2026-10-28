import queue
import threading
import tkinter as tk

from plugin_library import LibraryClient, available_items, library_category

FONT = ("Courier New", 11, "bold")


class LibraryWindow:
    def __init__(self, app):
        self.app = app
        self.root = tk.Toplevel(app.root)
        self.root.title("HAZZAA PLUGIN LIBRARY")
        self.root.geometry("780x650")
        self.root.minsize(660, 480)
        self.root.configure(bg="black")
        self.root.transient(app.root)
        self.poll_id = None
        self.root.bind("<Destroy>", self._destroyed, add="+")
        self.queue = queue.Queue()
        self.items = []
        self.selected = {}
        self.loading = False
        top = tk.Frame(self.root, bg="black", padx=18, pady=12)
        top.pack(fill="x")
        app._label(top, "DOWNLOAD LIBRARY").pack(side="left")
        self.refresh_button = app._button(top, "REFRESH", self.refresh)
        self.refresh_button.pack(side="right")
        tabs=tk.Frame(self.root,bg='black')
        tabs.pack(fill='x',padx=18,pady=(0,10))
        self.category='Plug-ins'
        self.category_buttons={}
        for category in ('Plug-ins','Scripts'):
            button=app._button(tabs,category.upper(),lambda value=category:self.set_category(value))
            button.pack(side='left',fill='x',expand=True,padx=(0,6))
            self.category_buttons[category]=button
        self.query = tk.StringVar()
        self.query.trace_add("write", lambda *_: self.render())
        search = tk.Entry(self.root, textvariable=self.query, bg="#202020", fg="white",
                          insertbackground="white", font=FONT, relief="flat")
        search.pack(fill="x", padx=18, ipady=8)
        tk.Label(self.root, text="SEARCH BY NAME", bg="black", fg="white", font=FONT).pack(anchor="w", padx=18)
        area = tk.Frame(self.root, bg="black")
        area.pack(fill="both", expand=True, padx=18, pady=8)

        self.canvas = tk.Canvas(area, bg="black", highlightthickness=0, height=1)
        scrollbar = tk.Scrollbar(area, command=self.canvas.yview)
        scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        self.rows = tk.Frame(self.canvas, bg="black")
        self.window_item = self.canvas.create_window(0, 0, window=self.rows, anchor="nw")
        self.rows.bind("<Configure>", lambda _: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda event: self.canvas.itemconfigure(self.window_item, width=event.width))
        self.root.bind("<MouseWheel>", lambda event: self.canvas.yview_scroll(-int(event.delta / 120), "units"))
        self.status = tk.Label(self.root, text="", bg="black", fg="white", font=FONT, wraplength=720)
        self.status.pack(fill="x", padx=18, pady=6)
        footer = tk.Frame(self.root, bg="black", padx=18, pady=12)
        footer.pack(fill="x")
        app._button(footer, "SELECT ALL", lambda: self.mark(True)).pack(side="left")
        app._button(footer, "CLEAR", lambda: self.mark(False)).pack(side="left", padx=6)
        self.install_button = app._button(footer, "DOWNLOAD + INSTALL", self.install)
        self.install_button.pack(side="right")
        app._apply_cursor(self.root)
        self.root.update_idletasks()
        app._set_dark_titlebar(self.root)
        self.refresh()
        self.poll_id = self.root.after(100, self.poll)

    def _destroyed(self, event):
        if event.widget is self.root and self.poll_id is not None:
            self.root.after_cancel(self.poll_id)
            self.poll_id = None

    def refresh(self):
        if self.loading:
            return
        self.loading = True
        self.refresh_button.configure(state="disabled")
        self.status.configure(text="LOADING LIBRARY...")
        def work():
            try:
                self.queue.put(("catalog", LibraryClient().catalog()))
            except Exception as exc:
                self.queue.put(("error", str(exc)))
        threading.Thread(target=work, daemon=True).start()

    def poll(self):
        if not self.root.winfo_exists():
            return
        try:
            while True:
                kind, value = self.queue.get_nowait()
                self.loading = False
                self.refresh_button.configure(state="normal")
                if kind == "catalog":
                    self.items = available_items(value)
                    self.selected = {item["id"]: self.selected.get(item["id"], tk.BooleanVar(master=self.root)) for item in self.items}
                    self.render()
                else:
                    self.status.configure(text=value + "  |  YOU CAN STILL SELECT FILES FROM PC.")
        except queue.Empty:
            pass
        self.poll_id = self.root.after(100, self.poll)

    def set_category(self,category):
        if category not in self.category_buttons:
            return
        self.category=category
        self.render()

    def visible_items(self):
        query=self.query.get().casefold().strip()
        return [item for item in available_items(self.items)
                if library_category(item)==self.category and query in item['name'].casefold()]

    def render(self):
        for child in self.rows.winfo_children():
            child.destroy()
        for category,button in self.category_buttons.items():
            button.configure(bg='#353535' if category==self.category else '#151515')
        visible = self.visible_items()
        for item in visible:
            text = f"{item['name']}  |  {item['bytes'] / 1024**2:.1f} MB"
            tk.Checkbutton(self.rows, text=text, variable=self.selected[item["id"]],
                command=self.update_status, bg="black", fg="white", activebackground="#202020",
                activeforeground="white", selectcolor="#303030", anchor="w", font=FONT,
                padx=8, pady=9, wraplength=650, justify="left").pack(fill="x")
        self.app._apply_cursor(self.rows)
        self.canvas.yview_moveto(0)
        self.update_status()

    def update_status(self):
        chosen = [item for item in self.items if self.selected[item["id"]].get()]
        self.status.configure(text=f"{len(chosen)} SELECTED  |  {sum(i['bytes'] for i in chosen) / 1024**2:.1f} MB")

    def mark(self, value):
        for item in self.visible_items():
            self.selected[item['id']].set(value)
        self.update_status()

    def install(self):
        chosen = [item for item in self.items if self.selected[item["id"]].get()]
        if chosen and self.app.start_library_install(chosen):
            self.root.destroy()
