"""交接封存与补正追溯。

设计约束：
* 封存是快照，任何字段一旦写入不得覆盖；补正只能追加新的封存记录。
* 已纳入封存的项目按 report_key 去重，补正重跑不得再次纳入。
* 隔离证据随封存件保存；补正未处理的旧证据以 carried 方式携转，记录不丢失。
* 补正结果必须通过 origin_ref 指回父封存中的隔离证据来源。
"""
import hashlib
import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from app.config import HANDOVER_DEADLINE, RULE_VERSION
from app.errors import DomainError

TZ = timezone(timedelta(hours=8))
RANK = {'PASS': 0, 'UNCERTAIN': 1, 'FAIL': 2}
IDENTITY_FIELDS = ('id', 'version', 'sample_id', 'batch', 'product', 'analyte', 'sampled', 'method')
CONTENT_FIELDS = ('id', 'version', 'sample_id', 'batch', 'product', 'analyte', 'sampled',
                  'reported', 'result', 'unit', 'lod', 'relation')


def _digest(parts):
    return hashlib.sha256('\x1f'.join(str(p) for p in parts).encode()).hexdigest()


def report_key(fields):
    return _digest(fields.get(k, '') for k in IDENTITY_FIELDS)[:16]


def base_key(fields):
    """不含 version 的身份键，用于识别已交件报告的新修订版。"""
    return _digest(fields.get(k, '') for k in IDENTITY_FIELDS if k != 'version')[:16]


def content_key(fields):
    return _digest(fields.get(k, '') for k in CONTENT_FIELDS)[:16]


def parse_cutoff(parameters):
    """返回本次交接采用的截止依据。显式参数优先，否则站点默认出证时点。"""
    raw = (parameters or {}).get('cutoff')
    if raw:
        try:
            moment = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
            if moment.tzinfo is None:
                raise ValueError
        except ValueError:
            raise DomainError('CUTOFF', '截止时点必须为含时区的ISO时间')
        local = moment.astimezone(TZ)
        return {'as_of': local.isoformat(), 'date': local.date().isoformat(),
                'timezone': 'Asia/Shanghai', 'source': '请求参数',
                'basis': '请求参数 cutoff=%s' % raw,
                'rule': '报告日期（含当日）不晚于截止当地日期者纳入；其余暂不采信'}
    local = datetime.fromisoformat(HANDOVER_DEADLINE).astimezone(TZ)
    return {'as_of': local.isoformat(), 'date': local.date().isoformat(),
            'timezone': 'Asia/Shanghai', 'source': '站点默认',
            'basis': '站点默认出证时点 %s' % HANDOVER_DEADLINE,
            'rule': '报告日期（含当日）不晚于截止当地日期者纳入；其余暂不采信'}


def _calc_evidence(row, run_id):
    fields = {k: row.get(k, '') for k in CONTENT_FIELDS}
    return {
        'ref': '%s:L%s' % (run_id, row.get('line', '')),
        'run_id': run_id, 'line': row.get('line'),
        'id': str(row.get('id', '')), 'version': str(row.get('version', '')),
        'sample_id': row.get('sample_id', ''), 'batch': row.get('batch', ''),
        'product': row.get('product', ''), 'analyte': row.get('analyte', ''),
        'method': row.get('method', ''), 'standard': row.get('standard', ''),
        'sampled': row.get('sampled', ''), 'reported': row.get('reported', ''),
        'lower': row.get('lower', ''), 'upper': row.get('upper', ''),
        'upper_exclusive': row.get('upper_exclusive', False), 'limit': row.get('limit', ''),
        'code': row.get('code', ''), 'replaces': row.get('replaces', '') or '',
        'result': row.get('result', ''), 'unit': row.get('unit', ''), 'lod': row.get('lod', ''),
        'report_key': report_key(row), 'base_key': base_key(row),
        'content_key': content_key(fields),
    }


def _withheld_evidence(item, run_id):
    raw = item.get('raw')
    fields = raw if isinstance(raw, dict) else {}
    identity = fields.get('id') or item.get('id') or ''
    payload_key = content_key(fields) if fields else _digest(
        [json.dumps(raw, sort_keys=True, ensure_ascii=False, default=str)])[:16]
    return {
        'ref': '%s:L%s' % (run_id, item.get('line', '')),
        'run_id': run_id, 'line': item.get('line'), 'id': str(identity),
        'version': str(fields.get('version', '')), 'batch': fields.get('batch', ''),
        'code': item.get('code', ''), 'raw': raw,
        'report_key': report_key(fields) if identity else None,
        'base_key': base_key(fields) if identity else None,
        'content_key': payload_key,
        'carried': False, 'retained': False,
        'origin_ref': None, 'origin_basis': None,
    }


