# fugue

fugue asks two to four coding agents the same question and has them check each other's answers, step by step, until no one objects or a round limit is reached. The members can be Claude Code, Codex and the DeepSeek Harness, in any mix, each with the model and effort level you choose. You watch the discussion live on a local web page (or in the terminal), add your own view whenever you want, and move it on when you're ready. Like the voices of a fugue, each member first states its own answer, then they answer each other until the lines fit together.

## Why use it

Several agents asked the same question cover more edge cases than any one of them alone. Doing that by hand means copying each reply into the other agents' sessions, in the right order and with the right framing, and keeping track of who has seen what; after a round or two it gets hard to manage. fugue does the shuttling, shows you what each member is doing while it works, and keeps the record.

Each member runs in its own command-line tool, so it keeps its normal tools, project instructions (`CLAUDE.md`, `AGENTS.md`) and its ability to read the code. Members can read files and run commands but can't change anything, so fugue suits analysis: questions about a codebase, design reviews, or checking a document or a resume.

## Quick start

You need [uv](https://docs.astral.sh/uv/), and on your PATH the tools your members will use, each signed in the way you normally use it:

- Claude Code (`claude`) and Codex (`codex`), with their own logins;
- for DeepSeek members, the DeepSeek Harness (`dsh`, installed with `npm install -g @deepseek-ai/dsh`), with a DeepSeek API key saved on dsh's Models page.

fugue uses each tool's own sign-in and keeps no API keys of its own. Install it:

```bash
git clone https://github.com/zherendong/agent-fugue.git
cd agent-fugue
uv tool install --python 3.11 --editable .
```

`--editable` means changes to the source (for example to the prompts or the page) take effect without reinstalling. Remove it with `uv tool uninstall agent-fugue`.

Start the page:

```bash
fugue web
```

It opens <http://127.0.0.1:8765/>. Click **New question**, write your question, set **Folder** to the project the members should work in (or click **No folder**), and click **Ask**. Unless you change it, a discussion has one Claude Code member and one Codex member, runs up to two rounds, and pauses after every step for you to continue. **Settings** (bottom left) shows whether fugue found each tool and its sign-in.

The same from the terminal:

```bash
cd ~/code/my-repo
fugue start "Why does the embedding cache miss on cold start? Propose a fix."
fugue continue    # run the next step; repeat until the discussion finishes
```

`fugue start` runs the first step and stops at the first pause; `fugue status` says where a discussion stands and what comes next. Discussions started in a terminal show up on the page live too.

## How a discussion works

### Steps and rounds

A discussion moves in steps, numbered from 1 on the page, in the transcript and in `fugue status`. Step 1 is the independent answers: every member gets your question unchanged, in its own session, and answers without seeing the others. What follows depends on how many members there are. A *round*, as in the round limit, is a pair of steps after the answers: a comment step and an update step with two members, or a review and a revision with three or four. The default round limit is 2.

### Two members: comment and update

- **Comment steps.** Each member reads the other's current answer and comments on it: what it got right that the commenter missed, what is wrong or unsupported (checking the claims with its tools rather than trusting them, and citing what it checked), and what is missing for what you asked. Each comment ends with `Objections: N`, the problems that would change the answer's conclusions or recommendations, and `Suggestions: N`, everything smaller, including anything beyond your question.
- **Update steps.** Each member reads the comments on its own answer and writes its full updated answer, keeping its layout so the changes are easy to see. The answer stands on its own; after it, under a `## Responses` heading, the member says for each objection whether it accepts it, accepts part of it, or turns it down, and why. What is still disputed goes there, not into the answer.

Comments and updates alternate, so a comment is always about the other's answer as it stands now.

### Three or four members: vote, merge, review, revise

Everyone reading and rewriting everyone's answer at every step would cost too much, so a council of three or four works from one shared answer:

- **Vote.** Each member ranks the other members' answers (never its own), each seeing them in its own random order, and says what each gets right and which problems would change its conclusions. Every ballot gives an answer one point for each answer ranked below it; the answer with the most points is the starting point. Between answers with equal points, the one ranked first more often wins, and a tie that remains is drawn at random. A ballot without a readable ranking isn't counted, and the page says why.
- **Merge.** The author of the starting point merges the other members' best points into it (it read their answers when it voted), fixes the problems the ballots found, and lists the points it didn't take, with reasons, under `## Points not taken`.
- **Review.** The other members review the result: objections (including their own points left out or misrepresented), suggestions, and whether another starting point would clearly have been better.
- **Revise.** The author writes the full revised answer, then, under `## Responses`, a response to each objection and the points it still hasn't taken.

Reviews and revisions alternate, and the last revision is reviewed too, so you see what is still open. When most reviewers say another starting point would have been better, or the objections haven't gone down over the last two reviews, every member answers again and the council votes again; this happens on its own once per question, and **Vote again** on the page (or `fugue revote`) asks for it whenever you like after a vote. Every member votes and every member but the author reviews, so no answer wins by who wrote it.

### How it ends

A discussion ends when a comment step (in a council, a review) has no objections, or when the round limit is reached.

- **No objections, but suggestions.** Each member whose answer got suggestions (in a council, the author) writes one closing version that takes in the suggestions it accepts, and lists the others under `## Responses`. The discussion ends there.
- **The result** is each member's latest answer, or the council's one shared answer. When the round limit ended the discussion, the page also shows the objections from the last comments with each author's responses, or in a council the last reviews that still object.

### Rewrites are checked against your material

When an answer rewrites, summarizes or quotes something you provided (an attached file, text in the question, a file in the folder), whoever comments on it or reviews it goes through it line by line against the source. A sentence that claims more than the source is an objection, quoted next to the source's own wording: a stronger role or credit for someone, a cause, method or number the source doesn't give, a dropped qualifier such as "estimated", an added list item, or a placeholder written as if it were known. Voters count such claims as problems, and every member checks its own changed claims before handing in an update, merge, revision or closing version. A change of wording that keeps the meaning is not an objection.

### Where you come in

fugue pauses and names the next step, for example "Next: each comments on the other's answer", and you move it on when you're ready. Two members pause after every step, except that an agreement with suggestions goes straight on to the closing version. A council pauses after its vote (to see the points, and start from another answer if you like) and after each revision. At any time you can:

- add a note to every member or just one; it goes into the next message fugue sends that member;
- stop the members mid-step;
- ask for one more round, also after they agreed or reached the limit;
- ask a follow-up question, which starts again with independent answers;
- change the round limit, or switch off pausing to let the discussion run to the end by itself. Changing these never starts the members on its own; Continue does.

If a member fails or is stopped, the discussion waits; continuing retries just that member with the same message.

### Sessions, letters and what each member has seen

fugue runs each tool headless (`claude -p`, `codex exec`, `dsh --profile headless`). Each member has one session for the whole discussion, and fugue resumes it at every step (`claude --resume`, `codex exec resume`, `dsh … --session-id`), so the member remembers its own earlier reasoning and fugue only sends what is new. fugue records which replies each member has already received, so nothing is forwarded twice or skipped. While a member works, fugue reads its live output (`--output-format stream-json` from Claude Code, `--json` from Codex and dsh) and shows what it is doing: "Read src/cache.py", "$ rg -n cache src", how far Claude has got while it thinks ("Thinking · about 12k tokens so far"; at high effort it can think for minutes before writing), and then the reply as it is written.

Members are never told which tool or model the others run. Each gets a letter for the whole discussion (A to D), drawn at random when it starts, so the first member you choose isn't always A. Every message after your first question tells the member its own letter and the others'; forwarded replies are marked with their author and step (`from="member B" step="2"`), so a member that cites a step means the same one you see; and members write about themselves as "I" and refer to each other only by letter, which means the same member to whoever reads a reply. The page, the transcript and `fugue status` show you who is who, for example "Claude (member A)", or "Codex · gpt-6.1-sol · high (member B)" when two members use the same tool.

Members running the same model tend to make the same mistakes and agree on them, so they catch less; different tools or models work best, and the page warns when two members run the same model.

The exact wording sent to the members is in [src/fugue/prompts.py](src/fugue/prompts.py); edit it there to change how they talk to each other.

## Reference

### What the members may do

Members may read files and run commands, but not change anything: several agents editing the same checkout at the same time would trip over each other, and fugue is meant for analysis. Each runs in its tool's own sandbox, where macOS itself refuses writes. These settings apply to fugue's runs only; your own settings for each tool are not changed.

- **Claude Code** runs in its sandbox, set with `--settings`. Nobody can answer permission prompts in a headless run, so Claude runs in "don't ask" mode, where anything that would ask is refused; Bash is allowed outright because every command runs inside the sandbox. The sandbox refuses writes to the project and your home folder (the temp folder stays writable, as some tools need it), refuses network access, refuses to start a command outside itself, and the run fails rather than going ahead if the sandbox can't start. Claude's own file tools can't write (`Edit`, `Write` and `NotebookEdit` are turned off), and its Read tool may read any file except in `~/.ssh` and `~/.aws`. If your Claude settings list `sandbox.excludedCommands`, those commands would run outside the sandbox, so fugue then stops allowing Bash outright and Claude only gets the commands the sandbox approves by itself.
- **Codex** runs in its `read-only` sandbox, which also refuses its own file editor (`apply_patch`). What in your Codex setup acts outside that sandbox is switched off: the apps connector (`--disable apps`), and every plugin and MCP server that is on in `~/.codex/config.toml` (computer use, Chrome, `node_repl` and so on), each with a `-c ….enabled=false` override. Your model and effort settings still apply.
- **The DeepSeek Harness** runs in its `read-only` permission mode (`DSH_PERMISSION_MODE=read-only`), in its own sandbox (Seatbelt on macOS), with approvals refused since nobody can answer them. A settings overlay written for each run (`--patch`) sets the member's model and effort, turns off its telemetry (which by default sends session records after a user's feedback) and its extra model call for a session title, and turns on dsh's store of saved keys, which its headless mode leaves off, so runs use the API key you saved in dsh.

