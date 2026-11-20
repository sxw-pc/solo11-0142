"""离线孔径测光采录与差分光变曲线。

两种采录形态共用本模块：

* 旧版单比较星行（``comparison_aperture``/``comparison_sky``），行为保持不变；
* 多比较星采录：会话级 ``comparisons`` 注册比较星身份与星表锚点（星等及误差），
  逐帧 ``comparisons`` 给出各星孔径计数、缺测与饱和标记，同一星在各帧标识一致。

多比较星会话用整夜重叠帧求解零点，用成对恒定链（重叠链）筛查比较星自身变化，
星表锚点提供绝对参照。只有重叠链可辨识、且检验帧拥有足够独立的锚定稳定参照时，
正式测光表才给出可发布结论；缺少锚点时明确报 ``indeterminate`` 并说明结论仍取决
于哪些比较星假设，绝不补造确定结果。当夜跟进单始终只是以观测员指定比较星稳定为
条件的快览，附补拍建议，与正式表互不替代。
"""
import json
import math
from statistics import median

from .store import ingest, decide, digest_of

MAG_SIGMA = 2.5 / math.log(10)  # 星等误差 = MAG_SIGMA * 相对通量误差
PAIR_ABS_TOL = 0.05             # 重叠链星等突变绝对容限
PAIR_SIGMA_TOL = 4.0            # 重叠链星等突变统计容限（光子噪声倍数）
MIN_USABLE_FRAMES = 3
MIN_INDEPENDENT_REFS = 2


