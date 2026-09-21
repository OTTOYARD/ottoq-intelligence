# Activating one-click deploy — a click-by-click runbook

**Who this is for:** Chase, or anyone non-technical. Every step says what to click, what to
copy and where to paste it. **Nothing here needs a terminal.** Written 2026-09-20.


> **SUPERSEDED — read `ACTIVATE_AWS_ACCESS.md` instead.** Chase has AWS Activate credits, so the
> AWS route is funded and strictly less work: **two** values instead of three, **no `.pem` key file
> to hunt for**, no security group to edit, and no port left open to the internet. This file is
> kept only as the fallback if AWS access cannot be granted for some reason.

**Time:** about 20 minutes, most of it hunting for one file on your computer.

**You do this once, ever.** After it works, every future code change deploys itself.

---

## What problem this solves, in plain words

There's a small Amazon server that runs one of our optimizer services. Five days ago the code for
it gained a new capability called CP-SAT. **But the copy actually running on that server was built
before that**, and it never rebuilds itself — it just restarts whatever was built last. So the
server is up, healthy, and cheerfully serving a version that has never heard of CP-SAT.

Rebuilding it currently means someone logging into that server by hand. This runbook gives GitHub
permission to do that for us, so it happens on a button press instead.

---

## Before you start — the one thing that might stop you

**You need the `.pem` key file for that server.** It was downloaded once, when the server was
created, and Amazon will not give you another copy. It's a small text file, probably named
something like `ottoq-intelligence.pem` or `ottoyard.pem`.

**Go look for it now, before anything else.** Check:

- your Downloads folder
- anywhere you keep AWS or server files
- search your whole computer for `.pem`
- on a Mac, also check `~/.ssh/`

**If you find it → carry on to Part 1.**

**If you cannot find it → stop and tell me.** Don't create a new key pair or change anything: a
lost key is recoverable but it's a different, longer procedure and I'd rather walk you through it
than have you guess. It is **not** a disaster.

---

## Part 1 — AWS: find the server and get its address

1. Open a browser and go to **https://console.aws.amazon.com/**
2. Sign in. (If it asks for an "IAM user" vs "Root user", use whichever you normally use.)
3. **Look at the top-right corner of the page.** There's a region name there, like
   *"N. Virginia"* or *"Ohio"*. **Write down what it says** — if the next step shows nothing,
   it's because the server lives in a different region and you'll need to switch to it here.
4. In the search box at the very top of the page, type **`EC2`** and click the result labelled
   **EC2** (it will say *"Virtual Servers in the Cloud"* underneath).
5. On the left-hand menu, click **Instances**.
6. You should see a list with at least one row. **Look for one named `ottoq-intelligence`.**
   - Its **Instance state** column should say **Running** with a green dot.
   - If you see no rows at all, go back to step 3 and change the region.
   - If you see several and none is named `ottoq-intelligence`, click each one and check —
     the name may have been left blank. Tell me what you find rather than guessing.
7. **Click on that row** (click the instance ID, the blue text starting `i-`).
8. A details panel opens. Find the field labelled **Public IPv4 address**. It looks like
   `12.34.56.78`.
9. **Copy that number.** There's a small copy icon next to it — click that rather than
   selecting the text, so you don't pick up a stray space.
10. **Paste it somewhere you can get at it again** — a sticky note, a text file, an email to
    yourself. You'll need it twice.

> **Note:** I have `98.93.216.144` in my notes from earlier, but I couldn't reach it to confirm,
> so **use what the console shows you, not my note.** If they differ, the console is right.

**Keep this browser tab open.** You come back to it in Part 3.

---

## Part 2 — Get the contents of the key file

You're not uploading the file. You're copying the **text inside it**.

1. Find the `.pem` file you located earlier.
2. Open it as **plain text**:
   - **Mac:** right-click the file → **Open With** → **TextEdit**
   - **Windows:** right-click → **Open with** → **Notepad**
   - If your computer offers to open it with something else, or says it doesn't know how,
     choose TextEdit or Notepad anyway. It is just text.
3. You should see something that starts with a line of dashes and the word BEGIN, like:

   ```
   -----BEGIN RSA PRIVATE KEY-----
   MIIEowIBAAKCAQEA...(many lines of jumbled letters and numbers)...
   -----END RSA PRIVATE KEY-----
   ```

   (It might say `OPENSSH PRIVATE KEY` instead of `RSA PRIVATE KEY`. Either is fine.)
