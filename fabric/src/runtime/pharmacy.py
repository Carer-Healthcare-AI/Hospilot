"""Pharmacy orders, inventory, and dispensing operations.

pharmacy_order and pharmacy_inventory are streamed (MedicationRequest /
InventoryItem via the change feed), so agents get live counts from the backend's
internal DB. Everything here is the full-list and rules-table side that a per-record
lookup can't hold — see service/pharmacy.py.
"""

from fastapi import APIRouter, Query

from service import pharmacy as pharmacy_svc

router = APIRouter()


@router.get("/pharmacy/orders", summary="All medication orders")
async def pharmacy_orders():
    return await pharmacy_svc.orders()


@router.get("/pharmacy/orders/pending", summary="Medication orders awaiting dispensing")
async def pharmacy_orders_pending():
    return await pharmacy_svc.pending_orders()


@router.get("/pharmacy/orders/stat", summary="STAT (urgent) medication orders")
async def pharmacy_orders_stat():
    return await pharmacy_svc.stat_orders()


@router.get("/pharmacy/inventory", summary="Drug inventory with stock levels")
async def pharmacy_inventory():
    return await pharmacy_svc.inventory()


@router.get("/pharmacy/dispensing-log", summary="Dispensing events over the last N hours")
async def pharmacy_dispensing_log(hours: int = Query(8)):
    return await pharmacy_svc.dispensing_log(hours=hours)


@router.get("/pharmacy/interactions", summary="Drug-interaction rules")
async def pharmacy_interactions():
    return await pharmacy_svc.interaction_rules()


@router.get("/pharmacy/substitutions", summary="Approved substitution rules for out-of-stock drugs")
async def pharmacy_substitutions():
    return await pharmacy_svc.substitution_rules()


@router.get("/pharmacy/controlled-log", summary="Controlled-substance register for the last N hours")
async def pharmacy_controlled_log(hours: int = Query(24)):
    return await pharmacy_svc.controlled_log(hours=hours)


@router.get("/pharmacy/capacity", summary="Daily pharmacy throughput history over the last N days")
async def pharmacy_capacity(days: int = Query(30)):
    return await pharmacy_svc.capacity_history(days=days)