def run(db, data):
    state = ingest(db, 'photometry', data)
    if state != 'new':
        return {'source_id': data['source_id'], 'status': state}
    session = data['session']
    sid = session['id']
    if db.execute('SELECT 1 FROM photometry_session WHERE id=?', (sid,)).fetchone():
        raise ValueError('photometry session already exists')
    registry = session.get('comparisons')
    if registry is not None:
        if not registry:
            raise ValueError('comparison registry must not be empty')
        if session['comparison'] not in registry:
            raise ValueError('designated comparison star is not registered')
        for star, anchor in registry.items():
            if not isinstance(star, str) or not star:
                raise ValueError('comparison star identity must be a non-empty string')
            mag, err = anchor.get('mag'), anchor.get('err')
            if (mag is None) != (err is None):
                raise ValueError('catalogue anchor needs both mag and err')
            if err is not None and float(err) <= 0:
                raise ValueError('catalogue anchor error must be positive')
    # Validate the whole payload before any write so a rejected intake leaves no rows.
    seen = set()
    for row in data['measurements']:
        frame_id = row['frame_id']
        if frame_id in seen or db.execute('SELECT 1 FROM photometry_measurement WHERE frame_id=?', (frame_id,)).fetchone():
            raise ValueError('duplicate measured frame')
        seen.add(frame_id)
        if int(row['pixels']) <= 0:
            raise ValueError('aperture pixel count must be positive')
        if registry is None:
            continue
        unknown = sorted(set(row.get('comparisons', {})) - set(registry))
        if unknown:
            raise ValueError(f'unregistered comparison star: {unknown[0]}')
        if not row.get('target_missing') and (row.get('target_aperture') is None or row.get('target_sky') is None):
            raise ValueError('target aperture and sky required unless target_missing')
        for star, entry in row.get('comparisons', {}).items():
            if not entry.get('missing') and (entry.get('aperture') is None or entry.get('sky') is None):
                raise ValueError(f'comparison {star} aperture and sky required unless missing')
    db.execute(
        'INSERT INTO photometry_session(id,slot_id,target,comparison,drop_fraction,status,version) '
        'VALUES(?,?,?,?,?,?,1)',
        (sid, session['slot_id'], session['target'], session['comparison'],
         float(session['drop_fraction']), 'pending'),
    )
    for purpose in ('followup', 'catalogue'):
        db.execute('INSERT INTO photometry_product(session_id,purpose,status,reason,version) VALUES(?,?,?,?,1)',
                   (sid, purpose, 'pending', 'measurements not yet reconciled'))
    db.execute('INSERT INTO photometry_intake(session_id,source_id,digest,kind) VALUES(?,?,?,?)',
               (sid, data['source_id'], digest_of(data), 'ensemble' if registry is not None else 'single'))
    for row in data['measurements']:
        frame_id = row['frame_id']
        pixels = int(row['pixels'])
        if registry is None:
            db.execute(
                'INSERT INTO photometry_measurement(frame_id,session_id,target_aperture,target_sky,'
                'comparison_aperture,comparison_sky,pixels,ratio,status) VALUES(?,?,?,?,?,?,?,?,?)',
                (frame_id, sid, float(row['target_aperture']), float(row['target_sky']),
                 float(row['comparison_aperture']), float(row['comparison_sky']), pixels, None, 'received'),
            )
            continue
        observed = row.get('comparisons', {})
        target_missing = bool(row.get('target_missing'))
        target_saturated = bool(row.get('target_saturated'))
        target_aperture, target_sky = row.get('target_aperture'), row.get('target_sky')
        db.execute(
            'INSERT INTO photometry_measurement(frame_id,session_id,target_aperture,target_sky,'
            'comparison_aperture,comparison_sky,pixels,ratio,status) VALUES(?,?,?,?,?,?,?,?,?)',
            (frame_id, sid,
             None if target_aperture is None else float(target_aperture),
             None if target_sky is None else float(target_sky),
             None, None, pixels, None, 'received'),
        )
        db.execute(
            'INSERT INTO photometry_frame_result(frame_id,session_id,target_missing,target_saturated,'
            'relative_flux,flux_err,refs,status,reason,version) VALUES(?,?,?,?,?,?,?,?,?,1)',
            (frame_id, sid, int(target_missing), int(target_saturated),
             None, None, None, 'received', 'awaiting reconcile'),
        )
        for star in sorted(registry):
            entry = observed.get(star)
            missing = entry is None or bool(entry.get('missing'))
            saturated = bool(entry.get('saturated')) if entry else False
            aperture = None if entry is None else entry.get('aperture')
            sky = None if entry is None else entry.get('sky')
            flux, status, reason = None, 'received', None
            if missing:
                status, reason = 'missing', 'comparison not measured on this frame'
            else:
                flux = float(aperture) - pixels * float(sky)
                if saturated:
                    status, reason = 'saturated', 'comparison flagged saturated'
                elif flux <= 0:
                    status, reason = 'excluded', 'sky-subtracted comparison flux not positive'
            db.execute(
                'INSERT INTO photometry_frame_star(frame_id,session_id,star,aperture,sky,pixels,'
                'missing,saturated,flux,status,reason) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (frame_id, sid, star,
                 None if aperture is None else float(aperture),
                 None if sky is None else float(sky),
                 pixels, int(missing), int(saturated), flux, status, reason),
            )
    if registry is not None:
        for star in sorted(registry):
            anchor = registry[star]
            db.execute(
                'INSERT INTO photometry_comparison(session_id,star,catalog_mag,catalog_err,designated,'
                'status,reason,version) VALUES(?,?,?,?,?,?,?,1)',
                (sid, star,
                 None if anchor.get('mag') is None else float(anchor['mag']),
                 None if anchor.get('err') is None else float(anchor['err']),
                 int(star == session['comparison']), 'pending', 'awaiting reconcile'),
            )
    return reconcile(db, sid)


def reconcile_slot(db, slot_id):
    sids = {r['id'] for r in db.execute('SELECT id FROM photometry_session WHERE slot_id=?', (slot_id,))}
    sids |= {r['session_id'] for r in db.execute(
        'SELECT DISTINCT m.session_id FROM photometry_measurement m '
        'JOIN science_frame f ON m.frame_id=f.id WHERE f.slot_id=?', (slot_id,))}
    for sid in sorted(sids):
        reconcile(db, sid)


