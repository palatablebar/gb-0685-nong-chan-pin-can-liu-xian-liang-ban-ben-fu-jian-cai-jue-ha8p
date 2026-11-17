"""封存证据导出：一份CSV，含封存头、交接范围、等级变化、补正溯源与隔离证据。"""
import csv
import io
import json


def _dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def seal_csv(seal):
    out = io.StringIO(newline='')
    writer = csv.writer(out)
    writer.writerow(['记录类型', '封存号', '来源行', '业务ID', '原因码', '内容'])
    sid = seal.get('seal_id', '')
    writer.writerow(['封存头', sid, '', '', seal.get('kind', ''), _dump({
        'seal_id': sid, 'kind': seal.get('kind'), 'parent': seal.get('parent'),
        'parent_checksum': seal.get('parent_checksum'), 'run_id': seal.get('run_id'),
        'created_at': seal.get('created_at'), 'rule_version': seal.get('rule_version'),
        'checksum': seal.get('checksum'), 'cutoff': seal.get('cutoff'),
        'source': seal.get('source'), 'counts': seal.get('counts'),
        'note': seal.get('note'),
    })])
    writer.writerow(['对账', sid, '', '', '', _dump(seal.get('balance', {}))])
    for grade in seal.get('grades', []):
        writer.writerow(['等级', sid, '', grade.get('batch', ''), 'CHANGED' if grade.get('changed') else '',
                         _dump(grade)])
    corrected = {item['ref'] for item in seal.get('corrections', [])}
    for item in seal.get('included', []):
        if item.get('ref') in corrected:
            continue
        writer.writerow(['纳入', sid, item.get('line', ''), item.get('id', ''), 'INCLUDED', _dump(item)])
    for item in seal.get('corrections', []):
        writer.writerow(['补正', sid, item.get('line', ''), item.get('id', ''), 'CORRECTION',
                         _dump(item)])
    for item in seal.get('withheld', []):
        writer.writerow(['隔离', sid, item.get('line', ''), item.get('id', ''), item.get('code', ''),
                         _dump(item)])
    for item in seal.get('deferred', []):
        writer.writerow(['延期', sid, item.get('line', ''), item.get('id', ''), item.get('reason', ''),
                         _dump(item)])
    for item in seal.get('already_sealed', []):
        writer.writerow(['已交件', sid, item.get('line', ''), item.get('id', ''),
                         'SEALED', _dump(item)])
    for item in seal.get('divergences', []):
        writer.writerow(['差异', sid, '', item.get('id', ''), item.get('reason', ''), _dump(item)])
    return '﻿' + out.getvalue()
