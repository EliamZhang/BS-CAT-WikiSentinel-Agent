import argparse, csv, json, os, re, sys, collections
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


def split_kws(s):
    return [x.strip() for x in (s or '').split('|') if x.strip()]


def norm_verdict(v):
    v = (v or '').strip()
    mapping = {
        '无需更改': '无需更改', '保留': '无需更改', '无需修改': '无需更改', '通过': '无需更改',
        '建议修改': '建议修改', '修改': '建议修改', '需修改': '建议修改',
        '待核实': '待核实', '不确定': '待核实', '人工确认': '待核实',
        '建议删除行': '建议删除行', '删除': '建议删除行', '建议删除': '建议删除行',
    }
    return mapping.get(v, v)


def as_list(v):
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str):
        if '|' in v:
            return split_kws(v)
        return [v.strip()] if v.strip() else []
    return []


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--kb', required=True)
    ap.add_argument('--batches', required=True)
    ap.add_argument('--results', required=True)
    ap.add_argument('--out', required=True)
    ap.add_argument('--summary', default='')
    args = ap.parse_args()

    expected = {}
    for fn in sorted(os.listdir(args.batches)):
        if not fn.endswith('.jsonl'):
            continue
        with open(os.path.join(args.batches, fn), encoding='utf-8') as f:
            for line in f:
                if not line.strip():
                    continue
                rec = json.loads(line)
                expected[rec['idx']] = rec

    results = {}
    duplicates = []
    invalid = []
    for fn in sorted(os.listdir(args.results)):
        if not fn.endswith('.jsonl'):
            continue
        path = os.path.join(args.results, fn)
        with open(path, encoding='utf-8') as f:
            for lineno, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    rec = json.loads(line)
                except Exception as e:
                    invalid.append({'file': fn, 'line': lineno, 'error': f'JSON错误: {e}'})
                    continue
                idx = rec.get('idx')
                if idx not in expected:
                    invalid.append({'file': fn, 'line': lineno, 'error': f'idx {idx} 不在样本中'})
                    continue
                if idx in results:
                    duplicates.append(idx)
                    continue
                verdict = norm_verdict(rec.get('verdict'))
                if verdict not in VERDICTS:
                    invalid.append({'file': fn, 'line': lineno, 'idx': idx,
                                    'error': f'非法结论 {rec.get("verdict")!r}'})
                    verdict = '待核实'
                new_kws = as_list(rec.get('new_keywords'))
                new_cat = (rec.get('new_category') or '').strip()
                if new_cat and new_cat not in CATEGORIES:
                    invalid.append({'file': fn, 'line': lineno, 'idx': idx,
                                    'error': f'非法类目 {new_cat!r}'})
                    new_cat = ''
                rule = expected[idx].get('rule')
                name = expected[idx]['merchant_name']
                cat = expected[idx]['category']
                supplied_cat = new_cat
                if new_cat == cat:
                    new_cat = ''
                orig_kws = expected[idx]['keywords']
                new_name = (rec.get('new_name') or '').strip()
                if new_name == name:
                    new_name = ''
                actual_change = bool(new_name or new_cat or (new_kws and new_kws != orig_kws))
                if verdict == '建议修改' and not actual_change:
                    if supplied_cat and supplied_cat == cat:
                        verdict = '无需更改'
                    else:
                        invalid.append({'file': fn, 'line': lineno, 'idx': idx,
                                        'error': '建议修改但无实际改动'})
                        verdict = '待核实'
                if verdict != '建议修改':
                    if verdict == '无需更改':
                        new_kws = []
                    elif verdict == '待核实':
                        new_kws = new_kws if actual_change else []
                results[idx] = {
                    'idx': idx,
                    'verdict': verdict,
                    'new_name': new_name,
                    'new_category': new_cat,
                    'new_keywords': new_kws,
                    'issues': as_list(rec.get('issues')),
                    'reason': (rec.get('reason') or '').strip(),
                    'confidence': (rec.get('confidence') or '').strip(),
                    'rule': rule,
                    'original_name': name,
                    'original_category': cat,
                    'original_keywords': orig_kws,
                }

    missing = [i for i in expected if i not in results]

    # load original rows for expected idx only
    wanted = set(expected)
    kb = {}
    with open(args.kb, encoding='utf-8-sig', newline='') as f:
        for idx, r in enumerate(csv.DictReader(f)):
            if idx in wanted:
                kb[idx] = ((r.get('merchant_name') or '').strip(),
                           split_kws(r.get('keywords')),
                           (r.get('category') or '').strip())
                if len(kb) == len(wanted):
                    break

    summary = {
        'expected': len(expected),
        'received': len(results),
        'missing': len(missing),
        'duplicates': len(duplicates),
        'invalid': invalid,
        'verdict': collections.Counter(),
        'confidence': collections.Counter(),
        'issues': collections.Counter(),
        'changed_rows': 0,
        'name_changes': 0,
        'keyword_changes': 0,
        'category_changes': 0,
        'rule_flagged': 0,
        'rule_flagged_llm_no_change': 0,
        'llm_new_issues': 0,
        'both_no_change': 0,
        'both_need_attention': 0,
        'apply_level': collections.Counter(),
    }

    os.makedirs(os.path.dirname(args.out) or '.', exist_ok=True)
    wb = xlsxwriter.Workbook(args.out, {'constant_memory': True,
                                        'strings_to_urls': False,
                                        'strings_to_formulas': False})
    ws = wb.add_worksheet('LLM复核')
    headers = ['idx', '商户名', '原关键词', '原类目',
               '规则状态', '规则问题类型', '规则说明',
               'LLM结论', 'LLM置信度', 'LLM问题类型', 'LLM复核说明',
               '更改后商户名', '更改后的关键词配置', '更改后的类别',
               '删除关键词数', '新增关键词数', '应用级别', '与规则一致性']
    widths = [10, 34, 60, 18, 10, 30, 40, 10, 8, 16, 40, 30, 60, 18, 10, 10, 14, 22]
    for c, (h, w) in enumerate(zip(headers, widths)):
        ws.write(0, c, h)
        ws.set_column(c, c, w)
    ws.freeze_panes(1, 0)

    row = 1
    for idx in sorted(results, key=lambda i: i):
        r = results[idx]
        name, kws, cat = kb.get(idx, (r['original_name'], r['original_keywords'], r['original_category']))
        rule = r['rule']
        rule_need = rule.get('need', '') if rule else '否（未标记）'
        rule_type = rule.get('issue_type', '') if rule else ''
        rule_reason = rule.get('reason', '') if rule else ''
        new_kws = r['new_keywords']
        removed = [k for k in kws if k not in new_kws] if new_kws else []
        added = [k for k in new_kws if k not in kws] if new_kws else []

        rule_flag = bool(rule and rule.get('need') in ('是', '待核实'))
        llm_action = r['verdict'] in ('建议修改', '建议删除行', '待核实')
        if rule_flag and llm_action:
            agreement = '一致-均需处理'
        elif rule_flag and not llm_action:
            agreement = '规则标记但LLM认为无需'
        elif not rule_flag and llm_action:
            agreement = 'LLM新增问题'
        else:
            agreement = '一致-均无需处理'

        if r['verdict'] == '建议修改':
            rule_cat = (rule or {}).get('new_category', '') if rule else ''
            if r['new_category']:
                apply_level = ('规则一致-可应用'
                               if rule_cat and rule_cat == r['new_category'] and r['confidence'] == '高'
                               else '需人工确认')
            elif r['confidence'] == '高':
                apply_level = '可直接应用'
            else:
                apply_level = '需人工确认'
        elif r['verdict'] in ('待核实', '建议删除行'):
            apply_level = '需人工确认'
        else:
            apply_level = ''
        summary['apply_level'][apply_level or '无需处理'] += 1

        summary['verdict'][r['verdict']] += 1
        summary['confidence'][r['confidence'] or '未填'] += 1
        for x in r['issues']:
            summary['issues'][x] += 1
        if r['verdict'] == '建议修改':
            summary['changed_rows'] += 1
        if r['new_name']:
            summary['name_changes'] += 1
        if new_kws and new_kws != kws:
            summary['keyword_changes'] += 1
        if r['new_category']:
            summary['category_changes'] += 1
        if rule_flag:
            summary['rule_flagged'] += 1
        if rule_flag and not llm_action:
            summary['rule_flagged_llm_no_change'] += 1
        if not rule_flag and llm_action:
            summary['llm_new_issues'] += 1
        if not rule_flag and not llm_action:
            summary['both_no_change'] += 1
        if rule_flag and llm_action:
            summary['both_need_attention'] += 1

        vals = [
            idx, name, '|'.join(kws), cat,
            rule_need, rule_type, rule_reason,
            r['verdict'], r['confidence'], '/'.join(r['issues']), r['reason'],
            r['new_name'], '|'.join(new_kws), r['new_category'],
            len(removed), len(added), apply_level, agreement,
        ]
        for c, v in enumerate(vals):
            ws.write(row, c, v)
        row += 1

    ws2 = wb.add_worksheet('说明')
    ws2.set_column(0, 0, 24)
    ws2.set_column(1, 1, 100)
    ws2.write(0, 0, '项目')
    ws2.write(0, 1, '值')
    notes = [
        ('样本行数', len(expected)),
        ('收到复核结果', len(results)),
        ('缺失/重复/非法', f'{len(missing)}/{len(duplicates)}/{len(invalid)}'),
        ('LLM结论', '，'.join(f'{k} {v}' for k, v in summary['verdict'].most_common())),
        ('置信度', '，'.join(f'{k} {v}' for k, v in summary['confidence'].most_common())),
        ('问题类型', '，'.join(f'{k} {v}' for k, v in summary['issues'].most_common())),
        ('更改统计', f'商户名 {summary["name_changes"]}，关键词 {summary["keyword_changes"]}，类目建议 {summary["category_changes"]}'),
        ('与规则一致性', f'均需处理 {summary["both_need_attention"]}，仅规则标记 {summary["rule_flagged_llm_no_change"]}，仅LLM新增 {summary["llm_new_issues"]}，均无需处理 {summary["both_no_change"]}'),
        ('应用级别', '，'.join(f'{k} {v}' for k, v in summary['apply_level'].most_common())),
        ('级别说明', '可直接应用=高置信且非类目变更；规则一致-可应用=类目变更且与规则一致、高置信；需人工确认=类目变更、待核实、建议删除行或中低置信'),
        ('使用提醒', '本表为抽样复核，不代表全库结论；类目变更建议需人工确认后再应用。'),
    ]
    for i, (k, v) in enumerate(notes, start=1):
        ws2.write(i, 0, k)
        ws2.write(i, 1, v)

    for k in ('verdict', 'confidence', 'issues', 'apply_level'):
        summary[k] = dict(summary[k])
    summary['missing_idx'] = missing[:100]
    summary['duplicate_idx'] = duplicates[:100]

    wb.close()
    out_summary = args.summary or os.path.splitext(args.out)[0] + '_summary.json'
    with open(out_summary, 'w', encoding='utf-8') as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    print('wrote', args.out, out_summary)


if __name__ == '__main__':
    main()
