"""Scan a world for block_entities / entities / blocks matching a substring filter
and dump the matching NBT as readable text. Useful for comparing 1.20 vs 1.21
data structures for specific mod blocks.

Usage:
  python dump_chunk_nbt.py <region_dir> <filter_substring> [--max N]

Filter matches against the 'id' field of block_entities/entities (e.g.
'create:chain_conveyor' or 'sophisticatedbackpacks:backpack') and against
'Name' of palette entries (block IDs).
"""
import sys
from pathlib import Path
from nbt import region
from nbt.nbt import TAG_Compound, TAG_List, TAG_String, NBTFile

REGION_DIR = Path(sys.argv[1])
FILTER = sys.argv[2].lower()
MAX = 5
if '--max' in sys.argv:
    MAX = int(sys.argv[sys.argv.index('--max') + 1])

def nbt_to_text(tag, indent=0, max_depth=12):
    if indent > max_depth:
        return '  ' * indent + '...'
    pad = '  ' * indent
    cname = type(tag).__name__
    if isinstance(tag, (TAG_Compound, NBTFile)):
        out = []
        try:
            for k in tag.keys():
                v = tag[k]
                vt = type(v).__name__
                if isinstance(v, (TAG_Compound, TAG_List)):
                    out.append(f"{pad}{k} ({vt}):")
                    out.append(nbt_to_text(v, indent + 1, max_depth))
                else:
                    val = getattr(v, 'value', v)
                    if hasattr(val, '__len__') and not isinstance(val, str) and len(val) > 16:
                        val = f"<{type(val).__name__} len={len(val)}>"
                    out.append(f"{pad}{k} ({vt}): {val}")
        except Exception as e:
            out.append(f"{pad}<error reading compound: {e}>")
        return '\n'.join(out)
    elif isinstance(tag, TAG_List):
        out = []
        for i, item in enumerate(tag):
            if i > 50:
                out.append(f"{pad}... ({len(tag) - 50} more)")
                break
            out.append(f"{pad}[{i}]:")
            out.append(nbt_to_text(item, indent + 1, max_depth))
        return '\n'.join(out)
    else:
        return f"{pad}{getattr(tag, 'value', tag)}"


found = 0

def search_in(tag, kind, region_name, chunk_xz):
    """Look at block_entities (kind='be'), entities (kind='ent'), or sections palette ('block')."""
    global found
    if found >= MAX:
        return
    if isinstance(tag, (TAG_Compound, NBTFile)):
        if kind in ('be', 'ent'):
            try:
                tid = tag['id'].value
                if FILTER in tid.lower():
                    print(f"\n=== {kind} in {region_name} chunk{chunk_xz}: {tid} ===")
                    print(nbt_to_text(tag))
                    found += 1
                    return
            except (KeyError, AttributeError):
                pass
        if kind == 'block':
            try:
                tid = tag['Name'].value
                if FILTER in tid.lower():
                    print(f"\n=== palette entry in {region_name} chunk{chunk_xz}: {tid} ===")
                    print(nbt_to_text(tag))
                    found += 1
                    return
            except (KeyError, AttributeError):
                pass


for region_file in sorted(REGION_DIR.glob('*.mca')):
    if found >= MAX:
        break
    try:
        rf = region.RegionFile(str(region_file))
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
        # block_entities array
        try:
            be_list = chunk['block_entities']
            for be in be_list:
                if found >= MAX:
                    break
                search_in(be, 'be', region_file.name, (entry['x'], entry['z']))
        except (KeyError, AttributeError):
            pass
        # palette in sections
        try:
            sections = chunk['sections']
            for sec in sections:
                if found >= MAX:
                    break
                try:
                    palette = sec['block_states']['palette']
                    for pal_entry in palette:
                        if found >= MAX:
                            break
                        search_in(pal_entry, 'block', region_file.name, (entry['x'], entry['z']))
                except (KeyError, AttributeError):
                    pass
        except (KeyError, AttributeError):
            pass

if found == 0:
    print(f"No matches for '{FILTER}' in {REGION_DIR}")
