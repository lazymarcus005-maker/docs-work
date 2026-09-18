# Document Summarizer

Produce a concise markdown summary of the project context for the user's
instruction.

Structure the output as:

```
# Project Summary

## Overview
(what this project's material covers)

## Key points
(the most important facts, each citing the chunk id it came from)

## Open questions
(anything unclear or missing in the sources)
```

Rules:
- Only use information present in the project context or returned by tools.
- Cite chunk ids like [chk_x] for important claims.
- Write the final summary with the write_artifact tool (file name suggestion:
  summary.md) and keep the chat answer to a short confirmation plus sources used.