def _sealed_index(seals):
    """所有封存件中已经纳入的项目：report_key / base_key -> (seal_id, evidence)。"""
    index, bases = {}, {}
    for seal in seals:
        for item in seal.get('cumulative') or seal.get('included') or []:
            index.setdefault(item['report_key'], (seal['seal_id'], item))
            if item.get('base_key'):
                bases.setdefault(item['base_key'], (seal['seal_id'], item))
    return index, bases


def _identity(item):
    if item.get('id'):
        return ('id', str(item['id']), str(item.get('version', '')))
    # 列数不齐等无ID证据只能按来源行定位，以便修复后仍能回到同一条来源。
    if item.get('line') is not None:
        return ('line', item['line'])
    return ('raw', item.get('content_key'))


def _pick(candidates, item):
    """在一组同源证据中挑出唯一对应项，并说明匹配依据。"""
    if not candidates:
        return None, 'ambiguous'
    exact = [c for c in candidates
             if c.get('content_key') and c.get('content_key') == item.get('content_key')]
    if len(exact) == 1:
        return exact[0], 'content'
    if len(candidates) == 1:
        only = candidates[0]
        if item.get('id') and only.get('id') == item.get('id'):
            return only, 'id_version'
        return only, 'line'
    same = [c for c in candidates if c.get('line') is not None
            and c.get('line') == item.get('line')]
    if len(same) == 1:
        return same[0], 'line'
    return None, 'ambiguous'


def _scope(state, seals, cutoff):
    run_id = state['run_id']
    sealed, sealed_bases = _sealed_index(seals)
    included, deferred, already, divergences = [], [], [], []
    for row in state.get('calculations', []):
        evidence = _calc_evidence(row, run_id)
        prior = sealed.get(evidence['report_key'])
        if prior:
            seal_id, old = prior
            evidence.update(seal_id=seal_id, sealed_code=old.get('code'),
                            sealed_ref=old.get('ref'))
            if (evidence['lower'], evidence['upper'], evidence['upper_exclusive'],
                    evidence['code']) != (old.get('lower'), old.get('upper'),
                                          old.get('upper_exclusive'), old.get('code')):
                evidence['divergent'] = True
                divergences.append({'ref': evidence['ref'], 'id': evidence['id'],
                                    'seal_id': seal_id, 'sealed_ref': old.get('ref'),
                                    'sealed': {'lower': old.get('lower'), 'upper': old.get('upper'),
                                               'code': old.get('code')},
                                    'rerun': {'lower': evidence['lower'],
                                              'upper': evidence['upper'],
                                              'code': evidence['code']},
                                    'reason': 'SEALED_DIVERGENCE'})
            else:
                evidence['divergent'] = False
            already.append(evidence)
        elif evidence['base_key'] in sealed_bases:
            # 已交件同源报告的新修订版：不覆盖、不重复纳入，留作差异证据。
            seal_id, old = sealed_bases[evidence['base_key']]
            evidence.update(seal_id=seal_id, sealed_code=old.get('code'),
                            sealed_ref=old.get('ref'), divergent=True)
            divergences.append({'ref': evidence['ref'], 'id': evidence['id'],
                                'seal_id': seal_id, 'sealed_ref': old.get('ref'),
                                'sealed': {'version': old.get('version'),
                                           'lower': old.get('lower'), 'upper': old.get('upper'),
                                           'code': old.get('code')},
                                'rerun': {'version': evidence['version'],
                                          'lower': evidence['lower'],
                                          'upper': evidence['upper'],
                                          'code': evidence['code']},
                                'reason': 'SEALED_REVISION'})
            already.append(evidence)
        elif str(row.get('reported', '')) > cutoff['date']:
            evidence['reason'] = 'CUTOFF'
            deferred.append(evidence)
        else:
            included.append(evidence)
    withheld = [_withheld_evidence(q, run_id) for q in state.get('quarantine', [])]
    return included, deferred, already, withheld, divergences


