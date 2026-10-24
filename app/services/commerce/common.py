"""Shared business contracts, ownership checks, opaque references and bounded pagination."""
import base64
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone
from decimal import Decimal
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

class BusinessError(Exception):
    def __init__(self, code, message, **details):
        self.code, self.message, self.details = code, message, details
        super().__init__(message)

def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)

def at(value):
    if value is None: return None
    if isinstance(value,str): value=datetime.fromisoformat(value.replace('Z','+00:00'))
    return value.astimezone(timezone.utc).replace(tzinfo=None) if value.tzinfo else value

def encode(value):
    if isinstance(value,Decimal): return format(value,'.2f')
    if isinstance(value,datetime): return value.isoformat()+'Z' if not value.tzinfo else value.isoformat()
    raise TypeError(type(value).__name__)

def dumps(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),default=encode)

def decoded(value, default=None):
    if value is None: return default
    return json.loads(value) if isinstance(value,(str,bytes)) else value

def digest(value):
    return hashlib.sha256(dumps(value).encode()).hexdigest()

def ref(kind,*parts):
    return base64.urlsafe_b64encode(dumps([kind,*map(str,parts)]).encode()).decode().rstrip('=')

def unref(value, kinds):
    try:
        data=json.loads(base64.urlsafe_b64decode(value+'='*(-len(value)%4)))
        if not isinstance(data,list) or data[0] not in kinds or not all(isinstance(x,str) and x for x in data): raise ValueError()
        if len(data)!=(3 if data[0]=='order' else 2): raise ValueError()
        return data
    except Exception:
        raise BusinessError('invalid_arguments','对象引用无效') from None

def money(value):
    if value is None: raise BusinessError('insufficient_data','金额缺失，无法核实')
    return Decimal(str(value)).quantize(Decimal('.01'))

def ok(data, *, limitations=(), sources=(), next_cursor=None):
    return {'ok':True,'success':True,'data':data,'observed_at':encode(utcnow()),'data_as_of':None,
        'sources':list(sources),'completeness':'partial' if limitations else 'complete',
        'limitations':list(limitations),'next_cursor':next_cursor}

def failure(error):
    legacy={'temporarily_unavailable':'transient_failure','result_unknown':'transient_failure',
        'state_changed':'confirmation_invalid','not_eligible':'business_rejection',
        'insufficient_data':'evidence_unavailable','unsupported_capability':'evidence_unavailable'}
    return {'ok':False,'success':False,'status':'no_match' if error.code=='not_found' else 'unavailable','error':{'code':error.code,'message':error.message,**error.details},
        'error_code':legacy.get(error.code,'validation_error')}

class Cipher:
    def __init__(self, keys, version):
        self.keys, self.version = decoded(keys,{}) or {}, version
    def key(self, version):
        try:
            key=base64.urlsafe_b64decode(self.keys[str(version)])
            if len(key)!=32: raise ValueError()
            return key
        except Exception:
            raise BusinessError('temporarily_unavailable','业务加密密钥未配置或版本不可用') from None
    def seal(self, value, purpose):
        nonce=os.urandom(12)
        return nonce+AESGCM(self.key(self.version)).encrypt(nonce,value.encode(),purpose.encode())
    def open(self, value, purpose, version):
        try:
            raw=bytes(value)
            return AESGCM(self.key(version)).decrypt(raw[:12],raw[12:],purpose.encode()).decode()
        except BusinessError: raise
        except Exception: raise BusinessError('insufficient_data','加密资料无法核实') from None

