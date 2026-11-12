# Roadmap

Everything Kosh could become, written down so it can be picked up cold. Nothing
here is a promise; the **State** column is the truth, and an item is only `done`
when its exit test passes in CI and the evidence is in `docs/FINDINGS.md`.

## How to use this

1. Pick the lowest-numbered milestone with work left. Do not start a later one to
   avoid a boring one.
2. An item is finished when: the exit test passes, `pytest` is green, the CLI and
   web UI both expose it, and a line lands in `CHANGELOG.md`. If it produced a
   number, that number goes in `docs/FINDINGS.md`.
3. If an item cannot be done, mark it `blocked` with the reason in place. A silent
   gap becomes a re-litigated decision six months later.
4. Anything that would put an entropy coder, a CHCE bitstream decoder or a GPU
   path inside this repository is rejected by rule, not by preference — see
   §9.

## Milestones

| Milestone | Theme | Why now | State |
| --- | --- | --- | --- |
| **1.2** | Trust it everywhere | It works on one Linux box; that is not a release | open |
| **1.3** | Reach the engine | The family exists and Kosh barely touches it | open |
| **1.4** | More formats | The optimiser covers four; users own ten | open |
| **1.5** | The UI becomes a tool | Today it is a demo of the CLI | open |
| **1.6** | Speed and scale | Zopfli is the bottleneck; large inputs are untested | open |
| **2.0** | The workbench as a hub | Recipes, watch folders, engine plugins | idea |
| **Backlog** | Unscheduled | Worth keeping, not worth ordering yet | — |

---

## 1.2 — Trust it everywhere

The point of this milestone is that someone else can install it and get the same
result. No new formats.

| ID | Item | Why | Exit test | Size |
| --- | --- | --- | --- | --- |
| CT-1 | **Windows CI runner** (`windows-latest` in the matrix) | `atomic_write` + `os.replace` on a file held open by another process is unproven; paths and modes differ | CI green on Windows for the full suite; `docs/FINDINGS.md` records what changed | M |
| CT-2 | **macOS runner** (`macos-latest`) | Same class of risk, cheaper to add now than to debug later | CI green on macOS | S |
| CT-3 | **Property-based round-trips** (Hypothesis) for split/join, pack/unpack and the PNG filter matrix | Hand-written tests cover what I thought of | 10⁴ generated cases, zero failures | M |
| CT-4 | **Corruption fuzzing** | `verify` must fail closed on *any* mutation, not just the ones I chose | 10⁵ random mutations: no crash, no hang, no false "intact" | M |
| CT-5 | **`verify --jobs N`** | Deep verify is CPU-bound and single-threaded | 4× throughput on a multi-entry container, measured | S |
| CT-6 | **ZIP64 in `verify`** | It is passed through today; verify should still read it | `verify` reports intact/entry-count for a >4 GB or >65 535-entry zip | S |
| CT-7 | **Durability: fsync the directory** after `os.replace` | The rename is atomic, the directory entry is not | A crash test (kill -9 mid-write) never loses the original | S |
| CT-8 | **Resource guard on extract** — `--max-size`, free-space check | A zip bomb is a legitimate archive | Refuses to write past the budget, with a clear message | S |
| CT-9 | **Publish to PyPI** (`kosh` if free, else `kosh-toolkit`) | `pip install -e .` is not an install | `pip install kosh` in a clean container, then `kosh doctor` passes | S |
| CT-10 | **Single-source version check in CI** | The fleet's rule; a drifting version string is a lie in the footer | CI fails if `pyproject.toml`, `__init__.py` and the badge disagree | S |
| CT-11 | **`scripts/check-claims.py`** — assert every README table matches `docs/FINDINGS.md` | ziptastic already has this gate; Kosh's README quotes numbers too | CI fails when a table cell is edited without the measurement | M |
| CT-12 | **Enable Pages, register in the fleet** | The landing page exists and is not live | `drsamonline.github.io/kosh/` returns 200; dashboard picks the repo up | S |

---

## 1.3 — Reach the engine

Kosh currently spawns nothing real. This milestone makes the family boundary
work end to end, and is where the two repositories have to agree on facts.

