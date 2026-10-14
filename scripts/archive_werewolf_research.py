import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def archive(report_path, output):
    report = json.loads(report_path.read_text())
    sources = report["source_manifests"]
    if len(sources) != 1:
        raise ValueError("confirmation does not have one frozen source snapshot")
    source_id, manifest = next(iter(sources.items()))
    payload = {}
    for relative, expected in manifest["files"].items():
        path = Path(relative)
        resolved = (ROOT / path).resolve()
        if path.is_absolute() or ".." in path.parts or any(part.startswith(".env") for part in path.parts) \
                or not resolved.is_relative_to(ROOT.resolve()):
            raise ValueError("unsafe source path")
        data = resolved.read_bytes()
        if hashlib.sha256(data).hexdigest() != expected:
            raise ValueError(f"frozen source changed: {relative}")
        payload[relative] = data
    if output.exists():
        raise ValueError("refuse to overwrite a source archive")
    with zipfile.ZipFile(output, "x", zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for relative, data in sorted(payload.items()):
            bundle.writestr(relative, data)
        bundle.writestr("FROZEN_SOURCE_MANIFEST.json", json.dumps(manifest, indent=2))
    with zipfile.ZipFile(output) as bundle:
        if bundle.testzip() is not None:
            raise ValueError("archive CRC verification failed")
        for relative, expected in manifest["files"].items():
            if hashlib.sha256(bundle.read(relative)).hexdigest() != expected:
                raise ValueError("archive source hash mismatch")
    return {"created": datetime.now().astimezone().isoformat(), "source_digest": source_id,
            "files": len(payload), "archive": str(output), "archive_sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
            "scope": "Exact confirmation source files, not later exploratory additions, logs, credentials, model weights or a standalone deployment bundle"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, default=ROOT / "docs/werewolf_overnight_confirmation.json")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--index", type=Path, required=True)
    args = parser.parse_args()
    if args.index.exists():
        parser.error("refuse to overwrite archive index")
    result = archive(args.report, args.out)
    args.index.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
