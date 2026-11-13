"""全量、可中断续跑的商户知识库 AI 复核。

状态存在 run_dir/state.sqlite，行级状态 pending/queued/done。
Excel 只是导出结果，随时中断都不影响续跑。
"""
import argparse, csv, json, os, re, sqlite3, sys, time
from datetime import datetime

import openpyxl
import xlsxwriter

csv.field_size_limit(10**9)

CATEGORIES = {
    'Home Improvement', 'Dining Out', 'Health', 'Groceries', 'Transport',
    'Financial Institutions', 'Retail', 'Automotive', 'Personal Care',
    'Information', 'Education', 'Entertainment', 'Travel',
    'Gyms and other memberships', 'Rent', 'Pet Care', 'Donations',
    'Insurance', 'Utilities', 'Telecommunications', 'Subscription TV',
    'Department Stores',
}
VERDICTS = ('无需更改', '建议修改', '待核实', '建议删除行')
FEATURE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_RUN = os.path.join(FEATURE_DIR, 'work', 'full')
DEFAULT_RULE_WB = os.path.join(FEATURE_DIR, 'data', 'merchant_kb_全量复核_漏检与调整.xlsx')
DEFAULT_OUT = os.path.join(FEATURE_DIR, 'data', 'merchant_kb_reviewed_AI.xlsx')


def now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def s(v):
    return '' if v is None else str(v)


def split_kws(v):
    return [x.strip() for x in s(v).split('|') if x.strip()]


def norm_verdict(v):
    v = s(v).strip()
    return {
        '无需更改': '无需更改', '保留': '无需更改', '无需修改': '无需更改', '通过': '无需更改',
        '建议修改': '建议修改', '修改': '建议修改', '需修改': '建议修改',
        '待核实': '待核实', '不确定': '待核实', '人工确认': '待核实',
        '建议删除行': '建议删除行', '删除': '建议删除行', '建议删除': '建议删除行',
    }.get(v, v)


def as_list(v):
    if isinstance(v, list):
        return [s(x).strip() for x in v if s(x).strip()]
    if isinstance(v, str):
        if '|' in v:
            return split_kws(v)
        return [v.strip()] if v.strip() else []
    return []


def connect(run_dir):
    con = sqlite3.connect(os.path.join(run_dir, 'state.sqlite'), timeout=60)
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA synchronous=NORMAL')
    con.row_factory = sqlite3.Row
    return con


def ensure_dirs(run_dir):
    for d in (run_dir, os.path.join(run_dir, 'batches'), os.path.join(run_dir, 'results')):
        os.makedirs(d, exist_ok=True)


def require_state(run_dir):
    p = os.path.join(run_dir, 'state.sqlite')
    if not os.path.exists(p):
        print(f'状态库不存在：{p}\n请先执行 init')
        return False
    return True


def create_schema(con):
    con.executescript('''
    CREATE TABLE IF NOT EXISTS rows(
        idx INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        cat TEXT NOT NULL,
        kws TEXT NOT NULL,
        rule_summary TEXT,
        rule_new_kw TEXT,
        rule_new_cat TEXT,
        rule_reason TEXT,
        rule_need TEXT,
        rule_conclusion TEXT,
        rule_note TEXT,
        rule_new_name TEXT,
        rule_orig_status TEXT,
        ai_status TEXT NOT NULL DEFAULT 'pending',
        ai_verdict TEXT,
        ai_confidence TEXT,
        ai_issues TEXT,
        ai_reason TEXT,
        ai_new_name TEXT,
        ai_new_kw TEXT,
        ai_new_cat TEXT,
        ai_apply_level TEXT,
        batch_id INTEGER,
        reviewed_at TEXT,
        review_source TEXT NOT NULL DEFAULT '',
        review_rule TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS batches(
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file TEXT UNIQUE,
        status TEXT NOT NULL,
        total INTEGER NOT NULL,
        done INTEGER NOT NULL DEFAULT 0,
        created_at TEXT,
        completed_at TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_rows_ai_status ON rows(ai_status);
    CREATE INDEX IF NOT EXISTS idx_rows_batch ON rows(batch_id);
    ''')
    migrate_schema(con)
    con.commit()


