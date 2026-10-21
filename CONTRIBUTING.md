# Contributing to collab-room

Thanks for improving collab-room. Bug reports, fixes, new agent integrations and documentation are all welcome.

## Before you start

- For a bug, open an issue with what you ran, what you expected and what happened (OS, Python, tmux,
  agent CLI versions).
- For a larger change, open an issue first so we can agree on the approach before you spend time on it.
- Security problems: do not open a public issue — use GitHub's private vulnerability reporting
  (Security → Report a vulnerability).

## Making a change

1. Fork the repository and create a branch from `main`.
2. Edit the package in `src/collab/`. The root file `collab` is built from it — rebuild it, never edit it by hand:

   ```sh
   python3 tools/build_collab.py
   ```

3. Every change of behaviour comes with a test that fails on the old code because of the behaviour itself,
   not because a name or signature changed.
4. Run the checks:

   ```sh
   scripts/ci --quick                       # lint, format, types — seconds
   python3 -m unittest discover -s tests    # the full suite, a few minutes
   ```

   Without tmux about half of the suite is skipped; install tmux to run it all. The tests use their own
   tmux server in a temporary folder and never touch your rooms.
5. Keep the code style: standard library only; the code runs on Python 3.11+ but must still *parse* on 3.9
   (an old `python3` hands the run over to the recorded 3.11+), so no newer syntax; `ruff format`, comments in English that
   explain *why*, not *what*.

## Pull request

Describe what changed and why, how you checked it (commands and their output), and what your checks do not
prove. Small, focused pull requests are reviewed faster.

## License

collab-room is licensed under the [GNU GPL v3.0 or later](LICENSE). By submitting a pull request you agree
that your contribution is licensed under the same terms.

If you maintain your own modified version, consider sending the improvements back — and if you distribute it,
the GPL requires its source to be available under the same license.
