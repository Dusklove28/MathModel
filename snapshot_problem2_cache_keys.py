"""Save a portable evaluation-key summary without copying a large cache."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from candidate_manager_problem1 import sha256_file
from candidate_manager_problem2 import json_sha256
from candidate_manager_problem2_stage3 import cache_key_summary
from contest_io import _read_json


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def snapshot(run_root: Path) -> Dict[str, Any]:
    run_root = Path(run_root).resolve()
    identity_path = run_root / "run_identity.json"
    summary_path = run_root / "summary.json"
    if not identity_path.is_file() or not summary_path.is_file():
        raise ValueError("run root lacks run_identity.json or summary.json")
    identity = _read_json(identity_path)
    group_files: List[Path] = []
    groups: List[Dict[str, Any]] = []
    for path in sorted((run_root / "groups").glob("*/k*/*.json")):
        value = _read_json(path)
        if not isinstance(value, dict) or not str(value.get("kind", "")).endswith(
                "_group"):
            continue
        group_files.append(path)
        groups.append(value)
    if not groups:
        raise ValueError("run root has no group manifests")
    result = cache_key_summary(groups)
    group_hashes = [
        {
            "path": path.relative_to(run_root).as_posix(),
            "sha256": sha256_file(path),
        }
        for path in group_files
    ]
    result.update({
        "source_run_root": str(run_root),
        "source_kind": _read_json(summary_path).get("kind"),
        "source_group_count": len(groups),
        "source_run_identity_sha256": identity.get("run_identity_sha256"),
        "source_run_identity_file_sha256": sha256_file(identity_path),
        "source_summary_file_sha256": sha256_file(summary_path),
        "source_group_manifest_set_sha256": json_sha256(group_hashes),
        "source_cache_path_recorded": identity.get("cache"),
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    value = snapshot(args.run)
    _write_json(args.output, value)
    print(json.dumps({
        "groups": value["source_group_count"],
        "evaluation_key_references": value["evaluation_key_references"],
        "unique_evaluation_keys": value["unique_evaluation_keys"],
        "cache_hit_references": value["cache_hit_references"],
        "evaluation_key_set_sha256": value["evaluation_key_set_sha256"],
        "output": str(args.output.resolve()),
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

