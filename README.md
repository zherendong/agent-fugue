# fugue

Ask two to four coding agents the same question, as a small council: Claude Code, Codex and the DeepSeek Harness, in any mix, with the models and effort levels you choose. Each answers on its own. Two members then comment on each other's answer and update their own, round by round; three or four vote for the best answer to start from, its author merges in the others' best points, and the others review it until no one objects. Either way it runs until there are no objections left or a round limit is reached. You watch it live in a local web page (or the terminal), move it on yourself after every round, and add your own view whenever you want, without copying replies between two windows. Like the voices of a fugue, each member first states its own answer, then they answer each other until the lines fit together.

```bash
fugue web                                  # open the page; start questions from there
fugue start "Why does the embedding cache miss on cold start? Propose a fix."   # or run one in the terminal
```

## Why

Sending one question to two agents covers more edge cases than either one alone. The hard part is the shuttling afterwards: copying each reply into the other agent, in the right order, with the right framing ("here is another agent's analysis of the same question" in the first round, "here is its review of your proposal" in the second), and keeping track of who has seen what. After a round or two that gets hard to manage. fugue does the shuttling, shows you what each agent is doing while it works, and keeps the record.

## How it works

**How fugue talks to the agents.** It runs their own command-line tools headless (`claude -p`, `codex exec` and `dsh --profile headless`), so each agent keeps its normal tools, project instructions (`CLAUDE.md`, `AGENTS.md`) and its ability to read the code. Each agent has one session for the whole discussion, and fugue resumes it at every step (`claude --resume`, `codex exec resume`, `dsh … --session-id`). So each agent remembers its own earlier reasoning, and fugue only sends what is new. While an agent works, fugue reads its live output (`--output-format stream-json` and `--json`) and turns it into actions you can watch: "Read src/cache.py", "$ rg -n cache src", how far Claude has got while it thinks ("Thinking: about 12k tokens so far"; at high effort it can think for minutes before writing), and the reply as it is written.

**Two members.** The members are equal; there are no roles. Each member is a tool (Claude Code, Codex or the DeepSeek Harness) with a model, an effort level and any extra arguments, and members can use the same tool, for example two Codex members with different models. With two members, both take part in every step, in parallel, and both do the same job:

- *Step 1, independent answers.* Both get your question unchanged, in separate sessions. Neither sees the other's answer.
- *Comment steps.* Each reads the other's current answer and comments on it: what it got right that it had missed, what is wrong or unsupported (checking the other's claims with its tools rather than trusting them, and citing what it checked), and what is missing for what you asked. Each comment ends with `Objections: N`, the problems in that answer that would change its conclusions or recommendations, and `Suggestions: N`. Smaller points, and anything beyond your question, are suggestions, so both members count objections the same way.
- *Update steps.* Each reads the comments on its own answer and writes its full updated answer, keeping its layout so the changes are easy to see. The answer stands on its own; after it, under a `## Responses` heading, the member says for each objection whether it accepts it, accepts part of it, or turns it down and why. What is still disputed goes there, not into the answer.

Comments and updates alternate. Each step only uses what the step before it produced, so no message has to keep track of replies still being written: comments are always about the other's answer as it stands now, which was written after reading every earlier comment.

It stops when a comment step has no objections on either side (they agree), or after the round limit (2 rounds of comments and updates by default; a round is a comment step and the update step after it). When they agree but a comment still has suggestions, the member whose answer got them writes one closing version that takes in the ones it accepts, and the discussion ends there. The result is the members' latest answers; if the round limit ended it with objections left, the page shows them under "Still open", with each author's responses.

**Three or four members.** Everyone reading and rewriting everyone's answer at every step would cost too much, so a council of three or four works from one shared answer:

