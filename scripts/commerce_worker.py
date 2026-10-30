# 文件说明：启动交易后台循环，处理持久渠道请求与未付款订单到期关闭。
"""Process durable provider requests and expire unpaid local orders. No payment execution."""
import argparse
import time
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from app.data.business_store import BusinessStore
from app.config.settings import settings
from app.services.commerce import service


def run_once(store):
    with store.transaction() as tx:
        jobs=tx.all("SELECT request_id,user_id FROM commerce_provider_requests WHERE status IN ('queued','unknown','accepted','processing') AND (lease_until IS NULL OR lease_until<UTC_TIMESTAMP(6)) ORDER BY updated_at LIMIT 100")
        orders=tx.all("SELECT e.platform_code,e.order_id,o.user_id FROM order_expirations e JOIN orders o ON o.platform_code=e.platform_code AND o.order_id=e.order_id WHERE e.status='active' AND e.expires_at<UTC_TIMESTAMP(6) LIMIT 100")
    errors=0
    for job in jobs:
        try: service(job['user_id']).process_provider_request(job['request_id'])
        except Exception: errors+=1
    for row in orders:
        try: service(row['user_id']).expire_order(row['platform_code'],row['order_id'])
        except Exception: errors+=1
    return {'requests_checked':len(jobs),'orders_checked':len(orders),'errors':errors}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--once',action='store_true')
    args=parser.parse_args(); store=BusinessStore(settings.order_db_dsn)
    while True:
        print(run_once(store),flush=True)
        if args.once: return
        time.sleep(10)

if __name__=='__main__': main()
