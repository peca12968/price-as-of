"""Count physical content files retained by Iceberg v1/v2 snapshots in one metadata revision."""
import argparse
from collections import defaultdict
import json

from pyiceberg.table import StaticTable
from pyiceberg.manifest import DataFileContent


def footprint(table, candidates):
    if table.metadata.format_version not in (1, 2):
        raise ValueError("Expected Iceberg v1/v2 metadata")
    snapshots = {s.snapshot_id: s for s in table.snapshots()}
    candidates = set(candidates)
    unknown = candidates - snapshots.keys()
    if unknown:
        raise ValueError(f"Unknown snapshot IDs: {sorted(unknown)}")
    refs = defaultdict(list)
    for name, ref in table.refs().items():
        refs[ref.snapshot_id].append(name)
    current = table.current_snapshot()
    if current and "main" not in refs[current.snapshot_id]:
        refs[current.snapshot_id].append("main")
    excluded = candidates.intersection(refs)
    removing = candidates - excluded
    manifests = {}
    owners = defaultdict(set)
    properties = {}
    for sid, snapshot in snapshots.items():
        for manifest in snapshot.manifests(table.io):
            if manifest.manifest_path not in manifests:
                paths = set()
                for entry in manifest.fetch_manifest_entry(table.io, discard_deleted=True):
                    item = entry.data_file
                    info = (item.file_size_in_bytes, DataFileContent(item.content).name)
                    if properties.setdefault(item.file_path, info) != info:
                        raise ValueError(f"Inconsistent physical file metadata for {item.file_path}")
                    paths.add(item.file_path)
                manifests[manifest.manifest_path] = paths
            for path in manifests[manifest.manifest_path]:
                owners[path].add(sid)
    files = []
    for path, pinned_by in sorted(owners.items()):
        if not pinned_by.intersection(candidates):
            continue
        size, kind = properties[path]
        files.append({"path": path, "bytes": size, "content": kind, "owners": sorted(pinned_by),
                      "surviving_owners": sorted(pinned_by - removing)})
    return {"metadata": table.metadata_location, "candidate_ids": sorted(candidates), "simulated_removal": sorted(removing),
            "protected": [{"id": sid, "refs": sorted(refs[sid])} for sid in sorted(excluded)],
            "snapshots": [{"id": sid, "parent_id": snap.parent_snapshot_id, "refs": sorted(refs.get(sid, []))}
                          for sid, snap in snapshots.items()],
            "candidate_file_bytes": sum(row["bytes"] for row in files),
            "unreferenced_file_bytes": sum(row["bytes"] for row in files if not row["surviving_owners"]),
            "files": files}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("metadata", help="URI of a specific Iceberg metadata JSON file")
    parser.add_argument("snapshots", type=int, nargs="*", help="Snapshots to simulate removing; omit to list IDs")
    args = parser.parse_args()
    try:
        print(json.dumps(footprint(StaticTable.from_metadata(args.metadata), args.snapshots), indent=2))
    except (OSError, ValueError) as exc:
        parser.exit(2, f"{exc}\n")