**The web.** "Members may use the web" (on by default; `--no-web` on the command line, `web = false` in the config file) applies to every member of a discussion:

- *On:* Claude may use WebSearch and WebFetch; its fetches come from your Mac, so fetches from `localhost` and `127.0.0.1` are refused, while other addresses only your Mac can reach (other local servers, your network) are not blocked. Codex keeps its web search, which runs on OpenAI's servers. The DeepSeek Harness keeps its web tools.
- *Off:* Claude's WebSearch and WebFetch are switched off, Codex runs with `web_search="disabled"`, and DeepSeek's web tools are switched off in its overlay.

With the web on, a page a member reads could contain instructions aimed at it, for example to send the contents of a file it read somewhere. Switch the web off for sensitive projects.

The exact flags are in `claude_read_only_args`, `codex_isolation_args` and `dsh_overlay` in [src/fugue/agents.py](src/fugue/agents.py).

**Sign-ins.** Each member uses its tool's own sign-in. Claude Code and Codex use their logins (`claude` and `/login`, or `codex login`). DeepSeek members find the key where your own dsh does: saved on dsh's Models page (in `~/.dsh/.credentials.yaml`), then `DEEPSEEK_API_KEY` in the environment, then `~/.dsh/.env` as `DEEPSEEK_API_KEY=…`. The background service doesn't get your shell's variables, so for it the key has to be saved in dsh or in `~/.dsh/.env`. Settings' status check says what it finds, without reading the key.

