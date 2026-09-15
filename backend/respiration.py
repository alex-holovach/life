"""Experimental respiratory modulation of pulse intervals, not direct respiration.

No fabricated beat times across missing records or rejected/empty interval groups.
Thresholds are engineering screens, not validated clinical confidence bounds.
"""
import json
import math
import statistics

ALGORITHM = 'r24_pulse_respiration_v1'
MIN_SECONDS = 90


def spectrum(times, values):
    """Least-squares sinusoidal power on uneven beat times after linear detrending."""
    mean_t, mean_v = statistics.mean(times), statistics.mean(values)
    tt = [t-mean_t for t in times]
    slope = sum(t*(v-mean_v) for t, v in zip(tt, values))/sum(t*t for t in tt)
    vv = [v-mean_v-slope*t for t, v in zip(tt, values)]
    energy = sum(v*v for v in vv)
    if energy < len(vv)*9:  # <3 ms RMS modulation cannot support this estimate.
        return None
    powers = []
    for step in range(241):
        hz = .10 + step/600  # 6..30 breaths/min, 0.1 bpm grid, not claimed accuracy.
        cc = [math.cos(2*math.pi*hz*t) for t in tt]
        ss = [math.sin(2*math.pi*hz*t) for t in tt]
        mc, ms = statistics.mean(cc), statistics.mean(ss)
        cc = [c-mc for c in cc]; ss = [s-ms for s in ss]
        c2, s2, cs = sum(c*c for c in cc), sum(s*s for s in ss), sum(c*s for c,s in zip(cc,ss))
        cv, sv = sum(c*v for c,v in zip(cc,vv)), sum(s*v for s,v in zip(ss,vv))
        determinant = c2*s2-cs*cs
        power = (s2*cv*cv+c2*sv*sv-2*cs*cv*sv)/determinant/energy if determinant>1e-8 else 0
        powers.append((hz, max(0, min(1, power))))
    hz, peak = max(powers, key=lambda x:x[1])
    other = max(p for f,p in powers if abs(f-hz)>.04)
    return dict(hz=hz, power=peak, separation=peak/max(other, 1e-9), detrended=vv)


def autocorrelation_rate(times, values):
    # Resample only within this already-contiguous beat segment, never across a
    # rejected beat or history gap. Four Hz interpolation is for a cross-check.
    points=[]; i=0; t=times[0]
    while t <= times[-1]:
        while i+1<len(times)-1 and times[i+1]<t: i+=1
        fraction=(t-times[i])/(times[i+1]-times[i])
        points.append(values[i]+fraction*(values[i+1]-values[i])); t+=.25
    correlations=[]
    for lag in range(7, 42):
        a,b=points[:-lag],points[lag:]
        ma,mb=statistics.mean(a),statistics.mean(b)
        a=[v-ma for v in a];b=[v-mb for v in b]
        denominator=math.sqrt(sum(v*v for v in a)*sum(v*v for v in b))
        correlations.append((lag, sum(x*y for x,y in zip(a,b))/denominator if denominator else 0))
    peaks=[correlations[i] for i in range(1,len(correlations)-1)
           if correlations[i][1]>correlations[i-1][1] and correlations[i][1]>=correlations[i+1][1]]
    if not peaks:return None
    strongest=max(p for _,p in peaks)
    # First strong peak prefers the fundamental period over repeated cycles.
    lag,power=next((lag,p) for lag,p in peaks if p>=max(.5,strongest*.9)) if strongest>=.5 else (0,0)
    return dict(rate=240/lag, power=power) if lag else None


def estimate(segment, end):
    duration=segment[-1]['at']-segment[0]['at']+100/104
    result=dict(state='insufficient_data', breaths_per_minute=None, algorithm=ALGORITHM,
                start=segment[0]['at'], end=segment[-1]['at'], analyzed_until=end,
                duration_seconds=duration, coverage=0.0, spectral_power=0.0,
                quality='insufficient_signal', validation='not_reference_validated', reasons=[])
    if duration<MIN_SECONDS:
        result['reasons']=['short_contiguous_segment'];return result
    words=[w for r in segment for w in r['words']]
    result['coverage']=sum(words)/1000/duration
    if not .98<=result['coverage']<=1.02:result['reasons'].append('incomplete_pulse_coverage')
    for i,value in enumerate(words):
        median=statistics.median(words[max(0,i-5):i+6])
        if abs(value-median)>.20*median:
            result['reasons'].append('pulse_artifacts');break
    rates=[statistics.mean(r['hr'] for r in segment[i:i+30]) for i in range(0,len(segment)-29,30)]
    if rates and max(rates)-min(rates)>10:result['reasons'].append('changing_heart_rate')
    if result['reasons']:return result
    times=[];clock=0
    for value in words:clock+=value/1000;times.append(clock)
    full=spectrum(times,words)
    if not full:
        result['reasons']=['weak_respiratory_modulation'];return result
    result['spectral_power']=full['power']
    if full['power']<.5 or full['separation']<1.8:result['reasons'].append('unclear_spectral_peak')
    if full['hz']<=.105 or full['hz']>=.495 or full['hz']>.45/(statistics.median(words)/1000):
        result['reasons'].append('frequency_outside_supported_range')
    ac=autocorrelation_rate(times,full['detrended'])
    if not ac or abs(ac['rate']-full['hz']*60)>1.5:result['reasons'].append('period_checks_disagree')
    halves=[]
    for indexes in ([i for i,t in enumerate(times) if t<clock/2], [i for i,t in enumerate(times) if t>=clock/2]):
        half=spectrum([times[i] for i in indexes],[words[i] for i in indexes])
        if half:halves.append(half)
    if len(halves)!=2 or any(h['power']<.35 or abs(h['hz']-full['hz'])*60>2 for h in halves):
        result['reasons'].append('unstable_respiratory_modulation')
    if not result['reasons']:
        result.update(state='ready', breaths_per_minute=round(full['hz']*60,1), quality='signal_checks_passed')
    return result


def calculate(rows, end):
    segments=[];current=[];previous=None
    for row in sorted((dict(r) for r in rows if end-300<=r['at']<end),key=lambda r:r['at']):
        row['words']=json.loads(row['words']) if isinstance(row['words'],str) else row['words']
        contiguous=previous is not None and .90<=row['at']-previous['at']<=1.05 and ((row['seq']-previous['seq'])&0xffffffff)==1
        valid=not row.get('conflict') and row['hr']>0 and row['words'] and all(333<w<2000 and w!=500 for w in row['words'])
        if not contiguous or not valid:
            if current:segments.append(current)
            current=[]
        if valid:current.append(row)
        previous=row
    if current:segments.append(current)
    if not segments:
        return dict(state='insufficient_data',breaths_per_minute=None,algorithm=ALGORITHM,
                    start=None,end=end,analyzed_until=end,duration_seconds=0,coverage=0,
                    spectral_power=0,quality='insufficient_signal',validation='not_reference_validated',reasons=['no_contiguous_intervals'])
    candidates=[s for s in segments if s[-1]['at']-s[0]['at']+100/104>=MIN_SECONDS]
    if not candidates:candidates=[max(segments,key=len)]
    results=[estimate(s,end) for s in reversed(candidates)]
    return next((r for r in results if r['state']=='ready'),results[0])
