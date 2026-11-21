"""Actual production reporting and reconciliation against a snapshot.

Rules implemented here (the whole batch is merged first, then checked):

* Rows carry a shop or line id and a non-negative integer quantity.
* A line that reported actuals is reconciled on its own: its actual figure is
  compared with its forecast.
* When a shop reports a total, the actuals of its reported lines are deducted
  from that total.  The remainder is reconciled against the *sum of forecasts*
  of the lines that have not reported yet.
* When a shop does not report a total, its unreported lines are marked as
  missing data (缺数); they take part in neither error sum.
* A batch is rejected as a whole when any shop's remainder would be negative,
  or when all of a shop's lines reported but their sum differs from the shop
  total.  The conflicting shops are listed.

The same reconciliation items are used for the before/after error comparison:
``base`` is the uploaded (pre-correction) forecast, ``corrected`` is the
published snapshot's reconciled forecast, and both error sums are the sum of
``abs(actual - forecast)`` over exactly the same items.
"""

from __future__ import annotations

from typing import Any

from .errors import ActualsValidationError
from .hierarchy import Hierarchy

ITEM_LINE = "line"
ITEM_SHOP_REMAINDER = "shop_remainder"

STATUS_TOTAL_REPORTED = "total_reported"
STATUS_FULLY_REPORTED = "fully_reported"
STATUS_NO_TOTAL = "no_total"


def _shop_lines(hierarchy: Hierarchy, shop_id: str) -> list[str]:
    """Lines under a shop in presentation (tree) order."""
    return [
        line
        for line in hierarchy.by_level.get("line", [])
        if hierarchy.nodes[line].parent_id == shop_id
    ]


def _line_name(hierarchy: Hierarchy, line_id: str) -> str:
    node = hierarchy.nodes[line_id]
    return f"{line_id} {node.name}" if node.name and node.name != line_id else line_id


def _q(value: float) -> float:
    """Round reported figures to 6 decimals to hide solver numerical noise."""
    return round(float(value), 6)


def evaluate_actuals(
    hierarchy: Hierarchy,
    base: dict[str, float],
    corrected: dict[str, float],
    line_reports: dict[str, int],
    shop_totals: dict[str, int],
) -> dict[str, Any]:
    """Check a merged actuals state and build reconciliation items.

    Raises :class:`ActualsValidationError` with ``details["conflicts"]`` when
    the state is infeasible.  Pure function; the caller decides about storage.
    """
    conflicts: list[dict[str, Any]] = []
    for shop_id in hierarchy.by_level.get("shop", []):
        if shop_id not in shop_totals:
            continue
        lines = _shop_lines(hierarchy, shop_id)
        reported = [line for line in lines if line in line_reports]
        unreported = [line for line in lines if line not in line_reports]
        reported_sum = sum(line_reports[line] for line in reported)
        total = shop_totals[shop_id]
        remainder = total - reported_sum
        if not unreported and remainder != 0:
            conflicts.append(
                {
                    "shop_id": shop_id,
                    "shop_name": hierarchy.nodes[shop_id].name,
                    "reason": "fully_reported_mismatch",
                    "message": (
                        f"车间 {shop_id} 产线已报齐，但实产合计 {reported_sum} "
                        f"与车间总量 {total} 不符（差 {remainder:+d}）。"
                    ),
                    "total": total,
                    "reported_line_sum": reported_sum,
                    "remainder": remainder,
                }
            )
        elif remainder < 0:
            conflicts.append(
                {
                    "shop_id": shop_id,
                    "shop_name": hierarchy.nodes[shop_id].name,
                    "reason": "negative_remainder",
                    "message": (
                        f"车间 {shop_id} 余额为负：总量 {total}，"
                        f"已报产线实产合计 {reported_sum}。"
                    ),
                    "total": total,
                    "reported_line_sum": reported_sum,
                    "remainder": remainder,
                }
            )
    if conflicts:
        raise ActualsValidationError(
            "实产批次被拒绝：" + "；".join(c["message"] for c in conflicts),
            {"conflicts": conflicts},
        )

    items: list[dict[str, Any]] = []
    shops: list[dict[str, Any]] = []
    missing_lines: list[str] = []

    for shop_id in hierarchy.by_level.get("shop", []):
        lines = _shop_lines(hierarchy, shop_id)
        reported = [line for line in lines if line in line_reports]
        unreported = [line for line in lines if line not in line_reports]
        total = shop_totals.get(shop_id)
        if total is None:
            # Without a shop total the unreported lines have no figure to
            # reconcile against, so they are explicitly marked as missing.
            missing_lines.extend(unreported)

        # Reported lines are always reconciled individually.
        for line in reported:
            actual = line_reports[line]
            base_value = float(base[line])
            corrected_value = float(corrected[line])
            items.append(
                {
                    "kind": ITEM_LINE,
                    "node_id": line,
                    "level": "line",
                    "label": _line_name(hierarchy, line),
                    "actual": actual,
                    "base_forecast": _q(base_value),
                    "corrected_forecast": _q(corrected_value),
                    "base_error": _q(abs(actual - base_value)),
                    "corrected_error": _q(abs(actual - corrected_value)),
                    "covered_lines": [line],
                    "covered_names": [_line_name(hierarchy, line)],
                    "shop_id": shop_id,
                }
            )

        remainder = None
        if total is not None:
            remainder = total - sum(line_reports[line] for line in reported)
            if unreported:
                # The remainder covers the lines that have not reported yet.
                remainder_base = sum(float(base[line]) for line in unreported)
                remainder_corrected = sum(float(corrected[line]) for line in unreported)
                items.append(
                    {
                        "kind": ITEM_SHOP_REMAINDER,
                        "node_id": shop_id,
                        "level": "shop",
                        "label": f"{shop_id} 余额（未报产线预测合计）",
                        "actual": remainder,
                        "base_forecast": _q(remainder_base),
                        "corrected_forecast": _q(remainder_corrected),
                        "base_error": _q(abs(remainder - remainder_base)),
                        "corrected_error": _q(abs(remainder - remainder_corrected)),
                        "covered_lines": list(unreported),
                        "covered_names": [_line_name(hierarchy, line) for line in unreported],
                        "shop_id": shop_id,
                        "shop_name": hierarchy.nodes[shop_id].name,
                    }
                )
            status = STATUS_TOTAL_REPORTED if unreported else STATUS_FULLY_REPORTED
        else:
            status = STATUS_NO_TOTAL

        shops.append(
            {
                "shop_id": shop_id,
                "shop_name": hierarchy.nodes[shop_id].name,
                "status": status,
                "total": total,
                "reported_line_sum": sum(line_reports[line] for line in reported),
                "remainder": remainder,
                "reported_lines": reported,
                "unreported_lines": unreported,
                "missing_lines": unreported,
            }
        )

    base_error = _q(sum(item["base_error"] for item in items))
    corrected_error = _q(sum(item["corrected_error"] for item in items))
    return {
        "items": items,
        "shops": shops,
        "missing_lines": missing_lines,
        "reported_lines": [
            line for line in hierarchy.by_level.get("line", []) if line in line_reports
        ],
        "reported_shops": [
            shop for shop in hierarchy.by_level.get("shop", []) if shop in shop_totals
        ],
        "totals": {
            "item_count": len(items),
            "base_error": base_error,
            "corrected_error": corrected_error,
            "improvement": base_error - corrected_error,
        },
    }
