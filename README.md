# InvenTree Build Blockers

InvenTree data-export plugin which adds a blocker-oriented export to the Build Order required-parts / line-items table.

## Blocker calculation (v0.1.1)

The remaining requirement is calculated as:

`Remaining Requirement = Required Qty - Consumed Qty - Allocated Qty`

A row is a blocker only when all three conditions are true:

- Direct available stock is less than the remaining requirement.
- No single acceptable variant has enough available stock to satisfy the remaining requirement.
- No single acceptable substitute has enough available stock to satisfy the remaining requirement.

Stock from different variants, substitutes, or the direct part is never pooled to clear a blocker.

The export retains InvenTree's aggregate Available Variant Stock and Available Substitute Stock fields and adds Max Single Variant Stock and Max Single Substitute Stock for auditability.

Purchase-order coverage remains informational and does not remove a current production blocker.
