# FolderSync

Small Python web app for comparing and syncing several folders (local and/or SSH)
— e.g. two local folders and one on a server.

## Run

```bash
pip install -r requirements.txt     # paramiko, only needed for SSH folders
python foldersync.py                # http://127.0.0.1:8765
python foldersync.py --port 9000 --config ~/my-sync.json
```

## Use

1. **Profiles** (top bar): each profile is one setup of folders. *New* / *Duplicate* / *Edit* / *Delete*.
   Folders are `local` (path, `~` allowed) or `ssh` (host, port, user, key file and/or password, remote path).
   Exclude patterns match any path part (`.git`, `node_modules`, `*.pyc`, `build/*`).
   Compare mode: *size + mtime* (fast) or *checksum when times differ* (uses `md5sum` on the server).
2. **Scan / Compare** lists every file in the union of all folders with size and modified time per folder.
   Green = newest, orange = older, red = missing.
3. **Choose what to do per row**: by default the newest copy is the source. Click another folder's cell
   to use it as source instead; Shift-click a cell to delete that copy. Or set *Bulk source* to one folder
   to push it everywhere.
4. Tick rows (or *Select all shown*) → **Sync selected**. You get a confirmation listing every action.
   Copies keep the source's modification time, so the next scan shows them as identical.
5. **Diff** shows a unified text diff between any two copies of a file.

The config is stored in `foldersync.json` next to the script (git-ignored, since it may contain passwords —
prefer SSH keys). The server listens on 127.0.0.1 only by default; it has no authentication,
so don't expose it on a network.
