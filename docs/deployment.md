# Deploying it

The interface and the API are deployed to two different places, because they
are two different kinds of thing.

```
GitHub repository
   ├── frontend/   →  Vercel                  the public link people open
   └── backend/    →  Hugging Face Spaces     the API behind it
                      (or Render)
```

**The API cannot go on Vercel.** Vercel runs serverless functions with a
250 MB limit, a read-only filesystem and a short execution limit. The reader is
over a gigabyte installed, writes each run's files to disk and reads them back,
and takes twenty to forty seconds on a large plan set. It needs a container.

---

## Before you start

You will need:

- A **GitHub** account
- A **Vercel** account (free is enough — the interface is a static build)
- A **Hugging Face** account (free), or a **Render** account with the paid
  Starter plan. Which, and why, is in step 2.

---

## 1. Put the code on GitHub

Create an **empty private repository** on GitHub — no README, no `.gitignore`,
nothing. Then, from the project folder:

```
git remote add origin https://github.com/YOUR-NAME/YOUR-REPO.git
git branch -M main
git push -u origin main
```

Git will ask you to sign in the first time; a browser window opens and you
approve it there.

**What is not committed**, and why:

| | |
|---|---|
| `input/*.pdf` | Real drawings carry a client's address, the office's name and its licence numbers. The application does not need them — a reader uploads their own. |
| `output/`, `logs/` | Produced from an upload and discarded with it |
| `backend/venv/`, `frontend/node_modules/` | Rebuilt from `requirements.txt` and `package.json` |
| `.env.local` | Points at a developer's own machine |

---

## 2. Deploy the API

### How much machine this actually needs — measured, not guessed

Every number below was taken by watching the process while it read the plan
sets in `input/`, largest first:

| | Memory |
|---|---|
| Idle, with everything loaded | **45 MB** |
| Reading the heaviest plan set (17 sheets, drawn as pictures) | **345 MB at its peak** |
| The same set read four times over | **still 345 MB** — it does not creep up |
| Character recognition models, while a scanned sheet is being read | **+184 MB** |
| Two plans read at the same moment | **roughly double** |

Two things follow, and they decide everything else on this page:

*   **Without character recognition, one plan at a time fits in 512 MB.**
    345 MB of working set leaves real headroom.
*   **With it, 512 MB is not enough.** 345 + 184 is 529 MB before the operating
    system has taken anything, which is why a 512 MB instance was killed. It
    needs about **1 GB**.

Character recognition is only ever used on a sheet that carries **no text of
its own** — a scan, or a drawing exported as an image. Every one of the plan
sets in use is read completely without it. It is now a **setting**
(`OCR_ENABLED=false`) rather than something baked into the image, so a small
host can turn it off and a larger one can turn it on, without rebuilding
anything. A sheet that needed it then says so on its own row instead of coming
back blank.

### Which host

| | Memory | Runs | Character recognition |
|---|---|---|---|
| A free Space with a CPU tier | 16 GB | Python (`space_app.py`) | yes, comfortably |
| Render **free** | 512 MB | `Dockerfile` | **no** — set `OCR_ENABLED=false` |
| Render **Starter** | 512 MB | `Dockerfile` | **no** — the same 512 MB; it buys processor, not memory |
| Anything with ~1 GB | 1 GB | either | yes |

**How many are read at once is not something you have to set.** The server
works it out from the memory it actually has — 350 MB per reading, 512 MB left
for everything else — because one at a time is right for a small container and
wrong for a large one, and which of the two it is only becomes knowable once it
is running:

| The machine has | It reads |
|---|---|
| 512 MB | 1 at a time |
| 1 GB | 1 at a time |
| 2 GB or more | 4 at a time |

Everyone else queues and is told where they are. `MAX_CONCURRENT_READINGS`
overrides it where that is wanted.

---

### 2a. Hugging Face Spaces — recommended

**Choose the Gradio SDK, not Static and not Docker.**