def _link_new_withheld(withheld, parent_withheld, consumed):
    """为本次仍隔离的条目补上父封存来源；未匹配的父证据随后携转。"""
    groups = defaultdict(list)
    for idx, item in enumerate(parent_withheld):
        if idx not in consumed:
            groups[_identity(item)].append((idx, item))
    for item in withheld:
        bucket = groups.get(_identity(item), [])
        if not bucket:
            continue
        picked, basis = _pick([entry for _, entry in bucket], item)
        if picked is None:
            continue
        for idx, entry in bucket:
            if entry is picked:
                consumed.add(idx)
                item['origin_ref'] = entry.get('origin_ref') or entry['ref']
                item['origin_basis'] = basis
                item['origin_code'] = entry.get('code')
                break


def grade_rows(cumulative, withheld, deferred, before=None):
    buckets = defaultdict(lambda: {'codes': [], 'samples': set(), 'open': 0,
                                   'retained': 0, 'deferred': 0})
    for item in cumulative:
        bucket = buckets[item['batch']]
        bucket['codes'].append(item.get('code'))
        bucket['samples'].add(item.get('sample_id'))
    for item in withheld:
        if not item.get('batch'):
            continue  # 无批次归属的证据只进隔离清单，不参与判级
        bucket = buckets[item['batch']]
        if item.get('carried') or item.get('retained'):
            bucket['retained'] += 1
        else:
            bucket['open'] += 1
    for item in deferred:
        if item.get('batch'):
            buckets[item['batch']]['deferred'] += 1
    before_map = {row['batch']: row for row in (before or [])}
    rows = []
    for batch in sorted(buckets):
        bucket = buckets[batch]
        after = max(bucket['codes'], key=lambda c: RANK[c]) if bucket['codes'] else None
        prior = before_map.get(batch)
        before_code = prior.get('code') if prior else None
        # 只有存在前件基线时才谈“变化”；基线中缺该批次说明本批为新增。
        changed = bool(before) and (before_code != after)
        before_label = before_code or '未交件'
        after_label = after or ('待补正' if (bucket['open'] or bucket['deferred']) else '无采纳项')
        rows.append({
            'batch': batch, 'code': after, 'before': before_code, 'after': after,
            'before_label': before_label, 'after_label': after_label,
            'changed': changed, 'delta': '%s → %s' % (before_label, after_label),
            'projects': len(bucket['codes']), 'samples': len(bucket['samples']),
            'open': bucket['open'], 'retained': bucket['retained'],
            'deferred': bucket['deferred'],
            'provisional': bool(bucket['open'] or bucket['deferred']
                                or (bucket['retained'] and not bucket['codes'])),
        })
    return rows


def _seal_id(parent, run_id, cutoff, kind):
    raw = '%s|%s|%s|%s' % (parent or '', run_id, cutoff['as_of'], kind)
    day = cutoff['date'].replace('-', '')
    return 'HS-%s-%s' % (day, hashlib.sha256(raw.encode()).hexdigest()[:8].upper())


def seal_checksum(seal):
    core = {k: v for k, v in seal.items() if k != 'checksum'}
    return hashlib.sha256(json.dumps(core, sort_keys=True, ensure_ascii=False,
                                     default=str).encode()).hexdigest()