| ID | Item | Why | Exit test | Size |
| --- | --- | --- | --- | --- |
| EN-1 | **Real ziptastic integration** — a CI job that builds ziptastic and runs `kosh pack --engine ziptastic` | Today the delegation is proven against a shell stand-in | Round-trip through a real `.dr` file in CI | L |
| EN-2 | **`kosh unpack --engine ziptastic`** | Packing without unpacking is half a boundary | `pack --engine ziptastic` then `unpack --engine ziptastic` returns the original bytes | M |
| EN-3 | **`kosh scan` estimates the engine's gain** from a sample (entropy + container type) | "Would ziptastic help?" is the question a user actually has | Prediction within ±10% of the real result on the bench corpus | M |
| EN-4 | **Full `.dr` header read** (block table), coordinated with ziptastic's `docs/FORMAT.md` | `info` on an engine stream is the natural companion to `info` on a container | `kosh info file.dr` prints blocks and offsets, matching `ziptastic info` | M |
| EN-5 | **`kosh run recipe.yaml`** — a local pipeline: optimize → pack → engine → verify, with a manifest of steps | heavenzip chains binaries globally; nobody can chain a *local* job | A recipe file reproduces a multi-step run and records each step's evidence | L |
| EN-6 | **Engine registry** — `kosh engines list`, version probe, capability report | Two engines exist; a third will | `kosh doctor` shows versions of installed engines, not just presence | S |
| EN-7 | **Write the boundary down**: `docs/ENGINE_BOUNDARY.md` | "Why doesn't Kosh just compress better?" is the most likely question | Document states what is engine work and what is workbench work, with examples | S |

---

## 1.4 — More formats

Each item is lossless, container-level, and must state what it refuses.

| ID | Item | Why | Exit test | Size |
| --- | --- | --- | --- | --- |
| OP-1 | **PDF stream re-compression** — inflate Flate streams, re-deflate with Zopfli, rebuild xref | The single most common "why is this file so big" file | 200 PDFs round-trip; page count, text and xref intact; savings measured | L |
| OP-2 | **WAV / AIFF** — re-compress PCM inside, keep headers | Audio containers are full of uncompressed PCM | Byte-exact audio after optimisation | M |
| OP-3 | **SQLite page re-compression** (vacuum-free) | Databases ship deflated text | Query results identical before/after | M |
| OP-4 | **PNG: interlaced (Adam7)** | Currently refused; a whole class of images is untouched | Interlaced file optimises and stays pixel-identical | M |
| OP-5 | **PNG: lossless palette + bit-depth reduction** (only when provably lossless) | 16-bit images holding 8-bit data are common | Pixels identical; colour type change recorded in the report | M |
| OP-6 | **APNG** — optimise frames, keep animation and timing | Currently skipped wholesale | Frames and delays preserved, size reduced | L |
| OP-7 | **Multi-member GZIP** (concatenated streams, e.g. `access.log.1.gz`) | Today only the first member is understood | Concatenated members optimise and decompress identically | S |
| OP-8 | **WebP / AVIF metadata strip** | The same EXIF problem as JPEG | Pixels identical, metadata gone, size reduced | S |
| OP-9 | **Optimise cache** — skip files already processed (keyed by content hash) | Re-running a folder should be instant | Second run over 1 000 files takes <5% of the first | M |
| OP-10 | **`--strip` granularity** — `--strip=exif,text,comments` | "Strip" is all-or-nothing and sometimes too much | Each class independently selectable | S |
| OP-11 | **Decide the Layer-0 overlap** with ziptastic (PDF, WAV, SQLite appear in both plans) | Duplicating a transform across two repos is how they diverge | A written decision in `docs/ENGINE_BOUNDARY.md`, one owner per format | S |

---

## 1.5 — The UI becomes a tool

The current page is a thin front over one job. It should be usable for an
afternoon of work.

