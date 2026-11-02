"""Build parameterized order predicates; uncertain clues rank rather than filter."""
import json

from app.services.commerce.common import at


def pattern(value):
    return '%' + value.replace('!', '!!').replace('%', '!%').replace('_', '!_') + '%'


def item_predicate(clues):
    """Every item clue must describe the SAME purchased line."""
    terms = list(dict.fromkeys((clues.get('query') or '').split()))
    text = "LOWER(CONCAT_WS(' ', i.title_snapshot, CAST(i.spec_snapshot AS CHAR)))"
    clauses = [text + " LIKE LOWER(%s) ESCAPE '!'" for _ in terms]
    values = [pattern(term) for term in terms]
    for spec in clues.get('specs') or []:
        clauses.append('JSON_UNQUOTE(JSON_EXTRACT(i.spec_snapshot,%s))=%s')
        values.extend(['$.' + json.dumps(spec['name'], ensure_ascii=False), spec['value']])
    if clues.get('item_quantity') is not None:
        clauses.append('i.quantity=%s')
        values.append(clues['item_quantity'])
    return ' AND '.join(clauses) or '0=1', values


def order_predicates(clues, *, legacy_id=False, required_item=None):
    """Named predicates also form the explainable, grouped hint score."""
    result = []
    item_sql, item_values = item_predicate(clues)
    if item_values:
        required_sql, required_values = item_predicate(required_item or {})
        if required_values:
            item_sql = '(' + required_sql + ') AND (' + item_sql + ')'
            item_values = required_values + item_values
        sql = ('EXISTS (SELECT 1 FROM order_items i WHERE i.platform_code=o.platform_code '
               'AND i.order_id=o.order_id AND (' + item_sql + '))')
        # Preserve old keyword/ID lookup only without structured line constraints.
        if legacy_id and clues.get('query') and not clues.get('specs') and clues.get('item_quantity') is None:
            sql = "(o.order_id LIKE %s ESCAPE '!' OR " + sql + ')'
            item_values = [pattern(clues['query']), *item_values]
        result.append(('item', sql, item_values))
    if clues.get('created_range'):
        dates = clues['created_range']
        result.append(('created_range', '(o.created_at>=%s AND o.created_at<%s)', [at(dates['from']), at(dates['to'])]))
    if clues.get('paid_amount_range'):
        amounts = clues['paid_amount_range']
        clauses, values = ['o.currency=%s'], [amounts['currency']]
        for key, op in [('minimum', '>='), ('maximum', '<=')]:
            if amounts.get(key) is not None:
                clauses.append('o.paid_amount' + op + '%s')
                values.append(amounts[key])
        result.append(('paid_amount_range', '(' + ' AND '.join(clauses) + ')', values))
    return result


def ranked_query(base, values, hints, *, required_item=None):
    """Ranking happens before SQL pagination, so later matches cannot be hidden."""
    predicates = order_predicates(hints or {}, required_item=required_item)
    if not predicates:
        return 'SELECT o.* ' + base, values, []
    projections, parameters, names = [], [], []
    for name, sql, params in predicates:
        names.append(name)
        projections.append('CASE WHEN ' + sql + ' THEN 1 ELSE 0 END hint_' + name)
        parameters.extend(params)
    inner = 'SELECT o.*,' + ','.join(projections) + ' ' + base
    score = '+'.join('hint_' + name for name in names)
    return 'SELECT ranked.*,(' + score + ') hint_score FROM (' + inner + ') ranked', parameters + values, names
