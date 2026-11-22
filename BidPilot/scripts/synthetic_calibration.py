#!/usr/bin/env python3
"""Generate synthetic completed jobs and score the estimator through the real backtest.

This proves the calibration pipeline end-to-end with invented data. It is NOT
evidence of real-world accuracy: actuals are simulated around the quoted plan
with seeded noise, and predictions use only history completed before each quote.

Usage: python3 scripts/synthetic_calibration.py [--out report.json] [--seed 20261121]
"""
import argparse
import copy
import json
import random
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import core
import db
import estimating
from scripts.backtest_estimates import run

CUTOFF = '2026-06-30'
RATE_VERSION = 'studio-syn-v1'
COST_CENTS, SELL_CENTS = 4500, 9000

# group, scope overrides, quoted dates (training <= cutoff < held-out)
FAMILIES = [
    ('edit-only', {'shoot_days': 0, 'locations': 0, 'graphics': 'basic_titles',
                   'editing_level': 'standard'},
     ['2026-01-12', '2026-03-02', '2026-07-06', '2026-09-14']),
    ('one-shoot-day', {'shoot_days': 1, 'locations': 1, 'graphics': 'basic_titles',
                       'editing_level': 'standard'},
     ['2026-02-09', '2026-04-20', '2026-08-03', '2026-10-12']),
    ('graphics-heavy', {'shoot_days': 1, 'locations': 1, 'graphics': 'light_motion',
                        'editing_level': 'heavy'},
     ['2026-05-11', '2026-06-15', '2026-07-27', '2026-11-02']),
]


def build_job(rng, group, scope, as_of, index):
    deliverables = rng.choice([2, 3]) if group != 'graphics-heavy' else 2
    captions = deliverables if rng.random() < 0.5 else 0
    offer = {'client': f'Synthetic {group} {index}', 'format': 'product_promo',
             'production_mode': 'edit_only' if scope['shoot_days'] == 0 else 'one_shoot_day',
             'scripting': 'light', 'deliverables': deliverables,
             'revision_rounds': 2, 'caption_versions': captions, 'interviewees': 0,
             'deadline': (date.fromisoformat(as_of) + timedelta(days=28)).isoformat(),
             'budget_sgd': 20000, 'direct_costs_sgd': 0, **scope}
    shoot = scope['shoot_days'] > 0
    master_hours = 6 if scope['editing_level'] == 'heavy' else 4
    lines = [('brief', 1, 3), ('select', 1, 2), ('masters', deliverables, master_hours), ('review', 1, 3)]
    if shoot:
        lines.insert(1, ('shoot', 1, 5))
    if captions:
        lines += [('caption-design', 1, 1), ('captions', captions, 0.5)]
    if scope['graphics'] == 'light_motion':
        lines.append(('motion', deliverables, 2))
    items = [{'id': name, 'role': 'producer' if name in ('brief', 'shoot') else 'editor',
              'basis': 'synthetic fixture 2026-11-21', 'quantity': q, 'unit_hours': h,
              'cost_rate_cents': COST_CENTS, 'sell_rate_cents': SELL_CENTS}
             for name, q, h in lines]
    expenses = [{'cost_cents': 12000, 'sell_cents': 12000, 'description': 'rental',
                 'provisional': True, 'supplier': 'studio'}] if shoot else []
    offer['direct_costs_sgd'] = sum(e['cost_cents'] for e in expenses) / 100
    spec = {'rate_version': RATE_VERSION, 'buffer_pct': 20, 'floor_rate_cents': 7000,
            'items': items, 'expenses': expenses}
    plan = estimating.detailed_estimate(spec)
    buffered_minutes = int(plan['planning_hours'] * 60)
    start = as_of + 'T09:00'
    end = offer['deadline'] + 'T17:00'
    tasks = [{'id': 'prep', 'minutes': 180, 'resources': ['producer'],
              'release_at': start, 'deadline': end}]
    deps = ['prep']
    if shoot:
        tasks.append({'id': 'filming', 'kind': 'shoot', 'minutes': 300,
                      'resources': ['producer', 'location'], 'depends_on': ['prep'],
                      'release_at': start, 'deadline': end})
        deps = ['filming']
    edit_minutes = buffered_minutes - 180 - (300 if shoot else 0)
    tasks.append({'id': 'edit', 'minutes': max(60, int(edit_minutes * 0.7)),
                  'resources': ['producer'], 'depends_on': deps,
                  'release_at': start, 'deadline': end})
    tasks.append({'id': 'finish', 'minutes': max(60, edit_minutes - int(edit_minutes * 0.7)),
                  'resources': ['producer'], 'depends_on': ['edit'],
                  'release_at': start, 'deadline': end})
    spec['tasks'] = tasks
    # Simulated actuals: seeded noise around the quoted plan, never used as input.
    factor = rng.uniform(0.85, 1.30)
    actual_hours = round(plan['planning_hours'] * factor, 2)
    return {'offer': offer, 'spec': spec, 'as_of': as_of, 'group': group,
            'plan': plan, 'actual_hours': actual_hours,
            'end': (date.fromisoformat(as_of) + timedelta(days=rng.randint(20, 35))).isoformat()}


