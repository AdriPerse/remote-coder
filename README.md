# remote-coder

A self-hosted coding agent you drive from your phone. Open source end to end,
billed per token, with no subscription and no dependency on any vendor's
proprietary client.

    deepagents (LangGraph)  +  Kimi K3 via Moonshot  +  Telegram

## Why it exists

The alternative stack was Claude Code driven remotely by the Happy daemon.
Three problems: it needs a Claude subscription, the agent is a closed client,
and sessions do not survive a reboot -- a restarted machine leaves dead
sessions in the app that can never be resumed. Here the loop is a LangGraph
graph with a SQLite checkpointer, so a session survives a reboot by
construction, and the model is a per-token API key you can point anywhere.

## Setup

    python -m venv .venv
    .venv/Scripts/pip install -r requirements.txt
    cp .env.example .env      # fill in RC_TELEGRAM_TOKEN

`MOONSHOT_API_KEY` is read from the environment if already set.

## Run

    python cli.py <workdir> "<task>"        # local terminal, no bot needed
    python bot.py                            # the phone-drivable bot

`--auto` drops the approval gates. Without it, every `execute`, `write_file`,
`edit_file` and `delete` waits for your approval.

## Things learned the hard way

- `use_responses_api=False` is REQUIRED. langchain-openai defaults to OpenAI's
  Responses API, which Moonshot does not implement. The failure is an opaque
  HTTP 500, and is documented nowhere in either project's docs.
- `temperature` must be exactly **1** on kimi-k3 and kimi-k2.7-code. LangChain
  defaults to 0.7, so the naive wiring fails closed.
- The shell backend does not inherit the environment, so `git` and `python` do
  not resolve. We pass an explicit environment with the secrets **scrubbed** --
  otherwise the agent's first shell command can read its own API key.
- File tools and the shell disagree about paths (`virtual_mode=True` maps `/`
  to the repo root while the shell prints host paths). Kept anyway for the
  traversal guardrail; the prompt states the contract explicitly.
- Two `edit_file` calls on the same file in one turn corrupt it (upstream
  bug #5589).
- `deepagents` ships breaking changes in minor releases and its checkpoint
  format is a one-way door. Pinned exactly, on purpose.
