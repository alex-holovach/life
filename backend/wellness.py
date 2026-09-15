"""Recorded cardio load and self-reported sleep. Independent Life algorithms.

See docs/SCORING.md for methods, units, coverage, and limits. Never substitute
an assumed age, HR maximum, sleep target, or an unobserved interval.
"""
from datetime import datetime, timedelta
import json
import math
import statistics
from zoneinfo import ZoneInfo
import temperature
import hrv
from bisect import bisect_right

ALGORITHM = 'life_edwards_hrmax_v1'
SLEEP_ALGORITHM = 'life_reported_duration_v1'
SCHEMA = """
CREATE TABLE IF NOT EXISTS sleep_sessions(
 id TEXT PRIMARY KEY, revision INTEGER NOT NULL, start REAL NOT NULL,
 end REAL, awake_seconds REAL NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS wellness_edits(
 id INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS wellness_results(
 key TEXT PRIMARY KEY, result TEXT NOT NULL, computed_at REAL NOT NULL);
"""


class EditConflict(Exception): pass


def profile(db):
    row=db.execute("SELECT value FROM settings WHERE key='wellness_profile'").fetchone()
    return json.loads(row[0]) if row else dict(timezone=None,max_hr=None,sleep_goal_minutes=None)


def save_profile(db,value,now):
    encoded=json.dumps(value,sort_keys=True)
    db.execute("INSERT OR REPLACE INTO settings VALUES('wellness_profile',?)",(encoded,))
    db.execute('INSERT INTO wellness_edits(kind,payload,at) VALUES(?,?,?)',('profile',encoded,now))


def save_sleep(db,identifier,value,now):
    db.execute('BEGIN IMMEDIATE')
    previous=db.execute('SELECT * FROM sleep_sessions WHERE id=?',(identifier,)).fetchone()
    if previous:
        old={k:previous[k] for k in ('revision','start','end','awake_seconds')}
        if value==old:return dict(previous)
        if value['revision']<=previous['revision']:raise EditConflict('A newer sleep edit is already saved')
    overlap=db.execute('''SELECT id FROM sleep_sessions WHERE id!=? AND start<? AND COALESCE(end,1e20)>? LIMIT 1''',
                       (identifier,value['end'] or 1e20,value['start'])).fetchone()
    if overlap:raise EditConflict('Sleep sessions cannot overlap')
    db.execute('INSERT OR REPLACE INTO sleep_sessions VALUES(?,?,?,?,?,?)',
               (identifier,value['revision'],value['start'],value['end'],value['awake_seconds'],now))
    db.execute('INSERT INTO wellness_edits(kind,payload,at) VALUES(?,?,?)',
               ('sleep',json.dumps(dict(id=identifier,**value),sort_keys=True),now))
    return dict(db.execute('SELECT * FROM sleep_sessions WHERE id=?',(identifier,)).fetchone())


def day_bounds(now,timezone):
    day=datetime.fromtimestamp(now,ZoneInfo(timezone)).replace(hour=0,minute=0,second=0,microsecond=0)
    return day.timestamp(),(day+timedelta(days=1)).timestamp(),day.date().isoformat()


def intervals(rows,start,end):
    """Only consecutive history records contribute time; never hold HR across gaps."""
    result=[]
    for a,b in zip(rows,rows[1:]):
        dt=b['at']-a['at']
        if a['conflict'] or b['conflict'] or not 0<a['hr']<=255:continue
        if not .90<=dt<=1.05 or ((b['seq']-a['seq'])&0xffffffff)!=1:continue
        left,right=max(start,a['at']),min(end,b['at'])
        if right>left:result.append((left,right,a['hr']))
    return result


def history_rows(db,device,start,end):
    evidence=db.execute('SELECT observed_at,firmware FROM firmware_observations WHERE device=? ORDER BY observed_at',(device,)).fetchall()
    times=[r[0] for r in evidence]
    rows=db.execute('SELECT * FROM hrv_records WHERE device=? AND at BETWEEN ? AND ? ORDER BY at',(device,start-1.05,end+1.05)).fetchall()
    valid=[]
    for row in rows:
        index=bisect_right(times,row['at'])-1
        if index>=0 and evidence[index][1]==temperature.FIRMWARE:valid.append(row)
    return valid