### Attached files

A question or a follow-up can come with files: PDFs, Word files, images, text, up to 50 MB per question. They are kept in the discussion's own `attachments/` folder, and each member's first message for that question lists them after the question with their kind, size and path.

The members run read-only, so they can't install a PDF reader for themselves; fugue does the converting when a file arrives:

- PDF and Word files get a text version next to them. For PDFs, pypdf reads each page by where its letters sit, so words drawn a few letters at a time stay whole. A PDF without a text layer says so.
- A PDF's first 10 pages are also rendered as PNG images, with pypdfium2; a longer PDF's note says how many pages were left out.
- Each member is told what suits its tool: Claude Code opens the PDF itself, Codex is handed the page images and attached images with its message (`--image`), and the DeepSeek Harness is given the page images' paths to open on image-capable models such as DeepSeek Flash.
- Every member is told that the original is what counts and that a text version can get spacing, order or symbols wrong, so layout and formatting are judged from the original or its page images.

A question doesn't need a project folder: without one (`--no-folder`, or **No folder** on the page), the members work in the attachments folder, which suits reviewing a resume or a design document.

### The web page

`fugue web` serves the page at <http://127.0.0.1:8765/> and opens it. On the left is every discussion, from every project, with a dot on the ones that are working or waiting for you. On the right is the selected discussion:

