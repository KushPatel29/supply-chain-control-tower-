"""
Synthetic data generator for the Supply Chain Control Tower portfolio project.
Produces realistic perishable-goods supply chain data (products, lots, warehouses,
customers, inventory snapshots, orders) and writes it as CSVs into data/bronze/,
mimicking the raw landing zone of a Fabric Lakehouse.

Usage:
    python generate_data.py
"""

import numpy as np
import pandas as pd
from faker import Faker
from datetime import timedelta
from pathlib import Path

fake = Faker()
Faker.seed(42)
np.random.seed(42)

OUT_DIR = Path(__file__).resolve().parent.parent / "data" / "bronze"
OUT_DIR.mkdir(parents=True, exist_ok=True)

SNAPSHOT_START = pd.Timestamp("2025-01-01")
SNAPSHOT_END = pd.Timestamp("2025-06-30")

CATEGORIES = {
    "Fresh Meat": ["Beef", "Pork", "Lamb", "Poultry"],
    "Deli": ["Sliced Meats", "Cheese", "Prepared Salads"],
    "Seafood": ["Fresh Fish", "Shellfish", "Smoked Seafood"],
    "Frozen": ["Frozen Poultry", "Frozen Seafood", "Frozen Prepared"],
}

CHANNELS = ["Retail", "Foodservice", "Wholesale"]
REGIONS = ["BC Lower Mainland", "BC Interior", "Alberta", "Ontario", "Quebec"]

N_PRODUCTS = 60
N_SUPPLIERS = 15
N_WAREHOUSES = 8
N_CUSTOMERS = 40
N_LOTS = 3000
N_ORDERS = 20000


def gen_dim_product():
    rows = []
    for i in range(1, N_PRODUCTS + 1):
        category = np.random.choice(list(CATEGORIES.keys()))
        subcategory = np.random.choice(CATEGORIES[category])
        # Fresh/deli/seafood = short shelf life, frozen = long
        shelf_life_days = (
            np.random.randint(3, 10) if category in ("Fresh Meat", "Deli", "Seafood")
            else np.random.randint(90, 270)
        )
        unit_cost = round(np.random.uniform(2.5, 40.0), 2)
        margin_pct = np.random.uniform(0.18, 0.42)
        rows.append({
            "product_id": i,
            "sku": f"SKU-{1000 + i}",
            "product_name": f"{subcategory} {fake.word().capitalize()}",
            "category": category,
            "subcategory": subcategory,
            "shelf_life_days": shelf_life_days,
            "unit_of_measure": np.random.choice(["KG", "CASE", "EA"]),
            "unit_cost": unit_cost,
            "unit_price": round(unit_cost * (1 + margin_pct), 2),
        })
    return pd.DataFrame(rows)


def gen_dim_supplier():
    rows = []
    for i in range(1, N_SUPPLIERS + 1):
        rows.append({
            "supplier_id": i,
            "supplier_name": fake.company(),
            "region": np.random.choice(REGIONS),
        })
    return pd.DataFrame(rows)


def gen_dim_warehouse():
    rows = []
    for i in range(1, N_WAREHOUSES + 1):
        region = np.random.choice(REGIONS)
        rows.append({
            "warehouse_id": i,
            "warehouse_name": f"{region} DC {i}",
            "region": region,
            "city": fake.city(),
        })
    return pd.DataFrame(rows)


def gen_dim_customer():
    rows = []
    for i in range(1, N_CUSTOMERS + 1):
        rows.append({
            "customer_id": i,
            "customer_name": fake.company(),
            "channel": np.random.choice(CHANNELS, p=[0.5, 0.3, 0.2]),
            "region": np.random.choice(REGIONS),
        })
    return pd.DataFrame(rows)


def gen_dim_lot(products: pd.DataFrame, suppliers: pd.DataFrame, warehouses: pd.DataFrame):
    rows = []
    for i in range(1, N_LOTS + 1):
        product = products.sample(1).iloc[0]
        production_date = SNAPSHOT_START + timedelta(days=int(np.random.uniform(-30, 150)))
        expiry_date = production_date + timedelta(days=int(product["shelf_life_days"]))
        rows.append({
            "lot_id": i,
            "product_id": product["product_id"],
            "supplier_id": int(suppliers.sample(1)["supplier_id"].iloc[0]),
            "warehouse_id": int(warehouses.sample(1)["warehouse_id"].iloc[0]),
            "production_date": production_date.date(),
            "received_date": (production_date + timedelta(days=np.random.randint(1, 4))).date(),
            "expiry_date": expiry_date.date(),
        })
    return pd.DataFrame(rows)


