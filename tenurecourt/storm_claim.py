"""Storm damage claim adjudication for leased machine-room sites.

Every damaged component binds its pre-storm condition, the damage scope,
weather and maintenance evidence, the repair work items and the completion
acceptance. Competing quotes for the same work item only select a
contractor; they never add up. Sudden rainstorm and long neglected leakage
carry different deductibles, and while causal evidence is missing none of
the repair cost can be pushed to the insurer. Emergency tarping, formal
repair, customer relocation and equipment salvage are separate ledgers, and
service credits already granted to customers are deducted from the claim
instead of being claimed twice. Permanent repair is the irreversible step:
it needs either an adjuster's inspection of the intact scene or a notarized
record of the damaged originals under the emergency clause, and once any
repair has started no inspection can be redone. Insurer payments, operator
advances, contractor receipts and customer compensation are separate
vouchers; weather or acceptance evidence arriving after settlement can only
raise a recovery or a refund, never rewrite the settlement.
"""
from .store import one, rows, tx

SCHEMA = """
CREATE TABLE IF NOT EXISTS storm_policies(id TEXT PRIMARY KEY,site TEXT,storm_deductible INTEGER,neglect_deductible INTEGER);
CREATE TABLE IF NOT EXISTS storm_components(id TEXT PRIMARY KEY,site TEXT,name TEXT,pre_condition TEXT,maintained INTEGER,at INTEGER);
CREATE TABLE IF NOT EXISTS storm_weather(id TEXT PRIMARY KEY,site TEXT,kind TEXT,at INTEGER,filed INTEGER);
CREATE TABLE IF NOT EXISTS storm_damages(id TEXT PRIMARY KEY,component TEXT,site TEXT,scope TEXT,cause TEXT,status TEXT,at INTEGER);
CREATE TABLE IF NOT EXISTS storm_items(id TEXT PRIMARY KEY,damage TEXT,site TEXT,kind TEXT,lease TEXT,customer TEXT,from_ts INTEGER,to_ts INTEGER,status TEXT,contractor TEXT,amount INTEGER);
CREATE TABLE IF NOT EXISTS storm_quotes(id TEXT PRIMARY KEY,item TEXT,contractor TEXT,amount INTEGER,at INTEGER,status TEXT);
CREATE TABLE IF NOT EXISTS storm_inspections(id TEXT PRIMARY KEY,site TEXT,adjuster TEXT,verdict TEXT,reason TEXT,at INTEGER);
CREATE TABLE IF NOT EXISTS storm_notarizations(id TEXT PRIMARY KEY,damage TEXT,notary TEXT,at INTEGER);
CREATE TABLE IF NOT EXISTS storm_contractors(id TEXT PRIMARY KEY,site TEXT,contractor TEXT,at INTEGER);
CREATE TABLE IF NOT EXISTS storm_settlements(id TEXT PRIMARY KEY,site TEXT,at INTEGER,insurer INTEGER,operator INTEGER,status TEXT);
CREATE TABLE IF NOT EXISTS storm_settlement_lines(settlement TEXT,damage TEXT,cause TEXT,basis INTEGER,insurer INTEGER,PRIMARY KEY(settlement,damage));
CREATE TABLE IF NOT EXISTS storm_credits(id TEXT PRIMARY KEY,lease TEXT,customer TEXT,amount INTEGER,from_ts INTEGER,to_ts INTEGER,at INTEGER);
CREATE TABLE IF NOT EXISTS storm_vouchers(id TEXT PRIMARY KEY,site TEXT,kind TEXT,item TEXT,damage TEXT,lease TEXT,amount INTEGER,payer TEXT,payee TEXT,at INTEGER);
"""
KINDS = ('tarping', 'repair', 'relocation', 'salvage')
REPAIR_STAGES = ('started', 'completed', 'accepted', 'failed')


def _ensure(db):
    db.executescript(SCHEMA)


def _damage(db, id):
    return one(db, 'SELECT * FROM storm_damages WHERE id=?', (id,))


def _item(db, id):
    return one(db, 'SELECT * FROM storm_items WHERE id=?', (id,))


def _total(db, q, a=()):
    return one(db, q, a)['t']


def _kind_total(db, site, kind):
    return _total(db, 'SELECT COALESCE(SUM(amount),0) AS t FROM storm_vouchers WHERE site=? AND kind=?', (site, kind))


def _damage_voucher_total(db, damage, kind):
    return _total(db, 'SELECT COALESCE(SUM(amount),0) AS t FROM storm_vouchers WHERE damage=? AND kind=?', (damage, kind))


