"""Build blocker export plugin for InvenTree."""

from decimal import Decimal, InvalidOperation

from rest_framework import serializers

from plugin import InvenTreePlugin
from plugin.mixins import DataExportMixin

from build.models import BuildLine
from build.serializers import BuildLineSerializer
from order.models import PurchaseOrderLineItem

try:
    from order.status_codes import PurchaseOrderStatus, PurchaseOrderStatusGroups

    PO_OPEN_STATUSES = PurchaseOrderStatusGroups.OPEN
    PO_PLACED_STATUS = PurchaseOrderStatus.PLACED.value
except ImportError:
    # Compatibility fallback for older InvenTree installations.
    PO_OPEN_STATUSES = [10, 20, 25]
    PO_PLACED_STATUS = 20

from . import PLUGIN_VERSION


class BuildBlockerExporterOptionsSerializer(serializers.Serializer):
    """Options displayed in the export dialog."""

    blockers_only = serializers.BooleanField(
        default=True,
        label="Blockers Only",
        help_text=(
            "Only export components which cannot currently be satisfied "
            "by consumed quantity, allocation, or available stock."
        ),
    )

    include_optional = serializers.BooleanField(
        default=False,
        label="Include Optional BOM Items",
        help_text="Allow optional BOM items to appear as blockers.",
    )

    include_all_open_po_statuses = serializers.BooleanField(
        default=True,
        label="Include Pending / On-Hold POs",
        help_text=(
            "Include all open purchase orders. If disabled, only Placed "
            "purchase orders are considered."
        ),
    )


