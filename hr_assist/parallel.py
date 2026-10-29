"""Bounded candidate scheduling and one atomic model budget per batch."""
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
import threading

from .storage import BusinessError


class BatchStopped(BusinessError):
    """Another worker stopped the batch; this worker has no new error to report."""
    def __init__(self):
        super().__init__('BATCH_STOPPED','同批次其他任务已暂停；本项未继续发送模型请求，已完成步骤保留。')


class BatchControl:
    def __init__(self, maximum, concurrency, shutdown):
        self.maximum=maximum
        self.concurrency=concurrency
        self.shutdown=shutdown
        self.lock=threading.Lock()
        self.calls=0
        self.error=None
        self.phase=None

    def stop(self,error,phase=None):
        with self.lock:
            if self.error is None:
                self.error=error
                self.phase=phase
            elif self.phase is None and (self.error is error or error.__cause__ is self.error):
                self.error=error
                self.phase=phase

    def check(self):
        with self.lock:
            self._check()

    def _check(self):
        if self.error is not None:
            raise BatchStopped()
        if self.shutdown.is_set():
            self.error=BusinessError('SHUTDOWN','服务关闭，未完成项将在后续任务继续。')
            raise self.error

    def reserve(self):
        with self.lock:
            self._check()
            if self.calls>=self.maximum:
                self.error=BusinessError('MODEL_BUDGET','本批次模型调用上限已达到；姓名岗位识别与评分共用上限，已完成结果保留，请在后续任务继续。')
                raise self.error
            self.calls+=1

    def stopped(self):
        with self.lock:
            if self.shutdown.is_set() and self.error is None:
                self.error=BusinessError('SHUTDOWN','服务关闭，未完成项将在后续任务继续。')
            return self.error is not None


def bounded_candidates(items,worker,control):
    """Yield completed work, never queue more candidates than can run now.

    Executor shutdown joins *all* workers even if the caller fails while handling
    a result. Therefore report export and the run lock cannot outlive a worker.
    """
    iterator=iter(items)
    with ThreadPoolExecutor(max_workers=control.concurrency,thread_name_prefix='hr-resume') as pool:
        pending=set()
        exhausted=False
        while pending or not exhausted:
            while not exhausted and len(pending)<control.concurrency and not control.stopped():
                try:
                    item=next(iterator)
                except StopIteration:
                    exhausted=True
                    break
                pending.add(pool.submit(worker,item))
            if not pending:
                return
            done,pending=wait(pending,return_when=FIRST_COMPLETED)
            for future in done:
                yield future.result()