def generate(seed=20261121):
    rng = random.Random(seed)
    conn = sqlite3.connect(':memory:')
    conn.row_factory = sqlite3.Row
    conn.executescript(db.SCHEMA + estimating.SCHEMA)
    columns = ['project_id', 'client', 'format', 'production_mode', 'shoot_days', 'locations',
               'scripting', 'deliverables', 'editing_level', 'graphics', 'revision_rounds',
               'caption_versions', 'est_hours', 'actual_hours', 'price_sgd', 'start', 'end']
    seed_data = core.seed_data()
    for row in seed_data['past_projects']:
        conn.execute('INSERT INTO seed_past_projects VALUES (' + ','.join('?' for _ in columns) + ')',
                     [row[k] for k in columns])
    estimating.save_rate_version(conn, RATE_VERSION, [
        {'role': 'producer', 'cost_rate_cents': COST_CENTS, 'sell_rate_cents': SELL_CENTS},
        {'role': 'editor', 'cost_rate_cents': COST_CENTS, 'sell_rate_cents': SELL_CENTS}])
    jobs = []
    index = 0
    for group, scope, dates in FAMILIES:
        for as_of in dates:
            index += 1
            jobs.append(build_job(rng, group, scope, as_of, index))
    jobs.sort(key=lambda j: j['as_of'])
    rows = []
    for number, job in enumerate(jobs, 1):
        as_of, offer, spec = job['as_of'], job['offer'], job['spec']
        # Record earlier synthetic completions so the history model can train on
        # them; only jobs finished strictly before this quote are visible.
        for prior in jobs:
            if prior is job or prior['end'] >= as_of or prior.get('recorded'):
                continue
            estimating.add_past_project(conn, {
                'client': prior['offer']['client'], 'format': offer['format'],
                'production_mode': prior['offer']['production_mode'],
                'shoot_days': prior['offer']['shoot_days'], 'locations': prior['offer']['locations'],
                'scripting': 'light', 'deliverables': prior['offer']['deliverables'],
                'editing_level': prior['offer']['editing_level'], 'graphics': prior['offer']['graphics'],
                'revision_rounds': 2, 'caption_versions': prior['offer']['caption_versions'],
                'actual_hours': prior['actual_hours'],
                'price_sgd': round(prior['plan']['planning_hours'] * SELL_CENTS / 100, 2),
                'end': prior['end'], 'project_brief': 'Synthetic calibration fixture'})
            prior['recorded'] = True
        base = {'as_of': as_of, 'offer': offer, 'comparable_ids': []}
        rate = estimating.evaluate(conn, dict(base, historical_pricing=False), copy.deepcopy(seed_data))
        history_rows = [r for r in estimating.get_history(conn)
                        if r.get('end') and r['end'] < as_of]
        hist = estimating.evaluate(conn, dict(copy.deepcopy(base), pricing_mode='historical_replay'), copy.deepcopy(seed_data),
                                   history_override=history_rows)
        if rate.get('estimate') is None or hist.get('estimate') is None:
            raise ValueError('synthetic job produced no estimate: ' + offer['client'])
        detailed = estimating.detailed_estimate(spec)
        rows.append({'project_id': 'syn-' + str(number), 'campaign_id': 'syn-' + str(number),
                     'quoted_at': as_of, 'actual_hours': job['actual_hours'], 'group': job['group'],
                     'predictions': {'ratecard': rate['estimate']['planning_hours'],
                                     'history': hist['estimate']['planning_hours'],
                                     'detailed': detailed['planning_hours']},
                     'actual_cost_cents': round(job['actual_hours'] * COST_CENTS) + sum(
                         e['cost_cents'] for e in spec['expenses']),
                     'predicted_cost_cents': {'detailed': detailed['production_cost_cents']}})
    conn.close()
    report = run(rows, CUTOFF)
    report['synthetic'] = True
    report['seed'] = seed
    report['disclaimer'] = ('Simulated actuals around quoted plans; demonstrates pipeline '
                            'consistency only. Not evidence of real-world accuracy.')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', help='write the JSON report to this path')
    parser.add_argument('--seed', type=int, default=20261121)
    args = parser.parse_args()
    report = generate(args.seed)
    text = json.dumps(report, indent=2)
    if args.out:
        Path(args.out).write_text(text + '\n')
    print(text)


if __name__ == '__main__':
    main()