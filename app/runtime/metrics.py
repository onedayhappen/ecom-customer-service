"""Bounded-label infrastructure metrics, available in API and background roles."""
from prometheus_client import Counter, Histogram, Gauge, REGISTRY, start_http_server
from prometheus_client.core import GaugeMetricFamily

DELIVERIES = Counter('platform_outbox_deliveries_total', 'Outbox delivery attempts', ['result'])
RATE_REJECTIONS = Counter('platform_rate_rejections_total', 'Requests rejected by hierarchical budgets')
RECOVERIES = Counter('platform_job_recoveries_total', 'Durable deadline recovery', ['status'])
POOL_WAIT = Histogram('platform_db_pool_wait_seconds', 'Time acquiring a database connection')
POOL_TIMEOUTS = Counter('platform_db_pool_timeouts_total', 'Database pool acquisition timeouts')
RUNTIME_HEARTBEAT = Gauge('platform_runtime_last_success_unixtime', 'Last successful queue poll or dispatcher iteration', ['role'])
EXECUTION = Histogram('platform_job_execution_seconds', 'Worker job wall time', ['result'],
                      buckets=(.1, .5, 1, 2, 5, 10, 30, 60, 120, 300, 600))
QUEUE_WAIT = Histogram('platform_job_queue_wait_seconds', 'Durable admission to execution',
                       buckets=(.01, .1, .5, 1, 2, 5, 10, 30, 60, 120))


class PlatformCollector:
    def collect(self):
        from app.support.runtime import get_store
        from app.runtime.outbox import clock
        from app.runtime.notifications import get_hub
        from app.db import _POOLS, _POOLS_LOCK
        up = GaugeMetricFamily('platform_store_up', 'Durable runtime database scrape successful')
        depth = GaugeMetricFamily('platform_jobs', 'Nonterminal durable jobs', labels=['status'])
        age = GaugeMetricFamily('platform_oldest_waiting_job_seconds', 'Age of oldest job not yet started')
        notification = GaugeMetricFamily('platform_notification_backlog', 'Coalesced sessions awaiting notification')
        retries = GaugeMetricFamily('platform_delivery_retry_jobs', 'Waiting jobs with multiple delivery attempts')
        operations = GaugeMetricFamily('platform_business_operations', 'Business operations requiring reconciliation', labels=['status'])
        pipeline = GaugeMetricFamily('platform_pipeline_mode', 'Current automatic processing mode', labels=['mode'])
        try:
            with get_store().transaction(read_only=True) as tx:
                timestamp = clock(tx)
                rows = tx.all("SELECT status,COUNT(*) AS n,MIN(created_at) AS oldest FROM runtime_jobs WHERE status IN ('pending','dispatched','running') GROUP BY status")
                notification.add_metric([], tx.one('SELECT COUNT(*) AS n FROM runtime_notifications')['n'])
                retries.add_metric([], tx.one("SELECT COUNT(*) AS n FROM runtime_jobs WHERE status IN ('pending','dispatched') AND attempts>1")['n'])
                unresolved = tx.all("SELECT status,COUNT(*) AS n FROM support_operations WHERE status IN ('running','unknown') GROUP BY status")
                mode = tx.one('SELECT mode FROM task_pipeline_state WHERE singleton=1')
            operation_counts = {r['status']: r['n'] for r in unresolved}
            for state in ('running', 'unknown'):
                operations.add_metric([state], operation_counts.get(state, 0))
            for state in ('all', 'selected', 'stopped'):
                pipeline.add_metric([state], int(bool(mode) and mode['mode'] == state))
            counts = {r['status']: r for r in rows}
            for state in ('pending', 'dispatched', 'running'):
                depth.add_metric([state], counts.get(state, {}).get('n', 0))
            waiting = [r['oldest'] for r in rows if r['status'] in {'pending', 'dispatched'}]
            age.add_metric([], max(0, timestamp - min(waiting)) if waiting else 0)
            up.add_metric([], 1)
            yield depth
            yield age
            yield notification
            yield retries
            yield operations
            yield pipeline
        except Exception:
            up.add_metric([], 0)
        yield up
        connections = GaugeMetricFamily('platform_sse_connections', 'SSE connections in this process')
        connections.add_metric([], get_hub().connections)
        yield connections
        pools = GaugeMetricFamily('platform_db_pool_connections', 'Connections created by this process')
        idle = GaugeMetricFamily('platform_db_pool_idle_connections', 'Idle pooled connections in this process')
        capacity = GaugeMetricFamily('platform_db_pool_capacity', 'Configured connection limit in this process')
        with _POOLS_LOCK:
            pools.add_metric([], sum(p.created_count for p in _POOLS.values()))
            idle.add_metric([], sum(p.idle_count for p in _POOLS.values()))
            capacity.add_metric([], sum(p.max_size for p in _POOLS.values()))
        yield pools
        yield idle
        yield capacity


_installed = False


def install(port=0, host='127.0.0.1'):
    global _installed
    if not _installed:
        REGISTRY.register(PlatformCollector())
        _installed = True
    if port:
        return start_http_server(port, addr=host)
