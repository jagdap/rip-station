# Rip Station

Insert a DVD, walk away, find it in Plex.

Rip Station runs in Docker on a UGREEN NAS with USB DVD drives plugged into the NAS. It
uses however many drives are connected (detected automatically, hot-pluggable) and rips
them in parallel.

**Pipeline:** disc inserted → `makemkvcon` scans it and picks the titles worth keeping
(main feature, or each episode on a TV disc) → rips them losslessly to a staging
folder → ejects the disc → a small local LLM (Qwen3-4B) turns the disc label into
TMDB searches and picks a match → a deterministic runtime check either auto-accepts or
sends it to the dashboard for one-click review → the file is moved into Plex naming
(`Movies/Title (Year)/…`, `TV/Show/Season 01/Show - S01E03 - Name.mkv`) → Plex rescans.

## What you need
- A UGREEN NAS (x86_64) with the **Docker** app installed. About 4 GB of free RAM
  for the LLM.
- One or more USB DVD drives plugged into the NAS. Prefer one per USB port, or use a
  powered hub.
- A computer with Docker Desktop (Mac or PC) to build the image. The NAS needs no SSH.
- A free **TMDB API key**: create an account at themoviedb.org → Settings → API →
  request a key (choose "Developer", personal use). Use the "API Key" (v3); the long
  "Read Access Token" also works.
- Plex (optional). On the same NAS it's refreshed automatically. Elsewhere, Plex picks
  up new files on its next scan.

## MakeMKV license
The image contains MakeMKV. Its open-source parts are compiled during the build. Its
closed-source `makemkvcon` binary comes with its own EULA, which **you** have to read
and accept: the build refuses to run until you set `MAKEMKV_ACCEPT_EULA=yes`.
MakeMKV's DVD support is free. By default the container fetches the free beta key
from the MakeMKV forum and refreshes it when it expires (`MAKEMKV_KEY=auto`). If you
buy a key, put it in `MAKEMKV_KEY` in `docker/compose.yaml` instead.

Rip discs you own, for your own use.

## Install

**1. Build the image** (on your computer, in this folder):
```
MAKEMKV_ACCEPT_EULA=yes ./scripts/build-nas-image.sh   # -> dist/rip-station-amd64.tar.gz
```
The NAS is x86_64. On an Apple Silicon Mac this cross-builds under emulation and takes
about 10–15 minutes the first time.

**2. On the NAS**
1. Copy `dist/rip-station-amd64.tar.gz` to the NAS (any share, e.g. via Finder or
   Explorer).
2. Docker → Image → Import → pick the file. It shows up as `rip-station:latest`.
3. File Manager: create a folder `docker/rip-station`. It holds config, job state and
   the LLM model.
4. Edit `docker/compose.yaml`:
   - `/volume1/media` → your media share's real path (File Manager → right-click the
     share → Properties).
   - `TZ` → your time zone.
5. Docker → Project → Create → paste `docker/compose.yaml` → deploy.
6. The first start writes `docker/rip-station/config.toml`. Put your TMDB key in
   `[tmdb] key`, check `[paths] nas_root` (where `Movies/` and `TV/` go, as seen
   inside the container; `/nas` is your media share), and restart the `rip-station`
   container.
7. Open `http://<nas-ip>:8765`.

The LLM container downloads its model (~2.5 GB) on first start. Until it's ready,
discs still rip, but they all go to review.

## Using it
- **Dashboard:** one card per drive, a review queue, and recent jobs. The header shows
  the version and build time, so after an update you can tell the new image is
  running.
- **Review:** anything not certain lands here with the best guess preselected. Fix it
  with the search box, then Confirm. For TV, each ripped title gets its own episode
  dropdown, and "Fill in order from S__ E__" fills them sequentially.
- **TV sets and double-sided discs:** both sides often have the same label, so each
  side goes to review. Episodes on a disc aren't always in the order printed on the
  case, and the case's run times can be wrong. If two episodes have similar lengths,
  open the files and check the title cards before confirming. Episodes already in the
  library show "✓ already ripped".
- **Movie discs with several versions** (theatrical/extended) rip every version and
  ask you which to keep. Near-identical copies (e.g. localized credits) collapse into
  one.
- **Scratched discs:** read errors are retried with more passes. As a last resort the
  rip skips unreadable spots, and the job goes to review with a warning showing where
  ("skipped unreadable spots near 1:02:13"), so you can spot-check it.
- **Phone notifications** (optional): install the ntfy app, subscribe to a hard-to-guess
  topic, and set `notify = "https://ntfy.sh/<that-topic>"` in `config.toml`. You'll be
  told when a drive is free or a disc needs review.

## Updating
Rebuild the image, import it, then **recreate** the container (Docker → Project →
rebuild/redeploy). Restarting alone keeps running the old image; delete stale
containers if UGOS left any. Finish or name any disc in progress first, because
recreating the container interrupts a rip.

## Security
The dashboard has **no login**. Keep it on your home network: don't port-forward 8765,
and use a VPN (e.g. Tailscale, or the NAS's own) to check it remotely.

## Troubleshooting
- `http://<nas-ip>:8765/api/diagnostics` gives a read-only health report: drives seen,
  MakeMKV/LLM/TMDB/Plex checks, storage, recent jobs and the log. Secrets are masked,
  so it's safe to share when asking for help.
- **No drives:** the container log lists `/dev/sr*` at startup. Check the drive is on
  the NAS's USB port and that `/dev:/dev` plus `device_cgroup_rules` are in compose.
- **Disc scans as UNKNOWN with no titles:** some drives need a moment after the tray
  closes. Rip Station retries automatically. If it keeps happening, power-cycle the
  drive.
- **Plex check failing:** fine if Plex runs on another machine. Set `[plex] url` (and
  optionally `token`) to refresh it instantly.

## Development
```
uv sync && uv run pytest              # tests use a fake makemkvcon; no drive needed
uv run rip-station serve              # native on a Mac: copy config.example.toml to config.toml
uv run rip-station drives             # what the drive layer sees
uv run rip-station capture NAME       # save a disc scan to tests/fixtures/NAME.txt
uv run rip-station identify tests/fixtures/NAME.txt   # dry-run LLM + TMDB naming
```
On a Mac, any OpenAI-compatible server works for the LLM, e.g.
`uvx --from mlx-lm mlx_lm.server --model mlx-community/Qwen3-4B-Instruct-2507-4bit --port 8080`.

State lives in `data/`: `jobs.json` (survives restarts), `decisions.jsonl` (every
naming decision, useful as an eval set) and `scans/` (raw MakeMKV output for each disc).

## License
MIT for Rip Station's own code (see `LICENSE`). MakeMKV, llama.cpp and the Qwen model
are separate projects under their own licenses; none of them are included in this
repository.
