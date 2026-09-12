# ApiPi

HTTP API for hosted agents. A session is a conversation that can use tools
and a computer. Pi runs the loop. Any OpenAI-compatible model endpoint works.

Repo: [github.com/GEKI-AI/apipi](https://github.com/GEKI-AI/apipi)

Python 3.13+, [uv](https://docs.astral.sh/uv/) only.

```
uv sync
```

Docs site (not the apipi package):

```
uv run --no-project --with-requirements requirements-docs.txt mkdocs serve
```

The useful contribution is a detailed
[issue](https://github.com/GEKI-AI/apipi/issues). See
[CONTRIBUTING.md](CONTRIBUTING.md).

| Path | What |
| --- | --- |
| [CONSTITUTION.md](CONSTITUTION.md) | Project rules |
| [AGENTS.md](AGENTS.md) | Rules for agents |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Issues, git, pull requests |
| [.agents/skills/](.agents/skills/) | How agents plan, implement, and review |
| [docs/](docs/) | Specs |
| [docs/decisions/](docs/decisions/) | Architecture decisions |
| [mkdocs.yml](mkdocs.yml) | Docs site |
| [requirements-docs.txt](requirements-docs.txt) | Docs site deps (not apipi) |
| [examples/](examples/) | Tavily and Playwright MCP |

## Stack

- Gateway: Python 3.13, FastAPI
- Store: Postgres
- Harness: Pi (one process per session)
- Run mode: `host` \| `jail` \| `microvm` (default `jail`)
- Default environment: session directory (`openai_hosted`)
- MCP, function tools, and skills

## License

MIT. See [LICENSE](LICENSE).
