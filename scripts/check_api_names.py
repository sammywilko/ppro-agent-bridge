#!/usr/bin/env python3
"""Static check: every `ppro.X.y(` / `ppro.X.Y.z` / `.method(` used in plugin/index.js must exist in Adobe's typings.

Not a type checker — a typo net. Run: python3 scripts/check_api_names.py
Exit 1 on any unknown name.
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DTS = ROOT / "vendor/package/src/premierepro.d.ts"
JS = ROOT / "plugin/index.js"

dts = DTS.read_text()
js = JS.read_text()

# ---- names declared in the d.ts
types = set(re.findall(r"export declare type (\w+)", dts))
methods = set(re.findall(r"^\s+(\w+)\s*\(", dts, flags=re.M))          # methods in type bodies
props = set(re.findall(r"^\s+(?:readonly )?(\w+)\s*:", dts, flags=re.M))  # properties in type bodies
enums = set(re.findall(r"export enum (\w+)", dts))
enum_members = set(re.findall(r"^\s+(\w+),\s*$", dts, flags=re.M))
root_members = set(re.findall(r"^\s+(\w+): (\w+)(?:Static)?;", dts[: dts.find("};")], flags=re.M)) and set(re.findall(r"^\s+(\w+):", dts[: dts.find("};")], flags=re.M))

problems = []

# ---- ppro.<Root>.<member>
for root, member in set(re.findall(r"\bppro\.(\w+)\.(\w+)", js)):
    if root not in root_members:
        problems.append(f"ppro.{root} is not a root member of premierepro")
        continue
    if root == "Constants":
        if member not in enums:
            problems.append(f"ppro.Constants.{member} is not an enum")
        continue
    static_type = f"{root}Static"
    body_match = re.search(rf"export declare type {static_type} = \{{(.*?)\n\}};", dts, flags=re.S)
    static_body = body_match.group(1) if body_match else ""
    if not re.search(rf"^\s+(?:readonly )?{member}\b", static_body, flags=re.M):
        # some roots are instance types (Application, Component…) — check the plain type body too
        plain = re.search(rf"export declare type {root} = \{{(.*?)\n\}};", dts, flags=re.S)
        plain_body = plain.group(1) if plain else ""
        if not re.search(rf"^\s+(?:readonly )?{member}\b", plain_body, flags=re.M):
            problems.append(f"ppro.{root}.{member} not found on {static_type} or {root}")

# ---- ppro.Constants.<Enum>.<MEMBER>
for enum, member in set(re.findall(r"\bppro\.Constants\.(\w+)\.(\w+)", js)):
    if member not in enum_members:
        problems.append(f"ppro.Constants.{enum}.{member} is not an enum member")

# ---- generic .method( calls that look like Premiere API (camelCase get*/is*/create*/import*/set*/execute*)
api_like = set(re.findall(r"\.((?:get|is|create|import|set|execute|open|save|add|remove|find|cast|close|clear)\w*)\s*\(", js))
known_js = {  # DOM / JS built-ins, or covered by the root-member check above
    "getElementById", "getItem", "setItem", "getTime", "addEventListener", "getMarkers",
    "createElement", "removeChild", "find", "isArray", "isFinite", "isInteger", "setInterval", "setTimeout", "get",
}
for m in sorted(api_like):
    if m in known_js:
        continue
    if m not in methods:
        problems.append(f".{m}( is not a method in the typings")

# ---- property reads that look like API (.name/.path/.guid/.type/.seconds/.ticks/.id/.empty)
for p in sorted(set(re.findall(r"\.(seconds|ticks|ticksNumber|guid|path|empty|nativePath)\b", js))):
    if p not in props and p != "nativePath":
        problems.append(f".{p} is not a property in the typings")

print(f"checked {len(api_like)} API-like calls against {len(methods)} typed methods")
if problems:
    print("\n".join("  ✗ " + p for p in problems))
    sys.exit(1)
print("API names: CLEAN")
