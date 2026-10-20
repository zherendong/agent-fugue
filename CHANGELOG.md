# Changelog

This file records the changes to fugue that someone using it would notice, newest first and grouped by the day they were made. Each line says what changed; the commit messages explain why.

## 2026-10-20

- Comments, reviews and votes check any rewrite of your own material (an attached file, text in the question, a file in the folder) line by line against the source, and a claim stronger than the source counts as an objection.
- Members check their own changed claims against the source before they hand in an update, merge, revision or closing version.
- When the round limit ends a discussion with objections, a section under the latest answers shows the last objections: with two members, next to each author's response; in a council, the reviews that still object.
- A DeepSeek member's resume command now works: `dsh --profile headless --patch <overlay> --session-id <id> "<your message>"` sends one message to its session.
- DeepSeek members use the API key saved on dsh's Models page, also when fugue runs as a background service, and Settings looks for the key in the same order dsh does.
- The text version of a PDF reads each page by layout, so words that LaTeX draws a few letters at a time no longer come out split ("REVOL VE").
- A PDF's first 10 pages are rendered as images: Codex gets them with its message, the DeepSeek Harness gets their paths, and Claude Code is told to open the PDF itself.
- Every member is told that an attached file's original counts over its text version, so layout and formatting are judged from the original or its page images.
- "Changes since step N" shows an updated, merged or revised answer against the same member's previous version, word by word.
- An answer and its responses to the comments are shown apart: the responses are folded away under the latest answers, and Copy copies the answer alone.
- Comments and reviews end with a suggestion count next to the objection count.
- When a comment step or a council's review has no objections but has suggestions, a closing step takes in the suggestions each author accepts, and the discussion ends there.
- Updated, merged and revised answers stand on their own, with the discussion after a `## Responses` (or `## Points not taken`) heading instead of inside the answer.
- Members write about themselves as "I" and call the others by letter.
- A point beyond what you asked counts as a suggestion at most, not an objection.
- Updates and revisions keep the previous version's layout, so versions are easy to compare.
- Clicking Attach in the bottom bar no longer expands the bar.
- Steps are numbered from 1 on the page, in the transcript, in `fugue status` and in `fugue show`, and "round" means only what the round limit counts: a comment step and an update step, or a review and a revision.
- "Pause after each round" is now "Pause after each step", or "Pause after the vote and each revision" in a council, and a reply's own activity is called its actions.
- Page fixes: tables in replies scroll sideways instead of breaking words, long model names and one-word titles no longer overflow, notices no longer cover the bottom bar's button, link-style actions are easier to read, and Escape closes the details panel.
- A council's vote stays readable under "How they got here", and its row names the chosen answer.
- A queued follow-up question shows its files, and Edit takes them back into the box with the text.
- While a reply streams in, scrolling up stops the page from pulling you back down, and the page doesn't redraw under text you are selecting.
- In a council, waiting notes say they go out with their member's next message.
- A note typed while the members work no longer turns into a follow-up question when the step ends.
- When a step can't start (for example because its folder was moved), the reason stays on the page and in `fugue status` until a step starts again.
- Replies from a process that was killed show as stopped, and slow answers from the server no longer overwrite newer ones or the discussion you opened meanwhile.
- Double-clicking a button or pressing ⌘↩ twice no longer starts the members twice, Enter in a New question field no longer starts a question, and changing the round limit of a finished discussion no longer starts it.
- Short summaries tell apart members that use the same tool ("Codex B").
- The vote board and the bottom bar stay readable in narrower windows.
- Reply headers are shorter, and Vote again, One more round, Stop and Attach explain themselves on hover.
- The bottom bar's text box uses the full width while you write.
- With no discussions yet, the New question page explains what happens after you ask, and the folder field is labelled "Folder".
- Member rows are shorter and the same shape: the extra-arguments box stays folded unless it has something in it.
- Clicking an attached file shows it on the page (images and PDFs as they are, Word files as text, Markdown rendered), with a Download button.
- Each discussion keeps its own unsent text and files in the bottom bar, also across reloads.
- A banner says when fugue can't be reached or has restarted, with a button to reload the page.
- A missing tool is flagged under the member that uses it, instead of on every question.
- The round setting's label and explanation, the New question summary and the details panel match the number of members and whether the question has a folder.
- Fixed file sizes breaking over two lines, "null" in member rows, the layout switch showing over a single answer, and a multi-line question showing its first line twice.
- Files can be attached from the page: under the question box, by dropping or pasting them, or from the bottom bar with a follow-up.
- Questions and follow-ups can come with attached files (PDF, Word, images, text; up to 50 MB): fugue keeps them with the discussion, adds text versions of PDF and Word files, and lists them in each member's message (`--attach FILE`).
- A question no longer needs a project folder: without one (`--no-folder`, or No folder on the page), the members work among the attached files.

## 2026-10-19

- The page shows councils of three or four: up to four member rows, a vote board with each answer's points and "Start from this one", steps named for what they do, "The council's answer", and Vote again.
- Three or four members work as a council: they vote for the best answer to start from, its author merges the others' points, and the others review it until no one objects or the round limit is reached; `fugue revote` asks for a new vote.
- When a step can't be created, the discussion pauses instead of trying the same step again and again.
- The DeepSeek Harness (`dsh`) can be a member, with DeepSeek Flash or DeepSeek V4 Pro, running read-only in its own sandbox.
- The page and the context inspector name members by tool, model and effort, for example "Claude Opus 5.5 · xhigh, member A".
- "Inspect context", under every reply, shows exactly what fugue sent the member in that step and the whole conversation up to it.
- Your notes and follow-up questions appear in the discussion as your own entries, with Edit and Remove until they go out.
- One bar at the bottom of a discussion holds your note and the button for the next step, named for what it does; ⌘↩ presses it.
- The desktop notifications switch moved from the sidebar into Settings.
- The page puts the answers first: replies side by side (or "One at a time"), a ⋯ panel for the folder, members, plan and rarely used actions, and earlier steps folded into one line.
- Fixed Settings' status check showing "[object HTMLDivElement]" instead of the Claude Code and Codex details.
- An updated answer comes first, with the responses to the comments after it.
- A finished discussion opens on where it ended, with each member's latest answer, and in windows narrower than 760 px the list of discussions slides in from a ☰ button.
- fugue is licensed under Apache-2.0.
- Each member can be any tool with its own model, effort and extra arguments, so both members can use the same tool; on the command line, `--member TOOL[:MODEL[:EFFORT]]`.
- A Settings page holds the defaults for new discussions, saved to the config file that `fugue start` reads, with a status check of each tool's install and login.
- A discussion can be moved to the Trash from the page or with `fugue delete`; the members' own sessions are kept.
- The tool is renamed from duet to fugue: the command, the package, the `~/.fugue` folder, the `FUGUE_*` variables and the service label.
- First version, as duet: Claude Code and Codex answer a question on their own, then comment on each other's answers and update their own until neither objects or the round limit is reached.
- Members run read-only in their tools' sandboxes, know each other only by letter, and pause after every step; discussions run from the command line or a live local web page, which a launchd service can keep running on macOS.