- **At the top**, the question and one line saying where it stands ("Paused after step 2", "Agreed in step 4"). Attached files are listed under the question; click one to see it on the page (images and PDFs as they are, Word files as the text the members get) or to download it.
- **The answers**: the step in progress, or once it has finished, each member's latest answer side by side, or the council's answer ("One at a time" shows one member at full width). While a member works you see its last action, then the reply as it is written. An updated answer shows its responses to the comments under it (folded away under the latest answers), and "Changes since step N" shows what changed from that member's previous version, word by word. A council's vote shows each answer's points, and right after the vote "Start from this one" picks another answer as the starting point. Earlier steps are folded into one line; open it to see each step.
- **Inspect context**, under each reply, shows the exact message the member got in that step, and everything fugue had sent it up to then, with each part marked by where it came from.
- **You**: your notes and follow-up questions appear in the discussion as your own entries, saying when they go out ("note to both, goes out with step 3"; in a council, "goes out with member A's next message"), until they are sent.
- **At the bottom**, one bar for whatever happens next: write your view for the next step (or pick "Follow-up question"), and press the button that names the next step, such as "Continue: each updates their answer". It takes your note along, and ⌘↩ presses it. While a step runs, the same bar has "Pause after this step" and "Stop".
- **⋯** (top right) holds the folder, each member's model and effort, the round limit and pausing, the commands to resume each member's session, and Move to Trash.

A few more things to know:

- Discussions started from the page run inside the `fugue web` process. Stopping it (Ctrl-C) stops their agents cleanly; continue them from the page later.
- **Move to Trash** moves a discussion to the Trash, where Finder can restore it (on a system without the `trash` command, to `~/.fugue/trash/`). The members' own sessions are kept, so `claude --resume`, `codex resume` and the DeepSeek Harness still find them. It isn't offered while the discussion runs.
- To give the page its own window and Dock icon, open it in Safari and choose File → Add to Dock, or in Chrome use "Install page as app".
- The server only listens on 127.0.0.1, and every request must carry a random token that only the page it serves receives, so other websites open in your browser can't use it to start agents.

### Settings

**Settings**, at the bottom left of the page, holds the defaults for new discussions: the round limit, pausing, web access, the time limit for each member's step, and the usual two to four members, each with its tool, model, effort and extra arguments. They are saved to `~/.config/fugue/config.toml` (or `$FUGUE_CONFIG`), which `fugue start` reads too. Saving rewrites that file, so comments in it are not kept.

Below them is a status check: whether `claude`, `codex` and `dsh` are found and signed in (checked without calling a model), their versions, what fugue switches off in Codex for its runs, desktop notifications (turn them on there to hear when a discussion stops and needs you), whether the background service is running, and where discussions are stored.

#### Members: tool, model and effort

Each discussion has two to four members, each a tool (`claude`, `codex` or `dsh`) with a model and an effort level; members can use the same tool, for example two Codex members with different models. On the page, the New question form starts from your usual line-up in Settings, and what it shows is what runs: add or remove members, or change a member's tool, model, effort or extra arguments, for this discussion only. On the command line, give `--member TOOL[:MODEL[:EFFORT]]` two to four times, leaving out a part to use the tool's own default:

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
- **How they reach the tools.** Claude gets `--model ID --effort LEVEL`; Codex gets `-m ID -c model_reasoning_effort="LEVEL"`; dsh gets them in its overlay (`model`, `reasoningEffort`).