def reconcile(db, sid):
    session = db.execute('SELECT * FROM photometry_session WHERE id=?', (sid,)).fetchone()
    rows = db.execute(
        'SELECT m.*, f.at, f.target AS frame_target, a.status AS archive_status '
        'FROM photometry_measurement m LEFT JOIN science_frame f ON m.frame_id=f.id '
        'LEFT JOIN archive_case a ON m.frame_id=a.frame_id '
        'WHERE m.session_id=? ORDER BY f.at,m.frame_id', (sid,),
    ).fetchall()
    ensemble = db.execute('SELECT 1 FROM photometry_comparison WHERE session_id=? LIMIT 1', (sid,)).fetchone()
    if ensemble:
        return _reconcile_ensemble(db, sid, session, rows)
    return _reconcile_single(db, sid, session, rows)


def _reconcile_single(db, sid, session, rows):
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
    # Legacy single-comparison sessions keep both receiving products on the
    # quick-look result; independent reference adjudication needs ensemble intake.
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


def _solve(equations, size):
    """Dense normal-equation least squares; returns None when singular."""
    matrix = [[0.0] * (size + 1) for _ in range(size)]
    for coeffs, rhs, weight in equations:
        for i, ci in coeffs.items():
            matrix[i][size] += weight * ci * rhs
            for j, cj in coeffs.items():
                matrix[i][j] += weight * ci * cj
    for i in range(size):
        matrix[i][i] += 1e-9
    for col in range(size):
        pivot = max(range(col, size), key=lambda r: abs(matrix[r][col]))
        if abs(matrix[pivot][col]) < 1e-12:
            return None
        matrix[col], matrix[pivot] = matrix[pivot], matrix[col]
        for r in range(col + 1, size):
            factor = matrix[r][col] / matrix[col][col]
            if factor:
                for c in range(col, size + 1):
                    matrix[r][c] -= factor * matrix[col][c]
    out = [0.0] * size
    for r in range(size - 1, -1, -1):
        out[r] = (matrix[r][size] - sum(matrix[r][c] * out[c] for c in range(r + 1, size))) / matrix[r][r]
    return out


