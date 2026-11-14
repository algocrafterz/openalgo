# Committing while another session works on the same branch

- Another agent session may commit to the same branch and edit the same files at any moment. Re-run `git log -3` and `git status` immediately before any commit or reset; a snapshot from earlier in the conversation is stale.
- Never address a commit as `HEAD~1` or `HEAD` after time has passed. Use its full SHA (`git reset --soft <sha>^`). A `HEAD~1` reset once undid another session's commit; it was restored with `git reset --soft <that sha>`.
- Before staging a file, run `git diff <file>`. A file you edited may already hold another session's uncommitted hunks (listener.py carried an unrelated late-EXIT trading change into a feature commit). Stage only your hunks (`git apply --cached` of a patch, or `git update-index --cacheinfo` with a hash-object blob built from HEAD plus your edits).
- Do not rewrite a commit that has later commits on top while another session is active. Report the problem and let the user schedule a rebase.
- Check line endings first (`git ls-files --eol`): signal_engine/listener.py is CRLF in the working tree.
