# Changelog

## Logfire: explicit axis checkpoint tracing

Added named Logfire spans around the hidden axis-reasoning checkpoint path:
`axis checkpoint`, `axis evidence critic`, and `axis append planner`. This makes
it visible when a planner-selected checkpoint fires, how much evidence it
reviewed, which append attempt ran, and whether appended task validation
succeeded or failed. See `docs/CHANGELOG_LOGFIRE.md` for details.

## Workspace filesystem layout: namespace workspaces under user id

### Summary
Workspace directories on disk are now namespaced under the owning user's id.
Previously every user's workspaces shared one flat parent directory, so
workspaces from different users sat side by side. Each user's workspaces now
live under a parent folder named with their user id.

**Before:**
```
file_system_root/<workspace_name>/<query_counter>_<query_id>/...
```

**After:**
```
file_system_root/<user_id>/<workspace_name>/<query_counter>_<query_id>/...
```

### Why
With the old flat layout, all users' workspaces were children of the same
`file_system_root` directory — there was no per-user isolation at the
filesystem level, and workspace names could collide across users. Nesting each
workspace under its owner's user id gives every user their own parent folder
and mirrors the per-user scoping that already exists in the database
(`workspace_id` + `user_id`).

### Changes

#### `api/routes/chat.py`
- **Added `workspace_dir(user_id, workspace_name) -> Path` helper.** Single
  source of truth for the on-disk workspace path. Returns
  `Path.cwd() / "file_system_root" / str(user_id) / workspace_name`. Create,
  delete, and run all derive their paths from this one function so the layout
  can never drift between them (the same class of bug that previously caused
  `file_system_root` vs `filesystem_root` path mismatches).
- **`create_chat`** now builds the workspace path via `workspace_dir(user_id,
  workspace_name)` instead of the inline `f"{Path.cwd()}/file_system_root/
  {workspace_name}"`. `user_id` is already a parameter of `create_chat`, so no
  new plumbing was needed. The per-run subdir (`{query_counter}_{query_id}`)
  is still appended under this path, so the full run path becomes
  `file_system_root/<user_id>/<workspace_name>/<counter>_<query_id>/`.

#### `api/routes/workspace.py`
- **Imported `workspace_dir`** alongside the existing `make_workspace` import
  from `api.routes.chat`.
- **`register__workspace` (POST /create_workspace)** now creates the directory
  at `workspace_dir(user_id, workspace_name)`. Hoisted `user_id =
  session.get_user_id()` to the top so it is available for both the DB insert
  and the path.
- **`delete_workspace` (DELETE /delete_workspace)** now removes the directory
  tree at `workspace_dir(user_id, workspace_name)` instead of the old flat
  path.

All four path-construction sites (one in `chat.py`, create + delete in
`workspace.py`) were updated together. Updating only one would have caused
create/delete/run to point at different directories ("workspace not found" or
orphaned folders).

### Not changed (intentionally)
- **`db/models/models.py` — no schema change.** The database never stores the
  `file_system_root` path; the workspace directory path is computed at runtime
  by `workspace_dir`. `Workspace` holds only `workspace_id`, `user_id`,
  `created_at`. `QueryRun.workspace` is a free-text path column that simply
  records whatever absolute path `create_chat` computes, so new runs store the
  new-style path automatically with no migration.
- **No Alembic migration** — this is a filesystem layout change, not a schema
  change.
- **File-serving routes** (`/workspace/.../runs/.../outputs/...`) are
  unaffected: produced files are served from the DB (`QueryRun.produced_
  artifacts`, stored as base64), not from disk.
- `make_workspace` uses `os.makedirs`, which creates the intermediate
  `<user_id>` directory automatically on a user's first workspace.

### Migration note for existing data
Workspace directories created under the old flat layout
(`file_system_root/<workspace_name>`) will not be found at the new
user-namespaced path. Their DB rows still exist, so `create_chat` will detect
the workspace in the database and recreate an (empty) directory at the new
path — old on-disk files are not migrated automatically. If real workspace data
exists on disk under the old layout, move it once:

```
file_system_root/<workspace_name>  ->  file_system_root/<user_id>/<workspace_name>
```

This is a one-time filesystem move, not a database operation. (Produced-file
artifacts persisted in the DB are unaffected and remain downloadable
regardless.)
