"""Phase-2 in-place test: fix pocket computers + printed pages in chunk -47,-35,
reading each item's own custom_data (same approach disks used). Reuses migrate_world
helpers + build_printout_component + the mUTF-8 read/write patches."""
import sys
sys.path.insert(0, '/home/admin/claude/mc-world-migrator')
import migrate_world as mw
from nbt import region
from nbt.nbt import TAG_Compound, TAG_List, NBTFile

LIVE = '/home/admin/ffcreate/world'
rx, rz = -2, -2
gx, gz = -47, -35
lx, lz = gx - rx*32, gz - rz*32
rf = region.RegionFile(f'{LIVE}/region/r.{rx}.{rz}.mca')
chunk = rf.get_chunk(lx, lz)

pc = pr = 0
PRINTOUT = ('computercraft:printed_page', 'computercraft:printed_pages', 'computercraft:printed_book')

def reshape(item):
    global pc, pr
    idt = mw.cget(item, 'id')
    comp = mw.cget(item, 'components')
    if idt is None or comp is None:
        return
    cd = mw.cget(comp, 'minecraft:custom_data')
    if cd is None:
        return
    if 'pocket_computer' in idt.value:
        cid = mw.cget(cd, 'ComputerId')
        if cid is not None:
            mw.cset(comp, 'computercraft:computer_id', mw.make_int(cid.value))
            on = mw.cget(cd, 'On')
            if on is not None:
                mw.cset(comp, 'computercraft:on', mw.make_byte(on.value))
            for k in ('ComputerId', 'On', 'SessionId', 'InstanceId'):
                mw.cdel(cd, k)
            pc += 1
    elif idt.value in PRINTOUT:
        po = mw.build_printout_component(cd)
        if po is not None:
            mw.cset(comp, 'computercraft:printout', po)
            for t in list(cd.tags):
                if t.name and (t.name.startswith('Text') or t.name.startswith('Color') or t.name in ('Title', 'Pages')):
                    mw.cdel(cd, t.name)
            pr += 1
    if cd.tags == []:
        mw.cdel(comp, 'minecraft:custom_data')

def walk(t):
    if isinstance(t, (TAG_Compound, NBTFile)):
        if mw.cget(t, 'id') is not None:
            reshape(t)
        for x in list(t.tags):
            walk(x)
    elif isinstance(t, TAG_List):
        for x in t.tags:
            walk(x)

walk(chunk)
if '--dry' not in sys.argv:
    rf.write_chunk(lx, lz, chunk)
print(f"pocket computers fixed: {pc}   printed pages fixed: {pr}   (write={'--dry' not in sys.argv})")
