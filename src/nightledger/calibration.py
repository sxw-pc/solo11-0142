"""Dark and flat frame identity, setup fit and expiry."""
from .store import ingest,decide

def run(db,data):
    state=ingest(db,'calibration',data)
    if state!='new':return {'source_id':data['source_id'],'status':state}
    for frame in data.get('frames',[]):
        old=db.execute('SELECT digest FROM calibration_frame WHERE id=?',(frame['id'],)).fetchone()
        if old:
            verdict='duplicate' if old['digest']==frame['digest'] else 'conflict'
            decide(db,'calibration-frame',frame['id'],verdict,'same frame ID cannot change bytes',[data['source_id']])
            if verdict=='conflict':
                db.execute('UPDATE calibration_frame SET status="conflict" WHERE id=?',(frame['id'],))
                setrow=db.execute('SELECT slot_id FROM calibration_set WHERE id=?',(frame['set_id'],)).fetchone()
                if setrow:
                    reconcile_slot(db,setrow['slot_id'])
                    from .exposure import reconcile_slot as rearchive
                    rearchive(db,setrow['slot_id'])
            continue
        db.execute('INSERT INTO calibration_frame(id,set_id,kind,digest,filter,detector,temperature,exposure_ms,expires_at,healthy,status) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                   (frame['id'],frame['set_id'],frame['kind'],frame['digest'],frame['filter'],frame['detector'],
                    float(frame['temperature']),int(frame['exposure_ms']),int(frame['expires_at']),int(bool(frame['healthy'])),'received'))
        decide(db,'calibration-frame',frame['id'],'received','original frame digest retained',[data['source_id']])
    for cset in data.get('sets',[]):
        old=db.execute('SELECT version FROM calibration_set WHERE id=?',(cset['id'],)).fetchone()
        version=old['version']+1 if old else 1
        db.execute('INSERT INTO calibration_set(id,slot_id,dark_id,flat_id,status,version) VALUES(?,?,?,?,?,?) '
                   'ON CONFLICT(id) DO UPDATE SET dark_id=excluded.dark_id,flat_id=excluded.flat_id,version=excluded.version',
                   (cset['id'],cset['slot_id'],cset['dark_id'],cset['flat_id'],'pending',version))
        reconcile_slot(db,cset['slot_id'])
        from .exposure import reconcile_slot as rearchive
        rearchive(db,cset['slot_id'])
    return {'source_id':data['source_id'],'sets':[dict(r) for r in db.execute('SELECT * FROM calibration_set')]}

def reconcile_slot(db,slot_id):
    slot=db.execute('SELECT * FROM slot_allocation WHERE id=?',(slot_id,)).fetchone()
    setup=db.execute('SELECT * FROM instrument_setup WHERE slot_id=? ORDER BY version DESC LIMIT 1',(slot_id,)).fetchone()
    for cset in db.execute('SELECT * FROM calibration_set WHERE slot_id=?',(slot_id,)).fetchall():
        dark=db.execute('SELECT * FROM calibration_frame WHERE id=?',(cset['dark_id'],)).fetchone()
        flat=db.execute('SELECT * FROM calibration_frame WHERE id=?',(cset['flat_id'],)).fetchone()
        if not slot or not setup or not dark or not flat:status='missing_frame'
        elif not dark['healthy'] or not flat['healthy'] or dark['status']=='conflict' or flat['status']=='conflict':status='quarantined'
        elif dark['expires_at']<slot['ends'] or flat['expires_at']<slot['ends']:status='expired'
        elif dark['kind']!='dark' or flat['kind']!='flat' or dark['detector']!=setup['detector'] or flat['detector']!=setup['detector']:status='mismatch'
        elif abs(dark['temperature']-setup['temperature'])>2 or dark['exposure_ms']!=setup['exposure_ms'] or flat['filter']!=setup['filter']:status='mismatch'
        else:status='valid'
        if status!=cset['status']:
            db.execute('UPDATE calibration_set SET status=?,version=version+1 WHERE id=?',(status,cset['id']))
            decide(db,'calibration-set',cset['id'],status,'dark and flat fit current setup and time',[slot_id,cset['dark_id'],cset['flat_id']])
