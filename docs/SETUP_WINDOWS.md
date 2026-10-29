# Windows PC: one read-only MT5 snapshot (about 30 minutes the first time)

**Purpose.** Deriv's public data feed gives the index price but not your MT5 **spread**, the cost every trade pays. This step reads the spread, swaps and contract details from your Deriv **demo** account, saves them to one small file, and sends that file to the VPS. **It places no trades.**

You do this once now, and again only if Deriv changes its spreads.

## 1. Install Python (5 min)

1. Open <https://www.python.org/downloads/windows/>. Under **Python 3.12**, download **Windows installer (64-bit)**.
2. Run it. On the first screen, **tick "Add python.exe to PATH"**, then click **Install Now**.
3. Open **PowerShell** (Start menu → type `PowerShell`) and run:
   ```powershell
   py --version
   ```
   It should print `Python 3.12.x`.

## 2. Install MT5 and log in to your Deriv demo account (10–15 min)

1. Log in at deriv.com and open **Trader's Hub**. Switch to **Demo**.
2. Under **CFDs**, find the **MT5** account that offers **Derived** indices, often labelled *Standard* or *Derived*. Click **Get** or **Open** and choose a password.
3. On that account, find **MT5 for Windows**. Download it, install it and open it.
4. In MT5: **File → Login to Trade Account**. Enter the **login number** and **server** shown on that account in Trader's Hub, plus your MT5 password.
   **Don't paste these into any chat. Nobody here needs them.**
5. In MT5, open **View → Market Watch**. Right-click → **Symbols**, search `Volatility 75`, and check that it is listed.

Leave **"Algorithmic trading" switched OFF** (the toolbar button stays grey). This script only reads data and doesn't need it. With it off, the terminal refuses any automated order at all.

## 3. Run the snapshot (7 min, mostly waiting)

Keep MT5 open and logged in. In PowerShell:

```powershell
mkdir $HOME\deriv-research -Force; cd $HOME\deriv-research
Invoke-WebRequest https://raw.githubusercontent.com/pedrocti/deriv-research/main/windows/mt5_snapshot.py -OutFile mt5_snapshot.py
py -m pip install MetaTrader5==5.0.6231
py mt5_snapshot.py --minutes 5
```

Expected output:

```
Account: DEMO on Deriv-Demo. Found 11 of 11 instruments.
  1HZ100V    Volatility 100 (1s) Index    24h spread median ... pts
  ...
Sampling live spreads for 5 minutes ... (keep MT5 open)
Saved ./mt5_snapshot_2026....json
```

If it says **"Found 10 of 11"**, gold isn't on this account type. That's fine.

## 4. Send the file to the VPS (2 min)

Still in PowerShell, run this. It asks for your VPS root password, the same one you use for SSH:

```powershell
scp (Get-ChildItem mt5_snapshot_*.json | Select-Object -Last 1).FullName root@31.97.14.36:/tmp/mt5_snapshot.json
```

Then, in the VPS terminal:

```bash
dresearch import-mt5 /tmp/mt5_snapshot.json
```

Reload your status page. The **Pattern tests** table now shows your real spread, and a **no edge** or **open** verdict for each holding time.

## Troubleshooting

| Message | Fix |
| --- | --- |
| `py` is not recognised | Python wasn't added to PATH. Re-run the installer, choose **Modify**, and tick **Add Python to environment variables** |
| `Could not connect to MT5` | MT5 must be open and logged in. If you have several MT5 programs installed, open the Deriv one first |
| `MT5 is open but not logged in` | File → Login to Trade Account (step 2.4) |
| `Found 0 of 11` | You are logged in to an account without Derived indices. Use the demo account that lists Volatility indices |
| `scp` asks "Are you sure you want to continue connecting?" | Type `yes`. It happens only the first time |
| `scp: command not found` | Settings → Apps → Optional features → add **OpenSSH Client** |
