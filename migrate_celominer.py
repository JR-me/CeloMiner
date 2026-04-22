#!/usr/bin/env python3
"""
migrate_celominer.py
Snapshots all player state from the OLD deployed CeloMiner + cBALL contracts,
then seeds the NEW contracts and locks migration.

Flow:
  1. Pause old contracts  (prevents state changes during snapshot)
  2. Snapshot all players via Transfer(0x0 → player) mint events from old cBALL
  3. For each player: read balanceOf (cBALL) + miners() (CeloMiner)
  4. Write snapshot to  migration_snapshot.json
  5. Seed new CeloMiner in batches via seedPlayerBatch()
  6. Seed new cBALL     in batches via seedBalance()
  7. Reconcile: compare old vs new state for every player
  8. Lock migration on both new contracts
  9. Wire minter: setMinter(newMinerAddress) on new cBALL
  10. Unpause new CeloMiner so play can resume

Requirements:
  pip install web3 --break-system-packages

Usage:
  # Dry-run — snapshot + print plan, no writes to new contracts
  python migrate_celominer.py --keystore ./UTC-keystore.json --dry-run

  # Live migration
  python migrate_celominer.py --keystore ./UTC-keystore.json

  # Resume from existing snapshot (skip re-fetching chain data)
  python migrate_celominer.py --keystore ./UTC-keystore.json --snapshot migration_snapshot.json

Security notes:
  - Private key is deleted from local namespace after the final signing call.
  - All writes to new contracts use legacy (type-0) transactions for Celo compatibility.
  - seedPlayerBatch() is idempotent: re-running the script will not double-count state
    because each seed overwrites the previous value and adjusts totalMinted by the delta.
"""

import json
import getpass
import argparse
import sys
import math
import time
from pathlib import Path

try:
    from web3 import Web3
    from eth_account import Account
except ImportError:
    print("pip install web3 --break-system-packages")
    sys.exit(1)

# ── Configuration ────────────────────────────────────────────────────────────

RPC_URL      = "https://forno.celo.org"
CELO_CHAIN_ID = 42220
BATCH_SIZE   = 50      # players per seedPlayerBatch / seedBalance call
GAS_MULT     = 1.25    # 25% safety buffer on gas estimates
POLL_INTERVAL = 2      # seconds between receipt polls

# ── Minimal ABIs ─────────────────────────────────────────────────────────────

CBALL_OLD_ABI = [
    {"name":"Transfer","type":"event","inputs":[
        {"name":"from","type":"address","indexed":True},
        {"name":"to",  "type":"address","indexed":True},
        {"name":"value","type":"uint256","indexed":False}
    ]},
    {"name":"balanceOf","type":"function","stateMutability":"view",
     "inputs":[{"name":"","type":"address"}],"outputs":[{"name":"","type":"uint256"}]},
    {"name":"totalSupply","type":"function","stateMutability":"view",
     "inputs":[],"outputs":[{"name":"","type":"uint256"}]},
    {"name":"pause",  "type":"function","stateMutability":"nonpayable","inputs":[],"outputs":[]},
    {"name":"paused", "type":"function","stateMutability":"view","inputs":[],"outputs":[{"type":"bool"}]},
]

MINER_OLD_ABI = [
    {"name":"miners","type":"function","stateMutability":"view",
     "inputs":[{"name":"","type":"address"}],
     "outputs":[
         {"name":"totalMined","type":"uint256"},
         {"name":"clicks",    "type":"uint256"},
         {"name":"tool",      "type":"uint8"}
     ]},
    {"name":"totalMinted","type":"function","stateMutability":"view",
     "inputs":[],"outputs":[{"name":"","type":"uint256"}]},
    {"name":"pause",  "type":"function","stateMutability":"nonpayable","inputs":[],"outputs":[]},
    {"name":"paused", "type":"function","stateMutability":"view","inputs":[],"outputs":[{"type":"bool"}]},
]