- *Independent answers*, as above.
- *Vote.* Each member ranks the other members' answers (never its own), each seeing them in its own random order, and says what each gets right and the problems that would change its conclusions. Every ballot gives an answer one point for every answer it ranks below it; the answer with the most points is the starting point (a tie is drawn at random). A ballot without a readable ranking isn't counted, and the page says why.
- *Merge.* The author of the starting point merges the other members' best points into it (it read their answers when it voted), fixes the problems the ballots found, and lists the points it didn't take, with reasons.
- *Review.* The other members review the result: objections (including their own points left out or misrepresented), suggestions, and whether another starting point would clearly have been better.
- *Revise.* The author revises: the full answer first, then a response to each objection and the points it still hasn't taken.

Reviews and revisions alternate until no reviewer objects, or until the round limit (rounds of a review and a revision) is reached; the last revision is reviewed too, so you see what is still open. When no reviewer objects but some made suggestions, the author writes one closing version that takes in the ones it accepts. When most reviewers say the starting point was wrong, or the objections haven't gone down over the last two reviews, everyone answers again and the council votes again, once per question on its own; "Vote again" (or `fugue revote`) asks for it any time. Votes and reviews stay symmetric: every member ranks, and every member but the author reviews, so no answer wins by who wrote it. The result is the council's one answer, with the points it didn't take and any objections still open.

**Hidden names, fixed letters.** The members are never told which agent or model the other is. Each gets a letter for the whole discussion, member A or member B, drawn at random when the discussion starts, so the first member you choose isn't always "A". Every message from step 2 on says "You are member A; the other member is member B", forwarded replies are marked `from="member B"`, and the members are asked to write about themselves as "I" and refer to each other only by letter. A letter means the same member to whoever reads it, whereas "the other member" would point to someone else once a reply is passed on. The page, the transcript and `fugue status` show you who is who, for example "Claude (member A)", or "Codex · gpt-6.1-sol · high (member B)" when both members use the same tool. The exact wording sent to the members is in [src/fugue/prompts.py](src/fugue/prompts.py); edit it there to change how they talk to each other.

**Attached files.** A question (or a follow-up) can come with files: PDFs, Word files, images, text. They are kept in the discussion's own `attachments/` folder, and each member's first message for that question lists them after the question with their kind, size and path. PDF and Word files get a text version next to them (pypdf for PDFs), so every member can read them whatever its tool reads: Claude Code opens images and PDFs itself, Codex is handed the images with its message (`--image`), and the DeepSeek Harness reads text and, on image-capable models such as DeepSeek Flash, images. A question takes up to 50 MB of files. A question doesn't need a project folder: without one, the members work in the attachments folder, which suits reviewing a resume or a design document.

**Steps and rounds.** The page, the transcript and `fugue status` number each step from 1: step 1 is the independent answers, and every comment, update, vote, merge, review or revision after it is the next step. A round, as in the round limit, is a pair of steps after the answers: a comment step and an update step, or a review and a revision. Forwarded replies carry their step number (`step="2"`), so a member that cites a step means the same one you see.

**Where you come in.** fugue pauses and names the next step, for example "Next: each comments on the other's answer"; you move it on when you're ready. Two members pause after every step; a council pauses after its vote (to see the points, and start from another answer if you like) and after each revision. At any time you can:

- add a note to every member or just one; it goes into the next message fugue sends that member;
- stop the agents mid-step;
- ask for one more round, also after they agreed or reached the limit;
- ask a follow-up question, which starts again with independent answers;
- change the round limit, or switch off "pause after each step" to let it run to the end by itself. Changing these never starts the members on its own; Continue does.

If an agent fails or is stopped, the discussion waits; continuing retries just that agent with the same message.

**Who has seen what.** fugue records which of the other agent's replies each agent has already received, so nothing is forwarded twice or skipped and the order is always right. "Inspect context" on each reply shows the exact message the member got that round, and everything fugue had sent it up to then, with each part marked by where it came from.

**Same tool, same blind spots.** Members running the same model tend to make the same mistakes and agree on them, so the council catches less. Different tools or models work best; the page warns when two members run the same model.