def strain(rows,start,end,now,max_hr):
    observed=intervals(rows,start,min(end,now))
    seconds=sum(b-a for a,b,_ in observed)
    result=dict(state='set_max_hr' if max_hr is None else 'insufficient_history',score=None,
                load=None,algorithm=ALGORITHM,start=start,end=end,observed_until=observed[-1][1] if observed else None,
                recorded_seconds=seconds,coverage=seconds/max(1,min(now,end)-start),zone_minutes=[0.0]*5)
    if max_hr is None or seconds<600:return result
    for a,b,hr in observed:
        ratio=hr/max_hr
        zone=sum(ratio>=threshold for threshold in (.5,.6,.7,.8,.9))
        if zone:result['zone_minutes'][zone-1]+=(b-a)/60
    load=sum((i+1)*minutes for i,minutes in enumerate(result['zone_minutes']))
    # Life-specific display scale. Edwards defines load, not a proprietary score.
    ceiling=5*(end-start)/60
    result.update(state='ready' if result['coverage']>=.9 else 'partial',load=load,
                  score=21*math.log1p(load)/math.log1p(ceiling))
    return result


def sleep_result(db,session,device,goal):
    s=dict(session);start,end=s['start'],s['end']
    result=dict(id=s['id'],state='in_progress' if end is None else 'logged',start=start,end=end,
                duration_seconds=None,score=None,goal_minutes=goal,source='self_reported',algorithm=SLEEP_ALGORITHM,
                heart_rate_bpm=None,hr_coverage=0.0,hrv_ms=None,hrv_coverage=0.0,
                temperature_celsius=None,temperature_coverage=0.0,temperature_delta_celsius=None,baseline_nights=0)
    if end is None:return result
    duration=end-start-s['awake_seconds'];result['duration_seconds']=duration
    if goal is not None:result.update(state='ready',score=min(100,100*duration/(goal*60)))
    rows=history_rows(db,device,start,end)
    observed=intervals(rows,start,end);result['hr_coverage']=sum(b-a for a,b,_ in observed)/(end-start)
    if result['hr_coverage']>=.8 and end-start>=1800:
        result['heart_rate_bpm']=statistics.median(hr for _,_,hr in observed)
    # Five-minute windows overlap: union their spans rather than counting each as five new minutes.
    windows=[json.loads(r[0]) for r in db.execute('SELECT result FROM hrv_windows WHERE device=? AND algorithm=? AND end BETWEEN ? AND ? ORDER BY end',(device,hrv.ALGORITHM,start+300,end))]
    good=[r for r in windows if r['state']=='ready'];covered=0;through=start
    for w in good:
        covered+=max(0,w['end']-max(through,w['start']));through=max(through,w['end'])
    result['hrv_coverage']=covered/(end-start)
    if len(good)>=3 and result['hrv_coverage']>=.5:result['hrv_ms']=statistics.median(w['rmssd_ms'] for w in good)
    series=temperature.metric(temperature.CELSIUS_METRIC,decoder=temperature.DECODER)
    points=db.execute('SELECT timestamp,value FROM samples WHERE series=? AND timestamp BETWEEN ? AND ? ORDER BY timestamp',
                      (series,int(start*1000),int(end*1000))).fetchall()
    duration_covered=sum((b[0]-a[0])/1000 for a,b in zip(points,points[1:]) if 0<b[0]-a[0]<=1050)
    result['temperature_coverage']=duration_covered/(end-start)
    if result['temperature_coverage']>=.8 and end-start>=1800:
        result['temperature_celsius']=statistics.median(p[1] for p in points)
    return result


