# MC 1.20.1 (Forge) → 1.21.1 (NeoForge) World Migrator

A repair tool for Minecraft worlds that break when a **1.20.1 / Forge** modpack save
is opened in **1.21.1 / NeoForge**. It targets a Create-based modpack
(Create, Create: Steam 'n' Rails, Sophisticated Backpacks, Immersive Paintings,
CC: Tweaked) but the core item-conversion logic is generic.

## The problem

When the old world is opened in 1.21, vanilla's DataFixerUpper and the mods' own
fixers convert the data that lives in **vanilla** containers (chests, barrels,
player inventory, ender chests) correctly — but they do **not** reach data stored
**inside mod blocks/items**. The result is one root cause with two symptoms:

1. **Counts reset to 1.** Item stacks inside mod inventories (item vaults, funnels,
   backpacks, …) lose their stack size — `Count` is dropped and the reader defaults
   to 1.
2. **Reshaped payloads are wiped or defaulted.** Where a mod renamed/restructured
   its NBT (fluid tanks, factory-gauge links, shops, clipboards, paintings, …),
   migration writes an empty default instead of converting.

This tool repairs the migrated world by reading the **original 1.20 save as the
source of truth** and rewriting each affected block/entity/item/`.dat` record into
the correct 1.21 schema, keyed by block position, entity UUID, or storage UUID.

> Because the broken world no longer contains the lost values, **you must keep your
> original 1.20 save** — the tool reads from it.

## What it fixes

| Area | Detail |
|---|---|
| **Item counts** | Every stack in every mod inventory, world-wide (vaults, funnels, toolboxes, packagers, backpacks, shop displays, filters, …) |
| **Sophisticated Backpacks** | Placed, inventory, and (best-effort) worn backpacks; contents rehomed to each backpack's storage UUID in `sophisticatedbackpacks.dat`; nested backpacks |
| **Create chain conveyors** | Chain connections restored |
| **Create tracks** | Connection positions incl. **curved/bezier** segments |
| **Create track signals** | `TargetTrack` binding + state restored (see limitations) |
| **Create fluid tanks** | Fluid contents (`{Amount,FluidName}` → `TankContent.Fluid{amount,id}`) |
| **Create factory gauges** | Inter-panel connections (`Targeting`/`TargetedBy`) |
| **Create shops (table cloths)** | Encoded request / offer / target restored |
| **Create clipboards** | Page content |
| **Create packages** | Placed packages' contents (inventory packages: see limitations) |
| **Create postboxes** | Linked station target |
| **Item / attribute filters** | `create:filter` and `create:attribute_filter` contents and settings |
| **Immersive Paintings** | Orientation (facing enum remap — fixes wall→floor) and image (motive id) |
| **CC: Tweaked printouts** | Text in item frames |
| **Complex item NBT inside mod containers** | Enchantments, custom names, lore, damage, **written/writable books**, **shulker boxes / containers (recursive)**, **banners & shields** (pattern + colour), potions, trims, etc. |

Anything the item converter doesn't have an explicit rule for is preserved under
`minecraft:custom_data` (exactly what vanilla does) and **logged**, so nothing is
silently lost — see "Auditing" below.

## What it does NOT fix (known limitations)

- **Train signals' live track binding.** The track graph migrates, but the signal's
  runtime edge-binding does not reliably reconnect. Easiest fix: **break and
  re-place the signal blocks in-game** (a few seconds each). Editing the track graph
  directly risks derailing a working train, so the tool leaves it alone.
- **Worn backpacks.** The Curios→Accessories migration drops the equipped item;
  the tool re-inserts it and restores its contents, but the accessories mod may
  still reject it on load. **Recommended: have players unequip backpacks before
  migrating.**
- **Loose packages in the player inventory.** Create culls package *items* on load.
  Placed packages in the world are fine.
- **A few uncommon item components inside mod containers** are preserved in
  `custom_data` but not yet mapped to their native 1.21 component: loaded crossbows
  (`Charged`), player heads (`SkullOwner`), `HideFlags`. The items survive but may
  miss that one feature. *(Items in vanilla containers are unaffected — vanilla
  handles them completely.)*

## Requirements

- Python 3.9+
- The `NBT` library: `pip install -r requirements.txt`

## How to run (the real migration)

1. **Back up everything.** Always work on **copies** of your saves.
2. Keep your **original 1.20.1 (Forge) save** — it is the source of truth.
3. Let **1.21.1 / NeoForge open the world once** (this performs the vanilla
   migration), then quit. Make a copy of that migrated save to patch.
4. Run the repair (default is a safe **dry-run**; add `--write` to apply):

   ```bash
   # dry-run first — shows what it would change
   python migrate_world.py fix-all --source /path/to/1.20-original --target /path/to/migrated-copy

   # then apply
   python migrate_world.py fix-all --source /path/to/1.20-original --target /path/to/migrated-copy --write
   ```

   On Windows (PowerShell), quote paths with spaces:

   ```powershell
   python migrate_world.py fix-all --source "C:\saves\1.20-original" --target "C:\saves\migrated-copy" --write
   ```

5. Open the patched copy in 1.21. Re-place any train signals.

The tool is **idempotent** — running it again changes nothing.

## Subcommands

`fix-all` runs everything. Individual fixers exist for targeted runs/debugging:

```
index-source          preflight counts from the 1.20 source
fix-inventories        generic count/contents restore across mod block entities
fix-chain-conveyors    fix-tracks            fix-track-signals
fix-fluid-tanks        fix-factory-panels    fix-table-cloths
fix-clipboards         fix-postboxes         fix-paintings
fix-item-frames        fix-package-entities
fix-mod-items          mod items in vanilla containers (custom_data → components)
fix-be-backpacks       fix-backpack-dat      fix-backpacks (rehome + worn)
fix-all                everything, in order
verify                 dev/testing only — compares a patched world to a hand-built
                       1.21 reference (needs --ref); not used for real migrations
```

## Auditing what falls through (recommended before a real run)

Do a dry-run `fix-all` and read the **"tag keys routed to custom_data"** report it
prints. That is the complete list of item-NBT keys in *your* world that aren't yet
mapped to a native component. If something valuable shows up (e.g. a component you
care about), it can be added to the converter before you commit with `--write`.
For most worlds this list is short (crossbows / player heads).

## How it works

`migrate_world.py` is self-contained (only depends on `NBT`). It:

- indexes the 1.20 source world (block entities by position, entities by UUID,
  playerdata);
- walks the target world's region/entities/`data`/`playerdata`;
- for each broken structure, restores the value from the source in the 1.21 schema;
- converts 1.20 item NBT (`Count`/`tag`) to 1.21 (`count`/`components`) via a
  mapping table with a `custom_data` catch-all, including mod-specific converters
  (Create filters/packages/clipboards, backpacks) and recursive container items.

## Diagnostics (`tools/`)

Read-only NBT inspectors used during development; handy for troubleshooting:

```bash
python tools/dump_chunk_nbt.py <world>/region "create:item_vault"   # dump matching block entities
python tools/find_items.py     <world> "backpack"                   # find items by id substring
```

## Performance

The current `fix-all` makes several full-world passes, so it can take minutes on a
large world. It is correctness-first; a single-pass optimization is planned.

## Safety

- Default dry-run; `--write` required to modify anything.
- Only writes structures it understands; never deletes.
- Reads the 1.20 source read-only.
- **Still: back up, and run on copies.**