4. **Select absolutely all of it** — click into the text, then press **Cmd+A** (Mac) or
   **Ctrl+A** (Windows).
5. **Copy it** — **Cmd+C** or **Ctrl+C**.

**Three things that will break this if you get them wrong:**

- ✅ **Include the `-----BEGIN` line and the `-----END` line.** They're part of the key.
- ✅ **Keep the line breaks.** Don't paste it into anything that joins it into one long line.
- ❌ **Don't add spaces at the start or end.**

**Leave it on your clipboard and go straight to Part 4** — you'll paste it within a minute or two.
If you get distracted and lose it, just come back and copy it again.

> **Is it safe to put this in GitHub?** Yes — GitHub secrets are encrypted, are never shown again
> after you save them (not even to you), and are hidden from all log output. This is the normal,
> intended way to do this.

---

## Part 3 — AWS: let GitHub reach the server

**This is the step I nearly forgot, and without it everything else fails.**

When the server was set up, it was told to accept logins **only from your home internet
connection**. GitHub's computers are not your home connection, so right now they'd be turned away
at the door.

1. Go back to your AWS browser tab (the instance details page from Part 1).
2. Click the **Security** tab (it's in the row of tabs partway down: *Details, Status and alarms,
   Monitoring, **Security**, Networking...*).
3. Under **Security groups** you'll see a blue link like `sg-0abc123... (launch-wizard-1)`.
   **Click it.**
4. You're now looking at that security group. Click the **Inbound rules** tab.
5. You should see a small table. Look for the row where **Port range** is **22**.
   - Its **Source** column probably shows a specific address ending in `/32` — that's your
     home connection.
6. Click the **Edit inbound rules** button (top right of that table).
7. Find the row for port **22** again. In its **Source** column there are two boxes.
   - Click the **left** box (it currently says *"My IP"* or *"Custom"*).
   - Choose **Anywhere-IPv4** from the dropdown.
   - The right box will fill in with `0.0.0.0/0` by itself.
8. **While you're here, check port 8080.** There should be another row with **Port range 8080**
   and **Source** `0.0.0.0/0`.
   - **If it's there:** leave it alone.
   - **If it's missing, or its source is your home address:** click **Add rule**, set
     **Type** to **Custom TCP**, **Port range** to **8080**, and **Source** to
     **Anywhere-IPv4**. (The deploy's final check reads the server's health from GitHub, so
     this has to be open too.)
9. Click **Save rules** at the bottom right.

### Is opening port 22 to the internet safe?

**Honest answer: it's an accepted tradeoff, not a free lunch.** Here's the real picture so you can
decide rather than trust me:

- AWS's Ubuntu images ship with **password logins switched off**. So what's exposed is key-only
  SSH — someone would need the actual `.pem` key, which guessing cannot produce.
- You **will** get automated login attempts from the internet. They will all fail. This is
  ordinary background noise for any internet-facing server.
- The tighter alternative is to restrict port 22 to GitHub's own address ranges — but GitHub
  publishes **hundreds** of those and changes them regularly, so maintaining that list by hand is
  not realistic.

**The genuinely better long-term fix** is to stop using SSH for this at all and use AWS's own
remote-command service (SSM) instead, which needs no open port and no key file. **That's a change
I can build** — I just can't build it without you first doing this part once, because I can't
reach the server to set it up. Say the word after this is working and I'll do it, and then you can
close port 22 again.

---

## Part 4 — GitHub: add the three secrets

1. Go to **https://github.com/OTTOYARD/ottoq-intelligence**
2. Click **Settings** (in the row of tabs across the top: *Code, Issues, Pull requests, ...,
   **Settings***). If you don't see it, you're not signed in as an account with admin rights.
3. On the left menu, find **Secrets and variables** and click it. It expands.
4. Click **Actions** underneath it.
5. You'll see a page with a green **New repository secret** button, top right. You'll use it
   three times.

**Secret 1 of 3:**
- Click **New repository secret**
- **Name:** `EC2_USER`
- **Secret:** `ubuntu`
- Click **Add secret**