def analyze(connect,now):
    with connect() as db:
        db.execute('BEGIN')
        p=profile(db);device=db.execute("SELECT value FROM settings WHERE key='device'").fetchone()
        device=device[0] if device else ''
        start,end,day=day_bounds(now,p['timezone'] or 'UTC')
        rows=history_rows(db,device,start,now)
        result=strain(rows,start,end,now,p['max_hr']);result['day']=day
        if not p['timezone']:result.update(state='set_timezone',score=None)
        results=[('strain:'+day,result)]
        sessions=db.execute('SELECT * FROM sleep_sessions WHERE start>? ORDER BY start',(now-35*86400,)).fetchall()
        sleep=[sleep_result(db,s,device,p['sleep_goal_minutes']) for s in sessions]
        for i,s in enumerate(sleep):
            # At most one prior main sleep per date contributes, avoiding nap weighting.
            prior={}
            for previous in sleep[:i]:
                if p['timezone'] and previous['temperature_celsius'] is not None and previous['duration_seconds']>=3*3600 and previous['end']<=s['start'] and previous['start']>=s['start']-28*86400:
                    date=datetime.fromtimestamp(previous['end'],ZoneInfo(p['timezone'] or 'UTC')).date().isoformat()
                    if date not in prior or previous['duration_seconds']>prior[date]['duration_seconds']:prior[date]=previous
            s['baseline_nights']=len(prior)
            if s['temperature_celsius'] is not None and len(prior)>=7:
                s['temperature_delta_celsius']=s['temperature_celsius']-statistics.median(r['temperature_celsius'] for r in prior.values())
            results.append(('sleep:'+s['id'],s))
        # Profile/session writes invalidate cached responses via their edit watermark.
        revision=db.execute('SELECT COALESCE(max(id),0) FROM wellness_edits').fetchone()[0]
    with connect() as db:
        for key,result in results:
            db.execute('INSERT OR REPLACE INTO wellness_results VALUES(?,?,?)',(key,json.dumps(result,allow_nan=False),now))
        db.execute("INSERT OR REPLACE INTO settings VALUES('wellness_revision',?)",(str(revision),))
        db.execute("INSERT OR REPLACE INTO settings VALUES('wellness_heartbeat',?)",(str(now),))


def status(db,now):
    p=profile(db);_,_,day=day_bounds(now,p['timezone'] or 'UTC')
    sessions=[dict(r) for r in db.execute('SELECT * FROM sleep_sessions ORDER BY start DESC LIMIT 100')]
    def cached(key):
        row=db.execute('SELECT result FROM wellness_results WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row else None
    load=cached('strain:'+day)
    completed=next((s for s in sessions if s['end'] is not None),None)
    sleep=cached('sleep:'+completed['id']) if completed else None
    heartbeat=db.execute("SELECT value FROM settings WHERE key='wellness_heartbeat'").fetchone()
    revision=db.execute("SELECT value FROM settings WHERE key='wellness_revision'").fetchone()
    current_revision=db.execute('SELECT COALESCE(max(id),0) FROM wellness_edits').fetchone()[0]
    valid=heartbeat and 0<=now-float(heartbeat[0])<180 and revision and int(revision[0])==current_revision
    state='ready' if valid else 'analysis_pending'
    return dict(state=state,profile=p,sessions=sessions,strain=load if valid else None,sleep=sleep if valid else None,
                computed_at=float(heartbeat[0]) if heartbeat else None)


def metrics(db,now):
    s=status(db,now);load=s['strain'];sleep=s['sleep'];values={}
    values['life_strain_ready']=int(bool(load and load['score'] is not None))
    values['life_sleep_score_ready']=int(bool(sleep and sleep['score'] is not None and 0<=now-sleep['end']<36*3600))
    if load and load['state']!='set_timezone':
        values.update(life_strain_coverage_ratio=load['coverage'],life_strain_recorded_seconds=load['recorded_seconds'])
        if load['score'] is not None:values.update(life_strain_score=load['score'],life_cardio_load=load['load'])
    if sleep and sleep['end'] is not None and 0<=now-sleep['end']<36*3600:
        values.update(life_sleep_duration_seconds=sleep['duration_seconds'],life_sleep_end_timestamp_seconds=sleep['end'],
                      life_sleep_hr_coverage_ratio=sleep['hr_coverage'],life_sleep_hrv_coverage_ratio=sleep['hrv_coverage'])
        for key,name in [('score','life_sleep_score'),('heart_rate_bpm','life_sleep_heart_rate_bpm'),('hrv_ms','life_sleep_hrv_ms'),
                         ('temperature_celsius','life_sleep_temperature_celsius'),('temperature_delta_celsius','life_sleep_temperature_delta_celsius')]:
            if sleep[key] is not None:values[name]=sleep[key]
    return values
