"""Build Life's dashboard from measured and explicitly derived time series."""
import json
from pathlib import Path
root = Path(__file__).resolve().parents[1] / "grafana"
ds = {"type": "prometheus", "uid": "life-prometheus"}
panels = []
def panel(title, query, kind, x, y, w, h, unit=None, description=""):
    item = {"id":len(panels)+1,"title":title,"description":description,"type":kind,"datasource":ds,
            "gridPos":{"x":x,"y":y,"w":w,"h":h},
            "targets":[{"refId":"A","datasource":ds,"expr":query,"legendFormat":"{{source}}","range":kind=="timeseries","instant":kind=="stat"}],
            "fieldConfig":{"defaults":{"color":{"mode":"palette-classic"},"noValue":"No data"},"overrides":[]},
            "options": {"reduceOptions":{"calcs":["lastNotNull"],"fields":"","values":False},"colorMode":"value","graphMode":"none"} if kind=="stat" else {"legend":{"displayMode":"list","placement":"bottom"},"tooltip":{"mode":"multi"}}}
    if kind=="timeseries": item["fieldConfig"]["defaults"]["custom"]={"drawStyle":"line","lineInterpolation":"linear","lineWidth":2,"fillOpacity":8,"spanNulls":False,"showPoints":"auto"}
    if unit: item["fieldConfig"]["defaults"]["unit"]=unit
    panels.append(item)

panel("Battery · verified", 'last_over_time(whoop_battery_percent[1h]) and on(device) (time() - whoop_battery_last_observed_timestamp_seconds < 600)',"stat",0,0,6,4,"percent","Only cross-checked readings less than ten minutes old are displayed. No data means unknown, not zero battery.")
panel("Last strap traffic", 'time() - whoop_last_received_timestamp_seconds',"stat",6,0,6,4,"s","Age of phone-observed strap traffic. Delayed uploads do not make old readings fresh.")
panel("Backend queue", 'whoop_export_pending_samples',"stat",12,0,6,4,"short","Backend outgoing samples. The iPhone's separate queue appears in the Life app.")
panel("Export review", 'whoop_export_blocked_samples + whoop_export_conflicts',"stat",18,0,6,4,"short","Rejected or older-than-window samples and timestamp conflicts stay archived; they are never retimestamped.")
panel("Heart rate", 'whoop_heart_rate_bpm',"timeseries",0,4,16,8,"bpm","Original observation timestamps. Live points use phone receipt time. Graphs are visualization, not inputs for HRV calculations.")
panel("Battery history", 'whoop_battery_percent',"timeseries",16,4,8,8,"percent","Only matching standard/custom readings are exported. Gaps are kept visible.")
panel("Export queue", 'whoop_export_pending_samples',"timeseries",0,12,12,7,"short")
panel("Last battery cross-check", 'time() - whoop_battery_last_observed_timestamp_seconds',"stat",12,12,6,4,"s")
panel("Backend records", 'whoop_archived_records',"stat",18,12,6,4,"short")
panels.append({"id":len(panels)+1,"title":"Collection and validation","type":"text","gridPos":{"x":12,"y":16,"w":12,"h":3},"options":{"mode":"markdown","content":"**Life · WHOOP**\n\nStored history sync is enabled for validated transfer layouts. HRV uses firmware-decoded pulse intervals and conservative five-minute quality checks. Physiological accuracy has not been compared with ECG. Sleep duration is self-reported; strain is an independent Life cardio-load score. Prometheus alert rules are installed; external notifications are not configured."}})
panel("Skin temperature", 'last_over_time(whoop_skin_temperature_celsius[10m]) and on(device,decoder) (last_over_time(whoop_skin_temperature_valid[10m]) == 1) and on(device) (whoop_skin_temperature_decoder_supported == 1)', "stat",0,19,6,8,"celsius","WHOOP 4 skin sensor, decoded from Harvard 41.17.4.0 firmware. Signed word at frame offset 76 / 10 °C. Sensor error values and readings older than ten minutes are hidden. No user calibration.")
panels[-1]["fieldConfig"]["defaults"].update({"decimals":1,"noValue":"Awaiting sensor reading"})
panel("Skin temperature history", 'whoop_skin_temperature_celsius and on(device,decoder) (whoop_skin_temperature_valid == 1) and (time() - timestamp(whoop_skin_temperature_celsius) < 5)', "timeseries",6,19,18,8,"celsius","Original history-record time. Gaps mean missing or invalid sensor readings. Firmware decoder versions remain separate.")
panel("Temperature sensor diagnostics", 'whoop_temperature_candidate_raw', "timeseries",0,27,24,7,"short","Raw words from v24 history, shown without unit conversion. Offset 76 is now identified as skin temperature in tenths °C for the supported firmware; other channels retain raw labels.")
for item in panels:
    if item["type"] == "timeseries":
        name = {"Heart rate": "Heart rate", "Battery history": "Battery", "Export queue": "Queued samples", "Skin temperature history":"Skin temperature", "Temperature sensor diagnostics":"Field {{field}}"}[item["title"]]
        item["targets"][0]["legendFormat"] = name
        color = {"Heart rate": "#FF8F78", "Battery history": "#D3F5AE", "Export queue": "#BFB8F5", "Skin temperature history":"#F7C57A", "Temperature sensor diagnostics":"#F7C57A"}[item["title"]]
        item["fieldConfig"]["defaults"]["color"] = {"mode": "fixed", "fixedColor": color}
