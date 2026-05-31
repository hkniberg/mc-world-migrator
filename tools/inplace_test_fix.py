"""IN-PLACE FIX TEST — applies best-inference fixes to the live ffcreate world's
specific example chunks + one player, reading truth from the immutable 1.20 backup.
This is a throwaway validation of the transforms before baking them into the migrator.

Fixes: chains (-79,-45), disks (-47,-35), stock ticker (-25,-35), backpacks (fb3faad5).
Paintings are intentionally deferred (registry schema + hash scheme changed; needs more work).
"""
import sys, struct, uuid, math
from nbt import region, nbt as nbtmod
from nbt.nbt import (TAG_Compound, TAG_List, TAG_String, TAG_Byte, TAG_Int,
                     TAG_Int_Array, TAG_Short, NBTFile, StructError as _SE)
from mutf8 import decode_modified_utf8 as _dm, encode_modified_utf8 as _em

# --- modified-UTF-8 round-trip patch (read + write) ---
def _ps(self, b):
    l = TAG_Short(buffer=b); r = b.read(l.value)
    if len(r) != l.value: raise _SE()
    try: self.value = r.decode('utf-8')
    except UnicodeDecodeError: self.value = _dm(r)
def _rs(self, b):
    try: sv = _em(self.value)
    except Exception: sv = self.value.encode('utf-8')
    TAG_Short(len(sv))._render_buffer(b); b.write(sv)
TAG_String._parse_buffer = _ps
TAG_String._render_buffer = _rs

LIVE = '/home/admin/ffcreate/world'
SRC  = '/home/admin/backups/ffcreate_2026-05-31_original/world'
DRY  = '--dry' in sys.argv

# --- helpers ---
def cget(c, name):
    if c is None: return None
    for t in getattr(c, 'tags', []):
        if t.name == name: return t
    return None
def cset(c, name, tag):
    tag.name = name
    c.tags = [t for t in c.tags if t.name != name] + [tag]
    return tag
def cdel(c, name):
    c.tags = [t for t in c.tags if t.name != name]
def mk_int(v, n=''): t=TAG_Int(name=n); t.value=int(v); return t
def mk_byte(v, n=''): t=TAG_Byte(name=n); t.value=int(v); return t
def mk_str(v, n=''): t=TAG_String(name=n); t.value=str(v); return t
def mk_ia(vals, n=''): t=TAG_Int_Array(name=n); t.value=[int(x) for x in vals]; return t
def mk_comp(n=''): t=TAG_Compound(name=n); t.tags=[]; return t
def mk_list(it, n=''): t=TAG_List(name=n, type=it); t.tags=[]; return t

def reg(world, folder): return lambda rx,rz: region.RegionFile(f'{world}/{folder}/r.{rx}.{rz}.mca')
def local(gx,gz): return math.floor(gx/32), math.floor(gz/32), gx-math.floor(gx/32)*32, gz-math.floor(gz/32)*32
def ia_to_uuid(ia): return str(uuid.UUID(bytes=b''.join(struct.pack('>I',(x&0xffffffff)) for x in ia)))

changes = []

# ===================== 1. CHAINS =====================
def fix_chains():
    gx,gz = -79,-45
    rx,rz,lx,lz = local(gx,gz)
    tgt_rf = region.RegionFile(f'{LIVE}/region/r.{rx}.{rz}.mca')
    src_rf = region.RegionFile(f'{SRC}/region/r.{rx}.{rz}.mca')
    tgt = tgt_rf.get_chunk(lx,lz); src = src_rf.get_chunk(lx,lz)
    # source chain conveyors by (x,y,z)
    smap = {}
    for be in (cget(src,'block_entities') or []):
        if cget(be,'id') and cget(be,'id').value=='create:chain_conveyor':
            smap[(cget(be,'x').value,cget(be,'y').value,cget(be,'z').value)] = be
    n=0
    for be in (cget(tgt,'block_entities') or []):
        if not (cget(be,'id') and cget(be,'id').value=='create:chain_conveyor'): continue
        key=(cget(be,'x').value,cget(be,'y').value,cget(be,'z').value)
        s=smap.get(key)
        if s is None: continue
        sc=cget(s,'Connections')
        if sc is None or len(sc.tags)==0: continue
        newc=mk_list(TAG_Int_Array,'Connections')
        for e in sc.tags:
            newc.tags.append(mk_ia([cget(e,'X').value,cget(e,'Y').value,cget(e,'Z').value]))
        cset(be,'Connections',newc); n+=1
    if not DRY: tgt_rf.write_chunk(lx,lz,tgt)
    changes.append(f"chains: rewrote Connections on {n} chain_conveyor(s) in chunk {gx},{gz}")

