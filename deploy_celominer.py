#!/usr/bin/env python3
"""
deploy_celominer.py
Compiles and deploys cBALL → CeloMiner → MinerBadge,
then wires them together on Celo mainnet.

Note: CeloMinerOnboard is not included in this deployment.
      Add it back when the contract is ready.

Requirements:
  pip install web3 py-solc-x

Usage:
  # Dry-run (estimate gas, check balance, print plan — no transactions sent):
  python deploy_celominer.py --keystore ./UTC-keystore.json --dry-run

  # Live deployment:
  python deploy_celominer.py --keystore ./UTC-keystore.json

After deployment, paste the printed addresses into index.html.

Security notes:
  L-09: The private key bytes object is used only inside deploy() and send_tx()
        calls and is deleted from the local namespace immediately after the final
        signing call.  This does not guarantee the bytes are erased from CPython's
        heap (CPython does not zero memory on deallocation), but it does prevent
        accidental logging or serialisation of the variable for the remainder of
        the process lifetime.  For production deployments, prefer a hardware wallet
        or a secrets manager that never exposes the raw key to this process at all.
  I-04: Gas limits are estimated per-transaction (with a 20 % safety buffer) rather
        than hard-coded, so no deployment fails with out-of-gas if the contract's
        actual cost exceeds a fixed constant.
  M-04: Contract source paths are now anchored to the directory containing this
        script (Path(__file__).parent) rather than the process working directory,
        so the script works correctly regardless of where it is invoked from.
  I-03: --dry-run flag added. In dry-run mode the script estimates gas for every
        transaction, checks the deployer's CELO balance, and prints the full
        deployment plan without submitting any transactions to the network.
"""

import json
import getpass
import argparse
import os
import sys
import math
from pathlib import Path

try:
    from web3 import Web3
    from eth_account import Account
except ImportError:
    print("pip install web3")
    sys.exit(1)

try:
    from solcx import compile_files, install_solc, get_installed_solc_versions
except ImportError:
    print("pip install py-solc-x")
    sys.exit(1)

SOLC_VERSION = "0.8.20"

RPC_URL = os.environ.get("CELO_RPC_URL", "https://forno.celo.org")
CELO_CHAIN_ID = 42220

# I-04: safety multiplier applied to gas estimates to absorb minor EVM
# discrepancies between estimate_gas() and actual execution cost.
GAS_ESTIMATE_MULTIPLIER = 1.2


def ensure_solc():
    installed = get_installed_solc_versions()
    if not any(str(v) == SOLC_VERSION for v in installed):
        print(f"Installing solc {SOLC_VERSION}...")
        install_solc(SOLC_VERSION)


def compile_contracts():
    # M-04: paths are anchored to the script's own directory so the script works
    #       regardless of the working directory from which it is invoked.
    script_dir = Path(__file__).parent
    sol_files  = [
        str(script_dir / "cBALL.sol"),
        str(script_dir / "CeloMiner.sol"),
        str(script_dir / "MinerBadge.sol"),
    ]
    print("Compiling contracts...")
    result = compile_files(
        sol_files,
        output_values=["abi", "bin"],
        solc_version=SOLC_VERSION,
        optimize=True,
        optimize_runs=200,
    )
    cball_key = next(k for k in result if k.endswith(":cBALL"))
    miner_key = next(k for k in result if k.endswith(":CeloMiner"))
    badge_key = next(k for k in result if k.endswith(":MinerBadge"))
    return result[cball_key], result[miner_key], result[badge_key]


def load_keystore(path):
    ks_path = Path(path).expanduser()
    with open(ks_path) as f:
        keystore = json.load(f)
    password = getpass.getpass("🔑  Keystore password: ")
    private_key = Account.decrypt(keystore, password)
    acct = Account.from_key(private_key)
    print(f"👛  Deployer: {acct.address}")
    # Return the key as raw bytes; the hex string is never stored separately.
    return acct.address, private_key


def _estimate_gas(w3, tx_dict):
    """Return estimated gas with GAS_ESTIMATE_MULTIPLIER headroom (I-04)."""
    raw = w3.eth.estimate_gas(tx_dict)
    return math.ceil(raw * GAS_ESTIMATE_MULTIPLIER)


def deploy(w3, abi, bytecode, constructor_args, sender, private_key, label):
    print(f"\n🚀  Deploying {label}...")
    contract = w3.eth.contract(abi=abi, bytecode=bytecode)
    nonce = w3.eth.get_transaction_count(sender)

    # I-04: estimate gas instead of using a hard-coded constant
    base_tx = {
        "from": sender,
        "nonce": nonce,
        "gasPrice": w3.eth.gas_price,
        "chainId": w3.eth.chain_id,
    }
    gas = _estimate_gas(w3, contract.constructor(*constructor_args).build_transaction(base_tx))
    base_tx["gas"] = gas

    tx = contract.constructor(*constructor_args).build_transaction(base_tx)
    signed = Account.sign_transaction(tx, private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"    Tx:      {tx_hash.hex()}  (gas limit: {gas})")
    print(f"    Waiting for confirmation...")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=300)
    print(f"    ✅  {label} deployed at {receipt.contractAddress}")
    return receipt.contractAddress