for item in panels:
    if item["title"] == "Temperature sensor diagnostics":
        item["fieldConfig"]["defaults"]["color"] = {"mode":"palette-classic"}
hrv_query = 'whoop_hrv_rmssd_ms and on(device) (whoop_hrv_ready == 1) and on(device) (time() - whoop_hrv_window_end_timestamp_seconds <= 900)'
hrv_description = 'PPG-derived RMSSD over five minutes, rounded to whole milliseconds for display. At least 98% pulse duration coverage, no history gaps, artifact and adjacent-pair checks. Latest qualifying window expires after 15 minutes. Not ECG-validated. Scrape time is analysis observation time; the measurement window end is shown separately.'
panel("HRV · RMSSD", hrv_query, "stat", 0,34,8,4,"ms",hrv_description)
panels[-1]["fieldConfig"]["defaults"].update(decimals=0,noValue="Insufficient clean intervals",color={"mode":"fixed","fixedColor":"#D3F5AE"})
panel("HRV · latest pulse coverage", '100 * whoop_hrv_latest_window_coverage_ratio', "stat",8,34,8,4,"percent","Pulse interval duration / 300 seconds for the latest analyzed window. Acceptance requires 98–102%. This is not a medical confidence score.")
panel("HRV · measurement age", 'time() - whoop_hrv_window_end_timestamp_seconds', "stat",16,34,8,4,"s","Age of the qualifying five-minute measurement window.")
panel("HRV history · five-minute RMSSD", hrv_query, "timeseries",0,38,24,7,"ms",hrv_description)
panels[-1]["targets"][0]["legendFormat"]="RMSSD"
for existing in panels: existing["gridPos"]["y"] += 4
strain_query = 'life_strain_score and on(device) (life_strain_ready == 1)'
sleep_query = 'life_sleep_score and on(device) (life_sleep_score_ready == 1)'
strain_description = 'Life cardio strain, not WHOOP strain: Edwards load from recorded heart rate and your configured maximum, mapped logarithmically to 0–21. See coverage; missing time contributes no load. Set maximum HR in Life. No age-based defaults. See docs/SCORING.md.'
sleep_description = 'Self-reported sleep duration minus awake time / your configured sleep target, capped at 100%. This is a duration score, not inferred sleep quality or sleep stages. Log sleep in Life. Latest completed session expires after 36 hours.'
for index, (title, query, unit, color, description, missing) in enumerate([
    ("HRV", hrv_query, "ms", "#D3F5AE", hrv_description, "Insufficient clean intervals"),
    ("Strain · Life", strain_query, "suffix: / 21", "#FF8F78", strain_description, "Set max HR in Life; record 10 min"),
    ("Sleep · duration score", sleep_query, "percent", "#BFB8F5", sleep_description, "Log sleep and set target in Life"),
]):
    panel(title, query, "stat", index * 8, 0, 8, 4, unit, description)
    panels[-1]["fieldConfig"]["defaults"].update(noValue=missing,decimals=1 if index==1 else 0,color={"mode":"fixed","fixedColor":color})
    panels[-1]["options"].update(textMode="value",justifyMode="center",wideLayout=True)
vitals_y = 4
for existing in panels:
    if existing['gridPos']['y'] >= vitals_y: existing['gridPos']['y'] += 4