# ===================== 2. DISKS =====================
def reshape_disk(item):
    comp=cget(item,'components')
    if comp is None: return False
    cd=cget(comp,'minecraft:custom_data')
    if cd is None: return False
    did=cget(cd,'DiskId'); col=cget(cd,'Color')
    touched=False
    if did is not None:
        cset(comp,'computercraft:disk_id',mk_int(did.value)); cdel(cd,'DiskId'); touched=True
    if col is not None:
        dyed=mk_comp(); cset(dyed,'rgb',mk_int(col.value)); cset(dyed,'show_in_tooltip',mk_byte(0))
        cset(comp,'minecraft:dyed_color',dyed); cdel(cd,'Color'); touched=True
    if cd.tags==[]: cdel(comp,'minecraft:custom_data')
    return touched
def walk_disks(tag, hits):
    if isinstance(tag,(TAG_Compound,NBTFile)):
        idt=cget(tag,'id')
        if idt is not None and idt.value=='computercraft:disk':
            if reshape_disk(tag): hits[0]+=1
        for t in list(tag.tags): walk_disks(t,hits)
    elif isinstance(tag,TAG_List):
        for t in tag.tags: walk_disks(t,hits)
def fix_disks():
    gx,gz=-47,-35; rx,rz,lx,lz=local(gx,gz)
    rf=region.RegionFile(f'{LIVE}/region/r.{rx}.{rz}.mca')
    tgt=rf.get_chunk(lx,lz); hits=[0]
    walk_disks(tgt,hits)
    if not DRY: rf.write_chunk(lx,lz,tgt)
    changes.append(f"disks: reshaped {hits[0]} computercraft:disk item(s) in chunk {gx},{gz}")

# ===================== 3. STOCK TICKER =====================
WL={0:'whitelist_disj',1:'whitelist_conj',2:'blacklist'}
def build_attr_filter(src_tag):
    comp=mk_comp()
    nm=cget(src_tag,'display'); nm=cget(nm,'Name') if nm else None
    if nm is not None: cset(comp,'minecraft:custom_name',mk_str(nm.value))
    wm=cget(src_tag,'WhitelistMode'); cset(comp,'create:attribute_filter_whitelist_mode',mk_str(WL.get(wm.value if wm else 0,'whitelist_disj')))
    lst=mk_list(TAG_Compound,'create:attribute_filter_matched_attributes')
    for ma in (cget(src_tag,'MatchedAttributes') or []):
        attrid=cget(ma,'attributeId'); modid=cget(ma,'modId'); inv=cget(ma,'Inverted')
        a=mk_comp()
        attr=mk_comp(); cset(attr,'type',mk_str(attrid.value))
        if modid is not None: cset(attr,'value',mk_str(modid.value))
        else: cset(attr,'value',mk_comp())
        cset(a,'attribute',attr); cset(a,'inverted',mk_byte(inv.value if inv else 0))
        lst.tags.append(a)
    cset(comp,'create:attribute_filter_matched_attributes',lst)
    return comp
