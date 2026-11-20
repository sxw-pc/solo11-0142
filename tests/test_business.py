from pathlib import Path
from nightledger import slot,calibration,exposure,report
from nightledger.store import connect,read

ROOT=Path(__file__).resolve().parents[1]/'fixtures'
def data(name):return read(ROOT/name)
def db(tmp):return connect(tmp/'case.sqlite')
def seeded(tmp):
    cx=db(tmp)
    with cx:
        slot.run(cx,data('slot-ok.json'))
        calibration.run(cx,data('calibration-ok.json'))
        exposure.run(cx,data('exposure-ok.json'))
    return cx

def test_baseline_archivable_with_matching_dark_and_flat(tmp_path):
    cx=seeded(tmp_path)
    assert cx.execute('SELECT status FROM calibration_set').fetchone()[0]=='valid'
    assert cx.execute('SELECT status FROM archive_case').fetchone()[0]=='archivable'

def test_overlapping_slot_conflicts(tmp_path):
    cx=seeded(tmp_path)
    with cx:result=slot.run(cx,data('slot-conflict.json'))
    assert result['status']=='conflict'
    assert cx.execute('SELECT status FROM slot_allocation WHERE id="night-1"').fetchone()[0]=='allocated'

def test_higher_priority_displaces_prior_exposure(tmp_path):
    cx=seeded(tmp_path)
    higher=data('slot-conflict.json');higher['source_id']='priority';higher['slot']['priority']=2
    with cx:result=slot.run(cx,higher)
    assert result['displaced']==['night-1']
    assert cx.execute('SELECT status FROM archive_case WHERE frame_id="science-1"').fetchone()[0]=='slot_invalid'

def test_downtime_retracts_archived_frame(tmp_path):
    cx=seeded(tmp_path)
    revised=data('slot-ok.json');revised['source_id']='downtime-v2'
    revised['downtime']=[{'starts':50,'ends':500}]
    with cx:slot.run(cx,revised)
    assert cx.execute('SELECT status FROM slot_allocation WHERE id="night-1"').fetchone()[0]=='downtime'
    assert cx.execute('SELECT status FROM archive_case WHERE frame_id="science-1"').fetchone()[0]=='slot_invalid'

def test_unobservable_target_fails_visibility(tmp_path):
    cx=db(tmp_path)
    low=data('slot-ok.json');low['slot']['altitude_deg']=20
    with cx:result=slot.run(cx,low)
    assert result['status']=='unobservable'

def test_filter_revision_invalidates_flat_and_archive(tmp_path):
    cx=seeded(tmp_path)
    revised=data('slot-ok.json');revised['source_id']='filter-v2'
    revised['setup'].update({'filter':'B','version':2,'signed_at':0})
    with cx:slot.run(cx,revised)
    assert cx.execute('SELECT status FROM calibration_set').fetchone()[0]=='mismatch'
    assert cx.execute('SELECT status FROM archive_case').fetchone()[0]=='pending_calibration'

def test_dark_temperature_mismatch_prevents_calibration(tmp_path):
    cx=db(tmp_path)
    bad=data('calibration-ok.json');bad['frames'][0]['temperature']=-2
    with cx:
        slot.run(cx,data('slot-ok.json'))
        calibration.run(cx,bad)
    assert cx.execute('SELECT status FROM calibration_set').fetchone()[0]=='mismatch'

def test_expired_frame_quarantines_set(tmp_path):
    cx=db(tmp_path)
    expired=data('calibration-ok.json');expired['frames'][0]['expires_at']=100
    with cx:
        slot.run(cx,data('slot-ok.json'))
        calibration.run(cx,expired)
    assert cx.execute('SELECT status FROM calibration_set').fetchone()[0]=='expired'

def test_same_frame_id_conflict_keeps_original_digest_and_retracts_archive(tmp_path):
    cx=seeded(tmp_path)
    with cx:calibration.run(cx,data('calibration-conflict.json'))
    assert cx.execute('SELECT digest FROM calibration_frame WHERE id="dark-1"').fetchone()[0]=='dark-bytes'
    assert cx.execute('SELECT status FROM calibration_set').fetchone()[0]=='quarantined'
    assert cx.execute('SELECT status FROM archive_case').fetchone()[0]=='pending_calibration'

def test_pointing_failure_and_good_retake_preserve_lineage(tmp_path):
    cx=db(tmp_path)
    bad=data('exposure-ok.json');bad['frames'][0]['pointing_arcsec']=9
    good={'source_id':'retake','frames':[{'id':'science-2','slot_id':'night-1','at':200,
        'digest':'new-bytes','target':'target-X','pointing_arcsec':1,'replaces':'science-1'}]}
    with cx:
        slot.run(cx,data('slot-ok.json'))
        calibration.run(cx,data('calibration-ok.json'))
        exposure.run(cx,bad)
        exposure.run(cx,good)
    rows={r['frame_id']:r['status'] for r in cx.execute('SELECT frame_id,status FROM archive_case')}
    assert rows=={'science-1':'replaced','science-2':'archivable'}
    assert cx.execute('SELECT COUNT(*) FROM archive_event WHERE frame_id="science-1"').fetchone()[0]==2

def test_science_frame_identity_conflict_retracts_archive(tmp_path):
    cx=seeded(tmp_path)
    with cx:exposure.run(cx,data('exposure-conflict.json'))
    assert cx.execute('SELECT digest FROM science_frame').fetchone()[0]=='science-bytes'
    assert cx.execute('SELECT status FROM archive_case').fetchone()[0]=='conflict'

def test_report_retains_prior_archive_event(tmp_path):
    cx=seeded(tmp_path)
    revised=data('slot-ok.json');revised['source_id']='filter-v2';revised['setup'].update({'filter':'B','version':2,'signed_at':0})
    with cx:slot.run(cx,revised)
    history=report.build(cx)['archive_event']
    assert [e['status'] for e in history]==['archivable','pending_calibration']