| What the platform offers | Can it run this API? |
|---|---|
| **Static** | **No.** A Static Space serves files — HTML, CSS, JavaScript — from a CDN and runs no code at all. This API is a Python program that reads PDFs and writes files; there is nothing for a Static Space to run. |
| **Gradio** | **Yes.** A Gradio Space runs a Python file, and `space_app.py` in this repository is that file: it starts the same API, on the port a Space listens on. |
| **Docker** | Yes, and it is what `Dockerfile` is for — but only where the platform offers it. |

`space_app.py` is not a second copy of anything. It puts `backend/` on the
import path and starts `app.main:app`, which is what every other way of running
this application starts. It also mounts a one-paragraph page at `/` saying what
the address is, because a Space shows whatever its application serves there and
an API on its own serves nothing.

**Hardware.** This service never uses a GPU — it reads PDFs, measures line
work and writes files, all of which are processor and memory work. What it
needs is memory: 345 MB to read a plan, and 184 MB more while a scanned sheet
is being read.

*   **A free CPU tier is the right one.** Take it if it is offered.
*   **If the only free tier on offer is a GPU one, it is still worth trying.**
    A GPU tier runs an ordinary Python process on ordinary processors and
    attaches a GPU only to code that asks for one; nothing here ever asks, so
    the GPU sits unused and the service runs on the part that is not the GPU.
    It costs twenty minutes to find out.
*   **If it does not run there, the fallback is a small container host with
    `OCR_ENABLED=false`** — see the table above. 345 MB fits in 512 MB, which
    means a free container tier is enough for everything except scanned
    sheets, and those say so on screen rather than coming back empty.

---

1. Create an account at **huggingface.co**.

2. **New → Space** (huggingface.co/new-space), and fill in:

   | | |
   |---|---|
   | **Space name** | `loopsite-plan-reader-api` |
   | **License** | your choice |
   | **Space SDK** | **Gradio** |
   | **Template** | **Blank** |
   | **Hardware** | the free CPU option |
   | **Visibility** | **Public** |

   > **Blank, not one of the example templates.** A template writes its own
   > `app.py` and `requirements.txt` into the Space, and the first push from
   > this repository would have to overwrite both.

   > **It has to be Public.** A private Space needs an access token on every
   > request, and the interface calls this API straight from the visitor's
   > browser, where there is nowhere safe to keep one. Public exposes the API,
   > not anyone's plan: every plan route still checks the session, so one
   > visitor can never read another's upload.

3. **Make an access token.** Your account **Settings → Access Tokens → Create
   new token**, type **Write**. Copy it — a password will not work for pushing.

4. **Push the code to the Space.** From the project folder:

   ```
   git remote add space https://huggingface.co/spaces/YOUR-NAME/loopsite-plan-reader-api
   git push space main
   ```

   When it asks: **username** is your Hugging Face username, **password** is
   the token from step 3.

   > This is a second remote, not a move. `git push origin main` still goes to
   > GitHub; `git push space main` sends the same code to the Space. Push to
   > both whenever you change something.

   These four files at the top of the repository are what the Space reads:

   | | |
   |---|---|
   | `README.md` | the block at the very top names the SDK and the file to run |
   | `space_app.py` | starts the API |
   | `requirements.txt` | every package it needs — a Space mounts this file on its own, so it cannot point at another |
   | `packages.txt` | one system library the image may not already carry |

5. The Space starts building on its own. Open it and watch the **Building**
   log. The first build takes ten to twenty-five minutes — character
   recognition is a large set of packages. Later builds are much faster.

6. When the badge says **Running**, your API address is:

   ```
   https://YOUR-NAME-loopsite-plan-reader-api.hf.space
   ```

   All lower case, with a dash between your name and the Space name. Check it
   is alive by opening `<that address>/api/plan/health` — it should answer with
   `{"status":"ok", ...}`. Opening the address on its own shows a short page
   saying what the service is; that page is not the interface.