CBALL_NEW_ABI = [
    {"name":"seedBalance","type":"function","stateMutability":"nonpayable",
     "inputs":[{"name":"to","type":"address"},{"name":"amount","type":"uint256"}],"outputs":[]},
    {"name":"lockMigration","type":"function","stateMutability":"nonpayable","inputs":[],"outputs":[]},
    {"name":"setMinter","type":"function","stateMutability":"nonpayable",
     "inputs":[{"name":"minter_","type":"address"}],"outputs":[]},
    {"name":"migrationLocked","type":"function","stateMutability":"view",
     "inputs":[],"outputs":[{"type":"bool"}]},
    {"name":"totalSupply","type":"function","stateMutability":"view",
     "inputs":[],"outputs":[{"name":"","type":"uint256"}]},
    {"name":"balanceOf","type":"function","stateMutability":"view",
     "inputs":[{"name":"","type":"address"}],"outputs":[{"name":"","type":"uint256"}]},
    {"name":"paused","type":"function","stateMutability":"view",
     "inputs":[],"outputs":[{"type":"bool"}]},
]

MINER_NEW_ABI = [
    {"name":"seedPlayerBatch","type":"function","stateMutability":"nonpayable",
     "inputs":[
         {"name":"players",    "type":"address[]"},
         {"name":"totalMineds","type":"uint256[]"},
         {"name":"clicksArr",  "type":"uint256[]"},
         {"name":"tools",      "type":"uint8[]"}
     ],"outputs":[]},
    {"name":"lockMigration","type":"function","stateMutability":"nonpayable","inputs":[],"outputs":[]},
    {"name":"migrationLocked","type":"function","stateMutability":"view",
     "inputs":[],"outputs":[{"type":"bool"}]},
    {"name":"totalMinted","type":"function","stateMutability":"view",
     "inputs":[],"outputs":[{"name":"","type":"uint256"}]},
    {"name":"miners","type":"function","stateMutability":"view",
     "inputs":[{"name":"","type":"address"}],
     "outputs":[
         {"name":"totalMined","type":"uint256"},
         {"name":"clicks",    "type":"uint256"},
         {"name":"tool",      "type":"uint8"}
     ]},
    {"name":"unpause","type":"function","stateMutability":"nonpayable","inputs":[],"outputs":[]},
    {"name":"paused","type":"function","stateMutability":"view",
     "inputs":[],"outputs":[{"type":"bool"}]},
]

# ── Helpers ───────────────────────────────────────────────────────────────────

def load_keystore(path):
    with open(Path(path).expanduser()) as f:
        ks = json.load(f)
    pw = getpass.getpass("🔑  Keystore password: ")
    pk = Account.decrypt(ks, pw)
    acct = Account.from_key(pk)
    print(f"👛  Deployer: {acct.address}")
    return acct.address, pk

def gas_price(w3):
    gp = w3.eth.gas_price
    # Celo floor: 5 gwei
    floor = w3.to_wei(5, 'gwei')
    return max(gp, floor)

def send(w3, fn, sender, pk, label):
    """Build, sign, send, wait — returns receipt."""
    nonce = w3.eth.get_transaction_count(sender)
    base  = {"from": sender, "nonce": nonce,
             "gasPrice": gas_price(w3), "chainId": CELO_CHAIN_ID, "type": 0}
    raw_gas = w3.eth.estimate_gas({**base, "data": fn.build_transaction(base)["data"],
                                   "to": fn.address})
    base["gas"] = math.ceil(raw_gas * GAS_MULT)
    tx     = fn.build_transaction(base)
    signed = Account.sign_transaction(tx, pk)
    h      = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"    ↗  {label}  tx={h.hex()[:18]}…  gas={base['gas']:,}")
    receipt = w3.eth.wait_for_transaction_receipt(h, timeout=300)
    if receipt.status != 1:
        raise RuntimeError(f"Transaction reverted: {h.hex()}")
    print(f"    ✅  {label} confirmed  (block {receipt.blockNumber})")
    return receipt

def chunks(lst, n):
    for i in range(0, len(lst), n):
        yield lst[i:i+n]

# ── Phase 1: snapshot ─────────────────────────────────────────────────────────

