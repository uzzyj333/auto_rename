# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
pip install -r requirements.txt

# Run the application (monitor mode)
python -m video_organizer

# Run with web admin UI (--web flag)
python -m video_organizer --web

# Run all tests
pytest

# Run a single test file
pytest tests/unit/test_core/test_renamer.py -v

# Run tests with coverage
pytest --cov
```

## Architecture Overview

This is a video file auto-rename and organization tool. The entry point is `src/video_organizer/main.py` (`python -m video_organizer`). A legacy `main.py.tmp` exists at the root but is not the active entry point.

### Core Processing Pipeline

1. **`FileSystemMonitor`** (`core/filesystem_monitor.py`) — Orchestrator. Watches a directory (via polling or downloader API) and dispatches new files to `VideoFileHandler`.
2. **`DownloaderMonitor`** (`core/downloader_monitor.py`) — Abstract base with `Aria2Monitor` and `qBittorrentMonitor` implementations. Polls downloader APIs for completed downloads and fires a callback into `FileSystemMonitor`.
3. **`VideoFileHandler`** (`core/video_file_handler.py`) — Processes individual files: resolves path mappings, calls `VideoRenamer`, moves/copies files, triggers uploads.
4. **`VideoRenamer`** (`core/renamer.py`) — Three-stage filename parser:
   - **Stage 1**: Regex patterns (50+ patterns for common naming conventions)
   - **Stage 2**: `GuessItParser` (guessit library) for robust extraction
   - **Stage 3**: Compact format fallback (acts as safety net after GuessIt)
   - Then queries `TMDBClient` to enrich metadata and generates final output path via Jinja2 templates.
5. **`TMDBClient`** (`core/tmdb_client.py`) — Wraps the TMDB API. Supports both JWT Bearer tokens (starts with `eyJ`) and regular API keys. Routes through a proxy at `proxy1.liyk001.eu.org`.
6. **`ManualRuleEngine`** (`core/manual_rule_engine.py`) — DSL-based pre-processing layer that runs *before* filename parsing. Supports five rule types: `block:` (remove words), `replace:` (substitute text), `position:` (slice filename), `{[tmdbid=...]}` (embed TMDB ID/season/episode directly), and `when:` (conditional rules). Fields set by manual rules are locked and won't be overridden by TMDB.

### Web Admin Backend

`web/app.py` creates a FastAPI app mounted at `/`. Routers under `web/routers/` expose REST APIs at `/api/*` for config management, task monitoring, logs, manual file processing, and downloader status. A `StateManager` singleton (`web/services/state.py`) holds shared references to the running `VideoFileHandler` and config.

When `--web` is passed, the main process starts both `FileSystemMonitor` (in a thread) and `uvicorn` (serving the FastAPI app).

### Upload Integrations

`upload/upload_emos.py` implements `RobustEmosVideoUploader`, the only upload target. It talks to the official Emos API: `POST /api/upload/getUploadToken` → (multipart presign / PUT / complete, or google_drive resumable) → `POST /api/upload/video/save`. `core/emos_client.py` wraps every other official endpoint used for recognition (`/api/video/getVideoId`, `/api/video/tree`, `/api/upload/video/base`, ...).

### Online Recognition Upload

`core/online_upload.py` hosts the `OnlineUploadService` singleton driving the 「在线识别上传」 Web page:
1. Browse/scan a configured video root (`online_upload.video_root`) with path confinement.
2. `core/probe.py` runs ffprobe to validate the file and build the `file_metadata` sent to Emos (skipped gracefully when ffprobe is missing).
3. Recognition: `VideoRenamer` + TMDB → `GET /api/video/getVideoId`; falls back to `GET /api/video/tree` candidates the user picks manually.
4. Uploads run in a 3-worker thread pool; in-memory task progress is polled by the UI (`/api/online-upload/tasks`).

### Configuration

Config is an INI file (default `src/video_organizer/config.ini`; Docker uses `/app/data/config.ini`), loaded by `core/config_loader.py`. Key sections:
- `[monitoring]` — `watch_dir`, `output_dir`, polling settings, `path_mappings` (maps downloader container paths to host paths)
- `[naming]` — Jinja2-style format strings for `tv_show_format`, `movie_format`, `anime_format`, `simple_format`
- `[tmdb]` — `api_key`, `language`, `region`
- `[processing]` — `upload_targets` (fixed to `emos`), `max_upload_workers`, delete-after-upload behavior
- `[emos]` — `auth_token`, `base_url` (default `https://emos.best`), `file_storage`, `file_storages`, `chunk_size_mb`, `timeout`
- `[online_upload]` — `video_root`, `probe_enabled`, `ffprobe_path`, `path_type`
- `[logging]` — level / console / file logging; the log file always lives in the container's own log dir (`VIDEO_ORGANIZER_LOG_DIR`, default `/app/data/logs`)
- `[manual_rules]` — List of manual rules in DSL format (each rule on a new line)

Config edits made in the Web UI are hot-applied through `VideoFileHandler.apply_config()` and `OnlineUploadService.configure()` — no container restart required.

### Content Type Detection

`VideoRenamer` has a `DEFAULT_RELEASE_GROUP_MAPPING` dict that maps known fansub/release group names to content types (`anime`, `drama`, `movie`). This is used to bias TMDB searches toward the correct content type when guessit cannot determine it from the filename alone.

### Manual Rules DSL

Manual rules are processed *before* filename parsing and can lock fields to prevent TMDB override. Supported syntax:
- `block: word1,word2` — Remove words from filename
- `replace: old -> new` — Text substitution
- `position: start=N,end=M,offset=K` — Slice filename
- `{[tmdbid=123;type=tv;s=1;e=12]}` — Embed TMDB ID and episode info directly (also supports `doubanid`)
- `when: condition => rule` — Conditional application (e.g., `when: 包含"1080p" => block: 4K`)

Fields set by manual rules are locked and won't be overwritten by subsequent TMDB queries.

### Test Layout

Tests live in `tests/unit/test_core/` and `tests/integration/`. `pythonpath = ["src"]` is set in `pyproject.toml` so imports use `from video_organizer.core...` (not `src.video_organizer...`). The `src/video_organizer/main.py` module itself uses relative imports; only root-level scripts use the `src.` prefix.


## 语言规范
- 所有对话和文档都使用中文
- 文档使用 markdown 格式