class BaseService:
    def __init__(self, store, user, *, scope=None, secret='', keys='{}', key_version=1):
        if not user: raise BusinessError('invalid_arguments','需要登录身份')
        self.store,self.user,self.scope,self.secret=store,user,scope,secret
        self.cipher=Cipher(keys,key_version)
    def scope_check(self, row):
        for key,field in [('platform_code','platform_code'),('store_id','merchant_id')]:
            expected=getattr(self.scope,key,None)
            if expected and row.get(field)!=expected:
                raise BusinessError('not_found','对象不存在')
    def coupon_scope_check(self,row):
        # A platform coupon has no merchant restriction.
        self.scope_check({**row,'merchant_id':row.get('merchant_id') or getattr(self.scope,'store_id',None)})
    def default_platform(self,tx):
        # Resolve from trusted business records, not the legacy routing default.
        rows=tx.all('''SELECT platform_code FROM orders WHERE user_id=%s
            UNION SELECT platform_code FROM user_platform_accounts WHERE user_id=%s AND status='active' AND verified_at IS NOT NULL
            UNION SELECT platform_code FROM memberships WHERE user_id=%s AND status='active' LIMIT 2''',(self.user,self.user,self.user))
        if rows: return rows[0]['platform_code'] if len(rows)==1 else None
        rows=tx.all("SELECT DISTINCT platform_code FROM business_policy_versions WHERE status='active' AND effective_from<=%s AND (effective_until IS NULL OR effective_until>%s) LIMIT 2",(utcnow(),utcnow()))
        return rows[0]['platform_code'] if len(rows)==1 else None
    def sales_platform(self,tx):
        platform=getattr(self.scope,'platform_code',None)
        if platform: return platform
        from app.tasks.runtime import current_task
        ctx=current_task()
        if not ctx: return self.default_platform(tx)
        if ctx.runs.user!=self.user: return None
        candidate=ctx.record.details.get('platform_code')
        if not candidate: return self.default_platform(tx)
        owned=tx.one('SELECT order_id FROM orders WHERE user_id=%s AND platform_code=%s LIMIT 1',(self.user,candidate))
        bound=tx.one("SELECT id FROM user_platform_accounts WHERE user_id=%s AND platform_code=%s AND status='active' AND verified_at IS NOT NULL LIMIT 1",(self.user,candidate))
        return candidate if owned or bound else None
    def owned_order(self, tx, order_ref, *, lock=False):
        _,platform,oid=unref(order_ref,{'order'})
        row=tx.one('SELECT * FROM orders WHERE platform_code=%s AND order_id=%s AND user_id=%s'+(' FOR UPDATE' if lock else ''),(platform,oid,self.user))
        if not row: raise BusinessError('not_found','订单不存在')
        self.scope_check(row)
        return row
    def sale(self,tx,aid, *, lock=False):
        row=tx.one('SELECT * FROM after_sale_requests WHERE after_sale_id=%s AND user_id=%s'+(' FOR UPDATE' if lock else ''),(aid,self.user))
        if not row: raise BusinessError('not_found','售后申请不存在')
        order=self.owned_order(tx,ref('order',row['platform_code'],row['order_id']),lock=lock)
        return row,order
    def order_base_status(self,order):
        return order.get('status_before_after_sale') if order['status']=='after_sale' else order['status']
    def sync_order_after_sale(self,tx,order):
        # Lock the parent first, as all after-sale writers do. Multiple active
        # requests must not restore the order when only one request terminates.
        current=self.owned_order(tx,ref('order',order['platform_code'],order['order_id']),lock=True)
        active=tx.one("SELECT after_sale_id FROM after_sale_requests WHERE platform_code=%s AND order_id=%s AND status IN ('pending_review','approved','processing') LIMIT 1",(current['platform_code'],current['order_id']))
        if active and current['status']!='after_sale':
            tx.execute("UPDATE orders SET status_before_after_sale=%s,status='after_sale',updated_at=%s WHERE platform_code=%s AND order_id=%s",(current['status'],utcnow(),current['platform_code'],current['order_id']))
        elif not active and current['status']=='after_sale':
            previous=current.get('status_before_after_sale')
            if not previous: raise BusinessError('insufficient_data','售后前订单状态缺失')
            tx.execute('UPDATE orders SET status=%s,status_before_after_sale=NULL,updated_at=%s WHERE platform_code=%s AND order_id=%s',(previous,utcnow(),current['platform_code'],current['order_id']))
    def order_rows(self,tx,table,order):
        allowed={'order_items','shipments','payment_transactions','after_sale_requests','invoice_requests','coupon_redemptions','inventory_reservations','order_events'}
        if table not in allowed: raise ValueError('invalid internal table')
        return tx.all(f'SELECT * FROM {table} WHERE platform_code=%s AND order_id=%s',(order['platform_code'],order['order_id']))
    def public_product(self,tx,pid):
        row=tx.one('SELECT product_id,merchant_id,name,category,description,specs FROM products WHERE product_id=%s',(pid,))
        if not row: raise BusinessError('not_found','商品不存在')
        merchant=getattr(self.scope,'store_id',None)
        if merchant and row['merchant_id']!=merchant: raise BusinessError('not_found','商品不存在')
        row['specs']=decoded(row.get('specs'))
        return row
    def sku(self,tx,sid):
        lock=' FOR UPDATE' if tx.write else ''
        row=tx.one('SELECT * FROM product_skus WHERE sku_id=%s'+lock,(sid,))
        if not row: raise BusinessError('not_found','规格不存在')
        product=self.public_product(tx,row['product_id'])
        inv=tx.one('SELECT available_qty,updated_at FROM sku_inventory WHERE sku_id=%s'+lock,(sid,))
        row['spec_values']=decoded(row['spec_values'],{})
        row.update(merchant_id=product['merchant_id'],product_name=product['name'],inventory=inv,
            subject_ref=ref('sku',sid))
        return row
    def subject(self,tx,value, *, allow_order=True):
        if not value:
            return {'platform_code':self.sales_platform(tx),'merchant_id':getattr(self.scope,'store_id',None)}
        data=unref(value,{'merchant','product','sku','order'} if allow_order else {'merchant','product','sku'})
        if data[0]=='order':
            row=self.owned_order(tx,value)
            return dict(platform_code=row['platform_code'],merchant_id=row['merchant_id'],order=row)
        if data[0]=='sku': row=self.sku(tx,data[1])
        elif data[0]=='product': row=self.public_product(tx,data[1])
        else:
            row=tx.one('SELECT merchant_id FROM merchants WHERE merchant_id=%s',(data[1],))
            if not row: raise BusinessError('not_found','商家不存在')
            if getattr(self.scope,'store_id',None) and row['merchant_id']!=self.scope.store_id: raise BusinessError('not_found','商家不存在')
        return {**row,'platform_code':self.sales_platform(tx)}
    def sql_page(self,tx,query,values,tool,args,*,keys,size=20,data=None,limitations=()):
        """Bounded keyset pagination; query/keys are internal SQL, never model input."""
        binding=digest([self.user,tool,{k:v for k,v in args.items() if k!='cursor'},
                        getattr(self.scope,'platform_code',None),getattr(self.scope,'store_id',None)])
        token=args.get('cursor'); condition=''; parameters=list(values)
        if token:
            try:
                raw,sig=token.rsplit('.',1)
                if not self.secret or not hmac.compare_digest(sig,hmac.new(self.secret.encode(),raw.encode(),'sha256').hexdigest()): raise ValueError()
                info=json.loads(base64.urlsafe_b64decode(raw))
                if info['binding']!=binding or info['expires']<time.time() or info['keys']!=list(keys): raise ValueError()
                last=info['last']
                if len(last)!=len(keys): raise ValueError()
                parameters += [at(v['value']) if v['type']=='datetime' else v['value'] for v in last]
                condition=' WHERE ('+','.join(keys)+') > ('+','.join(['%s']*len(keys))+')' if len(keys)>1 else ' WHERE '+keys[0]+' > %s'
            except Exception: raise BusinessError('invalid_cursor','分页游标无效或已过期') from None
        total=tx.one('SELECT COUNT(*) AS n FROM ('+query+') page_count',tuple(values))['n']
        rows=tx.all('SELECT * FROM ('+query+') page_rows'+condition+' ORDER BY '+','.join(keys)+' LIMIT %s',tuple(parameters+[size+1]))
        more=len(rows)>size; rows=rows[:size]; next_token=None
        if more:
            if not self.secret: raise BusinessError('temporarily_unavailable','分页签名未配置')
            last=[{'type':'datetime' if isinstance(rows[-1][k],datetime) else 'value','value':rows[-1][k]} for k in keys]
            raw=base64.urlsafe_b64encode(dumps(dict(binding=binding,keys=list(keys),last=last,expires=int(time.time())+900)).encode()).decode()
            next_token=raw+'.'+hmac.new(self.secret.encode(),raw.encode(),'sha256').hexdigest()
        return ok({**(data or {}),'items':rows,'total':total,'has_more':more,
            'applied_conditions':{k:v for k,v in args.items() if k!='cursor'}},next_cursor=next_token,
            limitations=[*limitations,'实时分页：各页独立读取，数据变更后可从第一页重新查询'])

    def page(self,rows,tool,args, *, data=None,limitations=(),size=20):
        # A signed snapshot digest prevents skips/duplicates when the collection changes.
        if len(rows)>10000: raise BusinessError('insufficient_data','结果过多，请缩小查询范围')
        token=args.get('cursor'); offset=0
        binding=digest([self.user,tool,{k:v for k,v in args.items() if k!='cursor'},
                        getattr(self.scope,'platform_code',None),getattr(self.scope,'store_id',None)])
        snapshot=digest(rows)
        if token:
            try:
                raw,sig=token.rsplit('.',1)
                if not self.secret or not hmac.compare_digest(sig,hmac.new(self.secret.encode(),raw.encode(),'sha256').hexdigest()): raise ValueError()
                info=json.loads(base64.urlsafe_b64decode(raw))
                if info['binding']!=binding or info['expires']<time.time(): raise ValueError()
                if info['snapshot']!=snapshot: raise BusinessError('state_changed','查询结果已变化，请从第一页重新查询')
                offset=info['offset']
                if not isinstance(offset,int) or offset<0 or offset>=len(rows): raise ValueError()
            except BusinessError: raise
            except Exception: raise BusinessError('invalid_cursor','分页游标无效或已过期') from None
        next_token=None
        if offset+size<len(rows):
            if not self.secret: raise BusinessError('temporarily_unavailable','分页签名未配置')
            raw=base64.urlsafe_b64encode(dumps(dict(binding=binding,snapshot=snapshot,offset=offset+size,expires=int(time.time())+900)).encode()).decode()
            next_token=raw+'.'+hmac.new(self.secret.encode(),raw.encode(),'sha256').hexdigest()
        return ok({**(data or {}),'items':rows[offset:offset+size],'total':len(rows),
            'has_more':bool(next_token),'applied_conditions':{k:v for k,v in args.items() if k!='cursor'}},limitations=limitations,next_cursor=next_token)
    def snapshot(self,tx,order):
        tables=['order_items','shipments','payment_transactions','after_sale_requests','invoice_requests','coupon_redemptions','inventory_reservations','order_events']
        bundle={'order':order}
        for table in tables: bundle[table]=self.order_rows(tx,table,order)
        bundle['address']=tx.one('SELECT * FROM order_address_snapshots WHERE platform_code=%s AND order_id=%s',(order['platform_code'],order['order_id']))
        # Ciphertext is hashed, never returned to the model.
        def binary(v):
            if isinstance(v,bytes): return base64.b64encode(v).decode()
            if isinstance(v,dict): return {k:binary(x) for k,x in v.items()}
            if isinstance(v,list): return [binary(x) for x in v]
            return v
        bundle=binary(bundle)
        for table in tables: bundle[table]=sorted(bundle[table],key=dumps)
        return digest(bundle)