**What the agents may do.** Read files and run commands, but not change anything: two agents editing the same checkout at the same time would trip over each other, and fugue is meant for analysis. Both run in their tool's own sandbox, where macOS itself refuses writes:

- *Codex* runs in its `read-only` sandbox, which also refuses its own file editor (`apply_patch`). What in your Codex setup acts outside that sandbox is switched off for fugue's runs: the apps connector (`--disable apps`), and every plugin and MCP server that is on in `~/.codex/config.toml` (computer use, Chrome, `node_repl` and so on), each with a `-c ….enabled=false` override. Your Codex config file is not changed, and your model and effort settings still apply.
- *DeepSeek* (the DeepSeek Harness) runs with its `read-only` permission preset, in its own sandbox (Seatbelt on macOS), with approvals refused since nobody can answer them. Its telemetry, which by default sends session records after a user's feedback, and its extra model call for a session title are off for fugue's runs, set in a settings overlay written for each run (`--patch`); your dsh profile is not changed. The overlay also turns on dsh's store of saved keys, which its headless mode leaves off, so runs use the API key you saved in dsh.
- *Claude* runs in Claude Code's sandbox, set for fugue's runs only (passed with `--settings`; your own Claude settings are not changed). Nobody can answer permission prompts in a headless run, so Claude runs in "don't ask" mode, where anything that would ask is refused; Bash is allowed outright because every command runs inside the sandbox. The sandbox refuses writes to the project and your home folder (the temp folder stays writable, as some tools need it), refuses network access, refuses to start a command outside itself, and the run fails rather than going ahead if the sandbox can't start. Claude's own file tools can't write (`Edit`, `Write` and `NotebookEdit` are turned off), and its Read tool may read any file except in `~/.ssh` and `~/.aws`. If your Claude settings list `sandbox.excludedCommands`, those commands would run outside the sandbox, so fugue then stops allowing Bash outright and Claude only gets the commands the sandbox approves by itself.

**The web, for both or neither.** "Members may use the web" (on by default; `--no-web` on the command line, `web = false` in the config file) applies to both members:

- *On:* Codex keeps its web search, which runs on OpenAI's servers. Claude may use WebSearch and WebFetch. Claude's page fetches come from your Mac, so fetches from `localhost` and `127.0.0.1` are refused; other addresses only your Mac can reach (other local servers, your network) are not blocked.
- *Off:* Claude's WebSearch and WebFetch are switched off, Codex runs with `web_search="disabled"`, and DeepSeek's web tools are switched off in its overlay.

With the web on, a page an agent reads could contain instructions aimed at it, for example to send the contents of a file it read somewhere. That holds for both agents; switch the web off for sensitive projects.

The exact flags are in `claude_read_only_args`, `codex_isolation_args` and `dsh_overlay` in [src/fugue/agents.py](src/fugue/agents.py).

## Install

