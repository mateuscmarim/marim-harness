---
name: marim-docs
description: Use when the user asks a question about how Marim works, what a feature does, how to configure something, what docs cover a topic, wants an overview of the system, or needs to find relevant docs for a task they're working on.
---

# marim-docs

## Overview

This skill navigates and reads Marim's documentation to answer questions about the
project. It uses a curated index for fast routing and reads docs at runtime so
answers are always current.

## When to Use

Activate this skill when the user:
- Asks how a Marim feature works ("how do sessions work?", "what is compaction?")
- Asks how to configure something ("what env vars control the model?", "how do I set up MCP?")
- Wants an overview of the system or a subsystem
- Needs to find which docs are relevant for a task they're working on
- Asks about commands, slash commands, CLI flags, or API endpoints

Do NOT activate for:
- Questions about the source code implementation (use grep/read_file directly)
- Questions about bugs or unexpected behavior (use superpowers:systematic-debugging)
- General coding questions unrelated to Marim

## The Index

Read `index.md` (in this same directory) first. It maps every Marim doc to its
purpose, key topics, and example trigger questions.

## Routing Rules

1. **Match the user's question** against the index entries — look at the "Topics"
   and "When to ask" fields for each doc.

2. **If a match is found:** Read the matched doc(s) using `read_file` (with the
   workspace-relative path from the index). Answer the user's question from the
   doc content.

3. **If multiple docs match:** Read the most relevant one first. If the answer
   isn't complete, offer to read the others: "I found a few relevant docs — want
   me to also check [doc]?"

4. **If no match is found:** Fall back to reading `docs/README.md` to see the
   full doc structure, then route from there.

5. **For overviews:** Read 2-3 key docs (usually `docs/architecture.md` plus
   the most relevant guide) and synthesize.

## Fallback

When the index doesn't cover a question:
1. Read `docs/README.md` for the full doc listing
2. If still unclear, search the workspace with `grep` for relevant terms in `docs/`
3. If the question is about source code behavior rather than documented behavior,
   read the relevant source file directly

## Updating the Index

When docs are added to, renamed in, or removed from the project, update `index.md`
to match. Keep the index in sync with `docs/README.md` as the source of truth.
