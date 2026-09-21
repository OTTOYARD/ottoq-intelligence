# Giving Claude AWS access — the two-value version

**Who this is for:** Chase, or anyone non-technical. Written 2026-09-20.

**Time:** about 8 minutes. **You do this once, ever.**

**This REPLACES `ACTIVATE_DEPLOY.md`.** Don't do both. This one is shorter, and it deletes the
part you were dreading: **there is no `.pem` key file to hunt for, no security group to edit, and
no port left open to the internet.**

---

## What you're actually doing

You're creating a limited AWS login for automation, and putting its two values into GitHub. After
that, GitHub can drive your AWS account on my behalf — starting and stopping servers, rebuilding
the service, installing software — and **you never touch it again.**

**One thing I cannot do, so you have to:** I am blocked from writing GitHub secrets. I tested it —
the request comes back `403 Access to this GitHub Actions path is not permitted through this
proxy`. So Part 3 below is genuinely yours. Everything after Part 3 is mine.

---

## Part 1 — Create the automation login (AWS Console)

1. Go to **https://console.aws.amazon.com/** and sign in.
2. In the search box at the very top, type **`IAM`** and click the result labelled **IAM**
   (it says *"Manage access to AWS resources"* underneath).
3. On the left menu, click **Users**.
4. Click the orange **Create user** button (top right).
5. **User name:** type exactly `ottoq-automation`
6. **Do not** tick *"Provide user access to the AWS Management Console"* — this login is for
   software, not for a person.
7. Click **Next**.
8. You're on *"Set permissions"*. Click the box labelled **Attach policies directly**.
9. A long searchable list appears. You need to find and tick **three** policies. For each one:
   type its name in the search box, then tick the checkbox on its row.
   - `AmazonEC2FullAccess`
   - `AmazonSSMFullAccess`
   - `IAMFullAccess`
10. **Check you have exactly three ticked** — there's a counter near the top saying
    *"3 policies selected"* or similar. Then click **Next**.
11. Click **Create user**.

### About that third policy — read this, don't just click it

**`IAMFullAccess` is powerful. It can grant itself anything else.** I'd rather you know that now
than discover it later.

It's needed for exactly one job: attaching a role to your server so Amazon's remote-command
service can reach it. **That job takes about a minute and happens once.**

**So: attach it now, let me do that step, and then delete it.** Part 6 has the three clicks. After
that this login can only touch EC2 and SSM, which is the right long-run footprint. I'll remind you
when I'm done with it — and if I forget, hold me to it.

---

## Part 2 — Create the two values