def _voucher(db, id, site, kind, amount, at, item=None, damage=None, lease=None, payer=None, payee=None):
    db.execute('INSERT INTO storm_vouchers VALUES(?,?,?,?,?,?,?,?,?,?)',
               (id, site, kind, item, damage, lease, amount, payer, payee, at))


def _scene_altered(db, site):
    return bool(one(db, "SELECT id FROM storm_items WHERE site=? AND kind='repair' AND status IN ('started','completed','accepted','failed')", (site,)))


def _basis(db, damage):
    """Loss basis of one damage: accepted works plus uncompensated customer
    loss (relocation minus service credits already granted) minus salvage."""
    items = rows(db, "SELECT * FROM storm_items WHERE damage=? AND status='accepted'", (damage['id'],))
    works = sum(i['amount'] for i in items if i['kind'] in ('tarping', 'repair'))
    reloc = [i for i in items if i['kind'] == 'relocation']
    credits = sum(_total(db, 'SELECT COALESCE(SUM(amount),0) AS t FROM storm_credits WHERE lease=?', (lease,))
                  for lease in {i['lease'] for i in reloc})
    customer = max(0, sum(i['amount'] for i in reloc) - credits)
    salvage = sum(i['amount'] for i in items if i['kind'] == 'salvage')
    return max(0, works + customer - salvage)


def _due(db, policy, damage):
    """Insurer share currently evidenced for one damage."""
    if damage['status'] == 'denied':
        return 0
    basis = _basis(db, damage)
    if damage['cause'] == 'storm':
        return max(0, basis - policy['storm_deductible'])
    if damage['cause'] == 'neglect':
        return max(0, basis - policy['neglect_deductible'])
    return 0


def _accounted(db, damage):
    """Insurer money already booked against one damage."""
    line = _total(db, 'SELECT COALESCE(SUM(insurer),0) AS t FROM storm_settlement_lines WHERE damage=?', (damage,))
    return line + _damage_voucher_total(db, damage, 'recovery') - _damage_voucher_total(db, damage, 'refund')


