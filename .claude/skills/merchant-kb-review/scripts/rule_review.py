"""规则复核引擎：从 AI 已复核结论中提炼的高精度规则，自动处理待复核行。

规则只做可从文本本身确定的机械修正（支付前缀变体、交易噪声变体、名称重复短语），
语义判断（类目、他商关键词、待核实）仍交给 AI。每个规则命中行写回 state.sqlite，
review_source='RULE'，与 AI 结果同格式，便于统一导出。

子命令：
  analyze  分析已完成的 AI 结果，输出各规则的命中数与与 AI 的一致率（持续沉淀规则用）
  apply    对 pending 行应用规则并写回结果
  stats    查看规则复核覆盖率与规则命中分布
"""
import argparse, json, os, re, sqlite3, sys
from collections import Counter, defaultdict
from datetime import datetime

DEFAULT_RUN = 'C:/Users/zhangyuliang02/AppData/Local/Temp/kb_llm_review/full'


def now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


def split_kws(v):
    return [x.strip() for x in (v or '').split('|') if x.strip()]


def norm_kw(k):
    return re.sub(r'\s+', ' ', (k or '').strip().upper())


def connect(run_dir):
    con = sqlite3.connect(os.path.join(run_dir, 'state.sqlite'), timeout=60)
    con.execute('PRAGMA journal_mode=WAL')
    con.execute('PRAGMA synchronous=NORMAL')
    con.row_factory = sqlite3.Row
    return con


def migrate(con):
    cols = {r[1] for r in con.execute('PRAGMA table_info(rows)')}
    for name, ddl in (
        ('review_source', "ALTER TABLE rows ADD COLUMN review_source TEXT NOT NULL DEFAULT ''"),
        ('review_rule', "ALTER TABLE rows ADD COLUMN review_rule TEXT NOT NULL DEFAULT ''"),
    ):
        if name not in cols:
            con.execute(ddl)
    # 历史 AI 结果补标记
    con.execute("UPDATE rows SET review_source='AI' WHERE ai_status='done' AND review_source=''")
    con.commit()


# ---------------- 规则定义 ----------------

# 经验证的高精度支付终端前缀（ZLR/EZYPAY 100%，SQ 89%，SMP 79% 与 AI 一致；
# SP/LS/EZI 精确率过低，不启用）
PAYMENT_PREFIXES = {'SQ', 'SMP', 'ZLR', 'EZYPAY'}

# 交易噪声片段（AI 一致率 >= 77%）；裸 OF、CURRENCY、APPLE PAY 等一致率低，不启用
NOISE_PATTERNS = [
    r'CASH OUT(?:\s+PURCHASE)?',
    r'EFTPOS(?:\s+(?:REFUND|PURCHASE|WITHDRAWAL))?',
    r'BPAY',
    r'TRANSFER DEBIT',
    r'REPAYMENT OF',
    r'DIRECT DEBIT',
    r'WITHDRAWAL',
    r'PURCHASE',
    r'PAYPAL',
    r'VISA',
    r'POS(?:\s+HOLD)?',
    r'RETURN',
    r'REFUND',
    r'AU AUD',
    r'\bX{3,}(?:\s+XX)?(?:\s+\d{1,2})?',
]
NOISE_RX = re.compile('|'.join(f'(?:{p})' for p in NOISE_PATTERNS))

# 名称尾部相邻重复短语，如 "SMIGGLE PERTH AIRPORT PERTH AIRPORT"
NAME_REPEAT_RX = re.compile(r"^(.+?)\b([A-Za-z0-9&'\-]+(?:\s+[A-Za-z0-9&'\-]+)*)\s+\2$")


def _strip_noise(k):
    out = NOISE_RX.sub(' ', k)
    return re.sub(r'\s+', ' ', out).strip(' -,')