def migrate_schema(con):
    cols = {r[1] for r in con.execute('PRAGMA table_info(rows)')}
    for name, ddl in (
        ('review_source', "ALTER TABLE rows ADD COLUMN review_source TEXT NOT NULL DEFAULT ''"),
        ('review_rule', "ALTER TABLE rows ADD COLUMN review_rule TEXT NOT NULL DEFAULT ''"),
    ):
        if name not in cols:
            con.execute(ddl)
    con.execute("UPDATE rows SET review_source='AI' WHERE ai_status='done' AND review_source=''")


def rule_payload(row):
    need = s(row['rule_need'])
    if need not in ('是', '待核实'):
        return None
    return {
        'need': need,
        'new_name': s(row['rule_new_name']),
        'new_category': s(row['rule_new_cat']),
        'new_keywords': split_kws(row['rule_new_kw']),
        'reason': s(row['rule_reason']) or s(row['rule_note']),
        'issue_type': s(row['rule_conclusion']),
    }


def cmd_init(args):
    run_dir = args.run_dir
    ensure_dirs(run_dir)
    db = os.path.join(run_dir, 'state.sqlite')
    if os.path.exists(db):
        if not args.reset:
            print(f'状态库已存在：{db}\n如需重来，加 --reset（会删除该 run-dir 下的状态和批次）')
            return 2
        for name in ('state.sqlite', 'state.sqlite-wal', 'state.sqlite-shm',
                     'progress.json', 'errors.jsonl', 'manifest.json'):
            p = os.path.join(run_dir, name)
            if os.path.exists(p):
                os.remove(p)
        for d in ('batches', 'results'):
            p = os.path.join(run_dir, d)
            if os.path.isdir(p):
                for f in os.listdir(p):
                    if f.endswith('.jsonl'):
                        os.remove(os.path.join(p, f))
    con = connect(run_dir)
    create_schema(con)

    wb = None
    ws = None
    rule_wb = args.rule_workbook
    if not rule_wb and os.path.exists(DEFAULT_RULE_WB):
        rule_wb = DEFAULT_RULE_WB
        print(f'自动使用规则工作簿：{rule_wb}', file=sys.stderr)
    if rule_wb:
        rp = rule_wb
        if not os.path.exists(rp):
            print(f'找不到规则工作簿：{rp}')
            return 2
        wb = openpyxl.load_workbook(rp, read_only=True, data_only=True)
        ws = wb['全量核查']
        xl_iter = ws.iter_rows(min_row=2, values_only=True)
    else:
        xl_iter = None

    sql = '''INSERT INTO rows(
        idx,name,cat,kws,rule_summary,rule_new_kw,rule_new_cat,rule_reason,
        rule_need,rule_conclusion,rule_note,rule_new_name,rule_orig_status
    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)'''
    buf = []
    total = 0
    t0 = time.time()
    with open(args.kb, encoding='utf-8-sig', newline='') as f:
        reader = csv.DictReader(f)
        for idx, r in enumerate(reader):
            if args.limit and idx >= args.limit:
                break
            xl = next(xl_iter, None) if xl_iter is not None else None
            if xl_iter is not None:
                if xl is None:
                    raise SystemExit(f'规则工作簿行数不足，CSV 第 {idx} 行没有对应行')
                if int(xl[12]) != idx:
                    raise SystemExit(f'idx 不一致：CSV {idx}，规则工作簿 {xl[12]}')
                rule = [s(xl[i]) for i in (3, 4, 5, 6, 7, 8, 9, 10, 11)]
            else:
                rule = [''] * 9
            buf.append((
                idx,
                s(r.get('merchant_name')).strip(),
                s(r.get('category')).strip(),
                s(r.get('keywords')).strip(),
                *rule,
            ))
            total += 1
            if len(buf) >= 5000:
                con.executemany(sql, buf)
                con.commit()
                buf = []
                print(f'  已导入 {total:,} 行', file=sys.stderr)
    if buf:
        con.executemany(sql, buf)
        con.commit()
    if wb is not None:
        wb.close()

    manifest = {
        'kb': os.path.abspath(args.kb),
        'kb_size': os.path.getsize(args.kb),
        'kb_mtime': datetime.fromtimestamp(os.path.getmtime(args.kb)).strftime('%Y-%m-%d %H:%M:%S'),
        'rule_workbook': os.path.abspath(rule_wb) if rule_wb else '',
        'rows': total,
        'limit': args.limit,
        'created_at': now(),
        'batch_size_default': 100,
    }
    with open(os.path.join(run_dir, 'manifest.json'), 'w', encoding='utf-8') as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    con.close()
    print(json.dumps({'run_dir': run_dir, 'rows': total,
                      'rule_workbook': manifest['rule_workbook'],
                      'elapsed_sec': round(time.time() - t0, 1)},
                     ensure_ascii=False, indent=1))
    return 0