class BuildBlockers(InvenTreePlugin, DataExportMixin):
    """Export Build Order component blockers with PO target dates."""

    TITLE = "Build Blockers"
    NAME = "BuildBlockers"
    SLUG = "build-blockers"
    DESCRIPTION = (
        "Export current Build Order component blockers with open "
        "purchase-order quantities and expected dates."
    )
    VERSION = PLUGIN_VERSION

    AUTHOR = "Per Vices Corporation"
    LICENSE = "MIT"

    ExportOptionsSerializer = BuildBlockerExporterOptionsSerializer

    def supports_export(self, model_class: type, user, *args, **kwargs) -> bool:
        """Expose this exporter only for Build Order line-item exports."""
        serializer_class = kwargs.get("serializer_class")
        return model_class == BuildLine and serializer_class == BuildLineSerializer

    def update_headers(self, headers, context, **kwargs):
        """Set blocker-specific export headers."""
        headers.clear()

        headers["build_reference"] = "Build Order"
        headers["ipn"] = "IPN"
        headers["part_name"] = "Part Name"
        headers["part_description"] = "Description"
        headers["required_quantity"] = "Required Qty"
        headers["consumed_quantity"] = "Consumed Qty"
        headers["allocated_quantity"] = "Allocated Qty"
        headers["available_stock"] = "Available Stock"
        headers["available_substitute_stock"] = "Available Substitute Stock"
        headers["available_variant_stock"] = "Available Variant Stock"
        headers["uncovered_quantity"] = "Blocker Qty"

        headers["po_outstanding_quantity"] = "Open PO Qty"
        headers["po_coverage_quantity"] = "PO Qty Applied"
        headers["po_remaining_shortage"] = "Shortage After Open POs"
        headers["earliest_po_date"] = "Earliest PO Target Date"
        headers["full_coverage_po_date"] = "Expected Full Coverage Date"
        headers["po_references"] = "Purchase Orders"
        headers["po_detail"] = "PO Detail"

        headers["blocker"] = "Blocking"

        return headers

    @staticmethod
    def _decimal(value):
        """Convert model / serializer values to Decimal safely."""
        if value in (None, ""):
            return Decimal("0")

        try:
            return Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            return Decimal("0")

    @staticmethod
    def _effective_po_date(line):
        """Use line target date, falling back to parent PO target date."""
        return line.target_date or getattr(line.order, "target_date", None)

    def _purchase_order_data(
        self,
        part_id,
        shortage,
        include_all_open_statuses=True,
    ):
        """Return open PO supply for the required base part."""
        if not part_id:
            return {
                "outstanding": Decimal("0"),
                "coverage": Decimal("0"),
                "remaining": shortage,
                "earliest_date": None,
                "full_coverage_date": None,
                "references": "",
                "detail": "",
            }

        statuses = (
            PO_OPEN_STATUSES
            if include_all_open_statuses
            else [PO_PLACED_STATUS]
        )

        lines = (
            PurchaseOrderLineItem.objects.filter(
                part__part_id=part_id,
                order__status__in=statuses,
            )
            .select_related("order", "part", "part__supplier")
            .order_by(
                "target_date",
                "order__target_date",
                "order__reference",
                "pk",
            )
        )

        po_rows = []
        total_outstanding = Decimal("0")

        for line in lines:
            quantity = self._decimal(line.quantity)
            received = self._decimal(getattr(line, "received", 0))
            outstanding = max(Decimal("0"), quantity - received)

            if outstanding <= 0:
                continue

            effective_date = self._effective_po_date(line)
            total_outstanding += outstanding

            po_rows.append(
                {
                    "reference": getattr(line.order, "reference", ""),
                    "supplier": getattr(
                        getattr(line.part, "supplier", None),
                        "name",
                        "",
                    ),
                    "outstanding": outstanding,
                    "date": effective_date,
                    "status": getattr(line.order, "status_text", ""),
                }
            )

        dated_rows = sorted(
            [row for row in po_rows if row["date"]],
            key=lambda row: row["date"],
        )

        earliest_date = dated_rows[0]["date"] if dated_rows else None

        covered = Decimal("0")
        full_coverage_date = None

        for row in dated_rows:
            covered += row["outstanding"]
            if shortage > 0 and covered >= shortage:
                full_coverage_date = row["date"]
                break

        coverage = min(shortage, total_outstanding)
        remaining = max(Decimal("0"), shortage - total_outstanding)

        references = ", ".join(
            dict.fromkeys(
                row["reference"]
                for row in po_rows
                if row["reference"]
            )
        )

        detail_parts = []

        for row in po_rows:
            date_text = row["date"].isoformat() if row["date"] else "NO DATE"
            supplier = f" / {row['supplier']}" if row["supplier"] else ""
            status = f" / {row['status']}" if row["status"] else ""

            detail_parts.append(
                f"{row['reference']}: {row['outstanding']} due {date_text}"
                f"{supplier}{status}"
            )

        return {
            "outstanding": total_outstanding,
            "coverage": coverage,
            "remaining": remaining,
            "earliest_date": earliest_date,
            "full_coverage_date": full_coverage_date,
            "references": references,
            "detail": "; ".join(detail_parts),
        }

    def export_data(
        self,
        queryset,
        serializer_class,
        headers,
        context,
        output,
        **kwargs,
    ):
        """Generate blocker rows."""
        blockers_only = context.get("blockers_only", True)
        include_optional = context.get("include_optional", False)
        include_all_open_po_statuses = context.get(
            "include_all_open_po_statuses",
            True,
        )

        queryset = queryset.select_related(
            "build",
            "bom_item",
            "bom_item__sub_part",
            "bom_item__sub_part__category",
        )

        rows = []

        for build_line in queryset:
            data = serializer_class(build_line, exporting=True).data

            optional = bool(data.get("optional", False))

            if optional and not include_optional:
                continue

            required = self._decimal(data.get("quantity"))
            consumed = self._decimal(data.get("consumed"))
            allocated = self._decimal(data.get("allocated"))
            available = self._decimal(data.get("available_stock"))
            substitute_stock = self._decimal(
                data.get("available_substitute_stock")
            )
            variant_stock = self._decimal(
                data.get("available_variant_stock")
            )

            remaining_requirement = max(
                Decimal("0"),
                required - consumed - allocated,
            )

            uncovered = max(
                Decimal("0"),
                remaining_requirement - available,
            )

            if blockers_only and uncovered <= 0:
                continue

            part = build_line.bom_item.sub_part

            po = self._purchase_order_data(
                part.pk,
                uncovered,
                include_all_open_statuses=include_all_open_po_statuses,
            )

            rows.append(
                {
                    "build_reference": getattr(
                        build_line.build,
                        "reference",
                        "",
                    ),
                    "ipn": getattr(part, "IPN", ""),
                    "part_name": getattr(part, "name", ""),
                    "part_description": getattr(
                        part,
                        "description",
                        "",
                    ),
                    "required_quantity": required,
                    "consumed_quantity": consumed,
                    "allocated_quantity": allocated,
                    "available_stock": available,
                    "available_substitute_stock": substitute_stock,
                    "available_variant_stock": variant_stock,
                    "uncovered_quantity": uncovered,
                    "po_outstanding_quantity": po["outstanding"],
                    "po_coverage_quantity": po["coverage"],
                    "po_remaining_shortage": po["remaining"],
                    "earliest_po_date": (
                        po["earliest_date"].isoformat()
                        if po["earliest_date"]
                        else ""
                    ),
                    "full_coverage_po_date": (
                        po["full_coverage_date"].isoformat()
                        if po["full_coverage_date"]
                        else ""
                    ),
                    "po_references": po["references"],
                    "po_detail": po["detail"],
                    "blocker": "YES" if uncovered > 0 else "NO",
                }
            )

        return rows