def rule_keywords(kws, name=''):
    """返回 (新关键词列表, 触发的规则列表)。

    删除条件：被删词去掉支付前缀/交易噪声后的剩余词，必须与同列表另一保留词完全相同。
    只有「噪声/前缀之外完全相同」的变体才删，避免降低银行流水匹配覆盖。
    删除后做不动点校验：若等价的保留词自身也被删，则恢复，避免整行删空。
    硬性不变量：与商户名完全相同的关键词永不删除（防止 MEA/XXXX/POS 等短词误伤本名）。
    """
    norms = [norm_kw(k) for k in kws]
    name_norm = norm_kw(name)
    cand = {}   # i -> 去掉前缀/噪声后的剩余词（归一化字符串）
    fired = {}
    for i, k in enumerate(kws):
        if name_norm and norms[i] == name_norm:
            continue
        t = k.split()
        if not t:
            continue
        if t[0] in PAYMENT_PREFIXES and len(t) > 1:
            rest = norm_kw(' '.join(t[1:]))
            if rest and any(j != i and norms[j] == rest for j in range(len(kws))):
                cand[i] = rest
                fired[i] = 'R001_payment_prefix'
                continue
        cleaned = _strip_noise(k)
        cn = norm_kw(cleaned)
        if cn and cn != norms[i]:
            if any(j != i and norms[j] == cn for j in range(len(kws))):
                cand[i] = cn
                fired[i] = 'R002_noise_variant'

    keep = [True] * len(kws)
    for i in cand:
        keep[i] = False
    changed = True
    while changed:
        changed = False
        for i, rest in cand.items():
            if keep[i]:
                continue
            if not any(keep[j] and j != i and norms[j] == rest for j in range(len(kws))):
                keep[i] = True
                changed = True

    if all(keep):
        return kws, []
    rules = sorted({fired[i] for i in cand if not keep[i]})
    return [k for i, k in enumerate(kws) if keep[i]], rules


def rule_name(name):
    m = NAME_REPEAT_RX.match(name.strip())
    if not m:
        return name, []
    base = (m.group(1).rstrip() + ' ' + m.group(2)).strip()
    if not base or len(base) < 3:
        return name, []
    return base, ['R003_name_repeat']


RULE_DESC = {
    'R001_payment_prefix': '删除支付终端前缀变体（SQ/SMP/ZLR/EZYPAY），保留等价干净词',
    'R002_noise_variant': '删除交易噪声变体（CASH OUT/EFTPOS/BPAY/TRANSFER DEBIT 等），保留等价干净词',
    'R003_name_repeat': '合并商户名尾部相邻重复短语',
}


def apply_rules(name, kws):
    new_kws, kw_rules = rule_keywords(kws, name)
    new_name, name_rules = rule_name(name)
    rules = kw_rules + name_rules
    if not rules:
        return None
    issues = []
    if kw_rules:
        issues.append('关键词')
    if name_rules:
        issues.append('名称')
    conf = '高' if kw_rules and not name_rules else '中'
    reason_bits = [RULE_DESC[r] for r in rules]
    reason = '；'.join(reason_bits)[:100]
    return {
        'verdict': '建议修改',
        'new_name': new_name if name_rules else '',
        'new_category': '',
        'new_keywords': new_kws if kw_rules else [],
        'issues': issues,
        'reason': reason,
        'confidence': conf,
        'rules': rules,
    }


# ---------------- analyze ----------------

def load_done(con, source='AI', limit=0):
    q = "SELECT idx,name,cat,kws,ai_verdict,ai_new_name,ai_new_kw FROM rows WHERE ai_status='done'"
    args = []
    if source:
        q += ' AND review_source=?'
        args.append(source)
    q += ' ORDER BY idx'
    if limit:
        q += ' LIMIT ?'
        args.append(limit)
    return con.execute(q, args).fetchall()


