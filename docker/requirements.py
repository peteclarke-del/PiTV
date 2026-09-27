"""Print the runtime dependencies of the pyproject.toml files named, one per line, for pip."""

import sys
import tomllib

seen: list[str] = []
for path in sys.argv[1:]:
    with open(path, "rb") as f:
        for dep in tomllib.load(f)["project"].get("dependencies", []):
            if dep not in seen:
                seen.append(dep)
print("\n".join(seen))