You need [uv](https://docs.astral.sh/uv/), and on your PATH the tools your members use: `claude` and `codex`, each logged in as you normally use them (fugue uses those logins), and for DeepSeek members `dsh` (`npm install -g @deepseek-ai/dsh`) with a DeepSeek API key saved on dsh's Models page, or in `~/.dsh/.env` as `DEEPSEEK_API_KEY=…` (`DEEPSEEK_API_KEY` in your shell also works when fugue is started from that shell; the background service doesn't get it). Settings' status check shows what it finds.

```bash
git clone https://github.com/zherendong/agent-fugue.git
cd agent-fugue
uv tool install --python 3.11 --editable .
```

`--editable` means changes to the source (for example to the prompts or the page) take effect without reinstalling. Remove it with `uv tool uninstall agent-fugue`.

## The web page

`fugue web` serves the page at <http://127.0.0.1:8765/> and opens it. On the left is every discussion, from every project, with a dot on the ones that are working or waiting for you. On the right is the selected discussion:

- **At the top**, the question and one line saying where it stands ("Paused after step 2", "Agreed in step 4"). Attached files are listed under the question; click one to see it on the page (images and PDFs as they are, Word files as the text the members get) or to download it.
- **The answers**: the step in progress, or once it has finished, each member's latest answer, side by side ("One at a time" shows one member at full width). While a member works you see its last action, then the reply as it is written. An updated answer shows its responses to the comments under it (folded away under the latest answers), and "Changes since step N" shows what changed from that member's version before, word by word. Earlier steps are folded into one line; open it to see each step.
- **You**: your notes and follow-up questions appear in the discussion as your own entries, saying when they go out ("note to both, goes out with step 3"; in a council, "goes out with member A's next message"), until they are sent.
- **At the bottom**, one bar for whatever happens next: write your view for the next step (or pick "Follow-up question"), and press the button that names the next step, such as "Continue: each updates their answer". It takes your note along. While a step runs, the same bar has "Pause after this step" and "Stop".
- **⋯** (top right) holds the folder, each member's model and effort, the round limit and pausing, the commands to resume each member's session, and Move to Trash.

- Discussions started from the page run inside the `fugue web` process. Stopping it (Ctrl-C) stops their agents cleanly; continue them from the page later. Discussions started with `fugue start` in a terminal show up on the page live too.
- To give it its own window and Dock icon, open the page in Safari and choose File → Add to Dock, or in Chrome use "Install page as app".
- **Settings** (bottom left) holds the defaults for new discussions (rounds, pausing, web access, time limit, and the usual two to four members: each one's tool, model, effort and extra arguments), saved to `~/.config/fugue/config.toml`, which `fugue start` reads too. Saving rewrites that file, so comments in it are not kept. Below them is a status check: whether `claude`, `codex` and `dsh` are found and logged in (checked without calling a model), their versions, what fugue switches off in Codex for its runs, desktop notifications (turn them on there to hear when a discussion stops and needs you), whether the background service is running, and where discussions are stored.
- **Move to Trash**, in a discussion's ⋯, moves it to the Trash, where Finder can restore it. The members' own sessions are kept, so `claude --resume`, `codex resume` and the DeepSeek Harness still find them. It isn't offered while the discussion runs.
- The server only listens on 127.0.0.1, and every request must carry a random token that only the page it serves receives, so other websites open in your browser can't use it to start agents.

### Keeping it running in the background (macOS)

So that the page answers without a terminal window left open, install `fugue web` as a background service:

```bash
fugue service install      # start it now, and at every login
fugue service status       # is it running, and does the page answer?
fugue service restart      # after changing fugue's Python code
fugue service uninstall    # stop it and remove it
```

`install` writes a launchd agent to `~/Library/LaunchAgents/ai.fugue.web.plist` and starts it. launchd runs `fugue web --no-open` when you log in and starts it again if it stops. Its output goes to `~/.fugue/web.log`. With the service running, the page is always at <http://127.0.0.1:8765/> (`--port` changes this), and typing `fugue web` just opens it. Discussions started from the page now run in the service; `restart` and `uninstall` stop their agents cleanly, as Ctrl-C would.

- **Run `install` from a shell where `claude` and `codex` work.** The service gets that shell's `PATH`, `LANG` (`en_US.UTF-8` if the shell has none) and `FUGUE_*` settings, so it finds `claude`, `codex`, `uv` and whatever the agents run. Other variables from your shell profile are not passed on. If you change your PATH later, run `install` again; that also replaces a running service.
- **Folder access.** macOS protects Desktop, Documents and Downloads from programs started in the background; in a terminal, the terminal app's permission covers them. The service runs as fugue's Python (`python3.11`, installed by uv), so macOS asks whether `python3.11` may access a protected folder the first time the service reads from one, unless you have already allowed that Python before. Click Allow. If your clone is in one of those folders and the install is editable, that happens during `fugue service install`, which waits for the page to answer. Agents working in a project under Documents or Downloads cause the same question the first time. If no question appears and the page doesn't answer, give that Python Full Disk Access: System Settings → Privacy & Security → Full Disk Access, click +, press Cmd-Shift-G and enter the Python path that `install` and `status` print when the page doesn't answer (`readlink -f ~/.local/share/uv/tools/agent-fugue/bin/python` also shows it). The service tries again every 10 seconds. The permission belongs to that exact Python program, so macOS asks again after uv updates Python 3.11.
- **Login Items.** macOS shows a "Background Items Added" notification when the service is installed, and lists it in System Settings → General → Login Items & Extensions. Switching it off there stops it from starting.
- Changes to the page (HTML, JavaScript, CSS) show up when you reload it; changes to fugue's Python code need `fugue service restart`.

## Command line

| Command | What it does |
|---|---|
| `fugue web` | Open the web page (`--port`, `--no-open`). |
| `fugue service install` | Keep the web page running in the background on macOS; also `status`, `restart`, `uninstall`. |
| `fugue start "question"` | Start a discussion; every member answers, then it goes on to the first pause. `--member` (two to four times) picks the members, `--attach FILE` (repeatable) attaches files, `--no-folder` asks without a project folder, `-n 3` sets the round limit, `--no-pause` runs every step without waiting for you, `--no-web` keeps every member off the web. |
| `fugue continue` | Run the next step (it says which), or retry the agents that failed or were stopped. |
| `fugue next` | Run one more round, also after they agreed or reached the limit. |
| `fugue ask "question"` | Ask a follow-up in the same discussion (`--attach FILE` to attach files). |
| `fugue revote` | A council of three or four: every member answers again and the council votes again on where to start. |
| `fugue note "text"` | Add your own view to the next step, for every member or one (`--to A`, `--to B`, ...). |
| `fugue pause` / `fugue stop` | Pause after the current round / stop the agents now. Works on a discussion running in another terminal or the page. |
| `fugue status` | Who the members are, the steps so far, objection counts, the next step, usage, and the commands to continue either member's session yourself. |
| `fugue show [step]` | Print the replies from a step, numbered from 1 (default: the last; `--member A` for one member). `--prompt` shows exactly what fugue sent instead. |
| `fugue models` | The models and effort levels you can choose for each tool, and what each one uses when you choose nothing (`--refresh` asks Codex again). |
| `fugue list` | All discussions, newest first (`--here` for this folder only). |
| `fugue open` | Open the discussion's `transcript.md` in your default Markdown app. |
| `fugue delete ID` | Move a discussion to the Trash (asks first; `--yes` doesn't). |

Questions and notes can also come from a file (`-f question.md`), from stdin (`-`), or from your `$EDITOR` if you leave the text out. Commands work on the latest discussion in the current folder unless you pass `-d <id>`; the start of the id is enough.

### Continuing in an agent yourself

Both members' sessions are ordinary sessions of their tool. `fugue status` (or "Copy resume command for member A" in a discussion's ⋯ on the page) gives you, for example:

```
cd ~/code/my-repo && claude --resume 6f1c...
cd ~/code/my-repo && codex resume 0199...
```

Run either to continue in that agent's interactive interface, with everything from the discussion in its memory, for instance to have it implement the agreed plan.

## Settings

### Members: tool, model and effort

Each discussion has two to four members, each a tool (`claude`, `codex` or `dsh`) with a model and an effort level. On the page, the New question form starts from your usual line-up in Settings, and what it shows is what runs: add or remove members, or change a member's tool, model, effort or extra arguments, for this discussion only. On the command line, give `--member TOOL[:MODEL[:EFFORT]]` two to four times, leaving out a part to use the tool's own default:

```bash
fugue start "..." --member claude:claude-opus-5-5:high --member codex:gpt-6.1-sol:medium
fugue start "..." --member codex:gpt-6.1-sol:high --member codex:gpt-6-astra:medium  # two Codex members
fugue start "..." --member claude::max --member codex                               # default models
fugue start "..." --member claude --member codex --member dsh:deepseek-flash:high   # a council of three
```

Without `--member`, the line-up saved in Settings (or the config file) is used.

- **Where the lists come from.** Codex reports its own models: fugue asks `codex debug models` (and keeps a copy for a day; "Ask Codex again" on the page or `fugue models --refresh` asks again), so new Codex models show up by themselves, each with the effort levels it accepts. Claude Code and the DeepSeek Harness have no such command, so fugue carries short lists in [src/fugue/models.py](src/fugue/models.py): Claude Fable 5.1, Opus 5.5, Sonnet 5.5 and Haiku 5.5, each taking `low` to `max`; DeepSeek Flash and DeepSeek V4 Pro, each taking `off`, `low`, `high` or `max`. Add a line there when a new one comes out. "Other model id…" takes any id the tool accepts.
- **Effort levels are checked.** The effort menu only offers the levels the chosen model accepts, and `fugue start` refuses a level the model doesn't take.
- **Default means fugue passes nothing**, so the tool uses its own settings: Claude Code its `model` in `~/.claude/settings.json` and its own effort, Codex the `model` and `model_reasoning_effort` in `~/.codex/config.toml`, dsh its profile's model (DeepSeek Flash at high effort unless you changed it). The menus show what that is, and `fugue models` prints it.
- How they reach the tools: Claude gets `--model ID --effort LEVEL`; Codex gets `-m ID -c model_reasoning_effort="LEVEL"`; dsh gets them in its overlay (`model`, `reasoningEffort`).

### Other settings

Each member's extra arguments for its tool (`claude -p`, `codex exec` or `dsh --profile headless`) are in its row on the page. On the command line, `--member-args` goes with the `--member` in the same position; the time limit is `--timeout`:

```bash
fugue start "..." --member claude --member codex --member-args="--add-dir ../shared" --timeout 45
```

Use the `--member-args="..."` form with `=`, since the value itself starts with dashes.

### Defaults for every discussion

They go in `~/.config/fugue/config.toml`:

```toml
timeout_minutes = 30
max_rounds = 2                 # rounds of comments and updates (of review and revision, in a council)
pause_after_each_round = true  # false: run every round without waiting for you
web = true                     # false: neither member may use the web

# The usual members (two to four), in order. Leave out model or effort for the tool's own default.
[[members]]
tool = "claude"
model = "claude-opus-5-5"
effort = "high"

[[members]]
tool = "codex"
model = "gpt-6.1-sol"
effort = "medium"
args = []                      # extra arguments for `codex exec`
```

Values given when starting a discussion override the file; `--member` replaces the whole line-up. Config files from before members, with a `[claude]` and a `[codex]` table, still work, and saving Settings rewrites them as `[[members]]`.

## Files

Discussions are kept outside your projects, in `~/.fugue/discussions/` (or `$FUGUE_HOME/discussions/`), so the agents never come across them while exploring the code.

```
~/.fugue/discussions/20261019-153012-why-does-the-embedding/
  transcript.md                  the whole discussion, readable top to bottom
  state.json                     rounds, sessions, the status of each reply
  events.jsonl                   everything that happened, in order; the page reads this live
  control.json                   round limit, pause and stop settings
  inbox.json                     your notes and requests waiting for the next step
  attachments/                   the files attached to its questions, with text versions of PDF and Word files
  rounds/00-A.prompt.md          exactly what fugue sent to member A in step 1 (files count from 00)
  rounds/00-A.md                 member A's reply in step 1
  logs/                          raw output of each agent call, for debugging
```

Discussions from before members are converted when they are read, and keep their files, which are named after the tool (`00-claude.md`).

## Development

```bash
uv run pytest
scripts/demo-web.sh
```

The tests run whole discussions, and the web server's API, against two fake agents in `tests/fakes/`. The fakes mimic the live output of `claude -p --output-format stream-json` and `codex exec --json` and log exactly what they were sent. `scripts/demo-web.sh` runs the page against the same fakes (on port 8799, storing discussions in `.demo/`), to try the page without calling either agent.

## License

Apache-2.0. See [LICENSE](LICENSE).