def cmd_analyze(args):
    con = connect(DEFAULT_RUN if not args.run_dir else args.run_dir)
    migrate(con)
    rows = load_done(con, args.source, args.limit)
    if not rows:
        print('没有可分析的已完成行')
        return 2

    per_rule = defaultdict(Counter)
    name_cases = Counter()
    prefix_cases = defaultdict(Counter)
    total_deleted = 0
    rows_fire = 0
    for r in rows:
        old = split_kws(r['kws'])
        ai_new = split_kws(r['ai_new_kw'])
        ai_set = set(ai_new) if ai_new else set(old)
        if not old:
            continue
        new_kws, rules = rule_keywords(old, r['name'])
        deleted = [k for k in old if k not in set(new_kws)]
        if deleted:
            rows_fire += 1
            total_deleted += len(deleted)
        for rid in rules:
            per_rule[rid]['rows'] += 1
            per_rule[rid]['deleted'] += len(deleted)
            per_rule[rid]['agree'] += sum(1 for k in deleted if k in (set(old) - ai_set))
        for k in deleted:
            toks = k.split()
            if toks and toks[0] in PAYMENT_PREFIXES:
                prefix_cases[toks[0]]['deleted'] += 1
                if k in (set(old) - ai_set):
                    prefix_cases[toks[0]]['agree'] += 1
        new_name, nrules = rule_name(r['name'])
        if nrules:
            name_cases['hit'] += 1
            if (r['ai_new_name'] or '').strip() and (r['ai_new_name'] or '').strip() != r['name']:
                name_cases['ai_renamed'] += 1

    out = {
        'analyzed_rows': len(rows),
        'source': args.source or 'ALL',
        'rule_fire_rows': rows_fire,
        'rule_fire_rate': round(rows_fire * 100.0 / len(rows), 2),
        'deleted_keywords': total_deleted,
        'rules': [],
        'prefix_detail': {},
        'name_rule': dict(name_cases),
    }
    for rid, c in sorted(per_rule.items()):
        out['rules'].append({
            'rule': rid,
            'description': RULE_DESC.get(rid, ''),
            'rows': c['rows'],
            'deleted': c['deleted'],
            'ai_agree': c['agree'],
            'agreement_rate': round(c['agree'] * 100.0 / c['deleted'], 1) if c['deleted'] else 0,
        })
    for p, c in prefix_cases.items():
        out['prefix_detail'][p] = {
            'deleted': c['deleted'],
            'ai_agree': c['agree'],
            'agreement_rate': round(c['agree'] * 100.0 / c['deleted'], 1) if c['deleted'] else 0,
        }
    path = os.path.join(args.run_dir or DEFAULT_RUN, 'rule_analysis.json')
    with open(path, 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, indent=1)
    out['saved'] = path
    print(json.dumps(out, ensure_ascii=False, indent=1))
    con.close()
    return 0


# ---------------- apply ----------------

def cmd_apply(args):
    run_dir = args.run_dir or DEFAULT_RUN
    con = connect(run_dir)
    migrate(con)
    q = "SELECT idx,name,cat,kws FROM rows WHERE ai_status='pending' ORDER BY idx"
    params = []
    if args.limit:
        q += ' LIMIT ?'
        params.append(args.limit)
    rows = con.execute(q, params).fetchall()
    if not rows:
        print(json.dumps({'scanned': 0, 'note': '没有 pending 行'}, ensure_ascii=False))
        return 0

    ts = datetime.now().strftime('%Y%m%d_%H%M%S')
    audit_dir = os.path.join(run_dir, 'rule_results')
    os.makedirs(audit_dir, exist_ok=True)
    audit_path = os.path.join(audit_dir, f'rule_{ts}.jsonl')

    changes = []
    clean_rows = []
    rule_counter = Counter()
    for r in rows:
        res = apply_rules(r['name'], split_kws(r['kws']))
        if res:
            changes.append((r, res))
            rule_counter.update(res['rules'])
        else:
            clean_rows.append(r)

    if args.dry_run:
        preview = {
            'scanned': len(rows),
            'rule_changes': len(changes),
            'clean_rows': len(clean_rows),
            'rule_hits': dict(rule_counter),
            'sample': [
                {'idx': r['idx'], 'name': r['name'],
                 'new_name': res['new_name'], 'new_keywords': res['new_keywords'][:6],
                 'reason': res['reason']}
                for r, res in changes[:10]
            ],
            'dry_run': True,
        }
        print(json.dumps(preview, ensure_ascii=False, indent=1))
        con.close()
        return 0

    written = 0
    with open(audit_path, 'w', encoding='utf-8') as f:
        for r, res in changes:
            rec = {
                'idx': r['idx'], 'verdict': res['verdict'], 'new_name': res['new_name'],
                'new_category': res['new_category'],
                'new_keywords': res['new_keywords'] if res['new_keywords'] else [],
                'issues': res['issues'], 'reason': res['reason'],
                'confidence': res['confidence'], 'review_source': 'RULE',
                'rule': ','.join(res['rules']),
            }
            f.write(json.dumps(rec, ensure_ascii=False) + '\n')
            con.execute('''UPDATE rows SET ai_status='done',ai_verdict=?,ai_confidence=?,
                ai_issues=?,ai_reason=?,ai_new_name=?,ai_new_kw=?,ai_new_cat='',
                ai_apply_level=?,review_source='RULE',review_rule=?,reviewed_at=?
                WHERE idx=?''',
                (res['verdict'], res['confidence'], '/'.join(res['issues']),
                 res['reason'], res['new_name'], '|'.join(res['new_keywords']),
                 '可直接应用' if res['confidence'] == '高' else '需人工确认',
                 ','.join(res['rules']), now(), r['idx']))
            written += 1
        if args.include_clean:
            for r in clean_rows:
                rec = {
                    'idx': r['idx'], 'verdict': '无需更改', 'new_name': '',
                    'new_category': '', 'new_keywords': [], 'issues': [],
                    'reason': '', 'confidence': '中', 'review_source': 'RULE',
                    'rule': 'R000_clean_gate',
                }
                f.write(json.dumps(rec, ensure_ascii=False) + '\n')
                con.execute('''UPDATE rows SET ai_status='done',ai_verdict='无需更改',
                    ai_confidence='中',ai_issues='',ai_reason='',ai_new_name='',
                    ai_new_kw='',ai_new_cat='',ai_apply_level='',
                    review_source='RULE',review_rule='R000_clean_gate',reviewed_at=?
                    WHERE idx=?''', (now(), r['idx']))
                written += 1
                rule_counter['R000_clean_gate'] += 1
    con.commit()
    status = dict(con.execute("SELECT ai_status,COUNT(*) FROM rows GROUP BY ai_status").fetchall())
    out = {
        'scanned': len(rows),
        'rule_changes': len(changes),
        'clean_marked': len(clean_rows) if args.include_clean else 0,
        'written': written,
        'rule_hits': dict(rule_counter),
        'audit_file': audit_path,
        'done': status.get('done', 0),
        'pending': status.get('pending', 0),
    }
    print(json.dumps(out, ensure_ascii=False, indent=1))
    con.close()
    return 0


