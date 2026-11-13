import argparse, csv, json, os, random, re, sqlite3, sys, collections

csv.field_size_limit(10**9)

CATEGORIES = [
    'Home Improvement', 'Dining Out', 'Health', 'Groceries', 'Transport',
    'Financial Institutions', 'Retail', 'Automotive', 'Personal Care',
    'Information', 'Education', 'Entertainment', 'Travel',
    'Gyms and other memberships', 'Rent', 'Pet Care', 'Donations',
    'Insurance', 'Utilities', 'Telecommunications', 'Subscription TV',
    'Department Stores',
]


def split_kws(s):
    return [x.strip() for x in (s or '').split('|') if x.strip()]


def allocate(total, sizes, min_each=1, cap=None):
    """Proportional allocation with largest remainder."""
    keys = list(sizes)
    denom = sum(sizes.values())
    if denom <= 0:
        return {k: 0 for k in keys}
    quotas = {k: total * sizes[k] / denom for k in keys}
    alloc = {k: int(quotas[k]) for k in keys}
    for k in keys:
        alloc[k] = max(alloc[k], min(min_each, sizes[k]))
        if cap is not None:
            alloc[k] = min(alloc[k], cap)
    # shrink if over
    while sum(alloc.values()) > total:
        k = max((k for k in keys if alloc[k] > 0), key=lambda x: alloc[x])
        alloc[k] -= 1
    # grow by largest remainder
    remainders = sorted(keys, key=lambda k: (quotas[k] - int(quotas[k])), reverse=True)
    i = 0
    while sum(alloc.values()) < total:
        k = remainders[i % len(remainders)]
        if alloc[k] < sizes[k] and (cap is None or alloc[k] < cap):
            alloc[k] += 1
        i += 1
        if i > total * 10 + len(keys) * 10:
            break
    return alloc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--kb', required=True)
    ap.add_argument('--db', default='')
    ap.add_argument('--issues', default='')
    ap.add_argument('--out', required=True)
    ap.add_argument('--n', type=int, default=1000)
    ap.add_argument('--batch-size', type=int, default=50)
    ap.add_argument('--seed', type=int, default=42)
    ap.add_argument('--max-keywords', type=int, default=50)
    ap.add_argument('--max-chars', type=int, default=12000)
    ap.add_argument('--flagged-ratio', type=float, default=0.5)
    args = ap.parse_args()

    random.seed(args.seed)
    issues = {}
    if args.issues:
        with open(args.issues, encoding='utf-8') as f:
            issues = {x['idx']: x for x in json.load(f)['issues']}

    baseline = {}
    if args.db:
        con = sqlite3.connect(args.db)
        baseline = {i: (need, nk, nc, reason)
                    for i, need, nk, nc, reason in
                    con.execute('select idx,need,new_kw,new_cat,reason from audit')}
        con.close()

    rows = {}
    with open(args.kb, encoding='utf-8-sig', newline='') as f:
        for idx, r in enumerate(csv.DictReader(f)):
            name = (r.get('merchant_name') or '').strip()
            cat = (r.get('category') or '').strip()
            kws = split_kws(r.get('keywords'))
            chars = len(name) + sum(len(k) for k in kws)
            rows[idx] = (name, cat, kws, chars)

    def rule_for(idx):
        x = issues.get(idx)
        name, cat, kws, _ = rows[idx]
        if x:
            nk = split_kws(x.get('new_kw')) if x.get('new_kw') else []
            return {
                'need': x.get('action'),
                'new_name': x.get('new_name') if x.get('new_name') != name else '',
                'new_category': x.get('new_cat') if x.get('new_cat') != cat else '',
                'new_keywords': nk[:80],
                'reason': x.get('reason', ''),
                'issue_type': x.get('issue_type', ''),
            }
        b = baseline.get(idx)
        if b:
            need, nk, nc, reason = b
            if need != '否':
                return {
                    'need': need,
                    'new_name': '',
                    'new_category': nc if nc and nc != cat else '',
                    'new_keywords': split_kws(nk)[:80] if need == '是' else [],
                    'reason': reason or '',
                    'issue_type': '',
                }
        return None

    excluded = 0
    flagged, unflagged = [], []
    for idx, (name, cat, kws, chars) in rows.items():
        if not name or not kws or len(kws) > args.max_keywords or chars > args.max_chars:
            excluded += 1
            continue
        if rule_for(idx):
            flagged.append(idx)
        else:
            unflagged.append(idx)

    n_flag = min(len(flagged), int(round(args.n * args.flagged_ratio)))
    n_unflag = min(len(unflagged), args.n - n_flag)

    def stratified(pool, keyfunc, n, cap=80):
        groups = collections.defaultdict(list)
        for i in pool:
            groups[keyfunc(i)].append(i)
        for g in groups.values():
            random.shuffle(g)
        sizes = {k: len(v) for k, v in groups.items()}
        alloc = allocate(n, sizes, min_each=min(5, n), cap=cap)
        out = []
        for k, v in groups.items():
            out.extend(v[:alloc.get(k, 0)])
        return out

    def issue_key(i):
        x = issues.get(i)
        if x:
            return (x.get('issue_type') or '').split('+')[0]
        b = baseline.get(i)
        return (b[3] or '基础核查').split('；')[0][:40] if b else '其他'

    def cat_key(i):
        return rows[i][1]

    sample_flag = stratified(flagged, issue_key, n_flag)
    sample_unflag = stratified(unflagged, cat_key, n_unflag)

    seen = set(sample_flag)
    sample = sample_flag[:]
    for i in sample_unflag:
        if i not in seen:
            sample.append(i)
            seen.add(i)
    # fill if short
    for pool in (flagged, unflagged):
        for i in pool:
            if len(sample) >= args.n:
                break
            if i not in seen:
                sample.append(i)
                seen.add(i)
        if len(sample) >= args.n:
            break
    random.shuffle(sample)

    batch_dir = os.path.join(args.out, 'batches')
    os.makedirs(batch_dir, exist_ok=True)
    for f in os.listdir(batch_dir):
        if f.endswith('.jsonl'):
            os.remove(os.path.join(batch_dir, f))

    batches = []
    for bi in range(0, len(sample), args.batch_size):
        chunk = sample[bi:bi + args.batch_size]
        path = os.path.join(batch_dir, f'batch_{bi // args.batch_size:03d}.jsonl')
        with open(path, 'w', encoding='utf-8') as f:
            for idx in chunk:
                name, cat, kws, chars = rows[idx]
                rec = {
                    'idx': idx,
                    'merchant_name': name,
                    'category': cat,
                    'keywords': kws,
                    'keyword_count': len(kws),
                    'truncated': False,
                    'rule': rule_for(idx),
                }
                f.write(json.dumps(rec, ensure_ascii=False) + '\n')
        batches.append({'file': os.path.basename(path), 'rows': len(chunk),
                        'first_idx': chunk[0], 'last_idx': chunk[-1]})

    manifest = {
        'kb': args.kb,
        'seed': args.seed,
        'requested': args.n,
        'sampled': len(sample),
        'flagged_pool': len(flagged),
        'unflagged_pool': len(unflagged),
        'sampled_flagged': len(sample_flag),
        'sampled_unflagged': len(sample_unflag),
        'excluded_huge_or_empty': excluded,
        'batch_size': args.batch_size,
        'batches': batches,
        'categories': CATEGORIES,
    }
    with open(os.path.join(args.out, 'manifest.json'), 'w', encoding='utf-8') as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    print(json.dumps(manifest, ensure_ascii=False, indent=1))


if __name__ == '__main__':
    main()