def _reconcile_ensemble(db, sid, session, rows):
    stars = {r['star']: dict(r) for r in db.execute(
        'SELECT * FROM photometry_comparison WHERE session_id=?', (sid,))}
    flags = {r['frame_id']: r for r in db.execute(
        'SELECT * FROM photometry_frame_result WHERE session_id=?', (sid,))}
    star_rows = {}
    for r in db.execute('SELECT * FROM photometry_frame_star WHERE session_id=?', (sid,)):
        star_rows.setdefault(r['frame_id'], {})[r['star']] = r
    intake = db.execute('SELECT * FROM photometry_intake WHERE session_id=?', (sid,)).fetchone()
    drop = float(session['drop_fraction'])

    # ---- 1. frame usability follows the exposure ledger -------------------
    frames = []
    for row in rows:
        fid = row['frame_id']
        flag = flags[fid]
        reason, flux = None, None
        if row['archive_status'] != 'archivable':
            reason = f"归档状态不可用：{row['archive_status'] or '无归档记录'}"
        elif row['frame_target'] != session['target']:
            reason = '帧目标与会话目标不一致'
        elif flag['target_missing']:
            reason = '目标星缺测'
        elif flag['target_saturated']:
            reason = '目标星饱和'
        elif row['target_aperture'] is None or row['target_sky'] is None:
            reason = '目标星缺测'
        else:
            flux = row['target_aperture'] - row['pixels'] * row['target_sky']
            if flux <= 0:
                reason, flux = '目标星扣天光后通量非正', None
        frames.append({'frame_id': fid, 'row': row, 'usable': reason is None,
                       'reason': reason, 'target_flux': flux})
    usable = [f for f in frames if f['usable']]
    frame_by_id = {f['frame_id']: f for f in frames}

    # ---- 2. usable comparison observations (raw flags, not derived state) --
    obs = {s: [] for s in stars}
    for f in usable:
        for star, srow in star_rows.get(f['frame_id'], {}).items():
            if srow['missing'] or srow['saturated'] or srow['flux'] is None or srow['flux'] <= 0:
                continue
            sigma_flux = math.sqrt(srow['aperture'] + srow['pixels'] * srow['sky'])
            obs[star].append((f['frame_id'], -2.5 * math.log10(srow['flux']),
                              MAG_SIGMA * sigma_flux / srow['flux']))

    # ---- 3. overlap graph components --------------------------------------
    parent = {}

    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for star, entries in obs.items():
        for fid, _, _ in entries:
            union(('star', star), ('frame', fid))

    # ---- 4. pairwise constancy chains --------------------------------------
    pair_breaks = {}
    consistent = {s: set() for s in stars}
    star_list = sorted(s for s in stars if obs[s])
    for i, s in enumerate(star_list):
        ds = {f: (m, sg) for f, m, sg in obs[s]}
        for t in star_list[i + 1:]:
            dt = {f: (m, sg) for f, m, sg in obs[t]}
            common = sorted(set(ds) & set(dt))
            if len(common) < 2:
                continue
            diffs = [(f, ds[f][0] - dt[f][0], math.hypot(ds[f][1], dt[f][1])) for f in common]
            base = median(d for _, d, _ in diffs)
            bad = [(f, d - base) for f, d, sd in diffs
                   if abs(d - base) > max(PAIR_ABS_TOL, PAIR_SIGMA_TOL * sd)]
            if bad:
                pair_breaks[(s, t)] = bad
            else:
                consistent[s].add(t)
                consistent[t].add(s)

    # ---- 5. stable set: largest mutually consistent clique per component ---
    stable = set()
    comp_members = {}
    for s in star_list:
        comp_members.setdefault(find(('star', s)), []).append(s)
    for members in comp_members.values():
        order = sorted(members, key=lambda s: (-len(consistent[s] & set(members)), s))
        clique = []
        for s in order:
            if all(s in consistent[c] for c in clique):
                clique.append(s)
        stable.update(s for s in clique if consistent[s])

    excluded = {}
    for s in star_list:
        if s in stable:
            continue
        evidence = []
        for (a, b), bad in sorted(pair_breaks.items()):
            if s in (a, b):
                other = b if s == a else a
                if other in stable:
                    evidence.extend((other, f, d) for f, d in bad)
        if evidence:
            evidence.sort(key=lambda item: (item[1], item[0]))
            chain = '；'.join(f'{s} 与 {o} 的重叠链在 {f} 突变 {abs(d):.3f}mag' for o, f, d in evidence)
            excluded[s] = ('variable', f'重叠链不一致：{chain}')
        else:
            excluded[s] = ('unverified', '重叠链不足，无法核验该参照整夜稳定性')
    for s in stars:
        if not obs[s]:
            excluded[s] = ('unverified', '无可用观测')

    # ---- 6. night-wide zero-point solution ---------------------------------
    stable_obs = {s: entries for s, entries in obs.items() if s in stable and entries}
    frames_with_obs = sorted({f for entries in stable_obs.values() for f, _, _ in entries})
    anchors = {s: (stars[s]['catalog_mag'], stars[s]['catalog_err'])
               for s in stable if stars[s]['catalog_mag'] is not None}
    zpoint, kappa = {}, None
    if stable_obs:
        idx = {}
        for s in sorted(stable_obs):
            idx[('M', s)] = len(idx)
        for fid in frames_with_obs:
            idx[('Z', fid)] = len(idx)
        if anchors:
            idx[('K',)] = len(idx)
        equations = []
        for s, entries in stable_obs.items():
            for fid, mag, sigma in entries:
                equations.append(({idx[('M', s)]: 1.0, idx[('Z', fid)]: 1.0}, mag, 1.0 / sigma ** 2))
        for s, (mag, err) in anchors.items():
            equations.append(({idx[('M', s)]: 1.0, idx[('K',)]: 1.0}, float(mag), 1.0 / float(err) ** 2))
        anchored_roots = {find(('star', s)) for s in anchors}
        comp_frames = {}
        for fid in frames_with_obs:
            comp_frames.setdefault(find(('frame', fid)), []).append(fid)
        for root, fids in comp_frames.items():
            if not anchors or root not in anchored_roots:
                # Unanchored component: soft gauge only breaks the singularity;
                # its frames stay unpublishable per the adjudication below.
                equations.append(({idx[('Z', f)]: 1.0 / len(fids) for f in fids}, 0.0, 1e-6))
        solution = _solve(equations, len(idx))
        if solution is not None:
            for fid in frames_with_obs:
                wsum = sum(1.0 / sigma ** 2 for entries in stable_obs.values()
                           for f, _, sigma in entries if f == fid)
                zpoint[fid] = (solution[idx[('Z', fid)]], math.sqrt(1.0 / wsum))
            if anchors:
                kappa = solution[idx[('K',)]]

    # ---- 7. target relative flux per frame ---------------------------------
    curve = []
    for f in usable:
        fid = f['frame_id']
        if fid not in zpoint:
            continue
        row = f['row']
        sigma_t = MAG_SIGMA * math.sqrt(row['target_aperture'] + row['pixels'] * row['target_sky']) / f['target_flux']
        z, sigma_z = zpoint[fid]
        curve.append({'frame_id': fid, 'T': -2.5 * math.log10(f['target_flux']) - z,
                      'sigma': math.hypot(sigma_t, sigma_z)})
    if len(curve) >= 2:
        base_T = median(c['T'] for c in curve[:2])
        for c in curve:
            c['rel'] = 10 ** (-0.4 * (c['T'] - base_T))
            c['rel_err'] = c['rel'] * c['sigma'] / MAG_SIGMA
    else:
        for c in curve:
            c['rel'] = c['rel_err'] = None
    curve_ids = [c['frame_id'] for c in curve]
    unanchored = [f['frame_id'] for f in usable if f['frame_id'] not in zpoint]
    anchored_stable = sorted(s for s in stable if stars[s]['catalog_mag'] is not None)
    frame_refs = {fid: sorted(s for s, entries in stable_obs.items()
                              if any(f == fid for f, _, _ in entries))
                  for fid in frames_with_obs}
    low = min((c['rel'] for c in curve[2:]), default=None) if len(curve) >= MIN_USABLE_FRAMES else None

    # ---- 8. catalogue adjudication ------------------------------------------
    if len(usable) < MIN_USABLE_FRAMES:
        cat_status = 'pending'
        cat_reason = f'可用帧不足 {MIN_USABLE_FRAMES} 张（当前 {len(usable)} 张），待补观测或标定恢复'
    elif unanchored:
        cat_status = 'indeterminate'
        cat_reason = (f"帧 {','.join(unanchored)} 缺少稳定参照，零点不可辨识；"
                      '相关结论仍取决于未核验的比较星假设')
    elif not anchored_stable:
        cat_status = 'indeterminate'
        cat_reason = '整夜重叠链内缺少星表锚定的稳定参照，正式测光结论仍取决于比较星假设'
    else:
        later_refs = sorted({s for c in curve[2:] for s in frame_refs.get(c['frame_id'], [])}
                            & set(anchored_stable))
        if len(later_refs) < MIN_INDEPENDENT_REFS:
            cat_status = 'indeterminate'
            ids = ','.join(c['frame_id'] for c in curve[2:])
            cat_reason = (f"检验帧 {ids} 的星表锚定参照不足 {MIN_INDEPENDENT_REFS} 颗"
                          f"（{','.join(later_refs) or '无'}），目标暗化与参照星增亮不可区分；"
                          '需第二独立锚定参照或补拍重叠帧')
        else:
            threshold = 1.0 - drop
            refs_txt = ','.join(anchored_stable)
            excl_txt = '；'.join(f'{s}（{excluded[s][1]}）' for s in sorted(excluded)) or '无'
            if low <= threshold:
                cat_status = 'candidate'
                cat_reason = (f'星表锚定参照 {refs_txt}；排除 {excl_txt}；'
                              f'最低相对通量 {low:.4f} ≤ 阈值 {threshold:.4f}，确认暗化')
            else:
                cat_status = 'steady'
                cat_reason = (f'星表锚定参照 {refs_txt} 整夜一致；排除 {excl_txt}；'
                              f'最低相对通量 {low:.4f} 高于阈值 {threshold:.4f}，未见暗化')

    # ---- 9. follow-up quick look through the designated star ----------------
    designated = session['comparison']
    fup = []
    for f in usable:
        srow = star_rows.get(f['frame_id'], {}).get(designated)
        if (not srow or srow['missing'] or srow['saturated']
                or srow['flux'] is None or srow['flux'] <= 0):
            continue
        row = f['row']
        rel_t = math.sqrt(row['target_aperture'] + row['pixels'] * row['target_sky']) / f['target_flux']
        rel_s = math.sqrt(srow['aperture'] + srow['pixels'] * srow['sky']) / srow['flux']
        ratio = f['target_flux'] / srow['flux']
        fup.append({'frame_id': f['frame_id'], 'ratio': ratio, 'err': ratio * math.hypot(rel_t, rel_s)})
    premise = f'以 {designated} 稳定为条件'
    fup_extra = {}
    if len(fup) < MIN_USABLE_FRAMES:
        fup_status = 'pending'
        fup_reason = f'{premise}：可用帧不足 {MIN_USABLE_FRAMES} 张（当前 {len(fup)} 张）'
    else:
        ratios = [x['ratio'] for x in fup]
        base_r = median(ratios[:2])
        low_r = min(ratios[2:])
        thr_r = base_r * (1 - drop)
        hit = low_r <= thr_r
        fup_status = 'candidate' if hit else 'steady'
        notes = []
        if designated in excluded:
            notes.append(f'整夜重叠解对 {designated} 稳定性存疑（{excluded[designated][1]}）')
        advice = None
        if hit:
            others = [s for s in anchored_stable if s != designated]
            advice = (f"建议补拍与 {','.join(others)} 重叠的确认帧" if others
                      else '建议补拍含独立参照的重叠确认帧')
            notes.append(advice)
        verdict = f'最低相对通量 {low_r:.4f} {"≤" if hit else "高于"} 阈值 {thr_r:.4f}'
        fup_reason = f'{premise}：{verdict}' + ('，疑似暗化' if hit else '') + ('；' + '；'.join(notes) if notes else '')
        fup_extra = {'baseline': base_r, 'threshold': thr_r, 'min_later': low_r, 'advice': advice}

    # ---- 10. persist derived per-frame and per-star state -------------------
    curve_by_id = {c['frame_id']: c for c in curve}
    for f in frames:
        fid = f['frame_id']
        c = curve_by_id.get(fid)
        if not f['usable']:
            m_status, fr_reason, rel, err, refs = 'excluded', f['reason'], None, None, None
        elif c is None:
            m_status, fr_reason, rel, err, refs = 'unanchored', '无稳定参照可锚定零点', None, None, None
        else:
            m_status, fr_reason = 'usable', None
            rel, err = c['rel'], c['rel_err']
            refs = json.dumps(frame_refs.get(fid, []), ensure_ascii=False)
        db.execute('UPDATE photometry_measurement SET ratio=?,status=? WHERE frame_id=?',
                   (rel, m_status, fid))
        old = flags[fid]
        if (old['relative_flux'], old['flux_err'], old['refs'], old['status'], old['reason']) != \
           (rel, err, refs, m_status, fr_reason):
            db.execute('UPDATE photometry_frame_result SET relative_flux=?,flux_err=?,refs=?,status=?,'
                       'reason=?,version=version+1 WHERE frame_id=?',
                       (rel, err, refs, m_status, fr_reason, fid))
    for fid, per in star_rows.items():
        f = frame_by_id[fid]
        for star, srow in per.items():
            if srow['missing']:
                st, rs = 'missing', 'comparison not measured on this frame'
            elif srow['saturated']:
                st, rs = 'saturated', 'comparison flagged saturated'
            elif srow['flux'] is None or srow['flux'] <= 0:
                st, rs = 'excluded', 'sky-subtracted comparison flux not positive'
            elif not f['usable']:
                st, rs = 'excluded', f"frame not usable: {f['reason']}"
            elif star in excluded:
                st, rs = 'excluded-' + excluded[star][0], excluded[star][1]
            else:
                st, rs = 'usable', None
            if (srow['status'], srow['reason']) != (st, rs):
                db.execute('UPDATE photometry_frame_star SET status=?,reason=? WHERE frame_id=? AND star=?',
                           (st, rs, fid, star))
    for star in sorted(stars):
        if star in stable:
            st = 'stable'
            rs = '整夜重叠链一致' + ('；星表锚定' if stars[star]['catalog_mag'] is not None else '；无星表锚点')
        else:
            st, rs = excluded[star]
        old = stars[star]
        if (old['status'], old['reason']) != (st, rs):
            db.execute('UPDATE photometry_comparison SET status=?,reason=?,version=version+1 '
                       'WHERE session_id=? AND star=?', (st, rs, sid, star))

    # ---- 11. session headline and the two receiving products -----------------
    if (session['status'],) != (cat_status,):
        db.execute('UPDATE photometry_session SET status=?,version=version+1 WHERE id=?', (cat_status, sid))
        old_ev = db.execute('SELECT id FROM photometry_event WHERE session_id=? ORDER BY id DESC LIMIT 1',
                            (sid,)).fetchone()
        db.execute('INSERT INTO photometry_event(session_id,status,reason,previous) VALUES(?,?,?,?)',
                   (sid, cat_status, cat_reason, old_ev['id'] if old_ev else None))
        decide(db, 'photometry', sid, cat_status, cat_reason, [r['frame_id'] for r in rows])
    details = {
        'followup': {
            'designated': designated,
            'premise': f'{designated} 稳定',
            'references': [designated],
            'frames': [x['frame_id'] for x in fup],
            'ratios': [x['ratio'] for x in fup],
            'ratio_err': [x['err'] for x in fup],
            'ensemble_note': excluded[designated][1] if designated in excluded else None,
            **fup_extra,
        },
        'catalogue': {
            'references': sorted(stable),
            'anchors': {s: {'mag': stars[s]['catalog_mag'], 'err': stars[s]['catalog_err']}
                        for s in anchored_stable},
            'excluded': {s: {'status': st, 'reason': rs} for s, (st, rs) in sorted(excluded.items())},
            'frames': curve_ids,
            'relative_flux': [c['rel'] for c in curve],
            'flux_err': [c['rel_err'] for c in curve],
            'min_later': low,
            'drop_fraction': drop,
            'kappa': kappa,
        },
    }
    for purpose, p_status, p_reason in (('followup', fup_status, fup_reason),
                                        ('catalogue', cat_status, cat_reason)):
        detail = dict(details[purpose])
        detail['source_id'] = intake['source_id'] if intake else None
        detail['source_digest'] = intake['digest'] if intake else None
        detail_json = json.dumps(detail, ensure_ascii=False, sort_keys=True)
        old = db.execute('SELECT status,reason,version FROM photometry_product '
                         'WHERE session_id=? AND purpose=?', (sid, purpose)).fetchone()
        old_detail = db.execute('SELECT detail FROM photometry_product_detail '
                                'WHERE session_id=? AND purpose=?', (sid, purpose)).fetchone()
        if (old['status'], old['reason']) == (p_status, p_reason) \
                and old_detail and old_detail['detail'] == detail_json:
            continue
        version = old['version'] + 1
        db.execute('UPDATE photometry_product SET status=?,reason=?,version=? '
                   'WHERE session_id=? AND purpose=?', (p_status, p_reason, version, sid, purpose))
        previous = db.execute('SELECT id FROM photometry_product_event '
                              'WHERE session_id=? AND purpose=? ORDER BY id DESC LIMIT 1',
                              (sid, purpose)).fetchone()
        db.execute('INSERT INTO photometry_product_event(session_id,purpose,status,reason,previous) '
                   'VALUES(?,?,?,?,?)', (sid, purpose, p_status, p_reason,
                                         previous['id'] if previous else None))
        db.execute('INSERT INTO photometry_product_detail(session_id,purpose,version,detail) VALUES(?,?,?,?) '
                   'ON CONFLICT(session_id,purpose) DO UPDATE SET version=excluded.version,detail=excluded.detail',
                   (sid, purpose, version, detail_json))
    return {
        'session': sid,
        'status': cat_status,
        'usable_frames': len(curve),
        'ratios': [c['rel'] for c in curve],
        'followup': {'status': fup_status, 'reason': fup_reason},
        'catalogue': {'status': cat_status, 'reason': cat_reason},
        'comparisons': {s: ('stable' if s in stable else excluded[s][0]) for s in sorted(stars)},
    }
