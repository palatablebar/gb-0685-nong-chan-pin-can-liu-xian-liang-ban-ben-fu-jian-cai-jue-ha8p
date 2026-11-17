from flask import Blueprint, Response, request
from app.web import api, page
from app import store
from app.errors import DomainError
from app.modules.m8_handover.engine import build_seal, parse_cutoff
from app.modules.m8_handover.export import seal_csv
bp=Blueprint('m8_handover',__name__)

@bp.get('/m8_handover')
def screen():return page(8,'m8_handover.html')

@bp.route('/api/m8_handover',methods=['GET','POST'])
def preview():return api(8)

@bp.post('/api/handover/seals')
def seal():
    data=request.get_json(silent=True) or {}
    if not isinstance(data,dict):raise DomainError('PAYLOAD','请求必须是对象')
    run_id=data.get('run_id')
    if not isinstance(run_id,str):raise DomainError('RUN','请指定交接批次')
    state=store.get(run_id)
    cutoff=parse_cutoff(state.get('parameters') or {})
    chain=store.seals()
    parent=data.get('parent') or (chain[-1]['seal_id'] if chain else None)
    body=build_seal(state,chain,cutoff,parent=parent,note=data.get('note',''),
                    origins=data.get('origins'))
    return store.save_seal(body)

@bp.get('/api/handover/seals')
def seals():return store.seals()

@bp.get('/api/handover/seals/<seal_id>')
def seal_one(seal_id):return store.seal(seal_id)

@bp.get('/api/handover/seals/<seal_id>/export.csv')
def seal_export(seal_id):
    body=store.seal(seal_id)
    return Response(seal_csv(body),mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition':'attachment; filename=%s.csv' % seal_id})
