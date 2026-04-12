# CeloMiner Frontend

A multichain dApp frontend for the CeloMiner game contracts. Single-file, zero build step.

## Features

- **Mine cBALL** — click to mine, watch your tool upgrade in real-time
- **Token panel** — transfer, approve, check allowances
- **Badges panel** — claim Common / Rare / Legendary NFTs with eligibility gating
- **Leaderboard** — top-10 miners with live data
- **Admin panel** — pause/unpause, minter management, ownership transfer (two-step)
- **Multichain** — Celo Mainnet, Alfajores, Ethereum, Sepolia, Polygon, Base
- **Persistent config** — contract addresses saved to localStorage per browser

## Deployment

### GitHub Pages
1. Push `index.html` to a repo.
2. Settings → Pages → Source: `main` branch, `/ (root)`.
3. Visit `https://<user>.github.io/<repo>/`.

### Netlify
1. Drag the folder to [netlify.com/drop](https://app.netlify.com/drop), or connect your repo.
2. `netlify.toml` handles redirects and headers automatically.

### Vercel
1. `vercel --prod` from the folder, or connect your GitHub repo in the Vercel dashboard.
2. `vercel.json` handles rewrites and security headers automatically.

## Usage

1. Open the app and connect MetaMask (or any EIP-1193 wallet).
2. Enter your deployed contract addresses in the **Contract Addresses** bar at the top.
3. Click **Apply Addresses** — addresses are saved to localStorage for future visits.
4. Select the correct network from the chain badge in the top-right.
5. Start mining!

## Contract Addresses

Paste your deployed addresses from `deployment.json` into the config bar:

| Field      | Contract    |
|------------|-------------|
| cBALL Token | `cBALL`    |
| CeloMiner  | `CeloMiner` |
| MinerBadge | `MinerBadge`|

## Security Notes

- All write operations require wallet signature — no private keys are ever handled.
- The frontend is fully client-side with no backend.
- Admin functions (pause, minter management, ownership transfer) revert on-chain if called from a non-owner address.
