import json
from pathlib import Path

import pytest

from nightledger import slot, calibration, exposure, photometry, report
from nightledger.store import connect, read

ROOT = Path(__file__).resolve().parents[1] / 'fixtures'


def ready(db):
    with db:
        slot.run(db, read(ROOT / 'slot-ok.json'))
        calibration.run(db, read(ROOT / 'calibration-ok.json'))
        exposure.run(db, read(ROOT / 'exposure-photometry.json'))


def ready_ensemble(db):
    with db:
        slot.run(db, read(ROOT / 'slot-ok.json'))
        slot.run(db, read(ROOT / 'slot-night-2.json'))
        calibration.run(db, read(ROOT / 'calibration-ok.json'))
        calibration.run(db, read(ROOT / 'calibration-ok-2.json'))
        exposure.run(db, read(ROOT / 'exposure-ensemble.json'))


def product(db, sid, purpose):
    return db.execute('SELECT status,reason FROM photometry_product '
                      'WHERE session_id=? AND purpose=?', (sid, purpose)).fetchone()


def product_events(db, sid, purpose):
    return [r[0] for r in db.execute('SELECT status FROM photometry_product_event '
                                     'WHERE session_id=? AND purpose=? ORDER BY id',
                                     (sid, purpose)).fetchall()]


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
        assert product_events(db, 'lightcurve-X-1', purpose) == ['candidate', 'pending']
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


def test_ensemble_screens_variable_comparison(tmp_path):
    db = connect(tmp_path / 'ensemble.sqlite')
    ready_ensemble(db)
    with db:
        result = photometry.run(db, read(ROOT / 'photometry-ensemble.json'))
    # 正式表：重叠链与星表锚点识别出 C 增亮，目标无暗化
    assert result['status'] == 'steady'
    assert result['usable_frames'] == 4
    assert result['catalogue']['status'] == 'steady'
    # 跟进单：以 C 稳定为条件保留有条件暗化线索
    assert result['followup']['status'] == 'candidate'
    comps = {r['star']: (r['status'], r['designated'])
             for r in db.execute('SELECT star,status,designated FROM photometry_comparison')}
    assert comps == {'star-A': ('stable', 0), 'star-B': ('stable', 0), 'star-C': ('variable', 1)}
    status, reason = product(db, 'lightcurve-X-2', 'followup')
    assert status == 'candidate'
    assert '以 star-C 稳定为条件' in reason
    assert '补拍' in reason
    status, reason = product(db, 'lightcurve-X-2', 'catalogue')
    assert status == 'steady'
    assert 'star-A' in reason and 'star-B' in reason
    assert 'star-C' in reason and '重叠链' in reason
    frames = db.execute('SELECT frame_id,relative_flux,refs,status FROM photometry_frame_result '
                        'ORDER BY frame_id').fetchall()
    assert [f['status'] for f in frames] == ['usable'] * 4
    assert all(abs(f['relative_flux'] - 1.0) < 0.02 for f in frames)
    refs = {f['frame_id']: json.loads(f['refs']) for f in frames}
    assert refs['e-1'] == ['star-A', 'star-B']
    assert refs['e-3'] == ['star-A']
    assert refs['e-4'] == ['star-B']
    detail = json.loads(db.execute("SELECT detail FROM photometry_product_detail "
                                   "WHERE session_id='lightcurve-X-2' AND purpose='catalogue'").fetchone()[0])
    assert detail['anchors']['star-A'] == {'mag': 12.0, 'err': 0.02}
    assert detail['excluded']['star-C']['status'] == 'variable'
    assert detail['frames'] == ['e-1', 'e-2', 'e-3', 'e-4']
    assert detail['source_digest']
    fup = json.loads(db.execute("SELECT detail FROM photometry_product_detail "
                                "WHERE session_id='lightcurve-X-2' AND purpose='followup'").fetchone()[0])
    assert fup['premise'] == 'star-C 稳定'
    assert fup['references'] == ['star-C']
    assert fup['frames'] == ['e-1', 'e-2', 'e-3', 'e-4']
    assert fup['ensemble_note']
    db.close()


def test_ensemble_single_link_degeneracy_is_indeterminate(tmp_path):
    db = connect(tmp_path / 'blind.sqlite')
    with db:
        slot.run(db, read(ROOT / 'slot-ok.json'))
        calibration.run(db, read(ROOT / 'calibration-ok.json'))
        exposure.run(db, read(ROOT / 'exposure-ensemble-blind.json'))
        result = photometry.run(db, read(ROOT / 'photometry-ensemble-blind.json'))
    # 检验帧只有 C 单链：目标变暗与 C 变亮不可区分，正式表不得沿用无暗化
    assert result['status'] == 'indeterminate'
    assert result['catalogue']['status'] == 'indeterminate'
    assert result['followup']['status'] == 'candidate'
    status, reason = product(db, 'lightcurve-X-3', 'catalogue')
    assert status == 'indeterminate'
    assert '不可区分' in reason
    assert 'star-C' in reason
    status, reason = product(db, 'lightcurve-X-3', 'followup')
    assert status == 'candidate'
    assert '以 star-C 稳定为条件' in reason
    db.close()


