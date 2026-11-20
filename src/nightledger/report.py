"""Recomputable observation evidence and archive history."""
TABLES=['slot_allocation','instrument_setup','downtime','calibration_frame','calibration_set','science_frame','archive_case','archive_event','photometry_session','photometry_measurement','photometry_event','photometry_product','photometry_product_event','photometry_intake','photometry_comparison','photometry_frame_star','photometry_frame_result','photometry_product_detail','decision']
ORDER={'photometry_comparison':'session_id,star','photometry_frame_star':'frame_id,star','photometry_product_detail':'session_id,purpose'}
def build(db):
    return {n:[dict(r) for r in db.execute(f'SELECT * FROM {n} ORDER BY {ORDER.get(n,"1")}')] for n in TABLES}
