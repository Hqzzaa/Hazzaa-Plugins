import json
import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox

from ae_paths import common_plugin_directory, support_for_target
from installed_products import installed_inventory, remove_recorded_files, run_vendor_uninstaller


class UninstallWindow:
    def __init__(self,app,index):
        self.app=app;self.index=index;self.rows=[];self.selected={};self.category='Plug-ins'
        self.events=queue.Queue();self.running=False;self.closed=False
        self.root=tk.Toplevel(app.root);self.root.title('HAZZAA UNINSTALL')
        self.root.geometry('860x620');self.root.minsize(700,480);self.root.configure(bg='#000000')
        self.root.protocol('WM_DELETE_WINDOW',self.close)
        self.query=tk.StringVar();self.query.trace_add('write',lambda *_:self.render())
        heading=tk.Frame(self.root,bg='#000000');heading.pack(fill='x',padx=16,pady=12)
        app._label(heading,'INSTALLED ON THIS PC').pack(side='left')
        app._button(heading,'REFRESH',self.refresh).pack(side='right')
        tabs=tk.Frame(self.root,bg='#000000');tabs.pack(fill='x',padx=16)
        self.tabs={}
        for category in ('Plug-ins','Scripts'):
            button=app._button(tabs,category.upper(),lambda c=category:self.set_category(c))
            button.pack(side='left',padx=(0,8));self.tabs[category]=button
        entry=tk.Entry(self.root,textvariable=self.query,bg='#202020',fg='white',insertbackground='white',font=('Courier New',11,'bold'),relief='flat')
        entry.pack(fill='x',padx=16,pady=10,ipady=6)
        footer=tk.Frame(self.root,bg='#000000');footer.pack(side='bottom',fill='x',padx=16,pady=14)
        self.status=app._label(footer,'Scanning installed files...');self.status.pack(side='left',fill='x',expand=True)
        self.remove_button=app._button(footer,'REVIEW UNINSTALL',self.review);self.remove_button.pack(side='right')
        self.canvas=tk.Canvas(self.root,bg='#000000',highlightthickness=0,height=1)
        scroll=tk.Scrollbar(self.root,command=self.canvas.yview)
        scroll.pack(side='right',fill='y',padx=(0,14));self.canvas.pack(fill='both',expand=True,padx=(16,0))
        self.canvas.configure(yscrollcommand=scroll.set)
        self.list=tk.Frame(self.canvas,bg='#000000')
        self.window=self.canvas.create_window(0,0,anchor='nw',window=self.list)
        self.list.bind('<Configure>',lambda e:self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<Configure>',lambda e:self.canvas.itemconfigure(self.window,width=e.width))
        app._apply_cursor(self.root);app._set_dark_titlebar(self.root)
        self.job=self.root.after(100,self.poll)
        self.refresh()

    def roots(self):
        plugins={Path(p).absolute() for _,p in self.app.installations}
        current=Path(self.app.target_var.get()).absolute()
        plugins.add(current)
        plugins.update(common_plugin_directory(p) for p in list(plugins))
        scripts=set()
        for plugin in plugins:
            support=support_for_target(plugin)
            if support:scripts.add(support/'Scripts')
            elif plugin==current:scripts.add(plugin.parent/'Scripts' if plugin.name.casefold() in {'plugins','plug-ins'} else plugin/'Scripts')
        cep=Path(os.environ.get('APPDATA',str(Path.home()/'AppData/Roaming')))/'Adobe/CEP/extensions'
        return {'plugins':sorted(plugins),'scripts':sorted(scripts),'cep':[cep]}

    def refresh(self):
        if self.running:return
        self.running=True;self.status.configure(text='SCANNING...');self.remove_button.configure(state='disabled')
        roots=self.roots()
        def worker():
            try:self.events.put(('inventory',installed_inventory(roots,self.index)))
            except Exception as error:self.events.put(('error',str(error)))
        threading.Thread(target=worker,daemon=True).start()

    def set_category(self,value):self.category=value;self.render()

    def visible(self):
        query=self.query.get().strip().casefold()
        return [r for r in self.rows if r['category']==self.category and query in r['name'].casefold()]

    def render(self):
        for child in self.list.winfo_children():child.destroy()
        for category,button in self.tabs.items():button.configure(bg='#444444' if category==self.category else '#202020')
        visible=self.visible()
        for row in visible:
            frame=tk.Frame(self.list,bg='#151515',padx=8,pady=8);frame.pack(fill='x',pady=(0,6))
            var=self.selected.setdefault(row['id'],tk.BooleanVar(master=self.root))
            tk.Checkbutton(frame,text=row['name'],variable=var,bg='#151515',fg='white',selectcolor='#202020',
                activebackground='#151515',activeforeground='white',font=('Courier New',11,'bold'),anchor='w',command=self.count).pack(fill='x')
            detail=row['method']+' | '+str(len(row['files']))+' tracked file(s)'
            self.app._label(frame,detail).pack(anchor='w',padx=28)
        if not visible:self.app._label(self.list,'NO INSTALLED ITEMS FOUND').pack(pady=24)
        self.count()

    def count(self):
        if not self.running:
            count=sum(v.get() for v in self.selected.values())
            self.status.configure(text=f'{len(self.rows)} INSTALLED | {count} SELECTED')

    def review(self):
        if self.running or self.app.busy or (hasattr(self.app,'setup_dispatcher') and self.app.setup_dispatcher.pending_count):return
        chosen=[r for r in self.rows if self.selected.get(r['id']) and self.selected[r['id']].get()]
        if not chosen:return
        if not self.app._installation_preflight():return
        review=tk.Toplevel(self.root);review.title('HAZZAA CONFIRM UNINSTALL');review.geometry('780x520');review.configure(bg='black')
        self.app._label(review,'REVIEW THE SELECTED PRODUCTS AND EXACT FILES').pack(padx=16,pady=12,anchor='w')
        body=tk.Text(review,bg='#151515',fg='white',font=('Courier New',10,'bold'),wrap='word',relief='flat')
        body.pack(fill='both',expand=True,padx=16,pady=8)
        for row in chosen:
            body.insert('end',row['name']+'\n'+row['method']+'\n')
            if row.get('command'):body.insert('end','The official vendor uninstaller may remove this suite from ALL Adobe versions and other hosts.\n')
            for file in row['files']:body.insert('end',file['path']+'\n')
            body.insert('end','\n')
        body.insert('end','Recorded files are backed up before removal. Shared and changed files are preserved. User projects are not scanned.\n')
        body.configure(state='disabled')
        buttons=tk.Frame(review,bg='black');buttons.pack(fill='x',padx=16,pady=12)
        self.app._button(buttons,'CANCEL',review.destroy).pack(side='left')
        def confirm():review.destroy();self.remove(chosen)
        self.app._button(buttons,'UNINSTALL SELECTED',confirm).pack(side='right')
        self.app._apply_cursor(review);self.app._set_dark_titlebar(review)

    def remove(self,chosen):
        if self.app.busy or self.running or (hasattr(self.app,'setup_dispatcher') and self.app.setup_dispatcher.pending_count):return
        if not self.app._installation_preflight():return
        self.running=self.app.busy=True;self.status.configure(text='UNINSTALLING...');self.remove_button.configure(state='disabled')
        rows=list(self.rows)
        def worker():
            failures=[]
            log=lambda message:self.app.events.put(('log',message))
            try:
                direct=[r for r in chosen if not r.get('command')]
                if direct:remove_recorded_files(direct,rows,log)
                for row in chosen:
                    if row.get('command'):
                        try:run_vendor_uninstaller(row,log)
                        except Exception as error:failures.append(row['name']+': '+str(error))
            except Exception as error:failures.append(str(error))
            self.events.put(('removed',failures))
        threading.Thread(target=worker,daemon=True).start()

    def poll(self):
        try:
            while True:
                kind,payload=self.events.get_nowait()
                self.running=False
                self.remove_button.configure(state='normal')
                if kind=='inventory':
                    self.rows=payload;ids={r['id'] for r in payload}
                    self.selected={k:v for k,v in self.selected.items() if k in ids};self.render()
                elif kind=='removed':
                    self.app.busy=False
                    if payload:messagebox.showerror('HAZZAA UNINSTALL','\n'.join(payload),parent=self.root)
                    else:self.app._log('Selected uninstall operations completed. Restart After Effects before checking its menus.')
                    self.selected={};self.refresh()
                elif kind=='error':
                    self.status.configure(text='SCAN FAILED');self.app._log('Installed scan failed: '+payload)
        except queue.Empty:pass
        if not self.closed:self.job=self.root.after(100,self.poll)

    def close(self):
        if self.running:
            messagebox.showinfo('HAZZAA UNINSTALL','Wait for the current scan or uninstall to finish.',parent=self.root);return
        self.closed=True;self.root.after_cancel(self.job);self.root.destroy()
