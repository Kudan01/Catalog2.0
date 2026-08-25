from __future__ import annotations

from dataclasses import dataclass
from typing import Any


UPDATE_PLAN_VERSION = 3


@dataclass(frozen=True)
class UpdatePlanPhase:
    """One logical phase of the shared update workflow/result payload."""

    key: str
    category: str
    enabled: bool
    writes_catalog_db: bool = False
    writes_cache_files: bool = False
    writes_source_media: bool = False
    requires_confirmation: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "category": self.category,
            "enabled": self.enabled,
            "writes": {
                "catalog_db": self.writes_catalog_db,
                "cache_files": self.writes_cache_files,
                "source_media": self.writes_source_media,
            },
            "requires_confirmation": self.requires_confirmation,
        }


@dataclass(frozen=True)
class UpdatePlan:
    """Shared update workflow skeleton for full-catalog and branch updates."""

    source_action: str
    scope_type: str
    scope_rel_path: str
    phases: tuple[UpdatePlanPhase, ...]
    current_behavior_only: bool = False
    affected_scopes: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "update_plan": True,
            "version": UPDATE_PLAN_VERSION,
            "source_action": self.source_action,
            "scope": {
                "type": self.scope_type,
                "rel_path": self.scope_rel_path,
            },
            "current_behavior_only": self.current_behavior_only,
            "phases": [phase.to_dict() for phase in self.phases],
            "affected_scopes": self.affected_scopes,
            "rules": {
                "scan_activate_generates_thumbnails": False,
                "bulk_regenerate_all_thumbnails": False,
                "delete_source_media": False,
                "delete_protected_cache_without_cache_plan": False,
            },
        }


def build_update_plan(
    *,
    source_action: str,
    scope_rel_path: str = "",
    preview_decision: bool,
    media_previews: bool,
    folder_previews: bool,
    affected_scopes: dict[str, Any] | None = None,
) -> UpdatePlan:
    """Build a language-neutral technical plan for catalog update entrypoints.

    The payload contains stable identifiers and write flags only. User-facing
    labels belong to locale files and must not be embedded in this module.
    """
    normalized_scope = (scope_rel_path or "").strip("/")
    scope_type = "branch" if normalized_scope else "full_catalog"

    phases = [
        UpdatePlanPhase(
            key="scan_stage",
            category="database",
            enabled=True,
            writes_catalog_db=True,
        ),
        UpdatePlanPhase(
            key="user_decision",
            category="workflow",
            enabled=preview_decision,
            requires_confirmation=preview_decision,
        ),
        UpdatePlanPhase(
            key="scan_activate",
            category="database",
            enabled=True,
            writes_catalog_db=True,
        ),
        UpdatePlanPhase(
            key="media_previews",
            category="preview_cache",
            enabled=media_previews,
            writes_catalog_db=media_previews,
            writes_cache_files=media_previews,
        ),
        UpdatePlanPhase(
            key="folder_previews",
            category="preview_cache",
            enabled=folder_previews,
            writes_catalog_db=folder_previews,
            writes_cache_files=folder_previews,
        ),
        UpdatePlanPhase(
            key="cache_cleanup",
            category="cache_management",
            enabled=False,
        ),
    ]

    return UpdatePlan(
        source_action=source_action,
        scope_type=scope_type,
        scope_rel_path=normalized_scope,
        phases=tuple(phases),
        affected_scopes=affected_scopes,
    )


def build_catalog_update_plan_dict(
    *,
    source_action: str = "catalog-update",
    affected_scopes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Full-catalog database update plan; preview preparation is separate."""
    return build_update_plan(
        source_action=source_action,
        scope_rel_path="",
        preview_decision=True,
        media_previews=False,
        folder_previews=False,
        affected_scopes=affected_scopes,
    ).to_dict()


def build_branch_update_plan_dict(
    *,
    branch_rel_path: str,
    source_action: str = "update-branch",
    affected_scopes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Branch database update plan; preview preparation is separate."""
    return build_update_plan(
        source_action=source_action,
        scope_rel_path=branch_rel_path,
        preview_decision=False,
        media_previews=False,
        folder_previews=False,
        affected_scopes=affected_scopes,
    ).to_dict()