def fix_stock():
    gx,gz=-25,-35; rx,rz,lx,lz=local(gx,gz)
    trf=region.RegionFile(f'{LIVE}/region/r.{rx}.{rz}.mca')
    srf=region.RegionFile(f'{SRC}/region/r.{rx}.{rz}.mca')
    tgt=trf.get_chunk(lx,lz); src=srf.get_chunk(lx,lz)
    smap={}
    for be in (cget(src,'block_entities') or []):
        if cget(be,'id') and cget(be,'id').value=='create:stock_ticker':
            smap[(cget(be,'x').value,cget(be,'y').value,cget(be,'z').value)]=be
    n=0; cats=0
    for be in (cget(tgt,'block_entities') or []):
        if not (cget(be,'id') and cget(be,'id').value=='create:stock_ticker'): continue
        s=smap.get((cget(be,'x').value,cget(be,'y').value,cget(be,'z').value))
        if s is None: continue
        s_cat=cget(s,'Categories'); t_cat=cget(be,'Categories')
        if s_cat is None or len(s_cat.tags)==0: continue
        newcats=mk_list(TAG_Compound,'Categories')
        for sc in s_cat.tags:
            cid=cget(sc,'id').value; stag=cget(sc,'tag')
            item=mk_comp(); cset(item,'id',mk_str(cid)); cset(item,'count',mk_int(1))
            if cid=='create:attribute_filter' and stag is not None:
                cset(item,'components',build_attr_filter(stag))
            else:
                # leave non-attribute categories as-is from target if present, else minimal
                cset(item,'components',mk_comp())
            newcats.tags.append(item); cats+=1
        cset(be,'Categories',newcats); n+=1
    if not DRY: trf.write_chunk(lx,lz,tgt)
    changes.append(f"stock: rebuilt Categories on {n} stock_ticker(s) ({cats} categories) in chunk {gx},{gz}")

# ===================== 4. BACKPACKS =====================
def fix_backpacks():
    pid='fb3faad5-b1ee-42fe-a2e2-2e79fe5aec5c.dat'
    live=nbtmod.NBTFile(f'{LIVE}/playerdata/{pid}')
    src=nbtmod.NBTFile(f'{SRC}/playerdata/{pid}')
    # build source map (listname,slot,id)->contentsUuid values
    smap={}
    for ln in ('Inventory','EnderItems'):
        for it in (cget(src,ln) or []):
            idt=cget(it,'id')
            if idt and 'backpack' in idt.value:
                tag=cget(it,'tag'); cu=cget(tag,'contentsUuid') if tag else None
                slot=cget(it,'Slot')
                if cu is not None and slot is not None:
                    smap[(ln,slot.value,idt.value)]=list(cu.value)
    # storage uuids present in live sophisticatedbackpacks.dat
    sb=nbtmod.NBTFile(f'{LIVE}/data/sophisticatedbackpacks.dat')
    present={}
    for e in cget(cget(sb,'data'),'backpackContents'):
        u=cget(e,'uuid')
        if u is not None:
            cnt=0
            cc=cget(e,'contents')
            def ci(t):
                nonlocal cnt
                if isinstance(t,TAG_List):
                    for x in t.tags:
                        if isinstance(x,TAG_Compound) and cget(x,'id'): cnt+=1
                        else: ci(x)
                elif isinstance(t,TAG_Compound):
                    for tt in t.tags: ci(tt)
            ci(cc); present[ia_to_uuid(u.value)]=cnt
    fixed=0; missing=0
    for ln in ('Inventory','EnderItems'):
        for it in (cget(live,ln) or []):
            idt=cget(it,'id')
            if not (idt and 'backpack' in idt.value): continue
            slot=cget(it,'Slot')
            key=(ln, slot.value if slot else None, idt.value)
            su=smap.get(key)
            if su is None: continue
            comp=cget(it,'components')
            if comp is None: comp=cset(it,'components',mk_comp())
            cset(comp,'sophisticatedcore:storage_uuid',mk_ia(su))
            u=ia_to_uuid(su); items=present.get(u,'ABSENT')
            if items=='ABSENT': missing+=1
            fixed+=1
    if not DRY: live.write_file(f'{LIVE}/playerdata/{pid}')
    changes.append(f"backpacks: re-pointed storage_uuid on {fixed} backpack(s) for {pid[:8]} (contents missing for {missing})")

for fn in (fix_chains, fix_disks, fix_stock, fix_backpacks):
    try: fn()
    except Exception as e:
        import traceback; changes.append(f"ERROR in {fn.__name__}: {e}"); traceback.print_exc()

print("DRY RUN" if DRY else "APPLIED")
for c in changes: print("  -", c)