7. **Settings → Variables and secrets**, and add these as **Variables**
   (not secrets — none of them is one):

   | Name | Value |
   |---|---|
   | `ALLOWED_ORIGINS` | the Vercel address — filled in at step 4 |
   | `COOKIE_CROSS_SITE` | `true` |

   **That is all that has to be set.** How many plans are read at once is
   worked out from the memory the machine actually has — 350 MB is set aside
   for each reading and 512 MB for everything else, so a 16 GB Space reads
   four at a time while a 512 MB container reads one. Anyone arriving after
   that waits their turn and is told where they are in the queue, rather than
   everyone running out of memory together. The first line of the log says
   which figure it chose.

   Two settings exist for overriding that, and neither is needed to start:

   | Name | When |
   |---|---|
   | `MAX_CONCURRENT_READINGS` | to fix the number yourself |
   | `OCR_ENABLED` | set to `false` only if a scanned sheet exhausts the memory |

**Keep that address.** The interface needs it next.

**What to expect from a free Space**

- It **sleeps after a period with no visitors**. The next person to open it
  waits about a minute while it wakes. Nothing is lost by sleeping except the
  uploaded plans, which are not kept anyway.
- Its **disk is wiped on every restart and rebuild.** A plan being read at that
  moment is lost and has to be uploaded again. This is by design — a plan lives
  only as long as somebody is reading it.

**If the build fails**

| What the log says | What to do |
|---|---|
| a package will not install, naming `paddlepaddle` or `paddleocr` | Remove those two lines from `requirements.txt` and push again. Everything works except scanned sheets with no text of their own, which then say so on screen rather than coming back blank. |
| `libGL.so.1` or a similar library is missing | Add its name on its own line in `packages.txt` and push again. |
| it stops without a message | Almost always the build timing out on the large packages. The first row applies. |

---

### 2b. Render — the alternative

1. **New → Web Service**, and connect the repository.
2. Render finds `render.yaml` and fills most of it in. Confirm:
   - **Runtime**: Docker
   - **Dockerfile path**: `./Dockerfile`
   - **Docker build context**: `.` — the repository root
   - **Instance type**: **Starter**. The free instance does not have the memory
     for character recognition — see *Leaving character recognition out*.
3. Under **Environment**, add:

   | Name | Value |
   |---|---|
   | `ALLOWED_ORIGINS` | leave empty for now — filled in at step 4 |
   | `COOKIE_CROSS_SITE` | `true` |
   | `OCR_ENABLED` | `false`, on a 512 MB instance |

   512 MB holds one reading (345 MB) but not one reading plus the recognition
   models (529 MB). How many are read at once needs no setting: on this
   instance the server works out one, on its own.

4. Deploy, and check `<your address>/api/plan/health` when it finishes.

---

## 3. Deploy the interface on Vercel

1. **Add New → Project**, and import the same repository.
2. Set **Root Directory** to `frontend`. This matters — without it Vercel
   tries to build the whole repository.
3. Vercel detects Next.js on its own. Leave the build settings alone.
4. Under **Environment Variables**, add:

   | Name | Value |
   |---|---|
   | `NEXT_PUBLIC_API_BASE_URL` | the API address from step 2, with no trailing slash |

5. Deploy. This takes a minute or two.

Vercel gives you the public link — something like
`https://your-project.vercel.app`. That is the address you share.

> **`NEXT_PUBLIC_*` values are compiled in when the site is built, not read
> when it runs.** Set one *after* a deployment and nothing changes — the site
> that is already live still carries the old value inside it. Whenever you add
> or change one, **build the site again**: *Deployments → ⋯ → Redeploy*, with
> **Use existing Build Cache turned off**.
>
> If this is wrong, the interface loads perfectly and every upload fails,
> because the page is reaching for `http://localhost:8000` — a server on the
> visitor's own machine. The interface now says so on screen rather than
> letting it look like a broken plan.

---

## 4. Let the two talk to each other

Go back to wherever the API is — the Space's **Settings → Variables**, or
Render's **Environment** — and set `ALLOWED_ORIGINS` to the Vercel address:

```
https://your-project.vercel.app
```

No trailing slash. If you also want Vercel's preview deployments to work, list
them separated by commas.

The service restarts. **This step is not optional**: until the API knows which
address is allowed to call it, the browser refuses every request and the
interface sits there loading forever.

---

## 5. Check it works

Open the Vercel link and upload a plan PDF. You should see:

- the progress bar counting the sheets,
- the results appearing,
- a marked-up sheet when you open one,
- the 3D model building when you open that tab.

