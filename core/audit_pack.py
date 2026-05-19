"""Daily audit-pack builder."""

from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import date
from pathlib import Path
from typing import Iterable


def build_audit_pack(*, trading_day: date, reports_dir: Path, data_files: Iterable[Path], output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    pack_path = output_dir / f"hawksoptions_audit_{trading_day.isoformat()}.zip"
    manifest: dict[str, str] = {}
    archive_root = reports_dir.parent
    candidates = _audit_candidates(trading_day=trading_day, reports_dir=reports_dir, data_files=data_files)
    with zipfile.ZipFile(pack_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in candidates:
            if not path.is_file():
                continue
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            arcname = _archive_name(path, root=archive_root)
            manifest[arcname] = digest
            archive.write(path, arcname)
        manifest_bytes = json.dumps(manifest, indent=2, sort_keys=True).encode("utf-8")
        archive.writestr("manifest.sha256.json", manifest_bytes)
    return pack_path


def _audit_candidates(*, trading_day: date, reports_dir: Path, data_files: Iterable[Path]) -> list[Path]:
    day_text = trading_day.isoformat()
    candidates = [path for path in data_files if path.exists()]
    if reports_dir.exists():
        for pattern in (
            f"candidate_scans/scan_{day_text}_*.json",
            f"research_traces/research_trace_{day_text}_*.json",
            f"ai_disagreements/ai_disagreements_{day_text}_*.json",
            f"reconciliation/reconciliation_{trading_day:%Y%m%d}-*.json",
            f"eod_{day_text}.md",
        ):
            candidates.extend(sorted(reports_dir.glob(pattern)))
    return candidates


def _archive_name(path: Path, *, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(Path("external") / path.name)
