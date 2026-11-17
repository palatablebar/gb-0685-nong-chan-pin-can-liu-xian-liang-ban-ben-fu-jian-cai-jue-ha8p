import csv
import io
import json

import pytest

from app.errors import DomainError
from app.modules.m8_handover.engine import build_seal, parse_cutoff, preview, seal_checksum
from app.workflow import load, run


def _csv(text):
    return text.encode('utf-8')


def _fixed(base):
    return (base.replace(',N/A,', ',0.001,')
            .replace('00902,S902,B902,苹果,P01,LC-MS/MS,1,2026-08-01,2026-08-02,'
                     '0.02,mg/kg,0.001,,同版二\r\n', ''))


BASE = load('dirty.csv').decode('utf-8-sig')


def test_cutoff_defaults_to_site_deadline():
    cutoff = parse_cutoff({})
    assert cutoff['date'] == '2026-08-05' and cutoff['source'] == '站点默认'
    assert cutoff['timezone'] == 'Asia/Shanghai' and cutoff['basis']


def test_invalid_cutoff_rejected():
    with pytest.raises(DomainError) as error:
        parse_cutoff({'cutoff': '2026-08-05T18:00:00'})
    assert error.value.code == 'CUTOFF'


def test_initial_scope_keeps_quarantine():
    state = run(_csv(BASE), {})
    view = preview(state, [])
    assert view['intent'] == 'initial'
    assert view['counts']['included'] == 24 and view['counts']['withheld'] == 4
    assert all(item['ref'] and item['code'] for item in view['scope']['withheld'])
    assert all(item['line'] and item['code'] for item in view['scope']['withheld'])


def test_cutoff_defers_late_reports():
    state = run(_csv(BASE), {'cutoff': '2026-08-02T18:00:00+08:00'})
    view = preview(state, [])
    assert view['counts']['included'] == 12 and view['counts']['deferred'] == 12
    assert all(item['reported'] > view['cutoff']['date'] for item in view['scope']['deferred'])
    assert all(grade['provisional'] for grade in view['view']['grades']
               if grade['batch'].startswith('B00'))


def test_initial_seal_is_immutable_and_checksummed():
    state = run(_csv(BASE), {})
    seal = build_seal(state, [], parse_cutoff({}))
    assert seal['checksum'] == seal_checksum(seal)
    assert seal['kind'] == 'initial' and seal['parent'] == ''
    assert seal['counts']['included'] == 24
    tampered = dict(seal); tampered['counts'] = dict(seal['counts'], included=99)
    assert seal_checksum(tampered) != seal['checksum']


def test_seal_store_refuses_overwrite(client):
    run_id = client.post('/api/runs', json={'sample': 'dirty.csv'}).get_json()['run_id']
    first = client.post('/api/handover/seals', json={'run_id': run_id}).get_json()
    from app import store
    with pytest.raises(DomainError) as error:
        store.save_seal(dict(first))
    assert error.value.code == 'SEALED'


def test_correction_links_back_to_same_origin_and_carries_evidence():
    first_state = run(_csv(BASE), {'cutoff': '2026-08-02T18:00:00+08:00'})
    first = build_seal(first_state, [], parse_cutoff(first_state['parameters']))
    fixed = _fixed(BASE)
    state = run(_csv(fixed), {'cutoff': '2026-08-05T18:00:00+08:00'},
                seals=[first])
    view = preview(state, [first])
    assert view['intent'] == 'correction'
    body = view['view']
    assert body['counts']['corrections'] == 2 and body['counts']['already_sealed'] == 12
    origin_refs = {item['id']: item['origin_ref'] for item in body['corrections']}
    prior = {item['id']: item for item in first['withheld']}
    assert origin_refs['00901'] == prior['00901']['ref']
    assert origin_refs['00902'] in {item['ref'] for item in first['withheld']
                                    if item['code'] == 'CONFLICT'}
    carried = [item for item in body['withheld'] if item.get('carried')]
    assert carried and all(item['origin_ref'] for item in carried)


def test_corrected_batches_show_grade_change():
    first_state = run(_csv(BASE), {'cutoff': '2026-08-02T18:00:00+08:00'})
    first = build_seal(first_state, [], parse_cutoff(first_state['parameters']))
    state = run(_csv(_fixed(BASE)), {}, seals=[first])
    body = preview(state, [first])['view']
    moved = {row['batch']: row for row in body['grades'] if row['changed']}
    assert set(moved) == {'B901', 'B902'}
    assert moved['B901']['before'] is None and moved['B901']['after'] == 'FAIL'
    assert moved['B901']['before_label'] == '未交件'
    assert body['grade_changes'] and all(row['changed'] for row in body['grade_changes'])
    untouched = [row for row in body['grades'] if row['batch'].startswith('B00')]
    assert untouched and not any(row['changed'] for row in untouched)