If the upload starts but nothing ever appears, it is almost always one of two
things — see below.

---

## When it does not work

**A red panel says the site does not know where its API is.**

`NEXT_PUBLIC_API_BASE_URL` was missing when the site was *built*. Set it on
Vercel and redeploy **without the build cache** — setting it alone changes
nothing, because the value is compiled in at build time.

**The interface loads but every upload hangs, or the results never arrive.**

The API is refusing the browser. Check `ALLOWED_ORIGINS` on Render is exactly
the Vercel address, with `https://` and no trailing slash. Open the browser's
developer console: a CORS message names the origin the API saw, which is
usually the answer.

**It works in your browser but not in anyone else's.**

This was the shape of a real failure worth knowing about. The session used to
travel only in a cookie set by the API — a *third-party* cookie to the page the
reader is looking at, which browsers now block by default. It kept working for
whoever deployed it, because their own browser had visited the API's address
directly at some point and so kept the cookie, and failed for everyone they
shared the link with.

The browser now holds its session itself and presents it on every request, so
this no longer depends on a cookie being accepted. If you see it, the site is
running an older build: redeploy on Vercel **without the build cache**.

`COOKIE_CROSS_SITE=true` on Render is still worth setting — the cookie is still
offered, and it is one less thing to be wrong.

**A large plan set fails while a small one works.**

The reading now happens on the server after the upload is acknowledged, and
the browser follows its progress — so a plan that takes minutes no longer runs
into the hosting platform's request limit. If you still see this, the site is
running an older build: redeploy on Vercel **without the build cache**, and let
Render finish rebuilding.

If the progress bar stops moving part way and then reports that the server
stopped responding, the instance ran out of memory. Move to a larger one, or
build without character recognition.

**The first upload after a quiet period takes about a minute.**

On Render's free instance the service sleeps after fifteen minutes of no
traffic and takes around thirty seconds to wake. The Starter plan does not
sleep.

**The build runs out of memory, or the service restarts under load.**

Character recognition is the largest thing in the image and the heaviest at
runtime. Either move to a larger instance, or leave it out.

---

## Leaving character recognition out

A smaller, cheaper deployment that reads drawings which carry their own text —
which is most of them, and all of the plan sets in use here. Sheets stored as
images are still read for their line work; only their *text* is not recovered,
and each such sheet says so on its own row rather than appearing empty.

**Two ways, and they are for different situations.**

**To turn it off on a running service** — the one to reach for first, because
it takes effect on the next restart and can be undone just as quickly:

| Name | Value |
|---|---|
| `OCR_ENABLED` | `false` |

Nothing is rebuilt. The models are simply never loaded, so the 184 MB they
occupy is never taken and a 512 MB machine has room to read a plan.

**To leave it out of the image altogether** — a smaller image and a faster
build, at the cost of a rebuild to change your mind:

| Where | Name | Value |
|---|---|---|
| a Docker host, under Environment | `BUILD_WITH_OCR` | `false` |
| a Space | delete the `paddlepaddle` and `paddleocr` lines from `requirements.txt` | |

The image drops by roughly 400 MB.

---

## Keeping it up to date

Vercel watches the repository and rebuilds on its own, in a minute or two.

A Space is a git repository of its own, so it does not see a push to GitHub.
Send it the same commit:

```
git push origin main    # GitHub
git push space main     # the Space, which then rebuilds
```

To change what the product says about itself — its version, what it does, its
known limitations — edit `config/version.json` and push. The interface reads
that file live under *About this tool*; nothing is written into a screen.

---

## Costs

| | |
|---|---|
| Vercel | Free. The interface is a static build well inside the free allowance. |
| Hugging Face Space, free CPU tier | Free, with the sleeping described above |
| Render, with character recognition | Starter, a monthly charge |
| Render, without it | Free, with the sleep behaviour described above |

---

## A note on privacy

Every browser gets its own anonymous session, and one session can never read
another's plan. But an uploaded plan does sit on the server's disk while it is
being looked at, and Render's logs record that an upload happened.

Before putting a client's drawings through a deployed instance, make sure that
is acceptable to them. For sensitive work, run it locally instead — it needs no
internet connection at all.
