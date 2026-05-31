"""Read-only: walk a single NBT .dat (playerdata) for items matching a filter."""
import sys
from nbt import nbt as nbt_mod
from nbt.nbt import TAG_Compound, TAG_List, NBTFile, TAG_Short, TAG_String, StructError as _StructError
from mutf8 import decode_modified_utf8 as _decode_mutf8
def _p(self, buffer):
    length = TAG_Short(buffer=buffer); read = buffer.read(length.value)
    if len(read) != length.value: raise _StructError()
    try: self.value = read.decode("utf-8")
    except UnicodeDecodeError: self.value = _decode_mutf8(read)
TAG_String._parse_buffer = _p

def nbt_to_text(tag, indent=0, max_depth=14):
    if indent > max_depth: return '  '*indent + '...'
    pad = '  '*indent
    if isinstance(tag, (TAG_Compound, NBTFile)):
        out = []
        for k in tag.keys():
            v = tag[k]
            if isinstance(v, (TAG_Compound, TAG_List)):
                out.append(f"{pad}{k} ({type(v).__name__}):"); out.append(nbt_to_text(v, indent+1, max_depth))
            else:
                val = getattr(v,'value',v)
                if hasattr(val,'__len__') and not isinstance(val,str) and len(val)>24: val=f"<{type(val).__name__} len={len(val)}>"
                out.append(f"{pad}{k} ({type(v).__name__}): {val}")
        return '\n'.join(out)
    elif isinstance(tag, TAG_List):
        out = []
        for i, it in enumerate(tag):
            if i>60: out.append(f"{pad}...({len(tag)-60} more)"); break
            out.append(f"{pad}[{i}]:"); out.append(nbt_to_text(it, indent+1, max_depth))
        return '\n'.join(out)
    return f"{pad}{getattr(tag,'value',tag)}"

PATH = sys.argv[1]; FILTER = sys.argv[2].lower()
LOC = sys.argv[3] if len(sys.argv) > 3 else None  # optional: only under a top key path hint

def walk(tag, label):
    if isinstance(tag, (TAG_Compound, NBTFile)):
        try:
            tid = tag['id'].value
            if (('Count' in tag) or ('count' in tag)) and isinstance(tid, str) and FILTER in tid.lower():
                print(f"\n--- {label}: {tid} ---"); print(nbt_to_text(tag))
        except (KeyError, AttributeError):
            pass
        for k in list(tag.keys()):
            walk(tag[k], f"{label}.{k}")
    elif isinstance(tag, TAG_List):
        for i, it in enumerate(tag):
            walk(it, f"{label}[{i}]")

n = nbt_mod.NBTFile(PATH)
walk(n, "root")