1. You should now be looking at the user list, with `ottoq-automation` in it. **Click its name.**
2. Click the **Security credentials** tab.
3. Scroll down to the **Access keys** section and click **Create access key**.
4. AWS asks *"Which use case?"*. Choose **Other** (it's the last option). Click **Next**.
5. **Description tag:** leave blank or type `github actions`. Click **Create access key**.
6. **This page shows the secret once and never again.** You'll see two values:
   - **Access key** — starts with `AKIA`, about 20 characters
   - **Secret access key** — about 40 characters, hidden until you click **Show**
7. Click **Download .csv file**. Put it somewhere you can find it for the next five minutes.
   (You can delete it once Part 3 is done — GitHub will hold the only copy that matters.)
8. **Leave this browser tab open** until Part 3 is finished.

---

## Part 3 — Put them into GitHub (the only part that's yours)

1. Go to **https://github.com/OTTOYARD/ottoq-intelligence**
2. Click **Settings** (in the top row of tabs). If you don't see it, you're signed in as an
   account without admin rights on the repo.
3. Left menu → **Secrets and variables** → click it, then click **Actions** underneath.
4. Click the green **New repository secret** button. You'll use it twice.

**Secret 1 of 2**
- **Name:** `AWS_ACCESS_KEY_ID`
- **Secret:** the **Access key** from Part 2 (the one starting `AKIA`)
- Click **Add secret**

**Secret 2 of 2**
- **Name:** `AWS_SECRET_ACCESS_KEY`
- **Secret:** the **Secret access key** from Part 2 (the ~40-character one)
- Click **Add secret**

**Names must match exactly** — all capitals, underscores not spaces or dashes.

**Watch for these two mistakes**, which are the usual ones:
- ❌ pasting the two values into the wrong boxes (the long one is the *secret*)
- ❌ a trailing space picked up from the CSV — select carefully, or use the CSV's copy button

When you're done the page lists two secrets. **You cannot see their values again** — that's the
point. If one's wrong, click **Update** on it and paste fresh.

---

## Part 4 — Tell me, and I take over

Message me: **"AWS keys are in."**

**You do not need to tell me the region** — I'll find the server myself by looking across regions.
You don't need to tell me the instance ID, the IP, or anything else.

---

## Part 5 — What I do from there, without you

In roughly this order:

1. **Verify the access works** and find your `ottoq-intelligence` server across all regions.
2. **Attach the SSM role** to it, so Amazon's remote-command service can reach it without SSH.
   *(This is the one step that needs `IAMFullAccess`.)*
3. **Rebuild and restart the service** on the box so it finally serves CP-SAT — the thing this is
   all for. Verified by its own health check listing `cp_sat_forward_lex`, not merely returning OK.
4. **Rewrite the deploy workflow** to use SSM instead of SSH, so future deploys need no key and no
   open port.
5. **Close port 22** on the server, so it stops accepting SSH from the internet entirely.
6. **Test the full chain end to end** — the AI orchestrator handing a real problem to that server
   and getting a real answer back. That has never worked yet, and it's the point of the exercise.
7. **Report what I found and what it cost.**

After that I can start and stop that server, deploy to it, and install things on it, on my own.

---

## Part 6 — Afterwards: remove the powerful permission

**Do this once I tell you step 2 above is done.** Three clicks:

1. AWS Console → search **IAM** → **Users** → click **`ottoq-automation`**
2. On the **Permissions** tab, find the row **`IAMFullAccess`** and tick its checkbox
3. Click **Remove** (then confirm)

`AmazonEC2FullAccess` and `AmazonSSMFullAccess` stay. That's the permanent footprint.

**And a better end state I can build later:** GitHub supports proving its identity to AWS
directly, with **no stored keys at all**. Once the above is working I can set that up myself
(it needs the EC2/IAM access you've just granted), and then you delete this login entirely. Worth
doing eventually; not worth blocking on now.

---

## Part 7 — How I'll treat your money

You have **$10,000 of AWS Activate credits activated, roughly $100 used.** I'm about to be able
to spend that. Rules I'm holding myself to, so you don't have to watch me:

- **I will never launch a GPU instance without asking you first, with its hourly cost stated.**
  GPU boxes run from about $1/hour to over $30/hour. That's the category that can quietly eat
  credits.
- **I will not launch anything new at all without telling you the hourly cost first.** The current
  server is a `t3.medium`, roughly $0.04/hour — about $30/month. Nothing I plan changes that.
- **Anything I start for a test, I stop when the test ends.** A stopped instance costs almost
  nothing; a forgotten running one is how credits disappear.
- **I will not touch resources I didn't create**, other than the known `ottoq-intelligence`
  server, and I'll say so before I do anything to that one.
- **I'll report spend when I report results**, so you see it alongside the work rather than on a
  bill.

If I ever ask to spend more than a few dollars an hour, the right answer is to make me justify it.

---

## If something goes wrong in Parts 1–3

| What you see | What it means | What to do |
|---|---|---|
| No **Settings** tab on the GitHub repo | Signed in as an account without admin rights | Sign in as the account that owns `OTTOYARD` |
| Can't find a policy in the Part 1 list | Search is picky about spelling | Copy the name from this page and paste it into the search box |
| Closed the Part 2 page before copying the secret | AWS will not show it again | No harm: click **Create access key** again for a new pair, and delete the old one |
| Anything else | — | Stop and tell me what you see. Don't work around it. |
