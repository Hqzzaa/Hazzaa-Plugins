from collections import deque
from dataclasses import dataclass,field
import threading


@dataclass
class SetupTicket:
    key:str
    name:str
    run:object
    workspace:object
    finished:object
    ready:threading.Event=field(default_factory=threading.Event)
    state:str='queued'
    code:object=None
    verified:object=None
    error:object=None


class SetupDispatcher:
    def __init__(self,log=lambda text:None,event=lambda kind,value:None):
        self.log=log;self.event=event
        self._condition=threading.Condition();self._queue=deque();self._inflight={}
        self._active=None;self._thread=None;self._accepting=True

    def submit(self,key,name,run,workspace,finished=lambda *args:None):
        with self._condition:
            existing=self._inflight.get(key)
            if existing is not None:
                self.log('SETUP ALREADY OPEN OR QUEUED: '+name+' — no second copy was started.')
                return existing,'duplicate'
            if not self._accepting:raise RuntimeError('Setup queue is closed.')
            ticket=SetupTicket(key,name,run,workspace,finished)
            workspace.retain();self._inflight[key]=ticket
            queued=self._active is not None or bool(self._queue)
            self._queue.append(ticket)
            if self._thread is None:
                self._thread=threading.Thread(target=self._worker,daemon=True,name='Hazzaa setup queue')
                self._thread.start()
            self._condition.notify()
        if queued:
            self.log('SETUP QUEUED: '+name+' — other plug-ins/scripts continue.')
            self.event('setup_queue_count',self.queued_count)
            return ticket,'queued'


        ticket.ready.wait()
        return ticket,ticket.state

    @property
    def queued_count(self):
        with self._condition:return len(self._queue)

    @property
    def pending_count(self):
        with self._condition:return len(self._inflight)

    @property
    def status(self):
        with self._condition:
            manual=int(self._active is not None and self._active.state=='manual')
            return manual,len(self._queue)

    @property
    def automatic_active(self):
        with self._condition:return self._active is not None and self._active.state=='running'

    @property
    def retained_paths(self):
        with self._condition:return [ticket.workspace.name for ticket in self._inflight.values()]

    def close(self):

        with self._condition:
            self._accepting=False
            if self._active is not None:self._active.workspace.keep=True
            while self._queue:
                ticket=self._queue.popleft();ticket.workspace.keep=True;ticket.workspace.release()
                self.log('Queued setup files retained for later: '+ticket.workspace.name)
                self._inflight.pop(ticket.key,None)
            self._condition.notify_all()

    def _worker(self):
        while True:
            with self._condition:
                while not self._queue and self._accepting:self._condition.wait()
                if not self._queue:return
                ticket=self._queue.popleft();self._active=ticket
                ticket.state='running'
            self.event('setup_queue_count',self.queued_count)
            try:
                ticket.code,ticket.verified=ticket.run()
                if getattr(ticket.code,'handed_off',False):
                    ticket.state='manual';ticket.ready.set()
                    self.log('LEFT OPEN FOR YOU: '+ticket.name+' — continuing other items; no restart or field editing.')
                    self.event('setup_queue_count',self.queued_count)
                    try:
                        ticket.code.wait_until_closed()
                    except Exception as error:


                        ticket.workspace.keep=True
                        self.log('Setup tracking unavailable. Retained files: '+ticket.workspace.name)
                        with self._condition:self._accepting=False
                        self.close()
                        ticket.finished(None,None,error)
                        self.event('setup_queue_paused',None)
                    else:ticket.finished(ticket.code,None,None)


                else:
                    ticket.state='finished'
                    ticket.finished(ticket.code,ticket.verified,None)
            except Exception as error:
                ticket.error=error;ticket.state='failed'
                if getattr(error,'retry_safe',True) is False:
                    ticket.workspace.keep=True
                    with self._condition:self._accepting=False
                    self.close()
                    self.event('setup_queue_paused',None)
                try:ticket.finished(None,None,error)
                except Exception:pass
            finally:
                ticket.ready.set()
                with self._condition:
                    self._inflight.pop(ticket.key,None);self._active=None
                try:ticket.workspace.release()
                except OSError:self.log('Setup files could not be cleaned: '+ticket.workspace.name)
                self.event('setup_queue_count',self.queued_count)