resp_query = 'whoop_respiratory_rate_estimated_breaths_per_minute and on(device) (whoop_respiratory_rate_ready == 1) and on(device) (time() - whoop_respiratory_rate_window_end_timestamp_seconds <= 900)'
oxygen_query = 'whoop_spo2_percent and on(device) (whoop_spo2_ready == 1) and on(device) (time() - whoop_spo2_observed_timestamp_seconds <= 86400)'
resp_description = 'Estimated respiratory modulation of pulse intervals, not a device-reported breathing rate. At least 90 contiguous seconds, coverage and artifact checks, spectral/autocorrelation agreement and stable frequency. Accuracy has not been validated against a respiratory reference. Hidden after 15 minutes. Analysis scrape time is distinct from measurement time.'
oxygen_description = 'Device-calculated SpO2 from Harvard 41.17.4.0 history byte 86. Zero and firmware error codes are not percentages. 98 is withheld because the firmware can also return 98 for missing optical DC. Latest usable reading within 24 hours; see measurement age. Accuracy has not been checked with an independent sensor.'
panel('Breathing · est.', resp_query, 'stat',0,vitals_y,6,4,'suffix: breaths/min',resp_description)
panels[-1]['fieldConfig']['defaults'].update(decimals=0,noValue='Awaiting clean pulse timing',color={'mode':'fixed','fixedColor':'#D3F5AE'})
panel('SpO₂', oxygen_query, 'stat',6,vitals_y,6,4,'percent',oxygen_description)
panels[-1]['fieldConfig']['defaults'].update(decimals=0,noValue='Awaiting WHOOP reading',color={'mode':'fixed','fixedColor':'#BFB8F5'})
panel('Signal quality','whoop_respiratory_rate_ready','stat',12,vitals_y,6,4,None,'Signal checks are engineering screens, not a medical confidence score. A successful screen does not establish accuracy.')
panels[-1]['fieldConfig']['defaults'].update(noValue='Analysis unavailable',mappings=[{'type':'value','options':{'0':{'text':'Insufficient signal','color':'#A3B0A6'},'1':{'text':'Checks passed · unvalidated','color':'#D3F5AE'}}}])
panel('Oxygen age','time() - whoop_spo2_observed_timestamp_seconds','stat',18,vitals_y,6,4,'s',oxygen_description)
panels[-1]['fieldConfig']['defaults'].update(noValue='No usable reading yet',color={'mode':'fixed','fixedColor':'#A3B0A6'})
bottom=max(p['gridPos']['y']+p['gridPos']['h'] for p in panels)
panel('Breathing history · estimated',resp_query,'timeseries',0,bottom,12,7,'suffix: breaths/min',resp_description)
panels[-1]['targets'][0]['legendFormat']='Estimated breathing rate'
panel('Oxygen history · device calculation',oxygen_query,'timeseries',12,bottom,12,7,'percent',oxygen_description)
panels[-1]['targets'][0]['legendFormat']='Device SpO₂'
panel('Breathing · contiguous signal duration','whoop_respiratory_rate_contiguous_seconds','stat',0,bottom+7,8,4,'s','Longest segment when none reach 90 seconds; otherwise the latest candidate segment. Passing duration alone is insufficient.')
panel('Breathing · measurement age','time() - whoop_respiratory_rate_window_end_timestamp_seconds','stat',8,bottom+7,8,4,'s',resp_description)
panel('Oxygen · device status','whoop_spo2_raw_code','stat',16,bottom+7,8,4,None,'Raw diagnostic result code. 0 means no computed value. 98 is ambiguous and never published as saturation. Other flags stay unavailable. See docs/RESPIRATION-OXYGEN.md.')
panels[-1]['fieldConfig']['defaults']['mappings']=[{'type':'value','options':{'0':{'text':'Not measured'},'98':{'text':'Ambiguous firmware result'}}}]
bottom=max(p['gridPos']['y']+p['gridPos']['h'] for p in panels)
for i,(title,query,unit,description) in enumerate([
    ('Strain · recorded day','life_strain_coverage_ratio','percentunit','Fraction of elapsed local day covered by consecutive valid HR records. Missing intervals are excluded.'),
    ('Cardio load · Edwards','life_cardio_load','short',strain_description),
    ('Sleep · logged duration','life_sleep_duration_seconds','s',sleep_description),
    ('Sleeping heart rate','life_sleep_heart_rate_bpm','bpm','Median HR during your logged sleep interval, requires 80% recorded coverage and 30 minutes.'),
    ('Sleeping HRV','life_sleep_hrv_ms','ms','Median qualifying five-minute RMSSD windows inside logged sleep. At least three windows and 50% union coverage.'),
    ('Sleeping wrist temperature','life_sleep_temperature_celsius','celsius','Median firmware-decoded wrist temperature during logged sleep, requires 80% recorded coverage.'),
    ('Wrist temperature · baseline change','life_sleep_temperature_delta_celsius','celsius','Difference from the median of at least seven prior nights within 28 days. At most one sleep of at least three hours per date.'),
    ('Sleep · measurement age','time() - life_sleep_end_timestamp_seconds','s','Time since the most recent completed, self-reported sleep session. Results expire after 36 hours.'),
]):
    panel(title,query,'stat',(i%4)*6,bottom+(i//4)*4,6,4,unit,description)
    panels[-1]['fieldConfig']['defaults']['noValue']='Awaiting recorded data'
panel('Strain history · recorded load',strain_query,'timeseries',0,bottom+8,12,7,'suffix: / 21',strain_description+' Each point is the accumulated daily load at analysis time. Late backfill updates the current total without rewriting previous scrapes.')
panel('Sleep history · reported duration', 'life_sleep_duration_seconds','timeseries',12,bottom+8,12,7,'s',sleep_description)
# Keep coverage and reported duration adjacent to the summary cards.
summary_titles={'Strain · recorded day':0,'Cardio load · Edwards':6,'Sleep · logged duration':12,'Sleep · measurement age':18}
for item in panels:
    if item['title'] in summary_titles:
        item['gridPos'].update(x=summary_titles[item['title']],y=8)
    elif item['gridPos']['y']>=8:item['gridPos']['y']+=4
for item in panels:
    if item['gridPos']['y']>=4:item['gridPos']['y']+=3
panel('Stored history delay','time() - whoop_history_last_sample_timestamp_seconds','stat',0,4,12,3,'s',
      'Age of the latest indexed WHOOP history record. Live HR can remain current while stored history is delayed. HRV, breathing, temperature and overnight summaries depend on this history.')
panels[-1]['fieldConfig']['defaults'].update(noValue='Waiting for stored history',color={'mode':'thresholds'},thresholds={'mode':'absolute','steps':[{'color':'green','value':None},{'color':'orange','value':600},{'color':'red','value':1800}]})
panel('Analysis queue','whoop_analysis_pending_windows','stat',12,4,12,3,'short',
      'HRV and breathing windows awaiting calculation or recalculation. Existing downloaded data is processed before the analyzer returns to its idle interval.')
# Availability percentages are measured coverage, never physiological confidence.
quality_panels = [
    ('Stored records · 24h', 'whoop_history_record_coverage_24h_ratio', 'percentunit', 'Fraction of the past 24 hours covered by consecutive, nonconflicting history records. Missing time and sequence gaps are excluded. This is data availability, not sensor accuracy.'),
    ('HRV · qualified coverage 24h', 'whoop_hrv_qualified_coverage_24h_ratio', 'percentunit', 'Union of qualifying five-minute HRV windows over the past 24 hours. Overlapping windows count their covered time once. This is availability, not a medical confidence score.'),
    ('Breathing · qualified coverage 24h', 'whoop_respiratory_rate_qualified_coverage_24h_ratio', 'percentunit', 'Union of qualifying estimated-breathing segments over the past 24 hours. A zero means no segment passed the existing signal rules, not zero breaths.'),
    ('HRV · continuity-limited windows', 'whoop_hrv_pair_limited_windows_24h', 'short', 'Number of analyzed windows in the past 24 hours failing the adjacent-pair requirement. Includes other possible failures. Empty batches can be legitimate at low HR or withheld by the device; no intervals are joined across them.'),
]
for item in panels:
    if item['gridPos']['y'] >= 7: item['gridPos']['y'] += 3
for index,(title,query,unit,description) in enumerate(quality_panels):
    panel(title,query,'stat',index*6,7,6,3,unit,description)
    panels[-1]['fieldConfig']['defaults'].update(noValue='Analysis unavailable',decimals=0,color={'mode':'fixed','fixedColor':'#A3B0A6'})
panels.sort(key=lambda p:(p['gridPos']['y'],p['gridPos']['x']))
dashboard={"uid":"life-whoop","title":"Life · WHOOP","schemaVersion":41,"version":1,"tags":["life","whoop"],"timezone":"browser","time":{"from":"now-6h","to":"now"},"refresh":"30s","panels":panels}
(root/'dashboards/whoop.json').write_text(json.dumps(dashboard,indent=2)+"\n")
print("Generated Life Prometheus dashboard.")
