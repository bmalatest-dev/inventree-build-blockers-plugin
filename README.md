# InvenTree Build Blockers

InvenTree data-export plugin which adds a blocker-oriented export to the
Build Order required-parts / line-items table.

## Purpose

For a selected Build Order, identify required components which are not
currently covered by consumed quantity, allocated stock, or immediately
available stock, then show relevant open Purchase Orders and expected dates.

## Blocker calculation

```text
Remaining Requirement = Required Qty - Consumed Qty - Allocated Qty

Blocker Qty = max(
    Remaining Requirement - Available Stock,
    0
)
```

Purchase Orders are not subtracted from `Blocker Qty`. A part which is not
physically available is still considered a current production blocker.

The report separately calculates:

```text
Shortage After Open POs = max(Blocker Qty - Outstanding Open PO Qty, 0)
```

## Exported information

- Build Order
- IPN
- Part name and description
- Required quantity
- Consumed quantity
- Allocated quantity
- Available stock
- Available substitute stock
- Available variant stock
- Blocker quantity
- Open PO quantity
- PO quantity applied
- Shortage after open POs
- Earliest PO target date
- Expected full-coverage date
- Purchase Order references
- PO detail

## Installation through the InvenTree UI

Once this repository has been pushed to GitHub:

1. Navigate to **Admin Center > Plugins**.
2. Select **Install Plugin**.
3. Enter:

   **Package Name**
   ```text
   inventree-build-blockers
   ```

   **Source URL**
   ```text
   git+https://github.com/YOUR-ACCOUNT/inventree-build-blockers-plugin
   ```

   **Version**
   Leave blank.

4. Enable **Confirm plugin installation**.
5. Select **Install**.
6. After installation, go to **Settings > Plugin Management**.
7. Enable **Build Blockers**.

The server may restart when the plugin is enabled.

## Use

1. Open a Build Order.
2. Navigate to the Required Parts / Line Items table.
3. Select **Export Data**.
4. Choose **Build Blockers** as the exporter.
5. Leave **Blockers Only** enabled for the normal blocker report.

## Export options

### Blockers Only
Default: Yes.

### Include Optional BOM Items
Default: No.

### Include Pending / On-Hold POs
Default: Yes.

Disable this if only formally placed purchase orders should count as incoming
supply.

## Important validation before production use

Validate the plugin against a known Build Order containing:

1. A fully allocated line.
2. An unallocated line with enough stock.
3. A true stock shortage.
4. A shortage covered by one PO.
5. A shortage split over multiple POs.
6. A shortage not fully covered by open POs.
7. A PO line with no line target date but a parent PO target date.
8. An optional BOM item.
9. A part with more than one SupplierPart.

## Current version

0.1.0
