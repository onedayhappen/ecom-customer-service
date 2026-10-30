# 文件说明：读取会员和订阅事实，核验会员券资格、周期额度并维护展示投影。
"""Membership facts and derived users summary. All timestamps are UTC."""
from datetime import datetime, timezone, timedelta
import json
from uuid import uuid4

USER_MEMBERSHIP_COLUMNS = {
    'membership_platform_code': 'VARCHAR(16) NULL',
    'growth_level_code': 'VARCHAR(32) NULL',
    'vip_plan_code': 'VARCHAR(32) NULL',
    'vip_starts_at': 'DATETIME(6) NULL',
    'vip_expires_at': 'DATETIME(6) NULL',
    'membership_updated_at': 'DATETIME(6) NULL',
}


def now_utc():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def effective_status(row, now, end='expires_at'):
    if row['status'] != 'active':
        return row['status']
    if now < row['starts_at']:
        return 'scheduled'
    return 'expired' if now >= row[end] else 'active'


def json_value(value):
    return json.loads(value) if isinstance(value, (str, bytes)) else value


def membership_facts(tx, user_id, platform, now=None, store_id=None, lock=False):
    now = now or now_utc()
    suffix = ' FOR UPDATE' if lock else ''
    rows = tx.all('''SELECT m.*,p.program_type,p.name program_name,p.merchant_id,p.registration_default,
        p.status program_status,l.name,l.benefits,l.source,l.rank_value,l.growth_threshold
        FROM memberships m JOIN membership_programs p ON p.program_id=m.program_id AND p.platform_code=m.platform_code
        JOIN membership_levels l ON l.program_id=m.program_id AND l.level_code=m.level_code AND l.version=m.level_version
        WHERE m.user_id=%s AND m.platform_code=%s ORDER BY p.registration_default DESC,m.program_id''' + suffix, (user_id,platform))
    for row in rows:
        row.pop('user_id', None)
        row['benefits'] = json_value(row['benefits'])
        row['effective_status'] = effective_status(row,now)
        if row['program_status'] != 'active': row['effective_status']='unavailable'
        row['is_expired'] = now >= row['expires_at']
    growth = next((r for r in rows if r['program_type']=='growth'),None)
    paid = [r for r in rows if r['program_type']=='paid']
    stores = [r for r in rows if r['program_type']=='store' and (not store_id or r['merchant_id']==store_id)]
    subscriptions = tx.all('''SELECT s.*,m.program_id,m.status membership_status,p.status program_status,
        m.starts_at membership_starts_at,m.expires_at membership_expires_at
        FROM membership_subscriptions s JOIN memberships m ON m.membership_id=s.membership_id
        JOIN membership_programs p ON p.program_id=m.program_id
        WHERE m.user_id=%s AND m.platform_code=%s AND p.platform_code=m.platform_code AND p.program_type='paid'
        ORDER BY s.starts_at DESC,s.subscription_id''' + suffix, (user_id,platform))
    for sub in subscriptions:
        sub['plan_snapshot']=json_value(sub['plan_snapshot'])
        sub['effective_status']=effective_status(sub,now,'ends_at')
        if sub['membership_status']!='active' or sub['program_status']!='active':
            sub['effective_status']='suspended'
        elif sub['effective_status']=='active' and not sub['membership_starts_at']<=now<sub['membership_expires_at']:
            sub['effective_status']='unavailable'
        sub.pop('request_id',None)
        sub.pop('payment_ref',None)
    user=tx.one('SELECT status FROM users WHERE user_id=%s' + suffix,(user_id,))
    if not user or user['status']!='active':
        for row in rows: row['effective_status']='suspended'
        for sub in subscriptions: sub['effective_status']='suspended'
    current=[s for s in subscriptions if s['effective_status']=='active']
    vip_status='active' if len(current)==1 else 'unknown' if len(current)>1 else 'not_opened'
    if not current and subscriptions:
        vip_status=subscriptions[0]['effective_status']
    benefits=[]
    for row in rows:
        if row['program_type']=='store' and store_id and row['merchant_id']!=store_id: continue
        definitions=tx.all('''SELECT benefit_id,version,benefit_family,period_type,quota,membership_check,rules,status
            FROM membership_benefits WHERE program_id=%s AND level_code=%s AND level_version=%s
            AND effective_from<=%s AND (effective_until IS NULL OR effective_until>%s) ORDER BY benefit_id''' + suffix,
            (row['program_id'],row['level_code'],row['level_version'],now,now))
        for b in definitions:
            b['rules']=json_value(b['rules'])
            b['program_id']=row['program_id']
            b['membership_active']=row['effective_status']=='active' and (row['program_type']!='paid' or len(current)==1)
            benefits.append(b)
    entitlements=tx.all('''SELECT e.entitlement_id,e.program_id,e.benefit_family,e.period_key,e.quota,
        e.claimed_count,e.starts_at,e.expires_at,e.status,e.benefit_id,e.benefit_version
        FROM benefit_entitlements e JOIN membership_programs p ON p.program_id=e.program_id
        WHERE e.user_id=%s AND p.platform_code=%s ORDER BY e.period_key DESC,e.entitlement_id''', (user_id,platform))
    for e in entitlements:
        e['remaining_count']=max(0,e['quota']-e['claimed_count'])
        e['effective_status']=effective_status(e,now)
    return {'membership':growth, 'growth_membership':growth,
        'vip':{'is_vip':True if len(current)==1 else None if len(current)>1 else False,
            'status':vip_status,'subscription':current[0] if len(current)==1 else None,
            'records':paid,'subscription_history':subscriptions[:20],'history_total':len(subscriptions)},
        'store_memberships':stores,'benefit_definitions':benefits,'benefit_entitlements':entitlements,'platform_code':platform}