| ID | Item | Why | Exit test | Size |
| --- | --- | --- | --- | --- |
| UI-1 | **Job history** that survives a reload (localStorage + server list) | Closing the tab loses the result | Reopen and the last 20 jobs are still there with downloads | M |
| UI-2 | **Queue** — several jobs, one worker, visible order | "Drop a folder, walk away" is the actual use | Three jobs queued, run in order, each reports | M |
| UI-3 | **Image before/after preview** with zoom and a slider | The only way to trust a lossless claim visually | Side-by-side of the same pixels, checksum shown | M |
| UI-4 | **Report export** — copy/download the run as Markdown | People paste results into issues and notes | One click yields the table from `docs/FINDINGS.md`'s format | S |
| UI-5 | **Accessibility pass** — focus order, labels, contrast, reduced motion | It is a public page; keyboard users exist | Axe/Lighthouse audit, no critical findings | S |
| UI-6 | **Optional token when bound off-loopback** | `--host 0.0.0.0` is currently a foot-gun with no warning | Binding to a public interface requires a printed token | S |
| UI-7 | **Serve `/about`** from `docs/index.html` | One artefact, two places, no drift | The landing page renders from the running server | S |
| UI-8 | **Cancel a running job** | Long Zopfli runs cannot currently be stopped from the page | Cancel stops the worker and reports partial state | M |

---

## 1.6 — Speed and scale

Zopfli is the bottleneck and large inputs are untested. Both need numbers before
they need code.

| ID | Item | Why | Exit test | Size |
| --- | --- | --- | --- | --- |
| PF-1 | **Profile the PNG path** and publish the breakdown (decode / filter / deflate / rebuild) | Optimisation without a profile is guessing | `docs/FINDINGS.md` §performance, with a flame graph in `docs/assets/` | M |
| PF-2 | **Optional accelerator for the filter loop** (numba or a small Cython module) | Paeth/Average decoding is the pure-Python remainder | ≥3× on a 12 MP image, with the pure-Python path kept as fallback | L |
| PF-3 | **Parallel Zopfli over independent deflate blocks** for large PNGs | Cores are idle during a 74-second single-threaded pass | Measured ratio cost vs speed gain, documented; opt-in flag | L |
| PF-4 | **Memory ceiling test** — 10 GB input at <512 MB RSS | "Streams" is a claim; it needs a test | Test asserts peak RSS in CI (or a documented manual run) | M |
| PF-5 | **`--algo blake3`** for hashing (optional dependency) | SHA-256 is 400 MB/s and it is on the critical path for verify | Same digests where it matters, 3× verify throughput | S |
| PF-6 | **Faster default for `optimize` on big trees** — sample the first N files and pick a level | Users pick "max" once and then wait an hour | A 500-file folder finishes in a documented time with the auto level | M |

---

## 2.0 — The workbench as a hub

Only after 1.2–1.6. These are the items that would make Kosh the thing people
keep open.

| ID | Item | Why | Exit test | Size |
| --- | --- | --- | --- | --- |
| HB-1 | **Watch folders** — `kosh watch ~/inbox --recipe pack.yaml` | The manual loop is the friction | A file dropped in is processed, verified and logged | L |
| HB-2 | **Recipe library** — named, versioned, shareable pipelines | Recipes are the unit of reuse | Three shipped recipes (`photo-archive`, `release-bundle`, `disk-backup`) run in CI | M |
| HB-3 | **Engine plugin protocol** — a JSON contract so a new engine needs no Kosh release | The family will grow | A toy engine is added without touching `kosh/core/` | L |
| HB-4 | **Scheduled verification** — re-verify an archive tree weekly, report drift | Bit rot is real and silent | A cron-style run reports which files changed since last check | M |
| HB-5 | **Content-addressed store** — `kosh store add` deduplicating across containers | The obvious next step once containers are trusted | Same file twice costs once, measured | L |

---

## Backlog (unscheduled)

Worth recording, not worth ordering.

- **BL-1** TUI (`kosh tui`) with a file browser and a savings column.
- **BL-2** `kosh diff a.kosh b.kosh` — which entries changed, by hash.
- **BL-3** Container append: add an entry without rewriting the whole file.
- **BL-4** Export a container as a self-extracting script (sh-only, no binary).
- **BL-5** Fuzz the *archive* readers (zip/tar) as a scheduled CI job.
- **BL-6** Optional `--threads` for Brotli via process pool (Brotli releases the GIL poorly).
- **BL-7** A `kosh serve` mode for a LAN share, with the token from UI-6.
- **BL-8** Windows shell integration (context-menu "Optimise with Kosh").
- **BL-9** Read `.hzpkg` manifests (list-only, like `.dr` header reading).
- **BL-10** A comparison page in `docs/` against 7-Zip, PeaZip, optipng and ECT.

---

## 9. Rejected by rule