def build_seal(state, seals, cutoff, parent=None, note='', created_at=None, origins=None):
    """生成一份不可变封存件。

    parent 为封存链上的直接前件（默认最近一份）。若本次纳入项能对应到前件中的
    隔离证据，则本件为补正封存；否则为追加交接封存。首件没有前件。
    """
    run_id = state.get('run_id', '')
    parent_body = None
    if parent:
        parent_body = next((s for s in seals if s['seal_id'] == parent), None)
        if parent_body is None:
            raise DomainError('NOT_FOUND', '父封存件不存在', 404)
    elif seals:
        raise DomainError('CHAIN', '已有封存件时必须指定前件，保持封存链完整', 422)
    included, deferred, already, withheld, divergences = _scope(state, seals, cutoff)
    # 可用 origins 显式声明补正来源（本次纳入项 ref -> 前件证据 ref），
    # 用于源行位移、无法按内容唯一匹配的情形；未声明的仍自动匹配。
    declared = {}
    if origins:
        if not isinstance(origins, dict):
            raise DomainError('CORRECTION', 'origins 必须是对象映射', 422)
        declared = {str(k): str(v) for k, v in origins.items()}

    corrections = []
    if parent_body:
        parent_withheld = list(parent_body.get('withheld') or [])
        consumed = set()
        groups = defaultdict(list)
        by_line = defaultdict(list)
        for idx, item in enumerate(parent_withheld):
            groups[_identity(item)].append((idx, item))
            if item.get('id') is None or item.get('id') == '':
                by_line[item.get('line')].append((idx, item))
        for item in included:
            wanted = declared.get(item['ref'])
            bucket = groups.get(_identity(item), [])
            line_basis = ''
            if not bucket and by_line.get(item.get('line')):
                # 无ID的隔离行（如列数不齐）修复后只能按来源行回溯。
                bucket = by_line[item.get('line')]
                line_basis = 'line'
            if wanted:
                bucket = [(idx, entry) for idx, entry in enumerate(parent_withheld)
                          if entry['ref'] == wanted or (entry.get('origin_ref') or '') == wanted]
                if not bucket:
                    raise DomainError(
                        'CORRECTION',
                        '声明来源 %s 不在前件 %s 的隔离证据中' % (wanted, parent), 422)
            if not bucket:
                # 与前件隔离证据无关的新报告：普通纳入，不需要补正溯源。
                continue
            candidates = [entry for idx, entry in bucket if idx not in consumed]
            if wanted:
                picked, basis = (candidates[0], 'declared') if candidates else (None, 'ambiguous')
            elif line_basis:
                picked, basis = (candidates[0], line_basis) if candidates else (None, 'ambiguous')
            else:
                picked, basis = _pick(candidates, item)
            if picked is None:
                raise DomainError(
                    'CORRECTION',
                    '补正项 %s 对应多条同版隔离证据，无法回到同一条来源；'
                    '请用 origins 显式声明来源' % (item['id'] or item['ref']), 422)
            for idx, entry in bucket:
                if entry is picked:
                    consumed.add(idx)
                    item['origin_ref'] = entry.get('origin_ref') or entry['ref']
                    item['origin_basis'] = basis
                    item['origin_code'] = entry.get('code')
                    item['origin_first_seal'] = entry.get('origin_first_seal') or parent
                    break
            corrections.append(item)
        _link_new_withheld(withheld, parent_withheld, consumed)
        carried = []
        for idx, item in enumerate(parent_withheld):
            if idx in consumed:
                continue
            keep = dict(item)
            keep.update(carried=True, retained=True,
                        origin_ref=item.get('origin_ref') or item['ref'],
                        origin_basis='carried',
                        origin_first_seal=item.get('origin_first_seal') or parent)
            carried.append(keep)
        withheld = withheld + carried
        cumulative = list(parent_body.get('cumulative') or parent_body.get('included') or [])
        before = parent_body.get('grades') or []
        kind = 'correction' if corrections else 'supplement'
    else:
        cumulative, before, kind = [], [], 'initial'

    seen = {item['report_key'] for item in cumulative}
    for item in included:
        if item['report_key'] in seen:
            raise DomainError(
                'SEALED', '纳入项 %s 已在既往封存件中，不能重复纳入' % (item['id'] or item['ref']), 409)
        cumulative.append(item)
        seen.add(item['report_key'])

    if not included:
        raise DomainError('NO_CHANGE', '本次没有可纳入的项目（已交件或全部暂不采信）', 409)

    grades = grade_rows(cumulative, withheld, deferred, before)
    body = {
        'seal_id': _seal_id(parent, run_id, cutoff, kind),
        'kind': kind, 'parent': parent or '', 'run_id': run_id,
        'created_at': created_at or datetime.now(timezone.utc).isoformat(),
        'note': note or '',
        'cutoff': cutoff, 'rule_version': RULE_VERSION,
        'source': {'run_id': run_id,
                   'sha256': (state.get('manifest') or {}).get('sha256', ''),
                   'parameters': (state.get('manifest') or {}).get('parameters', {})},
        'included': included, 'deferred': deferred, 'withheld': withheld,
        'already_sealed': already, 'divergences': divergences, 'corrections': corrections,
        'cumulative': cumulative, 'grades': grades,
        'grade_changes': [row for row in grades if row['changed']],
        'balance': state.get('reconcile') or {},
        'counts': {
            'included': len(included), 'withheld': sum(1 for w in withheld if not w.get('carried')),
            'carried': sum(1 for w in withheld if w.get('carried')),
            'deferred': len(deferred), 'already_sealed': len(already),
            'cumulative': len(cumulative), 'divergences': len(divergences),
            'corrections': len(corrections),
        },
    }
    if parent_body:
        body['parent_checksum'] = parent_body.get('checksum', '')
    body['checksum'] = seal_checksum(body)
    return body


def _attempt(fn):
    try:
        return {'ok': True, 'body': fn()}
    except DomainError as error:
        return {'ok': False, 'code': error.code, 'error': error.message}