**Secret 2 of 3:**
- Click **New repository secret**
- **Name:** `EC2_HOST`
- **Secret:** the IP address you copied in Part 1 (just the numbers and dots — no `http://`,
  no `:8080`, no spaces)
- Click **Add secret**

**Secret 3 of 3:**
- Click **New repository secret**
- **Name:** `EC2_SSH_KEY`
- **Secret:** paste the key text from Part 2 (**Cmd+V** / **Ctrl+V**)
- Click **Add secret**

**Names must match exactly** — all capitals, underscores not spaces or dashes. `EC2_HOST`, not
`ec2_host` or `EC2 HOST`.

When you're done the page lists three secrets. **You won't be able to see their values again** —
that's by design. If you think one is wrong, click **Update** on it and paste a fresh value.

---

## Part 5 — GitHub: press the button

1. Still in `OTTOYARD/ottoq-intelligence`, click the **Actions** tab at the top.
2. On the left you'll see a list of workflows. Click **deploy**.
3. On the right, click the grey **Run workflow** button.
4. A small panel drops down.
   - **Branch:** leave it as `main`.
   - **"Optimizer name that /health must list after deploy":** leave it as
     `cp_sat_forward_lex`.
5. Click the green **Run workflow** button in that panel.
6. Wait about 5 seconds and **refresh the page**. A new row appears at the top with a yellow
   spinning dot.
7. **Click that row**, then click **deploy** in the left panel to watch it work.

It takes roughly **3 to 6 minutes** — most of that is rebuilding the software on the server.

---

## Part 6 — What success looks like

Every step gets a **green tick**, and the last one — *"Assert /health lists the expected
optimizer"* — prints something close to:

```
remote /health: {"ok":true,"service":"ottoq-intelligence","optimizers":["energy_mpc","cp_sat_forward_lex"]}
verified: /health lists cp_sat_forward_lex
```

**The words `cp_sat_forward_lex` appearing in that list is the whole point.** Before this it read
only `["energy_mpc"]`.

**Then tell me it's green** and I'll test the routing end to end from the twin side — that's the
part where the AI orchestrator hands a real problem to that server and gets a real answer back,
which has never worked yet.

---

## Part 7 — If it goes red, what it means

The workflow is deliberately talkative about failure. Find the step with the red ✗, click it, and
look for the line starting `Error:` or `##[error]`. Match it below:

| What it says | What it means | What to do |
|---|---|---|
| `deploy is not configured. Missing repository secret(s): ...` | A secret name is misspelled or wasn't saved | Redo Part 4 for the one it names. Check capitals. |
| `ssh: handshake failed` / `unable to authenticate` | The key text is wrong, truncated, or lost its line breaks | Redo Part 2 and Part 4's Secret 3. Most often the `-----BEGIN`/`-----END` lines were left out. |
| `dial tcp ...: i/o timeout` or it hangs then fails at the SSH step | GitHub can't reach the server | Part 3 didn't take effect, or the IP is wrong. Re-check the **Public IPv4 address** — **it changes if the server was ever stopped and started.** |
| `could not read OTTOQ_API_TOKEN from the running container` | The service isn't currently running on the server | **Stop and tell me.** This one needs investigating, not retrying — it means something already went wrong on that box before today. |
| `Authentication failed for 'https://github.com/...'` at the `git fetch` step | The server's saved GitHub login has expired | **Tell me.** It needs a fresh access token put on the server, which is its own short procedure. |
| `/health does not list 'cp_sat_forward_lex'` | It rebuilt but the new code still isn't serving | **Tell me** — this would be a real puzzle and I'd want the log. |

**Nothing here can break the running service.** The workflow builds the new version under a
temporary name first, so if the build fails the old version keeps serving exactly as it is now.
Re-running a failed deploy is always safe.

---

## Part 8 — After it works

**You never do this again.** From then on, any code change to the service that reaches `main`
redeploys it automatically, and the deploy **refuses to report success** unless the server's
health check actually lists the expected capability. A check that only proved "the server is up"
would have passed on the stale version — which is exactly how this hid for five days.

**Two follow-ups I can do once you confirm it's green:**

1. **Test the routing end to end** — the thing this was all for.
2. **Replace SSH with AWS SSM** so port 22 can be closed again and the key file stops mattering.
   Your call whether it's worth the time.
