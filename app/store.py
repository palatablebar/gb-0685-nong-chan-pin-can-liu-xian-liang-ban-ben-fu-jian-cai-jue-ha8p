import sqlite3,json,os
from pathlib import Path
from app.errors import DomainError

def connection():
    path=Path(os.environ.get('DB_PATH','data/records.db'));path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=10);db.row_factory=sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, body TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, decision TEXT NOT NULL DEFAULT "待复核")')
    db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
    # 封存件只增不改：同一 seal_id 再次写入会因主键冲突而失败，从而保证已交件原样。
    db.execute('CREATE TABLE IF NOT EXISTS seals (id TEXT PRIMARY KEY, parent TEXT, body TEXT NOT NULL, created_at TEXT NOT NULL)')
    return db

def save(run_id, body):
    with connection() as db:
        db.execute('INSERT OR IGNORE INTO runs(id,body) VALUES (?,?)',(run_id,json.dumps(body,ensure_ascii=False)))
        db.execute("INSERT INTO meta(key,value) VALUES ('latest',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(run_id,))
    return get(run_id)

def get(run_id):
    with connection() as db:r=db.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
    if not r:raise DomainError('NOT_FOUND','批次不存在',404)
    body=json.loads(r['body']);body.update(run_id=r['id'],review_version=r['version'],decision=r['decision'])
    # 交接视图按当前封存链刷新；运行快照本身不被改写。
    if 'handover' in body:
        from app.modules.m8_handover.engine import attach_ledger
        body=attach_ledger(body,seals())
    return body

def history():
    with connection() as db:rows=db.execute("SELECT id,version,decision FROM runs ORDER BY CASE WHEN id=(SELECT value FROM meta WHERE key='latest') THEN 0 ELSE 1 END,rowid DESC LIMIT 100").fetchall()
    return [dict(r) for r in rows]

def review(run_id, version, decision):
    if decision not in ('通过','退回'):raise DomainError('DECISION','复核状态无效')
    with connection() as db:
        cur=db.execute('UPDATE runs SET version=version+1,decision=? WHERE id=? AND version=?',(decision,run_id,version))
        if cur.rowcount!=1:
            if not db.execute('SELECT 1 FROM runs WHERE id=?',(run_id,)).fetchone():raise DomainError('NOT_FOUND','批次不存在',404)
            raise DomainError('STALE','复核版本已更新，请刷新',409)
    return get(run_id)

def save_seal(body):
    """封存写入只能成功一次；同号重写返回冲突，绝不覆盖既有封存。"""
    with connection() as db:
        try:
            db.execute('INSERT INTO seals(id,parent,body,created_at) VALUES (?,?,?,?)',
                (body['seal_id'],body.get('parent',''),json.dumps(body,ensure_ascii=False,sort_keys=True),body['created_at']))
        except sqlite3.IntegrityError:
            raise DomainError('SEALED','该封存号已存在，已交件保持原样',409)
    return seal(body['seal_id'])

def seal(seal_id):
    with connection() as db:r=db.execute('SELECT body FROM seals WHERE id=?',(seal_id,)).fetchone()
    if not r:raise DomainError('NOT_FOUND','封存件不存在',404)
    return json.loads(r['body'])

def seals():
    with connection() as db:rows=db.execute('SELECT body FROM seals ORDER BY created_at,rowid').fetchall()
    return [json.loads(r['body']) for r in rows]
