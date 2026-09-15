"""Index firmware evidence and decoded skin temperatures from the immutable archive.

Run inside the API container: python temperature_reprocess.py. Existing numeric samples
are immutable; a changed decoder must use a new version. Safe to rerun.
"""
import gzip
import json
import os
import time
from types import SimpleNamespace
import storage
import temperature


def reprocess():
    storage.initialize()
    with storage.connect() as db:
        evidence = db.execute("SELECT archive,ordinal FROM captures WHERE source='wire_frame' ORDER BY archive,ordinal").fetchall()
    archive, batch = None, None
    with storage.connect() as db:
        for row in evidence:
            if row['archive'] != archive:
                archive = row['archive']
                with gzip.open(storage.directory()/archive, 'rt') as f:
                    batch = json.load(f)['captures']
            temperature.record_firmware(SimpleNamespace(**batch[row['ordinal']]), db)
    with storage.connect() as db:
        rows = db.execute("SELECT archive,ordinal FROM captures WHERE source='history' ORDER BY archive,ordinal").fetchall()
    archive, batch, inserted, outside_window = None, None, 0, 0
    cutoff = int((time.time()-int(os.environ.get('BACKLOG_DAYS','30'))*86400)*1000)
    for row in rows:
        if row['archive'] != archive:
            archive = row['archive']
            with gzip.open(storage.directory()/archive, 'rt') as f:
                batch = json.load(f)['captures']
        capture = SimpleNamespace(**batch[row['ordinal']])
        observation = temperature.observation(capture)
        if observation and int(observation[0]*1000) < cutoff:
            outside_window += 1
            continue
        with storage.connect() as db:
            result = temperature.samples(capture, db)
            for series, at, value in result:
                previous = db.execute('SELECT value FROM samples WHERE series=? AND timestamp=?', (series,at)).fetchone()
                if previous:
                    if previous[0] != value:
                        raise ValueError('Temperature conversion changed. Use a new decoder version; existing values are immutable.')
                    continue
                state = 'historical_import' if at < cutoff else 'pending'
                db.execute('INSERT INTO samples(series,timestamp,value,state) VALUES(?,?,?,?)', (series,at,value,state))
                inserted += 1
    return dict(history_records=len(rows), inserted_samples=inserted, outside_window_records=outside_window)


if __name__ == '__main__':
    print(json.dumps(reprocess()))
