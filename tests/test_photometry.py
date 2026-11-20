from pathlib import Path
from nightledger import slot, calibration, exposure, photometry
from nightledger.store import connect, read

ROOT = Path(__file__).resolve().parents[1] / 'fixtures'


def ready(db):
    with db:
        slot.run(db, read(ROOT / 'slot-ok.json'))
        calibration.run(db, read(ROOT / 'calibration-ok.json'))
        exposure.run(db, read(ROOT / 'exposure-photometry.json'))


def test_lightcurve_reacts_to_archive_change(tmp_path):
    db = connect(tmp_path / 'photometry.sqlite')
    ready(db)
    with db:
        result = photometry.run(db, read(ROOT / 'photometry-dip.json'))
    assert result['status'] == 'candidate'
    assert result['usable_frames'] == 4
    with db:
        calibration.run(db, read(ROOT / 'calibration-conflict.json'))
    status = db.execute("SELECT status FROM photometry_session WHERE id='lightcurve-X-1'").fetchone()[0]
    assert status == 'pending'
    events = db.execute("SELECT status FROM photometry_event WHERE session_id='lightcurve-X-1' ORDER BY id").fetchall()
    assert [r[0] for r in events] == ['candidate', 'pending']
    for purpose in ('followup', 'catalogue'):
        product_events = db.execute('SELECT status FROM photometry_product_event '
                                    'WHERE session_id=? AND purpose=? ORDER BY id',
                                    ('lightcurve-X-1', purpose)).fetchall()
        assert [r[0] for r in product_events] == ['candidate', 'pending']
    db.close()


def test_source_replay_is_idempotent(tmp_path):
    db = connect(tmp_path / 'photometry.sqlite')
    ready(db)
    data = read(ROOT / 'photometry-dip.json')
    with db:
        photometry.run(db, data)
        second = photometry.run(db, data)
    assert second['status'] == 'duplicate'
    assert db.execute('SELECT count(*) FROM photometry_measurement').fetchone()[0] == 4
    db.close()
