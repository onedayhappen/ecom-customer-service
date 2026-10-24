"""Fill recent William demo parcel histories up to their recorded progress.

Default previews; --apply inserts explicitly simulated events for orders created
in the last 30 days. Existing events and delivery times remain intact.
"""
from __future__ import annotations
import argparse
from datetime import datetime,timedelta,timezone
import hashlib
from pathlib import Path
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import pymysql
from dotenv import dotenv_values
from app.data.business_store import Transaction
from app.data.order_dao import parse_mysql_dsn
from app.services.commerce.common import dumps

SOURCE='local_demo:william_tracking_v1'


def build_plan(tx,now):
    cutoff=now-timedelta(days=30)
    forward=tx.all("""SELECT s.* FROM shipments s JOIN orders o
        ON o.platform_code=s.platform_code AND o.order_id=s.order_id
        WHERE o.user_id='user_1' AND o.created_at>=%s ORDER BY s.shipment_id""",(cutoff,))
    reverse=tx.all("""SELECT s.* FROM return_shipments s JOIN return_authorizations a ON a.authorization_id=s.authorization_id
        JOIN after_sale_requests f ON f.after_sale_id=a.after_sale_id JOIN orders o ON o.platform_code=f.platform_code AND o.order_id=f.order_id
        WHERE o.user_id='user_1' AND o.created_at>=%s ORDER BY s.return_shipment_id""",(cutoff,))
    events=[];updates=[];skipped=[]
    for direction,parcels in [('forward',forward),('return',reverse)]:
        for parcel in parcels:
            field='shipment_id' if direction=='forward' else 'return_shipment_id';pid=parcel[field]
            table='shipment_events' if direction=='forward' else 'return_shipment_events'
            existing=tx.all('SELECT * FROM '+table+' WHERE '+field+'=%s ORDER BY event_time,event_id',(pid,))
            if any((e.get('source') or '').startswith(SOURCE) or (e.get('source_event_id') or '').startswith(SOURCE) for e in existing):
                skipped.append(pid);continue
            if any(not ('模拟' in (e.get('description') or '') or (e.get('source') or '').startswith('local_demo')) for e in existing):
                raise ValueError('Non-demo event history requires manual reconciliation: '+pid)
            if direction=='forward':
                if not parcel['shipped_at'] or parcel['status'] not in {'shipped','in_transit','delivered'}:
                    raise ValueError('Unsupported forward progress: '+pid)
                start=parcel['shipped_at'];end=parcel['delivered_at']
                if parcel['status']=='delivered':
                    if not end: raise ValueError('Delivered parcel has no delivery timestamp')
                    steps=[(0,'shipped','模拟发货仓','商家已发货'),
                        (.1,'collected','模拟始发网点','快递员已揽收'),
                        (.3,'in_transit','模拟始发分拨中心','完成分拣，发往中转中心'),
                        (.55,'in_transit','模拟中转中心','中转分拣完成，发往目的地'),
                        (.8,'in_transit','模拟目的地营业部','已到达目的地配送网点'),
                        (.93,'out_for_delivery','模拟送货上门路线','配送员正在送货上门'),
                        (1,'delivered','订单收货地址（模拟）','已送货上门并签收')]
                else:
                    if end is not None: raise ValueError('Undelivered parcel has delivery timestamp')
                    end=now-timedelta(minutes=30)
                    steps=[(0,'shipped','模拟发货仓','商家已发货'),
                        (.1,'collected','模拟始发网点','快递员已揽收'),
                        (.35,'in_transit','模拟始发分拨中心','完成分拣，发往中转中心'),
                        (.7,'in_transit','模拟中转中心','包裹到达中转中心'),
                        (1,'in_transit','模拟运输途中','离开中转中心，运输中，尚未签收')]
                    if parcel['status']=='shipped': updates.append(('UPDATE shipments SET status=%s WHERE shipment_id=%s',('in_transit',pid)))
            else:
                start=parcel['created_at'];end=parcel['updated_at']
                if parcel['status']=='accepted':
                    steps=[(0,'collected',None,'退件已由快递员揽收'),
                        (.25,'in_transit',None,'退件到达模拟始发分拨中心'),
                        (.5,'in_transit',None,'退件经模拟中转中心发往商家'),
                        (.75,'delivered',None,'退件已送达模拟商家退货仓'),
                        (.9,'inspecting',None,'模拟退货仓正在核对商品和数量'),
                        (1,'accepted',None,'模拟商家验收完成')]
                elif parcel['status']=='in_transit':
                    steps=[(0,'collected',None,'退件已由快递员揽收'),
                        (.33,'in_transit',None,'退件到达模拟始发分拨中心'),
                        (.66,'in_transit',None,'退件到达模拟中转中心'),
                        (1,'in_transit',None,'退件运输中，商家尚未收到')]
                else: raise ValueError('Unsupported return progress: '+pid)
            if not start<end<=now: raise ValueError('Invalid parcel timeline: '+pid)
            for index,(fraction,status,location,description) in enumerate(steps):
                when=start+(end-start)*fraction
                if any(e['event_time']==when and e['status']==status for e in existing): continue
                eid='wt_'+hashlib.sha256(f'{direction}:{pid}:{index}'.encode()).hexdigest()[:40]
                event=dict(event_id=eid,**{field:pid},event_time=when,status=status,description='【本地模拟补全】'+description)
                if direction=='forward': event.update(location=location,source_event_id=SOURCE+':'+str(index),recorded_at=now)
                else: event['source']=SOURCE
                events.append((table,event))
    return events,updates,dict(user_id='user_1',window_days=30,cutoff_utc=cutoff,forward_parcels=len(forward),return_parcels=len(reverse),
        already_filled=len(skipped),new_forward_events=sum(t=='shipment_events' for t,_ in events),
        new_return_events=sum(t=='return_shipment_events' for t,_ in events),parcel_status_updates=len(updates))


def run(dsn,apply=False):
    conn=pymysql.connect(**parse_mysql_dsn(dsn),charset='utf8mb4',cursorclass=pymysql.cursors.DictCursor,autocommit=False)
    try:
        with conn.cursor() as c:
            c.execute("SELECT GET_LOCK('ecom_mysql_business_schema',30) acquired")
            if c.fetchone()['acquired']!=1: raise TimeoutError('Data seed lock busy')
        tx=Transaction(conn,write=apply)
        if apply:
            tx.all("SELECT platform_code,order_id FROM orders WHERE user_id='user_1' ORDER BY platform_code,order_id FOR UPDATE")
            tx.all("SELECT s.shipment_id FROM shipments s JOIN orders o ON o.platform_code=s.platform_code AND o.order_id=s.order_id WHERE o.user_id='user_1' FOR UPDATE")
        events,updates,report=build_plan(tx,datetime.now(timezone.utc).replace(tzinfo=None,microsecond=0))
        if apply:
            with conn.cursor() as c:
                for sql,args in updates: c.execute(sql,args)
                for table,row in events:
                    c.execute('INSERT INTO '+table+' ('+','.join(row)+') VALUES ('+','.join(['%s']*len(row))+')',tuple(row.values()))
            conn.commit()
        return report
    except BaseException:
        conn.rollback();raise
    finally:
        with conn.cursor() as c: c.execute("SELECT RELEASE_LOCK('ecom_mysql_business_schema')")
        conn.close()


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--apply',action='store_true');args=p.parse_args()
    print(dumps(run(dotenv_values(ROOT/'.env')['ORDER_DB_DSN'],args.apply)))
