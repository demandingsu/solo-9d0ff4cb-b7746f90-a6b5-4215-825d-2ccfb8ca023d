"""Application service layer: uploads, previews, publishing and versioning.

Versioning model
----------------
* Each dataset has an ``input_version`` counter.  Uploading a *different*
  mapping, forecast or published file increments it.
* Every preview records the input version and the content hashes of the
  revisions it was built from.  Publishing re-checks all of those, so a preview
  built on older inputs can never be published ("旧预览不得发布").
* Failed reconciliations never create a preview or snapshot, and never modify
  the previous published snapshot ("校正失败时保留上一版结果").
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
from dataclasses import dataclass
from typing import Any

from . import db as db_module
from .actuals import evaluate_actuals
from .csv_io import parse_actuals, parse_forecast, parse_mapping, parse_published
from .errors import (
    ActualsValidationError,
    CsvParseError,
    PublishError,
    ReconciliationError,
    SnapshotValidationError,
    StalePreviewError,
)
from .hierarchy import build_hierarchy
from .reconcile import (
    ReconciliationResult,
    reconcile,
    verify_consistency,
    verify_hierarchy,
)

KIND_LABELS = {
    "mapping": "层级映射",
    "line_forecast": "产线预测",
    "published": "已发布车间/工厂预测",
}


def payload_hash(payload: Any) -> str:
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class UploadOutcome:
    kind: str
    changed: bool
    input_version: int
    row_count: int
    filename: str


class ReconciliationService:
    def __init__(self, database: db_module.Database) -> None:
        self.db = database

    # ------------------------------------------------------------------
    # Datasets
    # ------------------------------------------------------------------
    def create_dataset(self, name: str) -> db_module.Dataset:
        return self.db.create_dataset(name.strip() or "未命名数据集")

    def dataset_state(self, dataset_id: int) -> dict[str, Any]:
        dataset = self.db.get_dataset(dataset_id)
        if dataset is None:
            raise PublishError("数据集不存在。")
        revisions = self.db.latest_revisions(dataset_id)
        snapshots = self.db.list_snapshots(dataset_id)
        preview = self.db.latest_preview(dataset_id)
        current_hashes = {k: v["content_hash"] for k, v in revisions.items()}
        preview_state = None
        if preview is not None:
            preview_state = {
                "id": preview["id"],
                "created_at": preview["created_at"],
                "input_version": preview["input_version"],
                "superseded": preview["superseded"],
                "stale": self._preview_is_stale(preview, current_hashes, dataset.input_version),
                "result": preview["result"],
            }
        return {
            "dataset": {
                "id": dataset.id,
                "name": dataset.name,
                "created_at": dataset.created_at,
                "input_version": dataset.input_version,
            },
            "revisions": revisions,
            "snapshots": snapshots,
            "latest_snapshot": self.db.get_snapshot(dataset_id),
            "preview": preview_state,
            "events": self.db.list_events(dataset_id),
        }

    @staticmethod
    def _preview_is_stale(
        preview: dict[str, Any], current_hashes: dict[str, str], input_version: int
    ) -> bool:
        if preview.get("superseded"):
            return True
        if preview.get("input_version") != input_version:
            return True
        request_hashes = (preview.get("request") or {}).get("content_hashes", {})
        return request_hashes != current_hashes

    # ------------------------------------------------------------------
    # Uploads
    # ------------------------------------------------------------------
    def upload(
        self,
        dataset_id: int,
        kind: str,
        filename: str,
        data: bytes,
    ) -> UploadOutcome:
        if kind not in KIND_LABELS:
            raise PublishError(f"未知的上传类型 {kind!r}。")

        # Parsing and validation happen before touching the database so a bad
        # file leaves the previous revision (and published result) intact.
        try:
            payload = self._parse_upload(kind, filename, data)
            if kind == "mapping":
                build_hierarchy_from_payload(payload)  # validate eagerly
        except ReconciliationError as exc:
            self._log(dataset_id, "upload_rejected", f"{KIND_LABELS[kind]}上传被拒绝：{exc}")
            raise

        content_hash = payload_hash(payload)

        with self.db.connect() as conn:
            dataset = self.db.get_dataset(dataset_id, conn)
            if dataset is None:
                raise PublishError("数据集不存在。")
            previous = self.db.latest_revisions(dataset_id, conn).get(kind)
            changed = previous is None or previous["content_hash"] != content_hash
            if not changed:
                self.db.log_event(
                    conn,
                    dataset_id,
                    "upload_unchanged",
                    f"{KIND_LABELS[kind]}内容与当前版本相同，未产生新版本。",
                    {"filename": filename},
                )
                return UploadOutcome(
                    kind, False, dataset.input_version, payload["row_count"], filename
                )
            new_version = self.db.bump_input_version(conn, dataset_id)
            self.db.add_revision(
                conn, dataset_id, new_version, kind, filename, content_hash, payload
            )
            self.db.log_event(
                conn,
                dataset_id,
                "upload",
                f"上传{KIND_LABELS[kind]} {filename}，输入版本更新为 v{new_version}。",
                {"kind": kind, "rows": payload["row_count"]},
            )
        return UploadOutcome(kind, True, new_version, payload["row_count"], filename)

    @staticmethod
    def _parse_upload(kind: str, filename: str, data: bytes) -> dict[str, Any]:
        if not data:
            raise CsvParseError(f"文件 {filename} 为空。")
        if kind == "mapping":
            rows = parse_mapping(data, filename)
            return {
                "rows": [
                    {
                        "node_id": r.node_id,
                        "parent_id": r.parent_id,
                        "level": r.level,
                        "name": r.name,
                        "weight": r.weight if r.weight is not None else 1.0,
                    }
                    for r in rows
                ],
                "row_count": len(rows),
            }
        if kind == "line_forecast":
            rows = parse_forecast(data, filename)
            return {
                "rows": [
                    {"node_id": r.node_id, "period": r.period, "value": r.value} for r in rows
                ],
                "row_count": len(rows),
            }
        rows = parse_published(data, filename)
        return {
            "rows": [
                {"target": r.target, "kind": r.kind, "period": r.period, "value": r.value}
                for r in rows
            ],
            "row_count": len(rows),
        }

    # ------------------------------------------------------------------
    # Preview (reconciliation)
    # ------------------------------------------------------------------
    def preview(self, dataset_id: int) -> dict[str, Any]:
        # The whole read -> compute -> store sequence runs inside one write
        # transaction.  An upload racing with this call therefore either lands
        # before (and is included) or after (and marks the preview stale), never
        # in between, so a preview can never claim an input version it did not
        # actually use.
        try:
            with self.db.connect() as conn:
                dataset = self.db.get_dataset(dataset_id, conn)
                if dataset is None:
                    raise PublishError("数据集不存在。")
                revisions = self.db.latest_revisions(dataset_id, conn)
                missing = [
                    KIND_LABELS[k]
                    for k in ("mapping", "line_forecast")
                    if k not in revisions
                ]
                if missing:
                    raise SnapshotValidationError(
                        "缺少必要输入：" + "、".join(missing) + "。"
                    )

                mapping_payload = revisions["mapping"]["payload"]
                forecast_payload = revisions["line_forecast"]["payload"]
                published_payload = revisions.get("published", {}).get(
                    "payload", {"rows": [], "row_count": 0}
                )
                content_hashes = {k: v["content_hash"] for k, v in revisions.items()}

                hierarchy = build_hierarchy_from_payload(mapping_payload)
                result = reconcile(
                    hierarchy,
                    forecast_payload["rows"],
                    published_payload["rows"],
                )
                problems = verify_consistency(result)
                if problems:
                    raise SnapshotValidationError(
                        "校正结果未通过一致性校验：" + "; ".join(problems[:5])
                    )

                request = {
                    "content_hashes": content_hashes,
                    "revisions": {k: v["id"] for k, v in revisions.items()},
                }
                result_dict = result.to_dict()
                preview_id = self.db.create_preview(
                    conn, dataset_id, dataset.input_version, request, result_dict
                )
                self.db.log_event(
                    conn,
                    dataset_id,
                    "preview",
                    f"生成校正预览 #{preview_id}（输入版本 v{dataset.input_version}）。",
                    {"periods": len(result.periods)},
                )
                return {"preview_id": preview_id, "result": result_dict}
        except ReconciliationError as exc:
            # Failure: nothing is stored; the last published snapshot survives.
            # `connect()` already rolled the transaction back.
            self._log(
                dataset_id,
                "preview_failed",
                f"校正失败：{exc}",
                {"error_code": exc.code},
            )
            raise

    # ------------------------------------------------------------------
    # Publish
    # ------------------------------------------------------------------
    def publish(self, dataset_id: int, preview_id: int) -> dict[str, Any]:
        try:
            with self.db.connect() as conn:
                dataset = self.db.get_dataset(dataset_id, conn)
                if dataset is None:
                    raise PublishError("数据集不存在。")
                preview = self._get_preview(dataset_id, preview_id, conn)
                revisions = self.db.latest_revisions(dataset_id, conn)
                current_hashes = {k: v["content_hash"] for k, v in revisions.items()}

                if preview["superseded"]:
                    raise StalePreviewError("该预览已被新的输入版本取代，不能发布。")
                if preview["input_version"] != dataset.input_version:
                    raise StalePreviewError(
                        f"该预览基于输入版本 v{preview['input_version']}，"
                        f"当前为 v{dataset.input_version}，请重新校正。"
                    )
                request_hashes = (preview["request"] or {}).get("content_hashes", {})
                if request_hashes != current_hashes:
                    raise StalePreviewError(
                        "输入文件已变更，旧预览不得发布，请重新校正。"
                    )

                # Re-hydrate and re-verify the stored result before publishing:
                # this catches corrupted data and guarantees the hierarchy
                # constraints hold even if the preview was created by an older
                # build.
                result = ReconciliationResult.from_dict(preview["result"])
                problems = verify_hierarchy(result.hierarchy) + verify_consistency(result)
                if problems:
                    raise SnapshotValidationError(
                        "预览结果不满足层级一致性，拒绝发布：" + "; ".join(problems[:5])
                    )
                if not result.periods:
                    raise SnapshotValidationError("预览不包含任何时段，拒绝发布。")

                published = self.db.publish(
                    conn,
                    dataset_id,
                    preview["input_version"],
                    preview["result"],
                )
                self.db.log_event(
                    conn,
                    dataset_id,
                    "publish",
                    f"发布快照 v{published['version_number']}（输入版本 v{preview['input_version']}）。",
                    {"preview_id": preview_id},
                )
        except ReconciliationError as exc:
            self._log(
                dataset_id,
                "publish_rejected",
                f"发布被拒绝（预览 #{preview_id}）：{exc}",
                {"error_code": exc.code},
            )
            raise
        return {
            "snapshot_id": published["id"],
            "version_number": published["version_number"],
        }

    # ------------------------------------------------------------------
    # Actual production reporting against a published snapshot
    # ------------------------------------------------------------------
    def submit_actuals(
        self,
        dataset_id: int,
        version_number: int,
        period: str,
        filename: str,
        data: bytes,
    ) -> dict[str, Any]:
        """Merge one batch of actuals into a snapshot period and reconcile.

        The whole batch is merged with the already stored actuals *before* any
        rule is checked, so a later correction of a line is judged in the
        context of the full state.  On any rule violation the transaction is
        rolled back: the previously stored actuals and reconciliation stay
        exactly as they were.  Success updates both the detail and the error
        summary atomically.
        """
        snapshot = self.db.get_snapshot(dataset_id, version_number)
        if snapshot is None:
            raise PublishError("快照不存在。")
        result = ReconciliationResult.from_dict(snapshot["result"])
        period_result = next((p for p in result.periods if p.period == period), None)
        if period_result is None:
            raise ActualsValidationError(
                f"时段 {period} 不在快照 v{version_number} 中。"
            )
        hierarchy = result.hierarchy
        line_ids = set(hierarchy.by_level.get("line", []))
        shop_ids = set(hierarchy.by_level.get("shop", []))

        try:
            rows = parse_actuals(data, filename)
        except ReconciliationError as exc:
            self._record_actuals_rejection(
                dataset_id, version_number, period, filename, 0, exc
            )
            raise
        if not rows:
            raise ActualsValidationError("实产文件没有数据行。")

        # Validate row ids before opening a write transaction.
        unknown: list[str] = []
        wrong_level: list[str] = []
        for row in rows:
            if row.node_id in line_ids:
                continue
            if row.node_id in shop_ids:
                continue
            if row.node_id in hierarchy.nodes:
                wrong_level.append(row.node_id)
            else:
                unknown.append(row.node_id)
        try:
            if unknown:
                raise ActualsValidationError(
                    "实产文件包含映射里不存在的车间/产线："
                    + ", ".join(sorted(set(unknown))[:10]),
                    {"unknown_nodes": sorted(set(unknown))},
                )
            if wrong_level:
                raise ActualsValidationError(
                    "实产只能报车间或产线，以下节点层级不符："
                    + ", ".join(sorted(set(wrong_level))[:10]),
                    {"wrong_level_nodes": sorted(set(wrong_level))},
                )

            with self.db.connect() as conn:
                batch_id, reconciliation, message = self._apply_actuals_batch(
                    conn, dataset_id, version_number, period, filename, rows,
                    hierarchy, line_ids, period_result,
                )
                self.db.log_event(
                    conn,
                    dataset_id,
                    "actuals_applied",
                    f"快照 v{version_number} 时段 {period} 实产批次 #{batch_id} 已应用：{message}",
                    {"filename": filename, "rows": len(rows)},
                )
        except ActualsValidationError as exc:
            # The transaction rolled back, so actual_values and the stored
            # reconciliation are exactly as before.  Record the rejection in a
            # separate transaction so it can never resurrect a failed merge.
            self._record_actuals_rejection(
                dataset_id, version_number, period, filename, len(rows), exc
            )
            raise
        return {
            "batch_id": batch_id,
            "version_number": version_number,
            "period": period,
            "message": message,
            "reconciliation": reconciliation,
        }

    def _record_actuals_rejection(
        self,
        dataset_id: int,
        version_number: int,
        period: str,
        filename: str,
        row_count: int,
        exc: ReconciliationError,
    ) -> None:
        details = getattr(exc, "details", None)
        try:
            with self.db.connect() as conn:
                self.db.record_actual_batch(
                    conn,
                    dataset_id,
                    version_number,
                    period,
                    filename,
                    "rejected",
                    str(exc),
                    row_count,
                    details,
                )
                self.db.log_event(
                    conn,
                    dataset_id,
                    "actuals_rejected",
                    f"快照 v{version_number} 时段 {period} 实产批次被拒绝：{exc}",
                    {
                        "error_code": exc.code,
                        "filename": filename,
                        "conflicts": (details or {}).get("conflicts", []),
                    },
                )
        except Exception:  # pragma: no cover - recording must not mask the error
            pass

    def _apply_actuals_batch(
        self,
        conn: Any,
        dataset_id: int,
        version_number: int,
        period: str,
        filename: str,
        rows: list[Any],
        hierarchy: Any,
        line_ids: set[str],
        period_result: Any,
    ) -> tuple[int, dict[str, Any], str]:
        """Merge + validate + store one batch inside an existing transaction.

        Validation happens before any write; a raised ``ActualsValidationError``
        rolls the whole transaction back, leaving stored actuals and the prior
        reconciliation unchanged.
        """
        stored = self.db.actual_values(dataset_id, version_number, period, conn)
        line_reports: dict[str, int] = {}
        shop_totals: dict[str, int] = {}
        for node_id, value in stored.items():
            if value["kind"] == "line":
                line_reports[node_id] = value["quantity"]
            else:
                shop_totals[node_id] = value["quantity"]

        # Last row wins for a repeated id: a later row/batch is a correction.
        batch_values: dict[str, tuple[str, int]] = {}
        for row in rows:
            kind = "line" if row.node_id in line_ids else "shop"
            batch_values[row.node_id] = (kind, row.quantity)
        for node_id, (kind, quantity) in batch_values.items():
            if kind == "line":
                line_reports[node_id] = quantity
            else:
                shop_totals[node_id] = quantity

        # Validate the merged state before writing anything.
        evaluation = evaluate_actuals(
            hierarchy,
            period_result.base,
            period_result.corrected,
            line_reports,
            shop_totals,
        )

        row_count = len(rows)
        batch_id = self.db.record_actual_batch(
            conn,
            dataset_id,
            version_number,
            period,
            filename,
            "applied",
            "已应用",
            row_count,
            {"nodes": sorted(batch_values)},
        )
        self.db.upsert_actual_values(
            conn, dataset_id, version_number, period, batch_values, batch_id
        )
        reconciliation = {
            "period": period,
            "version_number": version_number,
            "items": evaluation["items"],
            "shops": evaluation["shops"],
            "missing_lines": evaluation["missing_lines"],
            "reported_lines": evaluation["reported_lines"],
            "reported_shops": evaluation["reported_shops"],
            "totals": evaluation["totals"],
            "actuals": {
                **dict(sorted(line_reports.items())),
                **dict(sorted(shop_totals.items())),
            },
        }
        self.db.save_actual_reconciliation(
            conn, dataset_id, version_number, period, reconciliation
        )
        message = (
            f"已合并 {row_count} 行实产并完成对账："
            f"{len(reconciliation['items'])} 个对账项，"
            f"缺数产线 {len(reconciliation['missing_lines'])} 条。"
        )
        conn.execute(
            "UPDATE actual_batches SET message=? WHERE id=?", (message, batch_id)
        )
        return batch_id, reconciliation, message

    def actuals_state(self, dataset_id: int, version_number: int) -> dict[str, Any] | None:
        """All actuals and reconciliations for a snapshot, for the page."""
        snapshot = self.db.get_snapshot(dataset_id, version_number)
        if snapshot is None:
            return None
        result = ReconciliationResult.from_dict(snapshot["result"])
        periods = [p.period for p in result.periods]
        stored_recon = self.db.list_actual_reconciliations(dataset_id, version_number)
        return {
            "snapshot": snapshot,
            "result": result,
            "periods": periods,
            "reconciliations": {
                period: stored_recon.get(period) for period in periods
            },
            "batches": {
                period: self.db.list_actual_batches(dataset_id, version_number, period)
                for period in periods
            },
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _get_preview(self, dataset_id: int, preview_id: int, conn: Any) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM previews WHERE id=? AND dataset_id=?", (preview_id, dataset_id)
        ).fetchone()
        if row is None:
            raise PublishError("预览不存在。")
        return {
            "id": row["id"],
            "dataset_id": row["dataset_id"],
            "input_version": row["input_version"],
            "created_at": row["created_at"],
            "superseded": bool(row["superseded"]),
            "request": json.loads(row["request_json"]),
            "result": json.loads(row["result_json"]),
        }

    def _log(self, dataset_id: int, kind: str, message: str, detail: Any = None) -> None:
        try:
            with self.db.connect() as conn:
                self.db.log_event(conn, dataset_id, kind, message, detail)
        except Exception:  # pragma: no cover - logging must never mask the error
            pass


def build_hierarchy_from_payload(payload: dict[str, Any]):
    from .csv_io import MappingRow

    rows = [
        MappingRow(
            node_id=r["node_id"],
            parent_id=r.get("parent_id"),
            level=r.get("level"),
            name=r.get("name"),
            weight=r.get("weight"),
        )
        for r in payload["rows"]
    ]
    return build_hierarchy(rows)


def result_to_csv(result: ReconciliationResult) -> str:
    """Long-format CSV with base, corrected and delta for every node."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        ["period", "node_id", "level", "node_name", "parent_id", "base", "corrected", "delta"]
    )
    for period in result.periods:
        for node_id in sorted(result.hierarchy.nodes):
            node = result.hierarchy.nodes[node_id]
            writer.writerow(
                [
                    period.period,
                    node_id,
                    node.level,
                    node.name,
                    node.parent_id or "",
                    f"{period.base[node_id]:.6f}",
                    f"{period.corrected[node_id]:.6f}",
                    f"{period.deltas.get(node_id, 0.0):.6f}",
                ]
            )
    return buffer.getvalue()