def open_batches(con):
    return con.execute("SELECT id,file,total,done FROM batches WHERE status='open' ORDER BY id").fetchall()


def cmd_next(args):
    run_dir = args.run_dir
    if args.batch_size <= 0:
        print('--batch-size 必须大于 0')
        return 2
    if not require_state(run_dir):
        return 2
    ensure_dirs(run_dir)
    con = connect(run_dir)
    create_schema(con)

    # 缺失文件的 open 批次先退回 pending，避免子代理永远读不到
    for b in open_batches(con):
        path = os.path.join(run_dir, 'batches', b['file'])
        if not os.path.exists(path):
            con.execute("UPDATE rows SET ai_status='pending',batch_id=NULL WHERE batch_id=?", (b['id'],))
            con.execute("UPDATE batches SET status='partial',completed_at=? WHERE id=?", (now(), b['id']))
    con.commit()

    existing = open_batches(con)
    open_rows = sum(b['total'] - b['done'] for b in existing)
    need = max(0, args.n - open_rows) if args.n else 0

    created = []
    if need > 0:
        rows = con.execute(
            "SELECT idx,name,cat,kws,rule_need,rule_new_name,rule_new_cat,"
            "rule_new_kw,rule_reason,rule_note,rule_conclusion "
            "FROM rows WHERE ai_status='pending' ORDER BY idx LIMIT ?", (need,)
        ).fetchall()
        for i in range(0, len(rows), args.batch_size):
            chunk = rows[i:i + args.batch_size]
            cur = con.execute(
                "INSERT INTO batches(file,status,total,done,created_at) VALUES(?,?,?,?,?)",
                ('', 'open', len(chunk), 0, now()))
            bid = cur.lastrowid
            fn = f'batch_{bid:05d}.jsonl'
            path = os.path.join(run_dir, 'batches', fn)
            with open(path, 'w', encoding='utf-8') as f:
                for r in chunk:
                    rec = {
                        'idx': r['idx'],
                        'merchant_name': r['name'],
                        'category': r['cat'],
                        'keywords': split_kws(r['kws']),
                        'keyword_count': len(split_kws(r['kws'])),
                        'rule': rule_payload(r),
                    }
                    f.write(json.dumps(rec, ensure_ascii=False) + '\n')
            con.execute("UPDATE batches SET file=? WHERE id=?", (fn, bid))
            con.executemany(
                "UPDATE rows SET ai_status='queued',batch_id=? WHERE idx=?",
                [(bid, r['idx']) for r in chunk])
            con.commit()
            created.append({'id': bid, 'file': path, 'rows': len(chunk)})

    all_open = open_batches(con)
    status = dict(con.execute("SELECT ai_status,COUNT(*) FROM rows GROUP BY ai_status").fetchall())
    pending_idx = con.execute("SELECT MIN(idx) FROM rows WHERE ai_status='pending'").fetchone()[0]
    done_idx = con.execute("SELECT MAX(idx) FROM rows WHERE ai_status='done'").fetchone()[0]
    con.close()

    out = {
        'open_batches': [{'id': b['id'],
                          'file': os.path.join(run_dir, 'batches', b['file']),
                          'total': b['total'], 'done': b['done']} for b in all_open],
        'created': created,
        'done': status.get('done', 0),
        'queued': status.get('queued', 0),
        'pending': status.get('pending', 0),
        'total': sum(status.values()),
        'last_done_idx': done_idx,
        'next_pending_idx': pending_idx,
        'note': '把 open_batches 分配给子代理；结果写入 run_dir/results/同名文件。',
    }
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


