"""Read-only: dump block_entities / entities / nested items matching a filter
from a SPECIFIC region file, keyed by global chunk coords. For comparing
1.20 source vs 1.21 target on the exact example chunks. Never writes.

Usage:
  python inspect_chunk.py <world_dir> <region|entities|poi> <gx> <gz> <id_substring> [--items]

<gx> <gz> are GLOBAL chunk coords (e.g. -79 -45). It opens the right r.X.Z.mca,
finds that chunk, and dumps matching block_entities/entities. With --items it
also walks nested item compounds (id+Count) matching the filter.
"""
import sys, math
from pathlib import Path
from nbt import region
from nbt.nbt import TAG_Compound, TAG_List, NBTFile, TAG_Short, TAG_String, StructError as _StructError
from mutf8 import decode_modified_utf8 as _decode_mutf8
def _tag_string_parse_buffer(self, buffer):
    length = TAG_Short(buffer=buffer)
    read = buffer.read(length.value)
    if len(read) != length.value:
        raise _StructError()
    try:
        self.value = read.decode("utf-8")
    except UnicodeDecodeError:
        self.value = _decode_mutf8(read)
TAG_String._parse_buffer = _tag_string_parse_buffer

WORLD = Path(sys.argv[1])
FOLDER = sys.argv[2]
GX, GZ = int(sys.argv[3]), int(sys.argv[4])
FILTER = sys.argv[5].lower()
ITEMS = '--items' in sys.argv

def nbt_to_text(tag, indent=0, max_depth=14):
    if indent > max_depth:
        return '  ' * indent + '...'
    pad = '  ' * indent
    if isinstance(tag, (TAG_Compound, NBTFile)):
        out = []
        for k in tag.keys():
            v = tag[k]
            if isinstance(v, (TAG_Compound, TAG_List)):
                out.append(f"{pad}{k} ({type(v).__name__}):")
                out.append(nbt_to_text(v, indent + 1, max_depth))
            else:
                val = getattr(v, 'value', v)
                if hasattr(val, '__len__') and not isinstance(val, str) and len(val) > 24:
                    val = f"<{type(val).__name__} len={len(val)}>"
                out.append(f"{pad}{k} ({type(v).__name__}): {val}")
        return '\n'.join(out)
    elif isinstance(tag, TAG_List):
        out = []
        for i, item in enumerate(tag):
            if i > 60:
                out.append(f"{pad}... ({len(tag)-60} more)"); break
            out.append(f"{pad}[{i}]:")
            out.append(nbt_to_text(item, indent + 1, max_depth))
        return '\n'.join(out)
    return f"{pad}{getattr(tag,'value',tag)}"

rx, rz = math.floor(GX/32), math.floor(GZ/32)
rfile = WORLD / FOLDER / f"r.{rx}.{rz}.mca"
print(f"# {rfile}  (global chunk {GX},{GZ})")
if not rfile.exists():
    print("  MISSING FILE"); sys.exit(0)
lx, lz = GX - rx*32, GZ - rz*32
rf = region.RegionFile(str(rfile))
chunk = rf.get_chunk(lx, lz)
if chunk is None:
    print("  chunk not present"); sys.exit(0)

def walk_items(tag, label):
    if isinstance(tag, (TAG_Compound, NBTFile)):
        try:
            tid = tag['id'].value
            if (('Count' in tag) or ('count' in tag)) and isinstance(tid, str) and FILTER in tid.lower():
                print(f"\n--- item @ {label}: {tid} ---")
                print(nbt_to_text(tag))
        except (KeyError, AttributeError):
            pass
        for k in list(tag.keys()):
            walk_items(tag[k], label)
    elif isinstance(tag, TAG_List):
        for it in tag:
            walk_items(it, label)

n = 0
for key in ('block_entities', 'Entities', 'entities'):
    try:
        lst = chunk[key]
    except (KeyError, AttributeError):
        continue
    for e in lst:
        try:
            tid = e['id'].value
        except (KeyError, AttributeError):
            continue
        if FILTER in tid.lower():
            print(f"\n=== {key}: {tid} ===")
            print(nbt_to_text(e)); n += 1
if ITEMS:
    walk_items(chunk, f"chunk({GX},{GZ})")
if n == 0 and not ITEMS:
    print(f"  no block_entities/entities match '{FILTER}'")
