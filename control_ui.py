import threading
import tkinter as tk

from app_control import cached_policy, refresh_policy


class ControlController:
    def __init__(self,app):
        self.app=app
        self.policy=cached_policy()
        self.checking=True
        self.in_flight=False
        self.job=None
        self.panel=tk.Frame(app.root,bg='black')
        app._label(self.panel,'HAZZAA PLUGINS').pack(pady=(90,24))
        self.title=app._label(self.panel,'CHECKING APPLICATION STATUS')
        self.title.pack(pady=12)
        self.message=app._label(self.panel,'')
        self.message.configure(wraplength=650,justify='center')
        self.message.pack(padx=24,pady=20)
        app._button(self.panel,'CHECK STATUS',self.check).pack(pady=8)
        app._button(self.panel,'CHECK UPDATE',app.updater.clicked).pack(pady=8)
        app._button(self.panel,'EXIT',self.close).pack(pady=8)
        self.display()
        self.check()

    @property
    def blocked(self):
        return self.checking or bool(self.policy and not self.policy['enabled'])

    def check(self):
        if self.in_flight:
            return
        self.in_flight=True
        def work():
            try:
                result=(refresh_policy(),None)
            except Exception as error:
                result=(None,str(error))
            self.app.events.put(('control_checked',result))
        threading.Thread(target=work,daemon=True).start()

    def receive(self,result):
        self.in_flight=False
        self.checking=False
        policy,error=result
        if policy is not None:
            self.policy=policy
        if self.policy and not self.policy['enabled']:
            self.app.download_cancel.set()
            self.app.preset_cancel.set()
        self.display()
        if self.job is not None:
            self.app.root.after_cancel(self.job)
        self.job=self.app.root.after(30000,self.check)

    def display(self):
        if not self.blocked:
            if self.panel.grab_current()==self.panel:
                self.panel.grab_release()
            self.panel.place_forget()
            return
        self.title.configure(text='CHECKING APPLICATION STATUS' if self.checking else 'APPLICATION DISABLED')
        self.message.configure(text='Please wait.' if self.checking else self.policy['message'])
        already_visible=self.panel.winfo_manager()=='place'
        self.panel.place(x=0,y=0,relwidth=1,relheight=1)
        self.panel.lift()
        if not already_visible:
            self.panel.grab_set()

    def close(self):
        if self.app.busy or self.app.setup_dispatcher.pending_count:
            self.message.configure(text='An installation is finishing. Wait for it to complete before closing.')
            return
        self.app.root.destroy()

    def cleanup(self):
        if self.job is not None:
            self.app.root.after_cancel(self.job)
            self.job=None
