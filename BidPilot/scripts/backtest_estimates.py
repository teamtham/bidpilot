#!/usr/bin/env python3
"""Score precomputed quote-time predictions on held-out completed jobs.

Input is a JSON array with project_id, campaign_id, quoted_at, actual_hours, group and
predictions: {ratecard, history, detailed}. Optional actual_cost_cents
and predicted_cost_cents by model are included only when supplied. Training and
prediction must be done before running; this script does not infer unseen actuals.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import estimating


def from_database(conn, cutoff):
    """Replay recorded quote inputs against later reliable, unchanged closeouts.

    Only jobs with a saved detailed specification can be compared across all
    three models. This does not infer missing task effort or reconstruct media.
    """
    import copy
    import core
    from datetime import date
    date.fromisoformat(cutoff)
    rows = []
    for row in conn.execute('SELECT c.*,o.evidence_json,o.as_of FROM closeouts c JOIN proposal_versions v ON v.id=c.version_id JOIN offers o ON o.id=v.offer_id ORDER BY o.as_of,c.id'):
        if not row['as_of'] or row['as_of'] <= cutoff:
            continue
        actual, scope, request = json.loads(row['actuals_json']), json.loads(row['scope_json']), json.loads(row['evidence_json'] or '{}')
        if not actual.get('reliable') or scope.get('change_orders') or not isinstance(request.get('detailed_spec'), dict):
            continue
        data = copy.deepcopy(core.seed_data())
        data['past_projects'] = [p for p in data['past_projects'] if p.get('end') and p['end'] <= cutoff and p['end'] < row['as_of']]
        training = [p for p in estimating.get_history(conn) if p.get('end') and p['end'] <= cutoff and p['end'] < row['as_of']]
        ids = {p['project_id'] for p in data['past_projects']}
        base = copy.deepcopy(request)
        base.pop('detailed_spec', None); base.pop('detailed_alternatives', None)
        base['comparable_ids'] = [p for p in base.get('comparable_ids', []) if p in ids]
        base['historical_pricing'] = False
        rate = estimating.evaluate(conn, base, data).get('estimate')
        base['historical_pricing'] = True
        history_result = estimating.evaluate(conn, base, data, history_override=training)
        history = history_result.get('estimate')
        if not rate or not history:
            continue
        detail = estimating.detailed_estimate(request['detailed_spec'])
        item = {'project_id':row['id'], 'quoted_at':row['as_of'], 'actual_hours':actual['actual_hours'],
                'group':request.get('offer', {}).get('production_mode', 'unclassified'),
                'predictions':{'ratecard':rate['planning_hours'], 'history':history['planning_hours'], 'detailed':detail['planning_hours']},
                'history_status':history_result.get('historical_pricing', {}).get('status')}
        if 'actual_labor_cost_sgd' in actual:
            item['actual_cost_cents'] = round((actual['actual_labor_cost_sgd'] + actual['actual_cost_sgd']) * 100)
            item['predicted_cost_cents'] = {'detailed':detail['production_cost_cents']}
        rows.append(item)
    return run(rows, cutoff)


def run(rows, cutoff):
    from datetime import date
    date.fromisoformat(cutoff)
    if not isinstance(rows, list):
        raise ValueError('input_must_be_array')
    held_out = []
    seen = set()
    campaign_splits = {}
    for row in rows:
        project = row['project_id']
        if not isinstance(project, str) or not project or project in seen:
            raise ValueError('project_ids_must_be_unique')
        seen.add(project)
        quoted = date.fromisoformat(row['quoted_at'])
        split = 'held_out' if quoted > date.fromisoformat(cutoff) else 'training'
        campaign = row.get('campaign_id', project)
        if not isinstance(campaign, str) or not campaign:
            raise ValueError('invalid_campaign_id')
        if campaign in campaign_splits and campaign_splits[campaign] != split:
            raise ValueError('campaign_crosses_cutoff:' + campaign)
        campaign_splits[campaign] = split
        if split == 'held_out':
            held_out.append(row)
    if not held_out:
        raise ValueError('no_held_out_projects')
    results = {}
    # Preserve the existing evidence-mode comparison for historical inputs.
    model_sets = (('ratecard', 'history', 'detailed'),
                  ('brief', 'detailed_brief', 'detailed_reference'))
    models = next((s for s in model_sets if all(m in held_out[0].get('predictions', {}) for m in s)), model_sets[0])
    for model in models:
        if any(model not in row.get('predictions', {}) for row in held_out):
            raise ValueError('missing_held_out_prediction:' + model)
        cases = [{'project_id': r['project_id'], 'group': r.get('group', 'unclassified'),
                  'actual_hours': r['actual_hours'], 'predicted_hours': r['predictions'][model],
                  'actual_cost_cents': r.get('actual_cost_cents'),
                  'predicted_cost_cents': r.get('predicted_cost_cents', {}).get(model)} for r in held_out]
        results[model] = estimating.backtest(cases)
    return {'training_cutoff': cutoff, 'held_out_projects': len(held_out), 'models': results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', help='JSON array, or SQLite database with --database')
    parser.add_argument('--database', action='store_true', help='replay eligible recorded quote inputs and closeouts')
    parser.add_argument('--train-through', required=True, help='last training quote date (YYYY-MM-DD)')
    args = parser.parse_args()
    try:
        if args.database:
            import db
            conn = db.connect(args.input)
            try:
                result = from_database(conn, args.train_through)
            finally:
                conn.close()
        else:
            result = run(json.loads(Path(args.input).read_text()), args.train_through)
        print(json.dumps(result, indent=2))
    except (ValueError, KeyError, TypeError, OSError) as exc:
        parser.exit(2, 'backtest error: ' + str(exc) + '\n')


if __name__ == '__main__':
    main()