def gen_fact_inventory_snapshot(lots: pd.DataFrame):
    """Weekly on-hand qty per lot until it expires or depletes."""
    rows = []
    snapshot_dates = pd.date_range(SNAPSHOT_START, SNAPSHOT_END, freq="7D")
    for _, lot in lots.iterrows():
        starting_qty = np.random.randint(50, 800)
        qty = starting_qty
        for snap in snapshot_dates:
            if snap.date() < lot["received_date"] or snap.date() > lot["expiry_date"]:
                continue
            depletion = np.random.uniform(0.05, 0.25) * starting_qty
            qty = max(0, qty - depletion)
            rows.append({
                "snapshot_date": snap.date(),
                "lot_id": lot["lot_id"],
                "product_id": lot["product_id"],
                "warehouse_id": lot["warehouse_id"],
                "qty_on_hand": round(qty, 1),
            })
            if qty <= 0:
                break
    return pd.DataFrame(rows)


def gen_fact_orders(products, customers, warehouses, lots):
    rows = []
    order_dates = pd.date_range(SNAPSHOT_START, SNAPSHOT_END, freq="D")
    for i in range(1, N_ORDERS + 1):
        product = products.sample(1).iloc[0]
        customer = customers.sample(1).iloc[0]
        candidate_lots = lots[lots["product_id"] == product["product_id"]]
        if candidate_lots.empty:
            continue
        lot = candidate_lots.sample(1).iloc[0]
        order_date = np.random.choice(order_dates)
        promised_date = pd.Timestamp(order_date) + timedelta(days=np.random.randint(1, 5))
        # 90% ship on time, 10% late -> drives OTIF KPI
        on_time = np.random.random() < 0.90
        ship_delay = 0 if on_time else np.random.randint(1, 6)
        shipped_date = promised_date + timedelta(days=ship_delay)
        qty_ordered = np.random.randint(5, 200)
        # Most orders ship complete; a realistic minority fall short (drives OTIF/fill-rate KPIs)
        fill_rate = 1.0 if np.random.random() < 0.85 else np.random.uniform(0.85, 0.99)
        qty_shipped = round(qty_ordered * fill_rate, 1)
        rows.append({
            "order_id": i,
            "order_date": pd.Timestamp(order_date).date(),
            "customer_id": customer["customer_id"],
            "product_id": product["product_id"],
            "lot_id": lot["lot_id"],
            "warehouse_id": lot["warehouse_id"],
            "qty_ordered": qty_ordered,
            "qty_shipped": qty_shipped,
            "promised_date": promised_date.date(),
            "shipped_date": shipped_date.date(),
            "unit_price": product["unit_price"],
            "unit_cost": product["unit_cost"],
        })
    return pd.DataFrame(rows)



# ---------------------------------------------------------------------------
# Global sourcing layer
#
# The order and inventory facts above describe one country's distribution
# network. What they cannot describe is where the goods come FROM, and that is
# the half an MNC control tower is actually built to watch: award concentration,
# single-source SKUs, country exposure, and how long a switch would take when a
# lane closes.
#
# This layer is ADDITIVE by design. The bronze contract allows new columns
# ("additive_change: allowed"), and it draws from its OWN generator so the
# existing RNG stream is untouched - dim_lot, fact_orders and every number
# already published from them stay byte-identical.
# ---------------------------------------------------------------------------

SOURCING_SEED = 20260907

# country -> (bloc, transit days door-to-door, transit sigma, cost index)
# Transit is the dominant term in an offshore lead time and the reason a cheaper
# unit price is not automatically a cheaper supply.
ORIGINS = {
    "Canada":      ("North America", 4, 1.0, 1.00),
    "USA":         ("North America", 6, 1.5, 0.97),
    "Mexico":      ("North America", 12, 3.0, 0.88),
    "Chile":       ("South America", 26, 5.0, 0.83),
    "Brazil":      ("South America", 29, 6.0, 0.81),
    "Spain":       ("Europe", 24, 4.0, 0.92),
    "Poland":      ("Europe", 27, 5.0, 0.86),
    "Turkiye":     ("Europe", 30, 6.0, 0.84),
    "Thailand":    ("Asia Pacific", 38, 7.0, 0.74),
    "Vietnam":     ("Asia Pacific", 40, 8.0, 0.72),
    "China":       ("Asia Pacific", 36, 7.0, 0.70),
    "India":       ("Asia Pacific", 41, 8.0, 0.73),
    "New Zealand": ("Asia Pacific", 33, 5.0, 0.95),
}


