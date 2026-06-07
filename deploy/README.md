# Deploy the auto-apply agent to an Azure VM (live URL for judges)

This app drives a **real Chromium** to fill applications, so it needs a real machine —
an Azure VM is the right fit (headful browser works, dedicated IP, free DNS name, stays
up 30 days). The web UI streams screenshots + a live log over SSE, so judges watch the
agent work from the URL without seeing the actual browser window.

Cost: a `Standard_B2ms` (2 vCPU / 8 GB) is ~$60/mo — comfortably inside $300 of credits.
Chromium is RAM-hungry; don't go below 8 GB.

---

## 1. Create the VM (run locally, needs Azure CLI: `az login`)

```bash
az group create -n resume-rg -l eastus

az vm create -g resume-rg -n resume-vm \
  --image Ubuntu2204 --size Standard_B2ms \
  --admin-username azureuser --generate-ssh-keys \
  --public-ip-sku Standard \
  --public-ip-address-dns-name resume-apply-demo   # → resume-apply-demo.eastus.cloudapp.azure.com

# Open HTTP/HTTPS (Caddy terminates TLS and proxies to the app on :8000 internally)
az vm open-port -g resume-rg -n resume-vm --port 80  --priority 100
az vm open-port -g resume-rg -n resume-vm --port 443 --priority 110
```

Note the FQDN it prints (`...cloudapp.azure.com`) and the public IP.

## 2. Get the code + secrets onto the VM

```bash
VM=azureuser@resume-apply-demo.eastus.cloudapp.azure.com

ssh $VM 'git clone <YOUR_REPO_URL> ~/Resume_Builder'   # or rsync your local copy up

# .env, browser_profile/ and user_profiles/ are gitignored — copy them explicitly:
scp .env $VM:~/Resume_Builder/.env
scp -r user_profiles $VM:~/Resume_Builder/   # the demo profile the agent fills from
```

> The demo résumé (`samples/demo_resume.pdf`) ships via git automatically — judges apply by
> pasting only a job URL; the agent tailors that base to each job, applies, then deletes the
> tweaked copy. Just make sure `user_profiles/1/profile.json` has the demo email/phone set.

### Pre-login so judges NEVER hit a CAPTCHA (do this — it's the whole strategy)
A datacenter IP gets far more anti-bot friction than your home IP. Since judges only see the
web page, they **cannot solve a CAPTCHA/OTP on the VM's invisible browser** — so the plan is
to make challenges never appear by shipping a **pre-authenticated session**:

```bash
# 1. On your LAPTOP, log into LinkedIn once (a browser opens; solve any CAPTCHA here):
uv run python linkedin_url_extractor.py login      # writes linkedin_cookies.json + browser_profile/

# 2. Ship the saved session to the VM (all gitignored):
scp linkedin_cookies.json $VM:~/Resume_Builder/
scp -r browser_profile    $VM:~/Resume_Builder/    # copy while no browser is using it
```

With cookies + profile in place, the agent reuses your session and skips the login wall
entirely. **Also keep the judge sample chips on non-CAPTCHA portals** (Greenhouse / Lever /
Workday — the defaults) and avoid demoing arbitrary LinkedIn URLs, which is what triggers
challenges. If a one-off challenge ever appears, *you* (the operator) RDP/VNC into the VM and
clear it once — judges never deal with it.

## 3. Provision + start (run on the VM)

```bash
ssh $VM
bash ~/Resume_Builder/deploy/setup.sh
```

This installs Xvfb + Caddy + uv, runs `playwright install --with-deps chromium`, installs the
`resume-apply` systemd service (auto-restart, survives reboots), and drops the Caddyfile.

Then point Caddy at your FQDN:
```bash
sudo sed -i 's/YOUR_FQDN/resume-apply-demo.eastus.cloudapp.azure.com/' /etc/caddy/Caddyfile
sudo systemctl reload caddy
```

## 4. Verify

```bash
systemctl status resume-apply        # app running?
journalctl -u resume-apply -f        # live logs while you test
```

Open `https://resume-apply-demo.eastus.cloudapp.azure.com` → that's the **live URL for judges**.

---

## Submit safety
Per your choice, the agent runs **fully auto-submit** (`auto_submit=True`) — judges testing it
will send real applications to real employers using your profile/email. If you want to flip
to a "fill but stop before final submit" demo mode later, it's a one-line change in `app.py`
(`auto_submit=False`) — ask and I'll wire an env toggle.

## Operating notes
- **Keep it up 30 days:** the systemd unit auto-restarts on crash/reboot. Just don't
  deallocate the VM (a *stopped* VM stops billing but the URL goes down).
- **If a run looks blocked** (captcha/login wall), it's the datacenter IP — re-copy a fresh
  logged-in `browser_profile`, or do the portal login on the VM via VNC.
- **Logs/screenshots** land in `output/` on the VM.
