# YTPod — YouTube to Podcast for YunoHost

YTPod is a small, self-hosted YunoHost package that turns YouTube channels and playlists into private podcast feeds suitable for Pocket Casts and other podcast clients.

It is deliberately simpler than a general media server:

```text
YouTube channel / playlist
        ↓
      yt-dlp
        ↓
   AAC/M4A audio
        ↓
  private RSS feed
        ↓
    Pocket Casts
```

## Features

- Native YunoHost package layout using Packaging v2 helpers.
- Multiple independent feeds.
- YouTube channels, playlists, and other yt-dlp-supported playlist URLs.
- Audio-only M4A/AAC downloads to reduce storage use.
- Configurable retention per feed.
- Optional `since` date to avoid importing older videos.
- Optional podcast artwork URL.
- Stable, tokenized feed and media URLs. No login is required to retrieve the RSS feed.
- YunoHost administrator-only management page.
- Background refresh on a configurable schedule.
- Byte-range media responses for podcast-player seeking and resumable downloads.
- Atomic configuration/state writes.
- No Docker, database server, or Node.js runtime.

## Requirements

- YunoHost 12.1.38 or newer.
- A YunoHost domain/path reachable by Pocket Casts. The package requires an application path rather than occupying an entire domain.
- Sufficient disk space for the retained audio episodes.

The package pins `yt-dlp` to version `2026.08.19`. Update that pin in `src/requirements.txt` when a newer tested release is adopted.

## Installation from GitHub

Replace `YOUR-ACCOUNT` with the GitHub account that contains this repository:

```bash
sudo yunohost app install https://github.com/YOUR-ACCOUNT/ytpod_ynh/tree/main --debug
```

The app creates an administrator page at the configured YunoHost path. The public root is intentionally not useful as a discovery page; feed URLs contain a high-entropy access token.

## First feed

Open the YunoHost application administration page and add:

- a short feed ID such as `daily-news`;
- a human-readable feed name;
- the YouTube channel or playlist URL;
- the number of recent episodes to retain;
- an optional earliest publication date;
- optional artwork.

The first refresh downloads recent episodes. Copy the displayed RSS URL into Pocket Casts as a private/custom feed.

## Configuration

The persistent configuration lives at:

```text
/etc/ytpod/feeds.toml
```

Persistent state and media live under the YunoHost data resource, normally:

```text
/var/lib/ytpod/
```

The default settings are:

```toml
update_interval_hours = 6
default_keep_last = 20
audio_bitrate = "96K"
```

`audio_bitrate` is passed to ffmpeg through yt-dlp. Raise it if source quality warrants it and storage is not a concern.

## YouTube cookies

Some YouTube sources may require authentication or encounter anti-bot challenges. This initial package does not provide a browser-cookie import UI. A future release can add a protected cookies file under `/etc/ytpod/` and pass it to yt-dlp.

Do not put a YouTube account password in `feeds.toml`.

## Security model

YTPod has two logical surfaces:

- `/admin/` is protected by YunoHost's administrator permission and consumes the `Auth-User` header only for CSRF token binding.
- `/feed/...` and `/media/...` are intentionally reachable without YunoHost authentication so Pocket Casts can retrieve them.

Feed and media URLs contain a random token. Treat the RSS URL as a bearer credential: anyone who obtains it can consume that feed until it is deleted/recreated.

The package does not expose a route that accepts arbitrary filesystem paths. Media filenames are restricted to a feed-specific prefix plus a YouTube-style identifier.

## Storage behavior

Only retained episodes are kept. When a feed exceeds its retention count, older audio files and state records are removed. A feed that keeps 20 one-hour episodes at 96 kbps requires roughly 1.3 GB for audio, plus overhead.

## Upgrading

The upgrade script refreshes the application code and Python virtual environment while preserving `/etc/ytpod` and `/var/lib/ytpod`.

## Backups

YunoHost backups include:

- application code and virtual environment;
- feed configuration and the secret token store;
- downloaded media/state;
- NGINX, systemd, logrotate, and log files.

Large media backups may be substantial. The YunoHost data resource is intentionally separate from the install directory.

## Development

This repository follows the structure recommended by the YunoHost `example_ynh` package template.

Local static checks:

```bash
bash -n scripts/*
python3 -m compileall src tests
python3 -m unittest discover -s tests -v
```

For full YunoHost integration testing, use the official `package_check` project against a clean YunoHost container/VM.

## Limitations in 0.1

- Audio only; no video podcast output.
- No browser-cookie import UI.
- No per-feed scheduling; one global refresh interval.
- No automatic local artwork download.
- No direct editing UI for existing feeds; delete/recreate changes an ID's token.
- The app currently processes feeds sequentially at the feed level and is intentionally conservative with concurrency.

## License

The YTPod package code is released under the MIT License. yt-dlp remains separately licensed under its own terms.
