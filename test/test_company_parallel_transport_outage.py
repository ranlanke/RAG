from concurrent.futures import ThreadPoolExecutor
import threading

from report_agents.company_transport import bounded_company_invoke,CompanyModelUnavailable


def test_parallel_failed_requests_do_not_each_start_another_retry_cycle():
    entered=threading.Barrier(3);calls=[];lock=threading.Lock()
    def fail(role,prompt,trace):
        with lock:calls.append(prompt)
        entered.wait(timeout=3)
        raise TimeoutError('observed provider outage')
    invoke=bounded_company_invoke(fail,cooldown_seconds=0)
    def job(i):
        try:invoke('read',str(i),None)
        except CompanyModelUnavailable:return
        raise AssertionError('outage cannot return a successful reading')
    with ThreadPoolExecutor(max_workers=3) as workers:list(workers.map(job,range(12)))
    assert len(calls)==3


def test_healthy_parallel_reading_keeps_all_six_available_workers():
    entered=threading.Barrier(6);calls=[];lock=threading.Lock()
    def succeed(role,prompt,trace):
        with lock:calls.append(prompt)
        entered.wait(timeout=3)
        return 'read-'+prompt
    invoke=bounded_company_invoke(succeed,cooldown_seconds=0)
    with ThreadPoolExecutor(max_workers=6) as workers:
        values=list(workers.map(lambda i:invoke('read',str(i),None),range(6)))
    assert values==['read-'+str(i) for i in range(6)] and len(calls)==6
