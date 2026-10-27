You check a lab notebook entry against the log it was written from. Split the entry into atomic claims: one fact each (one run's outcome, one number, one file, one parameter value). Ignore headings, questions and proposed next steps.

For each claim give its text, the IDs it cites (copied exactly, like `R:01J...`), and the checks that would verify it against the log. Each check has a kind and fields; leave unused fields as empty strings:
- `exit_code`: `run` is the run ID, `value` the exit code the claim states (for example "0" for a run that succeeded, "1" for one that failed).
- `command_contains`: `run` is the run ID, `value` a short exact fragment of the command the claim states (a script name, an option and its value such as `--cutoff 1.2`).
- `file_named`: `path` is a file the claim says was used or produced, as written in the claim; `run` the run if the claim ties the file to one.
- `param_value`: `path` is the parameter file, `key` the parameter name and `value` the value the claim states; `run` the run that used that value, if stated.
- `content_contains`: `path` is a text file and `value` a short exact fragment of its content that the claim quotes (a number, a line); `run` the run that wrote it, if stated.
- `none`: the claim cannot be checked mechanically (interpretation, a description of a figure).

Only add checks the claim actually makes. Prefer several precise checks to one vague one.
