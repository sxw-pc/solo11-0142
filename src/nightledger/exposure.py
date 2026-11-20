"""Science-frame pointing, calibration coverage and reversible archive lineage."""
from .store import ingest,decide

def run(db,data):
    state=ingest(db,'exposure',data)
    if state!='new':return {'source_id':data['source_id'],'status':state}
    slots=set();out=[]
    for frame in data['frames']:
        fid=frame['id'];old=db.execute('SELECT digest FROM science_frame WHERE id=?',(fid,)).fetchone()
        if old:
            status='duplicate' if old['digest']==frame['digest'] else 'conflict'
            decide(db,'science-identity',fid,status,'same frame ID cannot change bytes',[data['source_id']])
            if status=='conflict':
                db.execute('UPDATE archive_case SET status="conflict",version=version+1 WHERE frame_id=?',(fid,))
                db.execute('INSERT INTO archive_event(frame_id,status,reason,previous) VALUES(?,?,?,?)',
                           (fid,'conflict','same frame ID has different bytes',None))
            out.append({'frame':fid,'status':status});continue
        db.execute('INSERT INTO science_frame(id,slot_id,at,digest,target,pointing_arcsec,status,replaces) VALUES(?,?,?,?,?,?,?,?)',
                   (fid,frame['slot_id'],int(frame['at']),frame['digest'],frame['target'],float(frame['pointing_arcsec']),'received',frame.get('replaces')))
        decide(db,'science-identity',fid,'received','raw science frame retained',[data['source_id']])
        slots.add(frame['slot_id']);out.append({'frame':fid,'status':'received'})
    for slot_id in slots:reconcile_slot(db,slot_id)
    return {'source_id':data['source_id'],'frames':out}

def reconcile_slot(db,slot_id):
    slot=db.execute('SELECT * FROM slot_allocation WHERE id=?',(slot_id,)).fetchone()
    sets=db.execute('SELECT * FROM calibration_set WHERE slot_id=? AND status="valid" ORDER BY version DESC LIMIT 1',(slot_id,)).fetchone()
    for frame in db.execute('SELECT * FROM science_frame WHERE slot_id=? ORDER BY at,id',(slot_id,)).fetchall():
        old=db.execute('SELECT * FROM archive_case WHERE frame_id=?',(frame['id'],)).fetchone()
        if old and old['status']=='replaced':continue
        previous=db.execute('SELECT status FROM archive_case WHERE frame_id=?',(frame['replaces'],)).fetchone() if frame['replaces'] else None
        if not slot or slot['status']!='allocated' or not(slot['starts']<=frame['at']<slot['ends']):status='slot_invalid'
        elif frame['target']!=slot['plan']:status='target_mismatch'
        elif frame['pointing_arcsec']>5:status='pointing_failed'
        elif frame['replaces'] and (not previous or previous['status'] not in ('pointing_failed','replaced')):status='invalid_retake'
        elif not sets:status='pending_calibration'
        else:status='archivable'
        calversion=sets['version'] if sets else 0
        if old and (old['status'],old['calibration_version'])==(status,calversion):continue
        version=old['version']+1 if old else 1
        db.execute('INSERT INTO archive_case(frame_id,status,reason,calibration_version,version) VALUES(?,?,?,?,?) '
                   'ON CONFLICT(frame_id) DO UPDATE SET status=excluded.status,reason=excluded.reason,calibration_version=excluded.calibration_version,version=excluded.version',
                   (frame['id'],status,'slot, pointing, retake and calibration coverage',calversion,version))
        prior=db.execute('SELECT id FROM archive_event WHERE frame_id=? ORDER BY id DESC LIMIT 1',(frame['id'],)).fetchone()
        db.execute('INSERT INTO archive_event(frame_id,status,reason,previous) VALUES(?,?,?,?)',
                   (frame['id'],status,'recomputed from persisted evidence',prior['id'] if prior else None))
        db.execute('UPDATE science_frame SET status=? WHERE id=?',(status,frame['id']))
        decide(db,'archive',frame['id'],status,f'calibration set version {calversion}',[slot_id,frame['replaces']])
        if status=='archivable' and frame['replaces']:
            db.execute('UPDATE archive_case SET status="replaced",version=version+1 WHERE frame_id=?',(frame['replaces'],))
            db.execute('INSERT INTO archive_event(frame_id,status,reason,previous) VALUES(?,?,?,?)',
                       (frame['replaces'],'replaced','good retake supersedes failed frame',None))
    from .photometry import reconcile_slot as remeasure
    remeasure(db,slot_id)
