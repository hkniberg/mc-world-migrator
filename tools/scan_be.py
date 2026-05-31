"""Read-only: scan a region/entities dir for block_entities/entities matching an
id substring, dump first N with full NBT. Applies the modified-UTF-8 patch.

Usage: python scan_be.py <dir-with-mca> <id_substring> [--max N]
"""
import sys, glob, os
from nbt import region
from nbt.nbt import TAG_Compound, TAG_List, NBTFile, TAG_Short, TAG_String, StructError as SE
from mutf8 import decode_modified_utf8 as dm
def _p(self, b):
    l = TAG_Short(buffer=b); r = b.read(l.value)
    if len(r) != l.value: raise SE()
    try: self.value = r.decode('utf-8')
    except UnicodeDecodeError: self.value = dm(r)
TAG_String._parse_buffer = _p

DIR = sys.argv[1]; FILT = sys.argv[2].lower()
MAX = int(sys.argv[sys.argv.index('--max')+1]) if '--max' in sys.argv else 4

def t(tag, ind=0, md=14):
    if ind > md: return '  '*ind+'...'
    pad='  '*ind
    if isinstance(tag,(TAG_Compound,NBTFile)):
        o=[]
        for k in tag.keys():
            v=tag[k]
            if isinstance(v,(TAG_Compound,TAG_List)):
                o.append(f"{pad}{k} ({type(v).__name__}):"); o.append(t(v,ind+1,md))
            else:
                val=getattr(v,'value',v)
                if hasattr(val,'__len__') and not isinstance(val,str) and len(val)>24: val=f"<{type(val).__name__} len={len(val)}>"
                o.append(f"{pad}{k} ({type(v).__name__}): {val}")
        return '\n'.join(o)
    elif isinstance(tag,TAG_List):
        o=[]
        for i,it in enumerate(tag):
            if i>60: o.append(f"{pad}...({len(tag)-60} more)"); break
            o.append(f"{pad}[{i}]:"); o.append(t(it,ind+1,md))
        return '\n'.join(o)
    return f"{pad}{getattr(tag,'value',tag)}"

found=0
for rf_path in sorted(glob.glob(os.path.join(DIR,'*.mca'))):
    if found>=MAX: break
    try: rf=region.RegionFile(rf_path)
    except Exception: continue
    for e in rf.get_chunk_coords():
        if found>=MAX: break
        try: c=rf.get_chunk(e['x'],e['z'])
        except Exception: continue
        if c is None: continue
        for key in ('block_entities','Entities','entities'):
            try: lst=c[key]
            except (KeyError,AttributeError): continue
            for be in lst:
                if found>=MAX: break
                try: tid=be['id'].value
                except (KeyError,AttributeError): continue
                if FILT in tid.lower():
                    print(f"\n=== {os.path.basename(rf_path)} {key}: {tid} ==="); print(t(be)); found+=1
if not found: print(f"no match for '{FILT}' in {DIR}")