Each member's extra arguments for its tool (`claude -p`, `codex exec` or `dsh --profile headless`) are in its row on the page. On the command line, `--member-args` goes with the `--member` in the same position; the time limit is `--timeout`:

```bash
fugue start "..." --member claude --member codex --member-args="--add-dir ../shared" --timeout 45
```

Use the `--member-args="..."` form with `=`, since the value itself starts with dashes.

#### The config file

```toml
timeout_minutes = 30           # give up on a member's step after this long
max_rounds = 2                 # rounds of comments and updates (of review and revision, in a council)
pause_after_each_round = true  # false: run every step without waiting for you
web = true                     # false: no member may use the web

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

### Keeping the page running in the background (macOS)

So that the page answers without a terminal window left open, install `fugue web` as a background service:

```bash
fugue service install      # start it now, and at every login
fugue service status       # is it running, and does the page answer?
fugue service restart      # after changing fugue's Python code
fugue service uninstall    # stop it and remove it
```

`install` writes a launchd agent to `~/Library/LaunchAgents/ai.fugue.web.plist` and starts it. launchd runs `fugue web --no-open` when you log in and starts it again if it stops. Its output goes to `~/.fugue/web.log`. With the service running, the page is always at <http://127.0.0.1:8765/> (`install --port` changes this), and typing `fugue web` just opens it. Discussions started from the page then run in the service; `restart` and `uninstall` stop their agents cleanly, as Ctrl-C would.

- **Run `install` from a shell where the members' tools work.** The service gets that shell's `PATH`, `LANG` (`en_US.UTF-8` if the shell has none) and `FUGUE_*` settings, so it finds `claude`, `codex`, `dsh`, `uv` and whatever the agents run. Other variables from your shell profile are not passed on. If you change your PATH later, run `install` again; that also replaces a running service.
- **Folder access.** macOS protects Desktop, Documents and Downloads from programs started in the background; in a terminal, the terminal app's permission covers them. The service runs as fugue's Python (`python3.11`, installed by uv), so macOS asks whether `python3.11` may access a protected folder the first time the service reads from one, unless you have already allowed that Python. Click Allow. If your clone is in one of those folders and the install is editable, that happens during `fugue service install`, which waits for the page to answer. Agents working in a project under Documents or Downloads cause the same question the first time. If no question appears and the page doesn't answer, give that Python Full Disk Access: System Settings → Privacy & Security → Full Disk Access, click +, press Cmd-Shift-G and enter the Python path that `install` and `status` print when the page doesn't answer (`readlink -f ~/.local/share/uv/tools/agent-fugue/bin/python` also shows it). The service tries again every 10 seconds. The permission belongs to that exact Python program, so macOS asks again after uv updates Python 3.11.
- **Login Items.** macOS shows a "Background Items Added" notification when the service is installed, and lists it in System Settings → General → Login Items & Extensions. Switching it off there stops it from starting.
- Changes to the page (HTML, JavaScript, CSS) show up when you reload it; changes to fugue's Python code need `fugue service restart`.

### Command line

| Command | What it does |
|---|---|
| `fugue web` | Open the web page (`--port`, `--no-open`). |
| `fugue service install` | Keep the web page running in the background on macOS; also `status`, `restart`, `uninstall`. |
| `fugue start "question"` | Start a discussion; every member answers, then it goes on to the first pause. `--member` (two to four times) picks the members, `--attach FILE` (repeatable) attaches files, `--cwd DIR` sets the folder (default: the current one), `--no-folder` asks without one, `-n 3` sets the round limit, `--no-pause` runs every step without waiting for you, `--no-web` keeps every member off the web, `--title` names the discussion. |
| `fugue continue` | Run the next step (it says which), or retry the members that failed or were stopped. |
| `fugue next` | Run one more round, also after they agreed or reached the limit. |
| `fugue ask "question"` | Ask a follow-up in the same discussion (`--attach FILE` to attach files). |
| `fugue revote` | A council of three or four: every member answers again and the council votes again on where to start. |
| `fugue note "text"` | Add your own view to the next step, for every member or one (`--to A`, `--to B`, ...). |
| `fugue pause` / `fugue stop` | Pause after the current step / stop the members now. Works on a discussion running in another terminal or the page. |
| `fugue status` | Who the members are, the steps so far, objection counts, the next step, usage, and the commands to continue each member's session yourself. |
| `fugue show [step]` | Print the replies from a step, numbered from 1 (default: the last; `--member A` for one member). `--prompt` shows exactly what fugue sent instead. |
| `fugue models` | The models and effort levels you can choose for each tool, and what each one uses when you choose nothing (`--refresh` asks Codex again). |
| `fugue list` | All discussions, newest first (`--here` for this folder only). |
| `fugue open` | Open the discussion's `transcript.md` in your default Markdown app. |
| `fugue delete ID` | Move a discussion to the Trash (asks first; `--yes` doesn't). |

Questions and notes can also come from a file (`-f question.md`), from stdin (`-`), or from your `$EDITOR` if you leave the text out. Commands work on the latest discussion in the current folder unless you pass `-d <id>`; the start of the id is enough. Commands that run steps print each reply as it finishes; `-q` prints only the progress.

### Continuing in an agent yourself

The members' sessions are ordinary sessions of their tool. `fugue status` (or "Copy resume command for member A" in a discussion's ⋯ on the page) gives you, for example:

```
cd ~/code/my-repo && claude --resume 6f1c...
cd ~/code/my-repo && codex resume 0199...
cd ~/code/my-repo && dsh --profile headless --patch ~/.fugue/discussions/.../logs/03-C.dsh-patch.yml --session-id session-7a2e... "<your message>"
```

Run the Claude or Codex one to continue in that agent's interactive interface, with everything from the discussion in its memory, for instance to have it implement the agreed plan. The DeepSeek Harness opens a session by id only in headless mode, so its command sends one message (put yours in place of `<your message>`) and prints the answer; run it again for the next message. It uses the settings of the member's last run in the discussion (model, effort, and the API key saved in dsh), but not the discussion's read-only mode, so the agent can change files in the project.

### Where files are kept

Discussions are kept outside your projects, in `~/.fugue/discussions/` (or `$FUGUE_HOME/discussions/`), so the agents never come across them while exploring the code.

```
~/.fugue/discussions/20261019-153012-why-does-the-embedding/
  transcript.md                  the whole discussion, readable top to bottom
  state.json                     members, steps, sessions, the status of each reply
  events.jsonl                   everything that happened, in order; the page reads this live
  control.json                   round limit, pause and stop settings
  inbox.json                     your notes and requests waiting for the next step
  attachments/                   the attached files, with text versions of PDF and Word files and PDF page images
  rounds/00-A.prompt.md          exactly what fugue sent to member A in step 1 (files count from 00)
  rounds/00-A.md                 member A's reply in step 1
  logs/                          raw output of each agent call, and each dsh run's settings overlay
```

Discussions from before members are converted when they are read, and keep their files, which are named after the tool (`00-claude.md`).

fugue finds each tool on your PATH; `FUGUE_CLAUDE_BIN`, `FUGUE_CODEX_BIN` and `FUGUE_DSH_BIN` point it at a specific program instead. `FUGUE_PORT` sets the default port for `fugue web` and `fugue service install`.

### Development

```bash
uv run pytest
scripts/demo-web.sh
```

The tests run whole discussions, and the web server's API, against fake versions of all three tools in `tests/fakes/`. The fakes mimic the live output of `claude -p --output-format stream-json`, `codex exec --json` and `dsh --profile headless --json`, and log exactly what they were sent. `scripts/demo-web.sh` runs the page against the same fakes (on port 8799, storing discussions in `.demo/`), to try the page without calling any agent.

Each change that users would notice gets a line in [CHANGELOG.md](CHANGELOG.md).

## License

Apache-2.0. See [LICENSE](LICENSE).
