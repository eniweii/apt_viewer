#!/usr/bin/env python3
"""
Runtime-bound images of the Undercover apt movies.

  python runtime_images.py <UI folder> <apt_report.json> <apt_engine_map.json> [<attrib dump.json> <hashes_main.txt>] [<out folder>]

An image entry without a name (id 65535, "(no name entry)" in apt_report.json) is a placeholder. The scripts bind a texture to the clip
that shows it with `clip.TextureHash = <bStringHash of a texture name>`. This tool lists, for every movie:
  - the placeholder images (count, what uses them),
  - every `.TextureHash = value` binding in the decompiled scripts and where the value comes from
    (the game message SETTEXTUREHASH, a provider call from the engine map with the attrib classes it reaches, or a script variable),
and, from the attrib dump, the texture names the game can feed (hints and tips pictures, event icons, lens flares ...) with their bStringHash,
so a viewer can resolve a bound hash through the texture index.
Writes runtime_images.json, runtime_images.md and runtime_texture_names.json.
"""
import collections, glob, json, os, re, sys

sys.path[:0] = [os.path.dirname(os.path.abspath(__file__)), os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."),
                os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "common")]


def bstring_hash(text):
    """bStringHash (the BIN key hash): h = 0xFFFFFFFF; h = h * 33 + byte."""
    h = 0xFFFFFFFF
    for b in text.encode("latin1", "replace"):
        h = (h * 33 + b) & 0xFFFFFFFF
    return h


def decompiled(folder):
    import apt_movie, inventory
    out = {}
    for p in sorted(glob.glob(folder.rstrip("/\\") + "/*.big")):
        out[os.path.basename(p)] = inventory.movie_text(inventory.inventory_movie(apt_movie.load(p), log=lambda *a: None))
    return out


def classify(value, providers):
    v = value.strip()
    if "msg.textureHash" in v:
        return {"source": "game message SETTEXTUREHASH (msg.textureHash)"}
    m = re.search(r"_global\.(\w+)\.(\w+)\(", v)
    if m and m.group(2) in providers.get(m.group(1), {}):
        info = providers[m.group(1)][m.group(2)]
        return {"source": "provider %s.%s" % (m.group(1), m.group(2)), "attrib": info.get("attrib", {})}
    if m:
        return {"source": "provider %s.%s (not in the engine map)" % (m.group(1), m.group(2))}
    if re.fullmatch(r"-?\d+|0x[0-9A-Fa-f]+", v):
        return {"source": "constant"}
    return {"source": "script value", "expression": v[:100]}


def texture_names(dump_path, names_path):
    """Texture names the attrib data can hand to the UI: {source: [{name, hash}]}."""
    sys.argv = ["x"]
    import engine_map
    names = engine_map.load_names(names_path)
    dump = json.load(open(dump_path))
    nm = lambda h: names.get(int(h, 16), h) if isinstance(h, str) and h.startswith("0x") else h
    out = collections.defaultdict(dict)

    def walk(node, source):
        if isinstance(node, dict):
            for k, v in node.items():
                key = nm(k)
                if key in ("picture", "Texture_Name", "EventIconType", "IconModelName") and isinstance(v, str):
                    out[source + "." + key][v] = bstring_hash(v)
                else:
                    walk(v, source)
        elif isinstance(node, list):
            for x in node:
                walk(x, source)
    for c in dump["classes"]:
        cn = nm(c["name"])
        if cn in ("fe_hints_tips", "lensflare", "gameplay", "frontend"):
            for col in c["collections"]:
                walk(col["values"], cn)
    return {k: [{"name": n, "hash": "0x%08X" % h} for n, h in sorted(v.items())] for k, v in out.items()}


def main():
    folder, report_path, map_path = sys.argv[1:4]
    dump = sys.argv[4] if len(sys.argv) > 5 else None
    names = sys.argv[5] if len(sys.argv) > 5 else None
    out_dir = sys.argv[6] if len(sys.argv) > 6 else "."
    report = json.load(open(report_path))
    emap = json.load(open(map_path))["objects"]
    providers = {o: v["members"] for o, v in emap.items()}
    texts = decompiled(folder)
    movies, bind_total = {}, 0
    for m in report["movies"]:
        unnamed = [i for i in m["images"] if not i.get("texture")]
        sites = []
        for target, value in re.findall(r"([\w.\[\]\"]+)\.TextureHash = ([^;\n]+);", texts.get(m["file"], "")):
            sites.append({"target": target, "value": value.strip(), **classify(value, providers)})
        if unnamed or sites:
            movies[m["file"]] = {"placeholder_images": len(unnamed), "used_by": sorted({i.get("used_by") or "?" for i in unnamed}),
                                 "bindings": sites}
            bind_total += len(sites)
    result = {"movies": movies, "summary": {"movies_with_placeholders": sum(1 for v in movies.values() if v["placeholder_images"]),
                                            "placeholder_images": sum(v["placeholder_images"] for v in movies.values()),
                                            "binding_sites": bind_total}}
    json.dump(result, open(os.path.join(out_dir, "runtime_images.json"), "w"), indent=1)
    lines = ["# Runtime-bound images", "", "%(movies_with_placeholders)d movies, %(placeholder_images)d placeholder images, %(binding_sites)d binding sites in scripts." % result["summary"], "",
             "| movie | placeholders | binding (target = value) | source |", "|---|---|---|---|"]
    for f, v in sorted(movies.items()):
        if not v["bindings"]:
            lines.append("| %s | %d | (none in its own scripts: bound by game messages or code in another movie) | |" % (f, v["placeholder_images"]))
        for b in v["bindings"]:
            lines.append("| %s | %d | `%s = %s` | %s |" % (f, v["placeholder_images"], b["target"], b["value"][:50], b["source"]))
    open(os.path.join(out_dir, "runtime_images.md"), "w").write("\n".join(lines) + "\n")
    if dump and names:
        tn = texture_names(dump, names)
        json.dump(tn, open(os.path.join(out_dir, "runtime_texture_names.json"), "w"), indent=1)
        print({k: len(v) for k, v in tn.items()})
    print(result["summary"])


if __name__ == "__main__":
    main()
