# InvenTree Build Blockers

InvenTree data-export plugin for identifying Build Order component blockers.

## Modes

### Individual Build Order
Open a Build Order, go to Required Parts / Line Items, choose Export Data, then choose **Build Blockers**.

### All Production Build Orders
Go to the main Build Orders table, choose Export Data, then choose **Build Blockers**.

In this mode the plugin automatically evaluates **all Build Orders with status Production**. Pending, On Hold, Complete and Cancelled Build Orders are excluded.

## Blocker rule

For each BO line:

`Remaining Requirement = Required Qty - Consumed Qty - Allocated Qty`

A line remains a blocker only when all of the following are true:

- direct available stock is less than the remaining requirement;
- no single acceptable variant has enough stock to cover the remaining requirement; and
- no single acceptable substitute has enough stock to cover the remaining requirement.

Direct, variant and substitute quantities are never mixed together to clear one line. Stock from two different variants or two different substitutes is also never combined.

## Combined Production mode stock handling

The combined report uses a shared virtual stock pool. When one Production BO line is fully covered by one exact Part, that quantity is virtually consumed for the report so the same free stock cannot also clear another Production BO line.

The virtual allocation is reporting-only; the plugin does not create or change InvenTree allocations.

Direct stock is preferred. If more than one alternate can fully cover a requirement, the report uses the smallest sufficient alternate stock pool first to preserve larger pools for later lines.

PO information remains informational and is calculated against the base direct Part shortage. The plugin does not allocate Purchase Order quantities between multiple BO lines.

## Export options

- **Blockers Only**: default Yes.
- **Include Optional BOM Items**: default No.
- **Include Pending / On-Hold POs**: default Yes.

## Version

0.2.0
