# YTPod administration

## Add a feed

Use the YunoHost application tile as an administrator. Enter a unique lowercase feed ID, a display name, and an HTTP(S) source URL. The source is handed to yt-dlp.

Keep the retention value conservative. YTPod downloads audio and stores it locally, so a high retention value consumes disk space.

## Force a refresh

Use **Refresh now** from the feed card. Processing happens in the background. The feed card's last-run field will update when the scheduled job completes.

## Inspect logs

The main log is:

```text
/var/log/ytpod/ytpod.log
```

The systemd unit also writes service output to the same file.

For live service diagnostics:

```bash
sudo journalctl -u ytpod -f
```

## Change the public URL

YunoHost's normal app URL-change workflow calls `scripts/change_url`; YTPod regenerates its NGINX/systemd template values from the new domain/path. Existing feed tokens remain stable.

## Manual configuration

`/etc/ytpod/feeds.toml` can be edited with care if the administration page is unavailable. The next service restart will reread it. A safer approach is to stop the service before a manual edit and validate the TOML syntax with:

```bash
sudo /var/www/ytpod/venv/bin/python -c 'import tomllib; tomllib.load(open("/etc/ytpod/feeds.toml","rb"))'
```
