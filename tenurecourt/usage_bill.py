"""Draft a namespace bill from recorded writes."""
from .store import one


def provisional_invoice(db, namespace, unit_price):
 if unit_price<0:return {'decision':'invalid'}
 n=one(db,'SELECT uid FROM namespaces WHERE id=?',(namespace,))
 if not n:return {'decision':'unavailable'}
 count=one(db,'SELECT COUNT(*) AS n FROM writes WHERE namespace=?',(namespace,))['n']
 return {'decision':'estimated','namespace_uid':n['uid'],'amount':count*unit_price}