def normalize(rec, row):
    errs = []
    idx = rec.get('idx')
    verdict = norm_verdict(rec.get('verdict'))
    if verdict not in VERDICTS:
        errs.append(f'非法结论 {rec.get("verdict")!r}')
        verdict = '待核实'
    new_cat = s(rec.get('new_category')).strip()
    supplied_cat = new_cat
    if new_cat and new_cat not in CATEGORIES:
        errs.append(f'非法类目 {new_cat!r}')
        new_cat = ''
    if new_cat == s(row['cat']):
        new_cat = ''
    new_name = s(rec.get('new_name')).strip()
    if new_name == s(row['name']):
        new_name = ''
    new_kws = as_list(rec.get('new_keywords'))
    if new_kws == split_kws(row['kws']):
        new_kws = []
    actual = bool(new_name or new_cat or (new_kws and new_kws != split_kws(row['kws'])))
    if verdict == '建议修改' and not actual:
        if supplied_cat and supplied_cat == s(row['cat']):
            verdict = '无需更改'
        else:
            errs.append('建议修改但无实际改动')
            verdict = '待核实'
    if verdict == '无需更改':
        new_name, new_cat, new_kws = '', '', []
    elif verdict == '建议删除行':
        new_name, new_cat, new_kws = '', '', []
    elif verdict == '待核实' and not actual:
        new_name, new_cat, new_kws = '', '', []
    reason = s(rec.get('reason')).strip()[:200]
    if verdict == '待核实' and not reason:
        errs.append('待核实无说明')
        reason = '待核实（模型未给出说明）'
    if verdict == '无需更改':
        reason = ''
    conf = s(rec.get('confidence')).strip()
    if conf not in ('高', '中', '低'):
        errs.append(f'非法置信度 {conf!r}')
        conf = ''
    issues = '/'.join(as_list(rec.get('issues')))
    if verdict == '建议修改':
        if new_cat:
            apply_level = ('规则一致-可应用'
                           if s(row['rule_new_cat']) == new_cat and conf == '高'
                           else '需人工确认')
        elif conf == '高':
            apply_level = '可直接应用'
        else:
            apply_level = '需人工确认'
    elif verdict in ('待核实', '建议删除行'):
        apply_level = '需人工确认'
    else:
        apply_level = ''
    return {
        'idx': idx,
        'ai_verdict': verdict,
        'ai_confidence': conf,
        'ai_issues': issues,
        'ai_reason': reason,
        'ai_new_name': new_name,
        'ai_new_kw': '|'.join(new_kws),
        'ai_new_cat': new_cat,
        'ai_apply_level': apply_level,
    }, errs