# ---------------- stats ----------------

def cmd_stats(args):
    run_dir = args.run_dir or DEFAULT_RUN
    con = connect(run_dir)
    migrate(con)
    total = con.execute('SELECT COUNT(*) FROM rows').fetchone()[0]
    status = dict(con.execute('SELECT ai_status,COUNT(*) FROM rows GROUP BY ai_status').fetchall())
    src = dict(con.execute("SELECT review_source,COUNT(*) FROM rows WHERE ai_status='done' GROUP BY review_source").fetchall())
    verdict = dict(con.execute("SELECT ai_verdict,COUNT(*) FROM rows WHERE ai_status='done' AND review_source='RULE' GROUP BY ai_verdict").fetchall())
    rules = dict(con.execute(
        "SELECT review_rule,COUNT(*) FROM rows WHERE review_source='RULE' GROUP BY review_rule ORDER BY 2 DESC").fetchall())
    done = status.get('done', 0)
    out = {
        'total': total,
        'done': done,
        'done_by_source': {'AI': src.get('AI', 0), 'RULE': src.get('RULE', 0)},
        'rule_verdicts': verdict,
        'rule_hits': rules,
        'pending': status.get('pending', 0),
        'queued': status.get('queued', 0),
        'rule_coverage_of_done': round(src.get('RULE', 0) * 100.0 / done, 2) if done else 0,
    }
    print(json.dumps(out, ensure_ascii=False, indent=1))
    con.close()
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--run-dir', default=DEFAULT_RUN)
    sub = ap.add_subparsers(dest='cmd', required=True)

    p = sub.add_parser('analyze', help='用已完成的 AI 结果评估规则命中与一致率')
    p.add_argument('--source', default='AI')
    p.add_argument('--limit', type=int, default=0)
    p.set_defaults(func=cmd_analyze)

    p = sub.add_parser('apply', help='对 pending 行应用规则并写回结果')
    p.add_argument('--limit', type=int, default=0)
    p.add_argument('--dry-run', action='store_true')
    p.add_argument('--include-clean', action='store_true',
                   help='规则未命中且语法干净的行标记为“无需更改”（默认留给 AI）')
    p.set_defaults(func=cmd_apply)

    p = sub.add_parser('stats', help='查看规则复核覆盖率')
    p.set_defaults(func=cmd_stats)

    args = ap.parse_args()
    return args.func(args)


if __name__ == '__main__':
    sys.exit(main())
