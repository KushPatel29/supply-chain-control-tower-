# How to open this Power BI Project

Everything is here: the semantic model **and** the report. Both are authored in
code — TMDL for the model, PBIR for the pages — so what you open is the finished
thing, not a starting point.

|  | |
|---|---|
| Tables | **26** (10 dimensional and fact tables from the bronze CSVs, the rest engine outputs and disconnected what-if tables) |
| Relationships | **12** |
| Measures | **137**, in one `_Measures` table |
| Pages | **8**, **66** visuals |
| Security roles | **3** — Sales - BC Lower Mainland, Regional Manager, Field Ops |

## Steps

1. **Enable PBIP support** (one-time): Power BI Desktop → File → Options and
   settings → Options → Preview features → check **"Power BI Project (.pbip)
   save option"** → restart Desktop. Recent versions have this on by default —
   if it is not in the list, it is already enabled.
2. Double-click **`SupplyChainControlTower.pbip`**.
3. Click **Refresh** to load the CSVs into the model. The pages render as soon
   as it finishes.
4. If you cloned this repo somewhere other than the path it was authored at:
   Home → Transform data → Edit parameters → set **DataPath** to your own
   `...\supply-chain-control-tower\data\bronze` folder, then Refresh. Nothing
   else is path-dependent.

## Verify it loaded

- **Model view** — a star with `fact_orders` and `fact_inventory` in the middle,
  and the engine-output tables standalone beside it. Those are deliberate: a
  service-level exchange curve and a supplier scorecard are computed in Python
  under test, not recomputed in DAX.
- **Data pane** — `_Measures` with 137 measures, grouped into display folders.
- **Modeling → Manage roles** — the three roles above. Modeling → View as →
  Sales - BC Lower Mainland, and every total shrinks to that region.
- **Page 1, Executive Overview** — the Region and Channel slicers move the KPI
  cards. `% Inventory at Risk` is the one that does not, and says so on the
  card: stock on hand has no customer, so it has no channel.

## What is where

| Page | What it answers |
|---|---|
| Executive Overview | Revenue, margin, OTIF and expiry risk in one screen |
| Global Sourcing Risk | Country exposure and single-sourced spend |
| Supplier Performance | Scorecard, reject reasons, award-shift candidates |
| Inventory & Network Health | Inventory position and transfer opportunities |
| Service Level Economics | The service-level exchange curve and policy options |
| Inventory & Expiry Risk | Expiry bands, lot detail, inventory trend |
| Fulfillment (OTIF) | Perfect-order rate and where it fails |
| Executive Insights | The findings, written out |

[`../BUILD_GUIDE.md`](../BUILD_GUIDE.md) documents how the model and measures
are put together — read it to understand or extend the report, not to build it.

## If Desktop shows an error opening the project

Note the exact error text and the file it names. The TMDL and PBIR are
hand-authored, so a version-specific syntax quirk is possible; it is usually a
one-line fix. `pytest tests/test_semantic_model.py tests/test_powerbi_formatting.py`
checks the bindings without opening Desktop and will often find it first.
