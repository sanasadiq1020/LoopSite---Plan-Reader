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

The API is a container. Two places to put it are described here; **Hugging Face
Spaces is the one to choose** unless you already have a paid Render service
running well.

| | Hugging Face Spaces (free) | Render (Starter, paid) |
|---|---|---|
| Memory | **16 GB** | 512 MB |
| Processors | 2 | 0.5 |
| Cost | free | monthly |
| Sleeps when unused | after 48 hours, wakes on the next visit (~1 min) | no |
| Files kept between restarts | no | no |

Memory is the whole story. Reading a plan holds a sheet's line work, its text
and its picture at once, and character recognition adds about 184 MB more while
it runs. On 512 MB that is close to the limit with one plan and past it with
two — which is what "memory limit exceeded" means, and when it happens the
service is killed and **every** plan on it is lost, including those belonging to
people who were only reading results. On 16 GB it is not close.

---

### 2a. Hugging Face Spaces — recommended

1. Create an account at **huggingface.co**.

2. **New → Space** (huggingface.co/new-space), and fill in:

   | | |
   |---|---|
   | **Space name** | `loopsite-plan-reader-api` |
   | **License** | your choice |
   | **Space SDK** | **Docker → Blank** |
   | **Hardware** | **CPU basic — free** (2 vCPU, 16 GB) |
   | **Visibility** | **Public** |

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

5. The Space starts building on its own. Open it and watch the **Building**
   log. The first build takes fifteen to twenty-five minutes — it is compiling
   a large set of packages. Later builds are much faster.

6. When the badge says **Running**, your API address is:

   ```
   https://YOUR-NAME-loopsite-plan-reader-api.hf.space
   ```

   All lower case, with the dash between your name and the Space name. Check it
   is alive by opening `<that address>/api/plan/health` — it should answer with
   `{"status":"ok", ...}`.

7. **Settings → Variables and secrets**, and add these as **Variables**
   (not secrets — none of them is one):

   | Name | Value |
   |---|---|
   | `ALLOWED_ORIGINS` | leave empty for now — filled in at step 4 |
   | `COOKIE_CROSS_SITE` | `true` |
   | `MAX_CONCURRENT_READINGS` | `2` |
   | `MAX_WAITING_READINGS` | `8` |

   The last two are how many plans are read at once and how many may queue
   behind them. Two at a time is comfortable in 16 GB; anyone arriving after
   that waits their turn and is told so, rather than everyone running out of
   memory together.

**Keep that address.** The interface needs it next.

**What to expect from a free Space**

- It **sleeps after 48 hours** with no visitors. The next person to open it
  waits about a minute while it wakes. Nothing is lost by sleeping except the
  uploaded plans, which are not kept anyway.
- Its **disk is wiped on every restart and rebuild.** A plan being read at that
  moment is lost and has to be uploaded again. This is by design — a plan lives
  only as long as somebody is reading it.

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
   | `MAX_CONCURRENT_READINGS` | `1` |
   | `MAX_WAITING_READINGS` | `4` |

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
which is most of them. Sheets stored as images are still read for their line
work; only their *text* is not recovered, and each such sheet says so rather
than appearing empty.

On Render, under **Environment**, add:

| Name | Value |
|---|---|
| `BUILD_WITH_OCR` | `false` |

Render passes it to the Docker build. The image drops by roughly 400 MB and
the free instance becomes viable.

---

## Keeping it up to date

Both platforms watch the repository. Push to `main` and both rebuild on their
own — Vercel in a minute or two, Render in rather longer.

To change what the product says about itself — its version, what it does, its
known limitations — edit `config/version.json` and push. The interface reads
that file live under *About this tool*; nothing is written into a screen.

---

## Costs

| | |
|---|---|
| Vercel | Free. The interface is a static build well inside the free allowance. |
| Render, with character recognition | Starter, around **$7 per month** |
| Render, without it | Free, with the sleep behaviour described above |

---

## A note on privacy

Every browser gets its own anonymous session, and one session can never read
another's plan. But an uploaded plan does sit on the server's disk while it is
being looked at, and Render's logs record that an upload happened.

Before putting a client's drawings through a deployed instance, make sure that
is acceptable to them. For sensitive work, run it locally instead — it needs no
internet connection at all.
