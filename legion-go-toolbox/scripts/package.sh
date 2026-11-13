#!/usr/bin/env bash
# Builds the frontend and creates an installable Decky plugin zip in release/.
set -euo pipefail
cd "$(dirname "$0")/.."

pnpm install --frozen-lockfile
pnpm run build

python3 - <<'PY'
import json, os, zipfile

root = "legion-go-toolbox"
version = json.load(open("package.json"))["version"]
files = ["plugin.json", "package.json", "main.py", "LICENSE", "README.md",
         "dist/index.js"]
for base, _, names in os.walk("py_modules"):
    if "__pycache__" in base:
        continue
    files += [os.path.join(base, n) for n in sorted(names) if n.endswith(".py")]

os.makedirs("release", exist_ok=True)
target = os.path.join("release", "legion-go-toolbox.zip")
with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as z:
    for f in sorted(files):
        info = zipfile.ZipInfo(root + "/" + f.replace(os.sep, "/"),
                               date_time=(2024, 1, 1, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        with open(f, "rb") as src:
            z.writestr(info, src.read())
print("Created %s (v%s, %d files)" % (target, version, len(files)))
PY