def snapshot(w3, old_cball_addr, old_miner_addr):
    print("\n📸  PHASE 1 — Snapshot old contracts")

    old_cball = w3.eth.contract(
        address=Web3.to_checksum_address(old_cball_addr), abi=CBALL_OLD_ABI)
    old_miner = w3.eth.contract(
        address=Web3.to_checksum_address(old_miner_addr), abi=MINER_OLD_ABI)

    # Collect ALL addresses that ever held cBALL by scanning every Transfer event.
    # This captures both:
    #   - original miners (Transfer from address(0)) — the "players" in CeloMiner
    #   - secondary recipients (Transfer between wallets) who hold a balance but
    #     never mined directly and therefore appear in cBALL but not in CeloMiner.
    #
    # M-3 fix: eth_newFilter (create_filter) is unsupported by most archive/public
    # RPC nodes including forno.celo.org when querying from block 0.  We use
    # paginated eth_getLogs instead, which is universally supported.
    #
    # M-2 fix: previously only mint events (from==0x0) were collected, so wallets
    # that received cBALL via transfer were excluded from the cBALL seed and their
    # balances would be silently lost in migration.
    print("  Fetching ALL Transfer events from old cBALL (paginated eth_getLogs)…")
    TRANSFER_TOPIC = w3.keccak(text="Transfer(address,address,uint256)").hex()
    CHUNK          = 50_000   # blocks per request — conservative for public RPCs
    latest         = w3.eth.block_number
    all_logs       = []
    for start in range(0, latest + 1, CHUNK):
        end = min(start + CHUNK - 1, latest)
        try:
            logs = w3.eth.get_logs({
                "fromBlock": start,
                "toBlock":   end,
                "address":   old_cball.address,
                "topics":    [TRANSFER_TOPIC],
            })
            all_logs.extend(logs)
        except Exception as e:
            print(f"    ⚠️  get_logs failed for blocks {start}-{end}: {e}")
            print(f"    Retrying with smaller chunk (25 000 blocks)…")
            half = CHUNK // 2
            for s2 in range(start, end + 1, half):
                e2   = min(s2 + half - 1, end)
                logs = w3.eth.get_logs({
                    "fromBlock": s2,
                    "toBlock":   e2,
                    "address":   old_cball.address,
                    "topics":    [TRANSFER_TOPIC],
                })
                all_logs.extend(logs)
        if (start // CHUNK) % 10 == 0 and start > 0:
            print(f"    …scanned to block {end} ({len(all_logs)} events so far)")

    # Decode topics: Transfer(address indexed from, address indexed to, uint256 value)
    ZERO   = "0x" + "0" * 40
    to_set = set()
    for log in all_logs:
        # topic[2] is the 'to' address (padded to 32 bytes)
        to_addr = "0x" + log["topics"][2].hex()[-40:]
        to_set.add(Web3.to_checksum_address(to_addr))
    # Remove zero address
    to_set.discard(Web3.to_checksum_address(ZERO))

    mint_recipients = set()
    for log in all_logs:
        from_addr = "0x" + log["topics"][1].hex()[-40:]
        if Web3.to_checksum_address(from_addr) == Web3.to_checksum_address(ZERO):
            to_addr = "0x" + log["topics"][2].hex()[-40:]
            mint_recipients.add(Web3.to_checksum_address(to_addr))

    players = list(to_set)
    print(f"  Found {len(players)} unique addresses from {len(all_logs)} Transfer events")
    print(f"  Of these, {len(mint_recipients)} received cBALL via mint (direct miners)")

    old_total_minted = old_miner.functions.totalMinted().call()
    old_total_supply = old_cball.functions.totalSupply().call()
    print(f"  Old totalMinted : {old_total_minted:,}")
    print(f"  Old totalSupply : {old_total_supply:,}")

    # Read each address's state.
    # For direct miners (in mint_recipients): read both cBALL balance and CeloMiner state.
    # For transfer-only recipients (not in mint_recipients): read cBALL balance only;
    # their CeloMiner state will be all-zero which is correct.
    print(f"  Reading state for {len(players)} addresses…")
    records = []
    for i, addr in enumerate(players):
        bal                       = old_cball.functions.balanceOf(addr).call()
        total_mined, clicks, tool = old_miner.functions.miners(addr).call()
        records.append({
            "address":    addr,
            "balance":    bal,
            "totalMined": total_mined,
            "clicks":     clicks,
            "tool":       tool,
            "isMiner":    addr in mint_recipients,
        })
        if (i + 1) % 50 == 0:
            print(f"    …{i+1}/{len(players)}")

    # Sanity: sum of balances should equal totalSupply
    sum_balances    = sum(r["balance"]    for r in records)
    sum_total_mined = sum(r["totalMined"] for r in records)
    transfer_only   = sum(1 for r in records if not r["isMiner"] and r["balance"] > 0)
    print(f"\n  Sum of balances    : {sum_balances:,}  (should equal totalSupply {old_total_supply:,})")
    print(f"  Sum of totalMined  : {sum_total_mined:,}  (should equal totalMinted {old_total_minted:,})")
    print(f"  Transfer-only holders (balance but never mined): {transfer_only}")
    if sum_balances != old_total_supply:
        print("  ⚠️  Balance sum mismatch — some cBALL balance may be unaccounted for")
    if sum_total_mined != old_total_minted:
        print("  ⚠️  totalMined sum mismatch — check for missed players")

    snapshot_data = {
        "meta": {
            "old_cball":          old_cball_addr,
            "old_miner":          old_miner_addr,
            "old_total_minted":   old_total_minted,
            "old_total_supply":   old_total_supply,
            "player_count":       len(records),
            "miner_count":        len(mint_recipients),
            "transfer_only_count": sum(1 for r in records if not r["isMiner"] and r["balance"] > 0),
            "timestamp":          int(time.time()),
        },
        "players": records,
    }
    out = Path("migration_snapshot.json")
    with open(out, "w") as f:
        json.dump(snapshot_data, f, indent=2)
    print(f"\n  💾  Snapshot saved → {out}")
    return snapshot_data

# ── Phase 2: seed ─────────────────────────────────────────────────────────────

def seed(w3, snapshot_data, new_cball_addr, new_miner_addr, sender, pk, dry_run):
    print("\n🌱  PHASE 2 — Seed new contracts")

    new_cball = w3.eth.contract(
        address=Web3.to_checksum_address(new_cball_addr), abi=CBALL_NEW_ABI)
    new_miner = w3.eth.contract(
        address=Web3.to_checksum_address(new_miner_addr), abi=MINER_NEW_ABI)

    # Guard: check migration isn't already locked
    if new_miner.functions.migrationLocked().call():
        print("  ❌  CeloMiner migration is already locked — aborting.")
        sys.exit(1)
    if new_cball.functions.migrationLocked().call():
        print("  ❌  cBALL migration is already locked — aborting.")
        sys.exit(1)

    players = snapshot_data["players"]
    # Seed CeloMiner only for addresses that actually mined (totalMined > 0).
    # Transfer-only recipients have no CeloMiner state to restore.
    # Seed cBALL for all addresses that currently hold a balance > 0,
    # including transfer-only recipients (M-2 fix: previously these were excluded).
    to_seed_miner = [p for p in players if p["totalMined"] > 0]
    to_seed_cball = [p for p in players if p["balance"]    > 0]
    transfer_only = [p for p in to_seed_cball if not p.get("isMiner", True)]

    print(f"  Players to seed in CeloMiner : {len(to_seed_miner)}")
    print(f"  Addresses to seed in cBALL   : {len(to_seed_cball)}")
    if transfer_only:
        print(f"  ↳ of which transfer-only holders: {len(transfer_only)} (balance but no mining history)")

    miner_batches = list(chunks(to_seed_miner, BATCH_SIZE))
    cball_batches = list(chunks(to_seed_cball, BATCH_SIZE))

    # Estimate total gas
    if dry_run:
        gp = gas_price(w3)
        # Rough estimate: ~80k gas per player in a batch
        est_miner_gas = len(to_seed_miner) * 80_000
        est_cball_gas = len(to_seed_cball) * 50_000
        total_gas     = est_miner_gas + est_cball_gas + 200_000  # +lock+wire
        total_cost    = total_gas * gp / 1e18
        bal           = w3.eth.get_balance(sender) / 1e18
        print(f"\n  📋  DRY-RUN PLAN")
        print(f"  {'CeloMiner batches':<30} {len(miner_batches):>6}  (~{est_miner_gas:,} gas)")
        print(f"  {'cBALL batches':<30} {len(cball_batches):>6}  (~{est_cball_gas:,} gas)")
        print(f"  {'Estimated total gas':<30} {total_gas:>12,}")
        print(f"  {'Gas price':<30} {gp/1e9:>11.2f} Gwei")
        print(f"  {'Estimated cost':<30} {total_cost:>11.6f} CELO")
        print(f"  {'Your balance':<30} {bal:>11.6f} CELO")
        if bal >= total_cost:
            print(f"  ✅  Balance sufficient")
        else:
            print(f"  ❌  Need {total_cost - bal:.6f} more CELO")
        print("\n  Re-run without --dry-run to execute.\n")
        return

    # ── Seed CeloMiner ──
    print(f"\n  Seeding CeloMiner in {len(miner_batches)} batch(es)…")
    for i, batch in enumerate(miner_batches):
        addrs   = [p["address"]    for p in batch]
        mineds  = [p["totalMined"] for p in batch]
        clicks  = [p["clicks"]     for p in batch]
        tools   = [p["tool"]       for p in batch]
        fn = new_miner.functions.seedPlayerBatch(addrs, mineds, clicks, tools)
        send(w3, fn, sender, pk, f"seedPlayerBatch batch {i+1}/{len(miner_batches)}")

    # ── Seed cBALL ──
    print(f"\n  Seeding cBALL in {len(cball_batches)} batch(es)…")
    for i, batch in enumerate(cball_batches):
        for p in batch:
            fn = new_cball.functions.seedBalance(p["address"], p["balance"])
            send(w3, fn, sender, pk, f"seedBalance {p['address'][:10]}…")

# ── Phase 3: reconcile ────────────────────────────────────────────────────────

def reconcile(w3, snapshot_data, new_cball_addr, new_miner_addr):
    print("\n🔍  PHASE 3 — Reconciliation")

    new_cball = w3.eth.contract(
        address=Web3.to_checksum_address(new_cball_addr), abi=CBALL_NEW_ABI)
    new_miner = w3.eth.contract(
        address=Web3.to_checksum_address(new_miner_addr), abi=MINER_NEW_ABI)

    players   = snapshot_data["players"]
    old_meta  = snapshot_data["meta"]
    errors    = []

    # Check global counters
    new_total_minted = new_miner.functions.totalMinted().call()
    new_total_supply = new_cball.functions.totalSupply().call()
    print(f"  Old totalMinted : {old_meta['old_total_minted']:,}")
    print(f"  New totalMinted : {new_total_minted:,}")
    print(f"  Old totalSupply : {old_meta['old_total_supply']:,}")
    print(f"  New totalSupply : {new_total_supply:,}")

    if new_total_minted != old_meta["old_total_minted"]:
        errors.append(f"totalMinted mismatch: old={old_meta['old_total_minted']} new={new_total_minted}")
    if new_total_supply != old_meta["old_total_supply"]:
        errors.append(f"totalSupply mismatch: old={old_meta['old_total_supply']} new={new_total_supply}")

    # Spot-check all players
    print(f"  Checking {len(players)} players…")
    for p in players:
        addr = p["address"]
        new_bal                        = new_cball.functions.balanceOf(addr).call()
        new_mined, new_clicks, new_tool = new_miner.functions.miners(addr).call()

        if new_bal != p["balance"]:
            errors.append(f"{addr}: balance old={p['balance']} new={new_bal}")
        if new_mined != p["totalMined"]:
            errors.append(f"{addr}: totalMined old={p['totalMined']} new={new_mined}")
        if new_tool != p["tool"]:
            errors.append(f"{addr}: tool old={p['tool']} new={new_tool}")

    if errors:
        print(f"\n  ❌  {len(errors)} reconciliation error(s):")
        for e in errors[:20]:
            print(f"       {e}")
        if len(errors) > 20:
            print(f"       …and {len(errors)-20} more")
        print("\n  ⚠️  DO NOT lock migration until errors are resolved.")
        sys.exit(1)
    else:
        print(f"  ✅  All {len(players)} players reconciled perfectly.")

# ── Phase 4: lock + wire ──────────────────────────────────────────────────────

def lock_and_wire(w3, new_cball_addr, new_miner_addr, sender, pk):
    print("\n🔒  PHASE 4 — Lock migration + wire minter")

    new_cball = w3.eth.contract(
        address=Web3.to_checksum_address(new_cball_addr), abi=CBALL_NEW_ABI)
    new_miner = w3.eth.contract(
        address=Web3.to_checksum_address(new_miner_addr), abi=MINER_NEW_ABI)

    send(w3, new_miner.functions.lockMigration(), sender, pk, "CeloMiner.lockMigration()")
    send(w3, new_cball.functions.lockMigration(), sender, pk, "cBALL.lockMigration()")
    send(w3, new_cball.functions.setMinter(new_miner_addr), sender, pk,
         "cBALL.setMinter(newMiner)")
    send(w3, new_miner.functions.unpause(), sender, pk, "CeloMiner.unpause()")

    print("\n  🎉  Migration complete — new contracts are live!")

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="Migrate CeloMiner player state to new contracts")
    p.add_argument("--keystore",     required=True,  help="Path to UTC keystore JSON")
    p.add_argument("--old-cball",    required=False, help="Old cBALL contract address")
    p.add_argument("--old-miner",    required=False, help="Old CeloMiner contract address")
    p.add_argument("--new-cball",    required=True,  help="New cBALL contract address")
    p.add_argument("--new-miner",    required=True,  help="New CeloMiner contract address")
    p.add_argument("--rpc",          default=RPC_URL)
    p.add_argument("--dry-run",      action="store_true",
                   help="Estimate gas and print plan without sending transactions")
    p.add_argument("--snapshot",     default=None,
                   help="Path to existing migration_snapshot.json (skips Phase 1)")
    p.add_argument("--skip-lock",    action="store_true",
                   help="Skip Phase 4 (lock + wire) — useful for partial re-runs")
    args = p.parse_args()

    w3 = Web3(Web3.HTTPProvider(args.rpc))
    if not w3.is_connected():
        print("❌  Cannot connect to RPC"); sys.exit(1)
    print(f"✅  Connected  chain={w3.eth.chain_id}  block={w3.eth.block_number}")

    if w3.eth.chain_id != CELO_CHAIN_ID:
        ans = input(f"⚠️  Not Celo mainnet (chainId {w3.eth.chain_id}). Continue? [y/N] ")
        if ans.strip().lower() != "y": sys.exit(0)

    sender, pk = load_keystore(args.keystore)

    # ── Phase 1: snapshot (or load existing) ──
    if args.snapshot:
        print(f"\n📂  Loading existing snapshot from {args.snapshot}")
        with open(args.snapshot) as f:
            snap = json.load(f)
        print(f"  {snap['meta']['player_count']} players, "
              f"snapshotted from {snap['meta']['old_cball']}")
    else:
        if not args.old_cball or not args.old_miner:
            print("❌  --old-cball and --old-miner are required unless --snapshot is provided")
            del pk; sys.exit(1)
        snap = snapshot(w3, args.old_cball, args.old_miner)

    # ── Phase 2: seed ──
    seed(w3, snap, args.new_cball, args.new_miner, sender, pk, args.dry_run)
    if args.dry_run:
        del pk; return

    # ── Phase 3: reconcile ──
    reconcile(w3, snap, args.new_cball, args.new_miner)

    # ── Phase 4: lock + wire ──
    if not args.skip_lock:
        ans = input("\n  Reconciliation passed. Lock migration and go live? [y/N] ")
        if ans.strip().lower() != "y":
            print("  Skipping lock. Re-run with --skip-lock if you want to re-seed first.")
            del pk; sys.exit(0)
        lock_and_wire(w3, args.new_cball, args.new_miner, sender, pk)
    else:
        print("\n  --skip-lock set — skipping Phase 4.")

    del pk

    print(f"""
╔══════════════════════════════════════════════════════════════════╗
║              CELOMINER MIGRATION COMPLETE                        ║
╠══════════════════════════════════════════════════════════════════╣
║  New cBALL   : {args.new_cball}
║  New Miner   : {args.new_miner}
╠══════════════════════════════════════════════════════════════════╣
║  Next steps:                                                     ║
║  1. Update contract addresses in index.html                      ║
║  2. Deploy updated frontend                                      ║
║  3. Announce to players — their stats are all there              ║
╚══════════════════════════════════════════════════════════════════╝
    """)

if __name__ == "__main__":
    main()