def test_ensemble_confirms_real_dimming(tmp_path):
    db = connect(tmp_path / 'dip.sqlite')
    with db:
        slot.run(db, read(ROOT / 'slot-ok.json'))
        calibration.run(db, read(ROOT / 'calibration-ok.json'))
        exposure.run(db, read(ROOT / 'exposure-ensemble-dip.json'))
        result = photometry.run(db, read(ROOT / 'photometry-ensemble-dip.json'))
    # 目标真暗化不被零点吸收：比较星全部稳定，正式表确认暗化
    assert result['status'] == 'candidate'
    assert result['catalogue']['status'] == 'candidate'
    assert result['followup']['status'] == 'candidate'
    assert min(result['ratios']) <= 0.92
    comps = {r['star']: r['status'] for r in db.execute('SELECT star,status FROM photometry_comparison')}
    assert comps == {'star-A': 'stable', 'star-B': 'stable', 'star-C': 'stable'}
    db.close()


def test_ensemble_calibration_conflict_recomputes_both_products(tmp_path):
    db = connect(tmp_path / 'retract.sqlite')
    ready_ensemble(db)
    with db:
        photometry.run(db, read(ROOT / 'photometry-ensemble.json'))
    assert product(db, 'lightcurve-X-2', 'catalogue')[0] == 'steady'
    assert product(db, 'lightcurve-X-2', 'followup')[0] == 'candidate'
    with db:
        calibration.run(db, read(ROOT / 'calibration-conflict-2.json'))
    # 两张关键帧不可归档，其余帧仍可用
    archive = {r['frame_id']: r['status'] for r in db.execute('SELECT frame_id,status FROM archive_case')}
    assert archive['e-1'] == 'archivable' and archive['e-2'] == 'archivable'
    assert archive['e-3'] == 'pending_calibration' and archive['e-4'] == 'pending_calibration'
    # 两项结果都从仍可用帧重算，前后结论都保留
    products = {r['purpose']: r['status'] for r in db.execute(
        "SELECT purpose,status FROM photometry_product WHERE session_id='lightcurve-X-2'")}
    assert products == {'followup': 'pending', 'catalogue': 'pending'}
    assert product_events(db, 'lightcurve-X-2', 'followup') == ['candidate', 'pending']
    assert product_events(db, 'lightcurve-X-2', 'catalogue') == ['steady', 'pending']
    session_events = [r[0] for r in db.execute(
        "SELECT status FROM photometry_event WHERE session_id='lightcurve-X-2' ORDER BY id")]
    assert session_events == ['steady', 'pending']
    measurements = {r['frame_id']: r['status'] for r in db.execute(
        'SELECT frame_id,status FROM photometry_measurement')}
    assert measurements['e-1'] == 'usable' and measurements['e-2'] == 'usable'
    assert measurements['e-3'] == 'excluded' and measurements['e-4'] == 'excluded'
    # 重算幂等：再次调和不产生新事件
    before = db.execute('SELECT count(*) FROM photometry_product_event').fetchone()[0]
    with db:
        photometry.reconcile(db, 'lightcurve-X-2')
    assert db.execute('SELECT count(*) FROM photometry_product_event').fetchone()[0] == before
    # 报告包含多比较星证据表
    built = report.build(db)
    for table in ('photometry_comparison', 'photometry_frame_star',
                  'photometry_frame_result', 'photometry_product_detail'):
        assert table in built
    db.close()


def test_ensemble_saturated_reference_leaves_frame_unanchored(tmp_path):
    db = connect(tmp_path / 'saturated.sqlite')
    ready_ensemble(db)
    data = read(ROOT / 'photometry-ensemble.json')
    data['source_id'] = 'photometry-ensemble-saturated'
    data['session'] = dict(data['session'], id='lightcurve-X-5')
    for row in data['measurements']:
        if row['frame_id'] == 'e-3':
            row['comparisons']['star-A'] = {'aperture': 10100, 'sky': 10, 'saturated': True}
    with db:
        result = photometry.run(db, data)
    # e-3 只剩被排除的 C：无稳定参照可锚定，正式表不可判
    assert result['status'] == 'indeterminate'
    status, reason = product(db, 'lightcurve-X-5', 'catalogue')
    assert status == 'indeterminate'
    assert 'e-3' in reason
    star_a = db.execute("SELECT status FROM photometry_frame_star "
                        "WHERE frame_id='e-3' AND star='star-A'").fetchone()[0]
    assert star_a == 'saturated'
    assert product(db, 'lightcurve-X-5', 'followup')[0] == 'candidate'
    db.close()


def test_ensemble_rejects_unregistered_star(tmp_path):
    db = connect(tmp_path / 'invalid.sqlite')
    ready_ensemble(db)
    data = read(ROOT / 'photometry-ensemble.json')
    data['source_id'] = 'photometry-ensemble-typo'
    data['session'] = dict(data['session'], id='lightcurve-X-6')
    data['measurements'][0]['comparisons']['star-D'] = {'aperture': 10100, 'sky': 10}
    with db:
        with pytest.raises(ValueError):
            photometry.run(db, data)
    assert db.execute("SELECT count(*) FROM photometry_session WHERE id='lightcurve-X-6'").fetchone()[0] == 0
    db.close()