def coupon_membership_claim(tx, user_id, template, now=None):
    """Resolve a member coupon to one monthly family; never grant by its name."""
    from app.services.commerce.common import BusinessError,money
    now=now or now_utc()
    suffix=' FOR UPDATE' if tx.write else ''
    if tx.write:
        # Serialize cross-template/level claims for this user, including absent periods.
        tx.one('SELECT user_id FROM users WHERE user_id=%s FOR UPDATE',(user_id,))
    bindings=tx.all("SELECT benefit_id FROM membership_benefits WHERE JSON_UNQUOTE(JSON_EXTRACT(rules,'$.coupon_template_id'))=%s",(template['coupon_id'],))
    if not bindings:
        return None
    facts=membership_facts(tx,user_id,template['platform_code'],now,lock=tx.write)
    matches=[b for b in facts['benefit_definitions'] if b['rules'].get('coupon_template_id')==template['coupon_id']
        and b['status']=='active' and b['membership_active']]
    if len(matches)!=1:
        raise BusinessError('not_eligible','当前会员身份不符合该券的领取条件')
    b=matches[0]
    if b['period_type']!='month' or b['membership_check'] not in ('claim_only','claim_and_use'):
        raise BusinessError('insufficient_data','会员券周期或资格检查规则不完整')
    rule=b['rules']
    if (template['kind']!='amount_off' or str(rule.get('currency'))!=template['currency']
        or money(rule.get('threshold_amount'))!=money(template['threshold_amount'])
        or money(rule.get('discount_amount'))!=money(template['discount_amount'])):
        raise BusinessError('insufficient_data','会员权益与券模板金额不一致')
    local=now+timedelta(hours=8)
    month_start=datetime(local.year,local.month,1)-timedelta(hours=8)
    month_end=(datetime(local.year+1,1,1) if local.month==12 else datetime(local.year,local.month+1,1))-timedelta(hours=8)
    period=local.strftime('%Y-%m')
    member=next((m for m in [facts['growth_membership'],*facts['vip']['records'],*facts['store_memberships']]
        if m and m['program_id']==b['program_id'] and m['effective_status']=='active'),None)
    if not member:
        raise BusinessError('not_eligible','会员关系当前无效')
    end=min(month_end,member['expires_at'])
    if member['program_type']=='paid':
        if facts['vip']['status']!='active':
            raise BusinessError('not_eligible','VIP订阅当前无效或存在重叠')
        end=min(end,facts['vip']['subscription']['ends_at'])
    e=tx.one('''SELECT * FROM benefit_entitlements WHERE user_id=%s AND program_id=%s
        AND benefit_family=%s AND period_key=%s''' + suffix,(user_id,b['program_id'],b['benefit_family'],period))
    if e and (e['status']!='active' or not e['starts_at']<=now<e['expires_at']):
        raise BusinessError('not_eligible','本月会员券额度尚未开放或已失效')
    quota=max(e['quota'] if e else 0,b['quota'])
    claimed=e['claimed_count'] if e else 0
    if claimed>=b['quota']:
        raise BusinessError('not_eligible','本月同类会员券领取额度已用完')
    return dict(entitlement_id=e['entitlement_id'] if e else None,program_id=b['program_id'],
        benefit_id=b['benefit_id'],benefit_version=b['version'],benefit_family=b['benefit_family'],
        membership_id=member['membership_id'],membership_check=b['membership_check'],period_key=period,
        quota=quota,claimed_count=claimed,remaining_count=b['quota']-claimed,
        starts_at=max(month_start,member['starts_at']),expires_at=end)