def send_tx(w3, contract_fn, sender, private_key, label):
    print(f"\n🔗  {label}...")
    nonce = w3.eth.get_transaction_count(sender)

    # I-04: estimate gas instead of using a hard-coded constant
    base_tx = {
        "from": sender,
        "nonce": nonce,
        "gasPrice": w3.eth.gas_price,
        "chainId": w3.eth.chain_id,
    }
    gas = _estimate_gas(w3, contract_fn.build_transaction(base_tx))
    base_tx["gas"] = gas

    tx = contract_fn.build_transaction(base_tx)
    signed = Account.sign_transaction(tx, private_key)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    w3.eth.wait_for_transaction_receipt(tx_hash)
    print(f"    ✅  Done!")


def save_artifacts(addresses, abis):
    # M-05: anchor output paths to the script's own directory so that ABI files
    #       and deployment.json land next to index.html regardless of the working
    #       directory from which the script is invoked.  This mirrors the M-04
    #       fix already applied to compile_contracts().
    script_dir = Path(__file__).parent
    for name, abi in abis.items():
        fname = script_dir / f"{name.lower()}_abi.json"
        with open(fname, "w") as f:
            json.dump(abi, f, indent=2)
    print(f"\n📄  ABIs saved: cball_abi.json, celominer_abi.json, minerbadge_abi.json")

    with open(script_dir / "deployment.json", "w") as f:
        json.dump(addresses, f, indent=2)
    print(f"📄  Addresses saved: deployment.json")


def estimate_deploy_gas(w3, abi, bytecode, constructor_args, sender):
    """Return gas estimate for a deployment transaction (used in dry-run)."""
    contract = w3.eth.contract(abi=abi, bytecode=bytecode)
    base_tx = {
        "from": sender,
        "nonce": w3.eth.get_transaction_count(sender),
        "gasPrice": w3.eth.gas_price,
        "chainId": w3.eth.chain_id,
    }
    return _estimate_gas(w3, contract.constructor(*constructor_args).build_transaction(base_tx))


def dry_run(w3, sender, cball_art, miner_art, badge_art):
    """
    I-03: Estimate gas for all transactions, print a deployment plan, and
    check that the deployer's balance covers the estimated total cost.
    No transactions are submitted.
    """
    print("\n📋  DRY-RUN MODE — no transactions will be sent\n")
    gas_price = w3.eth.gas_price
    balance   = w3.eth.get_balance(sender)

    steps = [
        ("cBALL deploy",      lambda: estimate_deploy_gas(w3, cball_art["abi"], cball_art["bin"], [],              sender)),
        # Pass sender as the owner_ placeholder — non-zero, satisfies the constructor
        # guard, and gives an accurate gas estimate for both new constructor params.
        ("CeloMiner deploy",  lambda: estimate_deploy_gas(w3, miner_art["abi"], miner_art["bin"], [sender, sender], sender)),
        ("MinerBadge deploy", lambda: estimate_deploy_gas(w3, badge_art["abi"], badge_art["bin"], [sender, sender], sender)),
    ]

    total_gas = 0
    print(f"  {'Step':<30}  {'Gas (est.)':>12}  {'Cost (CELO)':>14}")
    print(f"  {'-'*30}  {'-'*12}  {'-'*14}")
    for label, estimator in steps:
        try:
            gas = estimator()
        except Exception as e:
            print(f"  {label:<30}  ESTIMATE FAILED: {e}")
            continue
        cost_wei  = gas * gas_price
        cost_celo = cost_wei / 1e18
        total_gas += gas
        print(f"  {label:<30}  {gas:>12,}  {cost_celo:>14.6f}")

    # setMinter is a simple state-writing call; 80,000 gas is a safe upper bound.
    set_minter_gas  = 80_000
    total_gas      += set_minter_gas
    set_minter_cost = set_minter_gas * gas_price / 1e18
    print(f"  {'setMinter call':<30}  {set_minter_gas:>12,}  {set_minter_cost:>14.6f}")

    total_cost_wei  = total_gas * gas_price
    total_cost_celo = total_cost_wei / 1e18
    balance_celo    = balance / 1e18
    print(f"\n  Total estimated gas : {total_gas:,}")
    print(f"  Gas price           : {gas_price / 1e9:.2f} Gwei")
    print(f"  Total estimated cost: {total_cost_celo:.6f} CELO")
    print(f"  Deployer balance    : {balance_celo:.6f} CELO")

    if balance >= total_cost_wei:
        print(f"\n  ✅  Balance sufficient — {balance_celo - total_cost_celo:.6f} CELO will remain after deployment.")
    else:
        shortfall = (total_cost_wei - balance) / 1e18
        print(f"\n  ❌  Insufficient balance — need {shortfall:.6f} more CELO to deploy.")

    print("\n  Re-run without --dry-run to deploy.\n")


