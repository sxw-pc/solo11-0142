"""Recomputable observation evidence and archive history."""
def build(db):
    names=['slot_allocation','instrument_setup','downtime','calibration_frame','calibration_set','science_frame','archive_case','archive_event','photometry_session','photometry_measurement','photometry_event','photometry_product','photometry_product_event','decision']
    return {n:[dict(r) for r in db.execute(f'SELECT * FROM {n} ORDER BY 1')] for n in names}