def cmd_ingest(args):
    run_dir = args.run_dir
    if not require_state(run_dir):
        return 2
    con = connect(run_dir)
    create_schema(con)
    results_dir = args.results or os.path.join(run_dir, 'results')
    if not os.path.isdir(results_dir):
        print(f'结果目录不存在：{results_dir}')
        return 2
    files = sorted(f for f in os.listdir(results_dir) if re.fullmatch(r'batch_\d+\.jsonl', f))
    errors = []
    ingested = 0
    done_batches = 0
    partial_batches = 0
    for fn in files:
        m = re.fullmatch(r'batch_(\d+)\.jsonl', fn)
        bid = int(m.group(1))
        b = con.execute("SELECT * FROM batches WHERE id=?", (bid,)).fetchone()
        if not b:
            errors.append({'file': fn, 'error': '状态库中无此批次'})
            continue
        if b['status'] == 'done' and not args.reingest:
            continue
        rows = con.execute(
            "SELECT idx,name,cat,kws,rule_new_cat FROM rows WHERE batch_id=?", (bid,)
        ).fetchall()
        expected = {r['idx']: r for r in rows}
        if not expected:
            # 批次中的行已全部退回并重新分配，旧结果文件不再处理
            con.execute("UPDATE batches SET status='done',completed_at=? WHERE id=?", (now(), bid))
            con.commit()
            continue
        got = {}
        with open(os.path.join(results_dir, fn), encoding='utf-8-sig') as f:
            for lineno, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except Exception as e:
                    errors.append({'file': fn, 'line': lineno, 'error': f'JSON错误: {e}'})
                    continue
                idx = rec.get('idx')
                try:
                    idx = int(idx)
                except (TypeError, ValueError):
                    errors.append({'file': fn, 'line': lineno, 'error': f'非法 idx {idx!r}'})
                    continue
                if idx not in expected:
                    errors.append({'file': fn, 'line': lineno, 'idx': idx, 'error': 'idx 不在本批次'})
                    continue
                if idx in got:
                    errors.append({'file': fn, 'line': lineno, 'idx': idx, 'error': '重复 idx'})
                    continue
                got[idx] = rec
        for idx, rec in got.items():
            out, errs = normalize(rec, expected[idx])
            for e in errs:
                errors.append({'file': fn, 'idx': idx, 'error': e})
            con.execute('''UPDATE rows SET
                ai_status='done',ai_verdict=?,ai_confidence=?,ai_issues=?,ai_reason=?,
                ai_new_name=?,ai_new_kw=?,ai_new_cat=?,ai_apply_level=?,reviewed_at=?,
                review_source='AI',review_rule=''
                WHERE idx=?''',
                (out['ai_verdict'], out['ai_confidence'], out['ai_issues'],
                 out['ai_reason'], out['ai_new_name'], out['ai_new_kw'],
                 out['ai_new_cat'], out['ai_apply_level'], now(), idx))
            ingested += 1
        missing = [i for i in expected if i not in got]
        status = 'done' if not missing else 'partial'
        con.execute("UPDATE batches SET status=?,done=?,completed_at=? WHERE id=?",
                    (status, len(got), now(), bid))
        if missing:
            con.executemany(
                "UPDATE rows SET ai_status='pending',batch_id=NULL WHERE idx=?",
                [(i,) for i in missing])
            partial_batches += 1
        else:
            done_batches += 1
        con.commit()
    ep = args.errors or os.path.join(run_dir, 'errors.jsonl')
    with open(ep, 'w', encoding='utf-8') as f:
        for e in errors:
            f.write(json.dumps(e, ensure_ascii=False) + '\n')
    statuses = dict(con.execute("SELECT ai_status,COUNT(*) FROM rows GROUP BY ai_status").fetchall())
    con.close()
    out = {
        'result_files': len(files),
        'rows_ingested': ingested,
        'batches_done': done_batches,
        'batches_partial': partial_batches,
        'errors': len(errors),
        'errors_file': ep,
        'done': statuses.get('done', 0),
        'queued': statuses.get('queued', 0),
        'pending': statuses.get('pending', 0),
        'total': sum(statuses.values()),
    }
    print(json.dumps(out, ensure_ascii=False, indent=1))
    return 0


