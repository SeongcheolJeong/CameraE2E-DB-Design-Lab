"""Short read-only status probes; lock retries never decode the frozen job."""
import sqlite3
import time

class BudgetExhausted(RuntimeError):pass

def fast_status(registry, identity):
    # Do not call get_run here: decoding large evidence documents is unrelated
    # to cancellation and can contend with evidence-store publication.
    with sqlite3.connect(f'file:{registry.path}?mode=ro',uri=True,timeout=.1,isolation_level=None) as conn:
        conn.execute('PRAGMA query_only=ON')
        row=conn.execute('SELECT status FROM jobs WHERE id=?',(identity,)).fetchone()
    if row is None:raise KeyError(identity)
    return row[0]

class StatusProbe:
    def __init__(self,registry,identity,clock=time.monotonic):
        self.registry=registry;self.identity=identity;self.clock=clock;self.last_ok=clock();self.next_poll=0
    def check(self):
        now=self.clock()
        if now<self.next_poll:return
        self.next_poll=now+2
        try:status=fast_status(self.registry,self.identity)
        except sqlite3.OperationalError as e:
            if not any(w in str(e).lower() for w in ('locked','busy')):raise
            if now-self.last_ok>=60:raise RuntimeError('Status DB unavailable for 60 seconds; completed samples retained') from e
            return
        self.last_ok=now
        if status!='running':raise ValueError('Cancelled; completed samples retained')