def adjustments_to_csv(result: ReconciliationResult) -> str:
    """Per-period adjustment provenance, one row per adjusted node."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "period",
            "node_id",
            "level",
            "node_name",
            "base",
            "corrected",
            "delta",
            "source",
            "source_nodes",
            "path",
        ]
    )
    for period in result.periods:
        for adj in period.node_adjustments:
            writer.writerow(
                [
                    period.period,
                    adj["node_id"],
                    adj["level"],
                    adj["name"],
                    f"{adj['base']:.6f}",
                    f"{adj['corrected']:.6f}",
                    f"{adj['delta']:.6f}",
                    adj["source"],
                    ";".join(adj.get("source_nodes", []))
                    if adj.get("source_nodes")
                    else adj["source"],
                    " > ".join(adj["path"]),
                ]
            )
    return buffer.getvalue()


def conflicts_to_csv(result: ReconciliationResult) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["period", "scope", "level", "base_total", "published", "difference"])
    for period in result.periods:
        for conflict in period.conflicts:
            writer.writerow(
                [
                    period.period,
                    conflict["scope"],
                    conflict["level"],
                    f"{conflict['base']:.6f}",
                    f"{conflict['published']:.6f}",
                    f"{conflict['diff']:.6f}",
                ]
            )
    return buffer.getvalue()


def actuals_reconciliation_to_csv(
    reconciliations: list[tuple[str, dict[str, Any] | None]]
) -> str:
    """Per-item actuals reconciliation with both forecast sets and errors."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(
        [
            "period",
            "kind",
            "node_id",
            "label",
            "actual",
            "base_forecast",
            "corrected_forecast",
            "base_error",
            "corrected_error",
            "covered_lines",
        ]
    )
    for period, stored in reconciliations:
        if stored is None:
            continue
        for item in stored["result"]["items"]:
            writer.writerow(
                [
                    period,
                    item["kind"],
                    item["node_id"],
                    item["label"],
                    item["actual"],
                    f"{item['base_forecast']:.6f}",
                    f"{item['corrected_forecast']:.6f}",
                    f"{item['base_error']:.6f}",
                    f"{item['corrected_error']:.6f}",
                    ";".join(item["covered_lines"]),
                ]
            )
    return buffer.getvalue()
