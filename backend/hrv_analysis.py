"""Scheduled, incremental archive analysis, separate from ingestion and remote write."""
import fcntl
import gzip
import json
import os
import sys
import time
from types import SimpleNamespace
import storage
import hrv
import vitals
import wellness
import analysis_quality


def firmware_signature(db):
    # Reconnects report the same version repeatedly. Only version transitions
    # (including the first evidence time) change historical window eligibility.
    # Keep unknown transitions and late evidence; never infer firmware backwards.
    timeline=[];previous={}
    for device,at,version in db.execute('SELECT device,observed_at,firmware FROM firmware_observations ORDER BY device,observed_at'):
        if previous.get(device)!=version:
            timeline.append((device,at,version));previous[device]=version
    return json.dumps(timeline)


def next_delay(captured,now):
    if captured>=2000:return 0
    with storage.connect() as db:
        pending=any(db.execute(f'SELECT 1 FROM {table} WHERE end<=? LIMIT 1',(now-10,)).fetchone()
                    for table in ('hrv_dirty','respiration_dirty'))
    return 1 if pending else 30


def run_once(now=None):
    now=time.time() if now is None else now
    with storage.connect() as db:
        cursor=db.execute("SELECT value FROM settings WHERE key='physiology_archive_cursor_v1'").fetchone()
        rows=db.execute("SELECT rowid,archive,ordinal FROM captures WHERE source='history' AND rowid>? ORDER BY rowid LIMIT 2000",
                         (int(cursor[0]) if cursor else 0,)).fetchall()
    # Decompress outside the write transaction. Advance cursor atomically with all indexes.
    decoded=[];cache={}
    for row in rows:
        if row['archive'] not in cache:
            with gzip.open(storage.directory()/row['archive'],'rt') as f: cache[row['archive']]=json.load(f)['captures']
        capture=SimpleNamespace(**cache[row['archive']][row['ordinal']])
        decoded.append((hrv.record(capture),vitals.oxygen_record(capture)))
    with storage.connect() as db:
        for row,oxygen in decoded:
            if row:hrv.index(db,row)
            vitals.index(db,oxygen,row)
        if rows:
            db.execute("INSERT OR REPLACE INTO settings VALUES('physiology_archive_cursor_v1',?)",(str(rows[-1]['rowid']),))
        # A firmware response can arrive after its historical samples. Revisit windows
        # on evidence changes instead of permanently freezing an unsupported result.
        signature=firmware_signature(db)
        previous=db.execute("SELECT value FROM settings WHERE key='hrv_firmware_signature'").fetchone()
        if not previous or previous[0]!=signature:
            db.execute('INSERT OR IGNORE INTO hrv_dirty SELECT device,end FROM hrv_windows WHERE end>?',(now-30*86400,))
            vitals.invalidate_firmware(db,now)
            db.execute("INSERT OR REPLACE INTO settings VALUES('hrv_firmware_signature',?)",(signature,))
        dirty=db.execute('SELECT device,end FROM hrv_dirty WHERE end<=? ORDER BY end LIMIT 500',(now-10,)).fetchall()
        for row in dirty:
            hrv.compute_window(db,row['device'],row['end'])
            db.execute('DELETE FROM hrv_dirty WHERE device=? AND end=?',tuple(row))
        db.execute("INSERT OR REPLACE INTO settings VALUES('hrv_heartbeat',?)",(str(now),))
    analyzed=vitals.analyze(storage.connect,now)
    wellness.analyze(storage.connect,now)
    with storage.connect() as db:analysis_quality.save(db,now)
    return len(rows),len(dirty)+analyzed


def main():
    os.umask(0o077);storage.initialize()
    with open(storage.directory()/'hrv.lock','w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        while True:
            count,analyzed=run_once()
            if '--once' in sys.argv and count==0 and analyzed==0:return
            if '--once' not in sys.argv:time.sleep(next_delay(count,time.time()))


if __name__=='__main__':
    if '--health' in sys.argv:
        with storage.connect() as db:row=db.execute("SELECT value FROM settings WHERE key='hrv_heartbeat'").fetchone()
        sys.exit(0 if row and 0<=time.time()-float(row[0])<180 else 1)
    else:main()
