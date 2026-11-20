"""Telescope time allocation, setup versions and target visibility."""
from .store import ingest,decide

def run(db,data):
    state=ingest(db,'slot',data)
    if state!='new':return {'source_id':data['source_id'],'status':state}
    s=data['slot'];sid=s['id'];starts=int(s['starts']);ends=int(s['ends'])
    if ends<=starts:raise ValueError('invalid slot interval')
    for down in data.get('downtime',[]):
        db.execute('INSERT INTO downtime(telescope,starts,ends,source_id) VALUES(?,?,?,?)',
                   (s['telescope'],int(down['starts']),int(down['ends']),data['source_id']))
    overlaps=db.execute('SELECT id,priority FROM slot_allocation WHERE telescope=? AND id<>? AND status="allocated" AND starts<? AND ends>?',
                        (s['telescope'],sid,ends,starts)).fetchall()
    displaced=[]
    for old in overlaps:
        if int(s['priority'])>old['priority']:
            db.execute('UPDATE slot_allocation SET status="displaced" WHERE id=?',(old['id'],))
            decide(db,'allocation',old['id'],'displaced','higher-priority slot took interval',[sid])
            displaced.append(old['id'])
    conflict=any(old['id'] not in displaced for old in overlaps)
    down=db.execute('SELECT 1 FROM downtime WHERE telescope=? AND starts<? AND ends>? LIMIT 1',
                    (s['telescope'],ends,starts)).fetchone()
    visible=float(s['altitude_deg'])>=30 and float(s['sun_separation_deg'])>=45
    status='conflict' if conflict else 'downtime' if down else 'unobservable' if not visible else 'allocated'
    old=db.execute('SELECT version FROM slot_allocation WHERE id=?',(sid,)).fetchone()
    version=old['version']+1 if old else 1
    db.execute('INSERT INTO slot_allocation(id,telescope,starts,ends,plan,priority,status,version) VALUES(?,?,?,?,?,?,?,?) '
               'ON CONFLICT(id) DO UPDATE SET starts=excluded.starts,ends=excluded.ends,plan=excluded.plan,priority=excluded.priority,status=excluded.status,version=excluded.version',
               (sid,s['telescope'],starts,ends,s['plan'],int(s['priority']),status,version))
    setup=data['setup'];prior=db.execute('SELECT * FROM instrument_setup WHERE slot_id=?',(sid,)).fetchone()
    if prior and int(setup['signed_at'])<prior['signed_at']:
        setup_version=prior['version'];decide(db,'setup',sid,'stale','late older setup does not rewrite exposures',[data['source_id']])
    else:
        setup_version=int(setup['version'])
        db.execute('INSERT INTO instrument_setup(id,slot_id,filter,detector,temperature,exposure_ms,version,signed_at) VALUES(?,?,?,?,?,?,?,?) '
                   'ON CONFLICT(id) DO UPDATE SET filter=excluded.filter,detector=excluded.detector,temperature=excluded.temperature,exposure_ms=excluded.exposure_ms,version=excluded.version,signed_at=excluded.signed_at',
                   (setup['id'],sid,setup['filter'],setup['detector'],float(setup['temperature']),int(setup['exposure_ms']),setup_version,int(setup['signed_at'])))
        decide(db,'setup',sid,'effective',f'setup version {setup_version}',[data['source_id']])
    decide(db,'allocation',sid,status,'priority and interval arbitration',[data['source_id']]+displaced)
    decide(db,'downtime',sid,'blocked' if down else 'clear','downtime intersects allocated interval',[data['source_id']])
    decide(db,'visibility',sid,'visible' if visible else 'unobservable','altitude >=30 and Sun separation >=45',[data['source_id']])
    from .calibration import reconcile_slot as recalibrate
    from .exposure import reconcile_slot as rearchive
    recalibrate(db,sid);rearchive(db,sid)
    for old_id in displaced:
        rearchive(db,old_id)
    return {'slot':sid,'status':status,'version':version,'setup_version':setup_version,'displaced':displaced}
