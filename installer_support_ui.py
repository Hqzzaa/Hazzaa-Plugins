import os
import tkinter as tk
from tkinter import filedialog
from installer_help import setup_steps
from product_deployment import bcc_library_directory,choose_bcc_library


class SetupHelpWindow:
    def __init__(self,app,payload):
        self.app=app
        self.close_job=None
        self.library_override=None
        self.root=tk.Toplevel(app.root);self.root.title('HAZZAA SETUP ASSISTANCE')
        self.root.geometry('850x410');self.root.minsize(700,350);self.root.configure(bg='black')
        self.heading=app._label(self.root,'');self.heading.pack(fill='x',padx=16,pady=12)
        self.message=tk.Text(self.root,height=5,bg='#151515',fg='white',font=('Courier New',11,'bold'),wrap='word',relief='flat')
        self.message.pack(fill='both',expand=True,padx=16,pady=4)
        self.fields=tk.Frame(self.root,bg='black');self.fields.pack(fill='x',padx=16,pady=8)
        self.status=app._label(self.root,'');self.status.pack(fill='x',padx=16,pady=8)
        self.footer=tk.Frame(self.root,bg='black');self.footer.pack(fill='x',padx=16,pady=12)
        app._button(self.footer,'CLOSE HELP',self.close).pack(side='right')
        self.root.protocol('WM_DELETE_WINDOW',self.close)
        self.files_button=app._button(self.footer,'OPEN FILES',self.open_files)
        self.lib_button=app._button(self.footer,'OPEN LIB',self.open_lib)
        self.browse_button=app._button(self.footer,'BROWSE LIB',self.browse_lib)
        self.footer.pack(side='bottom',before=self.message,fill='x',padx=16,pady=12)
        self.status.pack(side='bottom',before=self.message,fill='x',padx=16,pady=8)
        self.fields.pack(side='bottom',before=self.message,fill='x',padx=16,pady=8)
        self.update(payload)
        app._apply_cursor(self.root);app._set_dark_titlebar(self.root)

    def update(self,payload):
        if self.close_job is not None:
            self.root.after_cancel(self.close_job);self.close_job=None
        self.payload=payload;self.heading.configure(text=payload.get('product') or payload['name'])
        self.message.configure(state='normal');self.message.delete('1.0','end')
        self.message.insert('end',setup_steps(payload['name'],payload['reason']))
        self.message.configure(state='disabled')
        for child in self.fields.winfo_children():child.destroy()
        values=payload.get('values',{})
        height=max(410,300+50*sum(len(v) for v in values.values()))
        self.root.geometry(f'850x{height}')
        for kind,candidates in values.items():
            for number,value in enumerate(candidates,1):
                row=tk.Frame(self.fields,bg='black');row.pack(fill='x',pady=3)
                self.app._label(row,kind.upper()+(f' {number}' if len(candidates)>1 else '')).pack(side='left',padx=(0,10))
                entry=tk.Entry(row,bg='#202020',fg='white',readonlybackground='#202020',font=('Courier New',11,'bold'),relief='flat')
                entry.insert(0,value);entry.configure(state='readonly');entry.pack(side='left',fill='x',expand=True)
                self.app._button(row,'COPY',lambda v=value:self.copy(v)).pack(side='right',padx=(8,0))
        if payload.get('attachments'):self.files_button.pack(side='left')
        else:self.files_button.pack_forget()
        if payload['name'].casefold()=='continuum_2019_adobe_12_0_0_windows.exe':
            self.lib_button.pack(side='left',padx=6);self.browse_button.pack(side='left')
        else:self.lib_button.pack_forget();self.browse_button.pack_forget()
        self.status.configure(text='COMPLETE THE OPEN SETUP WINDOW')
        self.root.lift()

    def copy(self,value):
        self.root.clipboard_clear();self.root.clipboard_append(value)

    def open_files(self):
        folder=self.payload.get('attachments')
        if folder:self.open_folder(folder)

    def open_folder(self,folder):
        from pathlib import Path
        try:
            if not Path(folder).is_dir():raise FileNotFoundError(str(folder))
            os.startfile(str(folder))
            self.app.opened_installer_folders.add(str(Path(folder).absolute()).casefold())
        except OSError:self.status.configure(text='FOLDER NOT FOUND — CHECK ITS LOCATION.')

    def open_lib(self):
        folder=self.library_override or bcc_library_directory()
        if folder:self.open_folder(folder)
        else:self.browse_lib()

    def browse_lib(self):
        folder=filedialog.askdirectory(parent=self.root,title='Select Boris lib folder',mustexist=True)
        if not folder:return
        try:self.library_override=choose_bcc_library(folder)
        except (OSError,ValueError):
            self.status.configure(text='SELECT THE BORIS lib FOLDER OR ITS PARENT.');return
        self.open_folder(self.library_override)

    def finished(self,ok,reason):
        self.status.configure(text='SETUP FINISHED' if ok else 'SETUP STOPPED — COPY LOG FOR DETAILS')
        if ok:self.close_job=self.root.after(1800,self.close)

    def handed_off(self):
        self.status.configure(text='LEFT OPEN FOR YOU — OTHER ITEMS CONTINUE')

    def manual_closed(self):
        self.status.configure(text='SETUP CLOSED')
        self.close_job=self.root.after(1800,self.close)

    def close(self):
        if self.close_job is not None:
            self.root.after_cancel(self.close_job);self.close_job=None
        self.root.destroy()