def _draft(state, seals, cutoff):
    included, deferred, already, withheld, divergences = _scope(state, seals, cutoff)
    if seals:
        parent_withheld = list(seals[-1].get('withheld') or [])
        consumed = set()
        _link_new_withheld(withheld, parent_withheld, consumed)
        for idx, item in enumerate(parent_withheld):
            if idx in consumed:
                continue
            keep = dict(item)
            keep.update(carried=True, retained=True,
                        origin_ref=item.get('origin_ref') or item['ref'],
                        origin_basis='carried',
                        origin_first_seal=item.get('origin_first_seal') or seals[-1]['seal_id'])
            withheld.append(keep)
    grades = grade_rows(already + included, withheld, deferred,
                        (seals[-1].get('grades') if seals else []))
    return {
        'seal_id': '草稿', 'kind': 'draft', 'parent': '', 'run_id': state.get('run_id', ''),
        'created_at': datetime.now(timezone.utc).isoformat(), 'note': '',
        'cutoff': cutoff, 'rule_version': RULE_VERSION,
        'included': included, 'deferred': deferred, 'withheld': withheld,
        'already_sealed': already, 'divergences': divergences, 'corrections': [],
        'cumulative': already + included, 'grades': grades,
        'grade_changes': [], 'balance': state.get('reconcile') or {},
        'counts': {
            'included': len(included),
            'withheld': sum(1 for w in withheld if not w.get('carried')),
            'carried': sum(1 for w in withheld if w.get('carried')),
            'deferred': len(deferred), 'already_sealed': len(already),
            'cumulative': len(included) + len(already), 'divergences': len(divergences),
            'corrections': 0,
        },
    }


def _summary(seal):
    return {
        'seal_id': seal['seal_id'], 'kind': seal['kind'], 'parent': seal.get('parent', ''),
        'run_id': seal.get('run_id', ''), 'created_at': seal.get('created_at', ''),
        'note': seal.get('note', ''), 'checksum': seal.get('checksum', ''),
        'counts': seal.get('counts', {}), 'grades': seal.get('grades', []),
        'cutoff': seal.get('cutoff', {}),
    }


def preview(state, seals):
    cutoff = parse_cutoff(state.get('parameters') or {})
    latest = seals[-1]['seal_id'] if seals else None
    candidate = _attempt(lambda: build_seal(state, seals, cutoff, parent=latest))
    if candidate.get('ok'):
        body = candidate['body']
        intent = {'initial': 'initial', 'correction': 'correction',
                  'supplement': 'supplement'}[body['kind']]
    else:
        body = _draft(state, seals, cutoff)
        intent = 'none'
    label = {'correction': '补正封存草稿', 'initial': '首次交接封存草稿',
             'supplement': '追加交接封存草稿', 'none': '交接范围预览'}[intent]
    counts = body['counts']
    open_batches = [g['batch'] for g in body['grades'] if g.get('provisional')]
    if intent == 'correction':
        recommendation = (
            '检测到 %d 项既有隔离证据的补正，建议按补正封存并入（前件 %s）；'
            '已交件 %d 项保持原样，不重复纳入。' %
            (counts['corrections'], latest, counts['already_sealed']))
    elif intent == 'initial':
        recommendation = (
            '建议按截止依据封存 %d 项可采信项目；暂不采信 %d 项，超期延期 %d 项。' %
            (counts['included'], counts['withheld'], counts['deferred']))
    elif intent == 'supplement':
        recommendation = (
            '本次 %d 项为截止内新增报告（无隔离证据可补正），建议按追加交接封存并入。' %
            counts['included'])
    else:
        recommendation = '本次尚无可封存的新增项目：%s' % (candidate.get('error') or '无新增可采信项目')
    if open_batches:
        recommendation += ' 批次 %s 仍有待补正/延期项，等级为暂定。' % '、'.join(open_batches)
    return {
        'cutoff': cutoff,
        'seals': [_summary(seal) for seal in seals],
        'latest_seal': latest,
        'intent': intent, 'label': label,
        'view': body,
        'counts': counts,
        'recommendation': recommendation,
        'candidate': candidate,
        'scope': {'included': body['included'], 'withheld': body['withheld'],
                  'deferred': body['deferred'], 'already_sealed': body['already_sealed'],
                  'divergences': body['divergences']},
    }


def attach_ledger(state, seals):
    out = dict(state)
    out['handover'] = preview(out, seals)
    return out
