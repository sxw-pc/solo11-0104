import json
from pathlib import Path
from .store import connect
def report(db_path,out):
    db=connect(db_path);data={t:[dict(r) for r in db.execute(f'SELECT * FROM {t} ORDER BY 1')] for t in ('batch','zone','gate_map','ticket','setting','event','issue','occupancy','evidence','decision','over_capacity','run')}
    data['source']=[dict(r) for r in db.execute('SELECT id,name,sha256,length(raw) AS bytes FROM source ORDER BY id')]
    p=Path(out);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    lines=['# 闸机客流证据','']
    lines += [f"- {r['zone']}: {r['status']}，峰值 {r['peak']}" for r in data['decision']]
    if data['over_capacity']:
        lines.append('')
        lines.append('## 超容区间')
        for r in data['over_capacity']:
            lines.append(f"- {r['zone']} {r['started_at']} → {r['ended_at'] or '观测结束未解除'}：峰值 {r['peak']}，持续 {r['duration_seconds']} 秒，票据 {r['tickets']}，{r['classification']}（证据 {r['confidence']}，触发事件 {r['trigger_event_id']}，解除事件 {r['clear_event_id']}）")
    p.with_suffix('.md').write_text('\n'.join(lines),encoding='utf-8')
    db.close();return p
