"""Offline aperture measurements and a first-pass differential light curve."""
from statistics import median
from .store import ingest, decide


def run(db, data):
    state = ingest(db, 'photometry', data)
    if state != 'new':
        return {'source_id': data['source_id'], 'status': state}
    session = data['session']
    sid = session['id']
    if db.execute('SELECT 1 FROM photometry_session WHERE id=?', (sid,)).fetchone():
        raise ValueError('photometry session already exists')
    db.execute(
        'INSERT INTO photometry_session(id,slot_id,target,comparison,drop_fraction,status,version) '
        'VALUES(?,?,?,?,?,?,1)',
        (sid, session['slot_id'], session['target'], session['comparison'],
         float(session['drop_fraction']), 'pending'),
    )
    for purpose in ('followup', 'catalogue'):
        db.execute('INSERT INTO photometry_product(session_id,purpose,status,reason,version) VALUES(?,?,?,?,1)',
                   (sid, purpose, 'pending', 'measurements not yet reconciled'))
    seen = set()
    for row in data['measurements']:
        frame_id = row['frame_id']
        if frame_id in seen or db.execute('SELECT 1 FROM photometry_measurement WHERE frame_id=?', (frame_id,)).fetchone():
            raise ValueError('duplicate measured frame')
        seen.add(frame_id)
        pixels = int(row['pixels'])
        if pixels <= 0:
            raise ValueError('aperture pixel count must be positive')
        db.execute(
            'INSERT INTO photometry_measurement(frame_id,session_id,target_aperture,target_sky,'
            'comparison_aperture,comparison_sky,pixels,ratio,status) VALUES(?,?,?,?,?,?,?,?,?)',
            (frame_id, sid, float(row['target_aperture']), float(row['target_sky']),
             float(row['comparison_aperture']), float(row['comparison_sky']), pixels, None, 'received'),
        )
    return reconcile(db, sid)


def reconcile_slot(db, slot_id):
    for row in db.execute('SELECT id FROM photometry_session WHERE slot_id=?', (slot_id,)).fetchall():
        reconcile(db, row['id'])


def reconcile(db, sid):
    session = db.execute('SELECT * FROM photometry_session WHERE id=?', (sid,)).fetchone()
    rows = db.execute(
        'SELECT m.*, f.at, f.target AS frame_target, a.status AS archive_status '
        'FROM photometry_measurement m LEFT JOIN science_frame f ON m.frame_id=f.id '
        'LEFT JOIN archive_case a ON m.frame_id=a.frame_id '
        'WHERE m.session_id=? ORDER BY f.at,m.frame_id', (sid,),
    ).fetchall()
    ratios = []
    for row in rows:
        target = row['target_aperture'] - row['pixels'] * row['target_sky']
        comparison = row['comparison_aperture'] - row['pixels'] * row['comparison_sky']
        valid = (row['archive_status'] == 'archivable' and row['frame_target'] == session['target']
                 and target > 0 and comparison > 0)
        status = 'usable' if valid else 'excluded'
        ratio = target / comparison if valid else None
        db.execute('UPDATE photometry_measurement SET ratio=?,status=? WHERE frame_id=?',
                   (ratio, status, row['frame_id']))
        if valid:
            ratios.append(ratio)
    # The first two usable exposures establish the provisional local baseline.
    if len(ratios) < 3:
        status, reason = 'pending', 'fewer than three usable exposures'
    else:
        baseline = median(ratios[:2])
        low = min(ratios[2:])
        status = 'candidate' if low <= baseline * (1 - session['drop_fraction']) else 'steady'
        reason = f'baseline={baseline:.6f}; lowest later ratio={low:.6f}'
    if (session['status'],) != (status,):
        db.execute('UPDATE photometry_session SET status=?,version=version+1 WHERE id=?', (status, sid))
        old = db.execute('SELECT id FROM photometry_event WHERE session_id=? ORDER BY id DESC LIMIT 1',
                         (sid,)).fetchone()
        db.execute('INSERT INTO photometry_event(session_id,status,reason,previous) VALUES(?,?,?,?)',
                   (sid, status, reason, old['id'] if old else None))
        decide(db, 'photometry', sid, status, reason, [r['frame_id'] for r in rows])
    # Two receiving products already have separate histories. Both currently inherit
    # the one-comparison quick-look result; the catalogue product has no ensemble logic yet.
    for purpose in ('followup', 'catalogue'):
        old_product = db.execute('SELECT status FROM photometry_product WHERE session_id=? AND purpose=?',
                                 (sid, purpose)).fetchone()
        if old_product['status'] == status:
            continue
        db.execute('UPDATE photometry_product SET status=?,reason=?,version=version+1 '
                   'WHERE session_id=? AND purpose=?', (status, reason, sid, purpose))
        previous = db.execute('SELECT id FROM photometry_product_event '
                              'WHERE session_id=? AND purpose=? ORDER BY id DESC LIMIT 1',
                              (sid, purpose)).fetchone()
        db.execute('INSERT INTO photometry_product_event(session_id,purpose,status,reason,previous) '
                   'VALUES(?,?,?,?,?)', (sid, purpose, status, reason,
                                         previous['id'] if previous else None))
    return {'session': sid, 'status': status, 'usable_frames': len(ratios), 'ratios': ratios}