def _run(db, x):
    op = x['op']
    if op == 'policy':
        db.execute('INSERT INTO storm_policies VALUES(?,?,?,?)', (x['id'], x['site'], x['storm_deductible'], x['neglect_deductible']))
        return {'decision': 'recorded'}
    if op == 'component':
        db.execute('INSERT INTO storm_components VALUES(?,?,?,?,?,?)',
                   (x['id'], x['site'], x['name'], x['pre_condition'], 1 if x.get('maintained') else 0, x['at']))
        return {'decision': 'registered'}
    if op == 'weather':
        db.execute('INSERT INTO storm_weather VALUES(?,?,?,?,?)', (x['id'], x['site'], x['kind'], x['at'], x['filed']))
        return {'decision': 'filed'}
    if op == 'damage':
        c = one(db, 'SELECT * FROM storm_components WHERE id=?', (x['component'],))
        if not c:
            return {'decision': 'unknown_component'}
        db.execute('INSERT INTO storm_damages VALUES(?,?,?,?,?,?,?)', (x['id'], c['id'], c['site'], x['scope'], 'unknown', 'reported', x['at']))
        return {'decision': 'reported'}
    if op == 'assess':
        d = _damage(db, x['id'])
        if not d:
            return {'decision': 'unknown_damage'}
        if d['status'] == 'denied':
            return {'decision': 'damage_denied'}
        if d['cause'] != 'unknown':
            return {'decision': 'cause_already_determined'}
        c = one(db, 'SELECT * FROM storm_components WHERE id=?', (d['component'],))
        if x['cause'] == 'storm':
            w = one(db, "SELECT id FROM storm_weather WHERE site=? AND kind='rainstorm' AND at<=?", (d['site'], d['at']))
            if not w:
                return {'decision': 'insufficient_causal_evidence'}
        elif x['cause'] == 'neglect':
            if c['maintained'] and c['pre_condition'] != 'prior_damage':
                return {'decision': 'insufficient_causal_evidence'}
        else:
            return {'decision': 'unknown_cause'}
        db.execute("UPDATE storm_damages SET cause=?,status='verified' WHERE id=?", (x['cause'], d['id']))
        return {'decision': 'assessed'}
    if op == 'item':
        d = _damage(db, x['damage'])
        if not d:
            return {'decision': 'unknown_damage'}
        if d['status'] == 'denied':
            return {'decision': 'damage_denied'}
        if x['kind'] not in KINDS:
            return {'decision': 'invalid_kind'}
        lease = customer = frm = to = None
        if x['kind'] == 'relocation':
            lease = one(db, 'SELECT * FROM leases WHERE id=?', (x['lease'],))
            if not lease or lease['status'] != 'active' or lease['holder'] != x['customer'] \
                    or lease['start'] > x['from_ts'] or lease['expires'] < x['to_ts']:
                return {'decision': 'lease_not_active'}
            lease, customer, frm, to = x['lease'], x['customer'], x['from_ts'], x['to_ts']
        db.execute('INSERT INTO storm_items VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                   (x['id'], d['id'], d['site'], x['kind'], lease, customer, frm, to, 'open', None, None))
        return {'decision': 'created'}
    if op == 'quote':
        i = _item(db, x['item'])
        if not i:
            return {'decision': 'unknown_item'}
        if i['status'] != 'open':
            return {'decision': 'item_already_awarded'}
        if x['amount'] <= 0:
            return {'decision': 'invalid_amount'}
        db.execute('INSERT INTO storm_quotes VALUES(?,?,?,?,?,?)', (x['id'], i['id'], x['contractor'], x['amount'], x['at'], 'pending'))
        return {'decision': 'quoted'}
    if op == 'award':
        q = one(db, 'SELECT * FROM storm_quotes WHERE id=?', (x['id'],))
        if not q or q['status'] != 'pending':
            return {'decision': 'quote_not_pending'}
        i = _item(db, q['item'])
        if i['status'] != 'open':
            return {'decision': 'item_already_awarded'}
        db.execute("UPDATE storm_items SET status='awarded',contractor=?,amount=? WHERE id=?", (q['contractor'], q['amount'], i['id']))
        db.execute("UPDATE storm_quotes SET status='selected' WHERE id=?", (q['id'],))
        db.execute("UPDATE storm_quotes SET status='rejected' WHERE item=? AND status='pending'", (i['id'],))
        return {'decision': 'awarded'}
    if op == 'approve_contractor':
        db.execute('INSERT INTO storm_contractors VALUES(?,?,?,?)', (x['id'], x['site'], x['contractor'], x['at']))
        return {'decision': 'approved'}
    if op == 'start':
        i = _item(db, x['item'])
        if not i or i['status'] != 'awarded':
            return {'decision': 'item_not_awarded'}
        if i['contractor'] != x['contractor']:
            return {'decision': 'unauthorized_contractor'}
        if i['kind'] == 'repair':
            inspected = one(db, "SELECT id FROM storm_inspections WHERE site=? AND verdict='confirmed'", (i['site'],))
            notarized = one(db, 'SELECT id FROM storm_notarizations WHERE damage=?', (i['damage'],))
            if not inspected and not notarized:
                return {'decision': 'inspection_or_notarization_required'}
            if inspected and not notarized and not one(db, 'SELECT id FROM storm_contractors WHERE site=? AND contractor=?', (i['site'], i['contractor'])):
                return {'decision': 'contractor_not_approved'}
        db.execute("UPDATE storm_items SET status='started' WHERE id=?", (i['id'],))
        return {'decision': 'started'}
    if op == 'complete':
        i = _item(db, x['item'])
        if not i or i['status'] != 'started':
            return {'decision': 'item_not_started'}
        if i['contractor'] != x['contractor']:
            return {'decision': 'unauthorized_contractor'}
        db.execute("UPDATE storm_items SET status='completed' WHERE id=?", (i['id'],))
        return {'decision': 'completed'}
    if op == 'accept':
        i = _item(db, x['item'])
        if not i or i['status'] != 'completed':
            return {'decision': 'item_not_completed'}
        if x['result'] != 'passed':
            db.execute("UPDATE storm_items SET status='failed' WHERE id=?", (i['id'],))
            return {'decision': 'acceptance_failed'}
        db.execute("UPDATE storm_items SET status='accepted' WHERE id=?", (i['id'],))
        return {'decision': 'accepted'}
    if op == 'inspect':
        if x['verdict'] not in ('confirmed', 'denied'):
            return {'decision': 'invalid_verdict'}
        if _scene_altered(db, x['site']):
            return {'decision': 'scene_altered'}
        db.execute('INSERT INTO storm_inspections VALUES(?,?,?,?,?,?)', (x['id'], x['site'], x['adjuster'], x['verdict'], None, x['at']))
        if x['verdict'] == 'denied':
            db.execute("UPDATE storm_damages SET status='denied' WHERE site=? AND status!='denied'", (x['site'],))
            return {'decision': 'denied'}
        return {'decision': 'inspected'}
    if op == 'notarize':
        d = _damage(db, x['damage'])
        if not d:
            return {'decision': 'unknown_damage'}
        if one(db, "SELECT id FROM storm_items WHERE damage=? AND kind='repair' AND status IN ('started','completed','accepted','failed')", (d['id'],)):
            return {'decision': 'scene_altered'}
        db.execute('INSERT INTO storm_notarizations VALUES(?,?,?,?)', (x['id'], d['id'], x['notary'], x['at']))
        return {'decision': 'notarized'}
    if op == 'deny':
        d = _damage(db, x['damage'])
        if not d:
            return {'decision': 'unknown_damage'}
        db.execute("UPDATE storm_damages SET status='denied' WHERE id=?", (d['id'],))
        db.execute('INSERT INTO storm_inspections VALUES(?,?,?,?,?,?)', (x['id'], d['site'], x['adjuster'], 'denied', x.get('reason'), x['at']))
        return {'decision': 'denied'}
    if op == 'settle':
        p = one(db, 'SELECT * FROM storm_policies WHERE site=?', (x['site'],))
        if not p:
            return {'decision': 'missing_policy'}
        if one(db, 'SELECT id FROM storm_settlements WHERE site=?', (x['site'],)):
            return {'decision': 'already_settled'}
        insurer = operator = 0
        for d in rows(db, 'SELECT * FROM storm_damages WHERE site=?', (x['site'],)):
            basis = _basis(db, d)
            due = _due(db, p, d)
            insurer += due
            operator += basis - due
            db.execute('INSERT INTO storm_settlement_lines VALUES(?,?,?,?,?)', (x['id'], d['id'], d['cause'], basis, due))
        db.execute('INSERT INTO storm_settlements VALUES(?,?,?,?,?,?)', (x['id'], x['site'], x['at'], insurer, operator, 'settled'))
        return {'decision': 'settled', 'insurer': insurer, 'operator': operator}
    if op == 'advance':
        i = _item(db, x['item'])
        if not i or i['status'] != 'accepted' or i['kind'] not in ('tarping', 'repair'):
            return {'decision': 'item_not_accepted'}
        if one(db, "SELECT id FROM storm_vouchers WHERE item=? AND kind='operator_advance'", (i['id'],)):
            return {'decision': 'already_advanced'}
        _voucher(db, x['id'], i['site'], 'operator_advance', i['amount'], x['at'], item=i['id'], payer='operator', payee=i['contractor'])
        return {'decision': 'advanced'}
    if op == 'receipt':
        a = one(db, "SELECT * FROM storm_vouchers WHERE id=? AND kind='operator_advance'", (x['advance'],))
        if not a:
            return {'decision': 'unknown_advance'}
        if one(db, "SELECT id FROM storm_vouchers WHERE item=? AND kind='contractor_receipt'", (a['item'],)):
            return {'decision': 'already_receipted'}
        _voucher(db, x['id'], a['site'], 'contractor_receipt', a['amount'], x['at'], item=a['item'], payer=a['payee'], payee='operator')
        return {'decision': 'received'}
    if op == 'insurer_pay':
        s = one(db, 'SELECT * FROM storm_settlements WHERE site=?', (x['site'],))
        if not s:
            return {'decision': 'not_settled'}
        paid = _kind_total(db, x['site'], 'insurer_payment')
        if paid + x['amount'] > s['insurer'] - _kind_total(db, x['site'], 'refund'):
            return {'decision': 'exceeds_settlement'}
        _voucher(db, x['id'], x['site'], 'insurer_payment', x['amount'], x['at'], payer='insurer', payee='operator')
        return {'decision': 'paid'}
    if op == 'credit':
        lease = one(db, 'SELECT * FROM leases WHERE id=?', (x['lease'],))
        if not lease or lease['holder'] != x['customer']:
            return {'decision': 'unknown_lease'}
        db.execute('INSERT INTO storm_credits VALUES(?,?,?,?,?,?,?)',
                   (x['id'], x['lease'], x['customer'], x['amount'], x['from_ts'], x['to_ts'], x['at']))
        return {'decision': 'credited'}
    if op == 'compensate':
        i = _item(db, x['item'])
        if not i or i['status'] != 'accepted' or i['kind'] != 'relocation':
            return {'decision': 'item_not_accepted'}
        credits = _total(db, 'SELECT COALESCE(SUM(amount),0) AS t FROM storm_credits WHERE lease=?', (i['lease'],))
        comped = _total(db, "SELECT COALESCE(SUM(amount),0) AS t FROM storm_vouchers WHERE item=? AND kind='customer_compensation'", (i['id'],))
        if comped + credits + x['amount'] > i['amount']:
            return {'decision': 'credit_already_applied'}
        _voucher(db, x['id'], i['site'], 'customer_compensation', x['amount'], x['at'], item=i['id'], lease=i['lease'], payer='operator', payee=i['customer'])
        return {'decision': 'compensated'}
    if op == 'recover':
        d = _damage(db, x['damage'])
        if not d:
            return {'decision': 'unknown_damage'}
        if not one(db, 'SELECT id FROM storm_settlements WHERE site=?', (d['site'],)):
            return {'decision': 'not_settled'}
        p = one(db, 'SELECT * FROM storm_policies WHERE site=?', (d['site'],))
        delta = _due(db, p, d) - _accounted(db, d['id'])
        if delta > 0:
            _voucher(db, x['id'], d['site'], 'recovery', delta, x['at'], damage=d['id'], payer='insurer', payee='operator')
            return {'decision': 'recovered', 'amount': delta}
        if delta < 0:
            return {'decision': 'refund_due', 'amount': -delta}
        return {'decision': 'nothing_to_recover'}
    if op == 'refund':
        d = _damage(db, x['damage'])
        if not d:
            return {'decision': 'unknown_damage'}
        cash = _kind_total(db, d['site'], 'insurer_payment') + _kind_total(db, d['site'], 'recovery') - _kind_total(db, d['site'], 'refund')
        if x['amount'] <= 0 or x['amount'] > _accounted(db, d['id']) or x['amount'] > cash:
            return {'decision': 'refund_exceeds_payments'}
        _voucher(db, x['id'], d['site'], 'refund', x['amount'], x['at'], damage=d['id'], payer='operator', payee='insurer')
        return {'decision': 'refunded'}
    if op == 'contractor_refund':
        i = _item(db, x['item'])
        if not i:
            return {'decision': 'unknown_item'}
        got = _total(db, "SELECT COALESCE(SUM(amount),0) AS t FROM storm_vouchers WHERE item=? AND kind='contractor_receipt'", (i['id'],))
        back = _total(db, "SELECT COALESCE(SUM(amount),0) AS t FROM storm_vouchers WHERE item=? AND kind='contractor_refund'", (i['id'],))
        if x['amount'] <= 0 or back + x['amount'] > got:
            return {'decision': 'refund_exceeds_receipts'}
        _voucher(db, x['id'], i['site'], 'contractor_refund', x['amount'], x['at'], item=i['id'], payer=i['contractor'], payee='operator')
        return {'decision': 'refunded'}
    raise ValueError(op)


def run(db, x):
    _ensure(db)
    with tx(db):
        return _run(db, x)


def _item_amount(db, i):
    if i['amount'] is not None:
        return i['amount']
    return one(db, "SELECT MIN(amount) AS m FROM storm_quotes WHERE item=? AND status='pending'", (i['id'],))['m'] or 0


def claim_estimate(db, site):
    """Provisional claim: one amount per work item, never a sum of quotes."""
    _ensure(db)
    return sum(_item_amount(db, i) for i in rows(db, 'SELECT * FROM storm_items WHERE site=?', (site,)))


def ledger(db, site):
    """Separate ledgers for tarping, repair, relocation and salvage."""
    _ensure(db)
    out = {}
    for i in rows(db, 'SELECT * FROM storm_items WHERE site=?', (site,)):
        out[i['kind']] = out.get(i['kind'], 0) + _item_amount(db, i)
    return out


def balances(db, site):
    """Actual cash position of every party from the vouchers."""
    _ensure(db)
    b = {k: _kind_total(db, site, k) for k in
         ('insurer_payment', 'recovery', 'refund', 'operator_advance', 'contractor_receipt', 'contractor_refund', 'customer_compensation')}
    b['insurer_net'] = b['insurer_payment'] + b['recovery'] - b['refund']
    b['contractor_net'] = b['contractor_receipt'] - b['contractor_refund']
    return b


def dossier(db, damage):
    """The full evidence chain bound to one damaged component."""
    _ensure(db)
    d = _damage(db, damage)
    if not d:
        return None
    return {
        'component': one(db, 'SELECT * FROM storm_components WHERE id=?', (d['component'],)),
        'damage': d,
        'weather': rows(db, 'SELECT * FROM storm_weather WHERE site=?', (d['site'],)),
        'items': rows(db, 'SELECT * FROM storm_items WHERE damage=?', (d['id'],)),
        'notarizations': rows(db, 'SELECT * FROM storm_notarizations WHERE damage=?', (d['id'],)),
    }