def cmd_status(args):
    run_dir = args.run_dir
    if not require_state(run_dir):
        return 2
    con = connect(run_dir)
    migrate_schema(con)
    total = con.execute("SELECT COUNT(*) FROM rows").fetchone()[0]
    counts = dict(con.execute("SELECT ai_status,COUNT(*) FROM rows GROUP BY ai_status").fetchall())
    last_done = con.execute("SELECT MAX(idx) FROM rows WHERE ai_status='done'").fetchone()[0]
    next_pending = con.execute("SELECT MIN(idx) FROM rows WHERE ai_status='pending'").fetchone()[0]
    next_queued = con.execute("SELECT MIN(idx) FROM rows WHERE ai_status='queued'").fetchone()[0]
    open_b = [{'id': b['id'], 'file': os.path.join(run_dir, 'batches', b['file']),
               'total': b['total'], 'done': b['done']}
              for b in open_batches(con)]
    results_dir = os.path.join(run_dir, 'results')
    pending_results = 0
    if os.path.isdir(results_dir):
        for fn in os.listdir(results_dir):
            m = re.fullmatch(r'batch_(\d+)\.jsonl', fn)
            if not m:
                continue
            b = con.execute(
                "SELECT status,(SELECT COUNT(*) FROM rows WHERE batch_id=batches.id) AS n "
                "FROM batches WHERE id=?", (int(m.group(1)),)).fetchone()
            if b and b['status'] != 'done' and b['n'] > 0:
                pending_results += 1
    src = dict(con.execute(
        "SELECT review_source,COUNT(*) FROM rows WHERE ai_status='done' GROUP BY review_source"
    ).fetchall())
    con.close()
    done = counts.get('done', 0)
    if pending_results:
        hint = f'发现 {pending_results} 个未回收的结果文件，先运行 ingest；再运行 next 继续未完成批次。'
    elif open_b:
        hint = f'继续复核 {len(open_b)} 个 open 批次；已 done 的行不会重复分配。'
    elif next_pending is not None:
        hint = f'运行 next 生成从 idx {next_pending} 开始的新批次。'
    else:
        hint = '全部行已检测，可执行 export。'
    progress = {
        'total': total,
        'done': done,
        'done_ai': src.get('AI', 0),
        'done_rule': src.get('RULE', 0),
        'queued': counts.get('queued', 0),
        'pending': counts.get('pending', 0),
        'percent': round(done * 100.0 / total, 2) if total else 0,
        'last_done_idx': last_done,
        'next_pending_idx': next_pending,
        'next_queued_idx': next_queued,
        'open_batches': open_b,
        'pending_result_files': pending_results,
        'resume_hint': hint,
        'updated_at': now(),
    }
    with open(os.path.join(run_dir, 'progress.json'), 'w', encoding='utf-8') as f:
        json.dump(progress, f, ensure_ascii=False, indent=1)
    print(json.dumps(progress, ensure_ascii=False, indent=1))
    return 0


def ai_change_summary(r):
    verdict = s(r['ai_verdict'])
    if verdict == '建议删除行':
        return '建议删除该行'
    parts = []
    old_name, new_name = s(r['name']), s(r['ai_new_name'])
    if new_name and new_name != old_name:
        parts.append(f'【商户名】{old_name} → {new_name}')
    old_cat, new_cat = s(r['cat']), s(r['ai_new_cat'])
    if new_cat and new_cat != old_cat:
        parts.append(f'【类目】{old_cat} → {new_cat}')
    old_kws = split_kws(r['kws'])
    new_kws = split_kws(r['ai_new_kw'])
    if new_kws:
        old_set, new_set = set(old_kws), set(new_kws)
        removed = [k for k in old_kws if k not in new_set]
        added = [k for k in new_kws if k not in old_set]
        bits = []
        if removed:
            bits.append(f'删除 {len(removed)} 个（' + '、'.join(removed[:3]) + '）')
        if added:
            bits.append(f'新增 {len(added)} 个（' + '、'.join(added[:3]) + '）')
        if bits:
            parts.append('【关键词】' + '、'.join(bits))
    return '；'.join(parts)