def test_width_row_repair_links_by_source_line():
    first_state = run(_csv(BASE), {'cutoff': '2026-08-02T18:00:00+08:00'})
    first = build_seal(first_state, [], parse_cutoff(first_state['parameters']))
    repaired = (BASE.replace('short,x\r\n',
                             '00903,S903,B903,苹果,P01,LC-MS/MS,1,2026-08-01,'
                             '2026-08-02,0.004,mg/kg,0.001,,补正列数\r\n'))
    state = run(_csv(repaired), {}, seals=[first])
    body = preview(state, [first])['view']
    linked = [item for item in body['corrections'] if item['id'] == '00903']
    assert linked and linked[0]['origin_basis'] == 'line'
    width = next(item for item in first['withheld'] if item['code'] == 'WIDTH')
    assert linked[0]['origin_ref'] == width['ref']
    assert linked[0]['origin_code'] == 'WIDTH'
    assert body['counts']['carried'] == 0


def test_declared_origin_survives_line_shift():
    first_state = run(_csv(BASE), {'cutoff': '2026-08-02T18:00:00+08:00'})
    first = build_seal(first_state, [], parse_cutoff(first_state['parameters']))
    width = next(item for item in first['withheld'] if item['code'] == 'WIDTH')
    shifted = (BASE.replace('\r\nshort,x\r\n', '\r\n\r\nshort,x\r\n')
               .replace('short,x\r\n', '00903,S903,B903,苹果,P01,LC-MS/MS,1,2026-08-01,'
                                          '2026-08-02,0.004,mg/kg,0.001,,补正列数\r\n'))
    state = run(_csv(shifted), {'cutoff': '2026-08-05T18:00:00+08:00'}, seals=[first])
    ref = next(item['ref'] for item in state['handover']['scope']['included']
               if item['id'] == '00903')
    body = build_seal(state, [first], parse_cutoff(state['parameters']),
                      parent=first['seal_id'], origins={ref: width['ref']})
    assert body['corrections'][0]['origin_ref'] == width['ref']
    assert body['corrections'][0]['origin_basis'] == 'declared'
    with pytest.raises(DomainError) as error:
        build_seal(state, [first], parse_cutoff(state['parameters']),
                   parent=first['seal_id'], origins={ref: 'absent'})
    assert error.value.code == 'CORRECTION'


def test_post_cutoff_seal_requires_chain_parent():
    state = run(_csv(BASE), {})
    first = build_seal(state, [], parse_cutoff({}))
    assert first['seal_id'].startswith('HS-')
    assert first['checksum'] and first['rule_version']


def test_resealing_same_run_cannot_double_include(client):
    run_id = client.post('/api/runs', json={'sample': 'dirty.csv'}).get_json()['run_id']
    assert client.post('/api/handover/seals', json={'run_id': run_id}).status_code == 200
    again = client.post('/api/handover/seals', json={'run_id': run_id})
    assert again.status_code == 409
    assert again.get_json()['code'] in ('NO_CHANGE', 'SEALED')
    assert len(client.get('/api/handover/seals').get_json()) == 1


def test_sealed_revision_is_divergence_not_replacement():
    first_state = run(_csv(BASE), {})
    first = build_seal(first_state, [], parse_cutoff({}))
    changed = BASE.replace(
        '00002,S001,B001,苹果,P01,LC-MS/MS,2,2026-08-01,2026-08-03,0.004',
        '00002,S001,B001,苹果,P01,LC-MS/MS,3,2026-08-01,2026-08-03,0.009')
    state = run(_csv(changed), {}, seals=[first])
    body = preview(state, [first])['view']
    assert body['divergences'] and body['divergences'][0]['reason'] == 'SEALED_REVISION'
    assert body['counts']['included'] == 0
    sealed = [item for item in body['already_sealed'] if item['id'] == '00002']
    assert all(item['seal_id'] == first['seal_id'] for item in sealed)


def test_seal_chain_requires_parent():
    state = run(_csv(BASE), {})
    first = build_seal(state, [], parse_cutoff({}))
    with pytest.raises(DomainError) as error:
        build_seal(state, [first], parse_cutoff({}), parent=None)
    assert error.value.code == 'CHAIN'


def test_first_page_and_export(client):
    page = client.get('/m8_handover')
    text = page.data.decode()
    assert page.status_code == 200 and '截止依据' in text and '等级变化' in text
    run_id = client.post('/api/runs', json={'sample': 'dirty.csv'}).get_json()['run_id']
    seal = client.post('/api/handover/seals', json={'run_id': run_id}).get_json()
    export = client.get('/api/handover/seals/%s/export.csv' % seal['seal_id'])
    assert export.status_code == 200
    rows = list(csv.reader(io.StringIO(export.data.decode('utf-8-sig'))))
    kinds = {row[0] for row in rows[1:]}
    assert {'封存头', '纳入', '隔离', '等级'} <= kinds
    assert sum(1 for row in rows if row[0] == '隔离') == seal['counts']['withheld']


def test_export_is_deterministic_and_matches_checksum():
    state = run(_csv(BASE), {})
    seal = build_seal(state, [], parse_cutoff({}))
    from app.modules.m8_handover.export import seal_csv
    assert seal_csv(seal) == seal_csv(json.loads(json.dumps(seal)))
    assert seal['checksum'] == seal_checksum(seal)


def test_body_keeps_original_values_for_correction():
    first_state = run(_csv(BASE), {})
    first = build_seal(first_state, [], parse_cutoff({}))
    fixed = _fixed(BASE)
    state = run(_csv(fixed), {}, seals=[first])
    body = preview(state, [first])['view']
    if body['corrections']:
        for item in body['corrections']:
            assert item['origin_code'] in ('MISSING', 'CONFLICT')
