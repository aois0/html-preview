# Windows-safe history

This repository was rebuilt from the WSL Git history so Windows can check out every file.

Windows cannot store : in filenames, so historical paths containing : were rewritten to %3A across the Git history.

- Active Windows branch: $(System.Collections.Hashtable.SafeBranch)
- Path map: _migration-notes/path-map.jsonl
- Original WSL copy: D:\dev\from-wsl\home\html-preview

Do not force-push this branch over the original default branch unless a deliberate upstream migration has been planned.
