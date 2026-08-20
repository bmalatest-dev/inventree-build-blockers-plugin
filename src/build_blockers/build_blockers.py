"""Build blocker export plugin for InvenTree."""

from decimal import Decimal, InvalidOperation

from rest_framework import serializers

from plugin import InvenTreePlugin
from plugin.mixins import DataExportMixin

from build.models import Build, BuildLine
from build.serializers import BuildLineSerializer

try:
    from build.status_codes import BuildStatus
    BUILD_PRODUCTION_STATUS = BuildStatus.PRODUCTION.value
except ImportError:
    # Compatibility fallback for older InvenTree installations.
    BUILD_PRODUCTION_STATUS = 20
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
            "by consumed quantity, allocation, direct stock, or a single approved alternate."
        ),
    )

    include_optional = serializers.BooleanField(
        default=False,
        label="Include Optional BOM Items",
        help_text="Allow optional BOM items to appear as blockers.",
    )

    all_production_builds = serializers.BooleanField(
        default=False,
        label="All Production Build Orders",
        help_text=(
            "Ignore the currently opened Build Order and run the blocker report "
            "against all Build Orders whose status is Production."
        ),
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
        """Expose this exporter only for Build Order required-part lines.

        The export dialog itself provides an option to run against every
        Production Build Order. Keeping the exporter attached only to the
        BuildLine endpoint avoids relying on the Build-list export hook.
        """
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
        headers["max_single_substitute_stock"] = "Max Single Substitute Stock"
        headers["max_single_variant_stock"] = "Max Single Variant Stock"
        headers["uncovered_quantity"] = "Blocker Qty"

        headers["po_outstanding_quantity"] = "Open PO Qty"
        headers["po_coverage_quantity"] = "PO Qty Applied"
        headers["po_remaining_shortage"] = "Shortage After Open POs"
        headers["earliest_po_date"] = "Earliest PO Target Date"
        headers["full_coverage_po_date"] = "Expected Full Coverage Date"
        headers["po_references"] = "Purchase Orders"
        headers["po_detail"] = "PO Detail"

        headers["coverage_source"] = "Coverage Source"
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


    def _exact_part_available_stock(self, part, build=None):
        """Return available stock for one exact Part, never pooling variants.

        This intentionally mirrors InvenTree's basic availability semantics:
        in-stock quantity less existing build / sales allocations. If the Build
        Order has a take-from location, stock quantity is restricted to that
        location tree.
        """
        if part is None:
            return Decimal("0")

        location = getattr(build, "take_from", None) if build else None
        entries = part.stock_entries(
            include_variants=False,
            in_stock=True,
            location=location,
        )
        total = sum(
            (self._decimal(item.quantity) for item in entries),
            Decimal("0"),
        )
        allocated = self._decimal(
            part.allocation_count(include_variants=False)
        )
        return max(Decimal("0"), total - allocated)

    def _single_alternate_stock(self, build_line):
        """Return the best independently usable variant and substitute stock.

        Stock is evaluated per exact Part. Quantities from two different
        variants or substitutes are never added together.
        """
        bom_item = build_line.bom_item
        base_part = bom_item.sub_part
        build = build_line.build

        max_variant = Decimal("0")
        if bom_item.allow_variants:
            for variant in base_part.get_descendants(include_self=False):
                max_variant = max(
                    max_variant,
                    self._exact_part_available_stock(variant, build),
                )

        max_substitute = Decimal("0")
        for substitute_link in bom_item.substitutes.select_related("part").all():
            substitute = substitute_link.part
            max_substitute = max(
                max_substitute,
                self._exact_part_available_stock(substitute, build),
            )

            # InvenTree permits variants of an explicit substitute when the
            # BOM line itself allows variants. Treat each such Part as its own
            # independent option; never pool them with the substitute parent.
            if bom_item.allow_variants:
                for sub_variant in substitute.get_descendants(include_self=False):
                    max_substitute = max(
                        max_substitute,
                        self._exact_part_available_stock(sub_variant, build),
                    )

        return max_variant, max_substitute

    def _alternate_parts(self, build_line):
        """Return independently acceptable alternate Parts for a BOM line.

        The returned list contains exact Part objects. Stock from different
        Parts is never pooled to satisfy one requirement.
        """
        bom_item = build_line.bom_item
        base_part = bom_item.sub_part

        variants = []
        substitutes = []

        if bom_item.allow_variants:
            variants.extend(base_part.get_descendants(include_self=False))

        for substitute_link in bom_item.substitutes.select_related("part").all():
            substitute = substitute_link.part
            substitutes.append(substitute)

            if bom_item.allow_variants:
                substitutes.extend(
                    substitute.get_descendants(include_self=False)
                )

        # Preserve order while removing duplicate Part PKs.
        def unique(parts):
            seen = set()
            result = []
            for part in parts:
                if part.pk in seen:
                    continue
                seen.add(part.pk)
                result.append(part)
            return result

        return unique(variants), unique(substitutes)

    def _pool_quantity(self, pool, part, build=None):
        """Return a cached free-stock quantity for one exact Part."""
        if part is None:
            return Decimal("0")

        # take_from can make availability Build-specific, so include it in
        # the pool key. Normally this is None and stock is globally shared.
        location = getattr(build, "take_from", None) if build else None
        location_pk = getattr(location, "pk", None)
        key = (part.pk, location_pk)

        if key not in pool:
            pool[key] = self._exact_part_available_stock(part, build)

        return pool[key]

    def _consume_pool(self, pool, part, quantity, build=None):
        """Virtually consume exact-Part stock from the combined report pool."""
        location = getattr(build, "take_from", None) if build else None
        location_pk = getattr(location, "pk", None)
        key = (part.pk, location_pk)
        pool[key] = max(
            Decimal("0"),
            self._pool_quantity(pool, part, build) - quantity,
        )

    def _combined_source_check(self, build_line, remaining_requirement, pool):
        """Check one Production BO line against a shared virtual stock pool.

        A requirement may be covered by exactly one source: the direct Part,
        one acceptable variant, or one acceptable substitute. Sources are not
        mixed. When a source covers a line, its stock is virtually consumed so
        it cannot also clear another Production Build Order.
        """
        if remaining_requirement <= 0:
            return {
                "blocking": False,
                "direct": Decimal("0"),
                "max_variant": Decimal("0"),
                "max_substitute": Decimal("0"),
                "source": "Already consumed / allocated",
            }

        part = build_line.bom_item.sub_part
        build = build_line.build
        variants, substitutes = self._alternate_parts(build_line)

        direct = self._pool_quantity(pool, part, build)
        variant_rows = [
            (candidate, self._pool_quantity(pool, candidate, build))
            for candidate in variants
        ]
        substitute_rows = [
            (candidate, self._pool_quantity(pool, candidate, build))
            for candidate in substitutes
        ]

        max_variant = max(
            (qty for _, qty in variant_rows),
            default=Decimal("0"),
        )
        max_substitute = max(
            (qty for _, qty in substitute_rows),
            default=Decimal("0"),
        )

        # Prefer the direct Part. For alternates, use the smallest single stock
        # pool that can fully cover the line (best-fit), preserving larger pools
        # for later BO lines where possible.
        chosen = None
        source_label = ""

        if direct >= remaining_requirement:
            chosen = part
            source_label = "Direct"
        else:
            eligible_variants = [
                row for row in variant_rows if row[1] >= remaining_requirement
            ]
            eligible_substitutes = [
                row for row in substitute_rows if row[1] >= remaining_requirement
            ]

            eligible = [
                (candidate, qty, "Variant")
                for candidate, qty in eligible_variants
            ] + [
                (candidate, qty, "Substitute")
                for candidate, qty in eligible_substitutes
            ]

            if eligible:
                candidate, _, source_type = min(
                    eligible,
                    key=lambda row: (row[1], row[0].pk),
                )
                chosen = candidate
                source_label = f"{source_type}: {getattr(candidate, 'IPN', '') or candidate.pk}"

        if chosen is not None:
            self._consume_pool(pool, chosen, remaining_requirement, build)
            return {
                "blocking": False,
                "direct": direct,
                "max_variant": max_variant,
                "max_substitute": max_substitute,
                "source": source_label,
            }

        return {
            "blocking": True,
            "direct": direct,
            "max_variant": max_variant,
            "max_substitute": max_substitute,
            "source": "",
        }

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

    def _build_row(
        self,
        build_line,
        serializer_class,
        include_all_open_po_statuses,
        combined_pool=None,
    ):
        """Build one report row, or return None for an excluded optional line."""
        data = serializer_class(build_line, exporting=True).data

        required = self._decimal(data.get("quantity"))
        consumed = self._decimal(data.get("consumed"))
        allocated = self._decimal(data.get("allocated"))
        available = self._decimal(data.get("available_stock"))
        substitute_stock = self._decimal(data.get("available_substitute_stock"))
        variant_stock = self._decimal(data.get("available_variant_stock"))

        remaining_requirement = max(
            Decimal("0"),
            required - consumed - allocated,
        )

        if combined_pool is None:
            max_variant_stock, max_substitute_stock = self._single_alternate_stock(
                build_line
            )
            direct_covers = available >= remaining_requirement
            variant_covers = max_variant_stock >= remaining_requirement
            substitute_covers = max_substitute_stock >= remaining_requirement
            blocking = (
                remaining_requirement > 0
                and not direct_covers
                and not variant_covers
                and not substitute_covers
            )
            coverage_source = ""
            if remaining_requirement <= 0:
                coverage_source = "Already consumed / allocated"
            elif direct_covers:
                coverage_source = "Direct"
            elif variant_covers:
                coverage_source = "Variant"
            elif substitute_covers:
                coverage_source = "Substitute"
        else:
            combined = self._combined_source_check(
                build_line,
                remaining_requirement,
                combined_pool,
            )
            blocking = combined["blocking"]
            available = combined["direct"]
            max_variant_stock = combined["max_variant"]
            max_substitute_stock = combined["max_substitute"]
            coverage_source = combined["source"]

        # Preserve the existing PO calculation: PO coverage is for the base
        # direct Part shortage only, and is informational rather than stock.
        uncovered = max(
            Decimal("0"),
            remaining_requirement - available,
        )

        part = build_line.bom_item.sub_part
        po = self._purchase_order_data(
            part.pk,
            uncovered,
            include_all_open_statuses=include_all_open_po_statuses,
        )

        return {
            "build_reference": getattr(build_line.build, "reference", ""),
            "ipn": getattr(part, "IPN", ""),
            "part_name": getattr(part, "name", ""),
            "part_description": getattr(part, "description", ""),
            "required_quantity": required,
            "consumed_quantity": consumed,
            "allocated_quantity": allocated,
            "available_stock": available,
            "available_substitute_stock": substitute_stock,
            "available_variant_stock": variant_stock,
            "max_single_substitute_stock": max_substitute_stock,
            "max_single_variant_stock": max_variant_stock,
            "uncovered_quantity": uncovered,
            "po_outstanding_quantity": po["outstanding"],
            "po_coverage_quantity": po["coverage"],
            "po_remaining_shortage": po["remaining"],
            "earliest_po_date": (
                po["earliest_date"].isoformat() if po["earliest_date"] else ""
            ),
            "full_coverage_po_date": (
                po["full_coverage_date"].isoformat()
                if po["full_coverage_date"]
                else ""
            ),
            "po_references": po["references"],
            "po_detail": po["detail"],
            "coverage_source": coverage_source,
            "blocker": "YES" if blocking else "NO",
            "_blocking": blocking,
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
        """Generate blocker rows for one BO or all Production BOs."""
        blockers_only = context.get("blockers_only", True)
        include_optional = context.get("include_optional", False)
        include_all_open_po_statuses = context.get(
            "include_all_open_po_statuses",
            True,
        )
        all_production_builds = context.get("all_production_builds", False)

        if all_production_builds:
            # "Open" for this report means exactly the standard Production
            # status, rather than all active / outstanding Build statuses.
            production_builds = Build.objects.filter(
                status=BUILD_PRODUCTION_STATUS,
            )

            line_queryset = BuildLine.objects.filter(
                build__in=production_builds,
            )
            line_serializer = BuildLineSerializer
            combined_pool = {}
        else:
            line_queryset = queryset
            line_serializer = serializer_class
            combined_pool = None

        line_queryset = (
            line_queryset.select_related(
                "build",
                "bom_item",
                "bom_item__sub_part",
                "bom_item__sub_part__category",
            )
            .prefetch_related(
                "bom_item__substitutes__part",
            )
            .order_by("build__reference", "pk")
        )

        rows = []

        for build_line in line_queryset:
            data = line_serializer(build_line, exporting=True).data
            optional = bool(data.get("optional", False))
            if optional and not include_optional:
                continue

            row = self._build_row(
                build_line,
                line_serializer,
                include_all_open_po_statuses,
                combined_pool=combined_pool,
            )

            if blockers_only and not row["_blocking"]:
                continue

            row.pop("_blocking", None)
            rows.append(row)

        return rows