def main():
    parser = argparse.ArgumentParser(description="Deploy CeloMiner game contracts to Celo mainnet")
    parser.add_argument("--keystore", required=True)
    parser.add_argument("--rpc", default=RPC_URL)
    # I-03: dry-run flag — estimate gas and check balance without sending transactions
    parser.add_argument("--dry-run", action="store_true",
                        help="Estimate gas and check balance without submitting any transactions")
    # L-05: optional deployer check — aborts early if the decrypted key does not
    #        match the expected address, protecting against loading the wrong keystore.
    parser.add_argument("--expected-deployer", default=None,
                        help="Expected deployer address (0x…). Script aborts if decrypted "
                             "key does not match. Use this to guard against loading the "
                             "wrong keystore file.")
    args = parser.parse_args()

    w3 = Web3(Web3.HTTPProvider(args.rpc))
    if not w3.is_connected():
        print("❌  Cannot connect to RPC"); sys.exit(1)

    chain_id = w3.eth.chain_id
    print(f"✅  Connected  chain={chain_id}  block={w3.eth.block_number}")

    if chain_id != CELO_CHAIN_ID:
        print(f"⚠️   WARNING: Expected Celo mainnet (chainId {CELO_CHAIN_ID}), got chainId {chain_id}")
        confirm = input("Continue anyway? [y/N] ")
        if confirm.strip().lower() != "y":
            sys.exit(0)

    sender, private_key = load_keystore(args.keystore)

    # L-05: if the caller supplied --expected-deployer, abort early if the
    #        decrypted key does not match.  This guards against accidentally
    #        loading a keystore for the wrong account.
    if args.expected_deployer:
        expected = Web3.to_checksum_address(args.expected_deployer)
        actual   = Web3.to_checksum_address(sender)
        if actual != expected:
            print(f"❌  Deployer mismatch: expected {expected}, got {actual}")
            del private_key
            sys.exit(1)
        print(f"✅  Deployer address confirmed: {actual}")

    ensure_solc()
    cball_art, miner_art, badge_art = compile_contracts()

    # I-03: if --dry-run is set, print the plan and exit without deploying
    if args.dry_run:
        dry_run(w3, sender, cball_art, miner_art, badge_art)
        # Private key not needed in dry-run; delete it immediately
        del private_key
        return

    # Deploy in dependency order
    cball_address = deploy(w3, cball_art["abi"], cball_art["bin"], [],                           sender, private_key, "cBALL")
    miner_address = deploy(w3, miner_art["abi"], miner_art["bin"], [cball_address, sender],      sender, private_key, "CeloMiner")
    badge_address = deploy(w3, badge_art["abi"], badge_art["bin"], [miner_address, sender],      sender, private_key, "MinerBadge")

    # Wire: set CeloMiner as cBALL minter
    cball_contract = w3.eth.contract(address=cball_address, abi=cball_art["abi"])
    send_tx(w3, cball_contract.functions.setMinter(miner_address), sender, private_key,
            "Setting CeloMiner as cBALL minter")

    # L-09: private key no longer needed — delete it from local namespace.
    # Note: CPython does not zero the underlying memory buffer on deallocation,
    # so this does not guarantee the key bytes are erased from process memory.
    # It does prevent the variable from being accessed for the rest of the
    # process lifetime (e.g. via gc.get_objects() introspection).
    del private_key

    addresses = {
        "cBALL":     cball_address,
        "CeloMiner": miner_address,
        "MinerBadge": badge_address,
    }

    save_artifacts(addresses, {
        "cball":     cball_art["abi"],
        "celominer": miner_art["abi"],
        "minerbadge": badge_art["abi"],
    })

    celoscan_base = "https://celoscan.io/address"

    print(f"""
╔══════════════════════════════════════════════════════════════════╗
║                CELOMINER DEPLOYMENT COMPLETE                     ║
╠══════════════════════════════════════════════════════════════════╣
║  Network:     Celo Mainnet (chainId 42220)                       ║
║  cBALL Token: {cball_address}
║  CeloMiner:   {miner_address}
║  MinerBadge:  {badge_address}
╠══════════════════════════════════════════════════════════════════╣
║  Next steps:                                                     ║
║  1. Paste addresses into index.html (top of <script>)            ║
║  2. Open index.html in a browser with MetaMask on Celo           ║
║  3. Start mining!                                                ║
╚══════════════════════════════════════════════════════════════════╝
    """)

    print(f"  cBALL:     {celoscan_base}/{cball_address}")
    print(f"  CeloMiner: {celoscan_base}/{miner_address}")
    print(f"  MinerBadge:{celoscan_base}/{badge_address}")


if __name__ == "__main__":
    main()
