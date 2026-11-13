"""Install bundled, versioned prompts into the existing runtime locations.

Existing files are never overwritten. Run once on a new installation before
starting the knowledge worker; existing deployments can continue unchanged.
"""

import argparse
import hashlib
import os
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFINITIONS = (
    ("literature-three-class-v1.json", "var/experiments/three-class-20261022/task-definition-v1.json",
     "b3cf5804bad572c86aa7e30fb88a0a7e04322f60bbfdaddaa53983be3100b61b"),
    ("entity-lean-v1.json", "var/experiments/lean-entities-20261029/definition.json",
     "1f0b83f037ab55ac161d397d35c1d7649ee137200db1a951b8741bf81f60c35e"),
)


def sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def install(destination=ROOT, *, source=ROOT / "config/definitions"):
    destination, source = Path(destination), Path(source)
    for bundled, relative, expected in DEFINITIONS:
        current = destination / relative
        if sha256(source / bundled) != expected:
            raise ValueError(f"Bundled definition does not match its version: {bundled}")
        if current.is_symlink() or (current.exists() and sha256(current) != expected):
            raise ValueError(f"Existing runtime definition differs; preserve it: {relative}")

    results = []
    for bundled, relative, expected in DEFINITIONS:
        target = destination / relative
        if target.exists():
            results.append((relative, "reused"))
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=".definition-", dir=target.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write((source / bundled).read_bytes())
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary, 0o644)
            try:
                os.link(temporary, target)
                results.append((relative, "installed"))
            except FileExistsError:
                if target.is_symlink() or sha256(target) != expected:
                    raise ValueError(f"Concurrent runtime definition differs: {relative}") from None
                results.append((relative, "reused"))
        finally:
            Path(temporary).unlink(missing_ok=True)
    return results


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    for path, state in install(args.root):
        print(f"{state}: {path}")


if __name__ == "__main__":
    main()