def gen_sourcing(products: pd.DataFrame, suppliers: pd.DataFrame):
    """Approved vendor list: who may supply each SKU, and on what terms.

    Award shares are deliberately Pareto rather than uniform. A supply base
    where every SKU has a dozen interchangeable sources has no risk to measure
    and does not resemble any real one; a real AVL has a long tail of
    dual-sourced items and a dangerous handful that are single-sourced.
    """
    rng = np.random.default_rng(SOURCING_SEED)
    names = list(ORIGINS)

    # Supplier profile. Tier 1 are strategic partners with more awards.
    countries = rng.choice(names, size=len(suppliers),
                           p=_origin_weights(names))
    tiers = rng.choice(["Tier 1", "Tier 2", "Tier 3"], size=len(suppliers),
                       p=[0.27, 0.40, 0.33])
    suppliers = suppliers.copy()
    suppliers["country"] = countries
    suppliers["sourcing_bloc"] = [ORIGINS[c][0] for c in countries]
    suppliers["supplier_tier"] = tiers
    # A qualified alternate can absorb volume; an unqualified one needs a
    # requalification programme before it can, which is why the scenario
    # analysis counts them separately.
    suppliers["is_qualified_alternate"] = np.where(tiers == "Tier 3", 0, 1)

    rows = []
    for pid in products["product_id"]:
        # 1-4 approved sources, weighted so single-sourcing is a real minority
        # rather than an impossibility.
        n = int(rng.choice([1, 2, 3, 4], p=[0.15, 0.40, 0.30, 0.15]))
        chosen = rng.choice(suppliers["supplier_id"].to_numpy(), size=n, replace=False)
        # Dirichlet gives a primary source with a genuine majority share.
        shares = rng.dirichlet(np.full(n, 0.9))
        shares = np.round(shares / shares.sum(), 4)
        shares[-1] = round(1.0 - shares[:-1].sum(), 4)
        for supplier_id, share in zip(chosen, shares):
            country = suppliers.loc[suppliers.supplier_id == supplier_id, "country"].iloc[0]
            transit, sigma, cost_index = ORIGINS[country][1:]
            # Contract lead time = transit + the supplier's own processing.
            lead = int(round(transit + rng.normal(5, 1.5)))
            rows.append({
                "product_id": int(pid),
                "supplier_id": int(supplier_id),
                "allocation_share": float(share),
                "contract_lead_days": max(2, lead),
                "lead_time_sigma_days": round(float(sigma), 1),
                "moq_units": int(rng.choice([100, 250, 500, 1000, 2500])),
                "price_index": round(float(cost_index * rng.normal(1.0, 0.04)), 3),
                "is_primary": 0,
            })
    sourcing = pd.DataFrame(rows)
    primary = sourcing.groupby("product_id")["allocation_share"].idxmax()
    sourcing.loc[primary, "is_primary"] = 1
    return suppliers, sourcing


def _origin_weights(names):
    """Weight the supply base towards nearshore, with a real offshore tail."""
    w = np.array([3.0 if ORIGINS[n][0] == "North America" else
                  1.6 if ORIGINS[n][0] == "Europe" else
                  1.4 if ORIGINS[n][0] == "Asia Pacific" else 1.0
                  for n in names])
    return w / w.sum()

def main():
    print("Generating dimension tables...")
    products = gen_dim_product()
    suppliers = gen_dim_supplier()
    warehouses = gen_dim_warehouse()
    customers = gen_dim_customer()

    print("Generating lots (traceability)...")
    lots = gen_dim_lot(products, suppliers, warehouses)

    print("Generating inventory snapshots (this may take a moment)...")
    inventory = gen_fact_inventory_snapshot(lots)

    print("Generating orders...")
    orders = gen_fact_orders(products, customers, warehouses, lots)

    # Sourcing is generated last and from its own RNG, so nothing above shifts.
    print("Generating global sourcing layer (approved vendor list)...")
    suppliers, sourcing = gen_sourcing(products, suppliers)

    tables = {
        "dim_product": products,
        "dim_supplier": suppliers,
        "fact_sourcing": sourcing,
        "dim_warehouse": warehouses,
        "dim_customer": customers,
        "dim_lot": lots,
        "fact_inventory_snapshot": inventory,
        "fact_orders": orders,
    }

    for name, df in tables.items():
        path = OUT_DIR / f"{name}.csv"
        df.to_csv(path, index=False)
        print(f"  wrote {path}  ({len(df):,} rows)")

    print("\nDone. Raw CSVs are in data/bronze/ — these represent the Bronze layer.")


if __name__ == "__main__":
    main()