def consume_coupon_entitlement(tx, user_id, grant, wallet_id, now):
    """Consume the previously locked period and link issuance inside the claim transaction."""
    from app.services.commerce.common import BusinessError,dumps,decoded
    eid=grant['entitlement_id'] or uuid4().hex
    prior=tx.one('SELECT evidence_snapshot FROM benefit_entitlements WHERE entitlement_id=%s FOR UPDATE',(eid,))
    evidence=decoded(prior['evidence_snapshot'],{}) if prior else {}
    issued=list(evidence.get('user_coupon_ids',[]))
    if wallet_id in issued:
        raise BusinessError('state_changed','该券已计入会员额度')
    issued.append(wallet_id)
    evidence.update(user_coupon_ids=issued,last_claim_at=now,membership_id=grant['membership_id'],
        membership_check=grant['membership_check'],automatic_claim_enabled=True)
    if prior:
        changed=tx.execute('''UPDATE benefit_entitlements SET quota=%s,claimed_count=claimed_count+1,
            expires_at=%s,benefit_id=%s,benefit_version=%s,evidence_snapshot=%s
            WHERE entitlement_id=%s AND user_id=%s AND claimed_count=%s AND status='active' ''',
            (grant['quota'],grant['expires_at'],grant['benefit_id'],grant['benefit_version'],dumps(evidence),eid,user_id,grant['claimed_count']))
        if changed!=1:
            raise BusinessError('state_changed','会员额度已发生变化')
    else:
        tx.execute('''INSERT INTO benefit_entitlements(entitlement_id,user_id,program_id,benefit_family,period_key,
            quota,claimed_count,starts_at,expires_at,status,benefit_id,benefit_version,evidence_snapshot)
            VALUES (%s,%s,%s,%s,%s,%s,1,%s,%s,'active',%s,%s,%s)''',
            (eid,user_id,grant['program_id'],grant['benefit_family'],grant['period_key'],grant['quota'],
             grant['starts_at'],grant['expires_at'],grant['benefit_id'],grant['benefit_version'],dumps(evidence)))
    return eid


def check_coupon_membership_use(tx, user_id, template):
    from app.services.commerce.common import BusinessError
    requirement=template.get('membership_requirement')
    if not requirement:
        binding=tx.one("SELECT benefit_id FROM membership_benefits WHERE JSON_UNQUOTE(JSON_EXTRACT(rules,'$.coupon_template_id'))=%s LIMIT 1",(template['coupon_id'],))
        if binding:
            raise BusinessError('insufficient_data','会员券缺少领取时资格快照')
        return
    mode=requirement.get('membership_check')
    if mode=='claim_only':
        return
    if mode!='claim_and_use':
        raise BusinessError('insufficient_data','会员券使用资格规则不完整')
    if tx.write:
        tx.one('SELECT user_id FROM users WHERE user_id=%s FOR UPDATE',(user_id,))
    facts=membership_facts(tx,user_id,template['platform_code'],lock=tx.write)
    rows=[facts['growth_membership'],*facts['vip']['records'],*facts['store_memberships']]
    member=next((m for m in rows if m and m['program_id']==requirement['program_id'] and m['effective_status']=='active'),None)
    if not member or (member['program_type']=='paid' and facts['vip']['status']!='active'):
        raise BusinessError('not_eligible','使用该券时会员资格必须仍然有效')


def refresh_user_summary(tx, user_id, platform, now=None):
    """Only a display projection; entitlement checks always use membership facts."""
    now=now or now_utc()
    facts=membership_facts(tx,user_id,platform,now)
    growth=facts['growth_membership']; sub=facts['vip']['subscription']
    tx.execute('''UPDATE users SET membership_platform_code=%s,growth_level_code=%s,vip_plan_code=%s,
        vip_starts_at=%s,vip_expires_at=%s,membership_updated_at=%s WHERE user_id=%s''',
        (platform,growth['level_code'] if growth else None,sub['plan_code'] if sub else None,
         sub['starts_at'] if sub else None,sub['ends_at'] if sub else None,now,user_id))


def enroll_registered_user(tx, user_id, now=None):
    """Auth-only deployments remain valid; enroll only an explicitly configured default."""
    if not tx.one("SELECT 1 FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name='membership_programs'"):
        return
    programs=tx.all("SELECT * FROM membership_programs WHERE program_type='growth' AND registration_default=TRUE AND status='active'")
    if len(programs)!=1:
        if programs: raise ValueError('Multiple default membership programs require configuration')
        return
    p=programs[0]; now=now or now_utc()
    from datetime import timedelta
    local=now+timedelta(hours=8)
    month=((local.month-1)//3+1)*3+1
    end=datetime(local.year+int(month>12),1 if month>12 else month,1)-timedelta(hours=8)
    level=tx.one('SELECT * FROM membership_levels WHERE program_id=%s AND level_code=%s AND version=%s',
        (p['program_id'],'bronze',p['rule_version']))
    if not level: raise ValueError('Default bronze membership level missing')
    mid=uuid4().hex
    tx.execute('''INSERT INTO memberships(membership_id,user_id,platform_code,program_id,level_code,level_version,
        starts_at,expires_at,status,updated_at,growth_value,evaluated_at,next_evaluate_at,assignment_source)
        VALUES (%s,%s,%s,%s,'bronze',%s,%s,%s,'active',%s,0,%s,%s,'registration')''',
        (mid,user_id,p['platform_code'],p['program_id'],level['version'],now,end,now,now,end))
    tx.execute('''INSERT INTO membership_events(event_id,membership_id,event_type,to_level,effective_at,ends_at,reason,rule_version,request_id)
        VALUES (%s,%s,'enrolled','bronze',%s,%s,'首次注册默认青铜会员',%s,%s)''',
        (uuid4().hex,mid,now,end,level['version'],'registration:'+user_id))
    refresh_user_summary(tx,user_id,p['platform_code'],now)
