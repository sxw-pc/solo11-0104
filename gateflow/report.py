import json
from pathlib import Path
from .store import connect
def report(db_path,out):
    db=connect(db_path);data={t:[dict(r) for r in db.execute(f'SELECT * FROM {t} ORDER BY 1')] for t in ('batch','zone','gate_map','ticket','event','issue','occupancy','decision')}
    data['source']=[dict(r) for r in db.execute('SELECT id,name,sha256,length(raw) AS bytes FROM source ORDER BY id')]
    p=Path(out);p.parent.mkdir(parents=True,exist_ok=True);p.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    p.with_suffix('.md').write_text('# 闸机客流证据\n\n'+'\n'.join(f"- {r['zone']}: {r['status']}，峰值 {r['peak']}" for r in data['decision']),encoding='utf-8')
    db.close();return p