Recorded so they are not re-proposed every six months. Each is a rule of the
family, not a preference of the author.

| Rejected | Reason |
| --- | --- |
| **A new entropy coder** (rANS, CM, PPMd) | That is `ziptastic`. Kosh picks between codecs; it does not write them. |
| **Decoding the CHCE bitstream** | One format, one decoder. Kosh recognises the magic and defers. |
| **Writing the `CHCE` magic for `.kosh`** | A decoder would try to read a container as a bitstream. Distinct magic, explicit detection. |
| **GPU paths** | Family rule. Blocks already parallelise on cores. |
| **Linking a GPL compressor** | Process boundary or nothing. This is what keeps heavenzip GPL-safe. |
| **Telemetry, accounts, cloud upload** | Offline, accountless, no network. Non-negotiable. |
| **Lossy re-encoding** (JPEG quality reduction, palette quantisation) | The product promise is byte-exactness. A lossy mode would make every claim conditional. |
| **Home-grown encryption for containers** | A password field invites a broken cipher. Use an external tool. |
| **A GUI framework** (Qt/Tk) | The web UI already is the GUI, and it is one file of HTML. |

---

## 10. Decisions only the owner can make

| # | Question | Blocks |
| --- | --- | --- |
| D-1 | Enable GitHub Pages on `kosh` (`main` / `/docs`)? | CT-12, UI-7 |
| D-2 | PyPI name: `kosh` or `kosh-toolkit`? | CT-9 |
| D-3 | Do PDF/WAV/SQLite transforms belong here or in ziptastic's Layer 0? | OP-1, OP-2, OP-3, OP-11 |
| D-4 | Is Windows a first-class target (CI + a real run) or "should work"? | CT-1 |
| D-5 | Do you want standalone binaries (PyInstaller), given the fleet ships single-file apps? | PK-1..PK-3 |
| D-6 | Should `optimize` ever default above level 1 for folders? | PF-6 |
| D-7 | Is a scheduled verify (HB-4) worth a GitHub Actions cron, given Actions minutes are paused? | HB-4 |

---

## Status of the repository today

- **Done:** 1.0 (engine, CLI, UI, 31 tests), 1.1 (verify, info, doctor, bench,
  manifest digest, CHCE detection, engine delegation, 43 tests), and the two
  items that made the delegation true: **EN-1** (a CI job that fetches the
  published ziptastic binary, checks it against its own `SHA256SUMS`, and asserts
  Kosh and ziptastic read the same header) and **EN-2** (`unpack --engine`, so
  what `pack --engine` writes can be read back). 49 tests.
- **Also done:** **CT-1/CT-2** — the suite runs on `windows-latest` and
  `macos-latest` as well as Ubuntu, six jobs. The first Windows run found three
  real defects: `kosh doctor` died on a legacy codepage, the engine's output was
  decoded with the locale encoding, and the stand-in engine was a shell script.
- **Also done:** **EN-6** — `kosh engines` lists every engine with its path and
  version, and `doctor` reports the version rather than only presence.
- **Also done:** **CT-9** (the build half) — `release.yml` builds sdist and wheel,
  `twine check`s them, installs the wheel in a clean environment and runs
  `kosh doctor` from it. Publishing waits on a `PYPI_API_TOKEN` secret; the job
  stops at a guard that names it.
- **Also done:** **EN-3** — `kosh scan` predicts the engine's output from a 1 MiB
  middle sample compressed by the engine itself: worst error 2.2% across the
  corpus (`docs/FINDINGS.md` §7).
- **Known gaps, honestly:** Zopfli is the slow path on anything above 1 MB, the UI
  forgets a job when the tab closes, the landing page is not published, and an
  engine stream cannot be listed without decoding it.
- **Also done since:** **CT-10** — `tools/check-version.py` fails the build when
  `pyproject.toml`, `kosh/__init__.py` and the version badge disagree. **EN-4** —
  `kosh info file.dr` reads the v8 trailer and prints the block table, and CI
  asserts it matches `ziptastic info` block for block. **CT-11** —
  `tools/check-claims.py` fails the build when a number in the README is not in
  `docs/FINDINGS.md`.
- **Next three, in order:** EN-5 (`run recipe.yaml`), CT-3 (property-based
  round-trips), CT-4 (corruption fuzzing).
