import json
import sys
import threading
import time
from tkinter import messagebox
from pathlib import Path

from app_updater import check_update, prepare_update, launch_helper, load_job, cleanup_updates


class UpdateController:
    def __init__(self, app):
        self.app = app
        self.available = None
        self.checking = self.installing = False
        self.notice_pending = False
        self.button = app._button(app.root, 'CHECK UPDATE', self.clicked)
        self.button.place(relx=.98, rely=.98, anchor='se')

        if getattr(sys, 'frozen', False):
            app.root.after(1800, self.check)

    def clicked(self):
        if self.installing or self.checking:
            return
        if self.available:
            self.install()
        else:
            self.check(manual=True)

    def check(self, manual=False):
        if self.checking or self.installing:
            return
        self.checking = True
        self.button.configure(text='CHECKING...', state='disabled')
        def worker():
            try:
                cleanup_updates()
                self.app.events.put(('update_checked', (check_update(), manual)))
            except Exception as error:
                self.app.events.put(('update_check_error', (str(error), manual)))
        threading.Thread(target=worker, daemon=True).start()

    def install(self):
        if self.app.busy or (hasattr(self.app,'setup_dispatcher') and self.app.setup_dispatcher.pending_count):
            messagebox.showinfo('HAZZAA UPDATE', 'Finish the current installation or download before updating.', parent=self.app.root)
            return
        if not getattr(sys, 'frozen', False):
            messagebox.showinfo('HAZZAA UPDATE', 'App updates are available in the Windows EXE build.', parent=self.app.root)
            return
        self.app.busy = self.installing = True
        self.button.configure(text='DOWNLOADING 0%', state='disabled')
        envelope, release = self.available
        self.app._log('Downloading app update '+release['version']+'...')
        def worker():
            try:
                last = -1
                def progress(percent):
                    nonlocal last
                    if percent != last:
                        self.app.events.put(('update_progress', percent)); last = percent
                job_path = prepare_update(envelope, Path(sys.executable), progress)
                helper = launch_helper(job_path)
                self.app.events.put(('update_handoff', (job_path, helper, time.monotonic()+30)))
            except Exception as error:
                self.app.events.put(('update_install_error', str(error)))
        threading.Thread(target=worker, daemon=True).start()

    def notify_if_idle(self):
        setup_busy=hasattr(self.app,'setup_dispatcher') and self.app.setup_dispatcher.pending_count
        if self.notice_pending and not self.app.busy and not self.installing and not setup_busy:
            self.notice_pending = False
            release = self.available[1]
            text = 'Version '+release['version']+' is available.\n\n'+release.get('notes', '')+'\n\nUse UPDATE in the lower-right corner to install and restart.'
            messagebox.showinfo('HAZZAA UPDATE AVAILABLE', text, parent=self.app.root)

    def handle(self, kind, payload):
        if kind == 'update_checked':
            self.available, manual = payload
            self.checking = False
            self.button.configure(text='UPDATE' if self.available else 'CHECK UPDATE', state='normal')
            if self.available:
                self.notice_pending = True
                self.app._log('App update available: '+self.available[1]['version'])
            elif manual:
                messagebox.showinfo('HAZZAA UPDATE', 'You have the latest version.', parent=self.app.root)
        elif kind == 'update_check_error':
            error, manual = payload
            self.checking = False
            self.button.configure(text='UPDATE' if self.available else 'RETRY UPDATE', state='normal')
            self.app._log('Update check unavailable: '+error)
            if manual:
                messagebox.showerror('HAZZAA UPDATE', 'Could not check for updates.\n'+error, parent=self.app.root)
        elif kind == 'update_progress':
            self.button.configure(text=f'DOWNLOADING {payload}%')
        elif kind == 'update_install_error':
            self.app.busy = self.installing = False
            self.button.configure(text='UPDATE', state='normal')
            self.app._log('App update failed: '+payload)
            messagebox.showerror('HAZZAA UPDATE', payload+'\nThe current app is still available.', parent=self.app.root)
        elif kind == 'update_handoff':
            job_path, helper, deadline = payload
            stage = Path(job_path).parent
            try:
                _, job, _, _ = load_job(job_path)
                ready = stage/'ready'
                if ready.is_file() and ready.read_text(encoding='ascii') == job['token']:
                    self.app._log('Update verified. Restarting...')
                    self.app._update_exiting = True
                    self.app.root.destroy()
                elif helper.poll() is not None or time.monotonic() > deadline:
                    (stage/'cancel').write_text('cancel', encoding='ascii')
                    if helper.poll() is None:
                        helper.terminate()
                    result = stage/'result.json'
                    detail = json.loads(result.read_text(encoding='utf-8')).get('error') if result.is_file() else None
                    self.handle('update_install_error', detail or 'The update helper could not start. Close other Hazzaa windows and try again.')
                else:
                    self.app.root.after(150, lambda: self.app.events.put(('update_handoff', payload)))
            except Exception as error:
                self.handle('update_install_error', str(error))
