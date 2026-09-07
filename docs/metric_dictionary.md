# Metric Dictionary — Supply Chain Control Tower

The governance artifact that turns "we built some dashboards" into "we run
a governed semantic layer." Every measure exposed to report consumers is
defined once, here, with an owner, grain, and refresh expectation — the
Power BI model implements these definitions, it does not invent its own.

| # | Metric | Definition | Formula / source | Grain | Owner (role) | Refresh SLA |
|---|--------|------------|------------------|-------|--------------|-------------|
| 1 | Total Revenue | Value of goods shipped, at order-line price | `SUM(qty_shipped × unit_price)` from `gold.fact_orders` | Order line | Finance | Daily 6am |
| 2 | Gross Margin % | Margin after cost of goods, pre-opex | `(Revenue − COGS) / Revenue` | Order line | Finance | Daily 6am |
| 3 | OTIF % | Orders shipped on/before promised date **and** ≥95% fill | `otif_flag` computed in Silver (`02_silver_transform.py`) | Order | Supply Chain | Daily 6am |
| 4 | Fill Rate | Shipped quantity as share of ordered | `qty_shipped / qty_ordered` | Order line | Supply Chain | Daily 6am |
| 5 | Inventory Turns | Annualized sell-through of inventory | `(COGS / Avg Inventory Value) × (365 / days in period)` | Aggregate | Supply Chain | Weekly |
| 6 | Days on Hand | Days current inventory would last at current COGS run rate | `365 / Inventory Turns` | Aggregate | Supply Chain | Weekly |
| 7 | Expiry Risk Flag | Shelf-life urgency banding per lot snapshot | Critical ≤2 days, Warning ≤5 days, else OK — computed in Silver | Lot × snapshot | Operations | Weekly |
| 8 | % Inventory at Risk | Share of inventory value flagged Critical or Warning | `(Critical $ + Warning $) / Total Inventory $` | Aggregate | Operations | Weekly |
| 9 | Inventory Value | On-hand quantity valued at unit cost | `qty_on_hand × unit_cost` | Lot × snapshot | Finance | Weekly |
| 10 | DQ Pass Rate | Share of automated data-quality checks passing | `passed / total` from `gold.dq_log` | Pipeline run | Data team | Per run |
| 11 | Single-Source SKUs | SKUs with exactly one approved supplier | `COUNTROWS(FILTER(VALUES(product_id), DISTINCTCOUNT(supplier_id) = 1))` on `fact_sourcing` | SKU | Procurement | Weekly |
| 12 | Award HHI | Herfindahl index over award shares within a SKU, averaged across SKUs | `AVERAGEX(VALUES(product_id), SUMX(allocation_share²) × 10,000)` | SKU | Procurement | Weekly |
| 13 | Country HHI | Same concentration measure across origin countries, on COGS share | `SUMX(ALL(country_exposure), exposed_share²) × 10,000` | Origin | Procurement | Weekly |
| 14 | Stranded SKUs | SKUs with no **qualified** alternate if an origin closes | `analytics/supply_risk.py` disruption scenario | Origin | Procurement | Weekly |
| 15 | Contract Lead Days | Award-weighted contract lead time | `SUMX(lead × share) / SUMX(share)` on `fact_sourcing` | SKU | Procurement | Weekly |
| 16 | Reorder Point | Cycle stock plus safety stock | `μ·L + z·√(L·σ² + μ²·σ_L²)` (King's formula, z = 1.645) | SKU × warehouse | Supply Chain | Weekly |
| 17 | Replenishment Gap | Cost of returning every position to its reorder point | `SUM(MAX(reorder_point − on_hand − on_order, 0) × unit_cost)` — summed, never netted against surpluses | SKU × warehouse | Supply Chain | Weekly |
| 18 | Cover Days | Days of demand the stock on hand covers | `on_hand / avg_daily_demand` | SKU × warehouse | Supply Chain | Weekly |
| 19 | Stockout Window | Positions whose cover is shorter than **their own** lead time | `cover_days < lead_days` — per position, never against a network average | SKU × warehouse | Supply Chain | Weekly |
| 20 | Excess Value | Working capital above 1.5× the reorder point | `MAX(position − 1.5 × reorder_point, 0) × unit_cost` | SKU × warehouse | Finance | Weekly |
| 21 | Transfer Value | Shortfall coverable by moving stock, not buying it | `MIN(short_units, spare_units) × unit_cost` per SKU across warehouses | SKU | Supply Chain | Weekly |
| 22 | Planning Lane | Nearshore or offshore, by the bloc of the **slowest** approved source | `sourcing_bloc` of `MAX(contract_lead_days)` — the lead the reorder point was built from | SKU | Procurement | Weekly |

## Change control

- A metric definition changes only via a PR to this file **and** the
  matching DAX/notebook change in the same commit — definition and
  implementation never drift apart.
- Threshold constants (95% fill for OTIF, 2/5-day expiry bands, 0.5%
  variance tolerance) live in exactly one place each (Silver notebook or
  DAX measure) and are referenced here, not duplicated.

## Known definitional decisions (the "why" a stakeholder will ask about)

- **OTIF uses promised date, not requested date** — measures our
  reliability against what we committed, not what the customer wished for.
  Swap to requested-date OTIF if the business runs customer-scorecard
  negotiations on it.
- **Inventory Turns uses average inventory across snapshots**, not
  point-in-time — a single end-of-period snapshot over/understates turns
  when inventory is seasonal.
- **Revenue uses order-line price** (price at time of sale), not current
  product master price — historical reports must not change when prices do.
- **Safety stock carries both variability terms.** Demand varying over the
  lead time *and* the lead time itself varying against average demand.
  Dropping the second is the standard way a safety stock ends up too small on
  a long offshore lane, which is where it matters most.
- **A SKU's planning lane is its slowest approved source**, not an average of
  its sources — the slowest is the lead the reorder point was built from, and
  it is what you fall back on. It reuses the sourcing bloc from metric 14 so
  the sourcing and inventory pages cannot disagree about "offshore".
- **Shortfalls are summed, never netted against surpluses.** A surplus of
  SKU A in Calgary does not fill a hole in SKU B in Halifax. The part that
  *is* coverable by moving stock is reported separately as metric 21.
- **The disruption scenario counts qualified alternates only.** An
  unqualified supplier needs a requalification programme before it can absorb
  volume, so folding the two together produces a comforting number that is
  wrong in exactly the situation it exists for.