def cmd_export(args):
    run_dir = args.run_dir
    if not require_state(run_dir):
        return 2
    con = connect(run_dir)
    migrate_schema(con)
    total = con.execute("SELECT COUNT(*) FROM rows").fetchone()[0]
    counts = dict(con.execute("SELECT ai_status,COUNT(*) FROM rows GROUP BY ai_status").fetchall())
    base_headers = [
        'merchant_name', 'keywords', 'category', '变更摘要（原 → 新）',
        '更改后的关键词配置', '更改后的类别', '更改原因', '是否需要更改',
        '复核结论', '复核说明', '复核建议商户名', '原核查状态', 'idx',
    ]
    base_widths = [32, 58, 18, 46, 58, 18, 42, 10, 10, 42, 30, 10, 10]
    if args.detail:
        headers = base_headers + [
            '是否已AI检测', 'AI复核结论', 'AI置信度', 'AI问题类型', 'AI复核说明',
            'AI建议商户名', 'AI更改后的关键词配置', 'AI更改后的类别', 'AI应用级别',
            'review_source',
        ]
        widths = base_widths + [12, 12, 8, 16, 42, 30, 58, 18, 16, 14]
    else:
        headers = base_headers + ['是否已AI检测', 'review_source']
        widths = base_widths + [12, 14]
    wb = xlsxwriter.Workbook(args.out, {'constant_memory': True,
                                        'strings_to_urls': False,
                                        'strings_to_formulas': False})
    ws = wb.add_worksheet('全量核查')
    for c, (h, w) in enumerate(zip(headers, widths)):
        ws.write(0, c, h)
        ws.set_column(c, c, w)
    ws.freeze_panes(1, 0)

    q = '''SELECT idx,name,cat,kws,rule_summary,rule_new_kw,rule_new_cat,rule_reason,
        rule_need,rule_conclusion,rule_note,rule_new_name,rule_orig_status,
        ai_status,ai_verdict,ai_confidence,ai_issues,ai_reason,ai_new_name,
        ai_new_kw,ai_new_cat,ai_apply_level,review_source FROM rows ORDER BY idx'''
    if args.limit:
        q += f' LIMIT {int(args.limit)}'
    clipped = 0
    row_no = 1
    t0 = time.time()
    cur = con.execute(q)
    while True:
        batch = cur.fetchmany(2000)
        if not batch:
            break
        for r in batch:
            done = r['ai_status'] == 'done'
            if args.detail:
                vals = [
                    s(r['name']), s(r['kws']), s(r['cat']),
                    s(r['rule_summary']), s(r['rule_new_kw']), s(r['rule_new_cat']),
                    s(r['rule_reason']), r['rule_need'], r['rule_conclusion'],
                    s(r['rule_note']), s(r['rule_new_name']), r['rule_orig_status'], r['idx'],
                    '是' if done else '否',
                    r['ai_verdict'] if done else '',
                    r['ai_confidence'] if done else '',
                    r['ai_issues'] if done else '',
                    s(r['ai_reason']) if done else '',
                    s(r['ai_new_name']) if done else '',
                    s(r['ai_new_kw']) if done else '',
                    r['ai_new_cat'] if done else '',
                    r['ai_apply_level'] if done else '',
                    s(r['review_source']) if done else '',
                ]
            elif done:
                verdict = s(r['ai_verdict'])
                reason = s(r['ai_reason'])
                conf = s(r['ai_confidence'])
                need = {'建议修改': '是', '建议删除行': '是',
                        '待核实': '待核实', '无需更改': '否'}.get(verdict, '')
                change_reason = reason if verdict in ('建议修改', '建议删除行') else ''
                note = f'[置信度{conf}] {reason}'.strip() if reason else ''
                vals = [
                    s(r['name']), s(r['kws']), s(r['cat']), ai_change_summary(r),
                    s(r['ai_new_kw']), s(r['ai_new_cat']), change_reason, need,
                    verdict, note, s(r['ai_new_name']), s(r['rule_orig_status']),
                    r['idx'], '是', s(r['review_source']),
                ]
            else:
                vals = [
                    s(r['name']), s(r['kws']), s(r['cat']),
                    s(r['rule_summary']), s(r['rule_new_kw']), s(r['rule_new_cat']),
                    s(r['rule_reason']), r['rule_need'], r['rule_conclusion'],
                    s(r['rule_note']), s(r['rule_new_name']), r['rule_orig_status'],
                    r['idx'], '否', '',
                ]
            for i, v in enumerate(vals):
                if isinstance(v, str) and len(v) > 32000:
                    vals[i] = v[:31920] + f'…[超长，完整内容见原CSV第{r["idx"] + 2}行]'
                    clipped += 1
            ws.write_row(row_no, 0, vals)
            row_no += 1
        if row_no % 50000 < 2000:
            print(f'  已写出 {row_no - 1:,} 行', file=sys.stderr)
    ws.autofilter(0, 0, row_no - 1, len(headers) - 1)
    next_pending = con.execute("SELECT MIN(idx) FROM rows WHERE ai_status='pending'").fetchone()[0]
    open_count = con.execute("SELECT COUNT(*) FROM batches WHERE status='open'").fetchone()[0]
    rule_count = con.execute("SELECT COUNT(*) FROM rows WHERE rule_need<>''").fetchone()[0]
    src = dict(con.execute(
        "SELECT review_source,COUNT(*) FROM rows WHERE ai_status='done' GROUP BY review_source"
    ).fetchall())
    con.close()

    ws2 = wb.add_worksheet('说明')
    ws2.set_column(0, 0, 26)
    ws2.set_column(1, 1, 100)
    done = counts.get('done', 0)
    notes = [
        ('项目', '值'),
        ('生成时间', now()),
        ('数据行数（本次导出）', f'{row_no - 1:,}'),
        ('状态库总行数', f'{total:,}'),
        ('已复核合计（状态库）', f'{done:,}'),
        ('　其中 AI 复核', f'{src.get("AI", 0):,}'),
        ('　其中规则复核', f'{src.get("RULE", 0):,}'),
        ('未检测（待分配）', f'{counts.get("pending", 0):,}'),
        ('已分配未回传', f'{counts.get("queued", 0):,}'),
        ('下一个待检测 idx', '无（全部已检测）' if next_pending is None else f'{next_pending:,}'),
        ('未完成批次', f'{open_count} 个（续跑用 status/next 查看）'),
        ('完成比例（状态库）', f'{done * 100.0 / total:.2f}%' if total else '0%'),
        ('状态库', os.path.join(run_dir, 'state.sqlite')),
        ('继续方式', '运行 full_run.py status 查看进度；next 生成待复核批次；ingest 回收结果。'),
        ('是否已AI检测', '是=该行已有复核结果（AI或规则）；否=尚未复核，可继续运行。'),
        ('review_source', 'AI=大模型逐行复核；RULE=固定规则自动复核。'),
    ]
    if args.detail:
        notes += [
            ('列说明', 'A-M 为原规则核查结果；N-V 为复核结果，W 为复核来源（AI/RULE）。'),
            ('AI复核结论', '无需更改 / 建议修改 / 待核实 / 建议删除行。'),
            ('AI应用级别', '可直接应用 / 规则一致-可应用 / 需人工确认；空=无需处理。'),
            ('注意', '复核结果不覆盖原规则结果；未检测行的 N-V 为空。'),
        ]
    else:
        fill_note = ('N=是 时 D-M 为复核结果；N=否 时 D-M 暂显示原规则结果，仅作占位。'
                     if rule_count else
                     'N=是 时 D-M 为复核结果；本次未导入规则结果，N=否 时 D-M 为空。')
        notes += [
            ('列说明', 'A-M 与原工作簿同格式；N 为是否已复核；O 为复核来源（AI/RULE）。'),
            ('D-M 填充', fill_note),
            ('复核结论/说明', 'I 列为复核结论；J 列以 [置信度X] 开头，仅给出理由时填写。'),
            ('注意', '判断某行是否有复核结论，以 N 列为准；来源看 O 列。'),
        ]
    notes.append(('截断单元格', str(clipped)))
    for i, (k, v) in enumerate(notes, 0):
        ws2.write(i, 0, k)
        ws2.write(i, 1, v)
    wb.close()
    print(json.dumps({'out': os.path.abspath(args.out), 'rows': row_no - 1,
                      'done': done, 'done_ai': src.get('AI', 0),
                      'done_rule': src.get('RULE', 0), 'clipped': clipped,
                      'elapsed_sec': round(time.time() - t0, 1)},
                     ensure_ascii=False, indent=1))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--run-dir', default=DEFAULT_RUN)
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('init')
    p.add_argument('--kb', required=True)
    p.add_argument('--rule-workbook', default='')
    p.add_argument('--limit', type=int, default=0)
    p.add_argument('--reset', action='store_true')
    p.set_defaults(func=cmd_init)

    p = sub.add_parser('next')
    p.add_argument('--n', type=int, default=2000)
    p.add_argument('--batch-size', type=int, default=100)
    p.set_defaults(func=cmd_next)

    p = sub.add_parser('ingest')
    p.add_argument('--results', default='')
    p.add_argument('--errors', default='')
    p.add_argument('--reingest', action='store_true')
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser('status')
    p.set_defaults(func=cmd_status)

    p = sub.add_parser('export')
    p.add_argument('--out', default=DEFAULT_OUT)
    p.add_argument('--limit', type=int, default=0)
    p.add_argument('--detail', action='store_true',
                   help='附加 O-V 的AI明细列；默认只输出 A-M + 是否已AI检测')
    p.set_defaults(func=cmd_export)

    args = ap.parse_args()
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
