"""Find items (by id substring) nested inside block_entity Items lists,
player inventories, and entity inventories. Dumps the item NBT.

Usage: python find_items.py <world_dir> <filter>
"""
import sys
from pathlib import Path
from nbt import nbt as nbt_mod, region
from nbt.nbt import TAG_Compound, TAG_List, NBTFile
from nbt_helpers import nbt_to_text

WORLD = Path(sys.argv[1])
FILTER = sys.argv[2].lower()
MAX = 5
if '--max' in sys.argv:
    MAX = int(sys.argv[sys.argv.index('--max') + 1])

found = 0

def walk_items(tag, location_label):
    """Recurse, find any TAG_Compound that looks like an item (has 'id' string and 'Count' or 'count')."""
    global found
    if found >= MAX:
        return
    if isinstance(tag, (TAG_Compound, NBTFile)):
        try:
            tid = tag['id'].value
            has_count = ('Count' in tag) or ('count' in tag)
            if has_count and isinstance(tid, str) and FILTER in tid.lower():
                print(f"\n=== item in {location_label}: {tid} ===")
                print(nbt_to_text(tag, max_depth=10))
                found += 1
                return
        except (KeyError, AttributeError):
            pass
        for k in list(tag.keys()):
            walk_items(tag[k], location_label)
    elif isinstance(tag, TAG_List):
        for item in tag:
            walk_items(item, location_label)

# Scan region/ files
for sub in ['region']:
    d = WORLD / sub
    if not d.exists():
        continue
    for rfile in sorted(d.glob('*.mca')):
        if found >= MAX:
            break
        try:
            rf = region.RegionFile(str(rfile))
        except Exception:
            continue
        for entry in rf.get_chunk_coords():
            if found >= MAX:
                break
            try:
                chunk = rf.get_chunk(entry['x'], entry['z'])
            except Exception:
                continue
            if chunk is None:
                continue
            walk_items(chunk, f"{sub}/{rfile.name} chunk({entry['x']},{entry['z']})")

# Scan playerdata
pd = WORLD / 'playerdata'
if pd.exists():
    for f in sorted(pd.glob('*.dat')):
        if found >= MAX:
            break
        try:
            n = nbt_mod.NBTFile(str(f))
            walk_items(n, f"playerdata/{f.name}")
        except Exception as e:
            print(f"err {f}: {e}")

if found == 0:
    print(f"No matching items for '{FILTER}'")
